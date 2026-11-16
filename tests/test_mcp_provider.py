"""Explicit synthetic MCP sessions; these receipts are not live Fabric evidence."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from mcp.types import CallToolResult, Tool

from telco_iq.catalog import Catalog, digest
from telco_iq.contracts import Filter, QueryNode, Scope, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import ObservedSourceState, ReaderBinding, SourceIdentity
from telco_iq.mcp_client import DiscoveredClient, Endpoint, IQAdapter, ReviewedOutput
from telco_iq.mcp_provider import (
    CaptureReview,
    MCPQueryProvider,
    ReturnedColumn,
    ReviewedSourceBinding,
    bounded_dax,
)
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink

ROOT = Path(__file__).resolve().parents[1]
MODEL = UUID("00000000-0000-0000-0000-000000000020")
WORKSPACE = UUID("00000000-0000-0000-0000-000000000010")
OTHER = UUID("00000000-0000-0000-0000-000000000099")
METRIC = "finance.recognized_service_revenue"
ENVELOPE = {
    "type": "object", "additionalProperties": False,
    "required": ["rows", "complete", "total", "errors"],
    "properties": {
        "rows": {"type": "array", "items": {"type": "object"}},
        "complete": {"type": "boolean"}, "total": {"type": "integer", "minimum": 0},
        "errors": {"type": "array", "items": {"type": "string"}},
    },
}
QUERY_TOOL = Tool(name="ExecuteQuery", inputSchema={
    "type": "object", "additionalProperties": False,
    "required": ["artifactId", "daxQueries", "maxRows"],
    "properties": {
        "artifactId": {"type": "string"},
        "daxQueries": {"type": "array", "minItems": 1, "maxItems": 1, "items": {"type": "string"}},
        "maxRows": {"type": "integer", "minimum": 1},
    },
})


def reader_binding():
    return ReaderBinding(
        scope=Scope(
            tenant="synthetic-tenant", principal="synthetic-reader", authorization_revision="session-one",
            models=["finance"], mode="LIVE_DELEGATED",
        ),
        ontology_workspace_id=WORKSPACE, ontology_item_id="00000000-0000-0000-0000-000000000030",
        native_snapshot_hash="a" * 64,
        sources={"finance": SourceIdentity(workspace_id=WORKSPACE, dataset_id=MODEL)},
    )


def capture_review(result):
    return CaptureReview(digest(result.model_dump(mode="json")), "explicit synthetic test capture", "synthetic_test")


class SyntheticSession:
    def __init__(self, tools, responses):
        self.tools, self.responses = deepcopy(tools), deepcopy(responses)
        self.calls = []
        self.on_call = None
        self.on_list = None
        self.list_count = 0

    async def list_tools(self, cursor=None):
        self.list_count += 1
        if self.on_list:
            self.on_list(self.list_count)
        return SimpleNamespace(tools=deepcopy(self.tools), nextCursor=None)

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, deepcopy(arguments)))
        if self.on_call:
            self.on_call(name, arguments)
        response = self.responses[name]
        return deepcopy(response(arguments) if callable(response) else response)


async def fresh_state(binding, alias):
    assert alias in binding.sources
    return ObservedSourceState(
        data_revision="observed-data", model_revision="observed-model",
        evidence_hash=digest({"synthetic-state": True, "binding": binding.binding_hash, "alias": alias}),
        observed_at=datetime.now(timezone.utc),
    )


async def setup_provider(rows=None, *, total=None, complete=True, state_probe=fresh_state):
    binding = reader_binding()
    rows = [{"Account": 1, "Cents": 9007199254740993}] if rows is None else rows
    result = CallToolResult(content=[], structuredContent={
        "rows": rows, "complete": complete, "total": len(rows) if total is None else total, "errors": [],
    })
    session = SyntheticSession([QUERY_TOOL], {"ExecuteQuery": result})
    client = DiscoveredClient(
        session, Endpoint("https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq", "iq"), "synthetic-protocol",
        reader_scope=binding.scope,
    )
    await client.discover()
    output = ReviewedOutput(
        client.schema_hash("ExecuteQuery"), "/rows", "/complete", "/total", "/errors", ENVELOPE,
        "Synthetic envelope only; GA NOT_YET_REVIEWED",
    )
    source = ReviewedSourceBinding(
        identity=binding.sources["finance"], artifact_id=MODEL,
        columns={"account_month": {
            "account_key": ReturnedColumn("Account", "int64", False),
            METRIC: ReturnedColumn("Cents", "int64", True),
        }},
        output=output, review=capture_review(result),
    )
    active = {"value": True}
    provider = MCPQueryProvider(
        Catalog(ROOT / "data" / "contracts" / "catalog.json"), client, binding, "native-context-revision",
        source_bindings={"finance": source}, session_active=lambda scope: active["value"] and scope == binding.scope,
        state_probe=state_probe, synthetic=True,
    )
    node = QueryNode(
        id="revenue", kind="QuerySemanticModel", model="finance", table="account_month",
        metrics=[METRIC], group_by=["account_key"], date_role="month", expected_grain=["account_key"],
        filters=[Filter(column="month", op="eq", values=["2026-07-01"])], max_rows=2,
    )
    return provider, session, node, active


async def test_complete_query_preserves_exact_cents_nulls_rows_lineage_and_actual_receipt():
    provider, session, node, _ = await setup_provider(rows=[
        {"Account": 1, "Cents": 9007199254740993}, {"Account": 1, "Cents": None},
    ])
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.rows == [{"account_key": 1, METRIC: 9007199254740993}, {"account_key": 1, METRIC: None}]
    assert artifact.status == receipt.status == Status.COMPLETE
    assert artifact.lineage[METRIC][0].column == METRIC
    assert artifact.grain == ["account_key"] and artifact.aggregation[METRIC] == "additive"
    assert artifact.source_clocks[0].data == "observed-data"
    assert artifact.clocks.model == "observed-model" != provider.catalog.revision
    assert receipt.source_watermark is None
    assert receipt.id in artifact.evidence_ids and receipt.elapsed_ms >= 0
    assert receipt.schema_hash == provider.client.schema_hash("ExecuteQuery")
    assert receipt.lane == provider.mode == "SYNTHETIC_TEST"
    tool, args = session.calls[0]
    assert tool == "ExecuteQuery" and args["artifactId"] == str(MODEL) and args["maxRows"] == 3
    assert len(args["daxQueries"]) == 1 and args["daxQueries"][0] == receipt.query
    assert "TOPN(3," in receipt.query and "'Account Month'[account_key], ASC" in receipt.query
    assert "DATE(2026, 7, 1)" in receipt.query and "[Recognized Service Revenue]" in receipt.query


@pytest.mark.parametrize("rows,total,complete", [
    ([], 0, True), ([{"Account": 1, "Cents": None}], 1, True),
])
async def test_empty_and_null_are_not_fabricated_zero(rows, total, complete):
    provider, _, node, _ = await setup_provider(rows, total=total, complete=complete)
    artifact, _ = await provider.query(node, provider.binding.scope)
    assert artifact.status == Status.COMPLETE
    assert len(artifact.rows) == total
    if rows:
        assert artifact.rows[0][METRIC] is None


@pytest.mark.parametrize("table,metric", [
    ("service_month", "enterprise.sla_compliance"),
    ("sla_evaluation", "enterprise.eligible_credits"),
    ("sla_evaluation", "enterprise.credit_exposure"),
    ("applied_credit", "enterprise.applied_credits"),
])
async def test_unobserved_source_coverage_contract_blocks_before_mcp_dispatch(table, metric):
    original, session, _, _ = await setup_provider()
    scope = original.binding.scope.model_copy(update={"models": ["enterprise"]})
    identity = original.binding.sources["finance"]
    binding = original.binding.model_copy(update={"scope": scope, "sources": {"enterprise": identity}})
    client = DiscoveredClient(
        session, original.client.endpoint, original.client.protocol, reader_scope=scope,
    )
    await client.discover()
    provider = MCPQueryProvider(
        original.catalog, client, binding, "native-context-revision",
        source_bindings={"enterprise": replace(original.source_bindings["finance"], columns={})},
        session_active=lambda current: current == scope, state_probe=fresh_state, synthetic=True,
    )
    node = QueryNode(
        id="source", kind="QuerySemanticModel", model="enterprise", table=table,
        metrics=[metric], expected_grain=[], date_role="month",
        filters=[Filter(column="month", op="eq", values=["2026-07-01"])],
    )
    with pytest.raises(LabError, match="raw-source coverage proof") as error:
        await provider.query(node, scope)
    assert error.value.status == Status.BLOCKED_CAPABILITY
    assert session.calls == []


@pytest.mark.parametrize("total,complete", [(100, False), (100, True), (1, False), (0, True)])
async def test_partial_count_cannot_become_complete(total, complete):
    provider, _, node, _ = await setup_provider(total=total, complete=complete)
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.status == receipt.status == Status.PARTIAL
    assert artifact.rows[0][METRIC] == 9007199254740993
    assert artifact.warnings


async def test_limit_plus_one_is_partial_even_with_complete_bounded_response():
    provider, _, node, _ = await setup_provider([{"Account": i, "Cents": i} for i in range(3)])
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.status == receipt.status == Status.PARTIAL and len(artifact.rows) == 2
    assert "limit+1" in artifact.warnings[0]


async def test_response_exceeding_requested_sentinel_blocks():
    provider, _, node, _ = await setup_provider([{"Account": i, "Cents": i} for i in range(4)])
    with pytest.raises(LabError, match="sentinel row budget") as error:
        await provider.query(node, provider.binding.scope)
    assert error.value.status == Status.PARTIAL


@pytest.mark.parametrize("value", [1.0, True, "9007199254740993", 2**63, -(2**63)-1])
async def test_exact_int64_rejects_coercion_and_overflow(value):
    provider, _, node, _ = await setup_provider([{"Account": 1, "Cents": value}])
    with pytest.raises(LabError, match="no lossy coercion"):
        await provider.query(node, provider.binding.scope)


@pytest.mark.parametrize("row", [
    {"Account": None, "Cents": 1}, {"Account": 1, "Cents": 1, "Extra": 9},
    {"Account": 1}, {"Account": 1, "OtherCents": 1},
])
async def test_rows_require_exact_reviewed_columns_and_nullability(row):
    provider, _, node, _ = await setup_provider([row])
    with pytest.raises(LabError):
        await provider.query(node, provider.binding.scope)


async def test_missing_profile_and_missing_state_block_before_query():
    provider, session, node, _ = await setup_provider(state_probe=None)
    with pytest.raises(LabError, match="NOT_COMPARABLE"):
        await provider.query(node, provider.binding.scope)
    assert session.calls == []
    provider.source_bindings["finance"] = replace(provider.source_bindings["finance"], output=None)
    provider._configuration_hash = provider._configuration()
    with pytest.raises(LabError, match="profile is absent"):
        await provider.query(node, provider.binding.scope)
    assert session.calls == []


async def test_unknown_shape_has_separate_raw_capture_route_without_completeness_claim():
    provider, session, _, _ = await setup_provider()
    adapter = IQAdapter(provider.client)
    with pytest.raises(LabError, match="not been reviewed"):
        await adapter.execute(str(MODEL), "EVALUATE ROW(\"probe\", 1)", 1)
    assert not session.calls
    raw = await adapter.capture(str(MODEL), "EVALUATE ROW(\"probe\", 1)", 1)
    assert isinstance(raw, CallToolResult) and len(session.calls) == 1


async def test_wrong_artifact_workspace_and_alias_remap_are_rejected():
    provider, _, _, _ = await setup_provider()
    original = provider.source_bindings["finance"]
    for source in (
        replace(original, artifact_id=OTHER),
        replace(original, identity=SourceIdentity(workspace_id=OTHER, dataset_id=MODEL)),
        replace(original, identity=SourceIdentity(workspace_id=WORKSPACE, dataset_id=OTHER)),
    ):
        with pytest.raises(LabError, match="Source artifact identity"):
            MCPQueryProvider(
                provider.catalog, provider.client, provider.binding, "context",
                source_bindings={"finance": source}, session_active=lambda _: True, synthetic=True,
            )
    rebound = provider.binding.model_copy(deep=True)
    rebound.sources = {"subscriptions": rebound.sources["finance"]}
    rebound.scope.models = ["subscriptions"]
    with pytest.raises(LabError, match="exactly"):
        MCPQueryProvider(
            provider.catalog, provider.client, rebound, "context",
            source_bindings=provider.source_bindings, session_active=lambda _: True, synthetic=True,
        )


async def test_scope_epoch_and_mapping_changes_cannot_authorize_a_call():
    provider, session, node, _ = await setup_provider()
    scope = provider.binding.scope.model_copy(update={"authorization_revision": "other-session"})
    with pytest.raises(LabError, match="binding"):
        await provider.query(node, scope)
    provider.client.reader_scope = scope
    with pytest.raises(LabError, match="session epoch"):
        await provider.query(node, provider.binding.scope)
    provider.client.reader_scope = provider.binding.scope.model_copy(deep=True)
    provider.source_bindings["finance"] = replace(provider.source_bindings["finance"], artifact_id=OTHER)
    with pytest.raises(LabError, match="mapping changed"):
        await provider.query(node, provider.binding.scope)
    assert not session.calls


@pytest.mark.parametrize("when", ["before", "during"])
async def test_revoked_session_never_returns_data(when):
    provider, session, node, active = await setup_provider()
    if when == "before":
        active["value"] = False
    else:
        session.on_call = lambda *_: active.update(value=False)
    with pytest.raises(LabError, match="no longer current") as error:
        await provider.query(node, provider.binding.scope)
    assert error.value.status == Status.BLOCKED_AUTH
    assert len(session.calls) == (when == "during")


async def test_source_drift_preserves_partial_receipt_without_comparability():
    count = 0

    async def drift(binding, alias):
        nonlocal count
        state = await fresh_state(binding, alias)
        count += 1
        return state.model_copy(update={"data_revision": f"observed-{count}"})

    provider, _, node, _ = await setup_provider(state_probe=drift)
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.status == receipt.status == Status.PARTIAL
    assert any("NOT_COMPARABLE" in warning for warning in artifact.warnings)
    assert artifact.source_clocks[0].data == "observed-1"


async def test_stale_state_is_not_an_observed_clock():
    async def stale(binding, alias):
        state = await fresh_state(binding, alias)
        return state.model_copy(update={"observed_at": datetime.now(timezone.utc) - timedelta(minutes=1)})

    provider, session, node, _ = await setup_provider(state_probe=stale)
    with pytest.raises(LabError, match="fresh, timestamped"):
        await provider.query(node, provider.binding.scope)
    assert not session.calls


@pytest.mark.parametrize("when", ["before", "during", "after"])
async def test_current_tool_schema_hash_is_checked_around_queries(when):
    provider, session, node, _ = await setup_provider()

    def changed():
        session.tools[0] = session.tools[0].model_copy(update={"description": "changed"})

    if when == "before":
        changed()
    elif when == "during":
        def during(*_):
            provider.client.tools["ExecuteQuery"].description = "changed-during"
        session.on_call = during
    else:
        session.on_list = lambda count: changed() if count == 3 else None
    with pytest.raises(LabError, match="changed"):
        await provider.query(node, provider.binding.scope)


async def test_is_error_and_per_query_errors_never_become_empty_results():
    provider, session, node, _ = await setup_provider([])
    session.responses["ExecuteQuery"].isError = True
    with pytest.raises(LabError, match="isError"):
        await provider.query(node, provider.binding.scope)
    session.responses["ExecuteQuery"].isError = False
    session.responses["ExecuteQuery"].structuredContent["errors"] = ["source failed"]
    with pytest.raises(LabError, match="Per-query errors"):
        await provider.query(node, provider.binding.scope)


async def test_synthetic_profile_cannot_be_enabled_as_production():
    provider, session, node, _ = await setup_provider()
    provider = MCPQueryProvider(
        provider.catalog, provider.client, provider.binding, "context",
        source_bindings=provider.source_bindings, session_active=lambda _: True,
        state_probe=fresh_state,
    )
    with pytest.raises(LabError, match="Synthetic profiles"):
        await provider.query(node, provider.binding.scope)
    assert not session.calls


async def test_cents_mapping_cannot_opt_into_float_conversion():
    provider, session, node, _ = await setup_provider()
    source = provider.source_bindings["finance"]
    source.columns["account_month"][METRIC] = ReturnedColumn("Cents", "float64", True)
    provider._configuration_hash = provider._configuration()
    with pytest.raises(LabError, match="exact int64"):
        await provider.query(node, provider.binding.scope)
    assert not session.calls


async def test_compiler_quotes_literals_and_rejects_lossy_timestamp_filters():
    provider, _, _, _ = await setup_provider()
    node = QueryNode(
        id="raw", kind="QuerySemanticModel", model="finance", table="account",
        columns=["account_key", "display_name"], expected_grain=["account_key"],
        filters=[Filter(column="display_name", op="eq", values=['quote" EVALUATE bad'])], max_rows=2,
    )
    dax = bounded_dax(provider.catalog, node)
    assert 'quote"" EVALUATE bad' in dax and "TOPN(3," in dax
    node.filters = [Filter(column="account_key", op="eq", values=[True])]
    with pytest.raises(LabError, match="source type"):
        bounded_dax(provider.catalog, node)
    timestamp_node = QueryNode(
        id="incident", kind="QuerySemanticModel", model="network", table="incident",
        columns=["incident_key", "start"], date_role="start", expected_grain=["incident_key"],
        filters=[Filter(column="start", op="gt", values=["2026-07-01T00:00:00.001+00:00"])],
    )
    with pytest.raises(LabError, match="fractional seconds"):
        bounded_dax(provider.catalog, timestamp_node)


def attach_private_sink(provider, tmp_path, **changes):
    provider.evidence_sink = PrivateEvidenceSink(PrivateEvidenceConfig(
        directory=tmp_path / "source-evidence", approval_reference="synthetic-store-approval",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        max_capture_bytes=64_000, max_total_bytes=256_000, max_records=10,
    ).model_copy(update=changes), tmp_path / "synthetic-repository")
    provider._configuration_hash = provider._configuration()
    return provider.evidence_sink


@pytest.mark.parametrize("complete,total", [(True, 1), (False, 99)])
async def test_query_retains_original_response_and_exact_request_filter_source_hash_linkage(tmp_path, complete, total):
    provider, session, node, _ = await setup_provider(complete=complete, total=total)
    sink = attach_private_sink(provider, tmp_path)
    artifact, receipt = await provider.query(node, provider.binding.scope)
    files = list(sink.directory.glob("tq005-*.json"))
    assert len(files) == 1
    capture_id = files[0].stem.removeprefix("tq005-")
    raw = sink.read(capture_id)
    assert raw["lane"] == "SYNTHETIC_TEST"
    assert raw["request"] == session.calls[0][1]
    assert raw["response"] == session.responses["ExecuteQuery"].model_dump(mode="json", exclude_unset=True)
    assert raw["context"]["query_node"] == node.model_dump(mode="json")
    assert raw["context"]["source"] == provider.binding.sources["finance"].model_dump(mode="json")
    public = sink.derivative(capture_id)
    assert public["request_hash"] == receipt.query_hash == digest(raw["request"])
    assert public["response_hash"] == digest(raw["response"])
    assert capture_id in artifact.evidence_ids and public["response_hash"] in artifact.evidence_ids
    assert capture_id in receipt.completeness_evidence
    assert str(MODEL) not in json.dumps(public) and str(WORKSPACE) not in json.dumps(public)
    assert receipt.status == (Status.COMPLETE if complete else Status.PARTIAL)
    assert "not live Fabric" in " ".join(receipt.warnings)


@pytest.mark.parametrize("failure", ["malformed", "refused", "decode", "wrong-columns", "schema"])
async def test_raw_capture_precedes_decoder_refusal_and_advertised_schema_validation(tmp_path, failure):
    provider, session, node, _ = await setup_provider()
    result = session.responses["ExecuteQuery"]
    if failure == "malformed":
        result.structuredContent = {"unreviewed": {"raw": [None, "preserve"]}}
    elif failure == "refused":
        result.isError = True
    elif failure == "decode":
        result.structuredContent["errors"] = ["source denied the query"]
    elif failure == "wrong-columns":
        result.structuredContent["rows"][0]["extra"] = 42
    else:
        session.tools[0].outputSchema = {"type": "object", "required": ["missing"]}
        await provider.client.discover()
        source = provider.source_bindings["finance"]
        provider.source_bindings["finance"] = replace(
            source, output=replace(source.output, schema_hash=provider.client.schema_hash("ExecuteQuery")),
        )
    sink = attach_private_sink(provider, tmp_path)
    with pytest.raises(LabError):
        await provider.query(node, provider.binding.scope)
    files = list(sink.directory.glob("tq005-*.json"))
    assert len(files) == 1
    assert sink.read(files[0].stem.removeprefix("tq005-"))["response"] == result.model_dump(
        mode="json", exclude_unset=True,
    )


async def test_live_query_without_sink_blocks_before_source_requests(tmp_path):
    provider, session, node, _ = await setup_provider()
    source = provider.source_bindings["finance"]
    provider.source_bindings["finance"] = replace(
        source, review=replace(source.review, provenance="reader_capture"),
    )
    provider.synthetic = False
    provider._configuration_hash = provider._configuration()
    with pytest.raises(LabError, match="private retention"):
        await provider.query(node, provider.binding.scope)
    assert not session.calls


async def test_synthetic_flag_does_not_exempt_actual_sdk_source_transport_from_private_retention():
    from mcp import ClientSession

    provider, old_session, node, _ = await setup_provider()
    provider.client.session = object.__new__(ClientSession)
    with pytest.raises(LabError, match="private retention"):
        await provider.query(node, provider.binding.scope)
    assert not old_session.calls


async def test_sink_failure_and_token_echo_never_return_success_artifact(tmp_path):
    provider, session, node, _ = await setup_provider()
    sink = attach_private_sink(provider, tmp_path, max_capture_bytes=100)
    with pytest.raises(LabError, match="size"):
        await provider.query(node, provider.binding.scope)
    assert len(session.calls) == 1
    assert not list(sink.directory.glob("tq005-*.json"))
    sink.config.max_capture_bytes = 64_000
    provider._configuration_hash = provider._configuration()
    session.responses["ExecuteQuery"].structuredContent["note"] = "Bearer synthetic-token-never-persisted"
    with pytest.raises(LabError, match="Unsafe"):
        await provider.query(node, provider.binding.scope)
    assert not list(sink.directory.glob("tq005-*.json"))


async def test_raw_typed_sdk_path_retains_missing_required_fields_before_model_validation(tmp_path, monkeypatch):
    from mcp import ClientSession

    provider, old_session, node, _ = await setup_provider()
    session = object.__new__(ClientSession)
    payload = {"unexpected": {"complete": False}, "isError": True}
    requests = []

    async def send_request(request, result_type, **kwargs):
        requests.append(request.model_dump(mode="json", exclude_none=True))
        assert kwargs["request_read_timeout_seconds"].total_seconds() == 45
        return result_type.model_validate(payload)

    monkeypatch.setattr(session, "send_request", send_request)
    monkeypatch.setattr(session, "list_tools", old_session.list_tools)
    provider.client.session = session
    sink = attach_private_sink(provider, tmp_path)
    with pytest.raises(LabError, match="malformed"):
        await provider.query(node, provider.binding.scope)
    assert requests[0]["method"] == "tools/call" and requests[0]["params"]["name"] == "ExecuteQuery"
    files = list(sink.directory.glob("tq005-*.json"))
    assert len(files) == 1
    raw = sink.read(files[0].stem.removeprefix("tq005-"))
    assert raw["response"] == payload
    assert raw["request"] == requests[0]["params"]["arguments"]


@pytest.mark.parametrize("failure", ["protocol-error", "invalid-sdk-result"])
async def test_obtainable_sdk_failure_is_retained_without_claiming_original_wire_replay(tmp_path, monkeypatch, failure):
    from mcp import ClientSession
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    provider, old_session, node, _ = await setup_provider()
    session = object.__new__(ClientSession)
    error_payload = {"code": -32602, "message": "Synthetic request refused", "data": {"source": "synthetic"}}

    async def send_request(*args, **kwargs):
        if failure == "protocol-error":
            raise McpError(ErrorData.model_validate(error_payload))
        return CallToolResult.model_validate({"content": "not-an-array"})

    monkeypatch.setattr(session, "send_request", send_request)
    monkeypatch.setattr(session, "list_tools", old_session.list_tools)
    provider.client.session = session
    sink = attach_private_sink(provider, tmp_path)
    with pytest.raises(LabError, match="protocol error|failure evidence only"):
        await provider.query(node, provider.binding.scope)
    files = list(sink.directory.glob("tq005-*.json"))
    assert len(files) == 1
    retained = sink.read(files[0].stem.removeprefix("tq005-"))
    if failure == "protocol-error":
        assert retained["response_kind"] == "MCP_PROTOCOL_ERROR"
        assert retained["response"] == error_payload
    else:
        assert retained["response_kind"] == "SDK_RESULT_UNOBTAINABLE"
        assert retained["response"]["tool_result_available"] is False
        assert "wire bytes" in retained["response"]["limit"]
