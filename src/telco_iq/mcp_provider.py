"""Bounded delegated-reader bridge; no built-in profile claims live GA output.

Runtime wiring supplies one ReaderBinding, scope-bound discovered clients,
reviewed captures/mappings and a current-session callback. Query execution also
requires an explicit state_probe; configured revisions are not data clocks.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Literal
from uuid import UUID

from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.types import CallToolResult

from telco_iq.catalog import Catalog, CompiledDax, digest
from telco_iq.contracts import (
    Artifact,
    Clocks,
    QueryNode,
    Receipt,
    Scope,
    SourceClock,
    SourcePopulation,
    Status,
)
from telco_iq.errors import LabError
from telco_iq.live_contracts import (
    ObservedSourceState,
    ReaderBinding,
    ReaderProbeReceipt,
    ReaderSourceStateLease,
    SourceCallBudget,
    SourceIdentity,
)
from telco_iq.mcp_client import (
    CaptureKind,
    DiscoveredClient,
    IQAdapter,
    OntologyAdapter,
    ReviewedCSVOutput,
    ReviewedIQWitnessOutput,
    ReviewedOutput,
    mcp_capture_failure_status,
    observed_iq_schema,
    observed_iq_transport_rows,
    pointer,
)
from telco_iq.private_evidence import EvidenceReference, PrivateEvidenceSink, private_identifiers

SessionActive = Callable[[Scope], bool]
StateProbe = Callable[[ReaderBinding, str], Awaitable[ObservedSourceState]]
OutputProfile = ReviewedOutput | ReviewedCSVOutput | ReviewedIQWitnessOutput
ColumnType = Literal["int64", "float64", "string", "bool", "date", "timestamp"]


def _blocked(message: str) -> None:
    raise LabError(message, Status.BLOCKED_CAPABILITY)


def _sha256(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class CaptureReview:
    """Hash of an inspected raw receipt, not proof of current authorization."""

    capture_hash: str
    review_reference: str
    provenance: Literal["reader_capture", "author_shape_capture", "synthetic_test"]

    def verify(self, *, synthetic: bool) -> None:
        if not _sha256(self.capture_hash) or not self.review_reference.strip():
            _blocked("Reviewed profile requires captured evidence and a review reference")
        if self.provenance not in {"reader_capture", "author_shape_capture", "synthetic_test"}:
            _blocked("Unrecognized profile provenance")
        if self.provenance == "synthetic_test" and not synthetic:
            _blocked("Synthetic profiles cannot authorize production reader output")


@dataclass(frozen=True)
class ReturnedColumn:
    name: str
    dtype: ColumnType
    nullable: bool


@dataclass(frozen=True)
class ReviewedSourceBinding:
    identity: SourceIdentity
    artifact_id: UUID
    # Table -> catalogue output name -> actual returned key/type.
    columns: dict[str, dict[str, ReturnedColumn]]
    output: OutputProfile | None
    review: CaptureReview

    def verify(self, identity: SourceIdentity, *, synthetic: bool) -> None:
        if self.identity != identity or self.artifact_id != identity.dataset_id:
            raise LabError("Source artifact identity does not match ReaderBinding", Status.BLOCKED_AUTH)
        self.review.verify(synthetic=synthetic)
        if self.output is None:
            _blocked("Source output profile is absent; capture and review actual output first")


@dataclass(frozen=True)
class ReviewedCoverageBinding:
    """App-owned technical control review; never an additional native MCP field."""

    source: ReviewedSourceBinding
    source_schema_hash: str
    columns: dict[str, str]
    diagnostic: dict | None
    reviewed_at: datetime
    expires_at: datetime
    review_capture_id: str | None = None


def _coverage_raw(catalog: Catalog, node: QueryNode, columns: dict[str, str], diagnostic: dict | None):
    required = catalog.source_coverage_inputs(node)
    if not required:
        _blocked("Coverage control has no required source inputs")
    # Reuse the accepted required-component/effective-map authority. Live mapping
    # completeness is never inferred from a legacy profile without diagnostics.
    sql, _ = catalog.coverage_sql(
        node, {"coverage": diagnostic} if diagnostic else {},
        {key: "BOOLEAN" if value == "bool" else value for key, value in columns.items()},
        profile="showcase-v2",
    )
    for name in required:
        if columns.get(name) != catalog.tables[node.table]["columns"].get(name):
            _blocked("Coverage input types differ from the reviewed source schema")
    projected = node.model_copy(update={
        "metrics": [], "columns": required, "group_by": [], "expected_grain": [],
    })
    raw = catalog.dax(projected).removeprefix("EVALUATE\n")
    unknown = [f"ISBLANK([{name.replace(']', ']]')}])" for name in required]
    if '"coverage_complete" IS NOT TRUE' in sql:
        table = catalog.tables[node.table]["displayName"].replace("'", "''")
        raw = raw[:-1] + f', "coverage_complete", \'{table}\'[coverage_complete])'
        unknown.append("[coverage_complete] <> TRUE()")
    return raw, unknown


def coverage_dax(catalog: Catalog, node: QueryNode, profile: ReviewedCoverageBinding) -> str:
    raw, unknown = _coverage_raw(catalog, node, profile.columns, profile.diagnostic)
    return (
        f"EVALUATE\nVAR __raw = {raw}\nRETURN ROW("
        '"matched_source_rows", COALESCE(COUNTROWS(__raw), 0), '
        f'"unknown_source_rows", COALESCE(COUNTROWS(FILTER(__raw, {" || ".join(unknown)})), 0))'
    )


def compile_coverage_dax(
    catalog: Catalog, node: QueryNode, columns: dict[str, str], diagnostic: dict | None,
) -> CompiledDax:
    """Same filtered source control, serialized through the reviewed count-witness protocol."""
    _validate_dax_filters(catalog, node)
    raw, unknown = _coverage_raw(catalog, node, columns, diagnostic)
    counts = (
        'ROW("matched_source_rows", COALESCE(COUNTROWS(__raw), 0) * 1, '
        f'"unknown_source_rows", COALESCE(COUNTROWS(FILTER(__raw, {" || ".join(unknown)})), 0) * 1)'
    )
    text = (
        f"DEFINE\nVAR __raw = {raw}\nVAR __counts = {counts}\n"
        'VAR __total = COUNTROWS(__counts)\n'
        'VAR __witness = ROW("__kind", "count", "matched_source_rows", BLANK(), '
        '"unknown_source_rows", BLANK(), "__total_rows", __total)\n'
        'VAR __data = SELECTCOLUMNS(__counts, "__kind", "data", '
        '"matched_source_rows", [matched_source_rows] * 1, "unknown_source_rows", [unknown_source_rows] * 1, '
        '"__total_rows", __total)\n'
        'EVALUATE\nUNION(__witness, __data)\nORDER BY [__kind] ASC, [matched_source_rows] ASC, [unknown_source_rows] ASC'
    )
    names = ("matched_source_rows", "unknown_source_rows")
    return CompiledDax(text, names, names, 2, True, (), column_types={name: "int64" for name in names})


def _check_reader(
    binding: ReaderBinding, clients: tuple[DiscoveredClient, ...], session_active: SessionActive,
) -> None:
    if session_active(binding.scope.model_copy(deep=True)) is not True:
        raise LabError("Reader session is no longer current", Status.BLOCKED_AUTH)
    for client in clients:
        client.endpoint.validate()
        if client.reader_scope != binding.scope:
            raise LabError("MCP client belongs to a different reader scope/session epoch", Status.BLOCKED_AUTH)


def _scalar(value: object, dtype: str, *, nullable: bool) -> None:
    if value is None:
        if nullable:
            return
        _blocked("Null returned for a nonnullable reviewed column")
    valid = False
    if dtype == "int64":
        valid = type(value) is int and -(2**63) <= value < 2**63
    elif dtype == "float64":
        valid = type(value) in (int, float) and math.isfinite(value)
    elif dtype == "bool":
        valid = type(value) is bool
    elif dtype == "string":
        valid = type(value) is str
    elif dtype in {"date", "timestamp"} and isinstance(value, str):
        try:
            parsed = date.fromisoformat(value) if dtype == "date" else datetime.fromisoformat(value)
            valid = (
                parsed.isoformat() == value if dtype == "date"
                else parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
            )
        except ValueError:
            valid = False
    if not valid:
        _blocked("Returned value does not match reviewed source type; no lossy coercion")


def _validate_dax_filters(catalog: Catalog, node: QueryNode) -> None:
    catalog.validate_query(node)
    for predicate in node.filters:
        dtype = catalog.tables[node.table]["columns"][predicate.column]
        for value in predicate.values:
            _scalar(value, dtype, nullable=False)
            if dtype == "timestamp" and datetime.fromisoformat(value).microsecond:
                _blocked("Catalogue DAX timestamp literals cannot represent fractional seconds exactly")


def bounded_dax(catalog: Catalog, node: QueryNode) -> str:
    """Legacy limit+1 contract, emitted structurally rather than by parsing DAX."""
    _validate_dax_filters(catalog, node)
    return catalog.compile_dax(node, max_rows=node.max_rows + 1).text


async def _observed_state(
    probe: StateProbe, binding: ReaderBinding, alias: str,
) -> ObservedSourceState:
    started = datetime.now(timezone.utc)
    value = await probe(binding.model_copy(deep=True), alias)
    lower_bound = started - timedelta(seconds=probe.max_age_seconds) if isinstance(
        probe, ReaderSourceStateLease,
    ) else started
    if (
        not isinstance(value, ObservedSourceState) or not _sha256(value.evidence_hash)
        or not value.data_revision or not value.model_revision
        or value.observed_at.tzinfo is None
        or not lower_bound <= value.observed_at <= datetime.now(timezone.utc)
    ):
        _blocked("State probe did not return fresh, timestamped actual source-state evidence")
    return value.model_copy(deep=True)


class MCPQueryProvider:
    """Old explicit ``synthetic=True`` fixtures may omit private retention.

    Non-synthetic execution must supply ``evidence_sink``. Existing profiles,
    reader/identity checks and source-state probes still authorize the call;
    the sink grants no access. Retained capture/derivative hashes use existing
    Artifact.evidence_ids and Receipt.completeness_evidence, with no DTO change.
    """

    mode = "LIVE_DELEGATED"

    def __init__(
        self, catalog: Catalog, client: DiscoveredClient, binding: ReaderBinding, context_revision: str,
        *, source_bindings: dict[str, ReviewedSourceBinding], session_active: SessionActive,
        state_probe: StateProbe | None = None, synthetic: bool = False,
        evidence_sink: PrivateEvidenceSink | None = None,
        source_populations: list[SourcePopulation] | None = None,
        population_check: Callable[[], list[SourcePopulation]] | None = None,
        expected_revisions: dict[str, tuple[str, str]] | None = None,
        coverage_bindings: dict[str, dict[str, ReviewedCoverageBinding]] | None = None,
        source_schema_hashes: dict[str, str] | None = None,
        request_budget: SourceCallBudget | None = None,
        population_evidence_ids: dict[str, list[str]] | None = None,
    ):
        self.catalog, self.client = catalog, client
        self.binding = ReaderBinding.model_validate(binding.model_dump())
        self.context_revision = context_revision
        self.source_bindings = deepcopy(source_bindings)
        self.session_active, self.state_probe, self.synthetic = session_active, state_probe, synthetic
        self.evidence_sink = evidence_sink
        self.source_populations = source_populations if source_populations is not None else []
        self.population_check = population_check
        self.expected_revisions = deepcopy(expected_revisions or {})
        self.coverage_bindings = deepcopy(coverage_bindings or {})
        self.source_schema_hashes = deepcopy(source_schema_hashes or {})
        self.request_budget = request_budget
        self.population_evidence_ids = deepcopy(population_evidence_ids or {})
        self.mode = "SYNTHETIC_TEST" if synthetic else "LIVE_DELEGATED"
        if not context_revision or set(source_bindings) != set(binding.sources):
            _blocked("Query mappings must cover exactly the shared ReaderBinding source aliases")
        IQAdapter(client)
        if client.endpoint.kind == "synthetic_test" and not synthetic:
            _blocked("Synthetic transport requires explicit synthetic mode")
        for alias, source in self.source_bindings.items():
            if source.identity != binding.sources[alias] or source.artifact_id != binding.sources[alias].dataset_id:
                raise LabError("Source artifact identity does not match ReaderBinding", Status.BLOCKED_AUTH)
        self._configuration_hash = self._configuration()

    def _configuration(self) -> str:
        return digest({
            "binding": self.binding.model_dump(mode="json"),
            "sources": {alias: asdict(source) for alias, source in self.source_bindings.items()},
            "catalog": self.catalog.raw, "context": self.context_revision,
            "endpoint": asdict(self.client.endpoint), "protocol": self.client.protocol,
            "synthetic": self.synthetic,
            "private_evidence": self.evidence_sink.config.model_dump(mode="json") if self.evidence_sink else None,
            "populations": [record.model_dump(mode="json") for record in self.source_populations],
            "expected_revisions": self.expected_revisions,
            "coverage": {alias: {table: asdict(review) for table, review in tables.items()}
                         for alias, tables in self.coverage_bindings.items()},
            "source_schema_hashes": self.source_schema_hashes,
            "population_evidence_ids": self.population_evidence_ids,
            "state_lease": {
                "binding": self.state_probe.binding.binding_hash,
                "max_age_seconds": self.state_probe.max_age_seconds,
            } if isinstance(self.state_probe, ReaderSourceStateLease) else None,
        })

    def _current(self, scope: Scope) -> None:
        if scope != self.binding.scope or self._configuration() != self._configuration_hash:
            raise LabError("Reader binding or reviewed source mapping changed", Status.BLOCKED_AUTH)
        _check_reader(self.binding, (self.client,), self.session_active)
        if self.population_check is not None and self.population_check() != self.source_populations:
            _blocked("Reviewed source population changed; cached/output provenance cannot be relabelled")

    def _coverage(self, node: QueryNode) -> tuple[ReviewedCoverageBinding, str | CompiledDax] | None:
        if not self.catalog.source_coverage_inputs(node):
            return None
        profile = self.coverage_bindings.get(node.model, {}).get(node.table)
        if profile is None:
            _blocked("Required filtered raw-source coverage proof has no reviewed live contract")
        now = datetime.now(timezone.utc)
        if (
            profile.reviewed_at.tzinfo is None or profile.expires_at.tzinfo is None
            or not profile.reviewed_at <= now < profile.expires_at
            or profile.source_schema_hash != self.source_schema_hashes.get(node.model)
        ):
            _blocked("Coverage source schema/freshness review differs; re-review actual source capture")
        profile.source.verify(self.binding.sources[node.model], synthetic=self.synthetic)
        if not self.synthetic and profile.source.review.provenance != "reader_capture":
            _blocked("Coverage control requires an actual reviewed reader capture")
        mapping = profile.source.columns.get(node.table, {})
        if set(mapping) != {"matched_source_rows", "unknown_source_rows"} or any(
            col.dtype != "int64" or col.nullable or not col.name for col in mapping.values()
        ) or len({col.name for col in mapping.values()}) != 2:
            _blocked("Coverage requires exactly reviewed nonnullable integer count columns")
        IQAdapter(self.client, profile.source.output).require_review()
        observed = isinstance(profile.source.output, ReviewedIQWitnessOutput)
        if observed and any(column.name != name for name, column in mapping.items()):
            _blocked("Witnessed coverage requires the exact canonical technical count aliases")
        if not self.synthetic or profile.review_capture_id is not None:
            if self.evidence_sink is None or profile.review_capture_id is None:
                _blocked("Original reviewed coverage calibration capture is absent")
            original = self.evidence_sink.read(profile.review_capture_id)
            context = original.get("context", {})
            if (
                original.get("lane") != ("SYNTHETIC_TEST" if self.synthetic else "LIVE_DELEGATED")
                or original.get("tool") != "ExecuteQuery" or original.get("source_alias") != node.model
                or original.get("response_kind") != "MCP_TOOL_RESULT"
                or context.get("kind") != "SOURCE_COVERAGE_QUERY"
                or context.get("binding_hash") != self.binding.binding_hash
                or context.get("source") != profile.source.identity.model_dump(mode="json")
                or context.get("source_schema_hash") != profile.source_schema_hash
                or profile.source.review.capture_hash not in {
                    profile.review_capture_id, digest(original.get("response")),
                }
            ):
                _blocked("Coverage calibration is not an original reviewed capture for this source/reader")
            captured = datetime.fromisoformat(original["captured_at"])
            if not captured <= profile.reviewed_at:
                _blocked("Coverage review predates its calibration capture")
            sample = QueryNode.model_validate(context.get("query_node"))
            sample_compiled = compile_coverage_dax(
                self.catalog, sample, profile.columns, profile.diagnostic,
            ) if observed else None
            if sample.model != node.model or sample.table != node.table or original.get("request") != {
                "artifactId": str(profile.source.artifact_id),
                "daxQueries": [sample_compiled.text if observed else coverage_dax(self.catalog, sample, profile)],
                "maxRows": sample_compiled.max_rows if observed else 1,
            }:
                _blocked("Coverage calibration query/home/filter contract differs from the reviewed compiler")
            if observed and context.get("schema_hash") != self.client.schema_hash("ExecuteQuery"):
                _blocked("Witnessed coverage calibration belongs to another tool schema")
            decoded = profile.source.output.decode_result(
                CallToolResult.model_validate(original["response"]), self.client.schema_hash("ExecuteQuery"),
                **({"compiled": sample_compiled} if observed else {}),
            )
            self._coverage_counts(decoded, mapping)
        return profile, (
            compile_coverage_dax(self.catalog, node, profile.columns, profile.diagnostic)
            if observed else coverage_dax(self.catalog, node, profile)
        )

    @staticmethod
    def _coverage_counts(counts, mapping) -> dict[str, int]:
        if counts.status != Status.COMPLETE or len(counts.rows) != 1:
            raise LabError("Coverage control is partial, missing, or exceeds its one-row bound", Status.PARTIAL)
        row = counts.rows[0]
        if set(row) != {col.name for col in mapping.values()}:
            _blocked("Coverage control returned wrong reviewed count columns")
        coverage = {}
        for name, column in mapping.items():
            value = row[column.name]
            _scalar(value, "int64", nullable=False)
            if value < 0:
                _blocked("Coverage counts must be nonnegative")
            coverage[name] = value
        if coverage["unknown_source_rows"] > coverage["matched_source_rows"]:
            _blocked("Coverage unknown count exceeds matched source rows")
        return coverage

    def _mapping(self, node: QueryNode, source: ReviewedSourceBinding) -> dict[str, ReturnedColumn]:
        columns = source.columns.get(node.table, {})
        names = node.columns + node.group_by + node.metrics
        if not set(names) <= set(columns):
            _blocked("Query output columns have not been explicitly reviewed")
        selected = {name: columns[name] for name in names}
        if len({col.name for col in selected.values()}) != len(selected):
            _blocked("Returned output aliases are ambiguous")
        for name, col in selected.items():
            if (
                not col.name or type(col.nullable) is not bool
                or col.dtype not in {"int64", "float64", "string", "bool", "date", "timestamp"}
            ):
                _blocked("Output column alias/nullability is not reviewed")
            if name in node.metrics:
                if self.catalog.metrics[name]["unit"] == "cents" and col.dtype != "int64":
                    _blocked("Source cents require exact int64 output, not floating-point conversion")
            elif col.dtype != self.catalog.tables[node.table]["columns"][name]:
                _blocked("Reviewed output type disagrees with catalogue source type")
        return selected

    def _reviewed_table_clock(self, node: QueryNode, state: ObservedSourceState) -> str | None:
        if not self.expected_revisions or not self.source_populations:
            return None
        if self.population_check is None:
            _blocked("Reviewed table clock requires current source population verification")
        matches = [record for record in self.source_populations if record.model == node.model]
        if len(matches) != 1:
            _blocked("Reviewed table clock lacks one exact source population")
        population = matches[0]
        content = population.table_hashes.get(node.table)
        if (
            population.binding_hash != digest(self.binding.sources[node.model].model_dump(mode="json"))
            or content is None
            or population.shared_map_hashes.get(node.table, content) != content
            or (not self.synthetic and population.evidence_kind != "REVIEWED_LIVE")
            or self.expected_revisions.get(node.model) != (state.data_revision, state.model_revision)
        ):
            _blocked("Reviewed table content clock differs from source identity/population/current revisions")
        return "REVIEWED_TABLE_CONTENT:" + content

    def _observed_query(
        self, node: QueryNode, source: ReviewedSourceBinding, mapping: dict[str, ReturnedColumn],
    ) -> CompiledDax:
        output = source.output
        if not isinstance(output, ReviewedIQWitnessOutput):
            _blocked("Observed query requires its separately reviewed output profile")
        if output.artifact_id != source.artifact_id or output.workspace_id != source.identity.workspace_id:
            raise LabError("Observed profile belongs to a different source artifact", Status.BLOCKED_AUTH)
        for column in mapping.values():
            if output.columns.get(column.name) not in {
                "string": {"String"}, "int64": {"Int64"}, "float64": {"Double", "Object"},
                "bool": {"Boolean", "String"}, "date": {"String"}, "timestamp": {"String"},
            }.get(column.dtype, set()):
                _blocked("Observed output primitive differs from the reviewed trusted source type")
        _validate_dax_filters(self.catalog, node)
        compiled = self.catalog.compile_dax(
            node, max_rows=node.max_rows, witnessed=True,
            aliases={name: column.name for name, column in mapping.items()},
            int64_identity_metrics=output.int64_identity_metrics,
            float64_identity_metrics=output.float64_identity_metrics,
        )
        if not self.synthetic:
            capture_ids = output.review_capture_ids or ((output.review_capture_id,) if output.review_capture_id else ())
            if source.review.provenance != "reader_capture" or (
                self.evidence_sink is None or not capture_ids
            ):
                _blocked("Observed production profile requires an original current-reader calibration capture")
            matched = False
            for capture_id in capture_ids:
                original = self.evidence_sink.read(capture_id)
                context = original.get("context", {})
                if not isinstance(context, dict):
                    _blocked("Observed calibration context is not reviewed metadata")
                references = {digest(list(capture_ids))} if output.review_capture_ids else {
                    capture_id, digest(original.get("response")),
                }
                if (
                    original.get("lane") != "LIVE_DELEGATED" or original.get("tool") != "ExecuteQuery"
                    or original.get("source_alias") != node.model or original.get("response_kind") != "MCP_TOOL_RESULT"
                    or context.get("kind") != "SOURCE_BUSINESS_QUERY"
                    or context.get("binding_hash") != self.binding.binding_hash
                    or context.get("source") != source.identity.model_dump(mode="json")
                    or context.get("schema_hash") != self.client.schema_hash("ExecuteQuery")
                    or source.review.capture_hash not in references
                ):
                    _blocked("Observed calibration is not an original reviewed capture for this source/current reader")
                try:
                    sample = QueryNode.model_validate(context.get("query_node"))
                except (ValueError, TypeError):
                    _blocked("Observed calibration lacks its original compiler query contract")
                if sample.model != node.model or sample.table not in source.columns:
                    _blocked("Observed calibration source/home contract differs")
                sample_mapping = self._mapping(sample, source)
                _validate_dax_filters(self.catalog, sample)
                sample_compiled = self.catalog.compile_dax(
                    sample, max_rows=sample.max_rows, witnessed=True,
                    aliases={name: column.name for name, column in sample_mapping.items()},
                    int64_identity_metrics=output.int64_identity_metrics,
                    float64_identity_metrics=output.float64_identity_metrics,
                )
                if original.get("request") != {
                    "artifactId": str(source.artifact_id), "daxQueries": [sample_compiled.text],
                    "maxRows": sample.max_rows,
                }:
                    _blocked("Observed calibration request differs from the trusted witnessed compiler")
                if (output.review_capture_ids and sample.table != node.table) or (
                    not set(node.metrics) <= set(sample.metrics) or not set(compiled.columns) <= set(sample_compiled.columns)
                ):
                    continue
                reviewed = output.decode_result(
                    CallToolResult.model_validate(original["response"]), self.client.schema_hash("ExecuteQuery"),
                    compiled=sample_compiled,
                )
                if reviewed.status != Status.COMPLETE:
                    raise LabError("Observed calibration has no complete witnessed query result", Status.PARTIAL)
                matched = True
                break
            if not matched:
                _blocked("Observed projection/identity serialization lacks its actual reviewed calibration")
        return compiled

    async def query(self, node: QueryNode, scope: Scope) -> tuple[Artifact, Receipt]:
        start = perf_counter()
        self._current(scope)
        if node.model not in self.binding.sources:
            raise LabError("Source not authorized by ReaderBinding", Status.BLOCKED_AUTH)
        source = self.source_bindings[node.model]
        source.verify(self.binding.sources[node.model], synthetic=self.synthetic)
        if self.evidence_sink is None and (
            not self.synthetic or (
                isinstance(self.client.session, ClientSession) and self.client.endpoint.kind != "synthetic_test"
            )
        ):
            _blocked("Live source evidence requires configured private retention; a response hash is not replay evidence")
        if self.evidence_sink is not None:
            self.evidence_sink.ready()
        if not self.synthetic and (not self.source_populations or self.population_check is None):
            _blocked("Actual source population proof is absent; fixture/catalog defaults cannot authorize live reads")
        adapter = IQAdapter(self.client, source.output)
        # An absent/changed response review blocks before any query or state call.
        adapter.require_review()
        _validate_dax_filters(self.catalog, node)
        control = self._coverage(node)
        mapping = self._mapping(node, source)
        observed = isinstance(source.output, ReviewedIQWitnessOutput)
        compiled = self._observed_query(node, source, mapping) if observed else None
        dax = compiled.text if compiled is not None else bounded_dax(self.catalog, node)
        request_max_rows = node.max_rows if observed else node.max_rows + 1
        metadata = self.catalog.query_schema(node)
        if self.state_probe is None:
            _blocked("Trustworthy state_probe required; missing source state is NOT_COMPARABLE")
        await self.client.discover()
        self._current(scope)
        adapter.require_review()
        schema_hash = self.client.schema_hash("ExecuteQuery")
        before = await _observed_state(self.state_probe, self.binding, node.model)
        self._current(scope)
        if self.expected_revisions and self.expected_revisions.get(node.model) != (
            before.data_revision, before.model_revision,
        ):
            _blocked("Current source revisions differ from reviewed population capture")
        table_clock = self._reviewed_table_clock(node, before)
        coverage, coverage_ids = {}, []
        control_reference = None
        if control is not None:
            profile, control_query = control
            control_compiled = control_query if isinstance(control_query, CompiledDax) else None
            control_dax = control_compiled.text if control_compiled else control_query
            retained_control: EvidenceReference | None = None

            def capture_control(tool: str, request: dict, response: dict, response_kind: CaptureKind) -> None:
                nonlocal retained_control
                if self.evidence_sink is not None:
                    retained_control = self.evidence_sink.capture(
                        tool=tool, source_alias=node.model, request=request, response=response,
                        context={
                            "kind": "SOURCE_COVERAGE_QUERY", "binding_hash": self.binding.binding_hash,
                            "source": source.identity.model_dump(mode="json"),
                            "home_table": node.table, "query_node": node.model_dump(mode="json"),
                            "business_query_hash": digest({
                                "artifactId": str(source.artifact_id), "daxQueries": [dax],
                                "maxRows": request_max_rows,
                            }),
                            "source_schema_hash": profile.source_schema_hash,
                            "schema_hash": schema_hash, "review": asdict(profile.source.review),
                            "before": before.model_dump(mode="json"),
                        },
                        private_identifiers=private_identifiers(self.binding), synthetic=self.synthetic,
                        response_kind=response_kind,
                    )
                if self.request_budget is not None:
                    self.request_budget.response(response)
                    if control_compiled and response_kind == "MCP_TOOL_RESULT":
                        self.request_budget.returned(observed_iq_transport_rows(response))

            self._current(scope)
            if self.request_budget is not None:
                self.request_budget.reserve()
            counts, _ = await IQAdapter(self.client, profile.source.output).execute_result(
                str(source.artifact_id), control_dax, control_compiled.max_rows if control_compiled else 1,
                capture=capture_control, synthetic_capture=self.synthetic, compiled=control_compiled,
            )
            self._current(scope)
            if self.request_budget is not None and control_compiled is None:
                self.request_budget.returned(len(counts.rows))
            if self.evidence_sink is not None and retained_control is None:
                _blocked("Original coverage control response was not retained")
            coverage = self._coverage_counts(counts, profile.source.columns[node.table])
            if retained_control is not None:
                control_reference = asdict(retained_control)
                coverage_ids = [retained_control.capture_id, retained_control.response_hash,
                                retained_control.derivative_hash]
        retained: EvidenceReference | None = None

        def capture(tool: str, request: dict, response: dict, response_kind: CaptureKind) -> None:
            nonlocal retained
            if self.evidence_sink is not None:
                retained = self.evidence_sink.capture(
                    tool=tool, source_alias=node.model, request=request, response=response,
                    context={
                        "kind": "SOURCE_BUSINESS_QUERY", "binding_hash": self.binding.binding_hash,
                        "source": source.identity.model_dump(mode="json"),
                        "query_node": node.model_dump(mode="json"),
                        "schema_hash": schema_hash, "review": asdict(source.review),
                        "configuration_hash": self._configuration_hash,
                        "before": before.model_dump(mode="json"),
                        "ontology_clock": self.binding.native_snapshot_hash,
                        "context_clock": self.context_revision,
                        "coverage_control": control_reference,
                    },
                    private_identifiers=private_identifiers(self.binding), synthetic=self.synthetic,
                    response_kind=response_kind,
                )
            if self.request_budget is not None:
                self.request_budget.response(response)
            transport_rows = (
                observed_iq_transport_rows(response) if observed and response_kind == "MCP_TOOL_RESULT" else None
            )
            if self.request_budget is not None:
                if transport_rows is not None:
                    self.request_budget.returned(transport_rows)
                    if perf_counter() - self.request_budget.started >= self.request_budget.limits.timeout_seconds:
                        raise LabError("Source request time budget exhausted", Status.TIMED_OUT)

        self._current(scope)
        if self.request_budget is not None:
            self.request_budget.reserve()
        remaining = (
            self.request_budget.limits.timeout_seconds - (perf_counter() - self.request_budget.started)
            if observed and self.request_budget is not None else None
        )
        try:
            async with asyncio.timeout(remaining):
                decoded, raw = await adapter.execute_result(
                    str(source.artifact_id), dax, request_max_rows, capture=capture,
                    synthetic_capture=self.synthetic, compiled=compiled,
                )
        except TimeoutError:
            if not observed:
                raise
            raise LabError("Source request time budget exhausted", Status.TIMED_OUT) from None
        if self.evidence_sink is not None and retained is None:
            _blocked("Private source response was not retained; no replay-ready result")
        if self.request_budget is not None and not observed:
            self.request_budget.returned(len(decoded.rows))
        self._current(scope)
        after = await _observed_state(self.state_probe, self.binding, node.model)
        self._current(scope)
        await self.client.discover()
        self._current(scope)
        if self.client.schema_hash("ExecuteQuery") != schema_hash:
            _blocked("Current query tool/schema hash changed during retrieval")
        if len(decoded.rows) > request_max_rows:
            raise LabError("MCP response exceeded requested sentinel row budget", Status.PARTIAL)
        rows = []
        for row in decoded.rows:
            if set(row) != {col.name for col in mapping.values()}:
                _blocked("Returned column names do not exactly match reviewed output aliases")
            converted = {}
            for name, col in mapping.items():
                value = row[col.name]
                _scalar(value, col.dtype, nullable=col.nullable)
                converted[name] = value
            rows.append(converted)
        if coverage.get("matched_source_rows") == 0 and any(
            not node.metrics or any(row[metric] is not None for metric in node.metrics) for row in rows
        ):
            _blocked("Business output conflicts with the control's known absence of selected source rows")
        warnings = []
        status = decoded.status
        if len(rows) > node.max_rows:
            status = Status.PARTIAL
            warnings.append("Bounded query reached limit+1; returned rows are not a complete total.")
        if decoded.status != Status.COMPLETE:
            warnings.append("Reviewed response does not prove complete source results.")
        if coverage.get("unknown_source_rows", 0):
            status = Status.PARTIAL
            warnings.append(
                "Selected source rows contain unknown/incomplete inputs or mapping coverage; "
                "aggregate values are known subsets, not complete totals or compliance."
            )
        if (before.data_revision, before.model_revision) != (after.data_revision, after.model_revision):
            status = Status.PARTIAL
            warnings.append("NOT_COMPARABLE: observed source state changed during retrieval.")
        if self.synthetic:
            warnings.append("SYNTHETIC_TEST: injected transport/state evidence, not live Fabric availability.")
        if retained is None:
            warnings.append("SYNTHETIC_TEST: raw response not retained; hash-only evidence is not replay-ready.")
        rows = rows[:node.max_rows]
        clocks = Clocks(
            data=before.data_revision, model=before.model_revision,
            ontology=self.binding.native_snapshot_hash, context=self.context_revision,
        )
        raw_hash = retained.response_hash if retained else digest(raw.model_dump(mode="json"))
        query_hash = digest({"artifactId": str(source.artifact_id), "daxQueries": [dax], "maxRows": request_max_rows})
        evidence_id = digest({
            "binding": self.binding.binding_hash, "response": raw_hash, "query": query_hash,
            "schema": schema_hash, "before": before.model_dump(mode="json"), "after": after.model_dump(mode="json"),
            "review": asdict(source.review), "configuration": self._configuration_hash,
            "private_capture": retained.capture_id if retained else None,
        })
        evidence_ids = [evidence_id, raw_hash, before.evidence_hash, after.evidence_hash, source.review.capture_hash,
                        *coverage_ids, *self.population_evidence_ids.get(node.model, [])]
        if retained is not None:
            evidence_ids += [retained.capture_id, retained.derivative_hash]
        artifact = Artifact(
            id=node.id, rows=rows, status=status, scope=scope, clocks=clocks,
            source_models=[node.model], source_tables=[node.table], **metadata.model_dump(),
            source_populations=[record for record in self.source_populations if record.model == node.model],
            source_clocks=[SourceClock(
                model=node.model, table=node.table, data=before.data_revision, model_revision=before.model_revision,
                table_data=table_clock,
            )],
            evidence_ids=evidence_ids, warnings=warnings,
            coverage={
                "returned": len(rows), "bounded_response_total": decoded.total_rows, **coverage,
                **({"transport_returned": decoded.transport_rows, "transport_limit": request_max_rows,
                    "witness_rows": decoded.witness_rows} if observed else {}),
            },
        )
        receipt = Receipt(
            id=evidence_id, node_id=node.id, lane=self.mode,
            tool="ExecuteQuery", model=node.model, query=dax, query_hash=query_hash, schema_hash=schema_hash,
            elapsed_ms=(perf_counter() - start) * 1000, returned_rows=len(rows), status=status,
            completeness_evidence=(
                f"Reviewed profile {source.review.capture_hash}; response {raw_hash}; "
                + (f"transport limit={request_max_rows} including {decoded.witness_rows} count witness; "
                   f"transport returned={decoded.transport_rows}; full business result count={decoded.total_rows}; "
                   if observed else f"limit+1={request_max_rows}; bounded response total={decoded.total_rows}; ")
                + f"response status={decoded.status}; before={before.evidence_hash}; after={after.evidence_hash}"
                + (f"; reviewed table content clock={table_clock}" if table_clock is not None else "")
                + (f"; private capture={retained.capture_id}; sanitized derivative={retained.derivative_hash}"
                   if retained else "; SYNTHETIC_TEST hash only, no retained replay evidence")
                + (f"; filtered source coverage={coverage}; control={control_reference}" if control else "")
            ),
            clocks=clocks, warnings=warnings,
        )
        return artifact, receipt


def schema_fingerprint(tables: list[dict]) -> str:
    """Canonical typed-columns-v1: exact column names/types and measure names.

    All tables and columns are sorted by name; measure names are sorted. Source
    type spellings are preserved, not mapped to catalogue types. This profile
    does not claim measure-type evidence or any data/refresh clock.
    """
    canonical = []
    names = set()
    for table in tables:
        if not isinstance(table, dict) or set(table) != {"name", "columns", "measures"}:
            _blocked("Schema fingerprint requires exact table/column/measure name records")
        name = table["name"]
        if not isinstance(name, str) or not name or name in names:
            _blocked("Duplicate or invalid source table name")
        names.add(name)
        columns, measures = table["columns"], table["measures"]
        if not isinstance(columns, list):
            _blocked("Source column names and observed data types are required")
        column_names, normalized_columns = set(), []
        for column in columns:
            if (
                not isinstance(column, dict) or set(column) != {"name", "data_type"}
                or any(not isinstance(value, str) or not value.strip() for value in column.values())
                or column["name"] in column_names
            ):
                _blocked("Duplicate columns or missing observed source data types")
            column_names.add(column["name"])
            normalized_columns.append({"name": column["name"], "data_type": column["data_type"]})
        if (
            not isinstance(measures, list) or any(not isinstance(value, str) or not value for value in measures)
            or len(set(measures)) != len(measures)
        ):
            _blocked("Duplicate or incomplete source measure name evidence")
        canonical.append({
            "name": name, "columns": sorted(normalized_columns, key=lambda column: column["name"]),
            "measures": sorted(measures),
        })
    if not canonical:
        _blocked("Empty source schema is not authorization evidence")
    return digest({"tables": sorted(canonical, key=lambda table: table["name"])})


@dataclass(frozen=True)
class ReviewedSchemaProfile:
    """Exact captured schema shape/selectors; no assumed GA fields.

    Selectors are JSON pointers relative to structuredContent, then table and
    column/measure records respectively. exhaustive_response_reference must
    identify the inspected evidence that this response includes ALL table,
    column and measure names plus observed column data types. No source type
    is inferred from the catalogue; measure data types are not claimed.
    Optional completeness/count selectors check observed fields, never defaults.
    """

    schema_hash: str
    output_schema: dict
    review: CaptureReview
    artifact_argument: str
    arguments: dict
    workspace_pointer: str
    dataset_pointer: str
    tables_pointer: str
    table_name_pointer: str
    columns_pointer: str
    column_name_pointer: str
    column_type_pointer: str
    measures_pointer: str
    measure_name_pointer: str
    exhaustive_response_reference: str
    complete_pointer: str | None = None
    table_count_pointer: str | None = None

    def verify(self, actual_hash: str, *, synthetic: bool) -> None:
        self.review.verify(synthetic=synthetic)
        if (
            actual_hash != self.schema_hash or not self.exhaustive_response_reference.strip()
            or not self.artifact_argument or self.artifact_argument in self.arguments
        ):
            _blocked("Source schema profile is unreviewed, incomplete or changed")
        if self.output_schema.get("additionalProperties") is not False:
            _blocked("Schema normalization requires a closed reviewed envelope")
        Draft202012Validator.check_schema(self.output_schema)

    def normalize(self, result: CallToolResult, identity: SourceIdentity) -> str:
        data = result.structuredContent
        if result.isError or data is None or not Draft202012Validator(self.output_schema).is_valid(data):
            _blocked("Source schema output violates reviewed shape")
        try:
            workspace, dataset = UUID(pointer(data, self.workspace_pointer)), UUID(pointer(data, self.dataset_pointer))
        except (ValueError, TypeError, AttributeError) as error:
            raise LabError("Source schema identity is not UUID evidence", Status.BLOCKED_AUTH) from error
        if workspace != identity.workspace_id or dataset != identity.dataset_id:
            raise LabError("Observed source identity does not match ReaderBinding", Status.BLOCKED_AUTH)
        tables = pointer(data, self.tables_pointer)
        if not isinstance(tables, list):
            _blocked("Reviewed schema tables are not a complete list")
        if self.complete_pointer is not None and pointer(data, self.complete_pointer) is not True:
            _blocked("Source schema completeness evidence is missing")
        if self.table_count_pointer is not None:
            count = pointer(data, self.table_count_pointer)
            if type(count) is not int or count != len(tables):
                _blocked("Source schema table count is incomplete")
        normalized = []
        for table in tables:
            columns, measures = pointer(table, self.columns_pointer), pointer(table, self.measures_pointer)
            if not isinstance(columns, list) or not isinstance(measures, list):
                _blocked("Incomplete source column/measure evidence")
            normalized.append({
                "name": pointer(table, self.table_name_pointer),
                "columns": [
                    {
                        "name": pointer(column, self.column_name_pointer),
                        "data_type": pointer(column, self.column_type_pointer),
                    }
                    for column in columns
                ],
                "measures": [pointer(measure, self.measure_name_pointer) for measure in measures],
            })
        return schema_fingerprint(normalized)


@dataclass(frozen=True)
class ReviewedObservedIQSchemaProfile:
    """The reviewed JSON-text AI projection; hidden physical fields are not inferred."""

    schema_hash: str
    review: CaptureReview
    artifact_argument: str
    arguments: dict
    projection_reference: str

    def verify(self, actual_hash: str, *, synthetic: bool) -> None:
        self.review.verify(synthetic=synthetic)
        if (
            actual_hash != self.schema_hash or self.artifact_argument != "artifactId"
            or self.arguments != {} or not self.projection_reference.strip()
        ):
            _blocked("Observed IQ schema projection/profile/arguments are unreviewed")

    def normalize(self, result: CallToolResult, identity: SourceIdentity) -> str:
        schema = observed_iq_schema(result, identity.dataset_id, identity.workspace_id)
        fingerprint = schema_fingerprint([{
            "name": table["Name"],
            "columns": [{"name": column["Name"], "data_type": column["Type"]} for column in table["Columns"]],
            "measures": [measure["Name"] for measure in table.get("Measures", [])],
        } for table in schema["Tables"]])
        if "CustomInstructions" in schema:
            return digest({"schema": fingerprint, "CustomInstructions": schema["CustomInstructions"]})
        return fingerprint


class ReaderProbe:
    """Callable acquisition probe for acquire_reader_context(..., probe=...).

    Native clients must target the exact bound endpoint, including in explicit
    synthetic tests. Shape evidence never substitutes for current tool calls.
    """

    def __init__(
        self, native_client: DiscoveredClient, source_client: DiscoveredClient,
        *, schema_profiles: dict[str, ReviewedSchemaProfile | ReviewedObservedIQSchemaProfile],
        session_active: SessionActive,
        native_arguments: dict[str, dict], state_probe: StateProbe | None = None,
        synthetic: bool = False,
    ):
        self.native_client, self.source_client = native_client, source_client
        self.schema_profiles = deepcopy(schema_profiles)
        self.session_active, self.state_probe, self.synthetic = session_active, state_probe, synthetic
        self.native_arguments = deepcopy(native_arguments)
        OntologyAdapter(native_client)
        IQAdapter(source_client)
        if source_client.endpoint.kind == "synthetic_test" and not synthetic:
            _blocked("Synthetic source transport requires explicit synthetic mode")

    def _configuration(self) -> str:
        return digest({
            "profiles": {alias: asdict(profile) for alias, profile in self.schema_profiles.items()},
            "native_arguments": self.native_arguments, "synthetic": self.synthetic,
            "clients": [
                {"endpoint": asdict(client.endpoint), "protocol": client.protocol}
                for client in (self.native_client, self.source_client)
            ],
        })

    def _current(self, binding: ReaderBinding, configuration: str) -> None:
        _check_reader(binding, (self.native_client, self.source_client), self.session_active)
        expected_url = (
            "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/"
            f"{binding.ontology_workspace_id}/items/{binding.ontology_item_id}/ontologyEndpoint"
        )
        if self.native_client.endpoint.url != expected_url:
            raise LabError("Native endpoint does not match ReaderBinding ontology", Status.BLOCKED_AUTH)
        if self._configuration() != configuration:
            _blocked("Reader probe configuration/profile changed during acquisition")

    async def __call__(self, binding: ReaderBinding) -> ReaderProbeReceipt:
        binding = ReaderBinding.model_validate(binding.model_dump())
        configuration = self._configuration()
        clients = (self.native_client, self.source_client)
        self._current(binding, configuration)
        native_tools = {"list_ontology_entities", "list_ontology_rules"}
        if set(self.native_arguments) != native_tools or set(self.schema_profiles) != set(binding.sources):
            _blocked("Reader probe requires reviewed native inputs and every bound source schema")
        for alias, profile in self.schema_profiles.items():
            profile.verify(self.source_client.schema_hash("GetSemanticModelSchema"), synthetic=self.synthetic)
        for client in clients:
            await client.discover()
            self._current(binding, configuration)
        native_hashes = {name: self.native_client.schema_hash(name) for name in native_tools}
        for name, value in native_hashes.items():
            if self.native_client.reviewed_hashes.get(name) != value:
                _blocked("Native tool schema has not been reviewed")
        schema_hash = self.source_client.schema_hash("GetSemanticModelSchema")
        for profile in self.schema_profiles.values():
            profile.verify(schema_hash, synthetic=self.synthetic)
        evidence = {"binding": binding.binding_hash, "configuration": configuration}
        native_values = {}
        for name in sorted(native_tools):
            self._current(binding, configuration)
            result = await OntologyAdapter(self.native_client).read(name, self.native_arguments[name])
            self._current(binding, configuration)
            data = result.structuredContent
            if (
                result.isError or not isinstance(data, dict) or set(data) != {"values"}
                or not isinstance(data["values"], list) or any(not isinstance(v, dict) for v in data["values"])
            ):
                _blocked("Native context differs from the observed structuredContent.values shape")
            native_values[name] = deepcopy(data["values"])
            evidence[name] = digest({
                "binding": binding.binding_hash, "tool_schema": native_hashes[name],
                "arguments": self.native_arguments[name], "response": result.model_dump(mode="json"),
            })
        slots = asyncio.Semaphore(4)
        failed = False

        async def source_schema(alias, identity):
            nonlocal failed
            async with slots:
                if failed:
                    _blocked("Reader schema acquisition stopped after a required source failed")
                completed = False
                try:
                    profile = self.schema_profiles[alias]
                    arguments = {**profile.arguments, profile.artifact_argument: str(identity.dataset_id)}
                    self._current(binding, configuration)
                    result = await self.source_client.call("GetSemanticModelSchema", arguments)
                    self._current(binding, configuration)
                    fingerprint = profile.normalize(result, identity)
                    state = None
                    if self.state_probe is not None:
                        state = await _observed_state(self.state_probe, binding, alias)
                        self._current(binding, configuration)
                    response_hash = digest({
                        "binding": binding.binding_hash, "tool_schema": schema_hash, "arguments": arguments,
                        "response": result.model_dump(mode="json"), "profile": asdict(profile),
                    })
                    completed = True
                    return fingerprint, response_hash, digest(asdict(profile)), state
                finally:
                    if not completed:
                        failed = True

        try:
            async with asyncio.TaskGroup() as group:
                tasks = {
                    alias: group.create_task(source_schema(alias, identity))
                    for alias, identity in sorted(binding.sources.items())
                }
        except ExceptionGroup as error:
            if len(error.exceptions) == 1:
                raise error.exceptions[0] from None
            status = mcp_capture_failure_status(error)
            details = "; ".join(dict.fromkeys(str(cause) for cause in error.exceptions if isinstance(cause, LabError)))
            raise LabError("Concurrent reader source-schema acquisition failed: " + details, status) from None
        schema_hashes, states = {}, {}
        for alias, task in tasks.items():
            fingerprint, response_hash, profile_hash, state = task.result()
            schema_hashes[alias] = fingerprint
            evidence[f"source:{alias}"] = response_hash
            evidence[f"source_profile:{alias}"] = profile_hash
            if state is not None:
                states[alias] = state
                evidence[f"state:{alias}"] = state.evidence_hash
        for client in clients:
            await client.discover()
            self._current(binding, configuration)
        if (
            self.source_client.schema_hash("GetSemanticModelSchema") != schema_hash
            or any(self.native_client.schema_hash(name) != value for name, value in native_hashes.items())
        ):
            _blocked("Reader tool schemas changed during context acquisition")
        return ReaderProbeReceipt(
            binding_hash=binding.binding_hash, checked_at=datetime.now(timezone.utc),
            native_entities=native_values["list_ontology_entities"], native_rules=native_values["list_ontology_rules"],
            source_schema_hashes=schema_hashes, observed_source_states=states, evidence_hashes=evidence,
        )

    async def probe(self, binding: ReaderBinding) -> ReaderProbeReceipt:
        return await self(binding)
