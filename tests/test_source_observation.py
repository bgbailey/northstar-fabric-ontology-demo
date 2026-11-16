"""Sanitized AUTHOR shape data plus explicit synthetic reader/HTTP fixtures."""

import asyncio
import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from telco_iq.approval import Approval
from telco_iq.catalog import digest
from telco_iq.contracts import Budgets, Scope, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import ReaderBinding, SourceCallBudget, SourceIdentity
from telco_iq.mcp_configuration import state_probe
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink
from telco_iq.source_observation import (
    POWERBI,
    FrameAnchor,
    MetadataResponse,
    MetadataStateProfile,
    SourceMetadataCaptureConfig,
    SourceMetadataCaptureService,
    SourceMetadataObserver,
    TableTarget,
    code_hash,
    eligible_state,
    facts_from_captures,
    normalize_metadata,
)

FIXTURE = Path(__file__).parent / "fixtures" / "source_metadata_actual_2026_10_15.json"


def actual_shape():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == "a11a46ce1e1271d3ba98c0b6cdf0218fc88dd66b09a48dba6f96383e0cf359b1"
    return json.loads(FIXTURE.read_bytes())


def setup(tmp_path, *, alias="subscriptions", provenance="synthetic_test"):
    fixture = actual_shape()
    source = fixture["sources"][alias]
    identity = SourceIdentity(**source["identity"])
    scope = Scope(tenant="synthetic-tenant", principal="synthetic-reader", authorization_revision="epoch-A",
                  models=[alias], mode="LIVE_DELEGATED")
    binding = ReaderBinding(scope=scope, ontology_workspace_id=fixture["workspace_id"],
                            ontology_item_id="00000000-0000-0000-0000-000000000199",
                            native_snapshot_hash="a" * 64, sources={alias: identity})
    paths = {item["name"]: item for item in fixture["paths"]["paths"]}
    tables = []
    for part in source["definition"]["definition"]["parts"]:
        text = base64.b64decode(part["payload"]).decode()
        if "\t\t\tentityName: " not in text:
            continue
        entity = text.split("entityName: ", 1)[1].splitlines()[0]
        target_path = "dbo/" + entity
        incarnation = next(action["metaData"]["id"] for action in fixture["delta_zero_logs"][target_path]["actions"]
                           if "metaData" in action)
        metadata = next(action["metaData"] for action in fixture["delta_zero_logs"][target_path]["actions"]
                        if "metaData" in action)
        prefix = fixture["lakehouse_id"] + "/Tables/" + target_path
        tables.append(TableTarget(
            catalog_table=entity, part=part["path"], schema_name="dbo", entity_name=entity,
            incarnation_id=incarnation, service_birth=paths[prefix]["creationTime"],
            columns={field["name"]: field["type"] for field in json.loads(metadata["schemaString"])["fields"]},
            log_birth=paths[prefix + "/_delta_log/00000000000000000000.json"]["creationTime"],
        ))
    now = datetime.now(timezone.utc)
    profile = MetadataStateProfile(
        identity=identity, lakehouse_id=fixture["lakehouse_id"], tables=tables,
        part_paths=[part["path"] for part in source["definition"]["definition"]["parts"]],
        frame_request_id=source["framing_history"]["value"][0]["requestId"], code_hash=code_hash(),
        provenance=provenance, review_reference="Explicit synthetic reader fixture, not original author authority",
        reviewed_at=now - timedelta(seconds=1), expires_at=now + timedelta(hours=1),
    )
    evidence = PrivateEvidenceConfig(
        directory=tmp_path / "synthetic-private-captures", approval_reference="synthetic store approval",
        expires_at=now + timedelta(hours=1), max_capture_bytes=500_000, max_total_bytes=10_000_000, max_records=100,
    )
    sink = PrivateEvidenceSink(evidence, tmp_path / "synthetic-repository", forbidden_values=("synthetic-credential",))
    active, calls = {"yes": True}, []

    async def request(value):
        calls.append(value)
        headers = {}
        if value.role.startswith("definition-"):
            body = source["definition"]
        elif value.role.startswith("frame-"):
            body = source["framing_history"]
        elif value.role.startswith("paths-"):
            body = fixture["paths"]
        else:
            table = value.role.removeprefix("log0:")
            log = fixture["delta_zero_logs"][table]
            body = "\n".join(json.dumps(action) for action in log["actions"]).encode()
            headers = log["response_headers"]
            assert dict(value.header_fields)["If-Match"].strip('"') == headers["etag"].strip('"')
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return MetadataResponse(200, headers, raw, datetime.now(timezone.utc), scope, value.resource)

    budget = SourceCallBudget(Budgets(max_queries=50, max_rows=10_000, max_bytes=8_000_000, timeout_seconds=60))
    observer = SourceMetadataObserver(binding, {alias: profile}, {alias: profile.profile_hash}, request, sink, budget,
                                      lambda current: active["yes"] and current == scope, synthetic=True)
    return fixture, profile, binding, observer, active, calls


def original(value, documents):
    text = json.dumps(value, sort_keys=True)
    key = hashlib.sha256(text.encode()).hexdigest()
    documents[key] = text
    return key


def synthetic_frame_anchor(facts, profile):
    """Synthetic positive DAG, NOT correlation asserted for the actual fixture."""
    documents = {}
    frame_start = datetime.fromisoformat(facts.frame["startTime"].replace("Z", "+00:00"))
    model = original(facts.definition, documents)
    load = original({"status": "Completed", "startTimeUtc": "2026-09-29T17:03:11.963Z",
                     "endTimeUtc": "2026-09-29T17:05:00.506Z"}, documents)
    definition = original({
        "method": "POST", "status": 200, "bodySha256": digest({}),
        "path": f"/v1/workspaces/{profile.identity.workspace_id}/semanticModels/{profile.identity.dataset_id}/getDefinition",
        "startedAt": (frame_start - timedelta(seconds=2)).isoformat(), "response": facts.definition,
    }, documents)
    refresh_body = {"notifyOption": "NoNotification"}
    body = original(refresh_body, documents)
    path = f"/v1.0/myorg/groups/{profile.identity.workspace_id}/datasets/{profile.identity.dataset_id}/refreshes"
    accepted = original({
        "method": "POST", "status": 202, "path": path, "bodySha256": digest(refresh_body),
        "startedAt": (frame_start - timedelta(seconds=1)).isoformat(),
        "location": POWERBI + path + "/" + str(profile.frame_request_id),
    }, documents)
    profile.frame_anchor = FrameAnchor(model_body_hash=model, load_job_hash=load, definition_at_frame_hash=definition,
                                      full_refresh_body_hash=body, accepted_refresh_hash=accepted)
    return documents, model, load


async def test_actual_shape_capture_has_independent_derived_facts_and_replay_does_not_refresh(tmp_path):
    _, profile, binding, observer, _, calls = setup(tmp_path)
    facts = await observer.capture("subscriptions")
    assert not facts.blockers
    assert len(facts.tables) == 6 and facts.origin == "SYNTHETIC_TEST"
    assert observer.budget.requests == len(calls) == 12
    assert {call.resource for call in calls} == {"source_metadata", "iq", "storage"}
    assert all(dict(call.header_fields).get("x-ms-fabric-skill") == "semantic-model-authoring"
               for call in calls if call.resource == "source_metadata")
    assert all(not isinstance(value, type(eligible_state)) for value in facts.tables.values())
    replay = facts_from_captures(binding, "subscriptions", profile, observer.sink, facts.capture_ids, synthetic=True)
    assert replay.observed_at == facts.observed_at
    assert replay.data_fingerprint == facts.data_fingerprint
    assert replay.model_fingerprint == facts.model_fingerprint
    first = observer.sink.read(facts.capture_ids[0])
    assert first["response_kind"] == "HTTP_METADATA_RESULT"
    assert first["tool"] == "SourceMetadata" and "structuredContent" not in first["response"]
    derivative = observer.sink.derivative(facts.capture_ids[0])
    assert derivative["evidence"]["response"]["body_base64"].startswith("[PRIVATE_HTTP_BODY:")


@pytest.mark.parametrize("retry_log", [False, True])
async def test_initial_logs_overlap_without_losing_etags_original_chronology_or_retry_evidence(tmp_path, retry_log):
    _, profile, binding, observer, _, calls = setup(tmp_path)
    profile.max_read_attempts = 3
    observer.pins["subscriptions"] = profile.profile_hash
    original = observer.requester
    gate = asyncio.Event()
    active = maximum = started = 0
    failed = False
    indices = {target.table_path: index for index, target in enumerate(profile.tables)}

    async def request(value):
        nonlocal active, maximum, started, failed
        if not value.role.startswith("log0:"):
            assert active == 0
            return await original(value)
        active += 1
        started += 1
        maximum = max(maximum, active)
        if started == 4:
            gate.set()
        try:
            await asyncio.wait_for(gate.wait(), 5)
            index = indices[value.role.removeprefix("log0:")]
            if retry_log and index == 0 and not failed:
                failed = True
                calls.append(value)
                return MetadataResponse(
                    503, {"Retry-After": "0"}, b"{}", datetime.now(timezone.utc), binding.scope, value.resource,
                )
            await asyncio.sleep(.002 * (len(profile.tables) - index))
            return await original(value)
        finally:
            active -= 1

    observer.requester = request
    facts = await observer.capture("subscriptions")
    assert maximum == 4 and active == 0
    assert observer.budget.requests == len(calls) == 12 + int(retry_log)
    actual_logs = [role for role in facts.role_order if role.startswith("log0:")]
    expected_logs = ["log0:" + target.table_path for target in profile.tables]
    assert set(actual_logs) == set(expected_logs) and actual_logs != expected_logs
    replay = facts_from_captures(binding, "subscriptions", profile, observer.sink, facts.capture_ids, synthetic=True)
    assert replay.observed_at == facts.observed_at
    assert replay.data_fingerprint == facts.data_fingerprint and replay.role_order == facts.role_order
    if retry_log:
        originals = [observer.sink.read(key) for key in facts.capture_ids]
        retried = next(record for record in originals if record["context"].get("read_attempt") == 1)
        failed_id, = retried["context"]["failed_read_attempt_ids"]
        assert observer.sink.read(failed_id)["response"]["status"] == 503


async def test_concurrent_initial_log_failure_cancels_other_reads_before_after_boundaries(tmp_path):
    _, profile, binding, observer, _, calls = setup(tmp_path)
    original = observer.requester
    gate = asyncio.Event()
    started = cancelled = 0

    async def request(value):
        nonlocal started, cancelled
        if not value.role.startswith("log0:"):
            return await original(value)
        started += 1
        if started == 4:
            gate.set()
        await asyncio.wait_for(gate.wait(), 5)
        if value.role == "log0:" + profile.tables[0].table_path:
            calls.append(value)
            return MetadataResponse(412, {}, b"{}", datetime.now(timezone.utc), binding.scope, value.resource)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    observer.requester = request
    with pytest.raises(LabError, match="Metadata request failed"):
        await observer.capture("subscriptions")
    assert started == 4 and cancelled == 3
    assert not any(value.role.startswith("paths-after") or value.role == "definition-after" for value in calls)


@pytest.mark.parametrize("failures", [1, 2])
async def test_explicit_read_retry_retains_every_failed_original_and_counts_each_physical_request(tmp_path, failures):
    _, profile, binding, observer, _, calls = setup(tmp_path)
    profile.max_read_attempts = 3
    observer.pins["subscriptions"] = profile.profile_hash
    original = observer.requester
    attempts = 0

    async def request(value):
        nonlocal attempts
        if value.role == "frame-before":
            attempts += 1
            if attempts <= failures:
                calls.append(value)
                return MetadataResponse(
                    500, {"Retry-After": "0"}, b'{"Message":"An error has occurred."}',
                    datetime.now(timezone.utc), binding.scope, value.resource,
                )
        return await original(value)

    observer.requester = request
    facts = await observer.capture("subscriptions")
    assert not facts.blockers and len(facts.capture_ids) == 12
    assert observer.budget.requests == len(calls) == 12 + failures
    originals = [observer.sink.read(key) for key in facts.capture_ids]
    frame = next(record for record in originals if record["request"]["role"] == "frame-before")
    failed = frame["context"]["failed_read_attempt_ids"]
    assert len(failed) == failures and frame["context"]["read_attempt"] == failures
    assert all(observer.sink.read(key)["response"]["status"] == 500 for key in failed)
    assert not set(failed) & set(facts.capture_ids)
    replay = facts_from_captures(binding, "subscriptions", profile, observer.sink, facts.capture_ids, synthetic=True)
    assert replay.data_fingerprint == facts.data_fingerprint and replay.observed_at == facts.observed_at


@pytest.mark.parametrize("method,status,limit", [
    ("POST", 500, 3), ("GET", 401, 3), ("GET", 403, 3),
    ("GET", 404, 3), ("GET", 412, 3), ("GET", 500, 1), ("GET", 500, 3),
])
async def test_retry_never_repeats_post_auth_stale_or_unapproved_reads_and_stops_at_the_exact_bound(tmp_path, method, status, limit):
    _, profile, binding, observer, _, calls = setup(tmp_path)
    profile.max_read_attempts = limit
    observer.pins["subscriptions"] = profile.profile_hash
    original = observer.requester
    attempts = 0
    role = "definition-before" if method == "POST" else "frame-before"

    async def request(value):
        nonlocal attempts
        if value.role == role:
            attempts += 1
            calls.append(value)
            return MetadataResponse(status, {"Retry-After": "0"}, b"{}", datetime.now(timezone.utc),
                                    binding.scope, value.resource)
        return await original(value)

    observer.requester = request
    with pytest.raises(LabError):
        await observer.capture("subscriptions")
    assert attempts == (limit if method == "GET" and status == 500 else 1)
    assert len(list(observer.sink.directory.glob("tq005-*.json"))) == len(calls)


async def test_retry_after_cannot_overrun_the_actual_remaining_source_budget(tmp_path):
    _, profile, binding, observer, _, _ = setup(tmp_path)
    profile.max_read_attempts = 3
    observer.pins["subscriptions"] = profile.profile_hash
    original = observer.requester
    failed = 0

    async def request(value):
        nonlocal failed
        if value.role == "frame-before":
            failed += 1
            return MetadataResponse(503, {"Retry-After": "3600"}, b"{}", datetime.now(timezone.utc),
                                    binding.scope, value.resource)
        return await original(value)

    observer.requester = request
    with pytest.raises(LabError) as error:
        await observer.capture("subscriptions")
    assert error.value.status == Status.TIMED_OUT and failed == 1


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout])
async def test_response_absent_get_recovery_retains_transport_evidence_without_inventing_an_http_response(tmp_path, error_type):
    _, profile, binding, observer, _, calls = setup(tmp_path)
    profile.max_read_attempts = 3
    observer.pins["subscriptions"] = profile.profile_hash
    original = observer.requester
    first = True

    async def request(value):
        nonlocal first
        if value.role == "frame-before" and first:
            first = False
            calls.append(value)
            raise error_type("PRIVATE_TRANSPORT_DETAIL_NOT_FOR_CAPTURE")
        return await original(value)

    observer.requester = request
    facts = await observer.capture("subscriptions")
    success = next(observer.sink.read(key) for key in facts.capture_ids
                   if observer.sink.read(key)["request"]["role"] == "frame-before")
    failed = observer.sink.read(success["context"]["failed_read_attempt_ids"][0])
    assert failed["response_kind"] == "HTTP_METADATA_TRANSPORT_FAILURE"
    assert failed["response"]["response_obtained"] is False
    assert failed["response"]["transport_error_type"] == error_type.__name__
    assert "status" not in failed["response"] and "body_base64" not in failed["response"]
    assert "PRIVATE_TRANSPORT_DETAIL_NOT_FOR_CAPTURE" not in json.dumps(failed)
    assert len(calls) == observer.budget.requests == 13
    replay = facts_from_captures(binding, "subscriptions", profile, observer.sink, facts.capture_ids, synthetic=True)
    assert replay.data_fingerprint == facts.data_fingerprint


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_response_absent_requests_without_retry_authority_never_repeat(tmp_path, method):
    _, _, _, observer, _, calls = setup(tmp_path)
    original = observer.requester
    role = "frame-before" if method == "GET" else "definition-before"
    failed = 0

    async def request(value):
        nonlocal failed
        if value.role == role:
            failed += 1
            calls.append(value)
            raise httpx.ReadTimeout("PRIVATE_TRANSPORT_DETAIL")
        return await original(value)

    observer.requester = request
    with pytest.raises(LabError) as error:
        await observer.capture("subscriptions")
    assert error.value.status == Status.TIMED_OUT and failed == 1


@pytest.mark.parametrize("change", ["class", "status", "body_hash", "method", "actor", "resource"])
async def test_response_absent_replay_refuses_unreviewed_or_http_shaped_failure_evidence(tmp_path, change):
    _, profile, binding, observer, _, _ = setup(tmp_path)
    profile.max_read_attempts = 3
    observer.pins["subscriptions"] = profile.profile_hash
    original = observer.requester
    first = True

    async def request(value):
        nonlocal first
        if value.role == "frame-before" and first:
            first = False
            raise httpx.ReadError("PRIVATE_DETAIL")
        return await original(value)

    observer.requester = request
    facts = await observer.capture("subscriptions")
    success = next(observer.sink.read(key) for key in facts.capture_ids
                   if observer.sink.read(key)["request"]["role"] == "frame-before")
    failed_id = success["context"]["failed_read_attempt_ids"][0]

    class Changed:
        def read(self, key):
            value = deepcopy(observer.sink.read(key))
            if key == failed_id:
                if change == "class":
                    value["response"]["transport_error_type"] = "UnreviewedError"
                elif change == "status":
                    value["response"]["status"] = 500
                elif change == "body_hash":
                    value["response"]["body_sha256"] = digest({})
                elif change == "method":
                    value["request"]["method"] = "POST"
                elif change == "actor":
                    value["response"]["actor_scope"]["authorization_revision"] = "another-reader"
                else:
                    value["response"]["resource"] = "another-resource"
            return value

    with pytest.raises(LabError):
        facts_from_captures(binding, "subscriptions", profile, Changed(), facts.capture_ids, synthetic=True)


@pytest.mark.parametrize("change", ["request", "actor", "status", "attempt", "delay", "bytes"])
async def test_retained_failed_read_lineage_is_reopened_and_not_accepted_as_a_success_or_unrelated_request(tmp_path, change):
    _, profile, binding, observer, _, _ = setup(tmp_path)
    profile.max_read_attempts = 3
    observer.pins["subscriptions"] = profile.profile_hash
    original = observer.requester
    first = True

    async def request(value):
        nonlocal first
        if value.role == "frame-before" and first:
            first = False
            return MetadataResponse(500, {"Retry-After": "0"}, b'{"Message":"failed"}',
                                    datetime.now(timezone.utc), binding.scope, value.resource)
        return await original(value)

    observer.requester = request
    facts = await observer.capture("subscriptions")
    success = next(observer.sink.read(key) for key in facts.capture_ids
                   if observer.sink.read(key)["request"]["role"] == "frame-before")
    failed_id = success["context"]["failed_read_attempt_ids"][0]

    class Changed:
        def read(self, key):
            value = deepcopy(observer.sink.read(key))
            if key == failed_id:
                if change == "request":
                    value["request"]["url"] += "&unapproved=1"
                elif change == "actor":
                    value["response"]["actor_scope"]["authorization_revision"] = "other"
                elif change == "status":
                    value["response"]["status"] = 200
                elif change == "attempt":
                    value["context"]["read_attempt"] = 2
                elif change == "delay":
                    value["response"]["header_fields"]["retry-after"] = "3600"
                else:
                    value["response"]["body_base64"] = base64.b64encode(b"changed").decode()
            return value

    with pytest.raises(LabError):
        facts_from_captures(binding, "subscriptions", profile, Changed(), facts.capture_ids, synthetic=True)


async def test_facts_are_not_state_author_capture_and_missing_frame_proof_block(tmp_path):
    _, profile, _, observer, _, _ = setup(tmp_path)
    facts = await observer.capture("subscriptions")
    profile.calibration_capture_ids = facts.capture_ids
    with pytest.raises(LabError, match="full-model definition-at-frame"):
        eligible_state(facts, profile, {}, model_body_hash="a" * 64, load_job_hash="b" * 64, synthetic=True)
    facts.origin = "AUTHOR_SHAPE"
    with pytest.raises(LabError, match="Author/shape"):
        eligible_state(facts, profile, {}, model_body_hash="a" * 64, load_job_hash="b" * 64, synthetic=True)


async def test_only_reviewed_synthetic_full_frame_dag_can_convert_v0_facts(tmp_path):
    _, profile, _, observer, _, _ = setup(tmp_path)
    facts = await observer.capture("subscriptions")
    profile.calibration_capture_ids = facts.capture_ids
    documents, model, load = synthetic_frame_anchor(facts, profile)
    state = eligible_state(facts, profile, documents, model_body_hash=model, load_job_hash=load, synthetic=True)
    assert state.data_revision.startswith("DERIVED_DELTA_VECTOR:")
    assert state.model_revision.startswith("DERIVED_TMDL_FRAME:")
    assert state.observed_at == facts.observed_at
    assert state.data_revision != state.model_revision
    with pytest.raises(LabError):
        eligible_state(facts, profile, documents, model_body_hash=model, load_job_hash=load)


async def test_fresh_last_frame_cannot_freshen_stale_definitions_and_delta_logs(tmp_path):
    _, _, _, observer, _, _ = setup(tmp_path)
    request = observer.requester
    now = datetime.now(timezone.utc)

    async def stale_except_final_frame(value):
        response = await request(value)
        observed = now if value.role in {"frame-after", "definition-after"} else now - timedelta(seconds=301)
        return replace(response, observed_at=observed)

    observer.requester = stale_except_final_frame
    with pytest.raises(LabError, match="bounded observation window"):
        await observer.capture("subscriptions")


async def test_coherent_old_capture_keeps_original_clock_and_cannot_become_current_state(tmp_path):
    _, profile, binding, observer, _, _ = setup(tmp_path)
    request = observer.requester
    old = datetime.now(timezone.utc) - timedelta(seconds=301)

    async def old_response(value):
        return replace(await request(value), observed_at=old)

    observer.requester = old_response
    facts = await observer.capture("subscriptions")
    replay = facts_from_captures(binding, "subscriptions", profile, observer.sink, facts.capture_ids, synthetic=True)
    assert facts.observed_at == replay.observed_at == old
    profile.calibration_capture_ids = facts.capture_ids
    documents, model, load = synthetic_frame_anchor(facts, profile)
    with pytest.raises(LabError, match="stale"):
        eligible_state(replay, profile, documents, model_body_hash=model, load_job_hash=load, synthetic=True)


async def test_complete_slow_fresh_group_uses_oldest_required_response_on_capture_and_replay(tmp_path):
    _, profile, binding, observer, _, calls = setup(tmp_path)
    request = observer.requester
    start = datetime.now(timezone.utc) - timedelta(seconds=240)

    async def staggered_response(value):
        response = await request(value)
        return replace(response, observed_at=start + timedelta(seconds=15 * (len(calls) - 1)))

    observer.requester = staggered_response
    facts = await observer.capture("subscriptions")
    replay = facts_from_captures(binding, "subscriptions", profile, observer.sink, facts.capture_ids, synthetic=True)
    assert facts.observed_at == replay.observed_at == start
    assert facts.window_started_at == start
    assert facts.window_ended_at == start + timedelta(seconds=15 * (len(calls) - 1))
    assert facts.observation_id == replay.observation_id
    assert facts.role_order == [call.role for call in calls] == replay.role_order
    profile.calibration_capture_ids = facts.capture_ids
    documents, model, load = synthetic_frame_anchor(facts, profile)
    state = eligible_state(replay, profile, documents, model_body_hash=model, load_job_hash=load, synthetic=True)
    assert state.observed_at == start


@pytest.mark.parametrize("change", ["missing", "swapped", "duplicate", "mixed-group", "future-response"])
async def test_retained_observation_requires_all_ordered_roles_and_one_original_group(tmp_path, change):
    _, profile, binding, observer, _, _ = setup(tmp_path)
    facts = await observer.capture("subscriptions")
    keys = list(facts.capture_ids)
    if change == "missing":
        del keys[3]
    elif change == "swapped":
        keys[3], keys[4] = keys[4], keys[3]
    elif change == "duplicate":
        keys.insert(3, keys[3])
    elif change == "mixed-group":
        second = await observer.capture("subscriptions")
        keys[3] = second.capture_ids[3]
    else:
        record = observer.sink.read(keys[3])
        record["response"]["observed_at"] = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        keys[3] = observer.sink.capture(
            tool=record["tool"], source_alias=record["source_alias"], request=record["request"],
            response=record["response"], context=record["context"],
            private_identifiers=tuple(record["private_identifiers"]), synthetic=True,
            response_kind="HTTP_METADATA_RESULT",
        ).capture_id
    with pytest.raises(LabError):
        facts_from_captures(binding, "subscriptions", profile, observer.sink, keys, synthetic=True)


@pytest.mark.parametrize("group", [None, "", "not-an-observation-uuid"])
async def test_missing_or_malformed_original_group_is_an_explicit_capability_block(tmp_path, group):
    _, profile, binding, observer, _, _ = setup(tmp_path)
    facts = await observer.capture("subscriptions")
    keys = []
    for key in facts.capture_ids:
        record = observer.sink.read(key)
        record["context"]["observation_id"] = group
        keys.append(observer.sink.capture(
            tool=record["tool"], source_alias=record["source_alias"], request=record["request"],
            response=record["response"], context=record["context"],
            private_identifiers=tuple(record["private_identifiers"]), synthetic=True,
            response_kind="HTTP_METADATA_RESULT",
        ).capture_id)
    with pytest.raises(LabError):
        facts_from_captures(binding, "subscriptions", profile, observer.sink, keys, synthetic=True)


@pytest.mark.parametrize("failure", ["v1", "recreated", "incarnation", "missing", "checkpoint", "changed", "old", "selective", "frame"])
async def test_incomplete_or_unknown_metadata_never_becomes_an_observed_state(tmp_path, failure):
    fixture, profile, binding, observer, _, _ = setup(tmp_path)
    source = fixture["sources"]["subscriptions"]
    before = deepcopy(fixture["paths"]["paths"])
    after = deepcopy(before)
    prefix = str(profile.lakehouse_id) + "/Tables/dbo/account"
    if failure == "v1":
        entry = deepcopy(next(item for item in before if item["name"] == prefix + "/_delta_log/00000000000000000000.json"))
        entry["name"] = prefix + "/_delta_log/00000000000000000001.json"
        before.append(entry)
        after = deepcopy(before)
    elif failure == "recreated":
        next(item for item in before if item["name"] == prefix)["creationTime"] = "134351750068310133"
        after = deepcopy(before)
    elif failure == "missing":
        before = [item for item in before if item["name"] != prefix]
        after = deepcopy(before)
    elif failure == "checkpoint":
        before.append({"name": prefix + "/_delta_log/_last_checkpoint", "etag": "x"})
        after = deepcopy(before)
    elif failure == "changed":
        after[0]["etag"] = "changed"
    if failure == "missing":
        with pytest.raises(LabError):
            normalize_metadata(binding, "subscriptions", profile, definition=source["definition"],
                               framing=source["framing_history"], paths_before=before, paths_after=after,
                               zero_logs={}, observed_at=datetime.now(timezone.utc), capture_ids=[], origin="SYNTHETIC_TEST")
        return
    zero = {key: ("\n".join(json.dumps(action) for action in log["actions"]).encode(), log["response_headers"])
            for key, log in fixture["delta_zero_logs"].items()}
    if failure == "incarnation":
        actions = deepcopy(fixture["delta_zero_logs"]["dbo/account"]["actions"])
        next(action["metaData"] for action in actions if "metaData" in action)["id"] = "00000000-0000-0000-0000-000000000999"
        zero["dbo/account"] = "\n".join(json.dumps(action) for action in actions).encode(), zero["dbo/account"][1]
    facts = normalize_metadata(binding, "subscriptions", profile, definition=source["definition"],
                               framing=source["framing_history"], paths_before=before, paths_after=after,
                               zero_logs=zero, observed_at=datetime.now(timezone.utc), capture_ids=["c" * 64],
                               origin="SYNTHETIC_TEST")
    profile.calibration_capture_ids = ["c" * 64]
    documents, model, load = synthetic_frame_anchor(facts, profile)
    if failure == "old":
        facts.observed_at -= timedelta(minutes=6)
    elif failure == "selective":
        body = {"type": "full", "objects": [{"table": "Accounts"}]}
        body_hash = original(body, documents)
        profile.frame_anchor.full_refresh_body_hash = body_hash
    elif failure == "frame":
        accepted = json.loads(documents[profile.frame_anchor.accepted_refresh_hash])
        accepted["location"] = None  # Actual old accepted request IDs are unknown, not correlated by time.
        profile.frame_anchor.accepted_refresh_hash = original(accepted, documents)
    with pytest.raises(LabError):
        eligible_state(facts, profile, documents, model_body_hash=model, load_job_hash=load, synthetic=True)


@pytest.mark.parametrize("failure", ["logout", "resource", "actor", "budget", "retention"])
async def test_session_resource_and_all_call_budgets_fail_closed_with_original_retention(tmp_path, failure):
    _, _, binding, observer, active, calls = setup(tmp_path)
    original_request = observer.requester
    async def request(value):
        result = await original_request(value)
        if failure == "logout":
            active["yes"] = False
        elif failure == "resource":
            result = replace(result, resource="ontology")
        elif failure == "actor":
            result = replace(result, actor_scope=binding.scope.model_copy(update={"authorization_revision": "epoch-B"}))
        return result
    observer.requester = request
    if failure == "budget":
        observer.budget = SourceCallBudget(Budgets(max_queries=1))
    elif failure == "retention":
        observer.sink.config.max_capture_bytes = 100
    with pytest.raises(LabError):
        await observer.capture("subscriptions")
    assert len(calls) == 1
    if failure != "retention":
        assert len(list(observer.sink.directory.glob("tq005-*.json"))) == 1


async def test_regional_lro_never_receives_bearer_and_only_succeeded_grants_result(tmp_path):
    fixture, profile, binding, observer, _, calls = setup(tmp_path)
    profile.regional_location_origin = "https://wabi-us-central-b-primary-redirect.analysis.windows.net"
    observer.pins["subscriptions"] = profile.profile_hash
    operation = "00000000-0000-0000-0000-000000000188"
    real = observer.requester
    async def request(value):
        if value.role == "definition-before":
            calls.append(value)
            return MetadataResponse(202, {"x-ms-operation-id": operation,
                                         "Location": profile.regional_location_origin + "/v1/operations/" + operation},
                                    b"", datetime.now(timezone.utc), binding.scope, value.resource)
        if "-state-" in value.role:
            calls.append(value)
            return MetadataResponse(200, {"Location": profile.regional_location_origin + "/v1/operations/" + operation + "/result"},
                                    b'{"status":"Succeeded"}', datetime.now(timezone.utc), binding.scope, value.resource)
        if value.role.endswith("-result"):
            calls.append(value)
            return MetadataResponse(200, {}, json.dumps(fixture["sources"]["subscriptions"]["definition"]).encode(),
                                    datetime.now(timezone.utc), binding.scope, value.resource)
        return await real(value)
    observer.requester = request
    facts = await observer.capture("subscriptions")
    assert not facts.blockers
    assert all("wabi-" not in call.url for call in calls)
    assert all(dict(call.header_fields).get("x-ms-fabric-skill") == "semantic-model-authoring"
               for call in calls if call.resource == "source_metadata")
    assert sum(call.role == "definition-before" for call in calls) == 1


async def test_metadata_state_dispatch_does_not_execute_a_dax_query(tmp_path):
    _, profile, binding, observer, _, _ = setup(tmp_path)
    calls = []
    async def metadata(current, alias):
        calls.append((current, alias))
        return "explicit-metadata-result"
    client = SimpleNamespace(reader_scope=binding.scope)
    probe = state_probe(client, {}, session_active=lambda _: True, metadata_profiles={"subscriptions": profile},
                        metadata_probe=metadata)
    assert await probe(binding, "subscriptions") == "explicit-metadata-result"
    assert calls == [(binding, "subscriptions")]


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_fixed_capture_service_bootstraps_without_population_proof_model_provider_or_readiness(tmp_path, mode):
    _, profile, binding, fixture_observer, active, calls = setup(tmp_path, provenance="author_shape_capture")
    now = datetime.now(timezone.utc)
    approval = Approval(
        target_alias="APPROVED_LAB_ONLY", target_digest="pending", artifact_digest="pending",
        operations=["source-metadata-capture"], estimated_cost=0, cap=0, currency="USD",
        expires_at=now + timedelta(hours=1), ttl_hours=1, teardown_owner="synthetic owner",
        teardown_mechanism="No resources created", confirmation_receipt="Synthetic scoped capture approval",
    )
    config = SourceMetadataCaptureConfig(
        scope=binding.scope, ontology_workspace_id=binding.ontology_workspace_id,
        ontology_item_id=binding.ontology_item_id, native_snapshot_hash=binding.native_snapshot_hash,
        profiles={"subscriptions": profile}, profile_pins={"subscriptions": profile.profile_hash},
        budgets=Budgets(max_queries=30, max_rows=10_000, timeout_seconds=60),
        private_evidence=fixture_observer.sink.config, approval=approval,
        effects="SOURCE_DEFINITION_FRAMING_ONELAKE_METADATA_ONLY",
        source_execution_mode=mode,
    )
    config.approval.target_digest, config.approval.artifact_digest = config.target_digest, config.artifact_digest
    path = tmp_path / "fixed-capture-config.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    session = SimpleNamespace(scope=lambda: binding.scope)
    reader = SimpleNamespace(active=lambda current: current is session and active["yes"])
    service = SourceMetadataCaptureService(reader, path, tmp_path / "synthetic-repository",
                                           requester_factory=lambda *_: fixture_observer.requester)
    assert not calls
    result = await service.capture(session)
    assert result["kind"] == "SOURCE_METADATA_CAPTURE_ONLY" and result["ready"] is False
    assert len(result["capture_hashes"]) == 12
    assert "definition" not in result["facts"][0] and "answer" not in result
    assert str(profile.identity.dataset_id) not in json.dumps(result)
    # Exact epoch and immutable approval are required; no reload or token fallback.
    other = SimpleNamespace(scope=lambda: binding.scope.model_copy(update={"authorization_revision": "epoch-B"}))
    with pytest.raises(LabError):
        await service.capture(other)


def two_source_capture_service(tmp_path):
    _, subscription, _, subscription_observer, _, _ = setup(tmp_path, provenance="author_shape_capture")
    _, finance, binding, finance_observer, _, _ = setup(tmp_path, alias="finance", provenance="author_shape_capture")
    scope = binding.scope.model_copy(update={"models": ["finance", "subscriptions"]})
    now = datetime.now(timezone.utc)
    config = SourceMetadataCaptureConfig(
        scope=scope, ontology_workspace_id=binding.ontology_workspace_id,
        ontology_item_id=binding.ontology_item_id, native_snapshot_hash=binding.native_snapshot_hash,
        profiles={"subscriptions": subscription, "finance": finance},
        profile_pins={"subscriptions": subscription.profile_hash, "finance": finance.profile_hash},
        source_execution_mode="parallel", budgets=Budgets(max_queries=50, max_rows=20_000, timeout_seconds=60),
        private_evidence=subscription_observer.sink.config,
        approval=Approval(
            target_alias="APPROVED_LAB_ONLY", target_digest="pending", artifact_digest="pending",
            operations=["source-metadata-capture"], estimated_cost=0, cap=0, currency="USD",
            expires_at=now + timedelta(hours=1), ttl_hours=1, teardown_owner="synthetic owner",
            teardown_mechanism="No resources created", confirmation_receipt="Synthetic scoped capture approval",
        ),
        effects="SOURCE_DEFINITION_FRAMING_ONELAKE_METADATA_ONLY",
    )
    config.approval.target_digest, config.approval.artifact_digest = config.target_digest, config.artifact_digest
    session = SimpleNamespace(scope=lambda: scope)
    reader = SimpleNamespace(active=lambda current: current is session)
    path = tmp_path / "two-source-capture-config.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    service = SourceMetadataCaptureService(reader, path, tmp_path / "synthetic-repository", requester_factory=lambda *_: None)
    return config, service, session, subscription_observer, finance_observer


async def test_explicit_parallel_source_capture_overlaps_sources_and_retains_deterministic_separate_originals(tmp_path):
    config, service, session, subscriptions, finance = two_source_capture_service(tmp_path)
    started, together = set(), asyncio.Event()

    async def request(value):
        subscription = str(config.profiles["subscriptions"].identity.dataset_id) in value.url
        if value.role == "definition-before":
            started.add("subscriptions" if subscription else "finance")
            if len(started) == 2:
                together.set()
            await asyncio.wait_for(together.wait(), timeout=1)
        result = await (subscriptions if subscription else finance).requester(value)
        return replace(result, actor_scope=config.scope)

    service.requester_factory = lambda *_: request
    result = await service.capture(session)
    assert started == {"finance", "subscriptions"}
    assert [fact["source_alias"] for fact in result["facts"]] == ["finance", "subscriptions"]
    sink = PrivateEvidenceSink(config.private_evidence, tmp_path / "synthetic-repository")
    binding = ReaderBinding(
        scope=config.scope, ontology_workspace_id=config.ontology_workspace_id,
        ontology_item_id=config.ontology_item_id, native_snapshot_hash=config.native_snapshot_hash,
        sources={alias: profile.identity for alias, profile in config.profiles.items()},
    )
    for alias, profile in config.profiles.items():
        ids = [key for key in result["capture_hashes"] if sink.read(key)["source_alias"] == alias]
        facts = facts_from_captures(binding, alias, profile, sink, ids)
        assert not facts.blockers and facts.origin == "READER_DELEGATED"
    assert result["ready"] is False and "answer" not in result
    sequential = config.model_copy(update={"source_execution_mode": "sequential"})
    assert "source_execution_mode" not in sequential.model_dump(mode="json")
    assert sequential.artifact_digest != config.artifact_digest


async def test_parallel_source_capture_cancels_siblings_and_preserves_auth_failure_without_returning_partial_success(tmp_path, monkeypatch):
    _, service, session, _, _ = two_source_capture_service(tmp_path)
    started, cancelled = asyncio.Event(), []

    async def capture(self, alias):
        if alias == "finance":
            await started.wait()
            raise LabError("Synthetic current-reader permission lost", Status.BLOCKED_AUTH)
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(alias)

    monkeypatch.setattr(SourceMetadataObserver, "capture", capture)
    with pytest.raises(LabError) as error:
        await service.capture(session)
    assert error.value.status == Status.BLOCKED_AUTH
    assert cancelled == ["subscriptions"]


async def test_reader_calibration_cannot_survive_epoch_rotation(tmp_path):
    _, profile, binding, observer, _, _ = setup(tmp_path)
    facts = await observer.capture("subscriptions")
    rotated = binding.model_copy(update={"scope": binding.scope.model_copy(update={"authorization_revision": "epoch-B"})})
    with pytest.raises(LabError, match="current-reader"):
        facts_from_captures(rotated, "subscriptions", profile, observer.sink, facts.capture_ids, synthetic=True)


async def test_metadata_wrong_model_binding_stops_before_storage_or_frame_query(tmp_path):
    _, _, _, observer, _, calls = setup(tmp_path)
    original_request = observer.requester
    async def request(value):
        result = await original_request(value)
        body = json.loads(result.body)
        part = next(item for item in body["definition"]["parts"] if item["path"] == "definition/expressions.tmdl")
        text = base64.b64decode(part["payload"]).decode().replace(
            "00000000-0000-0000-0000-000000000110", "00000000-0000-0000-0000-000000000999",
        )
        part["payload"] = base64.b64encode(text.encode()).decode()
        return replace(result, body=json.dumps(body).encode())
    observer.requester = request
    with pytest.raises(LabError, match="workspace/lakehouse"):
        await observer.capture("subscriptions")
    assert len(calls) == 1


async def test_complete_listing_pages_follow_only_actual_service_continuation(tmp_path):
    fixture, profile, binding, observer, _, calls = setup(tmp_path)
    real = observer.requester
    async def request(value):
        if value.role.startswith("paths-"):
            calls.append(value)
            cursor = parse_qs(urlsplit(value.url).query).get("continuation")
            page = {"paths": fixture["paths"]["paths"][:120] if cursor is None else fixture["paths"]["paths"][120:],
                    "ContinuationToken": "actual-synthetic-cursor" if cursor is None else None}
            return MetadataResponse(200, {}, json.dumps(page).encode(), datetime.now(timezone.utc),
                                    binding.scope, "storage")
        return await real(value)
    observer.requester = request
    facts = await observer.capture("subscriptions")
    assert len(calls) == 14
    assert facts_from_captures(binding, "subscriptions", profile, observer.sink,
                               facts.capture_ids, synthetic=True).data_fingerprint == facts.data_fingerprint


@pytest.mark.parametrize("failure", ["error", "partial", "wrong_uuid"])
async def test_no_lro_result_dispatch_after_ambiguous_failed_or_partial_state(tmp_path, failure):
    _, profile, binding, observer, _, calls = setup(tmp_path)
    profile.regional_location_origin = "https://wabi-us-central-b-primary-redirect.analysis.windows.net"
    observer.pins["subscriptions"] = profile.profile_hash
    operation = "00000000-0000-0000-0000-000000000188"
    async def request(value):
        calls.append(value)
        if value.method == "POST":
            location_id = operation if failure != "wrong_uuid" else "00000000-0000-0000-0000-000000000189"
            return MetadataResponse(202, {"x-ms-operation-id": operation,
                                         "Location": profile.regional_location_origin + "/v1/operations/" + location_id},
                                    b"", datetime.now(timezone.utc), binding.scope, "source_metadata")
        result = {"status": "Succeeded", "error": {} if failure == "error" else None}
        if failure == "partial":
            result["partial"] = True
        return MetadataResponse(200, {}, json.dumps(result).encode(), datetime.now(timezone.utc),
                                binding.scope, "source_metadata")
    observer.requester = request
    with pytest.raises(LabError):
        await observer.capture("subscriptions")
    assert not any(call.role.endswith("-result") for call in calls)
    assert len(calls) <= 2


def test_source_only_schema_whitelist_refuses_latent_fields(tmp_path):
    _, profile, _, _, _, _ = setup(tmp_path)
    value = profile.tables[0].model_dump()
    value["columns"] = {**value["columns"], "latent_outcome": "long"}
    with pytest.raises(ValueError):
        TableTarget.model_validate(value)


@pytest.mark.parametrize("key", ["X-API-Key", "Ocp-Apim-Subscription-Key"])
def test_http_metadata_extension_preserves_credential_alias_refusal(tmp_path, key):
    _, _, binding, observer, _, _ = setup(tmp_path)
    with pytest.raises(LabError):
        observer.sink.capture_http_metadata(
            source_alias="subscriptions", request={}, status=200, header_fields={key: "opaque-secret"}, body=b"{}",
            observed_at=datetime.now(timezone.utc), actor_scope=binding.scope, context={},
            private_identifiers=(), synthetic=True,
        )
    with pytest.raises(LabError):
        observer.sink.capture_http_metadata(
            source_alias="subscriptions", request={}, status=200, header_fields={}, body=b"{}",
            original_header_pairs=[(key, "opaque-secret")],
            observed_at=datetime.now(timezone.utc), actor_scope=binding.scope, context={},
            private_identifiers=(), synthetic=True,
        )
