import json
import sys
import threading
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from test_auth import RESOURCES, ROOT, FakeMsal, config, resource_config

from telco_iq.api import create_app
from telco_iq.auth import STORAGE_SCOPES, AuthError, ReaderAuth
from telco_iq.contracts import Status
from telco_iq.errors import LabError

FIXED_LIVE = ROOT.parent / "synthetic-unit-fixed-live-config-not-read.json"
FIXED_OBSERVATION = ROOT.parent / "synthetic-unit-fixed-observation-config-not-read.json"


def browser(tmp_path, *, configured=True, **app_options):
    cfg = resource_config(tmp_path) if configured else config(tmp_path)
    fake = FakeMsal(cfg)
    manager = ReaderAuth(cfg, lambda _: fake)
    client = TestClient(create_app(ROOT, reader_auth=manager, **app_options), base_url="http://localhost:8765")
    login = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    callback = client.get("/auth/callback", params={"code": "synthetic", "state": state}, follow_redirects=False)
    assert callback.headers["location"] == "/"
    return client, manager, fake, manager.session(client.cookies.get("telco_iq_session"))


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_resource_consent_json_navigation_and_callback_preserve_identity_without_live_claim(tmp_path, resource, scopes):
    client, manager, fake, old = browser(tmp_path)
    initial = client.get("/api/health").json()
    def active_scope():
        return manager.active(old)
    response = client.post(f"/auth/consent/{resource}", json={}, headers={"X-CSRF-Token": old.csrf})
    assert response.status_code == 200 and "location" not in response.headers
    assert response.json()["resource"] == resource
    uri = urlparse(response.json()["authorization_uri"])
    query = parse_qs(uri.query)
    assert set(query["scope"][0].split()) == set(scopes) | {"openid", "profile", "offline_access"}
    assert query["redirect_uri"] == [manager.config.redirect_uri]
    assert uri.hostname == "login.microsoftonline.com"
    assert "httponly" in response.headers["set-cookie"].lower()
    assert manager.active(old) and active_scope()
    pending = next(iter(manager.pending.values()))
    assert pending.purpose == "resource_consent" and pending.prior_session_key == old.key
    for private in (
        "synthetic-unit-token", pending.flow["code_verifier"], pending.flow["nonce"], "token_cache",
    ):
        assert private not in response.text
    fake.result["scope"] = " ".join(scopes)
    result = client.get("/auth/callback", params={
        "code": "synthetic-resource-code", "state": query["state"][0],
        "resource": "graph", "scope": "https://graph.microsoft.com/.default",
    }, follow_redirects=False)
    assert result.headers["location"] == "/"
    new = manager.session(client.cookies.get("telco_iq_session"))
    assert new is not old and new.client is old.client and new.lock is old.lock
    assert new.epoch != old.epoch and new.csrf != old.csrf
    assert not active_scope()
    with pytest.raises(AuthError):
        manager.csrf(old, old.csrf)
    with pytest.raises(AuthError):
        manager.token(old, resource)
    updated = client.get("/api/health").json()
    assert updated["readiness"] == initial["readiness"]
    state = next(item for item in updated["resource_consent"] if item["resource"] == resource)
    assert state == {
        "resource": resource, "configuration": "CONFIGURED", "consent": "COMPLETED_IN_CURRENT_SESSION",
        "authorization": "UNKNOWN_NOT_PROBED", "execution": "NOT_ESTABLISHED",
    }
    assert not updated["data_ready"] and updated["readiness"]["demonstration"] == "NOT_ESTABLISHED"
    assert not fake.silent_requests and not manager.pending


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_existing_grant_api_validates_current_sdk_and_cookie_without_tokens_or_ready_claim(tmp_path, resource, scopes):
    client, manager, fake, old = browser(tmp_path)
    fake.result["scope"] = " ".join(scopes)
    before = client.get("/api/health").json()
    response = client.post(f"/auth/authorize-existing/{resource}", json={}, headers={"X-CSRF-Token": old.csrf})
    assert response.status_code == 200
    assert response.json()["status"] == "EXISTING_GRANT_VALIDATED_CURRENT_SESSION"
    assert response.json()["resource"] == resource and response.json()["ready"] is False
    assert "synthetic-unit-token" not in response.text and "authorization_uri" not in response.json()
    assert "httponly" in response.headers["set-cookie"].lower()
    new = manager.session(client.cookies["telco_iq_session"])
    assert new is not old and not manager.active(old) and new.epoch != old.epoch and new.csrf != old.csrf
    assert new.existing_grant_resources == {resource} and not new.consented_resources
    after = client.get("/api/health").json()
    state = next(item for item in after["resource_consent"] if item["resource"] == resource)
    assert state["consent"] == "EXISTING_GRANT_VALIDATED_IN_CURRENT_SESSION"
    assert state["authorization"] == "UNKNOWN_NOT_PROBED" and state["execution"] == "NOT_ESTABLISHED"
    assert after["readiness"] == before["readiness"] and not after["data_ready"]
    assert len(fake.begin_requests) == 1 and len(fake.callback_requests) == 1
    assert fake.silent_requests == [(list(scopes), fake.accounts[0])]


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_existing_grant_api_requires_current_session_csrf_canonical_host_origin_and_empty_body(
    tmp_path, resource, scopes,
):
    client, manager, fake, old = browser(tmp_path)
    for headers in (
        {}, {"X-CSRF-Token": "forged"}, {"X-CSRF-Token": old.csrf, "Origin": "https://untrusted.test"},
        {"X-CSRF-Token": old.csrf, "Host": "untrusted.test"},
    ):
        assert client.post(f"/auth/authorize-existing/{resource}", json={}, headers=headers).status_code in (400, 403)
    for query, body in (("?resource=graph", {}), ("", {"scopes": ["https://graph.microsoft.com/.default"]})):
        result = client.post(f"/auth/authorize-existing/{resource}" + query, json=body,
                             headers={"X-CSRF-Token": old.csrf})
        assert result.status_code == 400
    assert client.post(f"/auth/authorize-existing/{resource}", json={},
                       headers={"X-CSRF-Token": old.csrf, "Host": "127.0.0.1:8765"}).status_code == 401
    client.cookies.clear()
    assert client.post(f"/auth/authorize-existing/{resource}", json={},
                       headers={"X-CSRF-Token": old.csrf}).status_code == 403
    assert not fake.silent_requests and manager.active(old) and len(fake.begin_requests) == 1


@pytest.mark.parametrize("resource,scopes", RESOURCES)
@pytest.mark.parametrize("code", ["interaction_required", "consent_required", "invalid_grant"])
def test_existing_grant_api_keeps_interaction_required_explicit_and_preserves_reader(tmp_path, resource, scopes, code):
    client, manager, fake, old = browser(tmp_path)
    fake.result = {"error": code, "error_description": "sensitive-upstream-auth-value"}
    result = client.post(f"/auth/authorize-existing/{resource}", json={}, headers={"X-CSRF-Token": old.csrf})
    assert result.status_code == 401 and result.json()["status"] == "BLOCKED_AUTH"
    assert code in result.json()["detail"] and "sensitive-upstream" not in result.text
    assert manager.active(old) and manager.session(client.cookies["telco_iq_session"]) is old
    assert not old.existing_grant_resources and not manager.pending and len(fake.begin_requests) == 1


@pytest.mark.parametrize("resource", ["iq", "ontology", "graph", ".default"])
def test_existing_grant_api_rejects_unknown_resources_before_sdk(tmp_path, resource):
    client, manager, fake, old = browser(tmp_path)
    result = client.post(f"/auth/authorize-existing/{resource}", json={}, headers={"X-CSRF-Token": old.csrf})
    assert result.status_code == 422 and not fake.silent_requests and manager.active(old)


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_resource_consent_post_requires_session_csrf_origin_and_host(tmp_path, resource, scopes):
    client, manager, fake, old = browser(tmp_path)
    for headers in (
        {}, {"X-CSRF-Token": "forged"}, {"X-CSRF-Token": old.csrf, "Origin": "https://untrusted.test"},
        {"X-CSRF-Token": old.csrf, "Host": "untrusted.test"},
    ):
        assert client.post(f"/auth/consent/{resource}", json={}, headers=headers).status_code in (400, 403)
    client.cookies.clear()
    assert client.post(f"/auth/consent/{resource}", json={}, headers={"X-CSRF-Token": old.csrf}).status_code == 403
    assert len(fake.begin_requests) == 1 and not fake.silent_requests and not manager.pending


@pytest.mark.parametrize("resource", ["iq", "ontology", "graph", ".default", "unreviewed"])
def test_forged_consent_resource_is_rejected_before_sdk_request(tmp_path, resource):
    client, manager, fake, old = browser(tmp_path)
    response = client.post(f"/auth/consent/{resource}", json={}, headers={"X-CSRF-Token": old.csrf})
    assert response.status_code == 401 and response.json()["status"] == "BLOCKED_AUTH"
    assert len(fake.begin_requests) == 1 and not manager.pending


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_absent_optional_resource_remains_blocked_and_never_starts_consent(tmp_path, resource, scopes):
    client, manager, fake, old = browser(tmp_path, configured=False)
    assert all(state["configuration"] == "NOT_CONFIGURED" for state in client.get("/api/health").json()["resource_consent"])
    response = client.post(f"/auth/consent/{resource}", json={}, headers={"X-CSRF-Token": old.csrf})
    assert response.status_code == 401 and len(fake.begin_requests) == 1
    assert not manager.pending and not fake.silent_requests


def test_readiness_never_acquires_tokens_probes_or_starts_consent_even_after_consent_failure(tmp_path):
    client, manager, fake, old = browser(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("Health/readiness cannot acquire tokens, initiate interactive consent or create MSAL clients")
    manager.factory = manager.begin = manager.token = forbidden
    fake.get_accounts = fake.initiate_auth_code_flow = fake.acquire_token_silent_with_error = forbidden
    for _ in range(2):
        for route in ("/api/health", "/api/comparison/readiness"):
            response = client.get(route)
            assert response.status_code == 200
            readiness = response.json().get("readiness", response.json())
            assert readiness["native_source_probe"] == "UNKNOWN_NOT_PROBED"
            assert readiness["application_execution"] == "NOT_ESTABLISHED"
    assert len(fake.begin_requests) == 1 and not fake.silent_requests


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_logout_while_consent_pending_deletes_transaction_and_callback_cannot_restore_session(tmp_path, resource, scopes):
    client, manager, fake, old = browser(tmp_path)
    response = client.post(f"/auth/consent/{resource}", json={}, headers={"X-CSRF-Token": old.csrf})
    state = parse_qs(urlparse(response.json()["authorization_uri"]).query)["state"][0]
    pending_cookie = client.cookies.get("telco_iq_login")
    assert client.post("/auth/logout", json={}, headers={"X-CSRF-Token": old.csrf}).status_code == 200
    client.cookies.set("telco_iq_login", pending_cookie, domain="localhost.local", path="/")
    fake.result["scope"] = " ".join(scopes)
    result = client.get("/auth/callback", params={"code": "synthetic", "state": state}, follow_redirects=False)
    assert result.headers["location"] == "/?auth_error=sign_in_restart"
    assert not manager.sessions and not manager.pending and len(fake.callback_requests) == 1


def test_consent_requires_canonical_callback_origin_and_rejects_duplicate_response_fields(tmp_path):
    client, manager, fake, old = browser(tmp_path)
    response = client.post("/auth/consent/storage", json={}, headers={
        "X-CSRF-Token": old.csrf, "Host": "127.0.0.1:8765",
    })
    assert response.status_code == 401 and not manager.pending
    response = client.post("/auth/consent/storage", json={}, headers={"X-CSRF-Token": old.csrf})
    state = parse_qs(urlparse(response.json()["authorization_uri"]).query)["state"][0]
    result = client.get("/auth/callback", params=[
        ("code", "synthetic"), ("state", state), ("state", "forged"),
    ], follow_redirects=False)
    assert result.headers["location"] == "/?auth_error=sign_in_failed"
    assert len(fake.callback_requests) == 1 and manager.active(old)
    assert not manager.pending


def unit_gateway(reader, session, *, blockers=None):
    sources = {
        alias: SimpleNamespace(dataset_id=dataset) for alias, dataset in reader.config.model_ids.items()
    }
    proof_scope = session.scope().model_copy(update={"models": sorted(sources)})
    def populations(scope):
        assert scope == proof_scope
        return ["synthetic-local-population-validation"]
    gateway = SimpleNamespace(
        enrollment=SimpleNamespace(sources=sources),
        source_proof=SimpleNamespace(scope=proof_scope),
        profiles=SimpleNamespace(private_evidence="synthetic-local-retention-contract"),
        population_records=populations,
        readiness=lambda: {"ready": not blockers, "blockers": blockers or []},
        prepare=lambda *args: pytest.fail("Setup/readiness cannot prepare a provider or perform source execution"),
    )
    def validate(current):
        assert current is session and reader.active(current)
        gateway.population_records(proof_scope)
        checked = gateway.readiness()
        if not checked["ready"] or checked["blockers"]:
            raise LabError("; ".join(checked["blockers"]), Status.BLOCKED_CAPABILITY)
        return proof_scope
    gateway.validate_reader_session = validate
    return gateway


def test_deferred_constructor_and_get_readiness_do_not_load_config_or_authenticate(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Deferred construction/GET cannot read live config, authenticate, acquire tokens or prepare sources")
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", forbidden)
    monkeypatch.setitem(sys.modules, "telco_iq.source_observation", None)
    client, reader, fake, session = browser(
        tmp_path, deferred_live_config=FIXED_LIVE, source_observation_config=FIXED_OBSERVATION,
    )
    reader.factory = reader.token = forbidden
    fake.initiate_auth_code_flow = fake.acquire_token_silent_with_error = forbidden
    for path in ("/api/health", "/api/comparison/readiness"):
        response = client.get(path)
        assert response.status_code == 200
        readiness = response.json().get("readiness", response.json())
        assert not readiness["ready"] and readiness["status"] == "WAITING_CONFIG"
        assert readiness["configuration"] == "WAITING_CONFIG"
        assert readiness["native_source_probe"] == "UNKNOWN_NOT_PROBED"
    run = client.post("/api/compare/stream", json={"question": "Synthetic question before configuration"},
                      headers={"X-CSRF-Token": session.csrf})
    assert run.status_code == 409 and run.json()["ready"] is False
    assert reader.active(session) and not fake.silent_requests


def test_explicit_deferred_configuration_loads_once_and_keeps_exact_session_and_epoch(tmp_path, monkeypatch):
    client, reader, fake, session = browser(tmp_path, deferred_live_config=FIXED_LIVE)
    before = session.key, session.epoch, session.csrf, session.client
    calls = []
    gateway = unit_gateway(reader, session)
    def read(path, current, root):
        assert path == FIXED_LIVE and current is reader and root == ROOT
        calls.append(path)
        return gateway
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", read)
    reader.token = lambda *args, **kwargs: pytest.fail("Local configuration must not acquire a resource token")
    response = client.post("/api/comparison/configure", json={}, headers={"X-CSRF-Token": session.csrf})
    assert response.status_code == 200
    assert response.json()["status"] == "CONFIGURED_NOT_PROBED" and response.json()["ready"] is False
    assert reader.session(client.cookies.get("telco_iq_session")) is session
    assert (session.key, session.epoch, session.csrf, session.client) == before
    readiness = client.get("/api/comparison/readiness").json()
    assert readiness["ready"] and readiness["status"] == "READY_FOR_CURRENT_READER_PROBE"
    assert readiness["native_source_probe"] == "UNKNOWN_NOT_PROBED"
    assert readiness["application_execution"] == "NOT_ESTABLISHED"
    assert readiness["demonstration"] == "NOT_ESTABLISHED"
    assert client.post("/api/comparison/configure", json={}, headers={"X-CSRF-Token": session.csrf}).status_code == 409
    assert calls == [FIXED_LIVE] and not fake.silent_requests


@pytest.mark.parametrize("phase", ["prepare", "run"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("cause,status,logged", [
    (TimeoutError("PRIVATE_TEST_TIMEOUT"), "TIMED_OUT", False),
    (httpx.ReadTimeout("PRIVATE_TEST_MCP_READ_TIMEOUT"), "TIMED_OUT", False),
    (httpx.ConnectTimeout("PRIVATE_TEST_MCP_CONNECT_TIMEOUT"), "TIMED_OUT", False),
    (LabError("PRIVATE_TEST_LIMIT", Status.PARTIAL), "PARTIAL", False),
    (LabError("PRIVATE_TEST_AUTH", Status.BLOCKED_AUTH), "BLOCKED_AUTH", False),
    (LabError("PRIVATE_TEST_CAPABILITY", Status.BLOCKED_CAPABILITY), "BLOCKED_CAPABILITY", False),
    (ValueError("PRIVATE_TEST_UNKNOWN"), "FAILED", True),
    (ExceptionGroup("PRIVATE_TEST_MIXED", [
        TimeoutError("PRIVATE_TEST_TIMEOUT"), ValueError("PRIVATE_TEST_UNKNOWN"),
    ]), "FAILED", True),
])
def test_comparison_stream_preserves_known_nested_sdk_failure_status_without_private_detail(
    tmp_path, monkeypatch, caplog, phase, cause, status, logged, nested,
):
    client, reader, fake, session = browser(tmp_path, deferred_live_config=FIXED_LIVE)
    gateway = unit_gateway(reader, session)
    failure = ExceptionGroup("PRIVATE_TEST_OUTER", [ExceptionGroup("PRIVATE_TEST_INNER", [cause])]) if nested else cause

    class FailingComparison:
        async def run(self, *args, **kwargs):
            raise failure

    @asynccontextmanager
    async def prepare(current):
        assert current is session
        if phase == "prepare":
            raise failure
        yield FailingComparison()

    gateway.prepare = prepare
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", lambda *args: gateway)
    configured = client.post("/api/comparison/configure", json={}, headers={"X-CSRF-Token": session.csrf})
    assert configured.status_code == 200
    response = client.post(
        "/api/compare/stream", json={"question": "Synthetic nested failure, no model/source call"},
        headers={"X-CSRF-Token": session.csrf},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [frame for frame in response.text.split("\n\n") if frame.strip()]
    assert len(frames) == 1 and frames[0].startswith("event: error\n")
    body = json.loads(frames[0].split("data: ", 1)[1])
    assert body["status"] == status and body["detail"]
    if nested or not isinstance(cause, LabError):
        assert "PRIVATE_TEST" not in response.text
    else:
        assert body["detail"] == str(cause)
    assert "event: comparison" not in response.text
    assert reader.active(session) and not fake.silent_requests
    assert any(record.message == "Live comparison stream failed" for record in caplog.records) == logged


@pytest.mark.parametrize("authorization_lost", [False, True])
def test_failed_comparison_retains_diagnostic_plan_and_events_only_while_authorized(
    tmp_path, monkeypatch, authorization_lost,
):
    from telco_iq.comparison import PairEvent
    from telco_iq.contracts import Plan

    client, reader, _, session = browser(tmp_path, deferred_live_config=FIXED_LIVE)
    gateway = unit_gateway(reader, session)
    saved = []
    proposal = Plan(
        request_id="synthetic-failure-evidence", question="Synthetic failure evidence",
        principal_scope=session.scope(), as_of="2026-10-20T00:00:00Z",
        ontology_revision="SYNTHETIC", metric_selections=[],
        nodes=[{"id": "metadata", "kind": "ReadModelSchema", "references": []}],
        result_node="metadata",
    )

    class FailingComparison:
        async def run(self, question, *, on_event=None):
            await on_event(PairEvent(
                pair_id="SYNTHETIC_PAIR", arm="baseline", phase="plan_ready", plan=proposal,
            ))
            if authorization_lost:
                raise AuthError("Synthetic source authorization ended")
            raise TimeoutError("PRIVATE_TIMEOUT_DETAIL")

    @asynccontextmanager
    async def prepare(current):
        yield FailingComparison()
    gateway.prepare = prepare
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", lambda *args: gateway)
    monkeypatch.setattr("telco_iq.api.write_receipt", lambda directory, value: saved.append(value))
    assert client.post(
        "/api/comparison/configure", json={}, headers={"X-CSRF-Token": session.csrf},
    ).status_code == 200
    response = client.post(
        "/api/compare/stream", json={"question": proposal.question}, headers={"X-CSRF-Token": session.csrf},
    )
    assert "event: comparison" not in response.text and "event: error" in response.text
    assert "PRIVATE_TIMEOUT_DETAIL" not in response.text
    failure = next(value for value in saved if value.get("kind") == "ACTUAL_APPLICATION_PAIRED_FAILURE")
    assert failure["diagnostic_only_not_accepted_answer_or_executable_reader_grant"] is True
    if authorization_lost:
        assert failure["status"] == "BLOCKED_AUTH" and failure["diagnostic_events"] == []
    else:
        assert failure["status"] == "TIMED_OUT"
        assert len(failure["diagnostic_events"]) == 1
        assert failure["diagnostic_events"][0]["plan"] == proposal.model_dump(mode="json")
        assert "outcome" not in failure["diagnostic_events"][0]


@pytest.mark.parametrize("field,value", [
    ("principal", "other-approved-but-not-current"), ("tenant", "wrong-tenant"),
    ("authorization_revision", "previous-epoch"), ("models", []),
])
def test_deferred_configuration_rejects_wrong_proof_identity_or_epoch(tmp_path, monkeypatch, field, value):
    client, reader, _, session = browser(tmp_path, deferred_live_config=FIXED_LIVE)
    gateway = unit_gateway(reader, session)
    gateway.source_proof.scope = gateway.source_proof.scope.model_copy(update={field: value})
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", lambda *args: gateway)
    response = client.post("/api/comparison/configure", json={}, headers={"X-CSRF-Token": session.csrf})
    assert response.status_code == 401 and response.json()["status"] == "BLOCKED_AUTH"
    assert client.get("/api/comparison/readiness").json()["status"] == "WAITING_CONFIG"
    assert reader.active(session)


@pytest.mark.parametrize("failure", ["missing-file", "invalid-file", "missing-proof", "wrong-model", "approval", "profile"])
def test_deferred_fixed_config_failure_is_honestly_blocked_without_private_detail(tmp_path, monkeypatch, failure):
    client, reader, _, session = browser(tmp_path, deferred_live_config=FIXED_LIVE)
    gateway = unit_gateway(reader, session)
    def read(*args):
        if failure == "missing-file":
            raise FileNotFoundError("private-unit-path must never enter the API")
        if failure == "invalid-file":
            raise ValueError("private-unit-path must never enter the API")
        if failure == "missing-proof":
            gateway.source_proof = None
        if failure == "wrong-model":
            gateway.enrollment.sources["subscriptions"].dataset_id = "wrong-model"
        if failure == "approval":
            gateway.readiness = lambda: {"ready": False, "blockers": ["Approval expired or has no timezone"]}
        if failure == "profile":
            gateway.readiness = lambda: {"ready": False, "blockers": ["private-unit-path must never enter the API"]}
        return gateway
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", read)
    response = client.post("/api/comparison/configure", json={}, headers={"X-CSRF-Token": session.csrf})
    assert response.status_code == 409 and response.json()["status"] == "BLOCKED_CAPABILITY"
    assert "private-unit-path" not in response.text
    assert client.get("/api/comparison/readiness").json()["status"] == "WAITING_CONFIG"
    assert reader.active(session)


@pytest.mark.parametrize("route", ["/api/comparison/configure", "/api/source-observations/capture"])
def test_setup_posts_require_session_csrf_and_reject_client_paths_and_urls(tmp_path, monkeypatch, route):
    client, reader, _, session = browser(
        tmp_path, deferred_live_config=FIXED_LIVE, source_observation_config=FIXED_OBSERVATION,
    )
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", lambda *args: pytest.fail("Unexpected live file read"))
    monkeypatch.setitem(sys.modules, "telco_iq.source_observation", None)
    assert client.post(route, json={}).status_code == 403
    assert client.post(route, json={}, headers={"X-CSRF-Token": "forged"}).status_code == 403
    headers = {"X-CSRF-Token": session.csrf}
    for value in ({"path": "C:\\private\\unapproved.json"}, {"url": "https://unapproved.test"}, [], "unapproved"):
        assert client.post(route, json=value, headers=headers).status_code == 400
    assert client.post(route + "?config_file=unapproved", json={}, headers=headers).status_code == 400
    assert reader.active(session)
    reader.revoke(session.key)
    assert client.post(route, json={}, headers=headers).status_code == 403


def test_logout_during_local_population_validation_cannot_install_gateway(tmp_path, monkeypatch):
    client, reader, _, session = browser(tmp_path, deferred_live_config=FIXED_LIVE)
    gateway = unit_gateway(reader, session)
    paused, release = threading.Event(), threading.Event()
    results = []
    populations = gateway.population_records
    def validate(scope):
        paused.set()
        assert release.wait(10)
        return populations(scope)
    gateway.population_records = validate
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", lambda *args: gateway)
    with client:
        thread = threading.Thread(target=lambda: results.append(client.post(
            "/api/comparison/configure", json={}, headers={"X-CSRF-Token": session.csrf},
        )))
        thread.start()
        try:
            assert paused.wait(10)
            reader.revoke(session.key)
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive() and results[0].status_code == 401
        assert client.get("/api/comparison/readiness").json()["status"] == "WAITING_CONFIG"
    assert not reader.active(session)


def test_concurrent_setup_requests_install_only_once_without_reloading(tmp_path, monkeypatch):
    client, reader, _, session = browser(tmp_path, deferred_live_config=FIXED_LIVE)
    gateway = unit_gateway(reader, session)
    paused, release = threading.Event(), threading.Event()
    results, calls = [], []
    def read(*args):
        calls.append(args[0])
        paused.set()
        assert release.wait(10)
        return gateway
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", read)
    def configure():
        results.append(client.post("/api/comparison/configure", json={}, headers={"X-CSRF-Token": session.csrf}))
    with client:
        first, second = threading.Thread(target=configure), threading.Thread(target=configure)
        first.start()
        try:
            assert paused.wait(10)
            second.start()
        finally:
            release.set()
            first.join(10)
            if second.ident is not None:
                second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert sorted(response.status_code for response in results) == [200, 409]
    assert calls == [FIXED_LIVE] and reader.active(session)


def test_configured_deferred_gateway_cannot_be_used_by_a_replacement_reader_session(tmp_path, monkeypatch):
    client, reader, _, session = browser(tmp_path, deferred_live_config=FIXED_LIVE)
    gateway = unit_gateway(reader, session)
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", lambda *args: gateway)
    assert client.post("/api/comparison/configure", json={}, headers={"X-CSRF-Token": session.csrf}).status_code == 200
    gateway.readiness = lambda: pytest.fail("A replacement reader must be rejected before file/proof validation")
    # The existing explicit initial login replaces the reader, but must not reload session-bound source proof.
    login = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    client.get("/auth/callback", params={"code": "synthetic", "state": state}, follow_redirects=False)
    new = reader.session(client.cookies.get("telco_iq_session"))
    assert new is not session and new.epoch != session.epoch
    readiness = client.get("/api/comparison/readiness").json()
    assert readiness["ready"] is False and readiness["status"] == "BLOCKED_AUTH"
    response = client.post("/api/compare/stream", json={"question": "Synthetic replacement reader"},
                           headers={"X-CSRF-Token": new.csrf})
    assert response.status_code == 409 and response.json()["status"] == "BLOCKED_AUTH"


def test_missing_fixed_configuration_can_be_retried_only_at_the_same_server_path(tmp_path, monkeypatch):
    client, reader, _, session = browser(tmp_path, deferred_live_config=FIXED_LIVE)
    gateway = unit_gateway(reader, session)
    calls = []
    def read(path, *args):
        calls.append(path)
        if len(calls) == 1:
            raise FileNotFoundError("synthetic missing configuration, no actual file accessed")
        return gateway
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", read)
    headers = {"X-CSRF-Token": session.csrf}
    assert client.post("/api/comparison/configure", json={}, headers=headers).status_code == 409
    assert client.get("/api/comparison/readiness").json()["status"] == "WAITING_CONFIG"
    assert client.post("/api/comparison/configure", json={"path": "unapproved"}, headers=headers).status_code == 400
    assert client.post("/api/comparison/configure", json={}, headers=headers).status_code == 200
    assert client.post("/api/comparison/configure", json={}, headers=headers).status_code == 409
    assert calls == [FIXED_LIVE, FIXED_LIVE] and reader.active(session)


@pytest.mark.parametrize("change", ["epoch", "object", "tenant", "models"])
def test_installed_scope_is_checked_cheaply_before_any_local_readiness_validation(tmp_path, monkeypatch, change):
    client, reader, _, session = browser(tmp_path, deferred_live_config=FIXED_LIVE)
    gateway = unit_gateway(reader, session)
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", lambda *args: gateway)
    headers = {"X-CSRF-Token": session.csrf}
    assert client.post("/api/comparison/configure", json={}, headers=headers).status_code == 200
    gateway.readiness = lambda: pytest.fail("Changed reader scope must be blocked before any retained-file validation")
    if change == "epoch":
        session.epoch = "synthetic-changed-epoch"
    elif change == "object":
        session.object_id = "synthetic-other-object"
    elif change == "tenant":
        session.tenant_id = "synthetic-other-tenant"
    else:
        reader.config.model_ids["subscriptions"] = "synthetic-other-model"
    readiness = client.get("/api/comparison/readiness").json()
    assert readiness["ready"] is False and readiness["status"] == "BLOCKED_AUTH"
    assert client.post("/api/comparison/configure", json={}, headers=headers).status_code == 409


def test_capture_epoch_a_then_consent_epoch_b_cannot_configure_or_rebind_old_proof(tmp_path, monkeypatch):
    client, reader, fake, old = browser(
        tmp_path, deferred_live_config=FIXED_LIVE, source_observation_config=FIXED_OBSERVATION,
    )
    gateway = unit_gateway(reader, old)
    captured = gateway.source_proof.scope
    class Service:
        def __init__(self, *args):
            pass
        async def capture(self, current):
            assert current is old and current.epoch == captured.authorization_revision
            return {"kind": "SOURCE_METADATA_CAPTURE_ONLY", "ready": False, "retained_hashes": ["a" * 64]}
    install_capture_service(monkeypatch, Service)
    headers = {"X-CSRF-Token": old.csrf}
    assert client.post("/api/source-observations/capture", json={}, headers=headers).status_code == 200
    consent = client.post("/auth/consent/storage", json={}, headers=headers)
    state = parse_qs(urlparse(consent.json()["authorization_uri"]).query)["state"][0]
    fake.result["scope"] = " ".join(STORAGE_SCOPES)
    client.get("/auth/callback", params={"code": "synthetic", "state": state}, follow_redirects=False)
    new = reader.session(client.cookies.get("telco_iq_session"))
    assert new.epoch != old.epoch and not reader.active(old) and reader.active(new)
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", lambda *args: gateway)
    response = client.post("/api/comparison/configure", json={}, headers={"X-CSRF-Token": new.csrf})
    assert response.status_code == 401 and response.json()["status"] == "BLOCKED_AUTH"
    assert gateway.source_proof.scope is captured
    assert client.get("/api/comparison/readiness").json()["status"] == "WAITING_CONFIG"


def install_capture_service(monkeypatch, constructor):
    module = ModuleType("telco_iq.source_observation")
    module.SourceMetadataCaptureService = constructor
    monkeypatch.setitem(sys.modules, "telco_iq.source_observation", module)


@pytest.mark.parametrize("result_kind", ["SOURCE_METADATA_CAPTURE_ONLY", "SOURCE_QUERY_CALIBRATION_CAPTURE_ONLY"])
def test_fixed_source_capture_only_delegates_known_service_and_does_not_configure_gateway(
    tmp_path, monkeypatch, result_kind,
):
    client, reader, fake, session = browser(
        tmp_path, deferred_live_config=FIXED_LIVE, source_observation_config=FIXED_OBSERVATION,
    )
    calls = []
    class Service:
        def __init__(self, current, path, root):
            assert current is reader and path == FIXED_OBSERVATION and root == ROOT
            calls.append("local-constructor")
        async def capture(self, current):
            assert current is session
            calls.append("approved-service-capture")
            return {"kind": result_kind, "ready": False, "blockers": []}
    install_capture_service(monkeypatch, Service)
    before = session.key, session.epoch, session.csrf
    response = client.post("/api/source-observations/capture", json={}, headers={"X-CSRF-Token": session.csrf})
    assert response.status_code == 200 and response.json()["kind"] == result_kind
    assert response.json()["ready"] is False
    assert calls == ["local-constructor", "approved-service-capture"]
    assert (session.key, session.epoch, session.csrf) == before
    assert client.get("/api/comparison/readiness").json()["status"] == "WAITING_CONFIG"
    assert not fake.silent_requests


@pytest.mark.parametrize("failure", [
    "missing-module", "missing-config", "invalid-config", "service-blocked", "wrong-result", "query-grant",
])
def test_source_capture_unavailable_or_invalid_is_explicitly_blocked_without_fallback(tmp_path, monkeypatch, failure):
    client, reader, _, session = browser(tmp_path, source_observation_config=FIXED_OBSERVATION)
    class Service:
        def __init__(self, *args):
            if failure == "missing-config":
                raise FileNotFoundError("private-unit-observation-config")
            if failure == "invalid-config":
                raise ValueError("private-unit-observation-config")
        async def capture(self, current):
            if failure == "service-blocked":
                raise LabError("private-unit-observation-config", Status.BLOCKED_AUTH)
            if failure == "query-grant":
                return {"kind": "SOURCE_QUERY_CALIBRATION_CAPTURE_ONLY", "ready": True}
            return {"kind": "OBSERVED_SOURCE_STATE", "ready": True}
    if failure == "missing-module":
        monkeypatch.setitem(sys.modules, "telco_iq.source_observation", None)
    else:
        install_capture_service(monkeypatch, Service)
    response = client.post("/api/source-observations/capture", json={}, headers={"X-CSRF-Token": session.csrf})
    assert response.status_code == 409 and response.json()["ready"] is False
    assert response.json()["status"] == ("BLOCKED_AUTH" if failure == "service-blocked" else "BLOCKED_CAPABILITY")
    assert "private-unit-observation-config" not in response.text
    assert reader.active(session)


def test_source_capture_rechecks_session_after_service_returns(tmp_path, monkeypatch):
    client, reader, _, session = browser(tmp_path, source_observation_config=FIXED_OBSERVATION)
    class Service:
        def __init__(self, *args):
            pass
        async def capture(self, current):
            reader.revoke(current.key)
            return {"kind": "SOURCE_METADATA_CAPTURE_ONLY", "ready": False}
    install_capture_service(monkeypatch, Service)
    response = client.post("/api/source-observations/capture", json={}, headers={"X-CSRF-Token": session.csrf})
    assert response.status_code == 401 and response.json()["status"] == "BLOCKED_AUTH"


def test_disabled_setup_seams_never_import_or_read_configuration(tmp_path, monkeypatch):
    client, reader, _, session = browser(tmp_path)
    monkeypatch.setattr("telco_iq.api.ConfiguredLiveGateway.read", lambda *args: pytest.fail("Unexpected file read"))
    monkeypatch.setitem(sys.modules, "telco_iq.source_observation", None)
    for path in ("/api/comparison/configure", "/api/source-observations/capture"):
        response = client.post(path, json={}, headers={"X-CSRF-Token": session.csrf})
        assert response.status_code == 409 and response.json()["status"] == "BLOCKED_CAPABILITY"
    assert reader.active(session)


def test_setup_paths_must_be_private_and_reader_bound(tmp_path):
    fake = FakeMsal(config(tmp_path))
    reader = ReaderAuth(fake.cfg, lambda _: fake)
    for field in ("deferred_live_config", "source_observation_config"):
        with pytest.raises(ValueError, match="require reader"):
            create_app(ROOT, **{field: FIXED_LIVE})
        with pytest.raises(LabError, match="outside repository"):
            create_app(ROOT, reader_auth=reader, **{field: ROOT / "unapproved-config.json"})
    with pytest.raises(ValueError, match="cannot be replaced"):
        create_app(ROOT, reader_auth=reader, live_demo=SimpleNamespace(), deferred_live_config=FIXED_LIVE)


@pytest.mark.parametrize("flags", [
    ["--defer-live-config"], ["--defer-live-config", "--reader-config", "unit-reader"],
    ["--defer-live-config", "--live-config", "unit-live"], ["--source-observation-config", "unit-observation"],
])
def test_cli_rejects_unbound_setup_options_before_reading_files(monkeypatch, flags):
    from telco_iq import cli

    monkeypatch.setattr(sys, "argv", ["telco-iq", "serve", *flags])
    monkeypatch.setattr(cli.ReaderConfig, "read", lambda *args: pytest.fail("CLI must reject flags before file reads"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


@pytest.mark.parametrize("deferred", [False, True])
def test_cli_retains_eager_default_and_wires_explicit_fixed_setup_paths(tmp_path, monkeypatch, deferred):
    from telco_iq import cli

    flags = ["--reader-config", "unit-reader", "--live-config", str(FIXED_LIVE),
             "--source-observation-config", str(FIXED_OBSERVATION)]
    if deferred:
        flags.append("--defer-live-config")
    monkeypatch.setattr(sys, "argv", ["telco-iq", "serve", *flags])
    monkeypatch.setattr(cli.ReaderConfig, "read", lambda *args: config(tmp_path))
    loads, apps, runs = [], [], []
    gateway = object()
    def read(*args):
        loads.append(args)
        return gateway
    def app(*args, **kwargs):
        apps.append((args, kwargs))
        return "unit-app-no-server-start"
    monkeypatch.setattr(cli.ConfiguredLiveGateway, "read", read)
    monkeypatch.setattr(cli, "create_app", app)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: runs.append((args, kwargs)))
    cli.main()
    assert len(loads) == (0 if deferred else 1)
    assert apps[0][0][5] is (None if deferred else gateway)
    assert apps[0][1]["deferred_live_config"] == (FIXED_LIVE if deferred else None)
    assert apps[0][1]["source_observation_config"] == FIXED_OBSERVATION
    assert runs[0][1]["access_log"] is False and runs[0][1]["host"] == "127.0.0.1"
