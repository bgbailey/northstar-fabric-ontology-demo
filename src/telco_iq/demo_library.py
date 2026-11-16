"""Display-only archives of the two demo streams, not a native wire schema or reader grant."""

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


class RecordedDemoLibrary(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RECORDED_DEMO_LIBRARY"] = "RECORDED_DEMO_LIBRARY"
    exported_at: datetime
    executive: RecordedExecutiveDemo
    customer360: RecordedComparisons
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def separate_native_streams(self):
        full = self.customer360.ontology
        if self.exported_at.tzinfo is None or full is None or len(full.sources) <= 2 or (
            len(full.entities) <= 4 or full.native_snapshot_hash == self.executive.ontology.native_snapshot_hash
        ):
            raise ValueError("Both streams require distinct actual small/full native captures and original zoned dates")
        return self

    def verify_private_identifiers_absent(self, values: set[str]):
        text = self.model_dump_json().casefold()
        if any(value and value.casefold() in text for value in values):
            raise ValueError("Private target or principal identifier remains in the demo library")

    @classmethod
    def read(cls, path: Path):
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
