from datetime import datetime, timedelta, timezone

import pytest
from mcp.types import CallToolResult, Tool

from telco_iq.contracts import Scope
from telco_iq.errors import LabError
from telco_iq.live_contracts import ReaderBinding, SourceIdentity
from telco_iq.mcp_client import DiscoveredClient, Endpoint
from telco_iq.mcp_configuration import ColumnConfig, ReviewConfig, StateProfileConfig, state_probe
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink


def fixture():
    scope = Scope(tenant="unit", principal="unit", authorization_revision="epoch",
                  models=["source"], mode="LIVE_DELEGATED")
    source = SourceIdentity(workspace_id="00000000-0000-0000-0000-000000000001",
                            dataset_id="00000000-0000-0000-0000-000000000002")
    binding = ReaderBinding(
        scope=scope, ontology_workspace_id=source.workspace_id,
        ontology_item_id="00000000-0000-0000-0000-000000000003",
        native_snapshot_hash="a" * 64, sources={"source": source},
    )
    calls = []

    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, arguments))
            return CallToolResult(content=[], structuredContent={
                "dataRevision": "synthetic-rowset", "modelRevision": "synthetic-model",
                "complete": True, "errors": [],
            })

    client = DiscoveredClient(Session(), Endpoint("https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq", "iq"),
                              "unit-protocol", reader_scope=scope)
    client.tools["ExecuteQuery"] = Tool(name="ExecuteQuery", inputSchema={
        "type": "object", "properties": {
            "artifactId": {"type": "string"}, "daxQueries": {"type": "array", "items": {"type": "string"}},
            "maxRows": {"type": "integer"},
        }, "required": ["artifactId", "daxQueries", "maxRows"], "additionalProperties": False,
    })
    profile = StateProfileConfig(
        identity=source, schema_hash=client.schema_hash("ExecuteQuery"),
        review=ReviewConfig(capture_hash="b" * 64, review_reference="synthetic unit fixture", provenance="reader_capture"),
        dax="EVALUATE 'SyntheticSourceState'", data_revision_pointer="/dataRevision",
        model_revision_pointer="/modelRevision", errors_pointer="/errors", complete_pointer="/complete",
        evidence_meaning="Synthetic transport test; not real source state.",
        output_schema={"type": "object", "additionalProperties": False, "required": [
            "dataRevision", "modelRevision", "complete", "errors",
        ], "properties": {"dataRevision": {"type": "string"}, "modelRevision": {"type": "string"},
                          "complete": {"type": "boolean"}, "errors": {"type": "array"}}},
    )
    return scope, binding, profile, client, calls


async def test_state_probe_uses_bound_source_and_actual_response_not_configured_revision(tmp_path):
    scope, binding, profile, client, calls = fixture()
    started = datetime.now(timezone.utc)
    probe = state_probe(
        client, {"source": profile}, session_active=lambda value: value == scope, receipts=tmp_path, synthetic=True,
    )
    result = await probe(binding, "source")
    assert result.data_revision == "synthetic-rowset" and result.model_revision == "synthetic-model"
    assert result.observed_at >= started
    assert calls[0][1]["artifactId"] == str(binding.sources["source"].dataset_id)
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.parametrize("failure", ["missing", "identity", "scope", "schema"])
async def test_state_probe_blocks_before_query_when_not_reviewed_for_exact_source(tmp_path, failure):
    scope, binding, profile, client, calls = fixture()
    profiles = {"source": profile}
    if failure == "missing":
        profiles = {}
    elif failure == "identity":
        profile.identity = SourceIdentity(workspace_id=profile.identity.workspace_id,
                                          dataset_id="00000000-0000-0000-0000-000000000009")
    elif failure == "scope":
        client.reader_scope = scope.model_copy(update={"authorization_revision": "old"})
    else:
        profile.schema_hash = "c" * 64
    probe = state_probe(client, profiles, session_active=lambda value: True, receipts=tmp_path)
    with pytest.raises(LabError):
        await probe(binding, "source")
    assert not calls


def test_profile_column_types_match_reviewed_provider_vocabulary():
    assert ColumnConfig(name="flag", dtype="bool", nullable=False).dtype == "bool"
    with pytest.raises(ValueError):
        ColumnConfig(name="flag", dtype="boolean", nullable=False)


async def test_state_probe_without_private_retention_cannot_claim_live_source_evidence(tmp_path):
    scope, binding, profile, client, calls = fixture()
    probe = state_probe(client, {"source": profile}, session_active=lambda _: True, receipts=tmp_path)
    with pytest.raises(LabError, match="private retention"):
        await probe(binding, "source")
    assert not calls


async def test_synthetic_state_probe_flag_does_not_exempt_actual_sdk_source_transport(tmp_path):
    from mcp import ClientSession

    scope, binding, profile, client, calls = fixture()
    client.session = object.__new__(ClientSession)
    probe = state_probe(client, {"source": profile}, session_active=lambda _: True, receipts=tmp_path, synthetic=True)
    with pytest.raises(LabError, match="private retention"):
        await probe(binding, "source")
    assert not calls


@pytest.mark.parametrize("malformed", [False, True])
async def test_state_probe_retains_raw_before_business_decoding(tmp_path, malformed):
    scope, binding, profile, client, calls = fixture()
    if malformed:
        profile.output_schema["required"].append("unavailableField")
    sink = PrivateEvidenceSink(PrivateEvidenceConfig(
        directory=tmp_path / "captures", approval_reference="synthetic-store-review",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        max_capture_bytes=32_000, max_total_bytes=64_000, max_records=2,
    ), tmp_path / "synthetic-repository")
    probe = state_probe(client, {"source": profile}, session_active=lambda _: True, evidence_sink=sink, synthetic=True)
    if malformed:
        with pytest.raises(LabError, match="violates"):
            await probe(binding, "source")
    else:
        result = await probe(binding, "source")
        assert sink.read(result.evidence_hash)["context"]["kind"] == "SOURCE_STATE_QUERY"
    files = list(sink.directory.glob("tq005-*.json"))
    assert len(files) == 1
    retained = sink.read(files[0].stem.removeprefix("tq005-"))
    assert retained["request"] == calls[0][1]
    assert retained["response"]["structuredContent"]["dataRevision"] == "synthetic-rowset"
