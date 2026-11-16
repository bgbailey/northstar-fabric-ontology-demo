"""Private, explicitly reviewed MCP profiles; no inferred production response shapes."""

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.types import CallToolResult
from pydantic import Field
from referencing import Registry

from telco_iq.approval import private_config_path
from telco_iq.catalog import digest
from telco_iq.contracts import Contract, Scope, Sha256, Status
from telco_iq.errors import LabError
from telco_iq.evidence import write_receipt
from telco_iq.live_contracts import ObservedSourceState, ReaderBinding, SourceCallBudget, SourceIdentity
from telco_iq.mcp_client import (
    CaptureKind,
    DiscoveredClient,
    ReviewedCSVOutput,
    ReviewedIQWitnessOutput,
    ReviewedOutput,
    observed_iq_schema,
    pointer,
)
from telco_iq.mcp_provider import (
    CaptureReview,
    ReturnedColumn,
    ReviewedCoverageBinding,
    ReviewedObservedIQSchemaProfile,
    ReviewedSchemaProfile,
    ReviewedSourceBinding,
)
from telco_iq.private_evidence import (
    EvidenceReference,
    PrivateEvidenceConfig,
    PrivateEvidenceSink,
    private_identifiers,
)
from telco_iq.source_observation import MetadataStateProfile


class ReviewConfig(Contract):
    capture_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_reference: str = Field(min_length=1)
    provenance: Literal["reader_capture", "author_shape_capture", "synthetic_test"]

    def build(self):
        return CaptureReview(**self.model_dump())


class ColumnConfig(Contract):
    name: str
    dtype: Literal["string", "int64", "float64", "bool", "date", "timestamp"]
    nullable: bool


class OutputConfig(Contract):
    kind: Literal["structured"] = "structured"
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows_pointer: str
    complete_pointer: str
    total_rows_pointer: str
    errors_pointer: str
    output_schema: dict
    evidence: str

    def build(self):
        return ReviewedOutput(**self.model_dump(exclude={"kind"}))


class CSVConfig(Contract):
    kind: Literal["csv"] = "csv"
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    csv_pointer: str
    complete_pointer: str
    total_rows_pointer: str
    errors_pointer: str
    output_schema: dict
    evidence: str
    columns: dict[str, str]
    null_marker: str

    def build(self):
        return ReviewedCSVOutput(**self.model_dump(exclude={"kind"}))


class ObservedIQConfig(Contract):
    """Application-owned opt-in; never constructed from a fixture or a service API."""

    kind: Literal["observed_iq_witness_v1"]
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: str = Field(min_length=1)
    artifact_id: UUID
    workspace_id: UUID
    columns: dict[str, Literal["String", "Int64", "Double", "Object", "DateTime", "Boolean"]]
    int64_identity_metrics: tuple[str, ...]
    protocol: Literal["2025-11-25"]
    review_capture_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    float64_identity_metrics: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    review_capture_ids: tuple[Sha256, ...] = Field(default=(), max_length=64, exclude_if=lambda value: not value)
    grouped_int64_columns: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)

    def build(self):
        return ReviewedIQWitnessOutput(**self.model_dump(exclude={"kind"}))


class QueryProfileConfig(Contract):
    identity: SourceIdentity
    artifact_id: UUID
    columns: dict[str, dict[str, ColumnConfig]]
    output: Annotated[OutputConfig | CSVConfig | ObservedIQConfig, Field(discriminator="kind")]
    review: ReviewConfig

    def build(self):
        return ReviewedSourceBinding(
            identity=self.identity, artifact_id=self.artifact_id,
            columns={table: {key: ReturnedColumn(**column.model_dump()) for key, column in columns.items()}
                     for table, columns in self.columns.items()},
            output=self.output.build(), review=self.review.build(),
        )


class SchemaProfileConfig(Contract):
    kind: Literal["structured"] = "structured"
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema: dict
    review: ReviewConfig
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

    def build(self):
        return ReviewedSchemaProfile(**(self.model_dump(exclude={"review", "kind"}) | {"review": self.review.build()}))


class ObservedIQSchemaProfileConfig(Contract):
    kind: Literal["observed_iq_schema_v1"]
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review: ReviewConfig
    artifact_argument: Literal["artifactId"] = "artifactId"
    arguments: dict = Field(default_factory=dict)
    projection_reference: str = Field(min_length=1)

    def build(self):
        return ReviewedObservedIQSchemaProfile(**(
            self.model_dump(exclude={"kind", "review"}) | {"review": self.review.build()}
        ))


class StateProfileConfig(Contract):
    """A reviewed source query exposing actual source-owned revision values."""

    identity: SourceIdentity
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema: dict
    review: ReviewConfig
    dax: str = Field(min_length=1)
    data_revision_pointer: str
    model_revision_pointer: str
    errors_pointer: str
    complete_pointer: str
    evidence_meaning: str = Field(min_length=1)


class CoverageProfileConfig(Contract):
    """Private review of actual source columns and a separate ExecuteQuery result."""

    query: QueryProfileConfig
    method: Literal["catalog-filtered-raw-counts-v1"]
    review_capture_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    columns: dict[str, Literal["string", "int64", "float64", "bool", "date", "timestamp"]]
    diagnostic: dict | None = None
    reviewed_at: datetime
    expires_at: datetime

    def build(self):
        return ReviewedCoverageBinding(
            source=self.query.build(), source_schema_hash=self.source_schema_hash,
            columns=self.columns, diagnostic=self.diagnostic,
            reviewed_at=self.reviewed_at, expires_at=self.expires_at,
            review_capture_id=self.review_capture_id,
        )


class SourcePreparationConfig(Contract):
    evidence: PrivateEvidenceConfig
    capture_hashes: dict[str, Sha256] = Field(min_length=1)


class MCPProfiles(Contract):
    """Old profiles load, but live readiness requires approved private retention."""

    native_hashes: dict[str, str]
    native_arguments: dict[str, dict]
    sources: dict[str, QueryProfileConfig]
    schemas: dict[str, SchemaProfileConfig | ObservedIQSchemaProfileConfig]
    states: dict[str, StateProfileConfig] = Field(default_factory=dict)
    metadata_states: dict[str, MetadataStateProfile] = Field(default_factory=dict, exclude_if=lambda value: not value)
    metadata_state_pins: dict[str, str] = Field(default_factory=dict, exclude_if=lambda value: not value)
    private_evidence: PrivateEvidenceConfig | None = None
    coverage: dict[str, dict[str, CoverageProfileConfig]] = Field(default_factory=dict)
    preparation: SourcePreparationConfig | None = Field(default=None, exclude_if=lambda value: value is None)

    @classmethod
    def read(cls, path: Path, repo: Path):
        return cls.model_validate_json(private_config_path(path, repo).read_text(encoding="utf-8"))

    def verified_source_instructions(
        self, repo: Path, sources: dict[str, SourceIdentity], schema_hashes: dict[str, str],
    ) -> dict[str, str]:
        if self.preparation is None:
            return {}
        if set(self.preparation.capture_hashes) != set(sources) or set(self.schemas) != set(sources) or (
            set(schema_hashes) != set(sources)
        ):
            raise LabError("Source AI preparation does not cover the exact permitted models", Status.BLOCKED_CAPABILITY)
        sink = PrivateEvidenceSink(self.preparation.evidence, repo)
        instructions = {}
        for alias, identity in sources.items():
            profile = self.schemas[alias]
            capture_hash = self.preparation.capture_hashes[alias]
            if not isinstance(profile, ObservedIQSchemaProfileConfig) or (
                profile.review.provenance != "reader_capture" or profile.review.capture_hash != capture_hash
            ):
                raise LabError("Source AI preparation lacks reviewed reader schema evidence", Status.BLOCKED_CAPABILITY)
            original = sink.read(capture_hash)
            context = original.get("context")
            if original.get("tool") != "GetSemanticModelSchema" or original.get("source_alias") != alias or (
                not isinstance(context, dict) or "response" not in original
                or context.get("kind") != "CURRENT_READER_NATIVE_SCHEMA_METADATA_ONLY"
                or context.get("source") != identity.model_dump(mode="json")
                or context.get("tool_schema_hash") != profile.schema_hash
            ):
                raise LabError("Source AI preparation capture identity/provenance differs", Status.BLOCKED_AUTH)
            result = CallToolResult.model_validate(original["response"])
            built = profile.build()
            built.verify(profile.schema_hash, synthetic=False)
            if built.normalize(result, identity) != schema_hashes[alias]:
                raise LabError("Source AI preparation differs from reviewed schema fingerprint", Status.BLOCKED_CAPABILITY)
            schema = observed_iq_schema(result, identity.dataset_id, identity.workspace_id)
            if "CustomInstructions" not in schema:
                raise LabError("Source AI instructions are absent from the reviewed capture", Status.BLOCKED_CAPABILITY)
            instructions[alias] = schema["CustomInstructions"]
        return instructions


def state_probe(
    client: DiscoveredClient, profiles: dict[str, StateProfileConfig], *,
    session_active: Callable[[Scope], bool], receipts: Path | None = None,
    evidence_sink: PrivateEvidenceSink | None = None, synthetic: bool = False,
    request_budget: SourceCallBudget | None = None,
    metadata_profiles: dict[str, MetadataStateProfile] | None = None,
    metadata_probe: Callable | None = None,
):
    """Legacy receipt-only usage is explicit synthetic testing, never live proof."""

    async def observe(binding: ReaderBinding, alias: str) -> ObservedSourceState:
        if alias in (metadata_profiles or {}):
            if alias in profiles or metadata_probe is None:
                raise LabError("Explicit metadata source probe is ambiguous or not wired", Status.BLOCKED_CAPABILITY)
            return await metadata_probe(binding, alias)
        profile = profiles.get(alias)
        if profile is None:
            raise LabError("No reviewed source-owned revision query; source state is unproven", Status.BLOCKED_CAPABILITY)
        if not session_active(binding.scope) or client.reader_scope != binding.scope:
            raise LabError("Source-state probe is not bound to the current reader", Status.BLOCKED_AUTH)
        if profile.identity != binding.sources.get(alias) or profile.review.provenance not in (
            {"reader_capture", "synthetic_test"} if synthetic else {"reader_capture"}
        ):
            raise LabError("Source-state profile is not a reviewed capture for the bound source", Status.BLOCKED_CAPABILITY)
        if "ExecuteQuery" not in client.tools or client.schema_hash("ExecuteQuery") != profile.schema_hash:
            raise LabError("Source-state tool schema changed; re-review required", Status.BLOCKED_CAPABILITY)
        profile.review.build()
        Draft202012Validator.check_schema(profile.output_schema)
        if profile.output_schema.get("additionalProperties") is not False:
            raise LabError("Source-state envelope must expose unknown/truncation fields", Status.BLOCKED_CAPABILITY)
        if evidence_sink is None and (
            not synthetic or (isinstance(client.session, ClientSession) and client.endpoint.kind != "synthetic_test")
        ):
            raise LabError("Live source-state evidence requires configured private retention", Status.BLOCKED_CAPABILITY)
        retained: EvidenceReference | None = None

        def capture(tool: str, request: dict, raw: dict, response_kind: CaptureKind) -> None:
            nonlocal retained
            if evidence_sink is not None:
                retained = evidence_sink.capture(
                    tool=tool, source_alias=alias, request=request, response=raw,
                    context={
                        "kind": "SOURCE_STATE_QUERY", "binding_hash": binding.binding_hash,
                        "source": profile.identity.model_dump(mode="json"),
                        "schema_hash": profile.schema_hash, "review": profile.review.model_dump(mode="json"),
                    },
                    private_identifiers=private_identifiers(binding), synthetic=synthetic,
                    response_kind=response_kind,
                )
            if request_budget is not None:
                request_budget.response(raw)

        arguments = {"artifactId": str(profile.identity.dataset_id), "daxQueries": [profile.dax], "maxRows": 1}
        if evidence_sink is not None:
            evidence_sink.ready()
        if request_budget is not None:
            request_budget.reserve()
        response = await client.call("ExecuteQuery", arguments, capture=capture, synthetic_capture=synthetic)
        if not session_active(binding.scope):
            raise LabError("Reader ended during source-state probe", Status.BLOCKED_AUTH)
        raw = response.structuredContent
        try:
            valid = Draft202012Validator(profile.output_schema, registry=Registry()).is_valid(raw)
        except Exception as error:
            raise LabError("Source-state schema reference is not locally resolvable", Status.BLOCKED_CAPABILITY) from error
        if raw is None or not valid:
            raise LabError("Source-state output violates its reviewed profile", Status.BLOCKED_CAPABILITY)
        if response.isError or pointer(raw, profile.errors_pointer) != [] or pointer(raw, profile.complete_pointer) is not True:
            raise LabError("Source-state response is incomplete or failed", Status.PARTIAL)
        data_revision = pointer(raw, profile.data_revision_pointer)
        model_revision = pointer(raw, profile.model_revision_pointer)
        if not isinstance(data_revision, str) or not data_revision or not isinstance(model_revision, str) or not model_revision:
            raise LabError("Source-owned revision values are missing", Status.BLOCKED_CAPABILITY)
        if request_budget is not None:
            request_budget.returned(1)
        observed_at = datetime.now(timezone.utc)
        receipt = {
            "kind": "SYNTHETIC_SOURCE_STATE_QUERY" if synthetic else "ACTUAL_READER_SOURCE_STATE_QUERY",
            "binding_hash": binding.binding_hash,
            "source": alias, "query": profile.dax, "response": response.model_dump(mode="json"),
            "observed_at": observed_at.isoformat(), "meaning": profile.evidence_meaning,
        }
        if evidence_sink is not None:
            if retained is None:
                raise LabError("Private source-state response was not retained", Status.BLOCKED_CAPABILITY)
            evidence_hash = retained.capture_id
        else:
            if receipts is not None:
                write_receipt(receipts, receipt)
            evidence_hash = digest(receipt)
        return ObservedSourceState(
            data_revision=data_revision, model_revision=model_revision,
            evidence_hash=evidence_hash, observed_at=observed_at,
        )
    return observe
