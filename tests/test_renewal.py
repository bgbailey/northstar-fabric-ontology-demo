"""Synthetic local authorization tests. No Azure/Fabric/Graph network or credential reads."""

import json
import socket
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from infra import renewal
from infra.fabric_p0 import Author
from infra.renewal import Renewal, body_digest, read_json, sha256, stage_execution_window

TENANT = "00000000-0000-0000-0000-000000000001"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000002"
WORKSPACE = "00000000-0000-0000-0000-000000000003"
ORIGINAL = "00000000-0000-0000-0000-000000000004"
CREATED = "00000000-0000-0000-0000-000000000005"
LRO = "00000000-0000-0000-0000-000000000006"
OTHER = "00000000-0000-0000-0000-000000000007"
PREFIX = f"/v1/workspaces/{WORKSPACE}"
CAPACITY = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/synthetic/providers/Microsoft.Fabric/capacities/test"
AUTOMATION = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/synthetic"
    "/providers/Microsoft.Automation/automationAccounts/test"
)
CREATE = {"displayName": "Synthetic_Hero", "type": "Ontology"}
REGIONAL = "https://wabi-us-central-b-primary-redirect.analysis.windows.net"
PUBLIC_ID_POLICY = {"strategy": "PUBLIC_OPERATION_ID", "observedLocationOrigin": REGIONAL}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Renewal tests must never use network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def operation(name, method, path, body=None, **kwargs):
    return {"name": name, "method": method, "path": path, "bodySha256": body_digest(body), **kwargs}


def configure(private, *, change=None, started=None):
    now = datetime.now(timezone.utc)
    artifact = private / "synthetic-payload.json"
    write(artifact, CREATE)
    artifacts = [{"file": artifact.name, "sha256": sha256(artifact.read_bytes())}]
    approval = {
        "schemaVersion": "fabric-renewal-v1", "approved": True, "confirmationReference": "synthetic-test-only",
        "tenantId": TENANT, "subscriptionId": SUBSCRIPTION, "authorUser": "author@synthetic.invalid",
        "workspaceId": WORKSPACE, "ownedItems": {"ontology": ORIGINAL}, "protectedItemIds": [ORIGINAL],
        "artifacts": artifacts, "artifactSetSha256": body_digest(artifacts),
        "operations": [
            operation("list", "GET", PREFIX + "/items", allowPagination=True),
            operation("create", "POST", PREFIX + "/items", CREATE, allowLro=True,
                      createsAlias="hero", createdDisplayName=CREATE["displayName"]),
            operation("read-created", "GET", PREFIX + "/items/{hero}"),
            operation("delete-created", "DELETE", PREFIX + "/items/{hero}", allowLro=True),
            operation("read-original", "POST", PREFIX + f"/ontologies/{ORIGINAL}/getDefinition", {},
                      allowLro=True),
        ],
        "cost": {"resource": CAPACITY, "currency": "USD", "unitPrice": 1.0, "estimated": 2.0, "cap": 3.0,
                 "basis": "SYNTHETIC TEST NUMBERS ONLY, not a resource price claim"},
        "window": {"ttlSeconds": 3600, "minimumRemainingSeconds": 1, "shutdownRetrySeconds": 7200,
                   "latestStartUtc": (now + timedelta(hours=2)).isoformat(),
                   "notAfterUtc": (now + timedelta(hours=6)).isoformat()},
        "shutdown": {"approvalReference": "synthetic-shutdown-approval", "capacityResourceId": CAPACITY,
                     "scheduleResourceId": AUTOMATION + "/schedules/synthetic-deadline",
                     "runbookName": "SyntheticPause", "runbookSha256": "a" * 64,
                     "receiptFile": "renewal-shutdown-readback.json"},
        "teardown": {"owner": "synthetic-test-owner", "mechanism": "Delete exact newly owned IDs",
                     "approvalReference": "synthetic-cleanup-approval", "ownedOnly": True},
    }
    if change:
        change(approval)
    path = private / "renewal-approval.json"
    write(path, approval)
    write(private / "p0-workspace.json", {"id": WORKSPACE})
    write(private / "p0-ontology.json", {"id": ORIGINAL})
    write(private / "p0-approval.json", {
        "approved": True, "deadlineUtc": "2000-01-01T00:00:00Z",
        "tenantId": TENANT, "subscriptionId": SUBSCRIPTION, "authorUser": "expired@synthetic.invalid",
    })
    window = stage_execution_window(path, private, now=started or now)
    deadline = datetime.fromisoformat(window["deadlineUtc"])
    write(private / approval["shutdown"]["receiptFile"], {
        "observedAtUtc": (started or datetime.now(timezone.utc)).isoformat(),
        "tenantId": TENANT, "subscriptionId": SUBSCRIPTION, "runbookSha256": "a" * 64,
        "inspect": {"status": "Completed", "mutations": 0},
        "schedule": {
            "id": approval["shutdown"]["scheduleResourceId"],
            "properties": {"isEnabled": True, "frequency": "Hour", "interval": 1,
                           "startTime": window["deadlineUtc"],
                           "expiryTime": (deadline + timedelta(seconds=7200)).isoformat()},
        },
        "binding": {
            "id": AUTOMATION + "/jobSchedules/" + OTHER,
            "properties": {
                "schedule": {"name": "synthetic-deadline"}, "runbook": {"name": "SyntheticPause"},
                "parameters": {
                    "TargetResourceId": CAPACITY, "TargetSha256": sha256(CAPACITY.lower().encode()),
                    "TenantId": TENANT, "DeadlineUtc": window["deadlineUtc"], "Mode": "SuspendWhenDue",
                },
            },
        },
    })
    return path, approval, window


@pytest.fixture
def no_auth(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline test attempted authentication/client construction")
    monkeypatch.setattr(Author, "az_json", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)


@pytest.fixture
def transport(monkeypatch):
    sent, auth = [], []
    real_client = httpx.Client

    def identity(self, *args):
        auth.append(args)
        if args[:2] == ("account", "show"):
            return {"id": SUBSCRIPTION, "tenantId": TENANT,
                    "user": {"name": "author@synthetic.invalid", "type": "user"}}
        return {"accessToken": "SYNTHETIC-NOT-A-CREDENTIAL"}

    def handler(request):
        sent.append(request)
        path = request.url.path
        if request.method == "POST" and path == PREFIX + "/items":
            return httpx.Response(202, headers={
                "x-ms-operation-id": LRO, "Retry-After": "0",
                "Location": f"https://api.fabric.microsoft.com/v1/operations/{LRO}",
            })
        if path == f"/v1/operations/{LRO}":
            return httpx.Response(200, json={"status": "Succeeded"}, headers={
                "Location": f"https://api.fabric.microsoft.com/v1/operations/{LRO}/result",
            })
        if path in (f"/v1/operations/{LRO}/result", PREFIX + f"/items/{CREATED}"):
            return httpx.Response(200, json={"id": CREATED, **CREATE})
        if request.method == "POST" and path.endswith("/getDefinition"):
            return httpx.Response(200, json={"definition": {"parts": []}})
        return httpx.Response(200, json={"value": []})

    monkeypatch.setattr(Author, "az_json", identity)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handler),
    ))
    return sent, auth


def test_legacy_author_still_blocks_expired_p0_before_auth(tmp_path, no_auth):
    configure(tmp_path)
    with pytest.raises(RuntimeError, match="deadline elapsed"):
        Author(tmp_path)


def test_dedicated_shutdown_still_requires_actual_suspend_mode_not_stop_owned_jobs(tmp_path, no_auth):
    path, _, _ = configure(tmp_path)
    receipt = tmp_path / "renewal-shutdown-readback.json"
    evidence = read_json(receipt)
    evidence["binding"]["properties"]["parameters"]["Mode"] = "StopOwnedJobs"
    write(receipt, evidence)
    with pytest.raises(RuntimeError, match="Shutdown guard readback"):
        Renewal(tmp_path, path)
    evidence["binding"]["properties"]["parameters"]["Mode"] = "SuspendWhenDue"
    write(receipt, evidence)
    lease = Renewal(tmp_path, path)
    assert lease.verify_lifecycle_policy() == {
        "capacityPolicy": "DEDICATED_CAPACITY", "proofLevel": "ACTUAL_SHUTDOWN_READBACK",
    }
    receipt.unlink()
    with pytest.raises(RuntimeError, match="Invalid or unreadable"):
        Renewal(tmp_path, path)


@pytest.mark.parametrize("change", [
    lambda value: value.update(shutdown=None),
    lambda value: value.update(capacityPolicy="SHARED_CAPACITY_AS_IS", shutdown=None),
])
def test_missing_explicit_lifecycle_authority_does_not_disable_all_guards(tmp_path, no_auth, change):
    with pytest.raises(RuntimeError, match="requires"):
        configure(tmp_path, change=change)


@pytest.mark.parametrize("text", ["{", "[]", '{"approved":false}', '{"approved":true,"approved":false}', "{}"])
def test_invalid_json_and_unapproved_fail_before_auth(tmp_path, no_auth, text):
    path = tmp_path / "approval.json"
    path.write_text(text)
    with pytest.raises(RuntimeError):
        Author(tmp_path, renewal_approval=path)


def test_expired_renewal_fails_before_auth(tmp_path, no_auth):
    path, _, _ = configure(tmp_path, started=datetime.now(timezone.utc) - timedelta(hours=2))
    with pytest.raises(RuntimeError, match="expired"):
        Author(tmp_path, renewal_approval=path)


@pytest.mark.parametrize("target", ["workspace", "ontology"])
def test_owned_target_mismatch_before_auth(tmp_path, no_auth, target):
    path, _, _ = configure(tmp_path)
    write(tmp_path / f"p0-{target}.json", {"id": OTHER})
    with pytest.raises(RuntimeError, match="target mismatch"):
        Author(tmp_path, renewal_approval=path)


def test_renewal_accepts_new_expected_identity_without_rewriting_history(tmp_path, transport):
    path, _, _ = configure(tmp_path)
    historical = (tmp_path / "p0-approval.json").read_bytes()
    author = Author(tmp_path, renewal_approval=path)
    try:
        assert author.approval["authorUser"] == "author@synthetic.invalid"
        author.request("GET", PREFIX + "/items")
        assert len(transport[0]) == 1
        assert len(transport[1]) == 2
        assert (tmp_path / "p0-approval.json").read_bytes() == historical
        assert len(list(tmp_path.glob("http-*.json"))) == 1
    finally:
        author.client.close()


@pytest.mark.parametrize("field,value", [
    ("tenantId", OTHER), ("id", OTHER), ("name", "other@synthetic.invalid"), ("type", "servicePrincipal"),
])
def test_current_identity_checked_before_token(tmp_path, monkeypatch, transport, field, value):
    path, _, _ = configure(tmp_path)
    calls = []

    def identity(self, *args):
        calls.append(args)
        result = {"id": SUBSCRIPTION, "tenantId": TENANT,
                  "user": {"name": "author@synthetic.invalid", "type": "user"}}
        if field in {"name", "type"}:
            result["user"][field] = value
        else:
            result[field] = value
        return result

    monkeypatch.setattr(Author, "az_json", identity)
    with pytest.raises(RuntimeError, match="identity"):
        Author(tmp_path, renewal_approval=path)
    assert len(calls) == 1 and transport[0] == []


@pytest.mark.parametrize("method,path,kwargs", [
    ("PATCH", PREFIX + "/items", {"json": CREATE}),
    ("POST", PREFIX + "/items", {"json": CREATE | {"description": "unapproved"}}),
    ("POST", PREFIX + "/ontologies", {"json": CREATE}),
    ("GET", PREFIX + "/items?type=Ontology", {}),
    ("GET", "/v1/capacities", {}),
    ("GET", f"/v1/workspaces/{OTHER}/items", {}),
    ("GET", PREFIX + "/items", {"params": {"unapproved": "1"}}),
    ("GET", PREFIX + "/items", {"headers": {"Authorization": "not-allowed"}}),
    ("GET", "https://other.invalid" + PREFIX + "/items", {}),
    ("GET", "https://api.fabric.microsoft.com:443" + PREFIX + "/items", {}),
    ("GET", PREFIX + "/items/../items", {}),
    ("GET", PREFIX + "/items%2f" + ORIGINAL, {}),
    ("GET", PREFIX + "/items#fragment", {}),
    ("GET", PREFIX + "/items/{hero}", {}),
])
def test_outside_scope_denied_before_transport(tmp_path, transport, method, path, kwargs):
    approval_path, _, _ = configure(tmp_path)
    author = Author(tmp_path, renewal_approval=approval_path)
    try:
        with pytest.raises(RuntimeError):
            author.request(method, path, **kwargs)
        assert transport[0] == []
    finally:
        author.client.close()


def test_body_none_and_empty_object_are_distinct(tmp_path, transport):
    path, _, _ = configure(tmp_path)
    author = Author(tmp_path, renewal_approval=path)
    try:
        with pytest.raises(RuntimeError, match="allowlist"):
            author.request("POST", PREFIX + f"/ontologies/{ORIGINAL}/getDefinition")
        author.request("POST", PREFIX + f"/ontologies/{ORIGINAL}/getDefinition", json={})
        author.request("POST", PREFIX + f"/ontologies/{ORIGINAL}/getDefinition", json={})
        assert len(transport[0]) == 2
    finally:
        author.client.close()


def test_pagination_only_adds_one_continuation_token(tmp_path, transport):
    path, _, _ = configure(tmp_path)
    author = Author(tmp_path, renewal_approval=path)
    try:
        author.request("GET", PREFIX + "/items?continuationToken=opaque%2Bcursor")
        for query in ("continuationToken=one&continuationToken=two", "continuationToken=",
                      "continuationToken=one&extra=1"):
            with pytest.raises(RuntimeError):
                author.request("GET", PREFIX + "/items?" + query)
        assert len(transport[0]) == 1
    finally:
        author.client.close()


def test_cleanup_allowlist_cannot_mutate_original_ontology(tmp_path, transport):
    def extra(approval):
        approval["operations"].append(operation("old-cleanup", "DELETE", PREFIX + f"/items/{ORIGINAL}"))
    path, _, _ = configure(tmp_path, change=extra)
    author = Author(tmp_path, renewal_approval=path)
    try:
        with pytest.raises(RuntimeError, match="protected"):
            author.request("DELETE", PREFIX + f"/items/{ORIGINAL}")
        assert transport[0] == []
    finally:
        author.client.close()


def test_protection_is_uuid_case_insensitive(tmp_path, transport):
    original = "abcdefab-abcd-abcd-abcd-abcdefabcdef"

    def extra(approval):
        approval["protectedItemIds"].append(original)
        approval["operations"].append(operation("old-cleanup", "DELETE", PREFIX + f"/items/{original.upper()}"))

    path, _, _ = configure(tmp_path, change=extra)
    author = Author(tmp_path, renewal_approval=path)
    try:
        with pytest.raises(RuntimeError, match="protected"):
            author.request("DELETE", PREFIX + f"/items/{original.upper()}")
        assert transport[0] == []
    finally:
        author.client.close()


def test_lro_and_created_readback_are_receipt_bound_and_persistent(tmp_path, transport):
    path, _, _ = configure(tmp_path)
    author = Author(tmp_path, renewal_approval=path)
    try:
        with pytest.raises(RuntimeError, match="allowlist"):
            author.request("GET", f"/v1/operations/{LRO}")
        created = author.create_item("hero", CREATE)
        assert created["id"] == CREATED
        assert (tmp_path / "p0-hero-accepted.json").exists()
        assert (tmp_path / "p0-hero-readback.json").exists()
        count = len(transport[0])
        with pytest.raises(RuntimeError, match="already attempted"):
            author.request("POST", PREFIX + "/items", json=CREATE)
        with pytest.raises(RuntimeError, match="allowlist"):
            author.request("GET", f"/v1/operations/{OTHER}/result")
        assert len(transport[0]) == count
    finally:
        author.client.close()
    reopened = Author(tmp_path, renewal_approval=path)
    try:
        reopened.request("GET", PREFIX + f"/items/{CREATED}")
        with pytest.raises(RuntimeError, match="already attempted"):
            reopened.request("POST", PREFIX + "/items", json=CREATE)
    finally:
        reopened.client.close()


def test_unknown_mutation_outcome_is_not_retried(tmp_path, transport, monkeypatch):
    path, _, _ = configure(tmp_path)
    author = Author(tmp_path, renewal_approval=path)
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic ambiguous result", request=request)

    monkeypatch.setattr(author.client, "request", lambda method, path, **kwargs: timeout(
        httpx.Request(method, "https://synthetic.invalid"),
    ))
    try:
        with pytest.raises(httpx.ReadTimeout):
            author.request("POST", PREFIX + "/items", json=CREATE)
        with pytest.raises(RuntimeError, match="already attempted"):
            author.request("POST", PREFIX + "/items", json=CREATE)
        assert len(calls) == 1
        assert len(list(tmp_path.glob("transport-*.json"))) == 1
    finally:
        author.client.close()


def test_accepted_without_operation_id_is_not_resubmitted(tmp_path, transport, monkeypatch):
    path, _, _ = configure(tmp_path)
    author = Author(tmp_path, renewal_approval=path)
    calls = []

    def accepted(*args, **kwargs):
        calls.append(args)
        return httpx.Response(202)

    monkeypatch.setattr(author.client, "request", accepted)
    try:
        with pytest.raises(RuntimeError, match="operation ID absent"):
            author.request("POST", PREFIX + "/items", json=CREATE)
        with pytest.raises(RuntimeError, match="already attempted"):
            author.request("POST", PREFIX + "/items", json=CREATE)
        assert len(calls) == 1
        assert next((tmp_path / "renewal-requests").glob("*.json")).is_file()
    finally:
        author.client.close()


def test_create_cannot_adopt_original_id_even_if_returned_by_service(tmp_path, transport, monkeypatch):
    path, _, _ = configure(tmp_path)
    author = Author(tmp_path, renewal_approval=path)
    monkeypatch.setattr(author.client, "request", lambda *args, **kwargs: httpx.Response(
        201, json={"id": ORIGINAL, **CREATE},
    ))
    try:
        with pytest.raises(RuntimeError, match="existing/protected"):
            author.request("POST", PREFIX + "/items", json=CREATE)
        assert not author.renewal._derived()[0]
        with pytest.raises(RuntimeError, match="already attempted"):
            author.request("POST", PREFIX + "/items", json=CREATE)
    finally:
        author.client.close()


def test_legacy_accepted_marker_alone_prevents_create_resubmission(tmp_path):
    author = Author.__new__(Author)
    author.private = tmp_path
    write(tmp_path / "p0-hero-accepted.json", {"status": 202, "operationId": LRO})
    with pytest.raises(RuntimeError, match="already accepted"):
        author.create_item("hero", CREATE)


def test_separately_approved_hashed_receipt_can_recover_old_lro_without_mutating(tmp_path, transport):
    receipt = tmp_path / "http-previous-create.json"
    write(receipt, {
        "method": "POST", "path": PREFIX + "/items", "status": 202, "operationId": LRO,
        "approvalSha256": "e" * 64,
        "bodySha256": body_digest(CREATE), "location": f"https://api.fabric.microsoft.com/v1/operations/{LRO}",
        "resultPath": f"/v1/operations/{LRO}/result",
    })
    path, _, _ = configure(tmp_path, change=lambda a: a.update(
        recoveryReceipts=[{"file": receipt.name, "sha256": sha256(receipt.read_bytes())}],
    ))
    author = Author(tmp_path, renewal_approval=path)
    try:
        author.request("GET", f"/v1/operations/{LRO}")
        author.request("GET", f"/v1/operations/{LRO}/result")
        with pytest.raises(RuntimeError, match="allowlist"):
            author.request("GET", f"/v1/operations/{OTHER}")
        assert all(request.method == "GET" for request in transport[0])
        assert not list((tmp_path / "renewal-requests").glob("*.json"))
    finally:
        author.client.close()


def test_recovery_target_mismatch_blocks_before_auth(tmp_path, no_auth):
    receipt = tmp_path / "http-previous-create.json"
    write(receipt, {
        "method": "POST", "path": f"/v1/workspaces/{OTHER}/items", "status": 202, "operationId": LRO,
    })
    path, _, _ = configure(tmp_path, change=lambda a: a.update(
        recoveryReceipts=[{"file": receipt.name, "sha256": sha256(receipt.read_bytes())}],
    ))
    with pytest.raises(RuntimeError, match="exact workspace"):
        Author(tmp_path, renewal_approval=path)


def test_window_is_immutable_and_bounded_at_execution_not_proposal(tmp_path):
    path, approval, window = configure(tmp_path)
    start = datetime.fromisoformat(window["startedAtUtc"])
    assert datetime.fromisoformat(window["deadlineUtc"]) == start + timedelta(
        seconds=approval["window"]["ttlSeconds"],
    )
    with pytest.raises(FileExistsError):
        stage_execution_window(path, tmp_path)
    assert window["shutdownGuard"] == "NOT_VERIFIED" and window["resume"] == "NOT_RUN"


@pytest.mark.parametrize("change", [
    lambda e: e["schedule"]["properties"].update(isEnabled=False),
    lambda e: e["schedule"]["properties"].update(startTime="2099-01-01T00:00:00Z"),
    lambda e: e["schedule"]["properties"].update(expiryTime="2099-01-01T00:00:00Z"),
    lambda e: e["binding"]["properties"]["parameters"].update(TargetResourceId="another-capacity"),
    lambda e: e["binding"]["properties"]["runbook"].update(name="OtherRunbook"),
    lambda e: e["binding"]["properties"]["parameters"].update(tenantId=TENANT),
    lambda e: e.update(observedAtUtc="2000-01-01T00:00:00Z"),
    lambda e: e.update(inspect={"status": "Completed", "mutations": 1}),
])
def test_guard_requires_actual_matching_fresh_schedule_evidence(tmp_path, no_auth, change):
    path, _, _ = configure(tmp_path)
    receipt = tmp_path / "renewal-shutdown-readback.json"
    evidence = json.loads(receipt.read_bytes())
    change(evidence)
    write(receipt, evidence)
    with pytest.raises(RuntimeError, match="Shutdown guard"):
        Author(tmp_path, renewal_approval=path)


def test_missing_guard_and_modified_artifact_fail_before_auth(tmp_path, no_auth):
    path, _, _ = configure(tmp_path)
    (tmp_path / "renewal-shutdown-readback.json").unlink()
    with pytest.raises(RuntimeError, match="unreadable renewal JSON"):
        Author(tmp_path, renewal_approval=path)
    (tmp_path / "synthetic-payload.json").write_text("{}")
    with pytest.raises(RuntimeError, match="artifact changed"):
        Author(tmp_path, renewal_approval=path)


def test_scope_changes_require_a_new_context_and_window(tmp_path, transport):
    path, approval, _ = configure(tmp_path)
    author = Author(tmp_path, renewal_approval=path)
    try:
        approval["cost"]["cap"] += 1
        write(path, approval)
        with pytest.raises(RuntimeError, match="Renewal changed"):
            author.request("GET", PREFIX + "/items")
        assert transport[0] == []
    finally:
        author.client.close()


def test_expiry_rechecked_for_every_request(tmp_path, transport, monkeypatch):
    path, _, _ = configure(tmp_path)
    author = Author(tmp_path, renewal_approval=path)

    class AfterDeadline(datetime):
        @classmethod
        def now(cls, tz=None):
            return author.renewal.deadline + timedelta(seconds=1)

    monkeypatch.setattr(renewal, "datetime", AfterDeadline)
    try:
        with pytest.raises(RuntimeError, match="expired"):
            author.request("POST", PREFIX + "/items", json=CREATE)
        assert transport[0] == []
    finally:
        author.client.close()


def test_artifact_rechecked_for_every_mutation(tmp_path, transport):
    path, _, _ = configure(tmp_path)
    author = Author(tmp_path, renewal_approval=path)
    (tmp_path / "synthetic-payload.json").write_text("{}")
    try:
        with pytest.raises(RuntimeError, match="artifact changed"):
            author.request("POST", PREFIX + "/items", json=CREATE)
        assert transport[0] == []
    finally:
        author.client.close()


@pytest.mark.parametrize("change", [
    lambda a: a.update(approved=False),
    lambda a: a["window"].update(notAfterUtc="2026-10-02T00:00:00"),
    lambda a: a["cost"].update(estimated=100),
    lambda a: a["cost"].update(unitPrice=None),
    lambda a: a.update(artifactSetSha256="0" * 64),
    lambda a: a["operations"][0].update(path="/v1/workspaces/" + OTHER + "/items"),
])
def test_schema_expiry_target_and_cost_fail_locally(tmp_path, no_auth, change):
    with pytest.raises(RuntimeError):
        configure(tmp_path, change=change)


def test_window_tampering_and_missing_original_protection_block(tmp_path, no_auth):
    path, _, _ = configure(tmp_path, change=lambda a: a.update(protectedItemIds=[]))
    with pytest.raises(RuntimeError, match="explicitly protected"):
        Renewal(tmp_path, path)
    window_path = next(tmp_path.glob("renewal-window-*.json"))
    window = json.loads(window_path.read_bytes())
    window["deadlineUtc"] = "2099-01-01T00:00:00Z"
    write(window_path, window)
    with pytest.raises(RuntimeError, match="altered"):
        Renewal(tmp_path, path)


@pytest.mark.parametrize("kind", ["create", "notebook"])
@pytest.mark.parametrize("changed_body", [False, True])
def test_review_legacy_reservation_survives_new_approval_and_body_change(tmp_path, kind, changed_body):
    previous_body = CREATE if kind == "create" else None
    current_body = CREATE | {"description": "separately reviewed bytes"} if changed_body else CREATE
    origin = PREFIX + "/items" if kind == "create" else PREFIX + f"/items/{CREATED}/jobs/RunNotebook/instances"
    if kind == "notebook":
        current_body = {"executionData": {"reviewed": "different"}} if changed_body else None
    digest = body_digest(previous_body)
    ledger = tmp_path / "renewal-requests"
    ledger.mkdir()
    legacy = {
        "approvalSha256": "f" * 64, "operation": "old-" + kind, "method": "POST", "path": origin,
        "bodySha256": digest, "state": "RESERVED_OUTCOME_UNKNOWN",
        "startedAtUtc": "2026-10-01T00:00:00Z",
    }
    # Actual baseline30fdd48 key, not the new createName reservation filename.
    key = body_digest({"method": "POST", "path": origin, "bodySha256": digest})
    old_file = ledger / (key + ".json")
    write(old_file, legacy)
    original = old_file.read_bytes()

    def scopes(approval):
        approval["confirmationReference"] = "SYNTHETIC fresh approval, old unknown remains reserved"
        approval["operations"] = [operation(
            "create" if kind == "create" else "notebook", "POST", origin, current_body,
            **({"createsAlias": "hero", "createdDisplayName": CREATE["displayName"]} if kind == "create" else {}),
        )]

    path, _, _ = configure(tmp_path, change=scopes)
    lease = Renewal(tmp_path, path)
    with pytest.raises(RuntimeError, match="already attempted|reserved|unknown"):
        lease.begin("POST", origin, {} if current_body is None else {"json": current_body})
    assert old_file.read_bytes() == original
    assert list(ledger.glob("*.json")) == [old_file]


def test_review_pending_recovery_derives_result_from_new_exact_status_only(tmp_path, transport):
    receipt = tmp_path / "previous-pending.json"
    write(receipt, {
        "approvalSha256": "e" * 64, "method": "POST", "path": PREFIX + "/items", "status": 202,
        "operationId": LRO, "bodySha256": body_digest(CREATE),
        "location": f"https://api.fabric.microsoft.com/v1/operations/{LRO}",
    })
    original = receipt.read_bytes()
    path, _, _ = configure(tmp_path, change=lambda approval: approval.update(
        recoveryReceipts=[{"file": receipt.name, "sha256": sha256(original)}],
    ))
    author = Author(tmp_path, renewal_approval=path)
    try:
        with pytest.raises(RuntimeError, match="allowlist"):
            author.request("GET", f"/v1/operations/{LRO}/result")
        author.request("GET", f"/v1/operations/{LRO}")
        author.request("GET", f"/v1/operations/{LRO}/result")
        restarted = Renewal(tmp_path, path)
        assert f"/v1/operations/{LRO}/result" in restarted._derived()[1]
        assert all(request.method == "GET" for request in transport[0])
        assert receipt.read_bytes() == original
        assert not list((tmp_path / "renewal-requests").glob("*.json"))
    finally:
        author.client.close()


def pending_recovery(private, *, change=None, lro_polling=None):
    receipt = private / "previous-pending.json"
    record = {
        "approvalSha256": "e" * 64, "method": "POST", "path": PREFIX + "/items", "status": 202,
        "operationId": LRO, "bodySha256": body_digest(CREATE),
        "location": f"https://api.fabric.microsoft.com/v1/operations/{LRO}",
    }
    if change:
        change(record)
    write(receipt, record)
    path, _, _ = configure(private, change=lambda approval: approval.update(
        recoveryReceipts=[{"file": receipt.name, "sha256": sha256(receipt.read_bytes())}],
        **({"lroPolling": lro_polling} if lro_polling else {}),
    ))
    return path, receipt


@pytest.fixture
def regional_author(tmp_path, monkeypatch):
    def approve(value):
        value["lroPolling"] = PUBLIC_ID_POLICY
        value["operations"] = [operation(
            "read-protected-source", "POST", PREFIX + f"/semanticModels/{ORIGINAL}/getDefinition?format=TMDL",
            {}, allowLro=True,
        )]

    path, _, _ = configure(tmp_path, change=approve)
    source = PREFIX + f"/semanticModels/{ORIGINAL}/getDefinition"
    state = {
        "acceptedHeaders": {"Location": REGIONAL + "/v1/operations/" + LRO, "x-ms-operation-id": LRO, "Retry-After": "0"},
        "stateHeaders": {"Location": REGIONAL + "/v1/operations/" + LRO + "/result", "x-ms-operation-id": LRO},
        "stateBody": {"status": "Succeeded", "error": None},
        "stateCode": 200, "resultHeaders": {"x-ms-operation-id": LRO}, "resultCode": 200,
        "resultBody": {"definition": {"format": "TMDL", "parts": []}},
    }
    sent = []

    def handler(request):
        sent.append(request)
        assert request.url.host == "api.fabric.microsoft.com"
        if request.method == "POST" and request.url.path == source:
            return httpx.Response(202, headers=state["acceptedHeaders"])
        if request.method == "GET" and request.url.path == "/v1/operations/" + LRO:
            return httpx.Response(state["stateCode"], headers=state["stateHeaders"], json=state["stateBody"])
        if request.method == "GET" and request.url.path == "/v1/operations/" + LRO + "/result":
            return httpx.Response(state["resultCode"], headers=state["resultHeaders"], json=state["resultBody"])
        pytest.fail("Unreviewed regional-LRO mock route")

    monkeypatch.setattr("infra.fabric_p0.time.sleep", lambda _: None)
    monkeypatch.setattr(Author, "az_json", lambda *_: pytest.fail("Regional LRO tests attempted auth"))
    author = object.__new__(Author)
    author.private, author.renewal = tmp_path, Renewal(tmp_path, path)
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        author.client = client
        yield author, state, sent, source + "?format=TMDL"


def test_public_id_protected_definition_preserves_regional_headers_and_immutable_proof(regional_author):
    author, state, sent, source = regional_author
    accepted = author.request("POST", source, json={})
    raw_receipt = next(author.private.glob("http-*.json"))
    original_http = raw_receipt.read_bytes()
    record = author.renewal._records()[0]
    snapshot = author.private / record["acceptedReceipt"]["file"]
    original_snapshot = snapshot.read_bytes()
    with pytest.raises(RuntimeError, match="allowlist"):
        author.request("GET", "/v1/operations/" + LRO + "/result")
    assert author.completed(accepted) == state["resultBody"]
    assert accepted.headers["Location"] == REGIONAL + "/v1/operations/" + LRO
    assert raw_receipt.read_bytes() == original_http and snapshot.read_bytes() == original_snapshot
    observation = next(author.renewal._recovery_directory().glob("*.json"))
    assert read_json(observation)["location"] == REGIONAL + "/v1/operations/" + LRO + "/result"
    assert read_json(observation)["operationIdHeader"] == LRO
    reopened = Renewal(author.private, author.renewal.path)
    assert "/v1/operations/" + LRO + "/result" in reopened._derived()[1]
    assert author.completed(accepted) == state["resultBody"]
    assert sum(request.method == "POST" for request in sent) == 1
    assert all(request.url.host == "api.fabric.microsoft.com" for request in sent)


@pytest.mark.parametrize("header,location", [
    (None, REGIONAL + "/v1/operations/" + LRO),
    ("invalid", REGIONAL + "/v1/operations/" + LRO),
    ("AAAAAAAA-0000-0000-0000-000000000006", REGIONAL + "/v1/operations/" + LRO),
    (OTHER, REGIONAL + "/v1/operations/" + LRO),
    (LRO, "https://evil.invalid/v1/operations/" + LRO),
    (LRO, REGIONAL + "/v1/operations/" + OTHER),
    (LRO, REGIONAL + "/v1/operations/" + LRO + "/result"),
    (LRO, REGIONAL + "/v1/operations/" + LRO + "?unreviewed=1"),
    (LRO, REGIONAL + "/v1/operations/" + LRO + "#fragment"),
    (LRO, REGIONAL + "/v1/operations/%2e/" + LRO),
    (LRO, REGIONAL.replace("https://", "https://user@") + "/v1/operations/" + LRO),
    (LRO, REGIONAL.replace("https://", "http://") + "/v1/operations/" + LRO),
    (LRO, REGIONAL + ":443/v1/operations/" + LRO),
])
def test_public_id_initial_header_origin_path_denials_preserve_acceptance_without_polling(
    regional_author, header, location,
):
    author, state, sent, source = regional_author
    state["acceptedHeaders"] = {"Location": location, **({"x-ms-operation-id": header} if header else {})}
    with pytest.raises(RuntimeError):
        author.request("POST", source, json={})
    record = author.renewal._records()[0]
    assert record["location"] == location and record["operationId"] == header
    assert record.get("receiptValidated") is not True
    with pytest.raises(RuntimeError, match="allowlist"):
        author.request("GET", "/v1/operations/" + LRO)
    assert len(sent) == 1 and sent[0].method == "POST"


@pytest.mark.parametrize("kind", [
    "state-code", "unknown-state", "failed-state", "error-state", "partial-state",
    "state-header", "result-location-id", "result-location-host", "running-result-location",
])
def test_public_id_requires_exact_successful_state_before_result(regional_author, kind):
    author, state, sent, source = regional_author
    accepted = author.request("POST", source, json={})
    if kind == "state-code":
        state["stateCode"] = 202
    elif kind == "unknown-state":
        state["stateBody"]["status"] = "Unknown"
    elif kind == "failed-state":
        state["stateBody"]["status"] = "Failed"
    elif kind == "error-state":
        state["stateBody"]["error"] = {}
    elif kind == "partial-state":
        state["stateBody"]["partial"] = True
    elif kind == "state-header":
        state["stateHeaders"]["x-ms-operation-id"] = OTHER
    elif kind == "result-location-id":
        state["stateHeaders"]["Location"] = REGIONAL + "/v1/operations/" + OTHER + "/result"
    elif kind == "result-location-host":
        state["stateHeaders"]["Location"] = "https://evil.invalid/v1/operations/" + LRO + "/result"
    elif kind == "running-result-location":
        state["stateBody"]["status"] = "Running"
    with pytest.raises(RuntimeError):
        author.completed(accepted)
    with pytest.raises(RuntimeError, match="allowlist"):
        author.request("GET", "/v1/operations/" + LRO + "/result")
    assert not any(request.url.path.endswith("/result") for request in sent)
    assert sum(request.method == "POST" for request in sent) == 1


@pytest.mark.parametrize("kind", ["header", "status", "error", "partial"])
def test_public_id_result_response_does_not_accept_wrong_header_or_unknown_body(regional_author, kind):
    author, state, sent, source = regional_author
    accepted = author.request("POST", source, json={})
    if kind == "header":
        state["resultHeaders"]["x-ms-operation-id"] = OTHER
    elif kind == "status":
        state["resultCode"] = 202
    elif kind == "error":
        state["resultBody"] = {"error": {}}
    else:
        state["resultBody"]["partial"] = True
    with pytest.raises(RuntimeError):
        author.completed(accepted)
    assert sum(request.method == "POST" for request in sent) == 1


def test_public_id_recovery_of_original_regional_receipt_is_read_only_and_immutable(tmp_path):
    path, receipt = pending_recovery(
        tmp_path, change=lambda value: value.update(location=REGIONAL + "/v1/operations/" + LRO),
        lro_polling=PUBLIC_ID_POLICY,
    )
    original = receipt.read_bytes()
    lease = Renewal(tmp_path, path)
    with pytest.raises(RuntimeError, match="allowlist"):
        lease.begin("GET", "/v1/operations/" + LRO + "/result", {})
    lease.observe(None, "GET", "/v1/operations/" + LRO, httpx.Response(200, headers={
        "Location": REGIONAL + "/v1/operations/" + LRO + "/result", "x-ms-operation-id": LRO,
    }), {"status": "Succeeded", "error": None})
    assert "/v1/operations/" + LRO + "/result" in Renewal(tmp_path, path)._derived()[1]
    assert receipt.read_bytes() == original
    observation = next(lease._recovery_directory().glob("*.json"))
    value = read_json(observation)
    observation.unlink()
    value["location"] = REGIONAL + "/v1/operations/" + OTHER + "/result"
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (lease._recovery_directory() / (sha256(raw) + ".json")).write_bytes(raw)
    with pytest.raises(RuntimeError, match="recovery observation"):
        Renewal(tmp_path, path)
    assert receipt.read_bytes() == original


@pytest.mark.parametrize("location", [
    "https://evil.invalid/v1/operations/" + LRO + "/result",
    "https://user@api.fabric.microsoft.com/v1/operations/" + LRO + "/result",
    "http://api.fabric.microsoft.com/v1/operations/" + LRO + "/result",
    "https://api.fabric.microsoft.com/v1/operations/" + OTHER + "/result",
    "https://api.fabric.microsoft.com/v1/operations/" + LRO + "/result/extra",
])
def test_review_recovery_rejects_wrong_exposed_host_id_or_path(tmp_path, location):
    path, receipt = pending_recovery(tmp_path)
    before = receipt.read_bytes()
    lease = Renewal(tmp_path, path)
    lease.begin("GET", f"/v1/operations/{LRO}", {})
    with pytest.raises(RuntimeError):
        lease.observe(None, "GET", f"/v1/operations/{LRO}",
                      httpx.Response(200, headers={"Location": location}), {"status": "Succeeded"})
    with pytest.raises(RuntimeError, match="allowlist"):
        lease.begin("GET", f"/v1/operations/{LRO}/result", {})
    assert receipt.read_bytes() == before
    assert not list(lease._recovery_directory().glob("*.json"))


@pytest.mark.parametrize("status,body", [
    (200, {"status": "Running"}), (200, {"status": "Failed"}), (200, {"status": "Cancelled"}),
    (200, {"status": "Succeeded", "error": {"code": "partial"}}), (202, {"status": "Succeeded"}),
    (500, {"status": "Succeeded"}),
])
def test_review_pending_or_failed_recovery_response_never_grants_result(tmp_path, status, body):
    path, _ = pending_recovery(tmp_path)
    lease = Renewal(tmp_path, path)
    lease.observe(None, "GET", f"/v1/operations/{LRO}", httpx.Response(
        status, headers={"Location": f"https://api.fabric.microsoft.com/v1/operations/{LRO}/result"},
    ), body)
    with pytest.raises(RuntimeError, match="allowlist"):
        lease.begin("GET", f"/v1/operations/{LRO}/result", {})
    assert not list(lease._recovery_directory().glob("*.json"))


@pytest.mark.parametrize("change", [
    lambda record: record.update(approvalSha256="not-an-approval"),
    lambda record: record.update(state="RESERVED_OUTCOME_UNKNOWN"),
    lambda record: record.update(operationId=OTHER),
    lambda record: record.update(status=201),
])
def test_review_invalid_original_recovery_identity_or_approval_blocks_locally(tmp_path, change):
    path, _ = pending_recovery(tmp_path, change=change)
    with pytest.raises(RuntimeError):
        Renewal(tmp_path, path)


def observed_recovery(private):
    path, receipt = pending_recovery(private)
    lease = Renewal(private, path)
    lease.observe(None, "GET", f"/v1/operations/{LRO}", httpx.Response(
        200, headers={"Location": f"https://api.fabric.microsoft.com/v1/operations/{LRO}/result"},
    ), {"status": "Succeeded"})
    return lease, receipt, next(lease._recovery_directory().glob("*.json"))


@pytest.mark.parametrize("change", [
    lambda value: value.update(approvalSha256="f" * 64),
    lambda value: value.update(acceptedApprovalSha256="f" * 64),
    lambda value: value["acceptedReceipt"].update(sha256="0" * 64),
    lambda value: value.update(path=f"/v1/operations/{OTHER}"),
    lambda value: value.update(resultPath=f"/v1/operations/{OTHER}/result"),
    lambda value: value.update(responseBody={"status": "Failed"},
                               responseBodySha256=body_digest({"status": "Failed"})),
    lambda value: value.update(observedAtUtc="2099-01-01T00:00:00Z"),
])
def test_review_recovery_observation_cannot_cross_approval_receipt_or_outcome(tmp_path, change):
    lease, receipt, observation = observed_recovery(tmp_path)
    historical = receipt.read_bytes()
    value = read_json(observation)
    change(value)
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (observation.parent / (sha256(raw) + ".json")).write_bytes(raw)
    with pytest.raises(RuntimeError, match="recovery observation|Recovery observation"):
        lease.begin("GET", f"/v1/operations/{LRO}/result", {})
    assert receipt.read_bytes() == historical


def test_review_corrupt_observation_and_expired_status_never_grant_result(tmp_path, monkeypatch):
    lease, receipt, observation = observed_recovery(tmp_path)
    historical = receipt.read_bytes()
    observation.write_bytes(observation.read_bytes() + b" ")
    with pytest.raises(RuntimeError, match="Corrupt"):
        lease.begin("GET", f"/v1/operations/{LRO}/result", {})

    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return lease.deadline + timedelta(seconds=1)

    monkeypatch.setattr(renewal, "datetime", Expired)
    with pytest.raises(RuntimeError, match="expired"):
        lease.observe(None, "GET", f"/v1/operations/{LRO}", httpx.Response(
            200, headers={"Location": f"https://api.fabric.microsoft.com/v1/operations/{LRO}/result"},
        ), {"status": "Succeeded"})
    assert receipt.read_bytes() == historical
    assert len(list(lease._recovery_directory().glob("*.json"))) == 1


def test_review_legacy_unknown_does_not_block_a_different_exact_notebook_target(tmp_path):
    ledger = tmp_path / "renewal-requests"
    ledger.mkdir()
    original = {
        "approvalSha256": "f" * 64, "operation": "old-job", "method": "POST",
        "path": PREFIX + f"/items/{OTHER}/jobs/RunNotebook/instances", "bodySha256": body_digest(None),
        "state": "RESERVED_OUTCOME_UNKNOWN",
    }
    old_file = ledger / (body_digest({
        "method": "POST", "path": original["path"], "bodySha256": original["bodySha256"],
    }) + ".json")
    write(old_file, original)
    target = PREFIX + f"/items/{CREATED}/jobs/RunNotebook/instances"
    path, _, _ = configure(tmp_path, change=lambda approval: approval.update(operations=[
        operation("other-exact-job", "POST", target),
    ]))
    lease = Renewal(tmp_path, path)
    assert lease.begin("POST", target, {})["state"] == "RESERVED_OUTCOME_UNKNOWN"
    assert read_json(old_file) == original


def test_review_readback_verified_distinct_create_is_not_a_global_collection_block(tmp_path):
    ledger = tmp_path / "renewal-requests"
    ledger.mkdir()
    previous = {
        "approvalSha256": "f" * 64, "operation": "other-create", "method": "POST",
        "path": PREFIX + "/items", "bodySha256": body_digest({"displayName": "Different_Owned_Name"}),
        "state": "ACCEPTED", "receiptValidated": True, "readbackVerified": True, "createdId": OTHER,
        "createdItem": {"id": OTHER, "displayName": "Different_Owned_Name", "type": "Notebook"},
    }
    old_key = body_digest({"method": "POST", "path": previous["path"], "bodySha256": previous["bodySha256"]})
    write(ledger / (old_key + ".json"), previous)
    path, _, _ = configure(tmp_path)
    lease = Renewal(tmp_path, path)
    assert lease.begin("POST", PREFIX + "/items", {"json": CREATE})["state"] == "RESERVED_OUTCOME_UNKNOWN"
