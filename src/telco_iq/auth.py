from __future__ import annotations

import base64
import ctypes
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import msal
from pydantic import Field, model_validator

from telco_iq.approval import private_config_path
from telco_iq.contracts import Contract, Scope

POWER_BI_SCOPES = tuple(
    "https://analysis.windows.net/powerbi/api/" + name
    for name in ("Item.Read.All", "Item.Execute.All", "Dataset.Read.All")
)
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
SOURCE_METADATA_SCOPES = (FABRIC_RESOURCE + "/Item.ReadWrite.All",)
STORAGE_SCOPES = ("https://storage.azure.com/user_impersonation",)
Resource = Literal["iq", "ontology", "source_metadata", "storage"]
ConsentResource = Literal["source_metadata", "storage"]
_OIDC_SCOPES = frozenset({"openid", "profile", "offline_access"})


class ReviewedResourceConfig(Contract):
    review_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_reference: str = Field(min_length=1, pattern=r"\S")


class SourceMetadataResourceConfig(ReviewedResourceConfig):
    scopes: list[Literal["https://api.fabric.microsoft.com/Item.ReadWrite.All"]] = Field(
        min_length=1, max_length=1,
    )


class StorageResourceConfig(ReviewedResourceConfig):
    scopes: list[Literal["https://storage.azure.com/user_impersonation"]] = Field(
        min_length=1, max_length=1,
    )


class OntologyResourceConfig(Contract):
    """Explicitly reviewed challenge scopes; not inferred from the IQ registration."""

    scopes: list[str] = Field(min_length=1)
    challenge_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_reference: str = Field(min_length=1)

    @model_validator(mode="after")
    def delegated_fabric_scopes(self):
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("Ontology resource scopes must be unique")
        for scope in self.scopes:
            name = scope.removeprefix(FABRIC_RESOURCE + "/")
            if (
                not scope.startswith(FABRIC_RESOURCE + "/") or not name
                or name == ".default" or any(c in name for c in "/?#")
                or any(c.isspace() for c in name)
            ):
                raise ValueError("Explicit Fabric delegated scopes from a reviewed challenge are required")
        return self


class AuthError(Exception):
    """Messages contain no authorization response, token, cookie, nonce or verifier."""

    def __init__(
        self, message: str, *,
        code: Literal[
            "SIGN_IN_REJECTED", "TOKEN_UNAVAILABLE", "INVALID_SCOPE_RESPONSE",
            "WRONG_RESOURCE_SCOPES", "MISSING_REQUIRED_SCOPES", "IDENTITY_REJECTED",
            "SDK_VALIDATION_REJECTED", "TRANSACTION_MISSING", "TRANSACTION_EXPIRED",
            "STATE_REJECTED", "READER_WINDOW_EXPIRED",
        ] = "SIGN_IN_REJECTED",
    ):
        super().__init__(message)
        self.code = code


class ReaderConfig(Contract):
    tenant_id: UUID
    client_id: UUID
    allowed_object_ids: list[UUID] = Field(min_length=1)
    certificate_file: Path
    certificate_thumbprint: str = Field(pattern=r"^[A-Fa-f0-9]{40}$")
    redirect_uri: Literal["http://localhost:8765/auth/callback"] = "http://localhost:8765/auth/callback"
    expires_at: datetime
    model_ids: dict[str, UUID]
    receipt_directory: Path
    ontology_resource: OntologyResourceConfig | None = None
    source_metadata_resource: SourceMetadataResourceConfig | None = None
    storage_resource: StorageResourceConfig | None = None

    @model_validator(mode="after")
    def boundaries(self):
        if self.expires_at.tzinfo is None:
            raise ValueError("Reader expiry must have a timezone")
        if len(set(self.model_ids.values())) != len(self.model_ids):
            raise ValueError("Independent models must not alias one resource")
        return self

    @classmethod
    def read(cls, path: Path, root: Path) -> ReaderConfig:
        path = private_config_path(path, root)
        config = cls.model_validate_json(path.read_text(encoding="utf-8"))
        private_config_path(config.certificate_file, root)
        private_config_path(config.receipt_directory, root)
        return config


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_byte))]


def dpapi(value: bytes, *, decrypt: bool) -> bytes:
    if not hasattr(ctypes, "WinDLL"):
        raise AuthError("OS-protected credentials require Windows DPAPI; no plaintext fallback")
    buffer = ctypes.create_string_buffer(value)
    incoming = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    outgoing = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_Blob),
    ]
    function.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise AuthError("OS-protected credential operation failed")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.memset(outgoing.data, 0, outgoing.size)
        kernel.LocalFree(outgoing.data)


class MsalClient(Protocol):
    def initiate_auth_code_flow(self, scopes, **kwargs) -> dict: ...
    def acquire_token_by_auth_code_flow(self, flow, response) -> dict: ...
    def get_accounts(self) -> list[dict]: ...
    def acquire_token_silent_with_error(self, scopes, account, **kwargs) -> dict | None: ...


def msal_factory(config: ReaderConfig) -> MsalClient:
    key = dpapi(config.certificate_file.read_bytes(), decrypt=True).decode("utf-8")
    return msal.ConfidentialClientApplication(
        client_id=str(config.client_id),
        authority=f"https://login.microsoftonline.com/{config.tenant_id}",
        client_credential={"private_key": key, "thumbprint": config.certificate_thumbprint},
        token_cache=msal.SerializableTokenCache(),
        timeout=15, enable_pii_log=False,
    )


@dataclass(repr=False)
class PendingLogin:
    client: MsalClient
    flow: dict
    expires_at: float
    resource: ConsentResource | None = None
    prior_session: ReaderSession | None = None
    prior_identity: tuple[str, str, str] | None = None
    scopes: tuple[str, ...] = POWER_BI_SCOPES
    purpose: Literal["login", "resource_consent"] = "login"
    prior_session_key: str | None = None


@dataclass(repr=False)
class ReaderSession:
    key: str
    object_id: str
    tenant_id: str
    client: MsalClient
    csrf: str
    expires_at: float
    epoch: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    consented_resources: frozenset[str] = field(default_factory=frozenset)
    existing_grant_resources: frozenset[str] = field(default_factory=frozenset)

    def scope(self) -> Scope:
        return Scope(
            tenant=self.tenant_id, principal=self.object_id,
            authorization_revision=self.epoch, models=[],
            mode="LIVE_DELEGATED",
        )


class ReaderAuth:
    def __init__(self, config: ReaderConfig, factory: Callable[[ReaderConfig], MsalClient] = msal_factory):
        self.config, self.factory = config, factory
        self.pending: dict[str, PendingLogin] = {}
        self.sessions: dict[str, ReaderSession] = {}
        self.lock = threading.RLock()

    def _deadline(self) -> None:
        if self.config.expires_at <= datetime.now(timezone.utc):
            raise AuthError("The approved reader session window has expired", code="READER_WINDOW_EXPIRED")

    def _prune(self) -> None:
        now = time.time()
        for collection in (self.pending, self.sessions):
            for key in list(collection):
                if collection[key].expires_at <= now:
                    del collection[key]

    def _resource_scopes(self, resource: Resource) -> tuple[str, ...]:
        if resource == "iq":
            return POWER_BI_SCOPES
        if resource == "ontology":
            if self.config.ontology_resource is None:
                raise AuthError(
                    "Ontology resource is not configured: review its actual OAuth challenge and obtain "
                    "explicit Fabric delegated consent; Power BI tokens are not a fallback"
                )
            return tuple(self.config.ontology_resource.scopes)
        fixed = {
            "source_metadata": (self.config.source_metadata_resource, SOURCE_METADATA_SCOPES),
            "storage": (self.config.storage_resource, STORAGE_SCOPES),
        }
        if resource not in fixed:
            raise AuthError("Unreviewed delegated resource")
        configured, scopes = fixed[resource]
        if configured is None or tuple(configured.scopes) != scopes:
            raise AuthError(f"Reviewed {resource} resource is not configured; no automatic consent attempted")
        return scopes

    def resource_readiness(self, session: ReaderSession | None) -> list[dict]:
        """Local projection only: never acquire tokens, probe services or initiate consent."""
        current = session is not None and self.active(session)
        return [{
            "resource": resource,
            "configuration": "CONFIGURED" if configured is not None else "NOT_CONFIGURED",
            "consent": "COMPLETED_IN_CURRENT_SESSION" if current and resource in session.consented_resources
            else (
                "EXISTING_GRANT_VALIDATED_IN_CURRENT_SESSION"
                if current and resource in session.existing_grant_resources
                else "NOT_ESTABLISHED_IN_CURRENT_SESSION"
            ),
            "authorization": "UNKNOWN_NOT_PROBED", "execution": "NOT_ESTABLISHED",
        } for resource, configured in (
            ("source_metadata", self.config.source_metadata_resource), ("storage", self.config.storage_resource),
        )]

    @staticmethod
    def _session_identity(session: ReaderSession) -> tuple[str, str, str]:
        return session.tenant_id, session.object_id, session.epoch

    def _prior_active(self, pending: PendingLogin) -> bool:
        prior = pending.prior_session
        return (
            prior is not None and self.active(prior)
            and prior.key == pending.prior_session_key
            and self._session_identity(prior) == pending.prior_identity
        )

    def _validate_flow(self, flow: dict, scopes: tuple[str, ...], *, consent: bool) -> None:
        try:
            uri = urlparse(flow.get("auth_uri", ""))
            query = parse_qs(uri.query)
            verifier = flow.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            nonce_hash = hashlib.sha256(flow.get("nonce", "").encode("ascii")).hexdigest()
            requested = query.get("scope", [""])[0].split()
            expected = set(scopes) | _OIDC_SCOPES
            valid = (
                uri.scheme == "https" and uri.netloc == "login.microsoftonline.com"
                and uri.path == f"/{self.config.tenant_id}/oauth2/v2.0/authorize"
                and not uri.fragment and not uri.params
                and query.get("client_id") == [str(self.config.client_id)]
                and query.get("response_type") == ["code"] and query.get("response_mode") == ["query"]
                and query.get("prompt") == ["consent" if consent else "select_account"]
                and query.get("code_challenge_method") == ["S256"] and 43 <= len(verifier) <= 128
                and query.get("code_challenge") == [challenge.rstrip(b"=").decode("ascii")]
                and isinstance(flow.get("state"), str) and bool(flow["state"])
                and query.get("state") == [flow["state"]]
                and bool(flow.get("nonce")) and query.get("nonce") == [nonce_hash]
                and query.get("redirect_uri") == [self.config.redirect_uri]
                and flow.get("redirect_uri") == self.config.redirect_uri
                and len(query.get("scope", [])) == 1 and len(requested) == len(expected)
                and set(requested) == expected and set(flow.get("scope", [])) == expected
            )
        except (TypeError, ValueError, AttributeError, UnicodeError):
            valid = False
        if not valid:
            raise AuthError("Identity SDK did not produce the required bounded PKCE flow")

    def begin(
        self, prior_session: str | None, resource: ConsentResource | None = None,
    ) -> tuple[str, str]:
        self._deadline()
        if resource is not None and resource not in ("source_metadata", "storage"):
            raise AuthError("Unreviewed incremental consent resource")
        scopes = self._resource_scopes(resource or "iq")
        prior = self.session(prior_session) if resource is not None else None
        with self.lock:
            self._prune()
            if prior_session and resource is None:
                self.revoke(prior_session)
            if len(self.pending) >= 32 or len(self.sessions) >= 32:
                raise AuthError("Local sign-in session limit reached")
        client = prior.client if prior is not None else self.factory(self.config)
        identity = self._session_identity(prior) if prior is not None else None
        with prior.lock if prior is not None else threading.Lock():
            if prior is not None and not self.active(prior):
                raise AuthError("Reader session has ended")
            flow = client.initiate_auth_code_flow(
                scopes=list(scopes), redirect_uri=self.config.redirect_uri,
                prompt="consent" if resource is not None else "select_account", response_mode="query",
            )
        self._validate_flow(flow, scopes, consent=resource is not None)
        pending = PendingLogin(
            client=client, flow=flow,
            expires_at=min(time.time() + 600, self.config.expires_at.timestamp(),
                           prior.expires_at if prior is not None else float("inf")),
            resource=resource, prior_session=prior, prior_identity=identity, scopes=scopes,
            purpose="resource_consent" if resource is not None else "login",
            prior_session_key=prior.key if prior is not None else None,
        )
        key = secrets.token_urlsafe(32)
        with self.lock:
            self._deadline()
            self._prune()
            if resource is not None and not self._prior_active(pending):
                raise AuthError("Reader session ended during consent initiation")
            if len(self.pending) >= 32:
                raise AuthError("Local pending sign-in limit reached")
            self.pending[key] = pending
        return key, flow["auth_uri"]

    def _identity(self, claims: dict) -> str:
        if not isinstance(claims, dict):
            raise AuthError("Sign-in does not match an approved immutable DEV identity", code="IDENTITY_REJECTED")
        tenant, client_id = str(self.config.tenant_id), str(self.config.client_id)
        expected_issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
        oid = claims.get("oid")
        if (
            claims.get("tid") != tenant or claims.get("aud") != client_id
            or claims.get("iss") != expected_issuer
            or not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= time.time()
            or not claims.get("nonce")
            or oid not in {str(value) for value in self.config.allowed_object_ids}
        ):
            raise AuthError("Sign-in does not match an approved immutable DEV identity", code="IDENTITY_REJECTED")
        return oid

    @staticmethod
    def _scopes(
        result: dict, *, required_scopes: tuple[str, ...] = POWER_BI_SCOPES,
        sdk_scope_filtered_cache: bool = False,
    ) -> None:
        if not result.get("access_token"):
            known = {"interaction_required", "consent_required", "invalid_client", "invalid_grant",
                     "temporarily_unavailable", "server_error", "access_denied", "login_required"}
            code = result.get("error")
            raise AuthError(
                "Delegated token unavailable: " + (code if code in known else "sign_in_required"),
                code="TOKEN_UNAVAILABLE",
            )
        # MSAL 1.39 filters cache entries by requested scopes and account, but omits
        # scope from a cache-hit response. This exemption is only for that SDK call.
        if sdk_scope_filtered_cache and result.get("token_source") == "cache" and "scope" not in result:
            return
        prefix = required_scopes[0].rsplit("/", 1)[0] + "/"
        granted = result.get("scope", "")
        if not isinstance(granted, str):
            raise AuthError(
                "Required delegated consent is missing; invalid resource scopes", code="INVALID_SCOPE_RESPONSE",
            )
        scopes = granted.split()
        names = {value.removeprefix(prefix) for value in scopes}
        required = {value.removeprefix(prefix) for value in required_scopes}
        if any("/" in value and not value.startswith(prefix) for value in scopes) or ".default" in names:
            raise AuthError(
                "Required delegated consent has unreviewed resource scopes", code="WRONG_RESOURCE_SCOPES",
            )
        # Entra may return previously consented scopes for this same resource.
        # Those grants do not expand the fixed scopes requested by this client.
        if not required <= names:
            raise AuthError(
                "Required delegated consent is missing; no permission escalation attempted",
                code="MISSING_REQUIRED_SCOPES",
            )

    def finish(self, pending_key: str | None, response: dict[str, str]) -> ReaderSession:
        self._deadline()
        with self.lock:
            pending = self.pending.pop(pending_key, None)
        if pending is None:
            raise AuthError("Sign-in transaction missing or already used", code="TRANSACTION_MISSING")
        if pending.expires_at <= time.time():
            raise AuthError("Sign-in transaction expired; start a new sign-in", code="TRANSACTION_EXPIRED")
        if (pending.purpose == "resource_consent") != (pending.resource is not None):
            raise AuthError("Sign-in transaction purpose is invalid")
        if pending.resource is not None and not self._prior_active(pending):
            raise AuthError("Reader session ended before resource consent")
        if not response.get("code") or not response.get("state") or "error" in response:
            raise AuthError("Sign-in response rejected; start a new sign-in")
        if not secrets.compare_digest(response["state"], pending.flow["state"]):
            raise AuthError("Sign-in response rejected; start a new sign-in", code="STATE_REJECTED")
        prior = pending.prior_session
        with prior.lock if prior is not None else threading.Lock():
            if pending.resource is not None and not self._prior_active(pending):
                raise AuthError("Reader session ended before resource consent")
            self._validate_flow(pending.flow, pending.scopes, consent=pending.resource is not None)
            if self._resource_scopes(pending.resource or "iq") != pending.scopes:
                raise AuthError("Reviewed resource configuration changed during consent")
            try:
                result = pending.client.acquire_token_by_auth_code_flow(pending.flow, response)
            except (ValueError, RuntimeError):
                raise AuthError(
                    "Identity SDK rejected sign-in state or nonce", code="SDK_VALIDATION_REJECTED",
                ) from None
            self._scopes(result, required_scopes=pending.scopes)
            # MSAL checks its nonce; enforce immutable identity/issuer/audience/expiry on its endpoint result.
            oid = self._identity(result.get("id_token_claims", {}))
            if prior is not None and oid != prior.object_id:
                raise AuthError(
                    "Resource consent must retain the same approved reader identity", code="IDENTITY_REJECTED",
                )
            with self.lock:
                self._deadline()
                self._prune()
                if pending.expires_at <= time.time():
                    raise AuthError("Sign-in transaction expired during authorization", code="TRANSACTION_EXPIRED")
                if pending.resource is not None and not self._prior_active(pending):
                    raise AuthError("Reader session or consent transaction ended during authorization")
                session = self._publish_session(
                    pending.client, oid, prior, resource=pending.resource,
                )
        return session

    def _publish_session(
        self, client: MsalClient, oid: str, prior: ReaderSession | None, *,
        resource: ConsentResource | None, existing_grant: bool = False,
    ) -> ReaderSession:
        consented = prior.consented_resources if prior is not None else frozenset()
        existing = prior.existing_grant_resources if prior is not None else frozenset()
        if resource is not None:
            if existing_grant:
                existing, consented = existing | {resource}, consented - {resource}
            else:
                consented, existing = consented | {resource}, existing - {resource}
        if prior is not None:
            self.revoke(prior.key)
        if len(self.sessions) >= 32:
            raise AuthError("Local reader session limit reached")
        session = ReaderSession(
            key=secrets.token_urlsafe(32), object_id=oid, tenant_id=str(self.config.tenant_id),
            client=client, csrf=secrets.token_urlsafe(32),
            expires_at=min(
                time.time() + 8 * 3600, self.config.expires_at.timestamp(),
                prior.expires_at if prior is not None else float("inf"),
            ),
            epoch=secrets.token_hex(16), lock=prior.lock if prior is not None else threading.Lock(),
            consented_resources=consented, existing_grant_resources=existing,
        )
        self.sessions[session.key] = session
        return session

    def authorize_existing(self, session: ReaderSession, resource: ConsentResource) -> ReaderSession:
        if resource not in ("source_metadata", "storage"):
            raise AuthError("Unreviewed existing-grant resource")
        identity = self._session_identity(session)
        self.token(session, resource)
        with session.lock, self.lock:
            self._deadline()
            self._prune()
            if not self.active(session) or self._session_identity(session) != identity:
                raise AuthError("Reader session ended during existing-grant validation")
            return self._publish_session(
                session.client, session.object_id, session, resource=resource, existing_grant=True,
            )

    def session(self, key: str | None) -> ReaderSession:
        self._deadline()
        with self.lock:
            self._prune()
            session = self.sessions.get(key)
        if session is None:
            raise AuthError("Sign in with an approved DEV identity")
        return session

    def active(self, session: ReaderSession) -> bool:
        try:
            return self.session(session.key) is session
        except AuthError:
            return False

    def csrf(self, session: ReaderSession, supplied: str | None) -> None:
        if not supplied or not secrets.compare_digest(session.csrf, supplied) or not self.active(session):
            raise AuthError("Session or CSRF validation failed")

    def revoke(self, key: str | None) -> None:
        with self.lock:
            self.sessions.pop(key, None)
            for pending_key, pending in list(self.pending.items()):
                if pending.prior_session_key is not None and pending.prior_session_key == key:
                    del self.pending[pending_key]

    def cancel_pending(self, key: str | None) -> None:
        with self.lock:
            self.pending.pop(key, None)

    def token(self, session: ReaderSession, resource: Resource = "iq") -> str:
        if not self.active(session):
            raise AuthError("Reader session has ended")
        scopes = self._resource_scopes(resource)
        identity = (session.tenant_id, session.object_id, session.epoch)
        if (
            session.tenant_id != str(self.config.tenant_id)
            or session.object_id not in {str(value) for value in self.config.allowed_object_ids}
        ):
            raise AuthError("Current session identity is outside the approved reader configuration")
        with session.lock:
            if not self.active(session) or identity != self._session_identity(session):
                raise AuthError("Reader session has ended")
            accounts = [
                account for account in session.client.get_accounts()
                if account.get("local_account_id") == session.object_id
                and account.get("realm") == session.tenant_id
            ]
            if len(accounts) != 1:
                raise AuthError("Credential cache account does not match this reader")
            result = session.client.acquire_token_silent_with_error(
                scopes=list(scopes), account=accounts[0],
            )
            if not result:
                raise AuthError(
                    f"Explicit {resource} resource sign-in/consent is required; no automatic consent attempted"
                )
            self._scopes(
                result, required_scopes=scopes, sdk_scope_filtered_cache=True,
            )
            if result.get("id_token_claims") and self._identity(result["id_token_claims"]) != session.object_id:
                raise AuthError("Refreshed identity changed")
        if not self.active(session) or identity != (session.tenant_id, session.object_id, session.epoch):
            raise AuthError("Reader signed out during token acquisition")
        return result["access_token"]


class DelegatedBearer(httpx.Auth):
    def __init__(self, token: str):
        self._token = token

    def auth_flow(self, request):
        request.headers["Authorization"] = "Bearer " + self._token
        yield request
