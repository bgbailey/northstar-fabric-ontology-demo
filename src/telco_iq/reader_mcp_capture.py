"""Fixed-target current-reader MCP metadata bootstrap, never a serialized grant."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from mcp.shared.exceptions import McpError
from pydantic import Field, model_validator

from telco_iq.approval import Approval
from telco_iq.auth import DelegatedBearer
from telco_iq.catalog import digest
from telco_iq.contracts import Budgets, Contract, Scope, Sha256, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import ReaderBinding, SourceCallBudget, SourceIdentity
from telco_iq.mcp_client import DiscoveredClient, Endpoint, MCPToolError, mcp_capture_failure_status
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink, private_identifiers

NATIVE = frozenset({"list_ontology_entities", "list_ontology_rules"})
EFFECTS = "NATIVE_ENTITY_RULE_SOURCE_SCHEMA_METADATA_ONLY"


def producer_code_hash():
    root = Path(__file__).parent
    return digest({
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "reader_mcp_capture.py", "source_observation.py", "private_evidence.py",
            "mcp_client.py", "live_contracts.py", "auth.py",
        )
    })


class ReaderMCPMetadataCaptureConfig(Contract):
    scope: Scope
    ontology_workspace_id: UUID
    ontology_item_id: UUID
    native_snapshot_hash: Sha256
    sources: dict[str, SourceIdentity] = Field(min_length=1)
    native_hashes: dict[str, Sha256]
    native_arguments: dict[str, dict]
    schema_tool_hash: Sha256
    producer_code_hash: Sha256
    schema_arguments: dict[str, dict]
    budgets: Budgets
    private_evidence: PrivateEvidenceConfig
    approval: Approval
    effects: Literal["NATIVE_ENTITY_RULE_SOURCE_SCHEMA_METADATA_ONLY"]

    @model_validator(mode="after")
    def fixed_reads(self):
        if set(self.native_hashes) != NATIVE or set(self.native_arguments) != NATIVE:
            raise ValueError("Only both exact reviewed native metadata tools may be captured")
        if set(self.schema_arguments) != set(self.sources):
            raise ValueError("Each exact source needs reviewed schema arguments")
        if any(alias == "ontology" or not re.fullmatch(r"[a-z][a-z0-9_]*", alias) for alias in self.sources):
            raise ValueError("Reader source aliases must be unambiguous reviewed names")
        for alias, source in self.sources.items():
            if set(self.schema_arguments[alias]) != {"artifactId"} or (
                self.schema_arguments[alias].get("artifactId") != str(source.dataset_id)
            ):
                raise ValueError("Schema arguments must identify the exact bound source")
        ReaderBinding(
            scope=self.scope, ontology_workspace_id=self.ontology_workspace_id, ontology_item_id=self.ontology_item_id,
            native_snapshot_hash=self.native_snapshot_hash, sources=self.sources,
        )
        return self

    @property
    def artifact_digest(self):
        return digest(self.model_dump(mode="json", exclude={"approval"}))

    @property
    def target_digest(self):
        return digest({
            "scope": self.scope.model_dump(mode="json"), "ontology": str(self.ontology_item_id),
            "workspace": str(self.ontology_workspace_id),
            "sources": {alias: source.model_dump(mode="json") for alias, source in self.sources.items()},
        })


async def capture_reader_mcp_metadata(reader, config: ReaderMCPMetadataCaptureConfig, root, session):
    try:
        async with asyncio.timeout(config.budgets.timeout_seconds):
            return await _capture_reader_mcp_metadata(reader, config, root, session)
    except TimeoutError:
        raise LabError("Reader MCP metadata capture exceeded its approved time budget", Status.TIMED_OUT) from None


async def _capture_reader_mcp_metadata(reader, config: ReaderMCPMetadataCaptureConfig, root, session):
    scope = session.scope().model_copy(update={"models": sorted(config.sources)})

    def current():
        config.approval.authorize(config.artifact_digest, config.target_digest, "reader-mcp-metadata-capture")
        if config.producer_code_hash != producer_code_hash():
            raise LabError("Reviewed MCP metadata producer code changed", Status.BLOCKED_CAPABILITY)
        if scope != config.scope or not reader.active(session) or session.scope().model_copy(
            update={"models": sorted(config.sources)},
        ) != scope or any(
            reader.config.model_ids.get(alias) != source.dataset_id for alias, source in config.sources.items()
        ):
            raise LabError("Reader MCP capture belongs to another current source scope", Status.BLOCKED_AUTH)

    current()
    tokens = {}
    for resource in ("ontology", "iq"):
        current()
        tokens[resource] = await asyncio.to_thread(reader.token, session, resource=resource)
        current()
    binding = ReaderBinding(
        scope=scope, ontology_workspace_id=config.ontology_workspace_id, ontology_item_id=config.ontology_item_id,
        native_snapshot_hash=config.native_snapshot_hash, sources=config.sources,
    )
    sink = PrivateEvidenceSink(config.private_evidence, root, forbidden_values=tuple(tokens.values()))
    sink.ready()
    budget = SourceCallBudget(config.budgets)
    captures, outcomes, inventories = [], [], []
    endpoints = [
        ("ontology", Endpoint(
            f"https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/{config.ontology_workspace_id}"
            f"/items/{config.ontology_item_id}/ontologyEndpoint", "ontology",
        ), config.native_hashes, [(name, "ontology", config.native_arguments[name]) for name in sorted(NATIVE)]),
        ("iq", Endpoint("https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq", "iq"),
         {"GetSemanticModelSchema": config.schema_tool_hash},
         [("GetSemanticModelSchema", alias, config.schema_arguments[alias]) for alias in sorted(config.sources)]),
    ]
    async with asyncio.timeout(config.budgets.timeout_seconds):
        for resource, endpoint, reviewed, calls in endpoints:
            current()
            budget.reserve()
            try:
                async with DiscoveredClient.connect(
                    endpoint, auth=DelegatedBearer(tokens[resource]), reviewed_hashes=reviewed,
                    reader_scope=scope, timeout=budget.remaining_seconds(),
                ) as client:
                    def current_client():
                        current()
                        if client.reader_scope != scope or client.endpoint != endpoint:
                            raise LabError("MCP metadata client target/scope changed", Status.BLOCKED_AUTH)
                    current_client()
                    inventory = client.inventory()
                    inventory_capture = sink.capture(
                        tool="MCPInventory", source_alias=resource, request={"operation": "tools/list"},
                        response=inventory,
                        context={"kind": "CURRENT_READER_MCP_INVENTORY_PROJECTION",
                                 "binding_hash": binding.binding_hash, "code_hash": config.producer_code_hash},
                        private_identifiers=private_identifiers(binding), synthetic=False,
                        response_kind="MCP_INVENTORY_PROJECTION", metadata=True,
                    )
                    captures.append(inventory_capture.capture_id)
                    inventories.append({
                        "resource": resource, "protocol": inventory.get("protocol"),
                        "capture_hash": inventory_capture.capture_id,
                    })
                    before = {name: client.schema_hash(name) for name in reviewed}
                    if before != reviewed:
                        raise LabError("Reviewed MCP metadata tool schemas changed", Status.BLOCKED_CAPABILITY)
                    budget.response(inventory)
                    for name, alias, arguments in calls:
                        current_client()
                        budget.reserve()
                        retained = []

                        def capture(tool, request, raw, response_kind):
                            current_client()
                            reference = sink.capture(
                                tool=tool, source_alias=alias, request=request, response=raw,
                                context={
                                    "kind": "CURRENT_READER_NATIVE_SCHEMA_METADATA_ONLY",
                                    "binding_hash": binding.binding_hash,
                                    "code_hash": config.producer_code_hash,
                                    "tool_schema_hash": reviewed[tool],
                                    "source": config.sources[alias].model_dump(mode="json")
                                    if alias in config.sources else {"ontology": str(config.ontology_item_id)},
                                },
                                private_identifiers=private_identifiers(binding), synthetic=False,
                                response_kind=response_kind, metadata=True,
                            )
                            retained.append(reference.capture_id)
                            captures.append(reference.capture_id)
                            budget.response(raw)

                        try:
                            result = await client.call(name, arguments, capture=capture)
                        except MCPToolError:
                            current_client()
                            if not retained:
                                raise LabError(
                                    "Failed original MCP metadata result was not retained", Status.BLOCKED_CAPABILITY,
                                ) from None
                            budget.returned(1)
                            outcomes.append({
                                "resource": resource, "source_alias": alias, "tool": name,
                                "status": "TOOL_ERROR", "capture_hashes": retained,
                            })
                            continue
                        current_client()
                        if not retained:
                            raise LabError("Original MCP metadata result was not retained", Status.BLOCKED_CAPABILITY)
                        budget.returned(1)
                        outcomes.append({
                            "resource": resource, "source_alias": alias, "tool": name,
                            "status": "TOOL_ERROR" if result.isError else "CAPTURED",
                            "capture_hashes": retained,
                        })
                    budget.reserve()
                    await client.discover()
                    current_client()
                    if {name: client.schema_hash(name) for name in reviewed} != reviewed:
                        raise LabError("MCP metadata schema changed during capture", Status.BLOCKED_CAPABILITY)
            except LabError as error:
                current()
                if error.status in {Status.PARTIAL, Status.TIMED_OUT}:
                    raise
                outcomes.append({
                    "resource": resource, "status": error.status,
                    "detail": "Reviewed reader metadata endpoint or result was blocked; no grant or fallback.",
                })
            except (httpx.HTTPError, McpError, ExceptionGroup) as error:
                current()
                outcomes.append({
                    "resource": resource, "status": mcp_capture_failure_status(error),
                    "detail": "MCP metadata transport/protocol/guard failed; retained originals are not a reader grant.",
                })
    current()
    complete = len(outcomes) == 2 + len(config.sources) and all(
        outcome.get("status") == "CAPTURED" for outcome in outcomes
    )
    return {
        "kind": "SOURCE_METADATA_CAPTURE_ONLY", "capture_purpose": EFFECTS, "ready": False,
        "status": "METADATA_CAPTURED_NOT_READER_GRANT" if complete else "PARTIAL_METADATA_CAPTURE",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "capture_hashes": captures, "outcomes": outcomes, "inventories": inventories,
        "claim": "Original current-reader native/schema metadata only; no source-state, Rules execution or business answer.",
    }
