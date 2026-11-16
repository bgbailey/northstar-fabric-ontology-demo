"""Local immutable-proof reuse: exact byte checks, no source calls or freshness promotion."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from test_executor import population
from test_private_evidence import capture, store
from test_reader_native_context import reader_case

from telco_iq.contracts import Budgets, Contract, Scope, Status
from telco_iq.errors import LabError
from telco_iq.live_demo import ConfiguredLiveGateway
from telco_iq.private_evidence import PinnedEvidenceReader


def test_pinned_reads_validate_once_but_rehash_the_original_file_on_every_check(tmp_path, monkeypatch):
    sink = store(tmp_path)
    reference = capture(sink)
    checked = []
    original = sink._verified_record
    def verify(key):
        checked.append(key)
        return original(key)
    monkeypatch.setattr(sink, "_verified_record", verify)
    pinned = PinnedEvidenceReader(sink)
    assert pinned.read(reference.capture_id) == sink.read(reference.capture_id)
    pinned.seal()
    before = len(checked)
    for _ in range(5):
        pinned.verify()
    assert len(checked) == before
    path = sink.directory / f"tq005-{reference.capture_id}.json"
    raw = path.read_bytes()
    path.write_bytes(raw + b" ")
    with pytest.raises(LabError, match="bytes changed"):
        pinned.verify()
    path.write_bytes(raw)
    pinned.verify()
    with pytest.raises(LabError, match="cannot acquire"):
        pinned.read(reference.capture_id)


@pytest.mark.parametrize("change", ["remove", "tamper", "policy", "location", "expires", "hardlink"])
def test_pinned_originals_and_actual_retention_bounds_remain_required(tmp_path, change):
    sink = store(tmp_path)
    reference = capture(sink)
    pinned = PinnedEvidenceReader(sink)
    pinned.read(reference.capture_id)
    pinned.seal()
    path = sink.directory / f"tq005-{reference.capture_id}.json"
    if change == "remove":
        path.unlink()
    elif change == "tamper":
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'"Cents":-12', b'"Cents":-13'))
    elif change == "policy":
        sink.config.max_records += 1
    elif change == "location":
        sink.directory = tmp_path / "different"
    elif change == "expires":
        sink._now = lambda: sink.config.expires_at
    else:
        (tmp_path / "extra-link.json").hardlink_to(path)
    with pytest.raises(LabError):
        pinned.verify()


def test_pinned_reader_cannot_hide_a_change_between_two_original_validation_reads(tmp_path):
    sink = store(tmp_path)
    reference = capture(sink)
    pinned = PinnedEvidenceReader(sink)
    pinned.read(reference.capture_id)
    path = sink.directory / f"tq005-{reference.capture_id}.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(LabError, match="changed during"):
        pinned.read(reference.capture_id)


class SyntheticProof(Contract):
    scope: Scope
    reviewed_at: datetime
    expires_at: datetime
    original_document: str = "SYNTHETIC_ORIGINAL"


def gateway_case(tmp_path):
    snapshot, binding, inputs, _, catalog = reader_case(tmp_path)
    sink = store(tmp_path / "private", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    sink._now = lambda: datetime.now(timezone.utc)
    reference = capture(sink)
    now = datetime.now(timezone.utc)
    gateway = object.__new__(ConfiguredLiveGateway)
    gateway.source_proof = SyntheticProof(
        scope=binding.scope, reviewed_at=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=5),
    )
    gateway.config = Budgets()
    gateway.catalog, gateway.snapshot, gateway.enrollment = catalog, snapshot, inputs["enrollment"]
    profile = {"synthetic": "original-profile"}
    gateway.profiles = SimpleNamespace(model_dump=lambda **kwargs: dict(profile))
    records = [population("subscriptions", catalog.models["subscriptions"]["tables"])]
    calls = []

    def validate(scope, originals):
        assert scope == binding.scope
        assert originals.read(reference.capture_id)["response"]["structuredContent"]["rows"] == [{"Cents": -12}]
        calls.append("full_original_validation")
        return records
    gateway.population_records = validate
    return gateway, binding.scope, sink, reference, records, calls, profile


def test_fixed_gateway_population_semantics_validate_once_while_files_and_bindings_are_rechecked(tmp_path):
    gateway, scope, sink, _, records, calls, _ = gateway_case(tmp_path)
    check = gateway.population_guard(scope, sink)
    assert calls == ["full_original_validation"]
    for _ in range(5):
        assert check() == records
    assert calls == ["full_original_validation"]
    returned = check()
    returned[0].seed += 1
    assert check() == records
    check2 = gateway.population_guard(scope, sink)
    assert check2 is check and len(calls) == 1 and check2() == records


@pytest.mark.parametrize("change", ["epoch", "policy", "directory", "bytes"])
def test_gateway_reuse_does_not_rebind_original_proof_or_ignore_retention_and_byte_drift(tmp_path, change):
    gateway, scope, sink, reference, _, calls, _ = gateway_case(tmp_path)
    gateway.population_guard(scope, sink)()
    if change == "epoch":
        scope = scope.model_copy(update={"authorization_revision": "different-current-reader"})
    elif change == "policy":
        sink.config.max_records += 1
    elif change == "directory":
        sink.directory = tmp_path / "other-source-evidence"
    else:
        path = sink.directory / f"tq005-{reference.capture_id}.json"
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(LabError):
        gateway.population_guard(scope, sink)
    assert calls == ["full_original_validation"]


@pytest.mark.parametrize("change", [
    "proof", "scope", "catalog", "snapshot", "enrollment", "profiles", "budget", "record", "bytes",
])
def test_population_reuse_does_not_ignore_mutated_proof_or_binding_inputs(tmp_path, change):
    gateway, scope, sink, reference, records, _, profiles = gateway_case(tmp_path)
    check = gateway.population_guard(scope, sink)
    if change == "proof":
        gateway.source_proof.original_document = "SYNTHETIC_TAMPERED"
    elif change == "scope":
        scope.authorization_revision = "SYNTHETIC_OTHER_EPOCH"
    elif change == "catalog":
        gateway.catalog.raw["version"] = "SYNTHETIC_OTHER_VERSION"
    elif change == "snapshot":
        gateway.snapshot.metrics[0]["description"] = "SYNTHETIC_CHANGED"
    elif change == "enrollment":
        gateway.enrollment.review_reference = "SYNTHETIC_CHANGED"
    elif change == "profiles":
        profiles["synthetic"] = "SYNTHETIC_CHANGED"
    elif change == "budget":
        gateway.config.max_queries += 1
    elif change == "record":
        records[0].seed += 1
    else:
        path = sink.directory / f"tq005-{reference.capture_id}.json"
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(LabError) as error:
        check()
    assert error.value.status == Status.BLOCKED_CAPABILITY


def test_population_actual_review_expiry_still_stops_identical_bytes(tmp_path, monkeypatch):
    gateway, scope, sink, _, _, _, _ = gateway_case(tmp_path)
    check = gateway.population_guard(scope, sink)
    expiry = gateway.source_proof.expires_at
    class Clock:
        @staticmethod
        def now(tz):
            return expiry
    monkeypatch.setattr("telco_iq.live_demo.datetime", Clock)
    with pytest.raises(LabError, match="expired"):
        check()
