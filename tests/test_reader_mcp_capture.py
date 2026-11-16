"""Synthetic current-reader MCP bootstrap; no real credentials, tools or source calls."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from telco_iq.approval import Approval
from telco_iq.contracts import Budgets, Scope, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import SourceIdentity
from telco_iq.mcp_client import MCPToolError
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink
from telco_iq.reader_mcp_capture import (
    ReaderMCPMetadataCaptureConfig,
    capture_reader_mcp_metadata,
    producer_code_hash,
)
from telco_iq.source_observation import SourceMetadataCaptureService

ROOT = Path(__file__).resolve().parents[1]


def setup(tmp_path, monkeypatch, *, change=None, query_mode=False):
    scope = Scope(
        tenant="synthetic-tenant", principal="synthetic-reader", authorization_revision="synthetic-epoch",
        mode="LIVE_DELEGATED", models=["finance", "subscriptions"],
    )
    sources = {
        alias: SourceIdentity(workspace_id="00000000-0000-0000-0000-000000000001",
                              dataset_id=f"00000000-0000-0000-0000-{index:012d}")
        for index, alias in enumerate(scope.models, start=2)
    }
    now = datetime.now(timezone.utc)
    config = ReaderMCPMetadataCaptureConfig(
        scope=scope, ontology_workspace_id="00000000-0000-0000-0000-000000000001",
        ontology_item_id="00000000-0000-0000-0000-000000000004", native_snapshot_hash="a" * 64,
        sources=sources, native_hashes={"list_ontology_entities": "b" * 64, "list_ontology_rules": "c" * 64},
        native_arguments={"list_ontology_entities": {}, "list_ontology_rules": {}},
        schema_tool_hash="d" * 64,
        producer_code_hash=producer_code_hash(),
        schema_arguments={alias: {"artifactId": str(source.dataset_id)} for alias, source in sources.items()},
        budgets=Budgets(max_queries=8, max_bytes=1000000, timeout_seconds=30),
        private_evidence=PrivateEvidenceConfig(
            directory=tmp_path / "private", approval_reference="Synthetic store only",
            expires_at=now + timedelta(hours=1), max_capture_bytes=100000,
            max_total_bytes=1000000, max_records=20,
        ),
        approval=Approval(
            target_alias="APPROVED_LAB_ONLY", target_digest="pending", artifact_digest="pending",
            operations=["reader-mcp-metadata-capture"], estimated_cost=0, cap=0, currency="USD",
            expires_at=now + timedelta(hours=1), ttl_hours=1, teardown_owner="Synthetic test",
            teardown_mechanism="Finite mock calls, no resources", confirmation_receipt="Synthetic only",
        ),
        effects="NATIVE_ENTITY_RULE_SOURCE_SCHEMA_METADATA_ONLY",
    )
    config.approval.artifact_digest, config.approval.target_digest = config.artifact_digest, config.target_digest
    state = SimpleNamespace(active=True, token_calls=[], connections=[], tool_calls=[], discovery_calls=0)
    session = SimpleNamespace(scope=lambda: scope.model_copy(update={"models": []}))
    reader = SimpleNamespace(
        active=lambda current: current is session and state.active,
        config=SimpleNamespace(model_ids={alias: source.dataset_id for alias, source in sources.items()}),
    )

    def token(current, resource):
        assert current is session
        state.token_calls.append(resource)
        return "synthetic-" + resource + "-opaque-token"

    reader.token = token

    @asynccontextmanager
    async def connect(endpoint, **kwargs):
        endpoint.validate()
        if query_mode:
            assert kwargs.get("single_attempt") is True
        state.connections.append(endpoint)
        if change in {"http-auth", "http-group", "unexpected-group"} and (
            endpoint.kind == "ontology" or query_mode
        ):
            request = httpx.Request("POST", endpoint.url)
            error = httpx.HTTPStatusError(
                "synthetic sensitive upstream detail", request=request, response=httpx.Response(403, request=request),
            )
            if change == "http-auth":
                raise error
            if change == "http-group":
                raise ExceptionGroup("synthetic SDK wrapper", [error])
            raise ExceptionGroup("unexpected SDK defect", [ValueError("synthetic unexpected")])
        hashes = dict(kwargs["reviewed_hashes"])
        client = SimpleNamespace(
            endpoint=endpoint, reader_scope=kwargs["reader_scope"],
            schema_hash=lambda name: ("e" * 64 if (
                change == "schema" and (endpoint.kind == "ontology" or query_mode)
                or change == "late-schema" and state.discovery_calls
            ) else hashes[name]),
            inventory=lambda: {"protocol": "synthetic-unit-only", "tools": sorted(hashes)},
        )
        if change == "client-scope":
            client.reader_scope = scope.model_copy(update={"authorization_revision": "other"})
        async def call(name, arguments, capture):
            assert name in ({"ExecuteQuery"} if query_mode else {
                "list_ontology_entities", "list_ontology_rules", "GetSemanticModelSchema",
            })
            state.tool_calls.append((endpoint.kind, name, arguments))
            values = {"values": []} if name.startswith("list_") else {"synthetic_schema_only": True}
            if change == "secret" and (name == "list_ontology_entities" or query_mode):
                values = {"Authorization": "synthetic-unsafe-echo"}
            content = []
            if query_mode:
                table = {} if change == "unknown-rows" else {"rows": [["count", None, 1], ["data", 7, 1]]}
                content = [TextContent(type="text", text=json.dumps({"executionResult": {"tables": [table]}}))]
            result = CallToolResult(content=content, structuredContent=values, isError=change in {
                "tool-error", "wrapped-tool-error",
            })
            if change != "no-capture":
                capture(name, arguments, result.model_dump(mode="json", exclude_none=query_mode), "MCP_TOOL_RESULT")
            if change == "logout":
                state.active = False
            if change == "timeout":
                await asyncio.sleep(0.1)
            if result.isError:
                raise MCPToolError("Synthetic actual-client isError rejection")
            if change in {"sdk-error", "wrapped-sdk-error"}:
                raise LabError("Synthetic prevalidation failure", Status.BLOCKED_CAPABILITY)
            return result
        async def discover():
            state.discovery_calls += 1
        client.call, client.discover = call, discover
        try:
            yield client
        except LabError as error:
            if change in {"wrapped-tool-error", "wrapped-sdk-error", "wrapped-budget-error"}:
                raise ExceptionGroup(
                    "Synthetic outer SDK TaskGroup", [ExceptionGroup("Synthetic inner SDK TaskGroup", [error])],
                ) from None
            raise

    monkeypatch.setattr("telco_iq.reader_mcp_capture.DiscoveredClient.connect", connect)
    return config, reader, session, state


async def test_fixed_existing_capture_seam_bootstraps_native_and_source_metadata_without_provider_or_grant(
    tmp_path, monkeypatch,
):
    config, reader, session, state = setup(tmp_path, monkeypatch)
    path = tmp_path / "fixed.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    service = SourceMetadataCaptureService(reader, path, ROOT)
    assert not state.token_calls and not state.connections
    result = await service.capture(session)
    assert result["kind"] == "SOURCE_METADATA_CAPTURE_ONLY" and result["ready"] is False
    assert result["status"] == "METADATA_CAPTURED_NOT_READER_GRANT"
    assert state.token_calls == ["ontology", "iq"]
    assert [endpoint.kind for endpoint in state.connections] == ["ontology", "iq"]
    assert len(result["capture_hashes"]) == 6 and len(state.tool_calls) == 4
    sink = PrivateEvidenceSink(config.private_evidence, ROOT)
    for key in result["capture_hashes"]:
        raw = sink.read(key)
        assert raw["capture_purpose"] == "CURRENT_READER_NATIVE_SCHEMA_METADATA_ONLY"
        assert raw["response_kind"] in {"MCP_TOOL_RESULT", "MCP_INVENTORY_PROJECTION"}
        assert raw["context"]["kind"] in {
            raw["capture_purpose"], "CURRENT_READER_MCP_INVENTORY_PROJECTION",
        }
        assert "synthetic-ontology-opaque-token" not in json.dumps(sink.derivative(key))
    assert not any(name in {"ExecuteQuery", "ask_ontology"} for _, name, _ in state.tool_calls)


@pytest.mark.parametrize("change", ["epoch", "models", "approval", "expired", "artifact", "code"])
async def test_wrong_reader_or_approval_stops_before_any_token_or_endpoint(tmp_path, monkeypatch, change):
    config, reader, session, state = setup(tmp_path, monkeypatch)
    if change == "epoch":
        session.scope = lambda: config.scope.model_copy(update={"authorization_revision": "other", "models": []})
    elif change == "models":
        reader.config.model_ids["finance"] = config.sources["subscriptions"].dataset_id
    elif change == "approval":
        config.approval.operations = ["unrelated"]
    elif change == "expired":
        config.approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif change == "code":
        config.producer_code_hash = "0" * 64
        config.approval.artifact_digest = config.artifact_digest
    else:
        config.budgets.max_queries = 7
    with pytest.raises(LabError):
        await capture_reader_mcp_metadata(reader, config, ROOT, session)
    assert not state.token_calls and not state.connections and not state.tool_calls


@pytest.mark.parametrize("change", ["schema", "client-scope", "tool-error", "secret", "sdk-error"])
async def test_metadata_failures_are_explicit_partial_captures_never_readiness_or_fallback(
    tmp_path, monkeypatch, change,
):
    config, reader, session, state = setup(tmp_path, monkeypatch, change=change)
    result = await capture_reader_mcp_metadata(reader, config, ROOT, session)
    assert result["ready"] is False and result["status"] == "PARTIAL_METADATA_CAPTURE"
    assert any(outcome["status"] != "CAPTURED" for outcome in result["outcomes"])
    assert not any(name in {"ExecuteQuery", "ask_ontology"} for _, name, _ in state.tool_calls)
    if change == "sdk-error":
        assert result["capture_hashes"], "Failed post-capture metadata originals must remain referenced"
    if change == "secret":
        assert all("synthetic-unsafe-echo" not in path.read_text()
                   for path in config.private_evidence.directory.glob("tq005-*.json"))


async def test_logout_cannot_publish_capture_success_or_continue_other_endpoint(tmp_path, monkeypatch):
    config, reader, session, state = setup(tmp_path, monkeypatch, change="logout")
    with pytest.raises(LabError) as error:
        await capture_reader_mcp_metadata(reader, config, ROOT, session)
    assert error.value.status == Status.BLOCKED_AUTH
    assert len(state.tool_calls) == 1 and len(state.connections) == 1


@pytest.mark.parametrize("change", ["http-auth", "http-group"])
async def test_known_transport_denial_is_explicit_and_does_not_masquerade_as_reader_success(tmp_path, monkeypatch, change):
    config, reader, session, state = setup(tmp_path, monkeypatch, change=change)
    result = await capture_reader_mcp_metadata(reader, config, ROOT, session)
    assert result["status"] == "PARTIAL_METADATA_CAPTURE" and not result["ready"]
    assert result["outcomes"][0]["status"] == Status.BLOCKED_AUTH
    assert "sensitive" not in json.dumps(result)
    assert [name for _, name, _ in state.tool_calls] == ["GetSemanticModelSchema", "GetSemanticModelSchema"]


async def test_unknown_sdk_defect_is_not_swallowed_as_a_successful_or_partial_capture(tmp_path, monkeypatch):
    config, reader, session, _ = setup(tmp_path, monkeypatch, change="unexpected-group")
    with pytest.raises(ExceptionGroup):
        await capture_reader_mcp_metadata(reader, config, ROOT, session)


async def test_real_client_tool_error_is_retained_and_reported_without_escaping_sdk_context(tmp_path, monkeypatch):
    config, reader, session, state = setup(tmp_path, monkeypatch, change="wrapped-tool-error")
    result = await capture_reader_mcp_metadata(reader, config, ROOT, session)
    assert result["status"] == "PARTIAL_METADATA_CAPTURE" and result["ready"] is False
    assert len(result["outcomes"]) == 4 and all(item["status"] == "TOOL_ERROR" for item in result["outcomes"])
    assert len(result["capture_hashes"]) == 6 and len(state.tool_calls) == 4
    sink = PrivateEvidenceSink(config.private_evidence, ROOT)
    for outcome in result["outcomes"]:
        assert all(sink.read(key)["response"]["isError"] is True for key in outcome["capture_hashes"])


async def test_known_lab_error_in_nested_sdk_taskgroups_is_explicit_partial_not_http500(tmp_path, monkeypatch):
    config, reader, session, _ = setup(tmp_path, monkeypatch, change="wrapped-sdk-error")
    result = await capture_reader_mcp_metadata(reader, config, ROOT, session)
    assert result["status"] == "PARTIAL_METADATA_CAPTURE" and not result["ready"]
    assert all(item["status"] == Status.BLOCKED_CAPABILITY for item in result["outcomes"])
    assert len(result["capture_hashes"]) == 4


async def test_nested_sdk_budget_failure_stops_before_another_endpoint(tmp_path, monkeypatch):
    config, reader, session, state = setup(tmp_path, monkeypatch, change="wrapped-budget-error")
    config.budgets.max_bytes = 1
    config.approval.artifact_digest = config.artifact_digest
    with pytest.raises(LabError) as error:
        await capture_reader_mcp_metadata(reader, config, ROOT, session)
    assert error.value.status == Status.PARTIAL
    assert len(state.connections) == 1 and not state.tool_calls


async def test_timeout_is_a_typed_block_not_invalid_config_or_success(tmp_path, monkeypatch):
    config, reader, session, _ = setup(tmp_path, monkeypatch, change="timeout")
    config.budgets.timeout_seconds = 0.05
    config.approval.artifact_digest = config.artifact_digest
    with pytest.raises(LabError) as error:
        await capture_reader_mcp_metadata(reader, config, ROOT, session)
    assert error.value.status == Status.TIMED_OUT


@pytest.mark.parametrize("kind", ["requests", "bytes", "retention"])
async def test_exhausted_global_budget_or_store_does_not_start_another_endpoint(tmp_path, monkeypatch, kind):
    config, reader, session, state = setup(tmp_path, monkeypatch)
    if kind == "requests":
        config.budgets.max_queries = 1
    elif kind == "bytes":
        config.budgets.max_bytes = 1
    else:
        config.private_evidence.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    config.approval.artifact_digest = config.artifact_digest
    with pytest.raises(LabError):
        await capture_reader_mcp_metadata(reader, config, ROOT, session)
    assert not state.tool_calls and len(state.connections) <= 1


@pytest.mark.parametrize("change", ["ask", "execute", "wrong-model", "reserved-alias", "schema-filter"])
def test_bootstrap_config_cannot_select_query_mutation_or_another_source(tmp_path, monkeypatch, change):
    config, _, _, _ = setup(tmp_path, monkeypatch)
    values = config.model_dump(mode="json")
    if change == "ask":
        values["native_hashes"]["ask_ontology"] = "f" * 64
    elif change == "execute":
        values["native_arguments"]["ExecuteQuery"] = {}
    elif change == "wrong-model":
        values["schema_arguments"]["finance"]["artifactId"] = str(config.sources["subscriptions"].dataset_id)
    elif change == "schema-filter":
        values["schema_arguments"]["finance"]["queries"] = ["tables[0]"]
    else:
        values["sources"]["ontology"] = values["sources"].pop("finance")
    with pytest.raises(ValidationError):
        ReaderMCPMetadataCaptureConfig.model_validate(values)


@pytest.mark.parametrize("tool", ["ExecuteQuery", "SourceMetadata", "ask_ontology", "updateDefinition"])
def test_metadata_retention_purpose_never_accepts_business_query_or_mutation_tool(tmp_path, monkeypatch, tool):
    config, _, _, _ = setup(tmp_path, monkeypatch)
    sink = PrivateEvidenceSink(config.private_evidence, ROOT)
    with pytest.raises(LabError):
        sink.capture(
            tool=tool, source_alias="finance", request={}, response={}, context={},
            private_identifiers=(), synthetic=True, metadata=True,
        )
    assert not list(config.private_evidence.directory.glob("tq005-*.json"))


def test_business_evidence_path_does_not_silently_accept_metadata_tools(tmp_path, monkeypatch):
    config, _, _, _ = setup(tmp_path, monkeypatch)
    sink = PrivateEvidenceSink(config.private_evidence, ROOT)
    with pytest.raises(LabError):
        sink.capture(
            tool="GetSemanticModelSchema", source_alias="finance", request={}, response={}, context={},
            private_identifiers=(), synthetic=True,
        )
    assert not list(config.private_evidence.directory.glob("tq005-*.json"))
