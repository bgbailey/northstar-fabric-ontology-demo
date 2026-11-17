from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from telco_iq.approval import private_config_path
from telco_iq.auth import AuthError, DelegatedBearer, ReaderAuth, ReaderSession
from telco_iq.catalog import Catalog, digest
from telco_iq.comparison import ComparisonRequest, PairEvent
from telco_iq.context import offline_context
from telco_iq.contracts import Context, ExecutionResult, Plan, Scope, Status
from telco_iq.demo_library import RecordedAgentSurface, RecordedDemoLibrary
from telco_iq.errors import LabError
from telco_iq.events import ExecutionEvent
from telco_iq.evidence import write_receipt
from telco_iq.executor import Executor
from telco_iq.live_demo import ConfiguredLiveGateway, LiveComparisonGateway, unconfigured_readiness
from telco_iq.mcp_client import DiscoveredClient, Endpoint, mcp_capture_failure_status
from telco_iq.providers import OfflineProvider
from telco_iq.recorded_comparison import RecordedComparisons
from telco_iq.recorded_demo import RecordedDemo

logger = logging.getLogger(__name__)
_WAITING_CONFIG = "Waiting for an explicit current-reader POST to load the fixed private live configuration."
_DIFFERENT_READER = "Configured comparison belongs to another reader session; no automatic reload was attempted."

# Only reviewed local guard messages may cross the anonymous health boundary.
_PUBLIC_BLOCKERS = frozenset(unconfigured_readiness()["blockers"]) | frozenset({
    "Live demo and Azure OpenAI configurations must both be explicitly CONFIGURED.",
    "Reader credential window expired; renew it explicitly.",
    "Ontology-resource scopes/consent have not been reviewed and configured.",
    "Model-token identity and reader tenant differ.",
    "Native reader enrollment is expired or not yet valid.",
    "Native enrollment differs from its independently pinned review.",
    "Native enrollment and snapshot identity differ.",
    "Original native capture is absent or changed.",
    "Catalogue changed since native normalization; normalize and review again.",
    "No native execution-edge contracts have been reviewed for cross-model planning.",
    "Reader model identities do not cover the exact enrolled sources.",
    "Every enrolled source requires actual reviewed query and schema profiles.",
    "Every source requires a reviewed source-owned revision probe; configured clock strings are insufficient.",
    "Live source requests require explicitly approved bounded private evidence retention.",
    "The two actual native read-tool profiles must be explicitly reviewed.",
    "Model configuration is not bound to this native context, profile and plan-schema revision.",
    "A paired run requires two model requests within the shared approved cap.",
    "Configured inference cap cannot reserve both bounded model requests.",
    "Only explicitly approved lab targets are permitted",
    "Approval expired or has no timezone",
    "Approval is bound to another artifact revision/target",
    "Operation/cost outside explicit approval",
    "Private evidence approval expired or clock has no timezone",
    "Private evidence must be outside OneDrive sync roots",
    "Private evidence paths cannot traverse links or reparse points",
    "Private target/auth configuration must be outside repository and OneDrive",
    "Reviewed private live source population proof is not configured.",
    _WAITING_CONFIG,
    _DIFFERENT_READER,
})
_PRIVATE_GUARD_FAILURE = "A local configuration or approval guard failed; operator review is required."


def create_app(
    root: Path, data: Path | None = None, reader_auth: ReaderAuth | None = None,
    recorded_native: Path | None = None, local_port: int = 8765,
    live_demo: LiveComparisonGateway | None = None,
    *,
    deferred_live_config: Path | None = None,
    source_observation_config: Path | None = None,
    recorded_comparisons: Path | None = None,
    recorded_library: Path | None = None,
) -> FastAPI:
    if not 1 <= local_port <= 65535:
        raise ValueError("Local port must be between 1 and 65535")
    if reader_auth is not None and local_port != 8765:
        raise ValueError("The approved reader callback is bound to port 8765")
    if sum(value is not None for value in (reader_auth, recorded_native, recorded_comparisons, recorded_library)) > 1:
        raise ValueError("Live reader and recorded modes cannot be combined")
    if live_demo is not None and reader_auth is None:
        raise ValueError("Live comparison requires explicitly configured reader authentication")
    if (deferred_live_config is not None or source_observation_config is not None) and reader_auth is None:
        raise ValueError("Deferred configuration and source capture require reader authentication")
    if deferred_live_config is not None and live_demo is not None:
        raise ValueError("An existing live gateway cannot be replaced by deferred configuration")
    recording = RecordedDemo.read(recorded_native) if recorded_native is not None else None
    comparisons = RecordedComparisons.read(recorded_comparisons) if recorded_comparisons is not None else None
    library: RecordedDemoLibrary | None = None
    agent_surface: RecordedAgentSurface | None = None
    if recorded_library is not None:
        raw_recorded = json.loads(recorded_library.read_text(encoding="utf-8"))
        if isinstance(raw_recorded, dict) and raw_recorded.get("mode") == "RECORDED_AGENT_SURFACE":
            agent_surface = RecordedAgentSurface.model_validate(raw_recorded)
        else:
            library = RecordedDemoLibrary.model_validate(raw_recorded)
    root = root.resolve()
    deferred_live_config = private_config_path(deferred_live_config, root) if deferred_live_config is not None else None
    source_observation_config = (
        private_config_path(source_observation_config, root) if source_observation_config is not None else None
    )
    configuration_lock = asyncio.Lock()
    configured_reader: ReaderSession | None = None
    configured_scope: Scope | None = None
    configured_models: dict = {}
    catalog = Catalog(root / "data" / "contracts" / "catalog.json")
    data = data or root / "data" / "generated" / "miniature"
    scope = Scope(
        tenant="SYNTHETIC_LOCAL", principal="LOCAL_PROCESS",
        authorization_revision="offline-v1", models=list(catalog.models), mode="OFFLINE_SYNTHETIC",
    )
    context = offline_context(scope)
    executor = Executor(catalog, OfflineProvider(catalog, data), context)
    app = FastAPI(title="Telco IQ lab", version="0.1.0")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    def server_mode() -> str:
        if agent_surface is not None:
            return agent_surface.mode
        if library is not None:
            return library.mode
        if comparisons is not None:
            return comparisons.mode
        if recording is not None:
            return recording.mode
        if live_demo is not None:
            return "LIVE_CONFIGURED"
        return "DELEGATED_READER_SETUP" if reader_auth is not None else "OFFLINE_SYNTHETIC"

    def current_reader(request: Request) -> tuple[ReaderSession | None, str | None]:
        if reader_auth is None:
            return None, None
        try:
            return reader_auth.session(request.cookies.get("telco_iq_session")), None
        except AuthError as error:
            reason = str(error)
            return None, (reason if reason in {
                "The approved reader session window has expired", "Sign in with an approved DEV identity",
            } else "Reader session is not current; sign in again.")

    def public_readiness(session: ReaderSession | None, reader_error: str | None) -> dict:
        configured = live_demo is not None
        waiting = deferred_live_config is not None and not configured
        different_reader = configured_reader is not None and (
            session is not configured_reader or not reader_auth.active(configured_reader)
            or configured_scope is None
            or session.scope().model_copy(update={"models": configured_scope.models}) != configured_scope
            or any(reader_auth.config.model_ids.get(alias) != value for alias, value in configured_models.items())
        )
        if different_reader:
            checked = {"ready": False, "blockers": [_DIFFERENT_READER]}
        elif configured:
            checked = live_demo.readiness()
        else:
            checked = {"ready": False, "blockers": [_WAITING_CONFIG]} if waiting else unconfigured_readiness()
        raw_blockers = checked.get("blockers")
        valid = isinstance(raw_blockers, list) and all(isinstance(value, str) for value in raw_blockers)
        configuration_ready = checked.get("ready") is True and valid and not raw_blockers
        blockers = list(dict.fromkeys(
            value if value in _PUBLIC_BLOCKERS else _PRIVATE_GUARD_FAILURE
            for value in raw_blockers
        )) if valid else [_PRIVATE_GUARD_FAILURE]
        if not configuration_ready and not blockers:
            blockers.append(_PRIVATE_GUARD_FAILURE)
        if reader_error:
            blockers.append(reader_error)
        ready = configured and configuration_ready and session is not None and not different_reader
        return {
            "ready": ready,
            "status": "READY_FOR_CURRENT_READER_PROBE" if ready else (
                "WAITING_CONFIG" if waiting else (
                    "BLOCKED_AUTH" if different_reader or (configuration_ready and session is None) else "BLOCKED_CAPABILITY"
                )
            ),
            "blockers": blockers, "mode": server_mode(),
            "implementation": "LOCALLY_IMPLEMENTED",
            "configuration": "CONFIGURED" if configuration_ready else (
                "WAITING_CONFIG" if waiting else ("BLOCKED" if configured else "NOT_CONFIGURED")
            ),
            "reader_session": "CURRENT_SESSION" if session is not None else (
                "BLOCKED_AUTH" if reader_auth is not None else "NOT_USED"
            ),
            "native_source_probe": "UNKNOWN_NOT_PROBED",
            "application_execution": "NOT_ESTABLISHED",
            "demonstration": "NOT_ESTABLISHED",
            "claim": (
                "Local configuration and session checks only. Resource tokens and current native/source probes "
                "are not acquired here. Execution and demonstration require separate actual run evidence."
            ),
        }

    def execution_block(request: Request) -> JSONResponse | None:
        if recording is not None or comparisons is not None or library is not None or agent_surface is not None:
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY,
                "detail": "Recorded mode cannot execute or rerun a query.",
            }, status_code=409)
        if reader_auth:
            reader_auth.session(request.cookies.get("telco_iq_session"))
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY,
                "detail": "Live result/schema contract is not yet reviewed; offline execution is not a fallback.",
            }, status_code=409)
        return None

    def executor_for(plan: Plan) -> Executor:
        if plan.ontology_revision == context.revision:
            return executor
        from telco_iq.sla_fixture import sla_context

        fixture_context = sla_context(scope)
        if plan.ontology_revision != fixture_context.revision:
            raise LabError("Context revision is not registered for local execution", Status.BLOCKED_CAPABILITY)
        return Executor(catalog, OfflineProvider(catalog, data), fixture_context)

    def persist(plan: Plan, result: ExecutionResult) -> None:
        write_receipt(root / ".local" / "receipts", {
            "plan": plan.model_dump(mode="json"), "result": result.model_dump(mode="json"),
        })

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, error: AuthError):
        if request.url.path == "/auth/callback":
            logger.warning("Reader sign-in callback rejected (%s)", error.code)
            if isinstance(reader_auth, ReaderAuth):
                reader_auth.cancel_pending(request.cookies.get("telco_iq_login"))
            reason = {
                "TRANSACTION_EXPIRED": "sign_in_expired",
                "TRANSACTION_MISSING": "sign_in_restart",
                "STATE_REJECTED": "sign_in_restart",
                "READER_WINDOW_EXPIRED": "reader_window_expired",
            }.get(error.code, "sign_in_failed")
            response = RedirectResponse("/?auth_error=" + reason, status_code=303)
            response.delete_cookie("telco_iq_login", path="/")
            return response
        return JSONResponse({"status": Status.BLOCKED_AUTH, "detail": str(error)}, status_code=401)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        allowed_origins = {f"http://127.0.0.1:{local_port}", f"http://localhost:{local_port}"}
        if origin and origin not in allowed_origins:
            return JSONResponse({"detail": "Cross-origin local execution refused"}, status_code=403)
        if request.method == "POST":
            if reader_auth:
                try:
                    session = reader_auth.session(request.cookies.get("telco_iq_session"))
                    reader_auth.csrf(session, request.headers.get("x-csrf-token"))
                except AuthError:
                    return JSONResponse({"status": Status.BLOCKED_AUTH, "detail": "Session/CSRF required"}, status_code=403)
            elif request.headers.get("x-telco-local") != "submitted-plan":
                return JSONResponse({"detail": "Explicit local submitted-plan header required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
        )
        return response

    if reader_auth:
        async def setup_body(request: Request) -> JSONResponse | None:
            try:
                if request.query_params or (await request.body() and await request.json() != {}):
                    raise ValueError
            except ValueError:
                return JSONResponse({
                    "status": Status.BLOCKED_CAPABILITY, "ready": False,
                    "detail": "This setup operation accepts no client configuration or query parameters.",
                }, status_code=400)
            return None

        def setup_block(detail: str, *, status: Status = Status.BLOCKED_CAPABILITY) -> JSONResponse:
            return JSONResponse({"status": status, "ready": False, "detail": detail}, status_code=409)

        @app.post("/api/comparison/configure")
        async def configure_comparison(request: Request):
            nonlocal live_demo, configured_reader, configured_scope, configured_models
            session = reader_auth.session(request.cookies.get("telco_iq_session"))
            if blocked := await setup_body(request):
                return blocked
            if deferred_live_config is None:
                return setup_block("Deferred live configuration is not enabled; no file was loaded.")
            initial_scope = session.scope()
            async with configuration_lock:
                if live_demo is not None:
                    return setup_block("Live comparison is already configured; no replacement or file reload was attempted.")
                if not reader_auth.active(session):
                    raise AuthError("Reader session ended before local configuration")

                def load():
                    candidate = ConfiguredLiveGateway.read(deferred_live_config, reader_auth, root)
                    if any(
                        reader_auth.config.model_ids.get(alias) != source.dataset_id
                        for alias, source in candidate.enrollment.sources.items()
                    ):
                        raise LabError(
                            "Reader model identities do not cover the exact enrolled sources.", Status.BLOCKED_CAPABILITY,
                        )
                    expected = initial_scope.model_copy(update={"models": sorted(candidate.enrollment.sources)})
                    if candidate.source_proof is None:
                        raise LabError(
                            "Reviewed private live source population proof is not configured.", Status.BLOCKED_CAPABILITY,
                        )
                    if candidate.source_proof.scope != expected:
                        raise AuthError("Reviewed source proof is not bound to this exact current reader session")
                    validated = candidate.validate_reader_session(session)
                    if validated != expected:
                        raise AuthError("Local validation did not retain the exact current reader source scope")
                    return candidate, expected

                try:
                    candidate, expected = await asyncio.to_thread(load)
                except AuthError:
                    raise
                except LabError as error:
                    return setup_block(
                        str(error) if str(error) in _PUBLIC_BLOCKERS
                        else "Fixed live configuration failed local approval/profile/proof validation.",
                        status=Status.BLOCKED_AUTH if error.status == Status.BLOCKED_AUTH else Status.BLOCKED_CAPABILITY,
                    )
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    return setup_block("Fixed private live configuration is missing, invalid or unavailable.")
                if not reader_auth.active(session) or session.scope() != initial_scope:
                    raise AuthError("Reader session ended during local configuration")
                with reader_auth.lock:
                    if not reader_auth.active(session) or session.scope() != initial_scope:
                        raise AuthError("Reader session ended before configuration publication")
                    live_demo, configured_reader = candidate, session
                    configured_scope = expected
                    configured_models = {
                        alias: source.dataset_id for alias, source in candidate.enrollment.sources.items()
                    }
            return {
                "status": "CONFIGURED_NOT_PROBED", "ready": False,
                "claim": "Local wiring loaded for the unchanged reader session; no token, probe or execution was performed.",
            }

        @app.post("/api/source-observations/capture")
        async def capture_source_observations(request: Request):
            session = reader_auth.session(request.cookies.get("telco_iq_session"))
            if blocked := await setup_body(request):
                return blocked
            if source_observation_config is None:
                return setup_block("Source metadata capture is not enabled; no configuration or service was loaded.")
            initial_scope = session.scope()
            try:
                from telco_iq.source_observation import SourceMetadataCaptureService

                service = await asyncio.to_thread(
                    SourceMetadataCaptureService, reader_auth, source_observation_config, root,
                )
                if not reader_auth.active(session) or session.scope() != initial_scope:
                    raise AuthError("Reader session ended before metadata capture")
                result = await service.capture(session)
            except ImportError:
                return setup_block("Source metadata capture service is unavailable; no fallback was selected.")
            except LabError as error:
                return setup_block("Source metadata capture was blocked by its approved service guard.", status=error.status)
            except (OSError, ValueError):
                return setup_block("Fixed source-observation configuration is missing, invalid or unavailable.")
            if not reader_auth.active(session) or session.scope() != initial_scope:
                raise AuthError("Reader session ended during metadata capture")
            if (
                not isinstance(result, dict) or result.get("kind") not in {
                    "SOURCE_METADATA_CAPTURE_ONLY", "SOURCE_QUERY_CALIBRATION_CAPTURE_ONLY",
                    "NATIVE_ONTOLOGY_ANSWER_CAPTURE_ONLY",
                }
                or result.get("ready") is not False
            ):
                return setup_block("Source metadata capture returned an invalid diagnostic contract; no proof was accepted.")
            return result

        @app.get("/auth/login")
        async def login(request: Request):
            if str(request.base_url).rstrip("/") != "http://localhost:8765":
                return RedirectResponse("http://localhost:8765/auth/login", status_code=303)
            key, uri = await asyncio.to_thread(
                reader_auth.begin, request.cookies.get("telco_iq_session"),
            )
            response = RedirectResponse(uri, status_code=303)
            response.delete_cookie("telco_iq_session", path="/")
            response.set_cookie("telco_iq_login", key, httponly=True, samesite="lax", max_age=600, path="/")
            return response

        @app.get("/auth/callback")
        async def callback(request: Request):
            pairs = list(request.query_params.multi_items())
            if len({key for key, _ in pairs}) != len(pairs):
                raise AuthError("Duplicate sign-in response fields rejected")
            session = await asyncio.to_thread(
                reader_auth.finish, request.cookies.get("telco_iq_login"), dict(pairs),
            )
            response = RedirectResponse("/", status_code=303)
            response.delete_cookie("telco_iq_login", path="/")
            response.set_cookie(
                "telco_iq_session", session.key, httponly=True, samesite="lax",
                max_age=max(0, int(session.expires_at - time.time())), path="/",
            )
            return response

        @app.post("/auth/consent/{resource}")
        async def resource_consent(resource: str, request: Request):
            if str(request.base_url).rstrip("/") != "http://localhost:8765":
                raise AuthError("Resource consent requires the canonical http://localhost:8765 origin")
            session = reader_auth.session(request.cookies.get("telco_iq_session"))
            key, uri = await asyncio.to_thread(reader_auth.begin, session.key, resource)
            response = JSONResponse({"resource": resource, "authorization_uri": uri})
            response.set_cookie("telco_iq_login", key, httponly=True, samesite="lax", max_age=600, path="/")
            return response

        @app.post("/auth/authorize-existing/{resource}")
        async def authorize_existing_resource(
            resource: Literal["source_metadata", "storage"], request: Request,
        ):
            if str(request.base_url).rstrip("/") != "http://localhost:8765":
                raise AuthError("Existing-grant validation requires the canonical http://localhost:8765 origin")
            if blocked := await setup_body(request):
                return blocked
            prior = reader_auth.session(request.cookies.get("telco_iq_session"))
            session = await asyncio.to_thread(reader_auth.authorize_existing, prior, resource)
            response = JSONResponse({
                "resource": resource, "status": "EXISTING_GRANT_VALIDATED_CURRENT_SESSION", "ready": False,
                "claim": (
                    "The current reader's existing delegated grant was validated through MSAL. "
                    "No new consent, source access, probe or execution is established."
                ),
            })
            response.delete_cookie("telco_iq_login", path="/")
            response.set_cookie(
                "telco_iq_session", session.key, httponly=True, samesite="lax",
                max_age=max(0, int(session.expires_at - time.time())), path="/",
            )
            return response

        @app.post("/auth/logout")
        async def logout(request: Request):
            reader_auth.revoke(request.cookies.get("telco_iq_session"))
            response = JSONResponse({"signed_out": True})
            response.delete_cookie("telco_iq_session", path="/")
            response.delete_cookie("telco_iq_login", path="/")
            return response

        @app.post("/api/mcp/inventory")
        async def mcp_inventory(request: Request):
            session = reader_auth.session(request.cookies.get("telco_iq_session"))
            token = await asyncio.to_thread(reader_auth.token, session)
            try:
                async with DiscoveredClient.connect(
                    Endpoint("https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq", "iq"),
                    auth=DelegatedBearer(token),
                ) as client:
                    inventory = client.inventory()
            except LabError as error:
                return JSONResponse({"status": error.status, "detail": str(error)}, status_code=409)
            if not reader_auth.active(session):
                raise AuthError("Reader signed out during MCP discovery")
            write_receipt(reader_auth.config.receipt_directory / session.object_id, inventory)
            return {
                "status": "DISCOVERED", "lane": "ACTUAL_GA_IQ_MCP",
                "protocol": inventory["protocol"],
                "tools": [{"name": item["definition"]["name"], "schema_hash": item["schema_hash"]}
                          for item in inventory["tools"]],
                "warning": "Discovery only; not data execution, native ontology or RLS proof.",
            }

    @app.get("/api/health")
    async def health(request: Request):
        session, reader_error = current_reader(request)
        readiness = public_readiness(session, reader_error)
        if agent_surface is not None:
            return {
                "mode": agent_surface.mode, "scope": None, "data_ready": False,
                "context_revision": digest(agent_surface.model_dump(mode="json")),
                "authenticated": False, "live_auth": "NOT_USED_RECORDED_MODE",
                "readiness": {
                    "mode": agent_surface.mode, "ready": False, "status": "RECORDED_ONLY",
                    "blockers": ["Explicit single-surface capture playback; no live execution in this mode."],
                    "claim": "Sanitized agent surface capture with explicit provenance, not a reader grant.",
                },
            }
        if library is not None:
            return {
                "mode": library.mode, "scope": None, "data_ready": False,
                "context_revision": digest(library.model_dump(mode="json")),
                "authenticated": False, "live_auth": "NOT_USED_RECORDED_MODE",
                "readiness": {
                    "mode": library.mode, "ready": False, "status": "RECORDED_ONLY",
                    "blockers": ["Explicit two-stream historical playback; no live execution in this mode."],
                    "claim": "Separate actual small/full native captures with original answer/source clocks, not a reader grant.",
                },
            }
        if comparisons is not None:
            return {
                "mode": comparisons.mode, "scope": None, "data_ready": False,
                "context_revision": digest(comparisons.model_dump(mode="json")),
                "authenticated": False, "live_auth": "NOT_USED_RECORDED_MODE",
                "readiness": {
                    "mode": comparisons.mode, "ready": False, "status": "RECORDED_ONLY",
                    "blockers": ["Explicit historical playback; no live execution in this mode."],
                    "claim": "Original live results and clocks, not a current reader grant or a new source/model call.",
                },
            }
        if recording is not None:
            return {
                "mode": recording.mode, "scope": None, "data_ready": False,
                "context_revision": digest(recording.model_dump(mode="json", exclude_none=True)),
                "planner": "BLOCKED_CAPABILITY", "live_auth": "NOT_USED_RECORDED_MODE",
                "authenticated": False, "readiness": readiness, "provenance": recording.provenance(),
            }
        if reader_auth:
            return {
                "mode": server_mode(), "scope": None,
                "authenticated": session is not None, "csrf_token": session.csrf if session else None,
                "session_epoch": session.epoch if session else None, "signin_url": "/auth/login",
                "data_ready": False,
                "context_revision": "CURRENT_READER_PROBE_REQUIRED" if live_demo is not None else "NOT_CONFIGURED",
                "planner": "CONFIGURED_PROBE_REQUIRED" if live_demo is not None else "BLOCKED_CAPABILITY",
                "live_auth": "BFF_AUTH_CODE_PKCE_NOT_OBO", "readiness": readiness,
                "resource_consent": reader_auth.resource_readiness(session)
                if isinstance(reader_auth, ReaderAuth) else [],
            }
        return {
            "mode": "OFFLINE_SYNTHETIC", "scope": scope.model_dump(),
            "data_ready": (data / "manifest.json").is_file(),
            "context_revision": context.revision,
            "planner": "BLOCKED_CAPABILITY",
            "live_auth": "NOT_CONFIGURED", "authenticated": False, "readiness": readiness,
        }

    @app.get("/api/catalog")
    async def get_catalog(request: Request):
        if recording is not None or comparisons is not None or library is not None or agent_surface is not None:
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY,
                "detail": "Recorded sources are display-only, not the fixture catalogue.",
            }, status_code=409)
        if reader_auth:
            reader_auth.session(request.cookies.get("telco_iq_session"))
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY,
                "detail": "Reader metadata awaits actual permission-aware MCP schema normalization.",
            }, status_code=409)
        return catalog.raw

    @app.get("/api/context", response_model=Context)
    async def get_context(request: Request):
        if recording is not None or comparisons is not None or library is not None or agent_surface is not None:
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY,
                "detail": "The recorded definition is display-only, not executable planner context.",
            }, status_code=409)
        if reader_auth:
            reader_auth.session(request.cookies.get("telco_iq_session"))
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY,
                "detail": "No native context bound; no static context fallback in reader mode.",
            }, status_code=409)
        return context

    @app.get("/api/native-demo", response_model=RecordedDemo)
    async def native_demo():
        if recording is None:
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY,
                "detail": "Start explicitly with --recorded-native; no automatic replay fallback.",
            }, status_code=409)
        return recording

    @app.get("/api/recorded-comparisons", response_model=RecordedComparisons)
    async def recorded_pairs():
        if comparisons is None:
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY,
                "detail": "Start explicitly with --recorded-comparisons; no automatic replay fallback.",
            }, status_code=409)
        return comparisons

    @app.get("/api/demo-library", response_model=RecordedDemoLibrary)
    async def recorded_library_streams():
        if library is None:
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY,
                "detail": "Start explicitly with --recorded-library; no automatic recording or fixture fallback.",
            }, status_code=409)
        return library

    @app.get("/api/agent-surface", response_model=RecordedAgentSurface)
    async def recorded_agent_surface():
        if agent_surface is None:
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY,
                "detail": "Start explicitly with --recorded-library pointing to a surface capture; no automatic fallback.",
            }, status_code=409)
        return agent_surface

    @app.get("/api/capabilities")
    async def capabilities():
        register = json.loads((root / "eval" / "validation" / "register.json").read_text(encoding="utf-8"))
        return {
            "experiments": [
                {k: e[k] for k in ("id", "title", "phase", "status")} for e in register["experiments"]
            ],
            "mode": server_mode(),
            "warning": "Offline tests do not promote live experiment statuses.",
        }

    @app.post("/api/plan")
    async def plan_question():
        return JSONResponse({
            "status": Status.BLOCKED_CAPABILITY,
            "detail": (
                "This submitted-plan endpoint does not invoke the configured application planner. "
                "Use the paired comparison path, which checks current reader and source/native guards before inference."
                if live_demo is not None else
                "Natural-language planning has not run. Choose and approve an application-model "
                "hosting path/model/version and spending cap first. Submit an explicit typed plan "
                "to exercise local validation/execution; that is not LLM planning proof."
            ),
        }, status_code=409)

    @app.get("/api/comparison/readiness")
    async def comparison_readiness(request: Request):
        session, reader_error = current_reader(request)
        return public_readiness(session, reader_error)

    @app.post("/api/compare/stream")
    async def compare_stream(question: ComparisonRequest, request: Request):
        if live_demo is None or reader_auth is None:
            return JSONResponse(unconfigured_readiness(), status_code=409)
        session = reader_auth.session(request.cookies.get("telco_iq_session"))
        readiness = public_readiness(session, None)
        if not readiness.get("ready"):
            return JSONResponse(readiness, status_code=409)
        messages: asyncio.Queue[str | None] = asyncio.Queue()
        diagnostic_events = []

        async def observe(event: PairEvent):
            if event.phase in {"planning_started", "plan_ready", "execution"}:
                diagnostic_events.append(event.model_dump(mode="json", exclude={"outcome"}))
            await messages.put(f"event: pair\ndata: {event.model_dump_json()}\n\n")

        async def stopped(status: Status, detail: str):
            write_receipt(reader_auth.config.receipt_directory / session.object_id, {
                "kind": "ACTUAL_APPLICATION_PAIRED_FAILURE",
                "status": str(status), "detail": detail,
                "question": question.question,
                "diagnostic_events": diagnostic_events if status != Status.BLOCKED_AUTH and reader_auth.active(session) else [],
                "diagnostic_only_not_accepted_answer_or_executable_reader_grant": True,
            })
            await messages.put(f"event: error\ndata: {json.dumps({'status': status, 'detail': detail})}\n\n")

        async def produce():
            try:
                async with live_demo.prepare(session) as comparison:
                    result = await comparison.run(question.question, on_event=observe)
                if not reader_auth.active(session):
                    raise AuthError("Reader signed out during the paired run")
                write_receipt(reader_auth.config.receipt_directory / session.object_id, {
                    "kind": "ACTUAL_APPLICATION_PAIRED_RUN",
                    "result": result.model_dump(mode="json"),
                    "events": diagnostic_events,
                })
                await messages.put(f"event: comparison\ndata: {result.model_dump_json()}\n\n")
            except (LabError, AuthError) as error:
                status = error.status if isinstance(error, LabError) else Status.BLOCKED_AUTH
                await stopped(status, str(error))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                status = Status.FAILED
                detail = (
                    "Live comparison stopped unexpectedly; no result was accepted. See the local server log."
                )
                try:
                    status = mcp_capture_failure_status(error)
                except LabError as bounded:
                    status, detail = bounded.status, str(bounded)
                except Exception:
                    logger.exception("Live comparison stream failed")
                else:
                    detail = "Live comparison source/transport checks failed; no result was accepted."
                await stopped(status, detail)
            finally:
                messages.put_nowait(None)

        async def stream():
            task = asyncio.create_task(produce())
            try:
                while (message := await messages.get()) is not None:
                    if await request.is_disconnected():
                        break
                    yield message
            finally:
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/execute", response_model=ExecutionResult)
    async def execute(plan: Plan, request: Request):
        if blocked := execution_block(request):
            return blocked
        try:
            selected = executor_for(plan)
        except LabError as error:
            return JSONResponse({"status": error.status, "detail": str(error)}, status_code=409)
        result = await selected.execute(plan, scope)
        persist(plan, result)
        return result

    @app.get("/api/fixtures/sla", response_model=Plan)
    async def sla_plan(
        request: Request, period: str = "2026-07",
        view: Literal["contracts", "accounts"] = "contracts",
        incident_key: list[int] | None = Query(default=None),
    ):
        if blocked := execution_block(request):
            return blocked
        from telco_iq.sla_fixture import build_sla_plan

        try:
            return build_sla_plan(scope, period=period, incident_keys=incident_key, view=view)
        except (LabError, ValueError) as error:
            return JSONResponse({
                "status": Status.BLOCKED_CAPABILITY, "detail": str(error),
            }, status_code=400)

    @app.post("/api/execute/stream")
    async def execute_stream(plan: Plan, request: Request):
        if blocked := execution_block(request):
            return blocked
        try:
            selected = executor_for(plan)
        except LabError as error:
            return JSONResponse({"status": error.status, "detail": str(error)}, status_code=409)
        messages: asyncio.Queue[str | None] = asyncio.Queue()

        async def observe(event: ExecutionEvent) -> None:
            await messages.put(f"event: trace\ndata: {event.model_dump_json()}\n\n")

        async def produce() -> None:
            try:
                result = await selected.execute(plan, scope, on_event=observe)
                persist(plan, result)
                await messages.put(f"event: result\ndata: {result.model_dump_json()}\n\n")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Submitted-plan execution stream failed")
                payload = json.dumps({
                    "status": Status.FAILED,
                    "detail": "Execution stopped unexpectedly; no complete answer. See the local server log.",
                })
                await messages.put(f"event: error\ndata: {payload}\n\n")
            finally:
                messages.put_nowait(None)

        async def stream():
            task = asyncio.create_task(produce())
            try:
                while (message := await messages.get()) is not None:
                    if await request.is_disconnected():
                        break
                    yield message
            finally:
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    web = root / "apps" / "web" / "dist"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app
