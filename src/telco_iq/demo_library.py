"""Display-only archives of the recorded demo streams, not a wire schema or user grant."""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

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


class RecordedDemoLibrary(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RECORDED_DEMO_LIBRARY"] = "RECORDED_DEMO_LIBRARY"
    exported_at: datetime
    executive: RecordedExecutiveDemo
    customer360: RecordedComparisons
    service_assurance: RecordedServiceAssuranceDemo | None = Field(
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
