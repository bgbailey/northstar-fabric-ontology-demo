import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import pytest
import pytest_asyncio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, Tool, ToolAnnotations
from pydantic import BaseModel, ConfigDict

from telco_iq.contracts import Status
from telco_iq.errors import LabError
from telco_iq.mcp_client import (
    DiscoveredClient,
    Endpoint,
    IQAdapter,
    OntologyAdapter,
    ReviewedCSVOutput,
    ReviewedOutput,
)


class SyntheticEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[dict]
    complete: bool
    total: int
    errors: list[str]


@pytest_asyncio.fixture
async def server():
    app = FastMCP("SYNTHETIC_PROTOCOL_TEST_NOT_FABRIC", stateless_http=True, json_response=True)

    @app.tool(name="ExecuteQuery", annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def execute(artifactId: str, daxQueries: list[str], maxRows: int = 250) -> SyntheticEnvelope:
        count = int(daxQueries[0])
        return SyntheticEnvelope(
            rows=[{"key": i, "nullable": None, "text": 'a,"b"\nline', "cents": 9_000_000_000 + i}
                  for i in range(min(count, maxRows))],
            complete=count <= maxRows, total=count, errors=[],
        )

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app.streamable_http_app(), log_level="warning", lifespan="on")
    runner = uvicorn.Server(config)
    task = asyncio.create_task(runner.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not runner.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        runner.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


@pytest.mark.parametrize("count", [0, 200, 201, 250, 251, 1000, 1001])
async def test_actual_sdk_http_roundtrip_and_bounds(server, count):
    async with DiscoveredClient.connect(Endpoint(server, "synthetic_test")) as client:
        inventory = client.inventory()
        assert inventory["protocol"]
        assert inventory["lane"] == "synthetic_test"
        mapping = ReviewedOutput(
            schema_hash=client.schema_hash("ExecuteQuery"),
            rows_pointer="/rows", complete_pointer="/complete", total_rows_pointer="/total",
            errors_pointer="/errors", output_schema=SyntheticEnvelope.model_json_schema(),
            evidence="synthetic-test-server-code-not-a-Fabric-schema",
        )
        adapter = IQAdapter(client, mapping)
        if count > 250:
            with pytest.raises(LabError) as error:
                await adapter.execute("synthetic-item", str(count), 250)
            assert error.value.status == Status.PARTIAL
        else:
            rows, _ = await adapter.execute("synthetic-item", str(count), 250)
            assert len(rows) == count
            if count:
                assert rows[0]["nullable"] is None
                assert rows[0]["text"] == 'a,"b"\nline'
                assert rows[0]["cents"] == 9_000_000_000


async def test_unknown_output_does_not_become_success(server):
    async with DiscoveredClient.connect(Endpoint(server, "synthetic_test")) as client:
        with pytest.raises(LabError, match="not been reviewed"):
            await IQAdapter(client).execute("fixture", "1", 250)
        with pytest.raises(LabError, match="input schema"):
            await client.call("ExecuteQuery", {"inventedDatasetId": "x"})
        with pytest.raises(LabError, match="allowlist"):
            await client.call("updateDefinition", {})
        with pytest.raises(LabError, match="separate direct"):
            OntologyAdapter(client)


async def test_single_attempt_sdk_connection_never_installs_the_automatic_retry_transport(server, monkeypatch):
    def forbidden_retry(**kwargs):
        raise AssertionError("Single-attempt capture cannot install an automatic retry transport")
    monkeypatch.setattr("telco_iq.mcp_client.ReadOnlyRetryTransport", forbidden_retry)
    async with DiscoveredClient.connect(Endpoint(server, "synthetic_test"), single_attempt=True) as client:
        result = await client.call("ExecuteQuery", {
            "artifactId": "synthetic-item", "daxQueries": ["1"], "maxRows": 5,
        })
        assert result.isError is False and result.structuredContent["total"] == 1


async def test_public_sdk_capture_retains_returned_result_before_advertised_output_validation(server):
    captures = []

    def capture(tool, arguments, original, kind):
        captures.append((tool, arguments, original, kind))

    async with DiscoveredClient.connect(Endpoint(server, "synthetic_test")) as client:
        client.tools["ExecuteQuery"].outputSchema = {"type": "object", "required": ["notReturned"]}
        arguments = {"artifactId": "synthetic-item", "daxQueries": ["1"], "maxRows": 2}
        with pytest.raises(LabError, match="discovered MCP output schema"):
            await client.call("ExecuteQuery", arguments, capture=capture)
        assert len(captures) == 1
        tool, retained_request, original, kind = captures[0]
        assert tool == "ExecuteQuery" and retained_request == arguments and kind == "MCP_TOOL_RESULT"
        assert original["structuredContent"]["rows"][0]["nullable"] is None
        assert original["structuredContent"]["rows"][0]["cents"] == 9_000_000_000


async def test_capture_failure_and_mutation_cannot_create_a_validated_success(server):
    async with DiscoveredClient.connect(Endpoint(server, "synthetic_test")) as client:
        arguments = {"artifactId": "synthetic-item", "daxQueries": ["1"], "maxRows": 2}

        def failure(*args):
            raise LabError("synthetic storage unavailable")

        with pytest.raises(LabError, match="storage unavailable"):
            await client.call("ExecuteQuery", arguments, capture=failure)

        def mutate(tool, request, original, kind):
            request.clear()
            original["isError"] = True
            original["structuredContent"] = None

        result = await client.call("ExecuteQuery", arguments, capture=mutate)
        assert result.structuredContent["complete"] is True
        assert arguments["artifactId"] == "synthetic-item"


async def test_capture_never_bypasses_reviewed_input_tool_or_read_only_gates(server):
    captures = []
    async with DiscoveredClient.connect(Endpoint(server, "synthetic_test")) as client:
        for name, arguments in (
            ("updateDefinition", {}), ("ExecuteQuery", {"invented": "not-a-reviewed-argument"}),
        ):
            with pytest.raises(LabError):
                await client.call(name, arguments, capture=lambda *args: captures.append(args))
        client.tools["ExecuteQuery"].annotations.destructiveHint = True
        with pytest.raises(LabError, match="Destructive"):
            await client.call("ExecuteQuery", {"artifactId": "x", "daxQueries": ["1"], "maxRows": 2},
                              capture=lambda *args: captures.append(args))
    assert not captures


async def test_non_sdk_capture_transport_must_be_explicitly_synthetic(server):
    async with DiscoveredClient.connect(Endpoint(server, "synthetic_test")) as client:
        client.session = SimpleNamespace()
        with pytest.raises(LabError, match="explicit synthetic mode"):
            await client.call(
                "ExecuteQuery", {"artifactId": "unit", "daxQueries": ["1"], "maxRows": 2},
                capture=lambda *args: None,
            )


@asynccontextmanager
async def schema_client(output_schema, structured):
    """Real SDK sessions over memory streams, not an external service."""
    server = Server("SYNTHETIC_SCHEMA_POLICY_TEST")

    @server.list_tools()
    async def tools():
        return [Tool(name="ExecuteQuery", inputSchema={"type": "object"}, outputSchema=output_schema)]

    @server.call_tool()
    async def execute(name, arguments):
        return CallToolResult(content=[], structuredContent=structured)

    async with create_connected_server_and_client_session(server) as session:
        client = DiscoveredClient(session, Endpoint("http://127.0.0.1/mcp", "synthetic_test"), "synthetic-memory")
        await client.discover()
        yield client


@pytest.mark.parametrize("captured", [False, True])
async def test_sdk_and_capture_paths_deny_external_schema_references_without_retrieval(monkeypatch, captured):
    retrievals, captures = [], []

    def forbidden_retrieval(uri, *args, **kwargs):
        retrievals.append(uri)
        return BytesIO(b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object"}')

    monkeypatch.setattr("urllib.request.urlopen", forbidden_retrieval)
    async with schema_client({"$ref": "https://schemas.invalid/unapproved.json"}, {"Cents": 1}) as client:
        options = {"capture": lambda *args: captures.append(args)} if captured else {}
        with pytest.raises((LabError, RuntimeError), match="schema|reference"):
            await client.call("ExecuteQuery", {}, **options)
    assert retrievals == []
    assert len(captures) == int(captured)
    if captures:
        assert captures[0][2]["structuredContent"] == {"Cents": 1}


@pytest.mark.parametrize("captured", [False, True])
@pytest.mark.parametrize("valid", [False, True])
async def test_local_schema_references_still_validate_exact_output(captured, valid):
    schema = {
        "$defs": {"amount": {"type": "integer"}}, "type": "object",
        "properties": {"Cents": {"$ref": "#/$defs/amount"}}, "required": ["Cents"],
        "additionalProperties": False,
    }
    value = 9007199254740993 if valid else "not-an-integer"
    captures = []
    async with schema_client(schema, {"Cents": value}) as client:
        options = {"capture": lambda *args: captures.append(args)} if captured else {}
        if valid:
            result = await client.call("ExecuteQuery", {}, **options)
            assert result.structuredContent == {"Cents": value}
        else:
            with pytest.raises((LabError, RuntimeError), match="schema|structured content"):
                await client.call("ExecuteQuery", {}, **options)
    assert len(captures) == int(captured)


async def test_live_without_verified_auth_fails_before_network():
    with pytest.raises(LabError) as error:
        async with DiscoveredClient.connect(Endpoint(
            "https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq", "iq",
        )):
            pytest.fail("Must not connect")
    assert error.value.status == Status.BLOCKED_AUTH


def test_endpoint_separation_and_variant():
    assert Endpoint("https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq", "iq").headers == {
        "X-Variants": "Fabric.Routing.FabricIQ.V1",
    }
    with pytest.raises(LabError):
        Endpoint("https://api.fabric.microsoft.com/v1/mcp/fabriciq", "ontology").validate()
    with pytest.raises(LabError):
        Endpoint("http://example.com/mcp", "synthetic_test").validate()
    assert Endpoint(
        "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/"
        "00000000-0000-0000-0000-000000000001/items/"
        "00000000-0000-0000-0000-000000000002/ontologyEndpoint", "ontology",
    ).headers == {}


def csv_fixture():
    result = CallToolResult(
        content=[
            {"type": "text", "text": "INLINE PREVIEW IS NOT THE FULL DATASET"},
            {"type": "resource", "resource": {
                "uri": "synthetic://unit-test/full.csv", "mimeType": "text/csv",
                "text": 'key,text,cents,nullable\n1,"a,""b""\nline",9007199254740993,NULL\n',
            }},
        ],
        structuredContent={"complete": True, "total": 1, "errors": []},
    )
    schema = CallToolResult.model_json_schema(by_alias=False)
    schema["additionalProperties"] = False
    schema["properties"]["structuredContent"] = {
        "type": "object", "additionalProperties": False, "required": ["complete", "total", "errors"],
        "properties": {
            "complete": {"type": "boolean"}, "total": {"type": "integer"},
            "errors": {"type": "array", "items": {"type": "string"}},
        },
    }
    profile = ReviewedCSVOutput(
        schema_hash="a" * 64, csv_pointer="/content/1/resource/text",
        complete_pointer="/structuredContent/complete", total_rows_pointer="/structuredContent/total",
        errors_pointer="/structuredContent/errors", output_schema=schema,
        evidence="EXPLICIT SYNTHETIC CSV PROFILE, NOT A GA OUTPUT CONTRACT",
        columns={"key": "int64", "text": "string", "cents": "int64", "nullable": "int64"},
        null_marker="NULL",
    )
    return result, profile


def test_reviewed_csv_reads_full_resource_and_preserves_exact_integers_nulls_and_quoting():
    result, profile = csv_fixture()
    assert profile.decode(result, "a" * 64) == [{
        "key": 1, "text": 'a,"b"\nline', "cents": 9007199254740993, "nullable": None,
    }]


@pytest.mark.parametrize("change", ["partial", "count", "errors", "unknown", "isError"])
def test_csv_incomplete_or_unreviewed_envelope_cannot_become_complete(change):
    result, profile = csv_fixture()
    if change == "partial":
        result.structuredContent["complete"] = False
    elif change == "count":
        result.structuredContent["total"] = 100
    elif change == "errors":
        result.structuredContent["errors"] = ["failed"]
    elif change == "unknown":
        result.structuredContent["truncated"] = True
    else:
        result.isError = True
    with pytest.raises(LabError):
        profile.decode(result, "a" * 64)


def test_csv_unknown_null_semantics_or_tool_schema_requires_new_review():
    result, profile = csv_fixture()
    with pytest.raises(LabError, match="not been reviewed"):
        replace(profile, null_marker=None).decode(result, "a" * 64)
    with pytest.raises(LabError, match="not been reviewed"):
        profile.decode(result, "b" * 64)
