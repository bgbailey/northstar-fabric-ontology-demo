import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from telco_iq.catalog import Catalog, digest
from telco_iq.contracts import Budgets, Context, Scope
from telco_iq.errors import LabError
from telco_iq.live_contracts import (
    ObservedSourceState,
    ReaderSourceStateLease,
    SourceCallBudget,
    SourceIdentity,
)
from telco_iq.live_demo import (
    ConfiguredLiveGateway,
    DevModelTokenProvider,
    LiveDemoConfig,
    ModelTokenIdentity,
)
from telco_iq.private_evidence import PrivateEvidenceConfig
from telco_iq.source_observation import MetadataRequest, delegated_metadata_requester

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("wrong_field", ["tenantId", "id", "user"])
async def test_model_token_identity_is_explicit_and_checked_before_acquisition(tmp_path, monkeypatch, wrong_field):
    monkeypatch.setattr("telco_iq.live_demo.shutil.which", lambda name: "synthetic-az-not-executed")
    identity = ModelTokenIdentity(tenant_id="unit-tenant", subscription_id="unit-subscription",
                                  expected_user="unit-dev-user", azure_config_directory=tmp_path)
    provider = DevModelTokenProvider(identity, tmp_path / "synthetic-repository")
    calls = []
    observed = {"tenantId": "unit-tenant", "id": "unit-subscription", "user": {"name": "unit-dev-user"}}
    observed[wrong_field] = {"name": "other"} if wrong_field == "user" else "other"

    async def call(*arguments):
        calls.append(arguments)
        return observed

    monkeypatch.setattr(provider, "_call", call)
    with pytest.raises(LabError, match="identity differs"):
        await provider()
    assert len(calls) == 1


async def test_model_tokens_use_the_explicit_model_resource_not_a_source_connector(tmp_path, monkeypatch):
    monkeypatch.setattr("telco_iq.live_demo.shutil.which", lambda name: "synthetic-az-not-executed")
    provider = DevModelTokenProvider(ModelTokenIdentity(
        tenant_id="unit-tenant", subscription_id="unit-subscription",
        expected_user="unit-dev-user", azure_config_directory=tmp_path,
    ), tmp_path / "synthetic-repository")
    calls = []

    async def call(*arguments):
        calls.append(arguments)
        return ({"tenantId": "unit-tenant", "id": "unit-subscription", "user": {"name": "unit-dev-user"}}
                if arguments[:2] == ("account", "show") else {"accessToken": "synthetic-unit-token"})

    monkeypatch.setattr(provider, "_call", call)
    assert await provider() == "synthetic-unit-token"
    assert "https://ai.azure.com/.default" in calls[-1]
    assert "--subscription" in calls[-1] and "--tenant" not in calls[-1]
    assert all("login" not in arguments for arguments in calls)


def test_model_token_can_pin_the_reviewed_isolated_cli_without_ambient_selection(tmp_path, monkeypatch):
    executable = tmp_path / "synthetic-cli-not-executed.exe"
    executable.write_bytes(b"synthetic not executable")
    def ambient(name):
        raise AssertionError("Explicit isolated CLI must not consult ambient executable")
    monkeypatch.setattr("telco_iq.live_demo.shutil.which", ambient)
    identity = ModelTokenIdentity(
        tenant_id="unit", subscription_id="unit", expected_user="unit",
        azure_config_directory=tmp_path, azure_cli_executable=executable,
    )
    provider = DevModelTokenProvider(identity, tmp_path / "synthetic-repository")
    assert provider.azure == str(executable)
    identity.azure_cli_executable = tmp_path / "missing.exe"
    with pytest.raises(LabError, match="no fallback"):
        DevModelTokenProvider(identity, tmp_path / "synthetic-repository")


def test_model_token_cache_cannot_be_repository_or_onedrive(monkeypatch):
    monkeypatch.setattr("telco_iq.live_demo.shutil.which", lambda name: "synthetic-az-not-executed")
    identity = ModelTokenIdentity(tenant_id="unit", subscription_id="unit", expected_user="unit",
                                  azure_config_directory=ROOT)
    with pytest.raises(LabError, match="outside repository"):
        DevModelTokenProvider(identity, ROOT)


@pytest.mark.parametrize("budget_field", ["max_queries", "max_rows", "max_bytes", "timeout_seconds"])
@pytest.mark.parametrize("source_mode", ["per_query", "paired_boundary_lease"])
@pytest.mark.parametrize("metadata_mode", [False, True])
@pytest.mark.parametrize("arm_mode", ["sequential", "parallel"])
@pytest.mark.parametrize("admission_seconds", [0, 3])
@pytest.mark.parametrize("source_timeout_mode", ["whole_prepare", "per_phase"])
async def test_configured_gateway_wires_same_reader_binding_and_separate_resources(
    tmp_path, monkeypatch, budget_field, source_mode, metadata_mode, arm_mode, admission_seconds, source_timeout_mode,
):
    """Wiring regression with explicit synthetic collaborators; no external requests."""
    now = datetime.now(timezone.utc)
    identity = SourceIdentity(workspace_id="00000000-0000-0000-0000-000000000001",
                              dataset_id="00000000-0000-0000-0000-000000000002")
    files = {}
    for name in ("model", "approval", "snapshot", "enrollment", "capture", "profiles"):
        files[name] = tmp_path / f"{name}.json"
        files[name].write_text("{}")
    configuration = dict(
        model_config_file=files["model"], model_approval_file=files["approval"],
        model_token_identity=ModelTokenIdentity(tenant_id="unit-tenant", subscription_id="unit-subscription",
                                                expected_user="unit-dev-user", azure_config_directory=tmp_path),
        native_snapshot_file=files["snapshot"], native_enrollment_file=files["enrollment"],
        enrollment_digest="a" * 64, original_native_receipt_file=files["capture"],
        source_profiles_file=files["profiles"], state="CONFIGURED", source_state_mode=source_mode,
        arm_execution_mode=arm_mode, source_timeout_mode=source_timeout_mode,
    )
    if source_timeout_mode == "per_phase" and (source_mode != "paired_boundary_lease" or arm_mode != "parallel"):
        with pytest.raises(ValueError, match="shared plan-selected parallel boundary lease"):
            LiveDemoConfig(**configuration)
        return
    config = LiveDemoConfig(**configuration)
    if source_timeout_mode == "whole_prepare":
        assert "source_timeout_mode" not in config.model_dump(mode="json")
    else:
        assert config.model_dump(mode="json")["source_timeout_mode"] == "per_phase"
    scope = Scope(tenant="unit-tenant", principal="unit-reader", authorization_revision="unit-epoch",
                  models=[], mode="LIVE_DELEGATED")
    session = SimpleNamespace(scope=lambda: scope, object_id="unit-reader")
    token_calls = []

    class Reader:
        config = SimpleNamespace(
            tenant_id="unit-tenant", expires_at=now + timedelta(hours=1),
            ontology_resource=object(), model_ids={"subscriptions": identity.dataset_id}, receipt_directory=tmp_path,
        )

        def active(self, current):
            return current is session

        def token(self, current, resource="iq"):
            assert current is session
            token_calls.append(resource)
            return "synthetic-" + resource

    model = SimpleNamespace(
        state="CONFIGURED", max_requests=2, maximum_request_charge=Decimal("1"), operational_cap=Decimal("2"),
        artifact_context_digest="", target_digest="target", artifact_digest="artifact", timeout_seconds=7,
        maximum_invocation_seconds=7 + admission_seconds,
    )
    approval = SimpleNamespace(authorize=lambda *args: None)
    snapshot = SimpleNamespace(
        verify=lambda value: None, context=SimpleNamespace(scope="unit-diagnostic"),
        content_hash="b" * 64, evidence_hashes={"native_mcp_receipt": digest({}), "catalog": ""},
        execution_contracts=["synthetic-reviewed-edge"],
    )
    enrollment = SimpleNamespace(
        expires_at=now + timedelta(hours=1), reviewed_at=now, enrollment_digest=config.enrollment_digest,
        native_snapshot_hash=snapshot.content_hash, sources={"subscriptions": identity},
        source_schema_hashes={"subscriptions": "c" * 64},
        ontology_workspace_id=identity.workspace_id, ontology_item_id=UUID("00000000-0000-0000-0000-000000000003"),
    )
    preparation_calls = []
    source_instructions = {"subscriptions": "SYNTHETIC source-owned single-period snapshots."}

    def verified_preparation(repo, sources, hashes):
        preparation_calls.append((repo, sources, hashes))
        return source_instructions

    profiles = SimpleNamespace(
        sources={"subscriptions": SimpleNamespace(build=lambda: "unit-source-profile")},
        schemas={"subscriptions": SimpleNamespace(build=lambda: "unit-schema-profile")},
        states={"subscriptions": "unit-state-profile"},
        native_hashes={"list_ontology_entities": "e" * 64, "list_ontology_rules": "f" * 64},
        native_arguments={"list_ontology_entities": {}, "list_ontology_rules": {}},
        model_dump=lambda **kwargs: {"unit": "synthetic-profiles"},
        verified_source_instructions=verified_preparation,
        private_evidence=PrivateEvidenceConfig(
            directory=tmp_path / "source-evidence", approval_reference="synthetic-approved-private-store",
            expires_at=now + timedelta(hours=1), max_capture_bytes=32_000, max_total_bytes=64_000, max_records=4,
        ),
        coverage={},
    )
    if metadata_mode:
        profiles.states = {}
        profiles.metadata_states = {"subscriptions": SimpleNamespace(profile_hash="a" * 64)}
        profiles.metadata_state_pins = {"subscriptions": "a" * 64}
        config.catalog_file = tmp_path / "private-source-catalog.json"
        reviewed_catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json").raw
        service = next(table for table in reviewed_catalog["tables"] if table["id"] == "service_month")
        service["columns"].update(expected_minutes="int64", coverage_complete="bool")
        config.catalog_file.write_text(json.dumps(reviewed_catalog), encoding="utf-8")
    monkeypatch.setattr("telco_iq.live_demo.AzureOpenAIConfig.load", lambda path: model)
    monkeypatch.setattr("telco_iq.live_demo.Approval.model_validate_json", lambda text: approval)
    monkeypatch.setattr("telco_iq.live_demo.NativeSnapshot.model_validate_json", lambda text: snapshot)
    monkeypatch.setattr("telco_iq.live_demo.ReviewedReaderEnrollment.model_validate_json", lambda text: enrollment)
    monkeypatch.setattr("telco_iq.live_demo.MCPProfiles.read", lambda *args: profiles)
    monkeypatch.setattr(
        "telco_iq.live_demo.PrivateEvidenceSink",
        lambda *args, **kwargs: SimpleNamespace(ready=lambda: None),
    )
    connections = []

    @asynccontextmanager
    async def connect(endpoint, **kwargs):
        connections.append((endpoint, kwargs))
        yield SimpleNamespace(endpoint=endpoint, reader_scope=kwargs["reader_scope"])

    monkeypatch.setattr("telco_iq.live_demo.DiscoveredClient.connect", connect)
    http_options = []

    @asynccontextmanager
    async def metadata_http(**kwargs):
        http_options.append(kwargs)
        yield SimpleNamespace()

    monkeypatch.setattr("telco_iq.live_demo.httpx.AsyncClient", metadata_http)
    sinks = []

    state_calls, native_probe_states = [], []
    async def unit_state(binding, alias):
        state_calls.append(alias)
        return ObservedSourceState(data_revision="synthetic-data", model_revision="synthetic-model",
                                   evidence_hash="e" * 64, observed_at=datetime.now(timezone.utc))
    def observe_state(*args, **kwargs):
        sinks.append(kwargs["evidence_sink"])
        return unit_state

    monkeypatch.setattr("telco_iq.live_demo.state_probe", observe_state)
    def native_probe(*args, **kwargs):
        native_probe_states.append(kwargs["state_probe"])
        return "unit-reader-probe"
    monkeypatch.setattr("telco_iq.live_demo.ReaderProbe", native_probe)
    acquired = []

    async def acquire(received_snapshot, binding, **kwargs):
        assert received_snapshot is snapshot and kwargs["probe"] == "unit-reader-probe"
        assert kwargs["session_active"](binding.scope)
        assert kwargs["enrollment_digest"] == config.enrollment_digest
        acquired.append(binding)
        context = Context(mode="NATIVE_SNAPSHOT", revision="unit-reader-context", source="synthetic wiring",
                          scope=binding.scope, relationships=[], rules=[], provenance=["synthetic-unit"])
        return SimpleNamespace(context=context)

    monkeypatch.setattr("telco_iq.live_demo.acquire_reader_context", acquire)
    revalidated = []

    async def revalidate(bundle, **kwargs):
        assert bundle.context.scope == kwargs["scope"]
        assert kwargs["probe"] == "unit-reader-probe"
        revalidated.append(kwargs["scope"])

    monkeypatch.setattr("telco_iq.live_demo.revalidate_reader_context", revalidate)
    monkeypatch.setattr("telco_iq.live_demo.DevModelTokenProvider", lambda *args: "unit-model-token-provider")
    model_provider = SimpleNamespace(provider="synthetic-unit", model_version="unit-model")
    monkeypatch.setattr("telco_iq.live_demo.AzureOpenAICompletion", lambda *args, **kwargs: model_provider)
    source_bindings = []
    populations = []

    def query_provider(catalog, client, binding, revision, **kwargs):
        source_bindings.append(binding)
        assert kwargs["session_active"](binding.scope)
        assert kwargs["evidence_sink"] is sinks[0]
        assert kwargs["source_populations"] is populations
        assert isinstance(kwargs["state_probe"], ReaderSourceStateLease) == (source_mode == "paired_boundary_lease")
        return SimpleNamespace(mode="LIVE_DELEGATED")

    monkeypatch.setattr("telco_iq.live_demo.MCPQueryProvider", query_provider)
    gateway = ConfiguredLiveGateway(config, Reader(), ROOT)
    if metadata_mode:
        assert {"expected_minutes", "coverage_complete"} <= gateway.catalog.tables["service_month"]["columns"].keys()
        assert gateway.catalog.revision != Catalog(ROOT / "data" / "contracts" / "catalog.json").revision
    current_scope = scope.model_copy(update={"models": ["subscriptions"]})
    gateway.source_proof = SimpleNamespace(
        scope=current_scope, validate_for=lambda **kwargs: populations,
        sources={"subscriptions": SimpleNamespace(
            data_revision="synthetic-data", model_revision="synthetic-model",
            **{name: "a" * 64 for name in (
                "source_capture_id", "source_response_hash", "deployment_packet_hash", "source_manifest_file_hash",
                "load_assertion_hash", "model_inventory_hash",
            )},
            evidence_ids=lambda: ["a" * 64] * 6,
        )},
    )
    gateway.population_guard = lambda current, sink: lambda: gateway.population_records(current, sink)
    config.source_proof_digest = "d" * 64
    snapshot.evidence_hashes["catalog"] = gateway.catalog.revision
    model.artifact_context_digest = gateway.context_artifact_digest
    assert gateway.readiness()["ready"]
    async with gateway.prepare(session) as comparison:
        assert comparison.arm_execution_mode == arm_mode
        if source_mode == "paired_boundary_lease":
            assert native_probe_states == [None]
            assert not state_calls
            states = await comparison.planned_source_state_probe({"subscriptions"})
            assert revalidated == [comparison.scope]
            original_times = {alias: value.observed_at for alias, value in states.items()}
            await comparison.source_state_finalize()
            assert state_calls == ["subscriptions", "subscriptions"]
            assert original_times == {alias: value.observed_at for alias, value in states.items()}
        else:
            assert native_probe_states == [unit_state] and comparison.source_state_finalize is None
            assert not revalidated
        executor = comparison.executor_factory("native")
        assert executor.source_populations is populations
        assert comparison.scope.models == ["subscriptions"]
        assert comparison.planner.provider is model_provider
        assert comparison.planner.source_instructions == source_instructions
        assert comparison.planner.isolated_planning_phase == (source_mode == "paired_boundary_lease")
    assert preparation_calls == [(ROOT, enrollment.sources, enrollment.source_schema_hashes)]
    assert token_calls == ["iq", "ontology"] + (["source_metadata", "storage"] if metadata_mode else [])
    assert http_options == (
        [{"follow_redirects": False, "timeout": config.budgets.timeout_seconds}] if metadata_mode else []
    )
    assert connections[0][0].kind == "iq" and connections[1][0].kind == "ontology"
    planning_windows = 1 if arm_mode == "parallel" else 2
    source_windows = 4 if source_timeout_mode == "per_phase" else 1
    session_timeout = config.budgets.timeout_seconds * source_windows + (
        model.maximum_invocation_seconds * planning_windows if source_mode == "paired_boundary_lease" else 0
    )
    assert all(options["timeout"] == session_timeout for _, options in connections)
    assert str(enrollment.ontology_item_id) in connections[1][0].url
    assert source_bindings[0] is acquired[0]
    assert source_bindings[0].scope.authorization_revision == "unit-epoch"
    original_budget = getattr(config.budgets, budget_field)
    setattr(config.budgets, budget_field, original_budget + 1)
    assert gateway.context_artifact_digest != model.artifact_context_digest
    assert not gateway.readiness()["ready"]
    setattr(config.budgets, budget_field, original_budget)
    assert gateway.readiness()["ready"]
    config.source_state_mode = "per_query" if source_mode == "paired_boundary_lease" else "paired_boundary_lease"
    assert gateway.context_artifact_digest != model.artifact_context_digest
    assert not gateway.readiness()["ready"]
    config.source_state_mode = source_mode
    config.arm_execution_mode = "parallel" if arm_mode == "sequential" else "sequential"
    assert gateway.context_artifact_digest != model.artifact_context_digest
    assert not gateway.readiness()["ready"]
    config.arm_execution_mode = arm_mode
    profiles.private_evidence = None
    assert not gateway.readiness()["ready"]
    assert any("private evidence" in message for message in gateway.readiness()["blockers"])


async def test_slow_loopback_metadata_uses_approved_budget_instead_of_hidden_five_second_default():
    """A real loopback socket reproduces HTTPX defaults; no tenant or model request."""
    delay = 5.5

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer synthetic-unit-only"
            time.sleep(delay)
            body = b'{"synthetic":true}'
            with suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scope = Scope(
        tenant="synthetic", principal="synthetic", authorization_revision="synthetic",
        models=["synthetic"], mode="LIVE_DELEGATED",
    )
    request = MetadataRequest("synthetic-slow", "iq", "GET", f"http://127.0.0.1:{server.server_port}/")
    tokens = {"iq": "synthetic-unit-only"}
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            budget = SourceCallBudget(Budgets(timeout_seconds=9))
            requester = delegated_metadata_requester(client, scope, tokens, lambda current: current == scope)
            async with asyncio.timeout(budget.remaining_seconds()):
                with pytest.raises(httpx.ReadTimeout):
                    await requester(request)
        assert budget.remaining_seconds() > 0
        limits = Budgets(timeout_seconds=9)
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=limits.timeout_seconds, trust_env=False,
        ) as client:
            budget = SourceCallBudget(limits)
            requester = delegated_metadata_requester(client, scope, tokens, lambda current: current == scope)
            async with asyncio.timeout(budget.remaining_seconds()):
                response = await requester(request)
        assert response.status == 200 and response.body == b'{"synthetic":true}'
        assert response.actor_scope == scope and response.resource == "iq"
        assert budget.remaining_seconds() > 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_metadata_remaining_budget_still_cancels_a_slow_transport():
    scope = Scope(
        tenant="synthetic", principal="synthetic", authorization_revision="synthetic",
        models=["synthetic"], mode="LIVE_DELEGATED",
    )
    limits = Budgets(timeout_seconds=0.03)

    async def delayed(request):
        await asyncio.sleep(1)
        return httpx.Response(200, content=b'{"synthetic":true}')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(delayed), follow_redirects=False, timeout=limits.timeout_seconds,
    ) as client:
        budget = SourceCallBudget(limits)
        requester = delegated_metadata_requester(
            client, scope, {"iq": "synthetic-unit-only"}, lambda current: current == scope,
        )
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(budget.remaining_seconds()):
                await requester(MetadataRequest("synthetic-slow", "iq", "GET", "http://127.0.0.1/"))
