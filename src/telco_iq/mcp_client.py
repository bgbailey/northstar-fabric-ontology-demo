from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import AsyncIterator, Literal
from urllib.parse import urlparse
from uuid import UUID

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, ClientRequest, Tool
from pydantic import RootModel, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from telco_iq.catalog import CompiledDax, digest
from telco_iq.completeness import embedded_csv
from telco_iq.contracts import Scope, Status
from telco_iq.errors import LabError
from telco_iq.retry import ReadOnlyRetryTransport

IQ_TOOLS = frozenset({
    "DiscoverArtifacts", "ResolveFabricItem", "GetReportMetadata",
    "GetSemanticModelSchema", "ValueSearch", "ExecuteQuery",
})
ONTOLOGY_READ_TOOLS = frozenset({
    "list_ontology_entities", "list_ontology_entity_types", "list_ontology_rules", "ask_ontology",
})
IQ_ENDPOINTS = frozenset({
    "https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq",
    "https://api.fabric.microsoft.com/v1/mcp/fabriciq",
})
CaptureKind = Literal["MCP_TOOL_RESULT", "MCP_PROTOCOL_ERROR", "SDK_RESULT_UNOBTAINABLE"]
ResultCapture = Callable[[str, dict, dict, CaptureKind], None]


class MCPToolError(LabError):
    """A tool result explicitly reports a remote failure."""


def mcp_capture_failure_status(error: Exception) -> Status:
    """Classify known SDK task-group failures without hiding unknown defects or exhausted budgets."""
    pending, causes = [error], []
    while pending:
        cause = pending.pop()
        if isinstance(cause, ExceptionGroup):
            pending.extend(cause.exceptions)
        else:
            causes.append(cause)
    if any(not isinstance(cause, (httpx.HTTPError, McpError, LabError, TimeoutError)) for cause in causes):
        raise error
    if any(isinstance(cause, (TimeoutError, httpx.TimeoutException)) or isinstance(cause, LabError)
           and cause.status == Status.TIMED_OUT for cause in causes):
        raise LabError("MCP capture exceeded its approved time budget", Status.TIMED_OUT) from None
    if any(isinstance(cause, LabError) and cause.status == Status.PARTIAL for cause in causes):
        raise LabError("MCP capture exhausted its approved limits", Status.PARTIAL) from None
    denied = any(
        isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in (401, 403)
        or isinstance(cause, LabError) and cause.status == Status.BLOCKED_AUTH
        for cause in causes
    )
    return Status.BLOCKED_AUTH if denied else Status.BLOCKED_CAPABILITY


def _schema_valid(schema: dict, value: object) -> bool:
    try:
        # Like SDK 1.30, an explicit empty registry permits local references
        # without the jsonschema default's automatic external retrieval.
        return Draft202012Validator(schema, registry=Registry()).is_valid(value)
    except Unresolvable:
        raise LabError("Schema references must resolve locally; external retrieval is forbidden",
                       Status.BLOCKED_CAPABILITY) from None


@dataclass(frozen=True)
class Endpoint:
    url: str
    kind: Literal["iq", "ontology", "synthetic_test"]

    def validate(self) -> None:
        url = urlparse(self.url)
        if url.username or url.password or url.query or url.fragment:
            raise LabError("Endpoint must not contain credentials, query or fragment")
        if self.kind == "synthetic_test":
            if url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1"}:
                raise LabError("Synthetic MCP test transport is loopback-only")
        elif self.kind == "iq":
            if self.url not in IQ_ENDPOINTS:
                raise LabError("Not an approved GA IQ endpoint")
        elif self.kind == "ontology":
            import re
            import uuid
            match = re.fullmatch(
                r"/v1/mcp/dataPlane/workspaces/([^/]+)/items/([^/]+)/ontologyEndpoint", url.path
            )
            if url.scheme != "https" or url.netloc != "api.fabric.microsoft.com" or not match:
                raise LabError("Not a documented direct ontology endpoint")
            for value in match.groups():
                try:
                    uuid.UUID(value)
                except ValueError as error:
                    raise LabError("Ontology endpoint requires resolved item UUIDs") from error

    @property
    def headers(self) -> dict[str, str]:
        headers = {}
        if self.kind == "iq":
            headers["X-Variants"] = "Fabric.Routing.FabricIQ.V1"
        return headers


class DiscoveredClient:
    def __init__(
        self, session: ClientSession, endpoint: Endpoint, protocol: str,
        reviewed_hashes: dict[str, str] | None = None,
        *, reader_scope: Scope | None = None,
    ):
        self.session, self.endpoint, self.protocol = session, endpoint, protocol
        self.reviewed_hashes = reviewed_hashes or {}
        self.tools: dict[str, Tool] = {}
        self.reader_scope = reader_scope.model_copy(deep=True) if reader_scope else None

    @asynccontextmanager
    async def connect(
        endpoint: Endpoint, *, auth: httpx.Auth | None = None,
        reviewed_hashes: dict[str, str] | None = None, timeout: float = 45,
        reader_scope: Scope | None = None, single_attempt: bool = False,
    ) -> AsyncIterator[DiscoveredClient]:
        endpoint.validate()
        if endpoint.kind != "synthetic_test" and auth is None:
            raise LabError(
                "Verified per-user delegated authentication required; no ambient CLI token fallback",
                Status.BLOCKED_AUTH,
            )
        async with httpx.AsyncClient(
            headers=endpoint.headers, auth=auth,
            timeout=httpx.Timeout(timeout), follow_redirects=False,
            transport=httpx.AsyncHTTPTransport() if single_attempt else ReadOnlyRetryTransport(max_wait=timeout),
        ) as http:
            async with streamable_http_client(endpoint.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    async with asyncio.timeout(timeout):
                        initialized = await session.initialize()
                        client = DiscoveredClient(
                            session, endpoint, initialized.protocolVersion, reviewed_hashes,
                            reader_scope=reader_scope,
                        )
                        await client.discover()
                    yield client

    async def discover(self) -> None:
        tools, cursor, seen = {}, None, set()
        for _ in range(100):
            result = await self.session.list_tools(cursor=cursor)
            for tool in result.tools:
                if tool.name in tools:
                    raise LabError("Duplicate tool name in discovery")
                Draft202012Validator.check_schema(tool.inputSchema)
                if tool.outputSchema is not None:
                    Draft202012Validator.check_schema(tool.outputSchema)
                tools[tool.name] = tool
            cursor = result.nextCursor
            if not cursor:
                self.tools = tools
                return
            if cursor in seen:
                raise LabError("Repeated tool discovery cursor")
            seen.add(cursor)
        raise LabError("Tool discovery exceeded pagination budget", Status.PARTIAL)

    def schema_hash(self, name: str) -> str:
        if name not in self.tools:
            raise LabError("Required tool is absent from current discovery", Status.BLOCKED_CAPABILITY)
        tool = self.tools[name]
        return digest(tool.model_dump(mode="json", exclude_none=True))

    def inventory(self) -> dict:
        return {
            "provenance": "ACTUAL_MCP_INITIALIZE_AND_TOOLS_LIST",
            "lane": self.endpoint.kind, "protocol": self.protocol,
            "tools": [
                {"schema_hash": self.schema_hash(name), "definition": tool.model_dump(mode="json")}
                for name, tool in sorted(self.tools.items())
            ],
        }

    async def call(
        self, name: str, arguments: dict, timeout: float = 45, *,
        capture: ResultCapture | None = None, synthetic_capture: bool = False,
    ) -> CallToolResult:
        allowed = ONTOLOGY_READ_TOOLS if self.endpoint.kind == "ontology" else IQ_TOOLS
        if name not in allowed or name not in self.tools:
            raise LabError("Tool is absent or outside runtime read-only allowlist", Status.BLOCKED_CAPABILITY)
        tool = self.tools[name]
        tool_hash = self.schema_hash(name)
        if self.endpoint.kind == "ontology" and self.reviewed_hashes.get(name) != self.schema_hash(name):
            raise LabError("Ontology tool effects/schema require review before invocation", Status.BLOCKED_CAPABILITY)
        if tool.annotations and tool.annotations.destructiveHint is True:
            raise LabError("Destructive tool annotation contradicts read-only runtime")
        if not _schema_valid(tool.inputSchema, arguments):
            # Don't echo argument values: they could contain security-sensitive filters.
            raise LabError("Arguments do not conform to discovered MCP input schema")
        if capture is not None and not isinstance(self.session, ClientSession) and not synthetic_capture:
            raise LabError("Non-SDK capture transports require explicit synthetic mode", Status.BLOCKED_CAPABILITY)
        async with asyncio.timeout(timeout):
            if capture is None:
                result = await self.session.call_tool(
                    name, arguments, read_timeout_seconds=timedelta(seconds=timeout),
                )
            else:
                # call_tool validates before returning. Public send_request exposes
                # the result object first; all application validators still run below.
                try:
                    if isinstance(self.session, ClientSession):
                        returned = await self.session.send_request(
                            ClientRequest(CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments))),
                            RootModel[dict[str, object]],
                            request_read_timeout_seconds=timedelta(seconds=timeout),
                        )
                        original = returned.root
                    else:
                        fixture = await self.session.call_tool(
                            name, arguments, read_timeout_seconds=timedelta(seconds=timeout),
                        )
                        original = fixture.model_dump(mode="json", exclude_unset=True)
                except McpError as error:
                    capture(name, deepcopy(arguments), error.error.model_dump(mode="json", exclude_unset=True),
                            "MCP_PROTOCOL_ERROR")
                    raise LabError("MCP protocol error retained; no tool result is available",
                                   Status.BLOCKED_CAPABILITY) from None
                except ValidationError:
                    capture(name, deepcopy(arguments), {
                        "failure_type": "SDK_RESULT_VALIDATION_ERROR", "tool_result_available": False,
                        "limit": "Supported SDK did not expose the original protocol-invalid result or wire bytes",
                    }, "SDK_RESULT_UNOBTAINABLE")
                    raise LabError("SDK did not expose an original tool result; failure evidence only",
                                   Status.BLOCKED_CAPABILITY) from None
                capture(name, deepcopy(arguments), deepcopy(original), "MCP_TOOL_RESULT")
                try:
                    result = CallToolResult.model_validate(original)
                except ValidationError:
                    raise LabError("Returned MCP tool result is malformed; structured result retained",
                                   Status.BLOCKED_CAPABILITY) from None
        if self.schema_hash(name) != tool_hash:
            raise LabError("Tool definition changed during execution", Status.BLOCKED_CAPABILITY)
        if result.isError:
            raise MCPToolError("MCP tool returned isError; no data total is available")
        if len(result.model_dump_json().encode()) > 16_000_000:
            raise LabError("MCP response exceeds adapter byte budget", Status.PARTIAL)
        if tool.outputSchema:
            if result.structuredContent is None:
                raise LabError("Tool advertised structured output but did not return it", Status.BLOCKED_CAPABILITY)
            if not _schema_valid(tool.outputSchema, result.structuredContent):
                raise LabError("Result violates discovered MCP output schema", Status.BLOCKED_CAPABILITY)
        return result


def pointer(value: object, path: str) -> object:
    if path == "":
        return value
    if not path.startswith("/"):
        raise LabError("Reviewed output selectors must be JSON pointers")
    current = value
    for part in path[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and key in current:
            current = current[key]
        elif isinstance(current, list) and key.isdecimal() and str(int(key)) == key and int(key) < len(current):
            current = current[int(key)]
        else:
            raise LabError("Reviewed result field absent", Status.BLOCKED_CAPABILITY)
    return current


@dataclass(frozen=True)
class DecodedRows:
    rows: list[dict]
    status: Status
    total_rows: int
    evidence: str
    transport_rows: int | None = None
    witness_rows: int = 0


def observed_int64(value: object) -> int:
    """Only exact Python integers or canonical integer strings; never formatted measures."""
    if type(value) is str and len(value) <= 20 and re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
        converted = int(value)
        if str(converted) == value:
            value = converted
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise LabError("Observed Int64 requires exact int64; no formatted or lossy coercion",
                       Status.BLOCKED_CAPABILITY)
    return value


def _reviewed_grouped_int64(value: object) -> int:
    if type(value) is str and len(value) <= 26 and re.fullmatch(r"-?[1-9][0-9]{0,2}(?:,[0-9]{3})+", value):
        return observed_int64(value.replace(",", ""))
    return observed_int64(value)


def _observed_json(text: object) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Non-JSON numeric constant")

    try:
        if not isinstance(text, str):
            raise ValueError("Text required")
        data = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(data, dict):
            raise ValueError("Object required")
        return data
    except (ValueError, TypeError, RecursionError):
        raise LabError("Observed IQ text must be one strict JSON object without duplicate keys",
                       Status.BLOCKED_CAPABILITY) from None


def _observed_body(raw: dict) -> dict:
    content = raw.get("content")
    if (
        set(raw) - {"content", "structuredContent", "isError"}
        or "isError" in raw and type(raw["isError"]) is not bool or raw.get("isError", False)
        or not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
        or set(content[0]) != {"type", "text"} or content[0]["type"] != "text"
    ):
        raise LabError("Result violates the observed IQ single JSON-text envelope", Status.BLOCKED_CAPABILITY)
    return _observed_json(content[0]["text"])


def observed_iq_transport_rows(response: dict) -> int:
    """Count transport rows, including witnesses, before the application decoder can refuse."""
    data = _observed_body(response)
    tables = pointer(data, "/executionResult/tables")
    if not isinstance(tables, list) or any(
        not isinstance(table, dict) or not isinstance(table.get("rows"), list) for table in tables
    ):
        raise LabError("Observed IQ transport rows have unknown shape", Status.BLOCKED_CAPABILITY)
    return sum(len(table["rows"]) for table in tables)


def _observed_iq_identity(data: dict, structured: object, artifact_id: UUID, workspace_id: UUID | None) -> None:
    if not isinstance(structured, dict) or set(structured) != {"artifact_citation"}:
        raise LabError("Unknown observed IQ metadata/error/truncation fields", Status.BLOCKED_CAPABILITY)
    fields = {"ArtifactId", "Name", "Description", "Url", "IconUrl"}
    models = [data["semanticModel"], structured["artifact_citation"]]
    for model in models:
        if not isinstance(model, dict) or set(model) != fields or any(type(v) is not str for v in model.values()):
            raise LabError("Observed IQ artifact metadata differs from the reviewed shape", Status.BLOCKED_CAPABILITY)
        if model["ArtifactId"] != str(artifact_id):
            raise LabError("Observed IQ artifact does not match the requested source", Status.BLOCKED_AUTH)
        url = urlparse(model["Url"])
        match = re.fullmatch(r"/groups/([^/]+)/modeling/([^/]+)/modelView", url.path)
        if (
            url.scheme != "https" or url.netloc != "app.powerbi.com" or url.query or url.fragment
            or not match or match[2] != str(artifact_id)
            or workspace_id is not None and match[1] != str(workspace_id)
        ):
            raise LabError("Observed IQ artifact URL differs from its bound source", Status.BLOCKED_AUTH)
    if any(models[0][field] != models[1][field] for field in fields - {"IconUrl"}):
        raise LabError("Observed IQ citation and result source metadata disagree", Status.BLOCKED_CAPABILITY)


def observed_iq_schema(result: CallToolResult, artifact_id: UUID, workspace_id: UUID) -> dict:
    """Observed AI-visible schema projection, not a physical-model completeness assertion."""
    data = _observed_body(result.model_dump(mode="json", exclude_unset=True))
    if set(data) != {"schema", "semanticModel"} or not isinstance(data["schema"], dict) or (
        set(data["schema"]) not in (
            {"Tables", "ActiveRelationships"},
            {"Tables", "ActiveRelationships", "CustomInstructions"},
        )
    ):
        raise LabError("Unknown observed IQ schema/error/truncation fields", Status.BLOCKED_CAPABILITY)
    _observed_iq_identity(data, result.structuredContent, artifact_id, workspace_id)
    schema = data["schema"]
    if "CustomInstructions" in schema and (
        type(schema["CustomInstructions"]) is not str or len(schema["CustomInstructions"]) > 10000
    ):
        raise LabError("Observed IQ AI instructions are unreviewed or oversized", Status.BLOCKED_CAPABILITY)
    if not isinstance(schema["Tables"], list) or not schema["Tables"] or not isinstance(
        schema["ActiveRelationships"], list,
    ):
        raise LabError("Observed IQ schema tables/relationships are not reviewed arrays", Status.BLOCKED_CAPABILITY)
    for table in schema["Tables"]:
        if (
            not isinstance(table, dict) or not {"Name", "Description", "Columns"} <= set(table)
            or set(table) - {"Name", "Description", "Columns", "Measures"}
            or type(table["Name"]) is not str or not table["Name"]
            or type(table["Description"]) is not str or not isinstance(table["Columns"], list)
            or "Measures" in table and not isinstance(table["Measures"], list)
        ):
            raise LabError("Observed IQ schema table differs from reviewed projection", Status.BLOCKED_CAPABILITY)
        for column in table["Columns"]:
            if (
                not isinstance(column, dict) or not {"Name", "Description", "Type"} <= set(column)
                or set(column) - {"Name", "Description", "Type", "FormatString"}
                or any(type(value) is not str for value in column.values()) or not column["Name"]
                or column["Type"] not in {"Integer", "Double", "Text", "DateTime", "Boolean"}
            ):
                raise LabError("Observed IQ schema column/type is unreviewed", Status.BLOCKED_CAPABILITY)
        for measure in table.get("Measures", []):
            if (
                not isinstance(measure, dict)
                or set(measure) != {"Name", "Description", "Type", "DisplayFolder", "FormatString"}
                or any(type(value) is not str for value in measure.values()) or not measure["Name"]
                or measure["Type"] not in {"Integer", "Double"}
            ):
                raise LabError("Observed IQ schema measure is unreviewed", Status.BLOCKED_CAPABILITY)
    if any(
        not isinstance(edge, dict) or set(edge) != {"PK", "FK", "UnidirectionalFilter"}
        or any(type(value) is not str or not value for value in edge.values())
        for edge in schema["ActiveRelationships"]
    ):
        raise LabError("Observed IQ schema relationships are unreviewed", Status.BLOCKED_CAPABILITY)
    return schema


def observed_iq_table(
    result: CallToolResult, artifact_id: UUID, workspace_id: UUID | None = None,
) -> dict:
    """Observed JSON-text envelope only; this helper makes NO completeness claim."""
    raw = result.model_dump(mode="json", exclude_unset=True)
    data = _observed_body(raw)
    if (
        set(data) != {"executionResult", "semanticModel"}
        or not isinstance(data["executionResult"], dict) or set(data["executionResult"]) != {"tables"}
    ):
        raise LabError("Unknown observed IQ metadata/error/truncation fields", Status.BLOCKED_CAPABILITY)
    _observed_iq_identity(data, result.structuredContent, artifact_id, workspace_id)
    tables = data["executionResult"]["tables"]
    if not isinstance(tables, list) or len(tables) != 1:
        raise LabError("Observed IQ requires exactly one result table", Status.BLOCKED_CAPABILITY)
    table = tables[0]
    if (
        not isinstance(table, dict) or set(table) != {"columns", "rows"}
        or not isinstance(table["columns"], list) or not table["columns"] or not isinstance(table["rows"], list)
    ):
        raise LabError("Observed IQ typed table has unknown fields or shape", Status.BLOCKED_CAPABILITY)
    names = set()
    for column in table["columns"]:
        if (
            not isinstance(column, dict) or set(column) != {"name", "type"}
            or not isinstance(column["name"], str) or not column["name"] or column["name"] in names
            or type(column["type"]) is not str or column["type"] not in {
                "String", "Int64", "Double", "Object", "DateTime", "Boolean",
            }
        ):
            raise LabError("Observed IQ column names/types are not reviewed primitives",
                           Status.BLOCKED_CAPABILITY)
        names.add(column["name"])
    if any(type(row) is not list or len(row) != len(names) for row in table["rows"]):
        raise LabError("Observed IQ row width differs from the typed columns", Status.BLOCKED_CAPABILITY)
    return table


@dataclass(frozen=True)
class ReviewedIQWitnessOutput:
    """App-owned review of the captured 2025-11-25 JSON-text/count-witness contract.

    No service completeness, revision or coverage fields are invented. A count
    proves only cardinality of the compiler's full filtered business result.
    """

    schema_hash: str
    evidence: str
    artifact_id: UUID
    workspace_id: UUID
    columns: dict[str, Literal["String", "Int64", "Double", "Object", "DateTime", "Boolean"]]
    int64_identity_metrics: tuple[str, ...]
    protocol: Literal["2025-11-25"] = "2025-11-25"
    review_capture_id: str | None = None
    float64_identity_metrics: tuple[str, ...] = ()
    review_capture_ids: tuple[str, ...] = ()
    grouped_int64_columns: tuple[str, ...] = ()

    def check_review(self, actual_hash: str) -> None:
        if (
            actual_hash != self.schema_hash or not self.evidence.strip() or not self.columns
            or self.protocol != "2025-11-25" or not isinstance(self.artifact_id, UUID)
            or not isinstance(self.workspace_id, UUID)
            or any(not name or dtype not in {"String", "Int64", "Double", "Object", "DateTime", "Boolean"}
                   for name, dtype in self.columns.items())
            or {"__kind", "__total_rows", "__serialization_errors"} & self.columns.keys()
            or len(set(self.int64_identity_metrics)) != len(self.int64_identity_metrics)
            or len(set(self.float64_identity_metrics)) != len(self.float64_identity_metrics)
            or set(self.int64_identity_metrics) & set(self.float64_identity_metrics)
            or len(set(self.review_capture_ids)) != len(self.review_capture_ids)
            or len(self.review_capture_ids) > 64
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.review_capture_ids)
            or len(set(self.grouped_int64_columns)) != len(self.grouped_int64_columns)
            or any(self.columns.get(name) != "Int64" for name in self.grouped_int64_columns)
        ):
            raise LabError("Observed IQ contract is unreviewed or has changed", Status.BLOCKED_CAPABILITY)

    def decode_result(
        self, result: CallToolResult, actual_hash: str, *, compiled: CompiledDax | None = None,
    ) -> DecodedRows:
        self.check_review(actual_hash)
        if (
            not isinstance(compiled, CompiledDax) or not compiled.witnessed or compiled.max_rows is None
            or not set(compiled.identity_metrics) <= set(self.int64_identity_metrics)
            or not set(compiled.float64_identity_metrics) <= set(self.float64_identity_metrics)
            or not set(compiled.columns) <= self.columns.keys()
        ):
            raise LabError("Observed completeness requires the trusted witnessed compiler contract",
                           Status.BLOCKED_CAPABILITY)
        table = observed_iq_table(result, self.artifact_id, self.workspace_id)
        expected = (
            [{"name": "__kind", "type": "String"}]
            + [{"name": name, "type": self.columns[name]} for name in compiled.columns]
            + ([{"name": "__serialization_errors", "type": "Int64"}] if compiled.iso_temporal_columns else [])
            + [{"name": "__total_rows", "type": "Int64"}]
        )
        if table["columns"] != expected:
            raise LabError("Observed IQ column order/types differ from the compiled projection",
                           Status.BLOCKED_CAPABILITY)
        for name in compiled.columns:
            dtype, transport = compiled.column_types.get(name), self.columns[name]
            allowed = {
                None: {"String", "Int64"}, "string": {"String"}, "int64": {"Int64"},
                "float64": {"Double"}, "bool": {"Boolean"}, "date": {"DateTime"}, "timestamp": {"DateTime"},
            }.get(dtype, set())
            if dtype == "float64" and compiled.metric_bindings.get(name) in compiled.float64_identity_metrics:
                allowed = allowed | {"Object"}
            if name in compiled.iso_temporal_columns:
                allowed = {"String"} if dtype in {"date", "timestamp"} else set()
            if name in compiled.boolean_string_columns:
                allowed = {"String"} if dtype == "bool" else set()
            if transport not in allowed:
                raise LabError("Observed primitive metadata differs from the trusted source type", Status.BLOCKED_CAPABILITY)
        transport_rows = len(table["rows"])
        if transport_rows > compiled.max_rows:
            raise LabError("Observed IQ response exceeded the transport cap including witness", Status.PARTIAL)
        witnesses = [row for row in table["rows"] if row[0] == "count"]
        if len(witnesses) != 1:
            raise LabError("Missing or duplicate count witness; completeness unknown", Status.PARTIAL)
        witness = witnesses[0]
        if table["rows"][0] != witness or any(value is not None for value in witness[1:1 + len(compiled.columns)]):
            raise LabError("Count witness order/business fields differ from the compiler", Status.BLOCKED_CAPABILITY)
        if compiled.iso_temporal_columns and observed_int64(witness[-2]) != 0:
            raise LabError("Source temporal precision exceeds the reviewed lossless ISO serialization", Status.BLOCKED_CAPABILITY)
        total = observed_int64(witness[-1])
        if total < 0:
            raise LabError("Count witness must be nonnegative", Status.BLOCKED_CAPABILITY)
        rows, grains = [], []
        seen_grains: set[tuple] = set()
        for row in table["rows"][1:]:
            if row[0] != "data" or observed_int64(row[-1]) != total:
                raise LabError("Business row kind/count metadata disagree with the witness", Status.BLOCKED_CAPABILITY)
            if compiled.iso_temporal_columns and observed_int64(row[-2]) != 0:
                raise LabError("Source temporal serialization guard disagrees with its witness", Status.BLOCKED_CAPABILITY)
            converted = {}
            for name, value in zip(compiled.columns, row[1:1 + len(compiled.columns)], strict=True):
                if value is not None:
                    dtype = compiled.column_types.get(name)
                    transport = self.columns[name]
                    if transport == "Int64" and dtype in (None, "int64"):
                        value = _reviewed_grouped_int64(value) if name in self.grouped_int64_columns else observed_int64(value)
                    elif transport == "String" and dtype in (None, "string") and type(value) is str:
                        pass
                    elif transport in {"Double", "Object"} and dtype == "float64":
                        try:
                            if type(value) not in (int, float) or not math.isfinite(value):
                                raise ValueError("Non-numeric or nonfinite source scalar")
                            value = float(value)
                        except (ValueError, OverflowError) as error:
                            raise LabError("Observed numeric scalar is formatted, nonfinite or unreviewed",
                                           Status.BLOCKED_CAPABILITY) from error
                    elif transport == "Boolean" and dtype == "bool" and type(value) is bool:
                        pass
                    elif name in compiled.boolean_string_columns and type(value) is str and value in {"true", "false"}:
                        value = value == "true"
                    elif (transport == "DateTime" or name in compiled.iso_temporal_columns) and (
                        dtype in {"date", "timestamp"} and type(value) is str
                    ):
                        try:
                            if dtype == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                                value = date.fromisoformat(value).isoformat()
                            else:
                                if not re.fullmatch(
                                    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
                                    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?", value,
                                ):
                                    raise ValueError("Unreviewed date/time representation")
                                stamp = datetime.fromisoformat(value)
                                if stamp.utcoffset() not in (None, timedelta(0)):
                                    raise ValueError("Non-UTC source timestamp")
                                if dtype == "date":
                                    if stamp.time() != time(0):
                                        raise ValueError("Date output contains time")
                                    value = stamp.date().isoformat()
                                else:
                                    # The reviewed source contract declares UTC, never the host's local timezone.
                                    value = stamp.replace(tzinfo=timezone.utc).isoformat()
                        except ValueError as error:
                            raise LabError("Observed date/time does not preserve its reviewed source type",
                                           Status.BLOCKED_CAPABILITY) from error
                    else:
                        raise LabError("Observed primitive cell differs from the trusted compiler type", Status.BLOCKED_CAPABILITY)
                converted[name] = value
            grain = tuple(converted[name] for name in compiled.grain)
            if any(value is None for value in grain) or grain in seen_grains:
                raise LabError("Observed result violates the unique nonnull expected grain", Status.BLOCKED_CAPABILITY)
            seen_grains.add(grain)
            grains.append(grain)
            rows.append(converted)
        if grains != sorted(grains) or len(rows) > total:
            raise LabError("Observed result order/cardinality contradicts the compiler witness",
                           Status.BLOCKED_CAPABILITY)
        status = Status.COMPLETE if len(rows) == total else Status.PARTIAL
        return DecodedRows(rows, status, total, self.evidence, transport_rows, 1)

    def decode(self, result: CallToolResult, actual_hash: str, *, compiled: CompiledDax | None = None) -> list[dict]:
        decoded = self.decode_result(result, actual_hash, compiled=compiled)
        if decoded.status != Status.COMPLETE:
            raise LabError("Full witnessed query result was not returned", Status.PARTIAL)
        return decoded.rows


@dataclass(frozen=True)
class ReviewedOutput:
    """An application-owned mapping, created ONLY after inspecting an actual output receipt."""

    schema_hash: str
    rows_pointer: str
    complete_pointer: str
    total_rows_pointer: str
    errors_pointer: str
    output_schema: dict
    evidence: str

    def check_review(self, actual_hash: str) -> None:
        if actual_hash != self.schema_hash or not self.evidence:
            raise LabError("Output/completeness mapping has not been reviewed or has changed", Status.BLOCKED_CAPABILITY)
        if self.output_schema.get("additionalProperties") is not False:
            raise LabError("Reviewed output envelope must reject unknown error/truncation fields")
        Draft202012Validator.check_schema(self.output_schema)

    def decode_result(self, result: CallToolResult, actual_hash: str) -> DecodedRows:
        self.check_review(actual_hash)
        data = result.structuredContent
        if data is None or result.isError:
            raise LabError("No reviewed structured data available", Status.BLOCKED_CAPABILITY)
        if not _schema_valid(self.output_schema, data):
            raise LabError("Output violates reviewed envelope", Status.BLOCKED_CAPABILITY)
        if pointer(data, self.errors_pointer):
            raise LabError("Per-query errors are not an empty dataset")
        rows = pointer(data, self.rows_pointer)
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise LabError("Output rows have unknown shape", Status.BLOCKED_CAPABILITY)
        total = pointer(data, self.total_rows_pointer)
        complete = pointer(data, self.complete_pointer)
        if type(complete) is not bool or type(total) is not int or total < 0:
            raise LabError("Reviewed completeness fields have invalid types", Status.BLOCKED_CAPABILITY)
        status = Status.COMPLETE if complete and total == len(rows) else Status.PARTIAL
        return DecodedRows(rows, status, total, self.evidence)

    def decode(self, result: CallToolResult, actual_hash: str) -> list[dict]:
        decoded = self.decode_result(result, actual_hash)
        if decoded.status != Status.COMPLETE:
            raise LabError("Full-result completeness not proven", Status.PARTIAL)
        return decoded.rows


@dataclass(frozen=True)
class ReviewedCSVOutput:
    """Selectors refer to the complete call result, never a guessed inline preview.

    Every selector, null marker and completeness field needs captured evidence.
    No production profile is shipped for the as-yet unobserved GA CSV response.
    """

    schema_hash: str
    csv_pointer: str
    complete_pointer: str
    total_rows_pointer: str
    errors_pointer: str
    output_schema: dict
    evidence: str
    columns: dict[str, str]
    null_marker: str

    def check_review(self, actual_hash: str) -> None:
        if actual_hash != self.schema_hash or not self.evidence or self.null_marker is None:
            raise LabError("CSV output/completeness mapping has not been reviewed", Status.BLOCKED_CAPABILITY)
        if self.output_schema.get("additionalProperties") is not False:
            raise LabError("Reviewed CSV envelope must reject unknown fields", Status.BLOCKED_CAPABILITY)
        Draft202012Validator.check_schema(self.output_schema)

    def decode_result(self, result: CallToolResult, actual_hash: str) -> DecodedRows:
        self.check_review(actual_hash)
        data = result.model_dump(mode="json")
        if result.isError or not _schema_valid(self.output_schema, data):
            raise LabError("CSV result violates reviewed envelope", Status.BLOCKED_CAPABILITY)
        if pointer(data, self.errors_pointer):
            raise LabError("CSV per-query errors are not an empty dataset")
        text = pointer(data, self.csv_pointer)
        total, complete = pointer(data, self.total_rows_pointer), pointer(data, self.complete_pointer)
        if not isinstance(text, str) or type(total) is not int or total < 0 or type(complete) is not bool:
            raise LabError("CSV payload/completeness fields have invalid types", Status.BLOCKED_CAPABILITY)
        # The existing helper requires proven full-resource completeness; partial
        # CSV is blocked rather than inventing a count or reading an inline preview.
        rows = embedded_csv(
            text, self.columns, null_marker=self.null_marker, expected_rows=total, complete=complete,
        )
        return DecodedRows(rows, Status.COMPLETE, total, self.evidence)

    def decode(self, result: CallToolResult, actual_hash: str) -> list[dict]:
        return self.decode_result(result, actual_hash).rows


class IQAdapter:
    def __init__(
        self, client: DiscoveredClient,
        output: ReviewedOutput | ReviewedCSVOutput | ReviewedIQWitnessOutput | None = None,
    ):
        if client.endpoint.kind not in {"iq", "synthetic_test"}:
            raise LabError("Ontology endpoint cannot serve as GA IQ")
        self.client, self.output = client, output

    async def execute(
        self, artifact_id: str, dax: str, max_rows: int, *, compiled: CompiledDax | None = None,
    ) -> tuple[list[dict], CallToolResult]:
        decoded, result = await self.execute_result(artifact_id, dax, max_rows, compiled=compiled)
        if decoded.status != Status.COMPLETE:
            raise LabError("Full-result completeness not proven", Status.PARTIAL)
        return decoded.rows, result

    def require_review(self) -> ReviewedOutput | ReviewedCSVOutput | ReviewedIQWitnessOutput:
        if self.output is None:
            raise LabError("Output/completeness mapping has not been reviewed", Status.BLOCKED_CAPABILITY)
        self.output.check_review(self.client.schema_hash("ExecuteQuery"))
        if isinstance(self.output, ReviewedIQWitnessOutput) and self.client.protocol != self.output.protocol:
            raise LabError("Observed IQ protocol changed; re-review required", Status.BLOCKED_CAPABILITY)
        return self.output

    async def execute_result(
        self, artifact_id: str, dax: str, max_rows: int, *,
        capture: ResultCapture | None = None, synthetic_capture: bool = False,
        compiled: CompiledDax | None = None,
    ) -> tuple[DecodedRows, CallToolResult]:
        output = self.require_review()
        if isinstance(output, ReviewedIQWitnessOutput) and (
            artifact_id != str(output.artifact_id) or not isinstance(compiled, CompiledDax)
            or not compiled.witnessed or compiled.text != dax or compiled.max_rows != max_rows
        ):
            raise LabError("Observed IQ request is not the bound witnessed compiler request", Status.BLOCKED_CAPABILITY)
        actual_hash = self.client.schema_hash("ExecuteQuery")
        result = await self.capture(
            artifact_id, dax, max_rows, capture=capture, synthetic_capture=synthetic_capture,
        )
        decoded = (
            output.decode_result(result, actual_hash, compiled=compiled)
            if isinstance(output, ReviewedIQWitnessOutput) else output.decode_result(result, actual_hash)
        )
        return decoded, result

    async def capture(
        self, artifact_id: str, dax: str, max_rows: int, *,
        capture: ResultCapture | None = None, synthetic_capture: bool = False,
    ) -> CallToolResult:
        """Raw shape inspection only. This route never declares a result complete."""
        # These input names were actually discovered through the host on 2026-09-29.
        # Every invocation is still validated against this connection's current tools/list.
        arguments = {"artifactId": artifact_id, "daxQueries": [dax], "maxRows": max_rows}
        if capture is None:
            return await self.client.call("ExecuteQuery", arguments)
        return await self.client.call(
            "ExecuteQuery", arguments, capture=capture, synthetic_capture=synthetic_capture,
        )


class OntologyAdapter:
    def __init__(self, client: DiscoveredClient):
        if client.endpoint.kind != "ontology":
            raise LabError("Native ontology requires its separate direct endpoint")
        self.client = client

    async def read(self, tool: str, arguments: dict) -> CallToolResult:
        # No guessed entity/metric/context response normalization.
        return await self.client.call(tool, arguments)
