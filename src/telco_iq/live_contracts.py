"""Shared identities and observed evidence for the opt-in live bridge."""

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from time import perf_counter
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, model_validator

from telco_iq.catalog import digest
from telco_iq.contracts import Budgets, Contract, Scope, Status
from telco_iq.errors import LabError


class SourceCallBudget:
    """One prepare/run allowance, including revision and coverage controls."""

    def __init__(self, limits: Budgets, *, phase_local_timeouts: bool = False):
        self.limits = Budgets.model_validate(limits.model_dump())
        self.started = perf_counter()
        self.requests = self.rows = self.bytes = 0
        self._planning_depth = 0
        self._planning_started: float | None = None
        self._phase_local_timeouts = phase_local_timeouts
        self._phases = ("metadata", "preflight", "execution", "postflight")
        self._phase_index = 0

    def begin_phase(self, phase: str) -> None:
        if not self._phase_local_timeouts or self._planning_depth or (
            self._phase_index + 1 >= len(self._phases)
            or phase != self._phases[self._phase_index + 1]
        ):
            raise LabError("Source timing phase is unconfigured, repeated or out of order",
                           Status.BLOCKED_CAPABILITY)
        self.check()
        self._phase_index += 1
        self.started = perf_counter()

    def _elapsed(self):
        return (self._planning_started if self._planning_started is not None else perf_counter()) - self.started

    @contextmanager
    def suspend_for_planning(self):
        self.check()
        if not self._planning_depth:
            self._planning_started = perf_counter()
        self._planning_depth += 1
        try:
            yield
        finally:
            self._planning_depth -= 1
            if not self._planning_depth:
                assert self._planning_started is not None
                self.started += perf_counter() - self._planning_started
                self._planning_started = None

    def check(self) -> None:
        if self._elapsed() >= self.limits.timeout_seconds:
            phase = f" in {self._phases[self._phase_index]} phase" if self._phase_local_timeouts else ""
            raise LabError("Source request time budget exhausted" + phase, Status.TIMED_OUT)
        if self.requests >= self.limits.max_queries:
            raise LabError("Source request budget exhausted, including control queries", Status.PARTIAL)

    def reserve(self) -> None:
        if self._planning_depth:
            raise LabError("Source requests cannot run during the isolated model-planning phase",
                           Status.BLOCKED_CAPABILITY)
        self.check()
        self.requests += 1

    def response(self, raw: dict) -> None:
        self.response_bytes(len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8")))

    def response_bytes(self, size: int) -> None:
        self.bytes += size
        if self.bytes > self.limits.max_bytes:
            raise LabError("Source response byte budget exhausted", Status.PARTIAL)

    def remaining_seconds(self) -> float:
        remaining = self.limits.timeout_seconds - self._elapsed()
        if remaining <= 0:
            raise LabError("Source request time budget exhausted", Status.TIMED_OUT)
        return remaining

    def returned(self, rows: int) -> None:
        self.rows += rows
        if self.rows > self.limits.max_rows:
            raise LabError("Source returned-row budget exhausted, including controls", Status.PARTIAL)


class SourceIdentity(Contract):
    workspace_id: UUID
    dataset_id: UUID


def accepted_refresh_location(
    receipt: dict, workspace: str, dataset: str, *, reviewed_regional_origin: str | None = None,
) -> tuple[str, str]:
    for value in (workspace, dataset):
        if str(UUID(value)) != value:
            raise ValueError("Canonical source group/dataset identities are required")
    if receipt.get("status") != 202 or receipt.get("method") != "POST":
        raise ValueError("Refresh has no exact accepted identity; read-only reconciliation required")
    base = f"/v1.0/myorg/groups/{workspace}/datasets/{dataset}/refreshes"
    raw = receipt.get("location")
    if receipt.get("path") != base or not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ValueError("Refresh accepted Location/target absent")
    origins = {"https://api.powerbi.com"}
    if reviewed_regional_origin is not None:
        if not re.fullmatch(r"https://wabi-[a-z0-9-]+\.analysis\.windows\.net", reviewed_regional_origin):
            raise ValueError("Refresh regional Location requires an exact reviewed HTTPS origin")
        origins.add(reviewed_regional_origin)
    parsed = urlsplit(raw)
    if (parsed.scheme or parsed.netloc) and parsed.scheme + "://" + parsed.netloc not in origins:
        raise ValueError("Refresh Location origin is not reviewed; no regional credential dispatch")
    if parsed.query or parsed.fragment or any(char in raw for char in "\r\n\0"):
        raise ValueError("Refresh Location query/fragment is not reviewed")
    path = parsed.path
    request_id = path.removeprefix(base + "/")
    if path != base + "/" + request_id or str(UUID(request_id)) != request_id:
        raise ValueError("Refresh Location escaped its exact dataset/group")
    if receipt.get("requestId") not in (None, request_id):
        raise ValueError("Refresh Location/request identity disagree")
    return request_id, path


class ReaderBinding(Contract):
    scope: Scope
    ontology_workspace_id: UUID
    ontology_item_id: UUID
    native_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: dict[str, SourceIdentity]

    @model_validator(mode="after")
    def identities(self):
        if self.scope.mode != "LIVE_DELEGATED" or set(self.sources) != set(self.scope.models):
            raise ValueError("Reader binding must match the current live source scope")
        if len(self.scope.models) != len(set(self.scope.models)):
            raise ValueError("Reader source aliases must be unique")
        identities = [(source.workspace_id, source.dataset_id) for source in self.sources.values()]
        if not identities or len(identities) != len(set(identities)):
            raise ValueError("Reader source identities must be nonempty and distinct")
        return self

    @property
    def binding_hash(self) -> str:
        return digest(self.model_dump(mode="json"))


class ObservedSourceState(Contract):
    data_revision: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime

    @model_validator(mode="after")
    def timestamp(self):
        if self.observed_at.tzinfo is None:
            raise ValueError("Source evidence requires an observed timezone")
        return self


class ReaderSourceStateLease:
    """One pair's actual preflight observations; never changes their original clocks."""

    max_age_seconds = 300

    def __init__(self, binding: ReaderBinding, session_active):
        self.binding = ReaderBinding.model_validate(binding.model_dump(mode="json"))
        self.session_active = session_active
        self.states: dict[str, ObservedSourceState] = {}
        self._integrity: str | None = None

    def _current(self):
        if not self.session_active(self.binding.scope):
            raise LabError("Reader source-state lease ended with its session", Status.BLOCKED_AUTH)
        if self.states and self._integrity != digest({
            alias: value.model_dump(mode="json") for alias, value in self.states.items()
        }):
            raise LabError("Original source-state lease evidence changed", Status.BLOCKED_AUTH)

    def seed(
        self, states: dict[str, ObservedSourceState], started_at: datetime, *, required_models: set[str] | None = None,
    ):
        self._current()
        now = datetime.now(timezone.utc)
        required = set(self.binding.sources) if required_models is None else set(required_models)
        if not required or not required <= self.binding.sources.keys() or (
            self.states or set(states) != required or started_at.tzinfo is None
        ) or any(
            not isinstance(value, ObservedSourceState) or not started_at <= value.observed_at <= now
            or (now - value.observed_at).total_seconds() > self.max_age_seconds
            for value in states.values()
        ):
            raise LabError("Source-state lease requires one actual complete pair preflight", Status.BLOCKED_CAPABILITY)
        self.states = {alias: value.model_copy(deep=True) for alias, value in states.items()}
        self._integrity = digest({alias: value.model_dump(mode="json") for alias, value in self.states.items()})

    async def __call__(self, binding: ReaderBinding, alias: str):
        self._current()
        value = self.states.get(alias)
        if binding != self.binding or value is None:
            raise LabError("Source-state lease belongs to another reader/source", Status.BLOCKED_AUTH)
        if not 0 <= (datetime.now(timezone.utc) - value.observed_at).total_seconds() <= self.max_age_seconds:
            raise LabError("Original source-state lease expired; no clock freshening", Status.BLOCKED_CAPABILITY)
        return value.model_copy(deep=True)

    def reconcile(self, states: dict[str, ObservedSourceState], started_at: datetime):
        self._current()
        now = datetime.now(timezone.utc)
        if set(states) != set(self.states) or not self.states or started_at.tzinfo is None or any(
            not isinstance(value, ObservedSourceState) or not started_at <= value.observed_at <= now
            for value in states.values()
        ):
            raise LabError("Actual complete post-pair source verification is absent", Status.PARTIAL)
        if any(
            (states[alias].data_revision, states[alias].model_revision)
            != (before.data_revision, before.model_revision)
            for alias, before in self.states.items()
        ):
            raise LabError("Source revisions changed across the paired run; complete answers withheld", Status.PARTIAL)


class ReaderProbeReceipt(Contract):
    binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_at: datetime
    native_entities: list[dict]
    native_rules: list[dict]
    source_schema_hashes: dict[str, str]
    observed_source_states: dict[str, ObservedSourceState] = Field(default_factory=dict)
    evidence_hashes: dict[str, str]

    @model_validator(mode="after")
    def evidence(self):
        if self.checked_at.tzinfo is None or not self.evidence_hashes:
            raise ValueError("Reader probes require timestamped actual evidence")
        return self
