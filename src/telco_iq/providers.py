from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Protocol

import duckdb
import pyarrow as pa

from telco_iq.catalog import Catalog, digest
from telco_iq.contracts import Artifact, Clocks, QueryNode, Receipt, Scope, SourceClock, Status
from telco_iq.errors import LabError


class QueryProvider(Protocol):
    mode: str

    async def query(self, node: QueryNode, scope: Scope) -> tuple[Artifact, Receipt]: ...


def scalar(value: object) -> str | int | float | bool | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise LabError(f"Unrecognized source type: {type(value).__name__}")


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class OfflineProvider:
    mode = "OFFLINE_SYNTHETIC"

    def __init__(self, catalog: Catalog, data_path: Path):
        self.catalog = catalog
        self.data_path = data_path.resolve()

    async def query(self, node: QueryNode, scope: Scope) -> tuple[Artifact, Receipt]:
        if scope.mode != "OFFLINE_SYNTHETIC":
            raise LabError("Offline provider cannot serve live identities", Status.BLOCKED_AUTH)
        if node.model not in scope.models:
            raise LabError("Source not authorized by server scope", Status.BLOCKED_AUTH)
        cancel = threading.Event()
        lock = threading.Lock()
        connections: list[duckdb.DuckDBPyConnection] = []
        worker = asyncio.create_task(asyncio.to_thread(self._query, node, scope, cancel, lock, connections))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel.set()
            with lock:
                for con in connections:
                    con.interrupt()
            # Wait for the interrupted owned connection to close, not an orphaned source query.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except (duckdb.InterruptException, LabError):
                    break
            try:
                worker.result()
            except (duckdb.InterruptException, LabError):
                pass
            raise

    def _query(
        self, node: QueryNode, scope: Scope, cancel: threading.Event,
        lock: threading.Lock, connections: list[duckdb.DuckDBPyConnection],
    ) -> tuple[Artifact, Receipt]:
        start = perf_counter()
        sql, parameters = self.catalog.sql(node)
        path = self.data_path / f"{node.table}.parquet"
        if not path.is_file():
            raise LabError(f"Synthetic table not generated: {node.table}", Status.BLOCKED_CAPABILITY)
        manifest_path = self.data_path / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        data_revision = manifest["content_sha256"]
        metadata = next((t for t in manifest["tables"] if t["id"] == node.table), None)
        if metadata is None or metadata["file"] != path.name:
            raise LabError("Source file absent from approved synthetic manifest")
        if path.stat().st_size != metadata["bytes"] or file_hash(path) != metadata["file_sha256"]:
            raise LabError("Source file changed without matching manifest refresh")
        watermark = manifest["watermarks"][node.model]
        through = datetime.fromisoformat(watermark)
        for predicate in node.filters:
            if predicate.column == "month" and predicate.op in ("eq", "in"):
                for value in predicate.values:
                    period = date.fromisoformat(str(value))
                    end = datetime(
                        period.year + (period.month == 12), period.month % 12 + 1, 1, tzinfo=timezone.utc,
                    )
                    if end > through:
                        raise LabError("Requested month is not complete at source watermark", Status.PARTIAL)
        clocks = Clocks(data=data_revision, model=self.catalog.revision, ontology="NONE", context="offline-v1")
        coverage = {}
        coverage_evidence = None
        with duckdb.connect() as con:
            with lock:
                connections.append(con)
            try:
                if cancel.is_set():
                    raise LabError("Query cancelled before source execution", Status.TIMED_OUT)
                # Paths/expressions originate in trusted server contracts, not generated code.
                con.execute("SET memory_limit='256MB'")
                con.execute("SET threads=2")
                con.execute("SET TimeZone='UTC'")
                con.read_parquet(str(path)).create_view(node.table)
                source_columns = {row[0]: row[1] for row in con.execute(f'DESCRIBE "{node.table}"').fetchall()}
                diagnostic = self.catalog.coverage_sql(
                    node, metadata, source_columns, profile=manifest.get("profile", "UNKNOWN"),
                )
                if diagnostic is not None:
                    coverage_sql, coverage_parameters = diagnostic
                    values = con.execute(coverage_sql, coverage_parameters).fetchone()
                    coverage = dict(zip(("matched_source_rows", "unknown_source_rows"), values, strict=True))
                    coverage_evidence = {
                        "query": coverage_sql, "parameters": coverage_parameters, **coverage,
                    }
                result = con.execute(sql + f" LIMIT {node.max_rows + 1}", parameters)
                table = result.to_arrow_table()
                # Fixed UTC offsets preserve instants without optional OS/pytz timezone databases.
                schema = pa.schema([
                    field.with_type(pa.timestamp(field.type.unit, tz="+00:00"))
                    if pa.types.is_timestamp(field.type) and field.type.tz else field
                    for field in table.schema
                ])
                rows = [
                    {name: scalar(value) for name, value in row.items()}
                    for row in table.cast(schema).to_pylist()
                ]
            finally:
                with lock:
                    connections.clear()
        if file_hash(path) != metadata["file_sha256"] or manifest != json.loads(manifest_path.read_text()):
            raise LabError("Source changed during retrieval; requery a consistent revision", Status.PARTIAL)
        truncated = len(rows) > node.max_rows
        rows = rows[:node.max_rows]
        status = Status.PARTIAL if truncated or coverage.get("unknown_source_rows", 0) else Status.COMPLETE
        warnings = ["Local SQL fixture arithmetic. Not source DAX, live MCP, or source RLS."]
        if coverage.get("unknown_source_rows", 0):
            warnings.append(
                f"Selected source rows contain unknown/incomplete inputs or mapping coverage "
                f"({coverage['unknown_source_rows']} of {coverage['matched_source_rows']}); "
                "aggregate values are known subsets, not complete totals or compliance."
            )
        evidence_id = digest({"node": node.model_dump(), "scope": scope.model_dump(), "clocks": clocks.model_dump()})
        artifact = Artifact(
            id=node.id, rows=rows, status=status, scope=scope, clocks=clocks,
            source_models=[node.model], source_tables=[node.table],
            **self.catalog.query_schema(node).model_dump(), evidence_ids=[evidence_id],
            source_clocks=[SourceClock(
                model=node.model, table=node.table,
                data=digest({
                    entry["id"]: entry["content_sha256"] for entry in manifest["tables"]
                    if entry["id"] in self.catalog.models[node.model]["tables"]
                }),
                model_revision=clocks.model, table_data=metadata["content_sha256"],
            )],
            warnings=warnings, coverage={"returned": len(rows), **coverage},
        )
        receipt = Receipt(
            id=evidence_id, node_id=node.id, lane=self.mode, tool="DuckDB.execute",
            model=node.model, query=sql + f" LIMIT {node.max_rows + 1}", parameters=parameters,
            query_hash=digest({"sql": sql + f" LIMIT {node.max_rows + 1}", "parameters": parameters}),
            elapsed_ms=(perf_counter() - start) * 1000, returned_rows=len(rows), status=status,
            completeness_evidence="Local result fetched with limit+1 sentinel" + (
                "; selected raw-source coverage: " + json.dumps(coverage_evidence, sort_keys=True)
                if coverage_evidence is not None else ""
            ),
            clocks=clocks, source_watermark=watermark, warnings=artifact.warnings,
        )
        return artifact, receipt
