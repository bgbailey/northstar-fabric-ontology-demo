from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from telco_iq.contracts import (
    QueryNode,
    ResultSchema,
    Scalar,
    SourceColumn,
    SourceFilter,
    SourcePopulation,
    Status,
)
from telco_iq.errors import LabError


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@dataclass(frozen=True)
class CompiledDax:
    """Application compiler contract, not fields claimed from a service response."""

    text: str
    columns: tuple[str, ...]
    grain: tuple[str, ...]
    max_rows: int | None
    witnessed: bool
    identity_metrics: tuple[str, ...]
    float64_identity_metrics: tuple[str, ...] = ()
    column_types: dict[str, str] = field(default_factory=dict)
    metric_bindings: dict[str, str] = field(default_factory=dict)
    iso_temporal_columns: tuple[str, ...] = ()
    boolean_string_columns: tuple[str, ...] = ()


def compatible_populations(
    left: list[SourcePopulation], right: list[SourcePopulation],
) -> list[SourcePopulation]:
    if not left or not right:
        raise LabError("Source population provenance UNKNOWN; transfer blocked", Status.BLOCKED_CAPABILITY)
    try:
        entries = [SourcePopulation.model_validate(entry.model_dump()) for entry in left + right]
    except (ValidationError, AttributeError) as error:
        raise LabError("Invalid source population provenance", Status.BLOCKED_CAPABILITY) from error
    first = entries[0]
    for entry in entries[1:]:
        if any(getattr(entry, key) != getattr(first, key) for key in (
            "evidence_kind", "profile", "seed", "generator_revision", "identity_revision", "shared_map_hashes",
        )):
            raise LabError("Incompatible source population lineage", Status.BLOCKED_CAPABILITY)
    by_model = {}
    for entry in entries:
        previous = by_model.get(entry.model)
        if previous is not None and (
            previous.binding_hash != entry.binding_hash
            or previous.manifest_hash != entry.manifest_hash
            or previous.table_hashes != entry.table_hashes
        ):
            raise LabError("Source population binding/manifest changed; requery", Status.BLOCKED_CAPABILITY)
        if any(
            other.model != entry.model and other.binding_hash == entry.binding_hash
            for other in by_model.values()
        ):
            raise LabError("Source population aliases reuse one actual binding", Status.BLOCKED_CAPABILITY)
        by_model[entry.model] = entry
    return list(by_model.values())


class Catalog:
    def __init__(self, path: Path):
        self.raw = json.loads(path.read_text(encoding="utf-8"))
        self.revision = digest(self.raw)
        self.models = {m["id"]: m for m in self.raw["models"]}
        self.tables = {t["id"]: t for t in self.raw["tables"]}
        self.metrics = {m["id"]: m for m in self.raw["metrics"]}

    def source_coverage_inputs(self, node: QueryNode) -> list[str]:
        """Required source inputs, not arbitrary nullable business attributes."""
        if node.model != "enterprise":
            return []
        inputs = {
            "service_month": {
                "contracted_services": ["contracted"],
                "monitored_service_minutes": ["monitored_minutes"],
                "breached_services": ["breached"],
                "sla_compliance": ["contracted", "breached"],
                "contracted_recurring_revenue": ["recurring_revenue_cents"],
            },
            "sla_evaluation": {
                "eligible_credits": ["eligible_credit_cents"],
                "credit_exposure": ["eligible_credit_cents", "applied_credit_cents"],
            },
            "applied_credit": {"applied_credits": ["amount_cents"]},
        }.get(node.table, {})
        selected = (
            [inputs[metric.removeprefix("enterprise.")] for metric in node.metrics]
            if node.metrics else list(inputs.values())
        )
        columns = list(dict.fromkeys(column for group in selected for column in group))
        if not node.metrics and node.table == "sla_evaluation":
            columns.append("outage_minutes")
        return columns

    def coverage_sql(
        self, node: QueryNode, metadata: dict, columns: dict[str, str], *, profile: str,
    ) -> tuple[str, list[Scalar]] | None:
        """Check the same filtered raw rows before SUM/ratios can discard NULL."""
        required = self.source_coverage_inputs(node)
        if not required:
            return None
        unknown = [f"{identifier(column)} IS NULL" for column in required]
        mapping_dependent = not node.metrics or bool(set(node.metrics) & {
            "enterprise.monitored_service_minutes", "enterprise.breached_services",
            "enterprise.sla_compliance", "enterprise.eligible_credits", "enterprise.credit_exposure",
        })
        if mapping_dependent and node.table in {"service_month", "sla_evaluation"}:
            diagnostic = metadata.get("coverage")
            if diagnostic is not None:
                if diagnostic != {"column": "coverage_complete", "kind": "effective_mapping"} or (
                    columns.get("coverage_complete") != "BOOLEAN"
                ):
                    raise LabError("Invalid source coverage diagnostic contract", Status.BLOCKED_CAPABILITY)
                unknown.append('"coverage_complete" IS NOT TRUE')
            elif profile == "showcase-v2":
                raise LabError(
                    "Source mapping coverage proof absent in historical showcase; use a new diagnostic revision",
                    Status.BLOCKED_CAPABILITY,
                )
        if not set(required) <= columns.keys():
            raise LabError("Required source coverage inputs absent", Status.BLOCKED_CAPABILITY)
        predicates, parameters = self._sql_filters(node)
        sql = (
            f"SELECT COUNT(*) AS matched_source_rows, "
            f"COUNT(*) FILTER (WHERE {' OR '.join(unknown)}) AS unknown_source_rows "
            f"FROM {identifier(node.table)}"
        )
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        return sql, parameters

    def fixture_population(self, folder: Path, model: str) -> SourcePopulation | None:
        """Read only structural fixture metadata; never generator code or evaluator labels."""
        try:
            return self._fixture_population(folder, model)
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise LabError("Invalid fixture population provenance; UNKNOWN", Status.BLOCKED_CAPABILITY) from error

    def _fixture_population(self, folder: Path, model: str) -> SourcePopulation | None:
        path = folder / "manifest.json"
        if not path.is_file():
            return None
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise LabError("Unreadable fixture population manifest", Status.BLOCKED_CAPABILITY) from error
        if not {"version", "profile", "seed", "tables"} <= manifest.keys():
            return None
        tables = {entry["id"]: entry for entry in manifest["tables"]}
        identity_tables = {
            name for name, spec in self.tables.items()
            if (spec["model"] == "core" and name != "quarantine")
            or name.endswith("_map") or name in {"subscription", "contract"}
        }
        required = set(self.models[model]["tables"]) | identity_tables
        if not required <= tables.keys() or len(tables) != len(manifest["tables"]):
            raise LabError("Incomplete fixture population manifest", Status.BLOCKED_CAPABILITY)
        for name in required:
            entry = tables[name]
            path = folder / f"{name}.parquet"
            if entry["file"] != path.name or not path.is_file():
                raise LabError("Fixture population source file differs from manifest", Status.BLOCKED_CAPABILITY)
            if path.stat().st_size != entry["bytes"]:
                raise LabError("Source file changed without matching manifest refresh")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["file_sha256"]:
                    raise LabError("Source file changed without matching manifest refresh")
        return SourcePopulation(
            model=model, evidence_kind="LOCAL_FIXTURE",
            binding_hash=digest({"mode": "OFFLINE_SYNTHETIC", "folder": str(folder.resolve()), "model": model}),
            profile=manifest["profile"], seed=manifest["seed"],
            # Existing fixtures declare their structural generator format, not a code-commit receipt.
            generator_revision=manifest["version"],
            identity_revision=digest({
                name: {key: self.tables[name][key] for key in ("columns", "primaryKey")}
                for name in sorted(identity_tables)
            }),
            manifest_hash=digest(manifest),
            table_hashes={name: tables[name]["content_sha256"] for name in self.models[model]["tables"]},
            shared_map_hashes={name: tables[name]["content_sha256"] for name in sorted(identity_tables)},
        )

    def validate_query(self, node: QueryNode) -> None:
        if node.model not in self.models or node.table not in self.tables:
            raise LabError("Unknown model/table", Status.BLOCKED_CAPABILITY)
        if node.table not in self.models[node.model]["tables"]:
            raise LabError("Table is outside the selected source model")
        if node.table in {"contract", "service_site_map"} and node.date_role != "month" and any(
            predicate.column == "month" for predicate in node.filters
        ):
            raise LabError("Validity dimensions require interval overlap, not a start-month filter")
        columns = self.tables[node.table]["columns"]
        for name in (
            node.columns + node.group_by + [f.column for f in node.filters]
            + [f.target_column for f in node.cohort_filters]
        ):
            if name not in columns:
                raise LabError(f"Unrecognized column: {name}")
        if node.date_role is None and any(t in ("date", "timestamp") for t in columns.values()):
            raise LabError("Only raw undated dimensions may omit a date role")
        if node.date_role is not None and node.date_role not in columns:
            raise LabError("Query date role is absent from the source table")
        if node.date_role is not None and columns[node.date_role] not in ("date", "timestamp"):
            raise LabError("Query date role must reference a date/timestamp column")
        if not set(node.expected_grain) <= set(node.group_by or node.columns):
            raise LabError("Expected grain not projected")
        for metric_id in node.metrics:
            metric = self.metrics.get(metric_id)
            if not metric or metric["model"] != node.model or metric["table"] != node.table:
                raise LabError("Metric source/home-table mismatch")
            if not set(node.group_by) <= set(metric["allowedDimensions"]):
                raise LabError("Metric does not permit this grouping")
            if node.date_role != metric["dateColumn"]:
                raise LabError("Metric date role mismatch")
            if metric["aggregation"] == "semi_additive":
                has_single_period = any(
                    f.column == metric["dateColumn"] and f.op in ("eq", "in") and len(f.values) == 1
                    for f in node.filters
                )
                if not has_single_period and metric["dateColumn"] not in node.group_by:
                    raise LabError("Snapshot requires one period or period grouping; cannot sum over time")

    def query_schema(self, node: QueryNode) -> ResultSchema:
        self.validate_query(node)
        return ResultSchema(
            lineage={
                c: [SourceColumn(model=node.model, table=node.table, column=c)]
                for c in node.columns + node.group_by + node.metrics
            },
            grain=list(node.expected_grain),
            derived_columns=list(node.metrics),
            aggregation=(
                {c: "raw" for c in node.columns + node.group_by}
                | {m: self.metrics[m]["aggregation"] for m in node.metrics}
            ),
            source_filters=[
                SourceFilter(model=node.model, table=node.table, **f.model_dump()) for f in node.filters
            ],
        )

    def _sql_filters(self, node: QueryNode) -> tuple[list[str], list[Scalar]]:
        if node.cohort_filters:
            raise LabError("Resolve dynamic cohorts before compiling a source query")
        predicates, parameters = [], []
        for f in node.filters:
            if any(v is None for v in f.values):
                raise LabError("Null filter semantics must be explicit; no implicit SQL NULL equality")
            op = {"eq": "=", "gte": ">=", "gt": ">", "lt": "<", "in": "IN"}[f.op]
            slots = ",".join("?" for _ in f.values)
            predicates.append(f"{identifier(f.column)} {op} ({slots})")
            parameters.extend(f.values)
        return predicates, parameters

    def sql(self, node: QueryNode) -> tuple[str, list[Scalar]]:
        self.validate_query(node)
        projection = [identifier(c) for c in node.columns or node.group_by]
        projection.extend(
            f"({self.metrics[m]['sql']}) AS {identifier(m)}" for m in node.metrics
        )
        predicates, parameters = self._sql_filters(node)
        query = f"SELECT {', '.join(projection)} FROM {identifier(node.table)}"
        if predicates:
            query += " WHERE " + " AND ".join(predicates)
        if node.group_by:
            query += " GROUP BY " + ", ".join(identifier(c) for c in node.group_by)
        if node.expected_grain:
            query += " ORDER BY " + ", ".join(identifier(c) for c in node.expected_grain)
        return query, parameters

    def dax(self, node: QueryNode) -> str:
        return self.compile_dax(node).text

    def compile_dax(
        self, node: QueryNode, *, max_rows: int | None = None, witnessed: bool = False,
        aliases: dict[str, str] | None = None, int64_identity_metrics: tuple[str, ...] = (),
        float64_identity_metrics: tuple[str, ...] = (),
    ) -> CompiledDax:
        self.validate_query(node)
        if node.cohort_filters:
            raise LabError("Resolve dynamic cohorts before compiling a source query")
        names = node.group_by + node.metrics if node.metrics else node.columns
        aliases = aliases if aliases is not None else {name: name for name in names}
        if set(aliases) != set(names) or any(not isinstance(name, str) or not name for name in aliases.values()):
            raise LabError("Compiler aliases must exactly cover the query projection")
        if len(set(aliases.values())) != len(names):
            raise LabError("Compiler output aliases must be unique")
        if max_rows is not None and (type(max_rows) is not int or max_rows < 1):
            raise LabError("Compiler transport row limit must be a positive integer")
        if witnessed:
            if max_rows is None or node.columns and node.metrics:
                raise LabError("Witnessed queries require one explicit bounded business projection")
            if {"__kind", "__total_rows", "__serialization_errors"} & set(aliases.values()):
                raise LabError("Business aliases collide with witness columns")
            if len(set(node.expected_grain)) != len(node.expected_grain) or (
                node.metrics and set(node.expected_grain) != set(node.group_by)
            ) or (not node.metrics and not node.expected_grain):
                raise LabError("Witnessed queries require the full unique expected grain")
            if set(int64_identity_metrics) & set(float64_identity_metrics) or not set(node.metrics) <= (
                set(int64_identity_metrics) | set(float64_identity_metrics)
            ) or any(
                self.metrics[name]["unit"] not in {"count", "cents", "minutes", "line_minutes", "units", "store_device_days"}
                for name in node.metrics if name not in float64_identity_metrics
            ):
                raise LabError("Witnessed metrics require disjoint reviewed exact-unit Int64 or Float64 identity projections")
        elif int64_identity_metrics or float64_identity_metrics:
            raise LabError("Identity serialization is restricted to the reviewed witnessed compiler")
        table = self.tables[node.table]["displayName"].replace("'", "''")

        def col(name: str) -> str:
            # Source templates retain snake_case column names for stable key identity.
            return f"'{table}'[{name.replace(']', ']]')}]"

        def literal(value: object) -> str:
            if isinstance(value, bool):
                return "TRUE()" if value else "FALSE()"
            if isinstance(value, (int, float)):
                return json.dumps(value, allow_nan=False)
            if not isinstance(value, str):
                raise LabError("Unsupported filter literal")
            return '"' + value.replace('"', '""') + '"'

        filters = []
        for f in node.filters:
            column_type = self.tables[node.table]["columns"][f.column]
            if column_type in ("date", "timestamp"):
                values = []
                for value in f.values:
                    if not isinstance(value, str):
                        raise LabError("Date filters require explicit ISO strings")
                    try:
                        parsed = datetime.fromisoformat(value) if column_type == "timestamp" else date.fromisoformat(value)
                    except ValueError as error:
                        raise LabError("Invalid date filter") from error
                    expression = f"DATE({parsed.year}, {parsed.month}, {parsed.day})"
                    if isinstance(parsed, datetime):
                        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                            raise LabError("Timestamp source filters require explicit UTC")
                        expression += f" + TIME({parsed.hour}, {parsed.minute}, {parsed.second})"
                    values.append(expression)
            else:
                values = [literal(v) for v in f.values]
            if f.op in ("eq", "in"):
                filters.append(f"TREATAS({{{', '.join(values)}}}, {col(f.column)})")
            else:
                op = {"gte": ">=", "gt": ">", "lt": "<"}[f.op]
                filters.append(f"FILTER(ALL({col(f.column)}), {col(f.column)} {op} {values[0]})")
        if node.metrics:
            args = [col(c) for c in node.group_by] + filters
            for m in node.metrics:
                measure = "[" + self.metrics[m]["name"].replace("]", "]]") + "]"
                args.extend([literal(aliases[m]), measure + (" * 1" if witnessed else "")])
            body = "SUMMARIZECOLUMNS(" + ", ".join(args) + ")"
        else:
            source = f"'{table}'"
            if filters:
                source = f"CALCULATETABLE({source}, {', '.join(filters)})"
            args = [source]
            for c in node.columns:
                expression = col(c)
                if witnessed and self.tables[node.table]["columns"][c] in {"int64", "float64"}:
                    expression += " * 1"
                args.extend([literal(aliases[c]), expression])
            body = "SELECTCOLUMNS(" + ", ".join(args) + ")"
        keys = [
            col(c) if node.metrics else "[" + aliases[c].replace("]", "]]") + "]"
            for c in node.expected_grain
        ]
        sorting = ", " + ", ".join(f"{key}, ASC" for key in keys) if keys else ""
        if witnessed:
            projection = []
            precision_checks = []
            temporal_columns = []
            boolean_columns = []
            for name in names:
                source = col(name) if name in node.group_by else "[" + aliases[name].replace("]", "]]") + "]"
                dtype = self.tables[node.table]["columns"].get(name) if name not in node.metrics else None
                if dtype in {"date", "timestamp"}:
                    temporal_columns.append(aliases[name])
                    rebuilt = f"DATE(YEAR({source}), MONTH({source}), DAY({source}))"
                    formatted = f'FORMAT({source}, "yyyy-MM-dd", "en-US")'
                    if dtype == "timestamp":
                        rebuilt += f" + TIME(HOUR({source}), MINUTE({source}), SECOND({source}))"
                        formatted += f' & "T" & FORMAT({source}, "HH:mm:ss", "en-US") & "+00:00"'
                    precision_checks.append(f"IF(ISBLANK({source}), 0, IF({source} == ({rebuilt}), 0, 1))")
                    source = f"IF(ISBLANK({source}), BLANK(), {formatted})"
                elif dtype == "bool":
                    boolean_columns.append(aliases[name])
                    source = f'IF(ISBLANK({source}), BLANK(), IF({source}, "true", "false"))'
                projection.extend([literal(aliases[name]), source])
            blanks = ", ".join(f"{literal(aliases[name])}, BLANK()" for name in names)
            guard_definition = (
                f"VAR __SerializationErrors = IF(ISEMPTY(__Result), 0, SUMX(__Result, {' + '.join(precision_checks)}))\n"
                if precision_checks else ""
            )
            guard_projection = ', "__serialization_errors", __SerializationErrors' if precision_checks else ""
            text = (
                f"DEFINE\nVAR __Result = {body}\n"
                "VAR __Count = IF(ISEMPTY(__Result), 0, COUNTROWS(__Result))\n"
                + guard_definition +
                "EVALUATE\nUNION(\n"
                f'    ROW("__kind", "count", {blanks}{guard_projection}, "__total_rows", __Count),\n'
                f'    SELECTCOLUMNS(TOPN({max_rows - 1}, __Result{sorting}), "__kind", "data", '
                f'{", ".join(projection)}{guard_projection}, "__total_rows", __Count)\n)'
                "\nORDER BY [__kind]"
                + "".join(", [" + aliases[name].replace("]", "]]") + "]" for name in node.expected_grain)
            )
        else:
            if max_rows is not None:
                body = f"TOPN({max_rows}, {body}{sorting})"
            text = "EVALUATE\n" + body + ("\nORDER BY " + ", ".join(keys) if keys else "")
        return CompiledDax(
            text=text, columns=tuple(aliases[name] for name in names),
            grain=tuple(aliases[name] for name in node.expected_grain), max_rows=max_rows,
            witnessed=witnessed,
            identity_metrics=tuple(m for m in node.metrics if m not in float64_identity_metrics) if witnessed else (),
            float64_identity_metrics=tuple(m for m in node.metrics if m in float64_identity_metrics) if witnessed else (),
            column_types={
                aliases[name]: (
                    "float64" if name in float64_identity_metrics else "int64"
                ) if name in node.metrics else self.tables[node.table]["columns"][name]
                for name in names
            } if witnessed or not node.metrics else {},
            metric_bindings={aliases[name]: name for name in node.metrics},
            iso_temporal_columns=tuple(temporal_columns) if witnessed else (),
            boolean_string_columns=tuple(boolean_columns) if witnessed else (),
        )
