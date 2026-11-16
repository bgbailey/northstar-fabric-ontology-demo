from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

Scalar = str | int | float | bool | None
ColumnReferenceMode = Literal["exact", "source_qualified"]
PopulationMode = Literal["complete", "known_mapped_subset"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Status(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    BLOCKED_AUTH = "BLOCKED_AUTH"
    BLOCKED_CAPABILITY = "BLOCKED_CAPABILITY"
    AMBIGUOUS = "AMBIGUOUS"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"


class Scope(Contract):
    tenant: str
    principal: str
    authorization_revision: str
    models: list[str]
    mode: Literal["OFFLINE_SYNTHETIC", "LIVE_DELEGATED"]


class Clocks(Contract):
    data: str
    model: str
    ontology: str
    context: str


class Budgets(Contract):
    max_rows: int = Field(default=10_000, ge=1, le=100_000)
    max_bytes: int = Field(default=8_000_000, ge=1, le=64_000_000)
    max_queries: int = Field(default=16, ge=1, le=512)
    timeout_seconds: float = Field(default=45, gt=0, le=300)


class Filter(Contract):
    column: str
    op: Literal["eq", "in", "gte", "gt", "lt"]
    values: list[Scalar] = Field(min_length=1, max_length=250)

    @model_validator(mode="after")
    def arity(self) -> Filter:
        if self.op != "in" and len(self.values) != 1:
            raise ValueError("Only IN accepts multiple values")
        return self


class Node(Contract):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    depends_on: list[str] = Field(default_factory=list)


class CohortFilter(Contract):
    source: str
    source_column: str
    target_column: str
    relationship: str | None = None
    population: PopulationMode = Field(default="complete", exclude_if=lambda value: value == "complete")


class QueryNode(Node):
    kind: Literal["QuerySemanticModel"]
    model: str
    table: str
    metrics: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    cohort_filters: list[CohortFilter] = Field(default_factory=list, max_length=1)
    date_role: str | None = None
    expected_grain: list[str]
    max_rows: int = Field(default=10_000, ge=1, le=100_000)
    completeness: Literal["full", "qualified_partial"] = "full"

    @model_validator(mode="after")
    def projection(self) -> QueryNode:
        if bool(self.metrics) == bool(self.columns):
            raise ValueError("Choose governed metrics OR raw columns, not both/neither")
        if self.metrics and self.date_role is None:
            raise ValueError("Metric queries require their declared date role")
        if self.columns and self.group_by:
            raise ValueError("Raw projection cannot also GROUP BY")
        if any(binding.population == "known_mapped_subset" for binding in self.cohort_filters) and (
            self.completeness != "qualified_partial"
        ):
            raise ValueError("Known-mapped cohorts require explicit qualified_partial result semantics")
        if len(set(self.metrics + self.columns + self.group_by)) != len(
            self.metrics + self.columns + self.group_by
        ):
            raise ValueError("Duplicate query outputs")
        return self


class ProjectColumns(Node):
    kind: Literal["ProjectColumns"]
    source: str
    columns: dict[str, str] = Field(min_length=1)


class TemporalPredicate(Contract):
    left_start: str
    left_end: str
    right_start: str
    right_end: str


class JoinNode(Node):
    kind: Literal["JoinResults", "ResolveIdentity", "SemiJoinCohort"]
    left: str
    right: str
    relationship: str
    left_keys: list[str] = Field(min_length=1)
    right_keys: list[str] = Field(min_length=1)
    cardinality: Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
    unmatched: Literal["error", "exclude", "preserve"]
    temporal: TemporalPredicate | None = None
    max_fanout: int = Field(default=1, ge=1, le=100)
    overlap_start: str | None = None
    overlap_end: str | None = None

    @model_validator(mode="after")
    def intersection(self) -> JoinNode:
        if (self.overlap_start is None) != (self.overlap_end is None):
            raise ValueError("Intersection output names must be supplied together")
        if self.overlap_start is not None:
            if not self.temporal or not self.overlap_start or not self.overlap_end:
                raise ValueError("Intersection outputs require a temporal predicate and nonempty names")
            if self.overlap_start == self.overlap_end:
                raise ValueError("Intersection output names must differ")
            if self.kind == "SemiJoinCohort":
                raise ValueError("A semijoin cannot project per-match interval intersections")
        return self


class AggregateNode(Node):
    kind: Literal["AggregateResult"]
    source: str
    group_by: list[str]
    sums: list[str] = Field(min_length=1)


class RuleNode(Node):
    kind: Literal["ApplyDeterministicRule", "ComparePeriods"]
    source: str
    operation: Literal["ratio", "difference", "capped_difference", "union_minutes"]
    output: str
    arguments: list[str]
    group_by: list[str] = Field(default_factory=list)


class RankNode(Node):
    kind: Literal["RankCandidates"]
    source: str
    column: str
    descending: bool = True
    limit: int = Field(ge=1, le=1000)
    population: PopulationMode = Field(default="complete", exclude_if=lambda value: value == "complete")


class ContextNode(Node):
    kind: Literal["ResolveConcept", "SelectMetric", "ReadModelSchema"]
    references: list[str]


class AnswerNode(Node):
    kind: Literal["SynthesizeAnswer"]
    source: str
    additional_sources: dict[str, str] = Field(default_factory=dict, max_length=4, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def separate_views(self):
        if any(not label.strip() or len(label) > 80 or not reference for label, reference in self.additional_sources.items()):
            raise PydanticCustomError(
                "answer_view_label_invalid", "Separate answer views require bounded nonempty labels and source node IDs",
            )
        references = list(self.additional_sources.values())
        if self.source in references or len(set(references)) != len(references):
            raise PydanticCustomError(
                "answer_view_source_repeated", "Separate answer views cannot repeat the primary or another source",
            )
        return self


PlanNode = Annotated[
    QueryNode | ProjectColumns | JoinNode | AggregateNode | RuleNode | RankNode | ContextNode | AnswerNode,
    Field(discriminator="kind"),
]


class Plan(Contract):
    version: Literal["1.0"] = "1.0"
    request_id: str
    question: str
    principal_scope: Scope
    as_of: datetime
    ontology_revision: str
    hypotheses: list[str] = Field(default_factory=list)
    metric_selections: list[str]
    nodes: list[PlanNode] = Field(min_length=1, max_length=100)
    result_node: str
    budgets: Budgets = Field(default_factory=Budgets)
    clarification_state: Literal["resolved", "required"] = "resolved"

    @model_validator(mode="after")
    def aware_clock(self) -> Plan:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of requires an explicit timezone")
        return self


class ReviewedIntervalOrigin(Contract):
    model: str
    table: str
    grain: list[str] = Field(min_length=1)
    keys: list[str] = Field(min_length=1)
    via_relationships: list[str] = Field(min_length=1, max_length=8)


class Relationship(Contract):
    id: str
    left_model: str
    right_model: str
    left_table: str
    right_table: str
    left_keys: list[str]
    right_keys: list[str]
    cardinality: Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
    temporal: TemporalPredicate | None = None
    version: str
    purposes: list[str]
    aligned_filters: list[str] = Field(default_factory=list)
    null_behavior: Literal["unmatched"] = "unmatched"
    left_interval_origin: ReviewedIntervalOrigin | None = Field(default=None, exclude_if=lambda value: value is None)
    right_interval_origin: ReviewedIntervalOrigin | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def interval_origins(self):
        if (self.left_interval_origin is not None or self.right_interval_origin is not None) and self.temporal is None:
            raise ValueError("Carried interval origins require a reviewed temporal predicate")
        return self


class Context(Contract):
    mode: Literal["OFFLINE_SYNTHETIC", "NATIVE_SNAPSHOT", "NATIVE_LIVE"]
    revision: str
    source: str
    schema_hash: str | None = None
    scope: Scope
    relationships: list[Relationship]
    rules: list[dict[str, str]]
    provenance: list[str]


class SourceColumn(Contract):
    model: str
    table: str
    column: str


class SourceFilter(Filter):
    model: str
    table: str


class SourceClock(Contract):
    model: str
    table: str
    # Model snapshot plus separately validated table-content evidence for shared dimensions.
    data: str
    model_revision: str
    table_data: str | None = None


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SourcePopulation(Contract):
    """Structural population evidence, independent of freshness and business time."""

    model: str = Field(min_length=1)
    evidence_kind: Literal["LOCAL_FIXTURE", "REVIEWED_LIVE"]
    binding_hash: Sha256
    profile: str = Field(min_length=1)
    seed: int = Field(strict=True)
    generator_revision: str = Field(min_length=1)
    identity_revision: Sha256
    manifest_hash: Sha256
    table_hashes: dict[str, Sha256] = Field(min_length=1)
    shared_map_hashes: dict[str, Sha256] = Field(min_length=1)


class IntervalLineage(Contract):
    starts: list[SourceColumn]
    ends: list[SourceColumn]


class ResultSchema(Contract):
    lineage: dict[str, list[SourceColumn]] = Field(default_factory=dict)
    derived_columns: list[str] = Field(default_factory=list)
    # None means a projection no longer exposes a unique grain; [] means scalar.
    grain: list[str] | None
    aggregation: dict[str, str]
    source_filters: list[SourceFilter] = Field(default_factory=list)
    intervals: list[IntervalLineage] = Field(default_factory=list)
    relationship_history: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)


class CohortExclusion(Contract):
    source_node: str
    source_column: str
    unknown_mapping_observations: int = Field(ge=0)
    null_key_rows: int = Field(ge=0)
    evidence_ids: list[str]


class Artifact(ResultSchema):
    id: str
    rows: list[dict[str, Scalar]]
    status: Status
    scope: Scope
    clocks: Clocks
    source_models: list[str]
    source_tables: list[str]
    source_clocks: list[SourceClock] = Field(default_factory=list)
    source_populations: list[SourcePopulation] = Field(default_factory=list)
    evidence_ids: list[str]
    warnings: list[str] = Field(default_factory=list)
    coverage: dict[str, int] = Field(default_factory=dict)
    population_qualification: Literal["unqualified", "known_mapped_subset"] = Field(
        default="unqualified", exclude_if=lambda value: value == "unqualified",
    )
    exclusions: list[CohortExclusion] = Field(default_factory=list, exclude_if=lambda value: not value)


class Receipt(Contract):
    id: str
    node_id: str
    lane: str
    tool: str
    model: str | None = None
    query: str | None = None
    parameters: list[Scalar] | None = None
    query_hash: str | None = None
    schema_hash: str | None = None
    elapsed_ms: float
    returned_rows: int
    status: Status
    completeness_evidence: str
    clocks: Clocks
    source_watermark: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ContextVerification(Contract):
    checked_at: datetime
    receipt_hash: Sha256
    evidence_hashes: dict[str, Sha256]


class ExecutionResult(Contract):
    request_id: str
    status: Status
    mode: str
    answer: Artifact | None = None
    additional_answers: dict[str, Artifact] = Field(default_factory=dict, exclude_if=lambda value: not value)
    context_verification: ContextVerification | None = Field(default=None, exclude_if=lambda value: value is None)
    receipts: list[Receipt] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    elapsed_ms: float
