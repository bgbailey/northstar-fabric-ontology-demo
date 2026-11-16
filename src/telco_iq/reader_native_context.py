"""Explicit, current-reader authorization of reviewed author-origin metadata.

No loader accepts a serialized reader grant. The server must pin an independently
reviewed enrollment digest and supply current-session probes. Enrollment records
belong in private configuration: ontology identity is absent from the old capture.
Source fingerprints are hashes of the reviewed probe's actual schema output, not
hashes of model aliases or asserted permissions.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from telco_iq.catalog import digest
from telco_iq.contracts import Context, Contract, Scope, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import (
    ObservedSourceState,
    ReaderBinding,
    ReaderProbeReceipt,
    SourceIdentity,
)
from telco_iq.native_context import NativeSnapshot, native_entities_fingerprint, native_rules_fingerprint

ReaderProbe = Callable[[ReaderBinding], Awaitable[ReaderProbeReceipt]]
SessionActive = Callable[[Scope], bool]
_ACQUIRED = object()


class ReviewedReaderEnrollment(Contract):
    """Server-reviewed capture ownership and schema expectations, never a grant.

    The digest must come from trusted server configuration independently of this
    record. An expiry bounds reuse of metric/edge metadata not exposed by the
    currently observed native reader tools. Only the captured, reviewed
    entity-only formal Rule shape is supported; property/relationship scopes
    remain blocked until actually observed and reviewed.
    """

    native_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ontology_workspace_id: UUID
    ontology_item_id: UUID
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_mcp_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: dict[str, SourceIdentity]
    source_schema_hashes: dict[str, str]
    review_reference: str = Field(min_length=1)
    reviewed_at: datetime
    expires_at: datetime
    max_probe_age_seconds: int = Field(default=300, ge=1, le=300)

    @model_validator(mode="after")
    def reviewed_evidence(self):
        if (
            not self.review_reference.strip()
            or self.reviewed_at.tzinfo is None or self.expires_at.tzinfo is None
            or self.expires_at <= self.reviewed_at
            or not self.sources or set(self.sources) != set(self.source_schema_hashes)
            or any(not re.fullmatch(r"[0-9a-f]{64}", h) for h in self.source_schema_hashes.values())
        ):
            raise ValueError("Enrollment requires reviewed, expiring, exact source-schema evidence")
        return self

    @property
    def enrollment_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


def _session(session_active: SessionActive, scope: Scope) -> None:
    if not callable(session_active) or session_active(scope.model_copy(deep=True)) is not True:
        raise LabError("Reader session ended or authorization epoch changed", Status.BLOCKED_AUTH)


def _enrolled(
    snapshot: NativeSnapshot, binding: ReaderBinding, enrollment: ReviewedReaderEnrollment,
    enrollment_digest: str, now: datetime,
) -> None:
    snapshot.verify(snapshot.context.scope)
    if (
        not isinstance(enrollment_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", enrollment_digest)
        or enrollment.enrollment_digest != enrollment_digest
    ):
        raise LabError("Reader enrollment does not match trusted review digest", Status.BLOCKED_AUTH)
    if not snapshot.captured_at <= enrollment.reviewed_at <= now < enrollment.expires_at:
        raise LabError("Reader enrollment is not current; review capture freshness", Status.BLOCKED_CAPABILITY)
    try:
        sources = {
            source.alias: SourceIdentity(workspace_id=source.workspace_id, dataset_id=source.dataset_id)
            for source in snapshot.sources
        }
    except ValueError as error:
        raise LabError("Snapshot lacks reviewed source UUID identities", Status.BLOCKED_CAPABILITY) from error
    if (
        binding.scope.mode != "LIVE_DELEGATED"
        or len(binding.sources) != len(snapshot.sources)
        or binding.sources != sources or enrollment.sources != sources
        or set(binding.scope.models) != set(sources)
        or binding.native_snapshot_hash != snapshot.content_hash
        or enrollment.native_snapshot_hash != snapshot.content_hash
        or enrollment.ontology_workspace_id != binding.ontology_workspace_id
        or enrollment.ontology_item_id != binding.ontology_item_id
        or enrollment.definition_hash != snapshot.evidence_hashes["definition"]
        or enrollment.native_mcp_receipt_hash != snapshot.evidence_hashes["native_mcp_receipt"]
    ):
        raise LabError("Reader binding differs from reviewed capture ownership/provenance", Status.BLOCKED_AUTH)


def _capture_values(snapshot: NativeSnapshot, receipt: dict) -> tuple[list[dict], list[dict]]:
    if digest(receipt) != snapshot.evidence_hashes["native_mcp_receipt"]:
        raise LabError("Original native capture receipt hash mismatch", Status.BLOCKED_CAPABILITY)
    try:
        if (
            receipt["lane"] != snapshot.capture_lane
            or receipt["definitionReceipt"] != snapshot.evidence_hashes["definition"] + ".json"
            or datetime.fromisoformat(receipt["completedAt"]) != snapshot.captured_at
        ):
            raise ValueError("Capture linkage changed")
        results = []
        for tool in ("list_ontology_entities", "list_ontology_rules"):
            raw = receipt["rawNativeContext"][tool]
            if raw["isError"] is not False or set(raw["structuredContent"]) != {"values"}:
                raise ValueError("Unreviewed native values envelope")
            values = raw["structuredContent"]["values"]
            if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
                raise ValueError("Unreviewed native values")
            results.append(values)
    except (KeyError, TypeError, ValueError) as error:
        raise LabError("Unsupported original native capture receipt", Status.BLOCKED_CAPABILITY) from error
    native_rules_fingerprint(results[1])
    return results[0], results[1]


def _probe_evidence(
    receipt: ReaderProbeReceipt, binding: ReaderBinding, enrollment: ReviewedReaderEnrollment,
    entities: list[dict], rules: list[dict], started_at: datetime, now: datetime,
) -> None:
    if receipt.binding_hash != binding.binding_hash:
        raise LabError("Probe belongs to a different reader binding/session epoch", Status.BLOCKED_AUTH)
    if (
        not started_at <= receipt.checked_at <= now
        or (now - receipt.checked_at).total_seconds() > enrollment.max_probe_age_seconds
    ):
        raise LabError("Reader probe is stale, future-dated or predates acquisition", Status.BLOCKED_CAPABILITY)
    if native_entities_fingerprint(receipt.native_entities) != native_entities_fingerprint(entities) or (
        native_rules_fingerprint(receipt.native_rules) != native_rules_fingerprint(rules)
    ):
        raise LabError("Actual native entity/property/rule values changed; recapture and review", Status.BLOCKED_CAPABILITY)
    if receipt.source_schema_hashes != enrollment.source_schema_hashes:
        raise LabError("Actual source schema fingerprints differ from reviewed capture", Status.BLOCKED_CAPABILITY)
    if (
        not receipt.evidence_hashes
        or any(not key or not re.fullmatch(r"[0-9a-f]{64}", value)
               for key, value in receipt.evidence_hashes.items())
        or not set(receipt.observed_source_states) <= binding.sources.keys()
    ):
        raise LabError("Reader probe lacks valid bound evidence", Status.BLOCKED_CAPABILITY)
    for state in receipt.observed_source_states.values():
        if not started_at <= state.observed_at <= now:
            raise LabError("Observed source state is outside this acquisition", Status.BLOCKED_CAPABILITY)


class ReaderNativeContext:
    """In-memory acquisition result; intentionally not a serializable grant model."""

    def __init__(
        self, *, _acquired: object, snapshot: NativeSnapshot, binding: ReaderBinding,
        enrollment: ReviewedReaderEnrollment, enrollment_digest: str,
        receipt: ReaderProbeReceipt, session_active: SessionActive,
    ):
        if _acquired is not _ACQUIRED:
            raise LabError("Use acquire_reader_context; configuration cannot grant reader access", Status.BLOCKED_AUTH)
        self._snapshot = snapshot.model_copy(deep=True)
        self._binding = binding.model_copy(deep=True)
        self._enrollment = enrollment.model_copy(deep=True)
        self._enrollment_digest = enrollment_digest
        self._receipt = receipt.model_copy(deep=True)
        self._verification = receipt.model_copy(deep=True)
        self._session_active = session_active
        self._evidence_hashes = {
            **{f"capture.{key}": value for key, value in snapshot.evidence_hashes.items()},
            **{f"probe.{key}": value for key, value in receipt.evidence_hashes.items()},
            "reader_probe_receipt": digest(receipt.model_dump(mode="json")),
            "reviewed_reader_enrollment": enrollment_digest,
        }
        self._context = snapshot.context.model_copy(deep=True, update={
            "scope": binding.scope.model_copy(deep=True),
            "revision": digest({
                "snapshot": snapshot.content_hash, "binding": binding.binding_hash,
                "evidence": self._evidence_hashes,
            }),
            "source": "Current-reader-authorized native metadata snapshot; author origin, not live source data",
            "provenance": snapshot.context.provenance + [
                f"reader_binding:{binding.binding_hash}",
                f"reviewed_reader_enrollment:{enrollment_digest}",
                f"reader_probe_receipt:{self._evidence_hashes['reader_probe_receipt']}",
                f"reader_checked_at:{receipt.checked_at.isoformat()}",
                "Native entity/rule reads and source-schema fingerprints verified for current session.",
                "Uniquely named entity/property collections matched without depending on response order; all other values retained.",
                "Metrics/edges retain reviewed author-capture origin; schema is not a data watermark.",
            ],
        })
        self._integrity = self._hash()

    def _hash(self) -> str:
        return digest({
            "binding": self._binding.model_dump(mode="json"),
            "context": self._context.model_dump(mode="json"),
            "receipt": self._receipt.model_dump(mode="json"),
            "evidence": self._evidence_hashes, "snapshot": self._snapshot.content_hash,
            "enrollment": self._enrollment_digest,
            "verification": self._verification.model_dump(mode="json"),
        })

    @property
    def binding(self) -> ReaderBinding:
        return self._binding.model_copy(deep=True)

    @property
    def context(self) -> Context:
        return self._context.model_copy(deep=True)

    @property
    def snapshot(self) -> NativeSnapshot:
        return self._snapshot.model_copy(deep=True)

    @property
    def metrics(self) -> list[dict]:
        return deepcopy(self._snapshot.metrics)

    @property
    def evidence_hashes(self) -> dict[str, str]:
        return dict(self._evidence_hashes)

    @property
    def probe_receipt(self) -> ReaderProbeReceipt:
        return self._receipt.model_copy(deep=True)

    @property
    def observed_source_states(self) -> dict[str, ObservedSourceState]:
        return self.probe_receipt.observed_source_states

    @property
    def verification_receipt(self) -> ReaderProbeReceipt:
        return self._verification.model_copy(deep=True)

    def verify(self, scope: Scope) -> None:
        if scope != self._binding.scope:
            raise LabError("Reader bundle scope/session epoch mismatch", Status.BLOCKED_AUTH)
        _session(self._session_active, scope)
        now = datetime.now(timezone.utc)
        _enrolled(self._snapshot, self._binding, self._enrollment, self._enrollment_digest, now)
        if self._hash() != self._integrity:
            raise LabError("Reader acquisition evidence was modified", Status.BLOCKED_AUTH)
        if not 0 <= (now - self._verification.checked_at).total_seconds() <= self._enrollment.max_probe_age_seconds:
            raise LabError("Reader probe expired; reacquire current context", Status.BLOCKED_CAPABILITY)


async def revalidate_reader_context(
    bundle: ReaderNativeContext, *, probe: ReaderProbe, scope: Scope, capture_receipt: dict,
) -> ReaderProbeReceipt:
    """Renew only current-reader verification through real matching reads, never its original context or clocks."""
    if not isinstance(bundle, ReaderNativeContext):
        raise LabError("Revalidation requires an actually acquired reader bundle", Status.BLOCKED_AUTH)
    bundle.verify(scope)
    started = datetime.now(timezone.utc)
    entities, rules = _capture_values(bundle._snapshot, deepcopy(capture_receipt))
    receipt = await probe(bundle.binding)
    if not isinstance(receipt, ReaderProbeReceipt):
        raise LabError("Reader revalidation requires actual typed probe evidence", Status.BLOCKED_CAPABILITY)
    receipt = ReaderProbeReceipt.model_validate(receipt.model_dump(mode="json"))
    _session(bundle._session_active, scope)
    _enrolled(bundle._snapshot, bundle._binding, bundle._enrollment, bundle._enrollment_digest,
              datetime.now(timezone.utc))
    if bundle._hash() != bundle._integrity:
        raise LabError("Reader acquisition changed during revalidation", Status.BLOCKED_AUTH)
    _probe_evidence(receipt, bundle._binding, bundle._enrollment, entities, rules,
                    started, datetime.now(timezone.utc))
    bundle._verification = receipt.model_copy(deep=True)
    bundle._integrity = bundle._hash()
    return bundle.verification_receipt


async def acquire_reader_context(
    snapshot: NativeSnapshot, binding: ReaderBinding, *, probe: ReaderProbe,
    session_active: SessionActive, enrollment: ReviewedReaderEnrollment,
    enrollment_digest: str, capture_receipt: dict,
) -> ReaderNativeContext:
    """Acquire once, then recheck epoch and bounded freshness on every verify.

    ``session_active(scope)`` is synchronous and must check the exact current
    authenticated session/authorization revision, not a browser-supplied boolean.
    ``probe(binding)`` must return actual current reads of those exact items.
    There is no author-token fallback or inferred ontology enrollment.
    """
    if (
        not isinstance(snapshot, NativeSnapshot) or not isinstance(binding, ReaderBinding)
        or not isinstance(enrollment, ReviewedReaderEnrollment)
    ):
        raise LabError("Acquisition requires explicit reviewed typed inputs", Status.BLOCKED_CAPABILITY)
    snapshot = snapshot.model_copy(deep=True)
    try:
        binding = ReaderBinding.model_validate(binding.model_dump(mode="json"))
        enrollment = ReviewedReaderEnrollment.model_validate(enrollment.model_dump(mode="json"))
    except ValidationError as error:
        raise LabError("Reader binding/enrollment contract is invalid", Status.BLOCKED_CAPABILITY) from error
    capture_receipt = deepcopy(capture_receipt)
    started_at = datetime.now(timezone.utc)
    _session(session_active, binding.scope)
    _enrolled(snapshot, binding, enrollment, enrollment_digest, started_at)
    entities, rules = _capture_values(snapshot, capture_receipt)
    receipt = await probe(binding.model_copy(deep=True))
    if not isinstance(receipt, ReaderProbeReceipt):
        raise LabError("Reader probe must return the reviewed receipt contract", Status.BLOCKED_CAPABILITY)
    try:
        receipt = ReaderProbeReceipt.model_validate(receipt.model_dump(mode="json"))
    except ValidationError as error:
        raise LabError("Reader probe contract is invalid", Status.BLOCKED_CAPABILITY) from error
    _session(session_active, binding.scope)
    _probe_evidence(
        receipt, binding, enrollment, entities, rules, started_at, datetime.now(timezone.utc),
    )
    result = ReaderNativeContext(
        _acquired=_ACQUIRED, snapshot=snapshot, binding=binding, enrollment=enrollment,
        enrollment_digest=enrollment_digest, receipt=receipt, session_active=session_active,
    )
    result.verify(binding.scope)
    return result
