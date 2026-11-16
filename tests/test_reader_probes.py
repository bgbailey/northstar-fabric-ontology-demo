"""Actual protocol result objects over explicitly synthetic, non-network sessions."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from mcp.types import CallToolResult, Tool
from test_mcp_provider import OTHER, SyntheticSession, capture_review, fresh_state, reader_binding

from telco_iq.contracts import Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import SourceIdentity
from telco_iq.mcp_client import DiscoveredClient, Endpoint
from telco_iq.mcp_provider import ReaderProbe, ReviewedSchemaProfile, schema_fingerprint

SCHEMA_TOOL = Tool(name="GetSemanticModelSchema", inputSchema={
    "type": "object", "additionalProperties": False, "required": ["artifactId"],
    "properties": {"artifactId": {"type": "string"}},
})
NATIVE_TOOLS = [
    Tool(name=name, inputSchema={"type": "object", "additionalProperties": False})
    for name in ("list_ontology_entities", "list_ontology_rules")
]
NAME_LIST = {
    "type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["label"],
        "properties": {"label": {"type": "string", "minLength": 1}},
    },
}
COLUMN_LIST = {
    "type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["label", "observedType"],
        "properties": {
            "label": {"type": "string", "minLength": 1}, "observedType": {"type": "string", "minLength": 1},
        },
    },
}
SCHEMA_ENVELOPE = {
    "type": "object", "additionalProperties": False,
    "required": ["workspace", "dataset", "tables", "done", "tableCount"],
    "properties": {
        "workspace": {"type": "string"}, "dataset": {"type": "string"},
        "done": {"type": "boolean"}, "tableCount": {"type": "integer"},
        "tables": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["label", "cols", "metrics"],
            "properties": {"label": {"type": "string"}, "cols": COLUMN_LIST, "metrics": NAME_LIST},
        }},
    },
}


async def setup_probe(*, state_probe=None):
    binding = reader_binding()
    source_result = CallToolResult(content=[], structuredContent={
        "workspace": str(binding.sources["finance"].workspace_id),
        "dataset": str(binding.sources["finance"].dataset_id), "done": True, "tableCount": 1,
        "tables": [{
            "label": "Account Month", "cols": [
                {"label": "revenue_cents", "observedType": "Int64"}, {"label": "account_key", "observedType": "Int64"},
            ],
            "metrics": [{"label": "Recognized Service Revenue"}],
        }],
    })
    source_session = SyntheticSession([SCHEMA_TOOL], {"GetSemanticModelSchema": source_result})
    source_client = DiscoveredClient(
        source_session, Endpoint("https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq", "iq"),
        "synthetic", reader_scope=binding.scope,
    )
    native_session = SyntheticSession(NATIVE_TOOLS, {
        "list_ontology_entities": CallToolResult(content=[], structuredContent={"values": [
            {"name": "Account Month", "properties": [{"name": "account_key", "type": "int64"}]},
        ]}),
        "list_ontology_rules": CallToolResult(content=[], structuredContent={"values": []}),
    })
    native_client = DiscoveredClient(
        native_session, Endpoint(
            "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/"
            f"{binding.ontology_workspace_id}/items/{binding.ontology_item_id}/ontologyEndpoint", "ontology",
        ), "synthetic", reader_scope=binding.scope,
    )
    await source_client.discover()
    await native_client.discover()
    native_client.reviewed_hashes = {tool.name: native_client.schema_hash(tool.name) for tool in NATIVE_TOOLS}
    profile = ReviewedSchemaProfile(
        schema_hash=source_client.schema_hash("GetSemanticModelSchema"),
        output_schema=SCHEMA_ENVELOPE, review=capture_review(source_result),
        artifact_argument="artifactId", arguments={},
        workspace_pointer="/workspace", dataset_pointer="/dataset", tables_pointer="/tables",
        table_name_pointer="/label", columns_pointer="/cols", column_name_pointer="/label",
        column_type_pointer="/observedType",
        measures_pointer="/metrics", measure_name_pointer="/label",
        exhaustive_response_reference="Synthetic exhaustive-response test contract; not GA evidence",
        complete_pointer="/done", table_count_pointer="/tableCount",
    )
    active = {"value": True}
    probe = ReaderProbe(
        native_client, source_client, schema_profiles={"finance": profile},
        session_active=lambda scope: active["value"] and scope == binding.scope,
        native_arguments={tool.name: {} for tool in NATIVE_TOOLS}, state_probe=state_probe, synthetic=True,
    )
    return probe, binding, native_session, source_session, active


async def test_probe_uses_actual_bound_native_and_source_calls_without_fake_states():
    probe, binding, native, source, _ = await setup_probe()
    started = datetime.now(timezone.utc)
    receipt = await probe(binding)
    assert receipt.binding_hash == binding.binding_hash
    assert started <= receipt.checked_at <= datetime.now(timezone.utc)
    assert receipt.observed_source_states == {}
    assert receipt.native_entities == native.responses["list_ontology_entities"].structuredContent["values"]
    assert receipt.native_rules == []
    assert receipt.source_schema_hashes == {"finance": schema_fingerprint([{
        "name": "Account Month", "columns": [
            {"name": "revenue_cents", "data_type": "Int64"}, {"name": "account_key", "data_type": "Int64"},
        ],
        "measures": ["Recognized Service Revenue"],
    }])}
    assert {name for name, _ in native.calls} == {"list_ontology_entities", "list_ontology_rules"}
    assert source.calls == [("GetSemanticModelSchema", {"artifactId": str(binding.sources["finance"].dataset_id)})]
    assert all(len(value) == 64 for value in receipt.evidence_hashes.values())
    assert "source_profile:finance" in receipt.evidence_hashes


async def multi_source_probe():
    probe, binding, native, source, active = await setup_probe()
    identity = binding.sources["finance"]
    profile = probe.schema_profiles["finance"]
    binding.sources = {
        f"source_{index}": SourceIdentity(
            workspace_id=identity.workspace_id, dataset_id=f"00000000-0000-0000-0000-{100 + index:012d}",
        ) for index in range(8)
    }
    binding.scope.models = sorted(binding.sources)
    probe.schema_profiles = {alias: deepcopy(profile) for alias in binding.sources}
    for client in (probe.native_client, probe.source_client):
        client.reader_scope = binding.scope.model_copy(deep=True)
    return probe, binding, native, source, active


async def test_all_source_schemas_overlap_with_a_four_request_bound_and_keep_exact_identities():
    probe, binding, _, source, _ = await multi_source_probe()
    original = source.call_tool
    gate = asyncio.Event()
    active = maximum = started = 0

    async def call(name, arguments, **kwargs):
        nonlocal active, maximum, started
        active += 1
        started += 1
        maximum = max(maximum, active)
        if started == 4:
            gate.set()
        try:
            await asyncio.wait_for(gate.wait(), 5)
            await asyncio.sleep(.001 * (108 - int(arguments["artifactId"].split("-")[-1])))
            result = (await original(name, arguments, **kwargs)).model_copy(deep=True)
            result.structuredContent["dataset"] = arguments["artifactId"]
            return result
        finally:
            active -= 1

    source.call_tool = call
    receipt = await probe(binding)
    assert maximum == 4 and started == 8 and active == 0
    assert list(receipt.source_schema_hashes) == sorted(binding.sources)
    assert {arguments["artifactId"] for _, arguments in source.calls} == {
        str(identity.dataset_id) for identity in binding.sources.values()
    }
    assert all(f"source:{alias}" in receipt.evidence_hashes for alias in binding.sources)


async def test_one_concurrent_source_failure_cancels_pending_reads_and_never_issues_a_receipt():
    probe, binding, _, source, active = await multi_source_probe()
    gate = asyncio.Event()
    started = cancelled = 0

    async def call(name, arguments, **kwargs):
        nonlocal started, cancelled
        started += 1
        if started == 4:
            gate.set()
        await asyncio.wait_for(gate.wait(), 5)
        if arguments["artifactId"] == str(binding.sources["source_0"].dataset_id):
            active["value"] = False
            raise LabError("Synthetic current-reader revocation", Status.BLOCKED_AUTH)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    source.call_tool = call
    with pytest.raises(LabError, match="revocation") as error:
        await probe(binding)
    assert error.value.status == Status.BLOCKED_AUTH
    assert started == 4 and cancelled == 3


async def test_explicit_source_state_is_optional_actual_evidence():
    probe, binding, _, _, _ = await setup_probe(state_probe=fresh_state)
    receipt = await probe.probe(binding)
    assert receipt.observed_source_states["finance"].data_revision == "observed-data"
    assert receipt.evidence_hashes["state:finance"] == receipt.observed_source_states["finance"].evidence_hash


@pytest.mark.parametrize("field", ["ontology_workspace_id", "ontology_item_id"])
async def test_native_endpoint_mismatch_cannot_authorize_unrelated_ontology(field):
    probe, binding, native, source, _ = await setup_probe()
    binding = binding.model_copy(update={field: OTHER})
    with pytest.raises(LabError, match="Native endpoint") as error:
        await probe(binding)
    assert error.value.status == Status.BLOCKED_AUTH
    assert not native.calls and not source.calls


@pytest.mark.parametrize("field", ["workspace", "dataset"])
async def test_actual_source_uuid_must_match_exact_binding_alias(field):
    probe, binding, _, source, _ = await setup_probe()
    source.responses["GetSemanticModelSchema"].structuredContent[field] = str(OTHER)
    with pytest.raises(LabError, match="Observed source identity") as error:
        await probe(binding)
    assert error.value.status == Status.BLOCKED_AUTH


async def test_alias_remap_does_not_reuse_successful_unrelated_schema_probe():
    probe, binding, _, _, _ = await setup_probe()
    receipt = await probe(binding)
    rebound = binding.model_copy(deep=True)
    rebound.sources["finance"] = SourceIdentity(workspace_id=OTHER, dataset_id=OTHER)
    assert receipt.binding_hash != rebound.binding_hash
    with pytest.raises(LabError, match="Observed source identity"):
        await probe(rebound)


@pytest.mark.parametrize("change", [
    "absent-profile", "missing-evidence", "unknown-shape", "missing-column-list",
    "missing-measure-list", "partial", "count-mismatch", "duplicate-table", "duplicate-column", "empty",
])
async def test_incomplete_or_unreviewed_schema_never_yields_reader_authorization(change):
    probe, binding, native, source, _ = await setup_probe()
    profile = probe.schema_profiles["finance"]
    data = source.responses["GetSemanticModelSchema"].structuredContent
    if change == "absent-profile":
        probe.schema_profiles = {}
    elif change == "missing-evidence":
        probe.schema_profiles["finance"] = replace(profile, exhaustive_response_reference="")
    elif change == "unknown-shape":
        data["newTruncationField"] = True
    elif change == "missing-column-list":
        del data["tables"][0]["cols"]
    elif change == "missing-measure-list":
        del data["tables"][0]["metrics"]
    elif change == "partial":
        data["done"] = False
    elif change == "count-mismatch":
        data["tableCount"] = 2
    elif change == "duplicate-table":
        data["tables"].append(deepcopy(data["tables"][0]))
        data["tableCount"] = 2
    elif change == "duplicate-column":
        data["tables"][0]["cols"].append(deepcopy(data["tables"][0]["cols"][0]))
    else:
        data["tables"], data["tableCount"] = [], 0
    with pytest.raises(LabError) as error:
        await probe(binding)
    assert error.value.status == Status.BLOCKED_CAPABILITY
    if change in {"absent-profile", "missing-evidence"}:
        assert not native.calls and not source.calls


@pytest.mark.parametrize("when", ["before", "native", "source"])
async def test_session_revocation_around_probes_prevents_receipt(when):
    probe, binding, native, source, active = await setup_probe()
    if when == "before":
        active["value"] = False
    elif when == "native":
        native.on_call = lambda *_: active.update(value=False)
    else:
        source.on_call = lambda *_: active.update(value=False)
    with pytest.raises(LabError, match="no longer current") as error:
        await probe(binding)
    assert error.value.status == Status.BLOCKED_AUTH


async def test_client_scope_and_epoch_are_checked_not_just_successful_tool_calls():
    probe, binding, native, source, _ = await setup_probe()
    probe.source_client.reader_scope.authorization_revision = "unrelated-session"
    with pytest.raises(LabError, match="session epoch"):
        await probe(binding)
    assert not native.calls and not source.calls


@pytest.mark.parametrize("target", ["native", "source"])
async def test_tool_schema_changes_during_probe_block(target):
    probe, binding, native, source, _ = await setup_probe()
    session = native if target == "native" else source

    def mutate(*_):
        session.tools[0].description = "changed after captured review"

    session.on_call = mutate
    with pytest.raises(LabError, match="changed"):
        await probe(binding)


@pytest.mark.parametrize("target", ["native", "source"])
async def test_is_error_never_proves_reader_access(target):
    probe, binding, native, source, _ = await setup_probe()
    session = native if target == "native" else source
    next(iter(session.responses.values())).isError = True
    with pytest.raises(LabError, match="isError"):
        await probe(binding)


async def test_native_unknown_values_shape_is_not_invented_context():
    probe, binding, native, _, _ = await setup_probe()
    native.responses["list_ontology_entities"].structuredContent = {"entities": []}
    with pytest.raises(LabError, match="structuredContent.values"):
        await probe(binding)


@pytest.mark.parametrize("target", ["endpoint", "profile"])
async def test_endpoint_and_review_profile_remain_bound_throughout_acquisition(target):
    probe, binding, native, _, _ = await setup_probe()

    def change(*_):
        if target == "endpoint":
            probe.native_client.endpoint = Endpoint(
                probe.native_client.endpoint.url.replace(str(binding.ontology_item_id), str(OTHER)), "ontology",
            )
        else:
            profile = probe.schema_profiles["finance"]
            profile.arguments["unreviewed"] = True

    native.on_call = change
    with pytest.raises(LabError, match="endpoint|profile changed"):
        await probe(binding)


def test_fingerprint_is_exact_names_order_independent_and_not_data_clock():
    first = [{
        "name": "Account Month",
        "columns": [{"name": "b", "data_type": "Int64"}, {"name": "a", "data_type": "String"}],
        "measures": ["Revenue"],
    }]
    second = deepcopy(first)
    second[0]["columns"].reverse()
    assert schema_fingerprint(first) == schema_fingerprint(second)
    second[0]["measures"] = ["revenue"]
    assert schema_fingerprint(first) != schema_fingerprint(second)


async def test_source_type_drift_changes_fingerprint_even_when_native_values_are_unchanged():
    probe, binding, _, source, _ = await setup_probe()
    original = await probe(binding)
    source.responses["GetSemanticModelSchema"].structuredContent["tables"][0]["cols"][0]["observedType"] = "Double"
    changed = await probe(binding)
    assert changed.native_entities == original.native_entities
    assert changed.source_schema_hashes["finance"] != original.source_schema_hashes["finance"]


@pytest.mark.parametrize("value", [None, "", " ", 1, False])
async def test_missing_or_unusable_observed_column_type_blocks_schema_evidence(value):
    probe, binding, _, source, _ = await setup_probe()
    source.responses["GetSemanticModelSchema"].structuredContent["tables"][0]["cols"][0]["observedType"] = value
    with pytest.raises(LabError) as error:
        await probe(binding)
    assert error.value.status == Status.BLOCKED_CAPABILITY


async def test_missing_type_selector_field_is_not_inferred_from_catalogue():
    probe, binding, _, source, _ = await setup_probe()
    del source.responses["GetSemanticModelSchema"].structuredContent["tables"][0]["cols"][0]["observedType"]
    with pytest.raises(LabError, match="reviewed shape"):
        await probe(binding)


def test_names_only_fingerprint_and_duplicate_typed_columns_are_rejected():
    with pytest.raises(LabError, match="observed source data types"):
        schema_fingerprint([{"name": "T", "columns": ["column"], "measures": []}])
    with pytest.raises(LabError, match="Duplicate columns"):
        schema_fingerprint([{
            "name": "T",
            "columns": [{"name": "column", "data_type": "Int64"}, {"name": "column", "data_type": "String"}],
            "measures": [],
        }])


def test_source_type_spelling_is_not_coerced_or_case_folded():
    first = [{"name": "T", "columns": [{"name": "column", "data_type": "Int64"}], "measures": []}]
    second = deepcopy(first)
    second[0]["columns"][0]["data_type"] = "int64"
    assert schema_fingerprint(first) != schema_fingerprint(second)
