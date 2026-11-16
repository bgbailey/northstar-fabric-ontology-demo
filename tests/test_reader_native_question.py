"""Explicit synthetic native-answer transport; no live model, tenant or answer claim."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from telco_iq.approval import Approval
from telco_iq.catalog import digest
from telco_iq.contracts import Budgets, Scope, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import SourceIdentity
from telco_iq.mcp_client import DiscoveredClient, Endpoint, MCPToolError
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink
from telco_iq.reader_native_question import (
    ReaderNativeQuestionConfig,
    capture_reader_native_question,
    producer_code_hash,
)
from telco_iq.source_observation import SourceMetadataCaptureService


def case(tmp_path, monkeypatch, change=None):
    scope = Scope(
        tenant="SYNTHETIC_TENANT", principal="SYNTHETIC_READER", authorization_revision="SYNTHETIC_EPOCH",
        models=["finance"], mode="LIVE_DELEGATED",
    )
    source = SourceIdentity(
        workspace_id="00000000-0000-0000-0000-000000000001",
        dataset_id="00000000-0000-0000-0000-000000000002",
    )
    now = datetime.now(timezone.utc)
    config = ReaderNativeQuestionConfig(
        scope=scope, ontology_workspace_id=source.workspace_id,
        ontology_item_id="00000000-0000-0000-0000-000000000003", native_snapshot_hash="a" * 64,
        sources={"finance": source}, question="SYNTHETIC business question; no actual model call.",
        reviewed_tool_hash="b" * 64, producer_code_hash=producer_code_hash(),
        budgets=Budgets(max_queries=3, max_rows=1, max_bytes=100000, timeout_seconds=1),
        private_evidence=PrivateEvidenceConfig(
            directory=tmp_path / "native-captures", approval_reference="SYNTHETIC_UNIT_APPROVAL",
            expires_at=now + timedelta(minutes=5), max_capture_bytes=20000,
            max_total_bytes=100000, max_records=4,
        ),
        approval=Approval(
            target_alias="APPROVED_LAB_ONLY", artifact_digest="pending", target_digest="pending",
            operations=["reader-native-question"], estimated_cost=0, cap=0, currency="USD",
            expires_at=now + timedelta(minutes=5), ttl_hours=1,
            teardown_owner="SYNTHETIC_TEST", teardown_mechanism="No resources or network",
            confirmation_receipt="SYNTHETIC_NOT_LIVE_AUTHORITY",
        ),
        effects="READ_ONLY_NATIVE_ONTOLOGY_QUESTION",
    )
    config.approval.artifact_digest, config.approval.target_digest = config.artifact_digest, config.target_digest
    state = SimpleNamespace(active=True, calls=[], tokens=[], discoveries=0, connections=0)
    session = SimpleNamespace(scope=lambda: scope.model_copy(update={"models": []}))
    reader = SimpleNamespace(
        active=lambda current: current is session and state.active,
        config=SimpleNamespace(model_ids={"finance": source.dataset_id}),
    )
    def token(current, *, resource):
        assert current is session and resource == "ontology"
        state.tokens.append(resource)
        return "SYNTHETIC_OPAQUE_TOKEN"
    reader.token = token

    @asynccontextmanager
    async def connect(endpoint, **kwargs):
        endpoint.validate()
        state.connections += 1
        assert kwargs["single_attempt"] is True and kwargs["reader_scope"] == scope
        assert kwargs["reviewed_hashes"] == {"ask_ontology": config.reviewed_tool_hash}
        client = SimpleNamespace(endpoint=endpoint, reader_scope=scope)
        client.schema_hash = lambda tool: "c" * 64 if (
            change == "schema" or change == "late-schema" and state.discoveries
        ) else config.reviewed_tool_hash
        client.inventory = lambda: {"provenance": "SYNTHETIC_UNIT", "tools": ["ask_ontology"]}
        async def call(tool, arguments, *, capture, timeout):
            assert 0 < timeout <= config.budgets.timeout_seconds
            state.calls.append((tool, arguments))
            if change == "timeout":
                await asyncio.sleep(2)
            response = CallToolResult(
                content=[TextContent(type="text", text="SYNTHETIC unreviewed answer transport")],
                structuredContent={"Authorization": "SYNTHETIC_UNSAFE"} if change == "secret" else None,
                isError=change == "tool-error",
            )
            if change != "missing-capture":
                capture(tool, arguments, response.model_dump(mode="json", exclude_none=True), "MCP_TOOL_RESULT")
            if change == "logout":
                state.active = False
            if change == "tool-error":
                raise MCPToolError("SYNTHETIC native refusal")
            return response
        async def discover():
            state.discoveries += 1
        client.call, client.discover = call, discover
        yield client
    monkeypatch.setattr("telco_iq.reader_native_question.DiscoveredClient.connect", connect)
    return config, reader, session, state


async def test_one_native_capture_retains_original_without_claiming_a_controlled_app_result(tmp_path, monkeypatch):
    config, reader, session, state = case(tmp_path, monkeypatch)
    result = await capture_reader_native_question(reader, config, tmp_path / "repo", session)
    assert result["status"] == "NATIVE_ANSWER_CAPTURED_NEEDS_REVIEW" and result["ready"] is False
    assert result["generated_code_executed"] is False and result["application_model_requests"] == 0
    assert len(state.calls) == len(result["capture_hashes"]) == 1
    assert state.calls == [("ask_ontology", {"request": config.question})]
    sink = PrivateEvidenceSink(config.private_evidence, tmp_path / "repo")
    original = sink.read(result["capture_hashes"][0])
    assert original["tool"] == "ask_ontology" and original["request"] == {"request": config.question}
    assert original["capture_purpose"].endswith("NOT_CONTROLLED_APP_COMPARISON")
    assert original["context"]["tool_schema_hash"] == config.reviewed_tool_hash
    with pytest.raises(LabError, match="invocation is spent"):
        await capture_reader_native_question(reader, config, tmp_path / "repo", session)
    assert len(state.calls) == 1


@pytest.mark.parametrize("change", ["code", "scope", "target", "question", "approval"])
async def test_authority_and_reader_binding_fail_before_token_or_dispatch(tmp_path, monkeypatch, change):
    config, reader, session, state = case(tmp_path, monkeypatch)
    if change == "code":
        config.producer_code_hash = "f" * 64
    elif change == "scope":
        config.scope = config.scope.model_copy(update={"authorization_revision": "SYNTHETIC_OTHER"})
    elif change == "target":
        reader.config.model_ids["finance"] = "00000000-0000-0000-0000-000000000099"
    elif change == "question":
        config.question = "SYNTHETIC_CHANGED_QUESTION"
    else:
        config.approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(LabError):
        await capture_reader_native_question(reader, config, tmp_path / "repo", session)
    assert state.tokens == [] and state.calls == [] and state.connections == 0


@pytest.mark.parametrize("change", ["schema", "late-schema", "missing-capture", "secret", "logout", "timeout"])
async def test_unknown_unsafe_changed_or_incomplete_native_outputs_never_become_answers(tmp_path, monkeypatch, change):
    config, reader, session, state = case(tmp_path, monkeypatch, change)
    if change == "timeout":
        config.budgets.timeout_seconds = .02
        config.approval.artifact_digest = config.artifact_digest
    with pytest.raises(LabError) as error:
        await capture_reader_native_question(reader, config, tmp_path / "repo", session)
    if change == "logout":
        assert error.value.status == Status.BLOCKED_AUTH
    if change == "timeout":
        assert error.value.status == Status.TIMED_OUT
    assert len(state.calls) <= 1
    with pytest.raises(LabError):
        await capture_reader_native_question(reader, config, tmp_path / "repo", session)


async def test_native_tool_error_is_retained_but_never_retried_or_accepted(tmp_path, monkeypatch):
    config, reader, session, state = case(tmp_path, monkeypatch, "tool-error")
    result = await capture_reader_native_question(reader, config, tmp_path / "repo", session)
    assert result["status"] == "NATIVE_TOOL_ERROR_CAPTURED" and result["ready"] is False
    assert len(state.calls) == 1


async def test_fixed_private_capture_seam_explicitly_routes_native_question_not_http_or_schema(tmp_path, monkeypatch):
    config, reader, session, _ = case(tmp_path, monkeypatch)
    path = tmp_path / "fixed-capture.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    service = SourceMetadataCaptureService(reader, path, tmp_path / "repo")
    assert isinstance(service.config, ReaderNativeQuestionConfig)
    result = await service.capture(session)
    assert result["kind"] == "NATIVE_ONTOLOGY_ANSWER_CAPTURE_ONLY"
    service = SourceMetadataCaptureService(reader, path, tmp_path / "repo", requester_factory=lambda *args: None)
    with pytest.raises(LabError, match="cannot select a native question"):
        await service.capture(session)


@pytest.mark.parametrize("reviewed,destructive,allowed", [
    (False, False, False), (True, True, False), (True, False, True),
])
async def test_real_discovered_client_requires_review_and_read_only_effects_before_native_answer(
    tmp_path, monkeypatch, reviewed, destructive, allowed,
):
    config, _, _, _ = case(tmp_path, monkeypatch)
    definition = Tool(
        name="ask_ontology", description="SYNTHETIC read-only answer contract",
        inputSchema={
            "type": "object", "properties": {"request": {"type": "string", "minLength": 3}},
            "required": ["request"], "additionalProperties": False,
        },
        annotations=ToolAnnotations(destructiveHint=destructive),
    )
    tool_hash = digest(definition.model_dump(mode="json", exclude_none=True))
    calls = []
    class Session:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[definition], nextCursor=None)
        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, arguments))
            return CallToolResult(content=[TextContent(type="text", text="SYNTHETIC_UNREVIEWED_OUTPUT")])
    client = DiscoveredClient(
        Session(), Endpoint(
            "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/"
            "00000000-0000-0000-0000-000000000001/items/00000000-0000-0000-0000-000000000003/ontologyEndpoint",
            "ontology",
        ), "SYNTHETIC_PROTOCOL", reader_scope=config.scope,
        reviewed_hashes={"ask_ontology": tool_hash} if reviewed else {},
    )
    await client.discover()
    if allowed:
        captured = []
        result = await client.call(
            "ask_ontology", {"request": "Synthetic question"},
            capture=lambda *args: captured.append(args), synthetic_capture=True,
        )
        assert result.content and len(calls) == len(captured) == 1
    else:
        with pytest.raises(LabError):
            await client.call("ask_ontology", {"request": "Synthetic question"})
        assert calls == []
