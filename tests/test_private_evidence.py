"""Synthetic private-store tests. No service, credentials or live provenance."""

import base64
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from telco_iq.catalog import digest
from telco_iq.contracts import Scope
from telco_iq.errors import LabError
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink

ROOT = Path(__file__).resolve().parents[1]
TARGET = "00000000-0000-0000-0000-000000000123"
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def configuration(directory, **changes):
    return PrivateEvidenceConfig(
        directory=directory, approval_reference="synthetic-store-review",
        expires_at=NOW + timedelta(days=1),
        max_capture_bytes=64_000, max_total_bytes=256_000, max_records=4,
    ).model_copy(update=changes)


def capture(sink, response=None, **changes):
    arguments = {
        "artifactId": TARGET, "daxQueries": ["EVALUATE ROW(\"Credit Exposure\", -12)"], "maxRows": 3,
    }
    values = {
        "tool": "ExecuteQuery", "source_alias": "finance", "request": arguments,
        "response": {"content": [], "structuredContent": {"rows": [{"Cents": -12}]}},
        "context": {"source": {"dataset_id": TARGET}, "filters": [{"month": "2026-07-01"}]},
        "private_identifiers": (TARGET,), "synthetic": True,
    }
    if response is not None:
        values["response"] = response
    values.update(changes)
    return sink.capture(**values)


def store(tmp_path, **changes):
    return PrivateEvidenceSink(configuration(tmp_path / "captures", **changes), ROOT, now=lambda: NOW)


@pytest.mark.parametrize("response", [
    {"content": [], "structuredContent": {"rows": [{"Cents": -12}], "complete": True}},
    {"content": [], "structuredContent": {"rows": [], "complete": False, "total": 99}},
    {"missingContent": {"unknown": [None, True, 9007199254740993, "malformed"]}},
    {"content": [{"type": "text", "text": "Refused synthetic query"}], "isError": True},
])
def test_raw_structure_hashes_and_deterministic_public_derivatives(tmp_path, response):
    sink = store(tmp_path)
    reference = capture(sink, response)
    retained = sink.read(reference.capture_id)
    assert retained["response"] == response
    assert reference.response_hash == digest(response)
    assert reference.request_hash == digest(retained["request"])
    assert retained["context"]["filters"] == [{"month": "2026-07-01"}]
    public = sink.derivative(reference.capture_id)
    assert TARGET not in json.dumps(public)
    assert public["raw_capture_hash"] == reference.capture_id
    assert public["response_hash"] == reference.response_hash
    assert public["request_hash"] == reference.request_hash
    assert public["evidence"]["response"] == response
    assert capture(sink, response) == reference
    assert len(list(sink.directory.glob("tq005-*.json"))) == 1


def test_response_absent_metadata_kind_is_not_a_query_or_metadata_tool_result(tmp_path):
    sink = store(tmp_path)
    response = {"response_obtained": False, "transport_error_type": "ReadError"}
    for tool in ("ExecuteQuery", "GetSemanticModelSchema"):
        with pytest.raises(LabError):
            capture(sink, response, tool=tool, response_kind="HTTP_METADATA_TRANSPORT_FAILURE")
    reference = capture(sink, response, tool="SourceMetadata", request={"method": "GET"},
                        response_kind="HTTP_METADATA_TRANSPORT_FAILURE")
    retained = sink.read(reference.capture_id)
    assert retained["tool"] == "SourceMetadata"
    assert retained["response_kind"] == "HTTP_METADATA_TRANSPORT_FAILURE"
    assert retained["response"] == response


def test_public_redaction_only_targets_identifiers_and_urls_preserving_keys_and_arithmetic(tmp_path):
    sink = store(tmp_path)
    response = {
        "content": [], "structuredContent": {
            "Metric": -123, "token_count": 8, "account_key": 7,
            "source": f"https://example.invalid/models/{TARGET}", "note": f"model {TARGET} credits",
        },
    }
    reference = capture(sink, response)
    public = sink.derivative(reference.capture_id)
    result = public["evidence"]["response"]["structuredContent"]
    assert result["Metric"] == -123 and result["account_key"] == 7 and result["token_count"] == 8
    assert set(result) == set(response["structuredContent"])
    assert "https://" not in json.dumps(public) and TARGET not in json.dumps(public)
    assert result["note"].endswith(" credits")
    assert sink.derivative(reference.capture_id) == public
    assert sink.read(reference.capture_id)["response"] == response


@pytest.mark.parametrize("uri", ["powerbi://synthetic.invalid/models/item", "file:///synthetic/private/source.csv"])
def test_non_http_resource_urls_are_private_but_raw_uri_remains_exact(tmp_path, uri):
    sink = store(tmp_path)
    reference = capture(sink, {"content": [], "resource": {"uri": uri}})
    raw = sink.read(reference.capture_id)
    public = sink.derivative(reference.capture_id)
    assert raw["response"]["resource"]["uri"] == uri
    assert uri not in json.dumps(public)
    assert public["evidence"]["response"]["resource"]["uri"].startswith("[url:")
    assert raw["retention_policy_hash"] == digest(sink.config.model_dump(mode="json"))


@pytest.mark.parametrize("location", [ROOT / ".local" / "private-captures", Path("relative-captures")])
def test_private_locations_rejected_without_creating_files(location):
    with pytest.raises(LabError, match="absolute|outside repository"):
        PrivateEvidenceSink(configuration(location), ROOT, now=lambda: NOW)


def test_onedrive_and_configured_sync_roots_rejected(tmp_path, monkeypatch):
    with pytest.raises(LabError, match="OneDrive"):
        PrivateEvidenceSink(configuration(tmp_path / "OneDrive - example" / "captures"), ROOT, now=lambda: NOW)
    monkeypatch.setenv("OneDriveCommercial", str(tmp_path / "custom-sync-root"))
    with pytest.raises(LabError, match="OneDrive"):
        PrivateEvidenceSink(configuration(tmp_path / "custom-sync-root" / "captures"), ROOT, now=lambda: NOW)


@pytest.mark.parametrize("secret", [
    {"Authorization": "synthetic-secret"}, {"access_token": "synthetic-secret"},
    {"AccountKey": "synthetic-storage-credential"},
    {"clientCredential": {"opaque": "synthetic-secret"}}, {"headers": {"X-Trace": "example"}},
    {"note": "Bearer synthetic-secret"}, {"note": "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.c2ln"},
    {"note": "https://example.invalid/?sig=synthetic-secret"},
    {"note": "-----BEGIN PRIVATE KEY-----synthetic-secret"},
    {"note": "registered-opaque-credential"},
])
def test_unsafe_credentials_refused_not_redacted_into_fake_raw_evidence(tmp_path, secret):
    sink = PrivateEvidenceSink(
        configuration(tmp_path / "captures"), ROOT, now=lambda: NOW,
        forbidden_values=("registered-opaque-credential",),
    )
    with pytest.raises(LabError, match="Unsafe"):
        capture(sink, secret)
    assert not list(sink.directory.glob("tq005-*.json"))
    assert "synthetic-secret" not in repr(sink)
    assert "registered-opaque-credential" not in repr(sink)


@pytest.mark.parametrize("text", [
    "// Bearer synthetic-encoded-credential",
    "// https://synthetic-user:synthetic-pass@example.invalid/source",
    "// https://example.invalid/source?sig=synthetic-signature",
    "// -----BEGIN PRIVATE KEY-----synthetic-encoded-key",
    "// registered-opaque-credential",
    '{"Ocp-Apim-Subscription-Key":"synthetic-encoded-value"}',
    '{"access_token":"synthetic-incomplete-value"',
    '{"password":"synthetic-incomplete-value"',
    '[{"x-api-key":"synthetic-incomplete-value"',
])
def test_inline_base64_definition_parts_cannot_hide_credentials_in_http_metadata(tmp_path, text):
    sink = PrivateEvidenceSink(
        configuration(tmp_path / "captures"), ROOT, now=lambda: NOW,
        forbidden_values=("registered-opaque-credential",),
    )
    body = {"definition": {"format": "TMDL", "parts": [{
        "path": "definition/expressions.tmdl", "payloadType": "InlineBase64",
        "payload": base64.b64encode(text.encode()).decode(),
    }]}}
    with pytest.raises(LabError, match="Unsafe"):
        sink.capture_http_metadata(
            source_alias="subscriptions", request={"role": "definition-before"}, status=200,
            header_fields={"Content-Type": "application/json"}, body=json.dumps(body).encode(),
            observed_at=NOW, actor_scope=Scope(
                tenant="synthetic-tenant", principal="synthetic-reader", authorization_revision="epoch-A",
                models=["subscriptions"], mode="LIVE_DELEGATED",
            ),
            context={}, private_identifiers=(TARGET,), synthetic=True, resource="source_metadata",
        )
    assert not list(sink.directory.glob("tq005-*.json"))


def test_actual_encoded_tmdl_with_large_safe_comment_has_bounded_capture_and_replay_time(tmp_path):
    program = """
import base64, json, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from telco_iq.contracts import Scope
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink
fixture = json.loads(Path(sys.argv[1]).read_bytes())
body = fixture["sources"]["subscriptions"]["definition"]
part = next(item for item in body["definition"]["parts"] if item["path"] == "definition/expressions.tmdl")
original = base64.b64decode(part["payload"])
part["payload"] = base64.b64encode(original + b"\\n// " + b"x" * 1_048_576).decode()
raw = json.dumps(body).encode()
now = datetime.now(timezone.utc)
root = Path(sys.argv[2])
sink = PrivateEvidenceSink(PrivateEvidenceConfig(
    directory=root / "captures", approval_reference="synthetic performance check",
    expires_at=now + timedelta(minutes=5), max_capture_bytes=8_000_000,
    max_total_bytes=32_000_000, max_records=4,
), root / "synthetic-repository")
scope = Scope(tenant="synthetic", principal="synthetic", authorization_revision="epoch-A",
              models=["subscriptions"], mode="LIVE_DELEGATED")
start = time.perf_counter()
reference = sink.capture_http_metadata(
    source_alias="subscriptions", request={"role":"definition-before"}, status=200,
    header_fields={"Content-Type":"application/json"}, body=raw, observed_at=now,
    actor_scope=scope, context={}, private_identifiers=(), synthetic=True, resource="source_metadata",
)
retained = sink.read(reference.capture_id)
assert base64.b64decode(retained["response"]["body_base64"]) == raw
assert sink.derivative(reference.capture_id)["evidence"]["response"]["body_base64"].startswith("[PRIVATE_HTTP_BODY:")
print(json.dumps({"elapsed_seconds": time.perf_counter() - start, "body_bytes": len(raw),
                  "part_path": part["path"]}))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(ROOT / "tests" / "fixtures" / "source_metadata_actual_2026_10_15.json"),
         str(tmp_path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    measured = json.loads(result.stdout)
    print(json.dumps(measured))
    assert measured["part_path"] == "definition/expressions.tmdl"
    assert measured["body_bytes"] >= 1_048_576
    assert measured["elapsed_seconds"] < 5, measured


@pytest.mark.parametrize("alias", ["x-api-key", "Ocp-Apim-Subscription-Key", "X_API_KEY", "ocp_apim_subscription_key"])
def test_standard_auth_header_aliases_rejected_recursively_before_raw_publication(tmp_path, alias):
    sink = store(tmp_path)
    unsafe = {"content": [], "structuredContent": {"metadata": [{"nested": {alias: "opaque-fixture-value"}}]}}
    with pytest.raises(LabError, match="Unsafe"):
        capture(sink, unsafe)
    assert not list(sink.directory.glob("tq005-*.json"))


@pytest.mark.parametrize("alias", ["x-api-key", "Ocp-Apim-Subscription-Key"])
def test_public_derivation_refuses_auth_aliases_even_in_internally_consistent_old_records(tmp_path, alias):
    sink = store(tmp_path)
    reference = capture(sink)
    envelope = sink.read(reference.capture_id)
    public = sink.derivative(reference.capture_id)
    unsafe = {"content": [], "structuredContent": {"metadata": [{alias: "opaque-fixture-value"}]}}
    envelope["response"] = unsafe
    capture_id = digest(envelope)
    public["raw_capture_hash"] = capture_id
    public["response_hash"] = digest(unsafe)
    public["evidence"]["response"] = unsafe
    path = sink.directory / f"tq005-{capture_id}.json"
    path.write_text(json.dumps({"envelope": envelope, "derivative": public}), encoding="utf-8")
    with pytest.raises(LabError, match="Unsafe"):
        sink.derivative(capture_id)


@pytest.mark.parametrize("unsafe", [
    {"rows": [object()]}, {"rows": [float("nan")]}, {1: "key"}, {"rows": [(1, 2)]},
])
def test_non_json_values_never_stringified_or_lossily_captured(tmp_path, unsafe):
    sink = store(tmp_path)
    with pytest.raises(LabError, match="JSON"):
        capture(sink, unsafe)
    assert not list(sink.directory.glob("tq005-*.json"))


def test_size_retention_expiry_and_unrelated_files_fail_closed(tmp_path):
    sink = store(tmp_path, max_capture_bytes=100)
    with pytest.raises(LabError, match="size"):
        capture(sink)
    sink = store(tmp_path, max_records=1)
    first = capture(sink)
    unrelated = sink.directory / "keep.txt"
    unrelated.write_text("unrelated")
    with pytest.raises(LabError, match="retention"):
        capture(sink, {"content": [], "new": True})
    assert sink.read(first.capture_id)
    assert unrelated.read_text() == "unrelated"
    expired = PrivateEvidenceSink(
        configuration(tmp_path / "captures"), ROOT, now=lambda: NOW + timedelta(days=2),
    )
    with pytest.raises(LabError, match="expired"):
        capture(expired)


def test_collision_corruption_and_atomic_write_failure_never_return_a_reference(tmp_path, monkeypatch):
    sink = store(tmp_path)
    reference = capture(sink)
    path = sink.directory / f"tq005-{reference.capture_id}.json"
    path.write_text("{}")
    with pytest.raises(LabError, match="collision|integrity"):
        capture(sink)
    with pytest.raises(LabError, match="integrity"):
        sink.read(reference.capture_id)
    other = store(tmp_path / "other")

    def fail_link(*args, **kwargs):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr("telco_iq.private_evidence.os.link", fail_link)
    with pytest.raises(LabError, match="write"):
        capture(other)
    assert not list(other.directory.glob("tq005-*.json"))
    assert not list(other.directory.glob("*.pending"))


def test_busy_store_and_total_byte_budget_fail_closed(tmp_path):
    sink = store(tmp_path)
    sink.directory.mkdir()
    lock = sink.directory / ".tq005.lock"
    lock.write_bytes(b"")
    with pytest.raises(LabError, match="busy"):
        capture(sink)
    assert lock.exists()
    lock.unlink()
    reference = capture(sink)
    size = (sink.directory / f"tq005-{reference.capture_id}.json").stat().st_size
    too_small = store(tmp_path, max_capture_bytes=size + 100, max_total_bytes=size * 2 - 1)
    with pytest.raises(LabError, match="retention"):
        capture(too_small, {"content": [], "structuredContent": {"rows": [{"Cents": -13}]}})
