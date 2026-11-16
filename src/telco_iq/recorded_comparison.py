"""Read-only projections of original application pairs; never executable plans or reader grants."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from telco_iq.catalog import digest
from telco_iq.comparison import ComparisonResult
from telco_iq.contracts import (
    Clocks,
    CohortExclusion,
    CohortFilter,
    ContextVerification,
    Contract,
    Receipt,
    Scalar,
    Sha256,
    SourceClock,
    Status,
)
from telco_iq.events import ExecutionEvent
from telco_iq.evidence import sanitized
from telco_iq.recorded_ontology import RecordedOntology


class RecordedNode(Contract):
    id: str
    kind: str
    model: str | None = Field(default=None, exclude_if=lambda value: value is None)
    table: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source: str | None = Field(default=None, exclude_if=lambda value: value is None)
    left: str | None = Field(default=None, exclude_if=lambda value: value is None)
    right: str | None = Field(default=None, exclude_if=lambda value: value is None)
    relationship: str | None = Field(default=None, exclude_if=lambda value: value is None)
    depends_on: list[str] = Field(default_factory=list)
    additional_sources: dict[str, str] = Field(default_factory=dict)
    cohort_filters: list[CohortFilter] = Field(default_factory=list)


class RecordedPlan(Contract):
    nodes: list[RecordedNode]
    hypotheses: list[str] = Field(default_factory=list)


class RecordedAnswer(Contract):
    rows: list[dict[str, Scalar]]
    status: Status
    grain: list[str] | None
    evidence_ids: list[str]
    coverage: dict[str, int]
    warnings: list[str]
    clocks: Clocks
    source_clocks: list[SourceClock]
    population_qualification: Literal["unqualified", "known_mapped_subset"] = "unqualified"
    exclusions: list[CohortExclusion] = Field(default_factory=list)

    @model_validator(mode="after")
    def qualification(self):
        if self.population_qualification == "known_mapped_subset" and (
            self.status != Status.PARTIAL or not self.exclusions or self.coverage.get("unknown_source_rows", 0)
        ):
            raise ValueError("Recorded mapping qualification must retain actual PARTIAL exclusions")
        return self


class RecordedExecution(Contract):
    request_id: str
    status: Status
    mode: Literal["LIVE_DELEGATED"]
    answer: RecordedAnswer | None
    additional_answers: dict[str, RecordedAnswer] = Field(default_factory=dict, max_length=4)
    context_verification: ContextVerification | None = None
    receipts: list[Receipt]
    errors: list[str]
    elapsed_ms: float = Field(ge=0)


class RecordedArm(Contract):
    arm: Literal["baseline", "native"]
    status: Status
    plan: RecordedPlan | None
    planning_receipt: dict[str, JsonValue] | None
    execution: RecordedExecution | None
    errors: list[str]
    elapsed_ms: float = Field(ge=0)


class RecordedPair(Contract):
    pair_id: str
    question: str
    mode: Literal["LIVE_DELEGATED"]
    source_facts_hash: str
    context_revision: str
    provider: str
    model_version: str
    arms: list[RecordedArm] = Field(min_length=2, max_length=2)
    comparability: Literal["NOT_COMPARABLE", "COMMON_SOURCE_STATE"]
    comparability_reasons: list[str]
    limitations: list[str]

    @model_validator(mode="after")
    def actual_native_answer(self):
        if {arm.arm for arm in self.arms} != {"baseline", "native"}:
            raise ValueError("Recorded pair needs the two distinct actual arms")
        native = next(arm for arm in self.arms if arm.arm == "native")
        if native.execution is None or native.execution.answer is None or (
            native.errors or native.execution.errors or not native.execution.answer.rows
            or native.status not in (Status.COMPLETE, Status.PARTIAL)
        ):
            raise ValueError("Recorded fallback requires an actual positive native answer")
        return self


class RecordedEntry(Contract):
    title: str = Field(min_length=1, max_length=80)
    kind: Literal["getting_started", "story"] = "story"
    run_started_at: datetime
    original_result_sha256: Sha256
    original_server_receipt_sha256: Sha256
    source_value_review_sha256: Sha256
    result: RecordedPair
    events: dict[Literal["baseline", "native"], list[ExecutionEvent]]

    @model_validator(mode="after")
    def original_capture(self):
        if self.run_started_at.tzinfo is None or set(self.events) != {"baseline", "native"}:
            raise ValueError("Recorded run requires its original zoned time and distinct event streams")
        for arm in self.result.arms:
            if any(
                arm.execution is None or event.run_id != arm.execution.request_id for event in self.events[arm.arm]
            ):
                raise ValueError("Recorded events must belong to their original execution")
        if self.kind == "getting_started":
            native = next(arm for arm in self.result.arms if arm.arm == "native")
            if {receipt.model for receipt in native.execution.receipts} != {"subscriptions", "finance"}:
                raise ValueError("Getting-started capture must actually use only its two source models")
        return self

    @classmethod
    def from_actual(
        cls, title: str, *, result_file: Path, server_file: Path, intent_file: Path, review_file: Path,
        kind: Literal["getting_started", "story"] = "story",
    ):
        raw = result_file.read_bytes()
        result = ComparisonResult.model_validate_json(raw)
        server = json.loads(server_file.read_bytes())
        intent = json.loads(intent_file.read_bytes())
        review = json.loads(review_file.read_bytes())
        original_hash = hashlib.sha256(raw).hexdigest()
        if server.get("kind") != "ACTUAL_APPLICATION_PAIRED_RUN" or (
            digest(server.get("result")) != digest(sanitized(result.model_dump(mode="json")))
            or intent.get("question") != result.question or review.get("pair_id") != result.pair_id
            or review.get("original_result_sha256") != original_hash
            or not any(arm["arm"] == "native" and arm.get("metric_cells_checked", 0) > 0 for arm in review["arms"])
        ):
            raise ValueError("Recorded output must match the actual terminal original and numerical review")
        allowed = set(RecordedNode.model_fields)
        answer_fields = set(RecordedAnswer.model_fields)
        arms = []
        for arm in result.arms:
            plan = RecordedPlan(nodes=[
                RecordedNode.model_validate(node.model_dump(mode="json", include=allowed))
                for node in arm.plan.nodes
            ], hypotheses=arm.plan.hypotheses) if arm.plan else None
            execution = None
            if arm.execution:
                value = arm.execution.model_dump(mode="json")
                for key in ("answer",):
                    if value[key] is not None:
                        value[key] = {k: v for k, v in value[key].items() if k in answer_fields}
                value["additional_answers"] = {
                    label: {k: v for k, v in view.items() if k in answer_fields}
                    for label, view in value.get("additional_answers", {}).items()
                }
                execution = RecordedExecution.model_validate(value)
            planning = {
                key: value for key, value in (arm.planning_receipt or {}).items()
                if key in {"provider", "model_version", "prompt_hash", "context_hash", "facts_hash",
                           "validation_status", "validation_error"}
            }
            arms.append(RecordedArm(
                arm=arm.arm, status=arm.status, plan=plan, planning_receipt=planning or None,
                execution=execution, errors=arm.errors, elapsed_ms=arm.elapsed_ms,
            ))
        safe_result = result.model_dump(mode="json", include=set(RecordedPair.model_fields) - {"arms"})
        safe_result["arms"] = arms
        events = {"baseline": [], "native": []}
        for event in server.get("events", []):
            if event.get("phase") == "execution" and event.get("event") is not None:
                events[event["arm"]].append(ExecutionEvent.model_validate(event["event"]))
        return cls(
            title=title, kind=kind, run_started_at=intent["started_at"], original_result_sha256=original_hash,
            original_server_receipt_sha256=hashlib.sha256(server_file.read_bytes()).hexdigest(),
            source_value_review_sha256=hashlib.sha256(review_file.read_bytes()).hexdigest(),
            result=RecordedPair.model_validate(safe_result), events=events,
        )


class RecordedComparisons(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RECORDED_LIVE_COMPARISON"] = "RECORDED_LIVE_COMPARISON"
    exported_at: datetime
    entries: list[RecordedEntry] = Field(min_length=1, max_length=10)
    limitations: list[str] = Field(min_length=1)
    ontology: RecordedOntology | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def explicit_recording(self):
        if self.exported_at.tzinfo is None:
            raise ValueError("Export time requires an explicit timezone")
        if len({entry.result.pair_id for entry in self.entries}) != len(self.entries):
            raise ValueError("Recorded pair identities must not repeat")
        if self.ontology is not None and any(
            view.clocks.ontology != self.ontology.native_snapshot_hash
            for entry in self.entries for arm in entry.result.arms if arm.execution is not None
            for view in [arm.execution.answer, *arm.execution.additional_answers.values()] if view is not None
        ):
            raise ValueError("Recorded definition must match the original answer's ontology clock")
        return self

    def verify_private_identifiers_absent(self, private_values: set[str]) -> None:
        text = self.model_dump_json().casefold()
        if any(value and value.casefold() in text for value in private_values):
            raise ValueError("Private target or principal identifier remains in the recorded packet")

    @classmethod
    def read(cls, path: Path):
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
