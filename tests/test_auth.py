import base64
import copy
import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from telco_iq.api import create_app
from telco_iq.auth import (
    POWER_BI_SCOPES,
    SOURCE_METADATA_SCOPES,
    STORAGE_SCOPES,
    AuthError,
    OntologyResourceConfig,
    ReaderAuth,
    ReaderConfig,
    SourceMetadataResourceConfig,
    StorageResourceConfig,
    dpapi,
)

ROOT = Path(__file__).resolve().parents[1]
TENANT = "00000000-0000-0000-0000-000000000001"
CLIENT = "00000000-0000-0000-0000-000000000002"
USER = "00000000-0000-0000-0000-000000000003"
OTHER = "00000000-0000-0000-0000-000000000004"


def config(tmp_path):
    return ReaderConfig(
        tenant_id=TENANT, client_id=CLIENT, allowed_object_ids=[USER, OTHER],
        certificate_file=tmp_path / "key.dpapi", certificate_thumbprint="a" * 40,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        model_ids={"subscriptions": "00000000-0000-0000-0000-000000000005"},
        receipt_directory=tmp_path / "receipts",
    )


class FakeMsal:
    def __init__(self, cfg, oid=USER):
        self.cfg, self.oid = cfg, oid
        self.accounts = [{"local_account_id": oid, "realm": TENANT}]
        self.claims = {
            "tid": TENANT, "oid": oid, "aud": CLIENT, "exp": time.time() + 3600,
            "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "nonce": "sdk-validated-test-nonce",
        }
        self.result = {
            "access_token": "synthetic-unit-token", "scope": " ".join(POWER_BI_SCOPES),
            "id_token_claims": self.claims,
        }
        self.last_flow = None
        self.begin_requests = []
        self.callback_requests = []
        self.silent_requests = []

    def initiate_auth_code_flow(self, scopes, **kwargs):
        state = secrets.token_urlsafe(16)
        nonce = secrets.token_urlsafe(16)
        verifier = "v" * 43
        decorated = list(scopes) + ["openid", "profile", "offline_access"]
        self.begin_requests.append((scopes, kwargs))
        self.last_flow = {
            "state": state, "code_verifier": verifier, "nonce": nonce,
            "scope": decorated, "redirect_uri": kwargs["redirect_uri"],
            "auth_uri": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?" + urlencode({
                "state": state, "code_challenge": base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode("ascii")).digest(),
                ).rstrip(b"=").decode("ascii"), "code_challenge_method": "S256",
                "redirect_uri": kwargs["redirect_uri"], "client_id": CLIENT,
                "scope": " ".join(decorated), "response_type": "code", "response_mode": "query",
                "prompt": kwargs["prompt"], "nonce": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
            }),
        }
        return self.last_flow

    def acquire_token_by_auth_code_flow(self, flow, response):
        self.callback_requests.append((flow, response))
        if response.get("unit_nonce_failure"):
            raise ValueError("sensitive upstream state must not leak")
        if response.get("unit_runtime_nonce_failure"):
            raise RuntimeError("sensitive upstream nonce must not leak")
        result = copy.deepcopy(self.result)
        if result.get("id_token_claims", {}).get("nonce") == "sdk-validated-test-nonce":
            result["id_token_claims"]["nonce"] = hashlib.sha256(flow["nonce"].encode("ascii")).hexdigest()
        return result

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent_with_error(self, scopes, account, **kwargs):
        self.silent_requests.append((scopes, account))
        return copy.deepcopy(self.result)


def signed_in(manager):
    key, uri = manager.begin(None)
    state = parse_qs(urlparse(uri).query)["state"][0]
    return manager.finish(key, {"code": "synthetic-code", "state": state})


def test_dpapi_uses_current_user_os_protection():
    plain = b"synthetic unit-test material, not a credential"
    protected = dpapi(plain, decrypt=False)
    assert protected != plain
    assert dpapi(protected, decrypt=True) == plain


def test_cookie_flow_is_single_use_and_keeps_tokens_server_side(tmp_path):
    manager = ReaderAuth(config(tmp_path), FakeMsal)
    key, uri = manager.begin(None)
    state = parse_qs(urlparse(uri).query)["state"][0]
    session = manager.finish(key, {"code": "synthetic", "state": state})
    assert session.key != key
    assert manager.token(session) == "synthetic-unit-token"
    assert session.scope().models == []
    assert "synthetic-unit-token" not in repr(session)
    with pytest.raises(AuthError, match="already used"):
        manager.finish(key, {"code": "synthetic", "state": state})


@pytest.mark.parametrize("field,value", [
    ("tid", OTHER), ("oid", "00000000-0000-0000-0000-000000000099"),
    ("aud", OTHER), ("iss", "https://untrusted.test"),
    ("exp", 1), ("nonce", None),
])
def test_wrong_tenant_guest_audience_issuer_expiry_rejected(tmp_path, field, value):
    fake = FakeMsal(config(tmp_path))
    fake.claims[field] = value
    manager = ReaderAuth(config(tmp_path), lambda _: fake)
    with pytest.raises(AuthError, match="immutable DEV identity"):
        signed_in(manager)
    assert not manager.sessions


def test_state_and_nonce_failures_are_explicit_without_sensitive_detail(tmp_path):
    manager = ReaderAuth(config(tmp_path), FakeMsal)
    key, _ = manager.begin(None)
    with pytest.raises(AuthError, match="response rejected"):
        manager.finish(key, {"code": "sensitive", "state": "wrong"})
    key, uri = manager.begin(None)
    state = parse_qs(urlparse(uri).query)["state"][0]
    with pytest.raises(AuthError) as error:
        manager.finish(key, {"code": "sensitive", "state": state, "unit_nonce_failure": "yes"})
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize("failure,expected_code", [
    ("expired", "TRANSACTION_EXPIRED"), ("missing", "TRANSACTION_MISSING"),
    ("state", "STATE_REJECTED"),
])
def test_signin_transaction_failures_are_distinct_from_identity_failure(tmp_path, failure, expected_code):
    fake = FakeMsal(config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    key, uri = manager.begin(None)
    state = parse_qs(urlparse(uri).query)["state"][0]
    if failure == "expired":
        manager.pending[key].expires_at = time.time() - 1
    elif failure == "missing":
        manager.cancel_pending(key)
    else:
        state = "synthetic-other-signin-tab"
    with pytest.raises(AuthError) as error:
        manager.finish(key, {"code": "never-log-code-material", "state": state})
    assert error.value.code == expected_code
    assert not manager.sessions and not manager.pending and not fake.callback_requests
    assert "never-log" not in str(error.value)


@pytest.mark.parametrize("failure,public_reason", [
    ("expired", "sign_in_expired"), ("missing", "sign_in_restart"), ("state", "sign_in_restart"),
])
def test_callback_reports_only_fixed_transaction_reason_without_claiming_wrong_identity(
    tmp_path, caplog, failure, public_reason,
):
    fake = FakeMsal(config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    client = TestClient(create_app(ROOT, reader_auth=manager), base_url="http://localhost:8765")
    login = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    pending_key = client.cookies["telco_iq_login"]
    if failure == "expired":
        manager.pending[pending_key].expires_at = time.time() - 1
    elif failure == "missing":
        manager.cancel_pending(pending_key)
    else:
        state = "synthetic-other-tab"
    response = client.get(
        "/auth/callback", params={"code": "never-log-code-material", "state": state}, follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/?auth_error=" + public_reason
    assert "IDENTITY_REJECTED" not in caplog.text and "never-log" not in caplog.text
    assert not manager.sessions and not manager.pending and not fake.callback_requests
    assert "telco_iq_login=" in response.headers["set-cookie"]


def test_scopes_do_not_accept_another_resource_or_silent_escalation(tmp_path):
    fake = FakeMsal(config(tmp_path))
    fake.result["scope"] = " ".join(s.replace("analysis.windows.net/powerbi/api", "graph.microsoft.com")
                                    for s in POWER_BI_SCOPES)
    manager = ReaderAuth(config(tmp_path), lambda _: fake)
    with pytest.raises(AuthError, match="consent"):
        signed_in(manager)


def test_cache_account_and_logout_are_bound_to_immutable_user(tmp_path):
    fake = FakeMsal(config(tmp_path))
    manager = ReaderAuth(config(tmp_path), lambda _: fake)
    session = signed_in(manager)
    fake.accounts = [{"local_account_id": OTHER, "realm": TENANT}]
    with pytest.raises(AuthError, match="cache account"):
        manager.token(session)
    fake.accounts = [{"local_account_id": USER, "realm": TENANT}]
    manager.revoke(session.key)
    with pytest.raises(AuthError, match="session has ended"):
        manager.token(session)


def test_msal_scope_filtered_cache_hit_does_not_require_omitted_scope_field(tmp_path):
    fake = FakeMsal(config(tmp_path))
    manager = ReaderAuth(config(tmp_path), lambda _: fake)
    session = signed_in(manager)
    fake.result = {
        "access_token": "synthetic-unit-token", "token_type": "Bearer",
        "expires_in": 3600, "token_source": "cache",
    }
    assert manager.token(session) == "synthetic-unit-token"
    with pytest.raises(AuthError, match="consent"):
        manager._scopes(fake.result)
    fake.result["scope"] = "Dataset.Read.All"
    with pytest.raises(AuthError, match="consent"):
        manager.token(session)
    fake.result.pop("scope")
    fake.result["token_source"] = "identity_provider"
    with pytest.raises(AuthError, match="consent"):
        manager.token(session)
    fake.result["token_source"] = "cache"
    with pytest.raises(AuthError, match="consent"):
        signed_in(manager)


def test_switching_users_invalidates_old_session_and_csrf(tmp_path):
    clients = iter([FakeMsal(config(tmp_path), USER), FakeMsal(config(tmp_path), OTHER)])
    manager = ReaderAuth(config(tmp_path), lambda _: next(clients))
    first = signed_in(manager)
    key, uri = manager.begin(first.key)
    assert not manager.active(first)
    second = manager.finish(key, {"code": "synthetic", "state": parse_qs(urlparse(uri).query)["state"][0]})
    assert second.object_id == OTHER
    with pytest.raises(AuthError, match="CSRF"):
        manager.csrf(second, first.csrf)


def test_actual_routes_require_session_csrf_and_canonical_callback_origin(tmp_path):
    manager = ReaderAuth(config(tmp_path), FakeMsal)
    client = TestClient(create_app(ROOT, reader_auth=manager), base_url="http://localhost:8765")
    assert client.get("/api/health").json()["authenticated"] is False
    assert client.get("/api/catalog").status_code == 401
    assert client.post("/api/mcp/inventory", json={}).status_code == 403
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 303
    assert "httponly" in response.headers["set-cookie"].lower()
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    callback = client.get("/auth/callback", params={"code": "synthetic", "state": state}, follow_redirects=False)
    assert callback.status_code == 303 and callback.headers["location"] == "/"
    health = client.get("/api/health").json()
    assert health["authenticated"] is True
    assert "access_token" not in str(health)
    assert client.post("/auth/logout", json={}).status_code == 403
    assert client.post("/auth/logout", json={}, headers={"X-CSRF-Token": health["csrf_token"]}).status_code == 200
    assert client.get("/api/health").json()["authenticated"] is False
    rejected = client.get("/auth/callback?code=never-log-this&state=wrong", follow_redirects=False)
    assert "never-log-this" not in rejected.headers["location"]


def test_ip_origin_redirects_before_creating_login_transaction(tmp_path):
    manager = ReaderAuth(config(tmp_path), FakeMsal)
    client = TestClient(create_app(ROOT, reader_auth=manager), base_url="http://127.0.0.1:8765")
    response = client.get("/auth/login", follow_redirects=False)
    assert response.headers["location"] == "http://localhost:8765/auth/login"
    assert not manager.pending


def test_auth_config_never_reads_private_material_from_repository(tmp_path):
    config_file = ROOT / "config-does-not-exist.json"
    with pytest.raises(Exception, match="outside repository"):
        ReaderConfig.read(config_file, ROOT)


def ontology_config(tmp_path):
    cfg = config(tmp_path)
    cfg.ontology_resource = OntologyResourceConfig(
        scopes=["https://api.fabric.microsoft.com/Item.Read.All"],
        challenge_evidence_hash="a" * 64,
        review_reference="Synthetic challenge scope for this test ONLY, not an observed ontology permission",
    )
    return cfg


def test_ontology_default_is_blocked_before_token_acquisition(tmp_path):
    fake = FakeMsal(config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    session = signed_in(manager)
    with pytest.raises(AuthError, match="Ontology resource is not configured"):
        manager.token(session, resource="ontology")
    assert fake.silent_requests == []
    assert manager.token(session) == "synthetic-unit-token"
    assert fake.silent_requests[0][0] == list(POWER_BI_SCOPES)


def test_explicit_ontology_resource_uses_only_reviewed_fabric_scopes(tmp_path):
    fake = FakeMsal(ontology_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    session = signed_in(manager)
    fake.result["scope"] = "https://api.fabric.microsoft.com/Item.Read.All"
    fake.result["access_token"] = "synthetic-fabric-token"
    assert manager.token(session, resource="ontology") == "synthetic-fabric-token"
    assert fake.silent_requests[0][0] == ["https://api.fabric.microsoft.com/Item.Read.All"]
    assert fake.silent_requests[0][1] == fake.accounts[0]
    with pytest.raises(AuthError, match="consent"):
        manager.token(session)


@pytest.mark.parametrize("scope", [
    "https://analysis.windows.net/powerbi/api/Item.Read.All",
    "https://graph.microsoft.com/Item.Read.All",
    "Item.Read.All https://graph.microsoft.com/User.Read",
    "https://api.fabric.microsoft.com/Other.Read",
])
def test_ontology_rejects_wrong_resource_scopes_even_same_permission_name(tmp_path, scope):
    fake = FakeMsal(ontology_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    session = signed_in(manager)
    fake.result["scope"] = scope
    with pytest.raises(AuthError, match="consent"):
        manager.token(session, resource="ontology")


@pytest.mark.parametrize("scope", [
    "https://api.fabric.microsoft.com/.default", "Item.Read.All",
    "https://analysis.windows.net/powerbi/api/Item.Read.All",
    "https://api.fabric.microsoft.com/Item.Read.All scope",
])
def test_ontology_configuration_never_infers_default_or_mixed_audiences(scope):
    with pytest.raises(ValidationError, match="Explicit Fabric delegated"):
        OntologyResourceConfig(scopes=[scope], challenge_evidence_hash="a" * 64, review_reference="synthetic")


def test_resource_cache_hit_retains_msal_scope_filtered_semantics(tmp_path):
    fake = FakeMsal(ontology_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    session = signed_in(manager)
    fake.result = {"access_token": "synthetic-resource-cache-token", "token_source": "cache"}
    assert manager.token(session, resource="ontology") == "synthetic-resource-cache-token"
    assert fake.silent_requests[-1][0] == ["https://api.fabric.microsoft.com/Item.Read.All"]
    fake.result["token_source"] = "identity_provider"
    with pytest.raises(AuthError, match="consent"):
        manager.token(session, resource="ontology")


def test_missing_ontology_consent_does_not_start_interactive_or_change_registration(tmp_path):
    fake = FakeMsal(ontology_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    session = signed_in(manager)
    original_flow = fake.last_flow
    fake.result = {"error": "consent_required"}
    with pytest.raises(AuthError, match="consent_required"):
        manager.token(session, resource="ontology")
    assert fake.last_flow is original_flow and not manager.pending
    assert manager.active(session)
    assert len(fake.silent_requests) == 1


@pytest.mark.parametrize("change", ["revoke", "epoch", "identity"])
def test_resource_acquisition_rechecks_current_session(tmp_path, change):
    fake = FakeMsal(ontology_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    session = signed_in(manager)

    def acquire(scopes, account):
        if change == "revoke":
            manager.revoke(session.key)
        elif change == "epoch":
            session.epoch = "changed"
        else:
            session.object_id = OTHER
        return {"access_token": "synthetic", "scope": "https://api.fabric.microsoft.com/Item.Read.All"}

    fake.acquire_token_silent_with_error = acquire
    with pytest.raises(AuthError, match="signed out"):
        manager.token(session, resource="ontology")


def resource_config(tmp_path):
    cfg = ontology_config(tmp_path)
    cfg.source_metadata_resource = SourceMetadataResourceConfig(
        scopes=list(SOURCE_METADATA_SCOPES), review_evidence_hash="b" * 64,
        review_reference="Synthetic reviewed source-metadata contract; no live permission evidence",
    )
    cfg.storage_resource = StorageResourceConfig(
        scopes=list(STORAGE_SCOPES), review_evidence_hash="c" * 64,
        review_reference="Synthetic reviewed Storage contract; no live permission evidence",
    )
    return cfg


RESOURCES = [("source_metadata", SOURCE_METADATA_SCOPES), ("storage", STORAGE_SCOPES)]


def consent_flow(manager, session, resource):
    key, uri = manager.begin(session.key, resource)
    return key, {"code": "synthetic-code", "state": parse_qs(urlparse(uri).query)["state"][0]}


@pytest.mark.parametrize("model,scopes", [
    (SourceMetadataResourceConfig, SOURCE_METADATA_SCOPES), (StorageResourceConfig, STORAGE_SCOPES),
])
@pytest.mark.parametrize("change", [
    "missing-scopes", "empty", "default", "other-audience", "other-scope", "extra-scope",
    "missing-hash", "bad-hash", "missing-reference", "blank-reference", "arbitrary-url",
])
def test_new_resource_configuration_requires_exact_reviewed_contract(model, scopes, change):
    values = {"scopes": list(scopes), "review_evidence_hash": "a" * 64, "review_reference": "synthetic"}
    if change == "missing-scopes":
        values.pop("scopes")
    elif change == "empty":
        values["scopes"] = []
    elif change == "default":
        values["scopes"] = [scopes[0].rsplit("/", 1)[0] + "/.default"]
    elif change == "other-audience":
        values["scopes"] = ["https://graph.microsoft.com/" + scopes[0].rsplit("/", 1)[1]]
    elif change == "other-scope":
        values["scopes"] = [scopes[0].rsplit("/", 1)[0] + "/Item.Read.All"]
    elif change == "extra-scope":
        values["scopes"] += ["https://graph.microsoft.com/User.Read"]
    elif change == "missing-hash":
        values.pop("review_evidence_hash")
    elif change == "bad-hash":
        values["review_evidence_hash"] = "unreviewed"
    elif change == "missing-reference":
        values.pop("review_reference")
    elif change == "blank-reference":
        values["review_reference"] = " \t"
    else:
        values["resource_url"] = "https://unreviewed.test"
    with pytest.raises(ValidationError):
        model.model_validate(values)


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_resource_consent_rotates_object_key_epoch_csrf_and_cancels_other_pending(tmp_path, resource, scopes):
    cfg = resource_config(tmp_path)
    fake = FakeMsal(cfg)
    factory_calls = []
    manager = ReaderAuth(cfg, lambda _: factory_calls.append("initial") or fake)
    old = signed_in(manager)
    original = old.key, old.epoch, old.csrf
    other, _ = consent_flow(manager, old, resource)
    key, response = consent_flow(manager, old, resource)
    pending = manager.pending[key]
    assert pending.prior_session is old and pending.resource == resource
    assert pending.expires_at <= old.expires_at
    assert fake.begin_requests[-1][0] == list(scopes)
    assert fake.begin_requests[-1][1]["prompt"] == "consent"
    fake.result["scope"] = " ".join(scopes)
    new = manager.finish(key, response)
    assert factory_calls == ["initial"]
    assert new is not old and new.client is old.client and new.lock is old.lock
    assert (new.key, new.epoch, new.csrf) != original
    assert all(a != b for a, b in zip((new.key, new.epoch, new.csrf), original))
    assert (old.key, old.epoch, old.csrf) == original
    assert new.object_id == old.object_id == USER and new.tenant_id == TENANT
    assert new.expires_at <= old.expires_at and new.expires_at <= cfg.expires_at.timestamp()
    assert new.consented_resources == {resource} and not old.consented_resources
    assert manager.active(new) and not manager.active(old)
    assert not manager.pending
    with pytest.raises(AuthError, match="CSRF"):
        manager.csrf(old, old.csrf)
    with pytest.raises(AuthError, match="already used"):
        manager.finish(other, response)
    with pytest.raises(AuthError, match="already used"):
        manager.finish(key, response)


def test_sequential_single_audience_consents_retain_same_identity_and_prior_consent(tmp_path):
    cfg = resource_config(tmp_path)
    fake = FakeMsal(cfg)
    manager = ReaderAuth(cfg, lambda _: fake)
    session = signed_in(manager)
    for resource, scopes in RESOURCES:
        key, response = consent_flow(manager, session, resource)
        fake.result["scope"] = " ".join(scopes)
        session = manager.finish(key, response)
    assert session.consented_resources == {"source_metadata", "storage"}
    assert len(manager.sessions) == 1
    assert [request[0] for request in fake.begin_requests] == [
        list(POWER_BI_SCOPES), list(SOURCE_METADATA_SCOPES), list(STORAGE_SCOPES),
    ]
    states = manager.resource_readiness(session)
    assert all(state["consent"] == "COMPLETED_IN_CURRENT_SESSION" for state in states)
    assert all(state["authorization"] == "UNKNOWN_NOT_PROBED" for state in states)


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_existing_grant_uses_current_scoped_sdk_and_rotates_without_interactive_consent(tmp_path, resource, scopes):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    original = old.key, old.epoch, old.csrf
    consent_flow(manager, old, resource)
    begins, callbacks = len(fake.begin_requests), len(fake.callback_requests)
    fake.result["scope"] = " ".join(scopes)
    new = manager.authorize_existing(old, resource)
    assert fake.silent_requests == [(list(scopes), fake.accounts[0])]
    assert len(fake.begin_requests) == begins and len(fake.callback_requests) == callbacks
    assert new is not old and new.client is old.client and new.lock is old.lock
    assert all(a != b for a, b in zip((new.key, new.epoch, new.csrf), original))
    assert (old.key, old.epoch, old.csrf) == original
    assert new.object_id == old.object_id and new.tenant_id == old.tenant_id
    assert new.expires_at <= old.expires_at and new.expires_at <= fake.cfg.expires_at.timestamp()
    assert new.existing_grant_resources == {resource} and not new.consented_resources
    assert manager.active(new) and not manager.active(old) and not manager.pending
    state = next(item for item in manager.resource_readiness(new) if item["resource"] == resource)
    assert state["consent"] == "EXISTING_GRANT_VALIDATED_IN_CURRENT_SESSION"
    assert state["authorization"] == "UNKNOWN_NOT_PROBED" and state["execution"] == "NOT_ESTABLISHED"
    with pytest.raises(AuthError):
        manager.csrf(old, old.csrf)


def test_sequential_existing_grants_and_interactive_callbacks_preserve_exact_provenance(tmp_path):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    session = signed_in(manager)
    for resource, scopes in RESOURCES:
        fake.result["scope"] = " ".join(scopes)
        session = manager.authorize_existing(session, resource)
    assert session.existing_grant_resources == {"source_metadata", "storage"}
    assert not session.consented_resources and len(fake.begin_requests) == 1
    key, response = consent_flow(manager, session, "source_metadata")
    fake.result["scope"] = " ".join(SOURCE_METADATA_SCOPES)
    session = manager.finish(key, response)
    assert session.consented_resources == {"source_metadata"} and session.existing_grant_resources == {"storage"}
    fake.result["scope"] = " ".join(SOURCE_METADATA_SCOPES)
    session = manager.authorize_existing(session, "source_metadata")
    assert not session.consented_resources and session.existing_grant_resources == {"source_metadata", "storage"}


@pytest.mark.parametrize("resource,scopes", RESOURCES)
@pytest.mark.parametrize("failure", [
    "missing", "interaction_required", "consent_required", "invalid_grant", "wrong-scopes",
    "wrong-account", "wrong-realm", "duplicate-account", "wrong-refreshed-identity",
])
def test_existing_grant_failure_preserves_session_and_never_initiates_consent(tmp_path, resource, scopes, failure):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    original = old.key, old.epoch, old.csrf
    if failure == "missing":
        fake.result = None
    elif failure in {"interaction_required", "consent_required", "invalid_grant"}:
        fake.result = {"error": failure, "error_description": "sensitive-provider-description"}
    elif failure == "wrong-scopes":
        fake.result["scope"] = "https://graph.microsoft.com/User.Read"
    elif failure == "wrong-account":
        fake.accounts[0]["local_account_id"] = OTHER
    elif failure == "wrong-realm":
        fake.accounts[0]["realm"] = OTHER
    elif failure == "duplicate-account":
        fake.accounts.append(dict(fake.accounts[0]))
    else:
        fake.result["scope"] = " ".join(scopes)
        fake.result["id_token_claims"]["oid"] = OTHER
    with pytest.raises(AuthError) as error:
        manager.authorize_existing(old, resource)
    assert "sensitive-provider-description" not in str(error.value)
    assert manager.active(old) and (old.key, old.epoch, old.csrf) == original
    assert not old.existing_grant_resources and not old.consented_resources
    assert len(manager.sessions) == 1 and not manager.pending and len(fake.begin_requests) == 1


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_existing_grant_cannot_publish_after_logout_during_sdk_acquisition(tmp_path, resource, scopes):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    def acquire(scopes, account):
        manager.revoke(old.key)
        return {"access_token": "synthetic", "scope": " ".join(scopes)}
    fake.acquire_token_silent_with_error = acquire
    with pytest.raises(AuthError, match="signed out"):
        manager.authorize_existing(old, resource)
    assert not manager.sessions and not old.existing_grant_resources and len(fake.begin_requests) == 1


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_existing_grant_accepts_only_sdk_scope_filtered_cache_omission(tmp_path, resource, scopes):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    fake.result = {"access_token": "synthetic-cache-only", "token_source": "cache"}
    new = manager.authorize_existing(old, resource)
    assert new.existing_grant_resources == {resource}
    assert fake.silent_requests == [(list(scopes), fake.accounts[0])] and len(fake.begin_requests) == 1


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_existing_grant_cannot_extend_an_expired_reader_window_during_sdk_call(tmp_path, resource, scopes):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    def acquire(scopes, account):
        manager.config.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        return {"access_token": "synthetic", "scope": " ".join(scopes)}
    fake.acquire_token_silent_with_error = acquire
    with pytest.raises(AuthError):
        manager.authorize_existing(old, resource)
    assert not old.existing_grant_resources and not old.consented_resources
    assert len(fake.begin_requests) == 1


@pytest.mark.parametrize("resource", ["iq", "ontology", "graph", ".default"])
def test_existing_grant_rejects_unreviewed_resource_before_sdk(tmp_path, resource):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    with pytest.raises(AuthError, match="Unreviewed"):
        manager.authorize_existing(old, resource)
    assert not fake.silent_requests and manager.active(old)


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_optional_resources_block_before_any_sdk_request_when_absent(tmp_path, resource, scopes):
    fake = FakeMsal(config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    session = signed_in(manager)
    with pytest.raises(AuthError, match="not configured"):
        manager.token(session, resource)
    with pytest.raises(AuthError, match="not configured"):
        manager.begin(session.key, resource)
    with pytest.raises(AuthError, match="not configured"):
        manager.authorize_existing(session, resource)
    assert len(fake.begin_requests) == 1 and not fake.silent_requests and not manager.pending
    assert manager.active(session)


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_new_resource_token_cache_is_bound_to_current_account_and_exact_audience(tmp_path, resource, scopes):
    cfg = resource_config(tmp_path)
    fake = FakeMsal(cfg)
    manager = ReaderAuth(cfg, lambda _: fake)
    session = signed_in(manager)
    fake.result = {"access_token": "synthetic-scoped-cache", "token_source": "cache"}
    assert manager.token(session, resource) == "synthetic-scoped-cache"
    assert fake.silent_requests[-1] == (list(scopes), fake.accounts[0])
    fake.result["scope"] = " ".join(STORAGE_SCOPES if resource == "source_metadata" else SOURCE_METADATA_SCOPES)
    with pytest.raises(AuthError, match="consent"):
        manager.token(session, resource)
    fake.result = {"access_token": "synthetic", "scope": " ".join(scopes)}
    fake.accounts = [{"local_account_id": OTHER, "realm": TENANT}]
    with pytest.raises(AuthError, match="cache account"):
        manager.token(session, resource)
    assert not manager.pending and len(fake.begin_requests) == 1


@pytest.mark.parametrize("resource,scopes", RESOURCES)
@pytest.mark.parametrize("grant", [
    "", "Item.Read.All", "https://graph.microsoft.com/Item.ReadWrite.All",
    "https://api.fabric.microsoft.com/.default",
    "https://storage.azure.com/.default", None,
])
def test_new_consent_rejects_absent_or_wrong_resource_grants(tmp_path, resource, scopes, grant):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    key, response = consent_flow(manager, old, resource)
    fake.result["scope"] = grant
    with pytest.raises(AuthError, match="consent"):
        manager.finish(key, response)
    assert manager.active(old) and not manager.pending and len(manager.sessions) == 1


@pytest.mark.parametrize("resource,scopes", RESOURCES)
@pytest.mark.parametrize("change", ["oid", "tid", "aud", "issuer", "expiry", "state", "nonce", "sdk-nonce"])
def test_resource_callback_rejects_identity_and_protocol_changes(tmp_path, resource, scopes, change):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    key, response = consent_flow(manager, old, resource)
    fake.result["scope"] = " ".join(scopes)
    if change == "oid":
        fake.claims["oid"] = OTHER  # Even another allowed object cannot replace the selected human.
    elif change == "tid":
        fake.claims["tid"] = OTHER
    elif change == "aud":
        fake.claims["aud"] = OTHER
    elif change == "issuer":
        fake.claims["iss"] = "https://untrusted.test"
    elif change == "expiry":
        fake.claims["exp"] = 1
    elif change == "state":
        response["state"] = "wrong"
    elif change == "nonce":
        response["unit_nonce_failure"] = "yes"
    else:
        response["unit_runtime_nonce_failure"] = "yes"
    with pytest.raises(AuthError) as error:
        manager.finish(key, response)
    assert "sensitive" not in str(error.value)
    assert manager.active(old) and len(manager.sessions) == 1 and not manager.pending


@pytest.mark.parametrize("resource,scopes", RESOURCES)
@pytest.mark.parametrize("moment", ["before", "during"])
@pytest.mark.parametrize("change", ["revoke", "session-expiry", "config-expiry", "epoch", "identity", "flow-expiry"])
def test_resource_consent_never_resurrects_session_or_flow(tmp_path, resource, scopes, moment, change):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    key, response = consent_flow(manager, old, resource)
    pending = manager.pending[key]
    fake.result["scope"] = " ".join(scopes)
    def invalidate():
        if change == "revoke":
            manager.revoke(old.key)
        elif change == "session-expiry":
            old.expires_at = 1
        elif change == "config-expiry":
            manager.config.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif change == "epoch":
            old.epoch = "changed"
        elif change == "identity":
            old.object_id = OTHER
        else:
            pending.expires_at = 1
    if moment == "before":
        invalidate()
    else:
        acquire = fake.acquire_token_by_auth_code_flow
        def invalidate_during(flow, response):
            result = acquire(flow, response)
            invalidate()
            return result
        fake.acquire_token_by_auth_code_flow = invalidate_during
    with pytest.raises(AuthError):
        manager.finish(key, response)
    assert all(session is old for session in manager.sessions.values())
    assert not old.consented_resources


@pytest.mark.parametrize("resource,scopes", RESOURCES)
@pytest.mark.parametrize("parameter,value", [
    ("scope", "https://graph.microsoft.com/User.Read openid profile offline_access"),
    ("scope", "https://api.fabric.microsoft.com/.default openid profile offline_access"),
    ("scope", "https://storage.azure.com/.default openid profile offline_access"),
    ("scope", "Item.ReadWrite.All openid profile offline_access"),
    ("client_id", OTHER), ("redirect_uri", "http://localhost:8766/auth/callback"),
    ("code_challenge_method", "plain"), ("code_challenge", "wrong"), ("state", "wrong"),
    ("nonce", "wrong"), ("response_type", "token"), ("response_mode", "fragment"), ("prompt", "none"),
])
def test_resource_authorization_uri_validates_actual_sdk_request(tmp_path, resource, scopes, parameter, value):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    initiate = fake.initiate_auth_code_flow
    def malformed(scopes, **kwargs):
        flow = initiate(scopes, **kwargs)
        uri = urlparse(flow["auth_uri"])
        query = parse_qs(uri.query)
        query[parameter] = [value]
        flow["auth_uri"] = uri._replace(query=urlencode(query, doseq=True)).geturl()
        return flow
    fake.initiate_auth_code_flow = malformed
    with pytest.raises(AuthError, match="bounded PKCE"):
        manager.begin(old.key, resource)
    assert not manager.pending and manager.active(old)


@pytest.mark.parametrize("replacement", [
    f"https://login.microsoftonline.com/{OTHER}/oauth2/v2.0/authorize",
    f"https://login.microsoftonline.com/{TENANT}/unreviewed",
    f"https://login.microsoftonline.com:443/{TENANT}/oauth2/v2.0/authorize",
    f"https://login.microsoftonline.com@untrusted.test/{TENANT}/oauth2/v2.0/authorize",
    f"http://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
])
def test_resource_authorization_uri_rejects_unbounded_authority(tmp_path, replacement):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    initiate = fake.initiate_auth_code_flow
    def malformed(scopes, **kwargs):
        flow = initiate(scopes, **kwargs)
        flow["auth_uri"] = replacement + "?" + urlparse(flow["auth_uri"]).query
        return flow
    fake.initiate_auth_code_flow = malformed
    with pytest.raises(AuthError, match="bounded PKCE"):
        manager.begin(old.key, "storage")
    assert not manager.pending


def test_pending_consent_cap_and_unknown_resource_never_create_another_client(tmp_path):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    for invalid in ("iq", "ontology", "graph", "https://storage.azure.com", ".default"):
        with pytest.raises(AuthError, match="Unreviewed"):
            manager.begin(old.key, invalid)
    with pytest.raises(AuthError, match="Sign in"):
        manager.begin(None, "storage")
    for _ in range(32):
        manager.begin(old.key, "storage")
    with pytest.raises(AuthError, match="limit"):
        manager.begin(old.key, "storage")
    assert len(manager.pending) == 32 and len(fake.begin_requests) == 33
    manager.revoke(old.key)
    assert not manager.pending and not manager.sessions


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_real_msal_139_authorization_flow_shape_is_accepted_without_network(tmp_path, resource, scopes):
    from msal.oauth2cli.oidc import Client as OidcClient

    sdk = OidcClient({
        "authorization_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
    }, CLIENT)
    manager = ReaderAuth(resource_config(tmp_path), FakeMsal)
    flow = sdk.initiate_auth_code_flow(
        scope=list(scopes) + ["openid", "profile", "offline_access"],
        redirect_uri=manager.config.redirect_uri, prompt="consent", response_mode="query",
    )
    manager._validate_flow(flow, scopes, consent=True)


@pytest.mark.parametrize("resource,scopes", RESOURCES)
@pytest.mark.parametrize("valid_nonce", [True, False])
def test_resource_callback_uses_real_msal_nonce_validation_without_token_endpoint_calls(
    tmp_path, monkeypatch, resource, scopes, valid_nonce,
):
    from msal.oauth2cli import oauth2, oidc

    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    key, response = consent_flow(manager, old, resource)
    result = copy.deepcopy(fake.result)
    result["scope"] = " ".join(scopes)
    result["id_token_claims"]["nonce"] = hashlib.sha256(
        manager.pending[key].flow["nonce"].encode("ascii"),
    ).hexdigest() if valid_nonce else "synthetic-wrong-nonce"
    monkeypatch.setattr(oauth2.Client, "obtain_token_by_auth_code_flow", lambda *args, **kwargs: result)
    sdk = oidc.Client({
        "authorization_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
    }, CLIENT)
    fake.acquire_token_by_auth_code_flow = sdk.obtain_token_by_auth_code_flow
    if valid_nonce:
        new = manager.finish(key, response)
        assert manager.active(new) and not manager.active(old)
    else:
        with pytest.raises(AuthError, match="SDK rejected") as error:
            manager.finish(key, response)
        assert "synthetic-wrong-nonce" not in str(error.value) and manager.active(old)


@pytest.mark.parametrize("resource,scopes", RESOURCES)
def test_paused_redemption_does_not_hold_global_lock_or_publish_after_logout(tmp_path, resource, scopes):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    key, response = consent_flow(manager, old, resource)
    fake.result["scope"] = " ".join(scopes)
    paused, release, revoked = threading.Event(), threading.Event(), threading.Event()
    acquire = fake.acquire_token_by_auth_code_flow
    errors, sessions = [], []
    def redeem(flow, response):
        paused.set()
        assert release.wait(10), "Unit redemption was not released"
        return acquire(flow, response)
    fake.acquire_token_by_auth_code_flow = redeem
    def finish():
        try:
            sessions.append(manager.finish(key, response))
        except AuthError as error:
            errors.append(str(error))
    thread = threading.Thread(target=finish)
    thread.start()
    try:
        assert paused.wait(10)
        def logout():
            manager.revoke(old.key)
            revoked.set()
        logout_thread = threading.Thread(target=logout)
        logout_thread.start()
        assert revoked.wait(5), "Global auth lock was held across MSAL I/O"
        logout_thread.join(5)
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not manager.sessions and not sessions and errors
    assert not manager.active(old) and not manager.pending


def test_overlapping_redemptions_replace_only_once_and_keep_client_lock(tmp_path):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    transactions = [consent_flow(manager, old, resource) for resource, _ in RESOURCES]
    paused, release = threading.Event(), threading.Event()
    acquired = fake.acquire_token_by_auth_code_flow
    def redeem(flow, response):
        paused.set()
        assert release.wait(10)
        result = acquired(flow, response)
        result["scope"] = " ".join(SOURCE_METADATA_SCOPES)
        return result
    fake.acquire_token_by_auth_code_flow = redeem
    sessions, errors = [], []
    def finish(transaction):
        try:
            sessions.append(manager.finish(*transaction))
        except AuthError as error:
            errors.append(str(error))
    first = threading.Thread(target=finish, args=(transactions[0],))
    second = threading.Thread(target=finish, args=(transactions[1],))
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
    assert len(sessions) == len(errors) == 1 and len(manager.sessions) == 1
    assert sessions[0].lock is old.lock and sessions[0].client is old.client
    assert manager.active(sessions[0]) and not manager.active(old)


@pytest.mark.parametrize("additional_scope", [
    "https://api.fabric.microsoft.com/Item.Read.All", "Item.Read.All",
])
def test_previously_consented_same_resource_scopes_do_not_expand_requested_scopes(tmp_path, additional_scope):
    cfg = resource_config(tmp_path)
    cfg.ontology_resource.scopes = list(SOURCE_METADATA_SCOPES)
    fake = FakeMsal(cfg)
    manager = ReaderAuth(cfg, lambda _: fake)
    session = signed_in(manager)
    fake.result["scope"] = " ".join(SOURCE_METADATA_SCOPES) + " " + additional_scope
    assert manager.token(session, "ontology") == "synthetic-unit-token"
    assert manager.token(session, "source_metadata") == "synthetic-unit-token"
    assert [request[0] for request in fake.silent_requests] == [
        list(SOURCE_METADATA_SCOPES), list(SOURCE_METADATA_SCOPES),
    ]
    assert manager.active(session) and not manager.pending and not session.consented_resources


@pytest.mark.parametrize("additional_scope", [
    "https://api.fabric.microsoft.com/Item.Read.All", "Item.Read.All",
])
def test_metadata_callback_accepts_incremental_grants_and_rotates_same_human(tmp_path, additional_scope):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    old.consented_resources = frozenset({"storage"})
    key, response = consent_flow(manager, old, "source_metadata")
    fake.result["scope"] = " ".join((*SOURCE_METADATA_SCOPES, additional_scope, "openid", "profile"))
    new = manager.finish(key, response)
    assert new.object_id == old.object_id == USER and new.tenant_id == old.tenant_id == TENANT
    assert new.client is old.client and new.lock is old.lock
    assert all(a != b for a, b in zip((new.key, new.epoch, new.csrf), (old.key, old.epoch, old.csrf)))
    assert new.consented_resources == {"source_metadata", "storage"}
    assert manager.active(new) and not manager.active(old) and not manager.pending
    assert fake.begin_requests[-1][0] == list(SOURCE_METADATA_SCOPES)


def test_setup_mode_metadata_callback_accepts_prior_grants_without_mistaking_null_scope_for_wrong_identity(tmp_path):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    client = TestClient(create_app(ROOT, reader_auth=manager), base_url="http://localhost:8765")
    login = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    client.get("/auth/callback", params={"code": "synthetic-code", "state": state}, follow_redirects=False)
    previous_epoch = client.get("/api/health").json()["session_epoch"]
    for resource, scopes in [("storage", STORAGE_SCOPES), ("source_metadata", SOURCE_METADATA_SCOPES)]:
        before = client.get("/api/health").json()
        consent = client.post(
            f"/auth/consent/{resource}", json={}, headers={"X-CSRF-Token": before["csrf_token"]},
        )
        state = parse_qs(urlparse(consent.json()["authorization_uri"]).query)["state"][0]
        fake.result["scope"] = " ".join(scopes)
        if resource == "source_metadata":
            fake.result["scope"] += " https://api.fabric.microsoft.com/Item.Read.All"
        callback = client.get(
            "/auth/callback", params={"code": "synthetic-code", "state": state}, follow_redirects=False,
        )
        assert callback.status_code == 303 and callback.headers["location"] == "/"
        after = client.get("/api/health").json()
        assert after["authenticated"] and after["scope"] is None
        assert after["session_epoch"] != previous_epoch
        previous_epoch = after["session_epoch"]
    assert all(
        item["consent"] == "COMPLETED_IN_CURRENT_SESSION"
        and item["authorization"] == "UNKNOWN_NOT_PROBED" and item["execution"] == "NOT_ESTABLISHED"
        for item in after["resource_consent"]
    )
    assert after["readiness"]["ready"] is False and not fake.silent_requests
    assert len(manager.sessions) == 1 and not manager.pending


@pytest.mark.parametrize("additional_scope", [
    "https://graph.microsoft.com/User.Read",
    "https://analysis.windows.net/powerbi/api/Item.ReadWrite.All",
    "https://storage.azure.com/user_impersonation",
    "https://api.fabric.microsoft.com/.default",
    ".default",
])
def test_metadata_callback_rejects_default_or_cross_resource_incremental_grants(tmp_path, additional_scope):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    old = signed_in(manager)
    key, response = consent_flow(manager, old, "source_metadata")
    fake.result["scope"] = " ".join((*SOURCE_METADATA_SCOPES, additional_scope))
    with pytest.raises(AuthError, match="consent"):
        manager.finish(key, response)
    assert manager.active(old) and not old.consented_resources
    assert not manager.pending and len(manager.sessions) == 1


@pytest.mark.parametrize("change,expected_code", [
    ("missing", "MISSING_REQUIRED_SCOPES"),
    ("resource", "WRONG_RESOURCE_SCOPES"),
    ("invalid", "INVALID_SCOPE_RESPONSE"),
    ("identity", "IDENTITY_REJECTED"),
    ("sdk", "SDK_VALIDATION_REJECTED"),
    ("endpoint", "TOKEN_UNAVAILABLE"),
])
def test_callback_logs_only_closed_failure_category_not_authentication_material(
    tmp_path, caplog, change, expected_code,
):
    fake = FakeMsal(resource_config(tmp_path))
    manager = ReaderAuth(fake.cfg, lambda _: fake)
    client = TestClient(create_app(ROOT, reader_auth=manager), base_url="http://localhost:8765")
    login = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    client.get("/auth/callback", params={"code": "synthetic-initial-code", "state": state}, follow_redirects=False)
    old = manager.session(client.cookies["telco_iq_session"])
    consent = client.post(
        "/auth/consent/source_metadata", json={}, headers={"X-CSRF-Token": old.csrf},
    )
    state = parse_qs(urlparse(consent.json()["authorization_uri"]).query)["state"][0]
    fake.result["access_token"] = "never-log-access-material"
    fake.result["error_description"] = "never-log-raw-endpoint-detail"
    fake.result["scope"] = " ".join(SOURCE_METADATA_SCOPES)
    response = {"code": "never-log-code-material", "state": state}
    if change == "missing":
        fake.result["scope"] = "https://api.fabric.microsoft.com/Item.Read.All"
    elif change == "resource":
        fake.result["scope"] += " https://graph.microsoft.com/User.Read"
    elif change == "invalid":
        fake.result["scope"] = None
    elif change == "identity":
        fake.claims["oid"] = OTHER
    elif change == "sdk":
        response["unit_runtime_nonce_failure"] = "yes"
    else:
        fake.result.pop("access_token")
        fake.result["error"] = "invalid_client"
    rejected = client.get("/auth/callback", params=response, follow_redirects=False)
    assert rejected.headers["location"] == "/?auth_error=sign_in_failed"
    messages = [record.getMessage() for record in caplog.records if record.name == "telco_iq.api"]
    assert messages == [f"Reader sign-in callback rejected ({expected_code})"]
    assert "never-log" not in caplog.text and "sensitive" not in caplog.text
    assert manager.active(old) and not manager.pending and not old.consented_resources
