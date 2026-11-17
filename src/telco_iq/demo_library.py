"""Display-only archives of the recorded demo streams, not a wire schema or user grant."""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from telco_iq.contracts import Contract, Sha256, SourceClock
from telco_iq.recorded_comparison import RecordedComparisons
from telco_iq.recorded_ontology import RecordedOntology

EXECUTIVE_METRICS = {
    "subscriptions.active_lines_eom",
    "finance.recognized_service_revenue",
    "finance.contribution_margin",
}
SERVICE_ASSURANCE_SOURCES = {"network", "enterprise", "finance", "care"}


class ExecutiveAccount(Contract):
    account_id: str = Field(min_length=1)
    account_key: int = Field(strict=True, ge=0, lt=2**53)
    active_lines_eom: int = Field(strict=True, ge=0, lt=2**53)
    recognized_service_revenue_cents: int = Field(strict=True, gt=-(2**53), lt=2**53)
    contribution_margin_cents: int = Field(strict=True, gt=-(2**53), lt=2**53)


class ExecutiveSourceQuery(Contract):
    model: Literal["subscriptions", "finance"]
    dax: str = Field(min_length=1)
    dax_sha256: Sha256
    response_sha256: Sha256
    observed_at: datetime

    @model_validator(mode="after")
    def original_query(self):
        if self.observed_at.tzinfo is None or hashlib.sha256(self.dax.encode()).hexdigest() != self.dax_sha256:
            raise ValueError("Executive query requires its original exact bytes and zoned observation")
        return self


class RecordedExecutiveDemo(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RECORDED_EXECUTIVE_NATIVE"] = "RECORDED_EXECUTIVE_NATIVE"
    captured_at: datetime
    question: str = Field(min_length=1)
    ontology: RecordedOntology
    focus_metric_ids: list[str] = Field(min_length=3, max_length=3)
    separate_native_item_verified: Literal[True]
    native_answer_origin: Literal["MANAGED_NATIVE_SERVICE"]
    native_answer_text: str = Field(min_length=1, max_length=200000)
    native_response_sha256: Sha256
    source_measure_review_sha256: Sha256
    native_answer_matches_source_values: Literal[True]
    rows: list[ExecutiveAccount] = Field(min_length=1, max_length=250)
    source_queries: list[ExecutiveSourceQuery] = Field(min_length=2, max_length=16)
    source_clocks: list[SourceClock] = Field(min_length=2)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def compact_native_capture(self):
        if self.captured_at.tzinfo is None:
            raise ValueError("Executive answer needs its original zoned capture time")
        if set(self.ontology.sources) != {"subscriptions", "finance"} or len(self.ontology.entities) != 4 or (
            set(self.focus_metric_ids) != EXECUTIVE_METRICS
            or not EXECUTIVE_METRICS <= {metric.id for metric in self.ontology.metrics}
        ):
            raise ValueError("Executive stream requires the actual compact ontology and its three source-owned focus metrics")
        if {query.model for query in self.source_queries} != set(self.ontology.sources) or (
            {clock.model for clock in self.source_clocks} != set(self.ontology.sources)
        ):
            raise ValueError("Executive source verification must preserve both actual model owners and clocks")
        if len({row.account_id for row in self.rows}) != len(self.rows) or (
            len({row.account_key for row in self.rows}) != len(self.rows)
        ):
            raise ValueError("Executive native answer account identities must not repeat")
        return self


class ServiceAssuranceHop(Contract):
    hop: int = Field(strict=True, ge=1, le=7)
    from_entity: str = Field(min_length=1)
    to_entity: str = Field(min_length=1)
    join_keys: list[str] = Field(min_length=1)
    validity: str = Field(min_length=1)
    crosses_models: bool = Field(strict=True)
    models_label: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class ServiceAssuranceModelEvidence(Contract):
    model_id: Literal["network", "enterprise", "finance", "care"]
    model_name: str = Field(min_length=1)
    home_tables: list[str] = Field(min_length=1)
    measures: list[str] = Field(min_length=1)
    grain: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    row_count: int = Field(strict=True, ge=0, lt=2**53)


class ServiceAssuranceAccountRow(Contract):
    account_key: int = Field(strict=True, ge=1, lt=2**53)
    account_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    region: str = Field(min_length=1)
    affected_sites: list[int] = Field(min_length=1)
    affected_contracts: list[int] = Field(min_length=1)
    credit_exposure_cents: int = Field(strict=True, gt=-(2**53), lt=2**53)
    recognized_service_revenue_cents: int = Field(strict=True, gt=-(2**53), lt=2**53)
    contribution_margin_cents: int = Field(strict=True, gt=-(2**53), lt=2**53)
    escalated_cases: int = Field(strict=True, ge=0, lt=2**53)
    repeat_contacts: int = Field(strict=True, ge=0, lt=2**53)


class ServiceAssuranceExclusion(Contract):
    category: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SurfaceQuestionTurn(Contract):
    question_id: str = Field(min_length=1)
    level: Literal["L2", "L4"]
    question: str = Field(min_length=1)
    tool_calls: list[str] = Field(min_length=1)
    models_queried: list[Literal["network", "enterprise", "finance", "care"]] = Field(min_length=1)
    answer_summary: str = Field(min_length=1)
    answer_markdown: str = Field(min_length=1, max_length=200000)
    row_count: int = Field(strict=True, ge=0, lt=2**53)
    exclusions_reported: int = Field(strict=True, ge=0, lt=2**53)


class RecordedAgentSurface(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RECORDED_AGENT_SURFACE"] = "RECORDED_AGENT_SURFACE"
    surface_id: Literal[
        "fabric-ontology-mcp",
        "foundry-agent",
        "github-copilot-vscode",
        "copilot-studio-m365",
    ]
    surface_name: str = Field(min_length=1)
    provenance: Literal[
        "LIVE_DEMO_TENANT_SANITIZED_CAPTURE",
        "STAGED_SURFACE_PREVIEW_CAPTURE",
    ]
    captured_at: datetime
    connection_summary: str = Field(min_length=1)
    turns: list[SurfaceQuestionTurn] = Field(min_length=2, max_length=4)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_surface_capture(self):
        if self.captured_at.tzinfo is None:
            raise ValueError("Agent surface recording requires a zoned UTC timestamp")
        levels = [turn.level for turn in self.turns]
        if len(set(levels)) != len(levels) or not {"L2", "L4"} <= set(levels):
            raise ValueError("Agent surface recording requires distinct L2 and L4 turns")
        return self

    def verify_private_identifiers_absent(self, values: set[str]):
        text = self.model_dump_json().casefold()
        if any(value and value.casefold() in text for value in values):
            raise ValueError("Private target or principal identifier remains in the surface capture")

    @classmethod
    def read(cls, path: Path):
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class RecordedServiceAssuranceDemo(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RECORDED_SERVICE_ASSURANCE"] = "RECORDED_SERVICE_ASSURANCE"
    generated_at: datetime
    profile: str = Field(min_length=1)
    period: str = Field(min_length=1)
    question: str = Field(min_length=1)
    ontology: RecordedOntology
    hops: list[ServiceAssuranceHop] = Field(min_length=7, max_length=7)
    model_evidence: list[ServiceAssuranceModelEvidence] = Field(min_length=4, max_length=4)
    ranked_accounts: list[ServiceAssuranceAccountRow] = Field(min_length=1, max_length=250)
    exclusions: list[ServiceAssuranceExclusion] = Field(min_length=1)
    agent_surfaces: list[RecordedAgentSurface] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_service_assurance_stream(self):
        if self.generated_at.tzinfo is None:
            raise ValueError("Service assurance recording requires a zoned UTC timestamp")
        if self.profile != "service-assurance":
            raise ValueError("Service assurance recording requires profile='service-assurance'")
        if set(self.ontology.sources) != SERVICE_ASSURANCE_SOURCES:
            raise ValueError("Service assurance ontology requires network, enterprise, finance and care sources")
        if (
            len(self.ontology.entities) != 17
            or len(self.ontology.relationships) != 17
            or len(self.ontology.metrics) != 14
            or len(self.ontology.rules) != 5
        ):
            raise ValueError("Service assurance ontology must match the 17-entity, 17-relationship, 14-metric, 5-rule contract")
        if [item.hop for item in self.hops] != list(range(1, 8)):
            raise ValueError("Service assurance path requires ordered hops 1 through 7")
        if {item.model_id for item in self.model_evidence} != SERVICE_ASSURANCE_SOURCES:
            raise ValueError("Service assurance model evidence must cover all four semantic models")
        if len({row.account_id for row in self.ranked_accounts}) != len(self.ranked_accounts) or (
            len({row.account_key for row in self.ranked_accounts}) != len(self.ranked_accounts)
        ):
            raise ValueError("Service assurance ranked account identities must not repeat")
        keys = [(-row.credit_exposure_cents, -row.contribution_margin_cents) for row in self.ranked_accounts]
        if keys != sorted(keys):
            raise ValueError(
                "Service assurance accounts must be ordered by credit_exposure_cents descending then contribution_margin_cents descending"
            )
        if len({surface.surface_id for surface in self.agent_surfaces}) != len(self.agent_surfaces):
            raise ValueError("Service assurance agent surfaces must not repeat surface_id")
        return self


class ScorecardContract(Contract):
    model_config = ConfigDict(extra="ignore")


class WilsonInterval(ScorecardContract):
    proportion: float = Field(ge=0.0, le=1.0)
    low: float = Field(ge=0.0, le=1.0)
    high: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.low - self.proportion > 1e-6 or self.proportion - self.high > 1e-6:
            raise ValueError("Wilson interval requires low <= proportion <= high")
        return self


class ScorecardConditions(ScorecardContract):
    execution_mode: Literal["offline_scripted", "live_demo_tenant"]
    profile: str = Field(min_length=1)
    seed: int | None = None
    question_count: int = Field(strict=True, ge=1, lt=2**53)
    repeats: int = Field(strict=True, ge=1, lt=2**53)
    arms: list[str] = Field(min_length=1)
    capacity_sku: str | None = None
    region: str | None = None
    model_deployment: str | None = None
    toolset_variant: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_conditions(self):
        if len(set(self.arms)) != len(self.arms) or any(not arm.strip() for arm in self.arms):
            raise ValueError("Scorecard conditions require distinct non-empty arm identifiers")
        if self.started_at is not None and self.started_at.tzinfo is None:
            raise ValueError("Scorecard started_at requires a zoned UTC timestamp")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("Scorecard completed_at requires a zoned UTC timestamp")
        return self


class ScorecardEffort(ScorecardContract):
    mean_tool_calls: float = Field(ge=0.0)
    mean_input_tokens: float = Field(ge=0.0)
    mean_output_tokens: float = Field(ge=0.0)
    mean_seconds: float = Field(ge=0.0)
    total_tokens: int = Field(ge=0, lt=2**53)


class ScorecardArmSummary(ScorecardContract):
    arm: str = Field(min_length=1)
    total_runs: int = Field(strict=True, ge=1, lt=2**53)
    question_count: int = Field(strict=True, ge=1, lt=2**53)
    repeats: int = Field(strict=True, ge=1, lt=2**53)
    answer_correct_runs: int = Field(strict=True, ge=0, lt=2**53)
    answer_accuracy: WilsonInterval
    mean_key_precision: float = Field(ge=0.0, le=1.0)
    mean_key_recall: float = Field(ge=0.0, le=1.0)
    mean_key_f1: float = Field(ge=0.0, le=1.0)
    path_accuracy: WilsonInterval
    unknown_handling_accuracy: WilsonInterval
    mean_repeat_stability: float = Field(ge=0.0, le=1.0)
    unanimous_pass_rate: float = Field(ge=0.0, le=1.0)
    traps_hit: dict[str, int] = Field(default_factory=dict)
    effort: ScorecardEffort

    @model_validator(mode="after")
    def validate_arm_counts(self):
        if self.answer_correct_runs > self.total_runs:
            raise ValueError("answer_correct_runs cannot exceed total_runs")
        if any(count < 0 for count in self.traps_hit.values()):
            raise ValueError("traps_hit counts must be non-negative")
        return self


class ScorecardDifference(ScorecardContract):
    comparison: str = Field(min_length=1)
    left_arm: str = Field(min_length=1)
    right_arm: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    diff: float = Field(ge=-1.0, le=1.0)
    low: float = Field(ge=-1.0, le=1.0)
    high: float = Field(ge=-1.0, le=1.0)
    excludes_zero: bool = Field(strict=True)

    @model_validator(mode="after")
    def validate_difference_interval(self):
        if self.low - self.diff > 1e-6 or self.diff - self.high > 1e-6:
            raise ValueError("Bootstrap interval requires low <= diff <= high")
        return self


class ScorecardLevelArmSummary(ScorecardContract):
    runs: int = Field(strict=True, ge=1, lt=2**53)
    answer_correct_runs: int = Field(strict=True, ge=0, lt=2**53)
    answer_accuracy: WilsonInterval
    mean_key_f1: float = Field(ge=0.0, le=1.0)
    path_accuracy: WilsonInterval
    unknown_handling_accuracy: WilsonInterval | None = None
    mean_repeat_stability: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_level_counts(self):
        if self.answer_correct_runs > self.runs:
            raise ValueError("Level answer_correct_runs cannot exceed runs")
        return self


class ScorecardLevelSummary(ScorecardContract):
    question_count: int = Field(strict=True, ge=1, lt=2**53)
    arms: dict[str, ScorecardLevelArmSummary] = Field(min_length=1)
    differences: list[ScorecardDifference] = Field(default_factory=list)


class ScorecardFailedQuestion(ScorecardContract):
    question_id: str = Field(min_length=1)
    level: str = Field(min_length=1)
    arm: str = Field(min_length=1)
    correct_repeats: int = Field(strict=True, ge=0, lt=2**53)
    repeats: int = Field(strict=True, ge=1, lt=2**53)
    traps_hit: list[str] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_repeats(self):
        if self.correct_repeats > self.repeats:
            raise ValueError("correct_repeats cannot exceed repeats")
        return self


class RecordedScorecard(ScorecardContract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RECORDED_SCORECARD"] = "RECORDED_SCORECARD"
    generated_at: datetime
    conditions: ScorecardConditions
    arm_labels: dict[str, str] = Field(min_length=1)
    summary_by_arm: dict[str, ScorecardArmSummary] = Field(min_length=1)
    by_level: dict[str, ScorecardLevelSummary] = Field(min_length=1)
    overall_differences: list[ScorecardDifference] = Field(default_factory=list)
    failed_questions: list[ScorecardFailedQuestion] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scorecard(self):
        if self.generated_at.tzinfo is None:
            raise ValueError("Scorecard recording requires a zoned UTC timestamp")
        expected_arms = set(self.conditions.arms)
        if set(self.summary_by_arm) != expected_arms:
            raise ValueError("summary_by_arm keys must match conditions.arms")
        for arm_key, summary in self.summary_by_arm.items():
            if summary.arm != arm_key:
                raise ValueError("summary_by_arm key must match summary.arm")
        if not expected_arms <= set(self.arm_labels) or any(
            not label.strip() for label in self.arm_labels.values()
        ):
            raise ValueError("arm_labels must provide a non-empty label for every arm in conditions.arms")
        if any(not item.strip() for item in self.limitations):
            raise ValueError("Scorecard limitations must be non-empty strings")
        return self

    def verify_private_identifiers_absent(self, values: set[str]):
        text = self.model_dump_json().casefold()
        if any(value and value.casefold() in text for value in values):
            raise ValueError("Private target or principal identifier remains in the scorecard")

    @classmethod
    def read(cls, path: Path):
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class RecordedDemoLibrary(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RECORDED_DEMO_LIBRARY"] = "RECORDED_DEMO_LIBRARY"
    exported_at: datetime
    executive: RecordedExecutiveDemo
    customer360: RecordedComparisons
    service_assurance: RecordedServiceAssuranceDemo | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    scorecard: RecordedScorecard | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def separate_native_streams(self):
        full = self.customer360.ontology
        if self.exported_at.tzinfo is None or full is None or len(full.sources) <= 2 or (
            len(full.entities) <= 4 or full.native_snapshot_hash == self.executive.ontology.native_snapshot_hash
        ):
            raise ValueError("Both streams require distinct actual small/full native captures and original zoned dates")
        if self.service_assurance is not None:
            if set(self.service_assurance.ontology.sources) != SERVICE_ASSURANCE_SOURCES or (
                len(self.service_assurance.hops) != 7
            ):
                raise ValueError("Service assurance stream requires four models and seven hops")
            if self.service_assurance.ontology.native_snapshot_hash in {
                self.executive.ontology.native_snapshot_hash,
                full.native_snapshot_hash,
            }:
                raise ValueError("Service assurance ontology snapshot must remain distinct from executive and customer360")
        return self

    def verify_private_identifiers_absent(self, values: set[str]):
        text = self.model_dump_json().casefold()
        if any(value and value.casefold() in text for value in values):
            raise ValueError("Private target or principal identifier remains in the demo library")

    @classmethod
    def read(cls, path: Path):
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
