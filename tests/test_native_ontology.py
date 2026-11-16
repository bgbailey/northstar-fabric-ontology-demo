import base64
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from mcp.types import CallToolResult, Tool

from infra.inspect_native_ontology import definition_census
from infra.probe_native_roundtrip import probe
from infra.stage_native_roundtrip import clone_request
from telco_iq.errors import LabError
from telco_iq.mcp_client import DiscoveredClient, Endpoint

ROOT = Path(__file__).resolve().parents[1]


def test_native_bootstrap_matches_source_catalogue_without_gold_answers():
    bootstrap = json.loads((ROOT / "fabric/ontology/two-model-native-bootstrap.json").read_text())
    catalog = json.loads((ROOT / "data/contracts/catalog.json").read_text())
    models = {model["id"]: model for model in catalog["models"]}
    tables = {table["id"]: table for table in catalog["tables"]}
    for source in bootstrap["sources"]:
        assert set(source["tables"]) == {
            tables[table]["displayName"] for table in models[source["model"]]["tables"]
        }
        focus = source["focusMetric"]
        matches = [metric for metric in catalog["metrics"]
                   if metric["model"] == source["model"] and metric["name"] == focus["name"]]
        assert len(matches) == 1
        assert tables[matches[0]["table"]]["displayName"] == focus["homeTable"]
        assert matches[0]["unit"] == focus["unit"]
    assert bootstrap["status"] == "STAGED_NOT_APPLIED"
    assert bootstrap["limits"]["maximumDefinitionApplications"] == 1
    assert "80,336" not in json.dumps(bootstrap)
    assert "80336" not in json.dumps(bootstrap)


def test_empty_generation_two_is_not_reported_as_native_metric_proof():
    definition = {"definition": {"parts": [
        {"path": "database.tmdl", "payloadType": "InlineBase64",
         "payload": base64.b64encode(b"database\n\tcompatibilityLevel: 1000000\n").decode()},
        {"path": "model.tmdl", "payloadType": "InlineBase64",
         "payload": base64.b64encode(b"model Model\n\tref namespace default\n").decode()},
    ]}}
    census = definition_census(definition)
    assert census["compatibility_1000000"]
    assert census["entity_parts"] == census["metric_parts"] == 0
    assert census["source_proxy_measures"] == 0
    assert "neither counts" in census["limitation"]
    definition["definition"]["parts"].append(definition["definition"]["parts"][0])
    with pytest.raises(ValueError, match="duplicate"):
        definition_census(definition)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_observed_source_proxy_measures_are_not_top_level_metric_parts(newline):
    text = (
        "table 'Subscription Month'\n"
        '\tmeasure Lines = EXTERNALMEASURE("Lines", INTEGER, "Synthetic AS source")\n'
        "\t\tannotation ONT_ItemName = SyntheticSubscriptions\n"
        "\t\tannotation rule_snapshot = Never sum snapshots over time.\n"
    )
    text = text.replace("\n", newline)
    census = definition_census({"definition": {"parts": [{
        "path": "tables/Subscription Month.tmdl", "payloadType": "InlineBase64",
        "payload": base64.b64encode(text.encode()).decode(),
    }]}})
    assert census["source_proxy_measures"] == 1
    assert census["source_model_names"] == ["SyntheticSubscriptions"]
    assert census["guidance_annotations"] == 1
    assert census["metric_parts"] == census["rule_parts"] == 0


async def test_observed_generation_two_entity_tool_requires_reviewed_schema():
    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            assert name == "list_ontology_entities"
            assert arguments == {}
            return CallToolResult(content=[])

    client = DiscoveredClient(Session(), Endpoint(
        "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/"
        "00000000-0000-0000-0000-000000000001/items/"
        "00000000-0000-0000-0000-000000000002/ontologyEndpoint", "ontology",
    ), "2025-06-18")
    client.tools["list_ontology_entities"] = Tool(
        name="list_ontology_entities", inputSchema={"type": "object", "properties": {}},
    )
    with pytest.raises(LabError, match="require review"):
        await client.call("list_ontology_entities", {})
    client.reviewed_hashes["list_ontology_entities"] = client.schema_hash("list_ontology_entities")
    assert not (await client.call("list_ontology_entities", {})).isError


def test_api_clone_preserves_every_native_part_except_disposable_platform_identity():
    original = {"definition": {"parts": [
        {"path": ".platform", "payloadType": "InlineBase64", "payload": base64.b64encode(json.dumps({
            "metadata": {"type": "Ontology", "displayName": "Original"},
            "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000001"},
        }).encode()).decode()},
        {"path": "tables/Account Month.tmdl", "payloadType": "InlineBase64",
         "payload": base64.b64encode(b"exact source-proxy bytes\r\n").decode()},
    ]}}
    before = json.dumps(original)
    clone = clone_request(original, "Disposable", "00000000-0000-0000-0000-000000000002")
    assert json.dumps(original) == before
    assert clone["definition"]["parts"][1:] == original["definition"]["parts"][1:]
    platform = json.loads(base64.b64decode(clone["definition"]["parts"][0]["payload"]))
    assert platform["metadata"]["displayName"] == "Disposable"
    assert platform["config"]["logicalId"].endswith("2")
    with pytest.raises(ValueError, match="distinct"):
        clone_request(original, "Original", "00000000-0000-0000-0000-000000000002")


@pytest.mark.parametrize("failure", [None, "mcp", "create_readback"])
async def test_api_probe_cleans_only_returned_clone_even_after_failure(tmp_path, monkeypatch, failure):
    workspace, original, clone = ("00000000-0000-0000-0000-00000000000" + n for n in ("1", "2", "3"))
    definition = {"definition": {"parts": [{
        "path": "database.tmdl", "payloadType": "InlineBase64",
        "payload": base64.b64encode(b"database\n\tcompatibilityLevel: 1000000\n").decode(),
    }]}}
    body = {"displayName": "Disposable", "type": "Ontology", "definition": definition["definition"]}
    payload = json.dumps(body).encode()
    (tmp_path / "request.json").write_bytes(payload)
    (tmp_path / "native-roundtrip-approval.json").write_text(json.dumps({
        "approved": True, "deadlineUtc": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "requestFile": "request.json", "requestSha256": hashlib.sha256(payload).hexdigest(),
        "workspaceId": workspace, "targetName": "Disposable", "tenantId": "synthetic",
        "maximumElapsedMinutes": 15,
    }))

    class Response:
        status_code = 200

        def json(self):
            return definition

    class AuthorFixture:
        private = tmp_path
        approval = {"tenantId": "synthetic", "capacityId": "synthetic"}
        deleted = []

        def owned(self, key):
            return {"id": workspace if key == "workspace" else original}

        def listing(self, path, **kwargs):
            if path == "/v1/capacities":
                return [{"id": "synthetic", "state": "Active", "sku": "F8"}]
            return [{"id": original}] if self.deleted else [{"id": original}, {"id": clone}]

        def request(self, method, path, **kwargs):
            if method == "DELETE":
                assert path == f"/v1/workspaces/{workspace}/items/{clone}"
                self.deleted.append(clone)
            else:
                assert method == "POST" and path.endswith("/getDefinition")
            return Response()

        def completed(self, response, **kwargs):
            return response.json()

        def create_item(self, key, request, **kwargs):
            assert request == body
            item = {"id": clone, "displayName": "Disposable"}
            (tmp_path / "p0-ontology-roundtrip.json").write_text(json.dumps(item))
            if failure == "create_readback":
                raise RuntimeError("synthetic create readback failure")
            return item

        def az_json(self, *args):
            return {"accessToken": "synthetic-unit-token"}

    class Client:
        async def call(self, name, arguments):
            return CallToolResult(content=[], structuredContent={"values": []})

    @asynccontextmanager
    async def connect(*args, **kwargs):
        if failure == "mcp":
            raise RuntimeError("synthetic MCP failure")
        yield Client()

    monkeypatch.setattr(DiscoveredClient, "connect", connect)
    author = AuthorFixture()
    if failure:
        with pytest.raises(RuntimeError, match="synthetic"):
            await probe(author)
    else:
        result = await probe(author)
        assert result["status"] == "PASS" and result["cleanup"] == "DELETED_VERIFIED"
    assert author.deleted == [clone]
    assert len(list((tmp_path / "native-roundtrip-receipts").glob("*.json"))) == 1
