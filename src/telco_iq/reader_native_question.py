"""One approved native answer request; capture unknown output without inventing an answer schema."""

from __future__ import annotations

import asyncio
import hashlib
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

EFFECTS = "READ_ONLY_NATIVE_ONTOLOGY_QUESTION"
TOOL = "ask_ontology"


def producer_code_hash() -> str:
    root = Path(__file__).parent
    return digest({
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("reader_native_question.py", "private_evidence.py", "mcp_client.py", "auth.py", "live_contracts.py")
    })


class ReaderNativeQuestionConfig(Contract):
    scope: Scope
    ontology_workspace_id: UUID
    ontology_item_id: UUID
    native_snapshot_hash: Sha256
    sources: dict[str, SourceIdentity] = Field(min_length=1)
    question: str = Field(min_length=3, max_length=10000)
    reviewed_tool_hash: Sha256
    producer_code_hash: Sha256
    maximum_native_requests: Literal[1] = 1
    budgets: Budgets
    private_evidence: PrivateEvidenceConfig
    approval: Approval
    effects: Literal["READ_ONLY_NATIVE_ONTOLOGY_QUESTION"]

    @model_validator(mode="after")
    def exact_target(self):
        ReaderBinding(
            scope=self.scope, ontology_workspace_id=self.ontology_workspace_id,
            ontology_item_id=self.ontology_item_id, native_snapshot_hash=self.native_snapshot_hash,
            sources=self.sources,
        )
        if not self.question.strip():
            raise ValueError("A reviewed native business question is required")
        return self

    @property
    def target_digest(self) -> str:
        return digest({
            "scope": self.scope.model_dump(mode="json"),
            "workspace": str(self.ontology_workspace_id), "ontology": str(self.ontology_item_id),
            "sources": {alias: value.model_dump(mode="json") for alias, value in self.sources.items()},
        })

    @property
    def artifact_digest(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"approval"}))


async def capture_reader_native_question(reader, config: ReaderNativeQuestionConfig, root: Path, session) -> dict:
    scope = session.scope().model_copy(update={"models": sorted(config.sources)})

    def current():
        config.approval.authorize(config.artifact_digest, config.target_digest, "reader-native-question")
        if config.producer_code_hash != producer_code_hash():
            raise LabError("Reviewed native question capture code changed", Status.BLOCKED_CAPABILITY)
        if (
            scope != config.scope or not reader.active(session)
            or session.scope().model_copy(update={"models": sorted(config.sources)}) != scope
            or any(reader.config.model_ids.get(alias) != source.dataset_id for alias, source in config.sources.items())
        ):
            raise LabError("Native question belongs to another current reader/source scope", Status.BLOCKED_AUTH)

    current()
    binding = ReaderBinding(
        scope=scope, ontology_workspace_id=config.ontology_workspace_id, ontology_item_id=config.ontology_item_id,
        native_snapshot_hash=config.native_snapshot_hash, sources=config.sources,
    )
    token = await asyncio.to_thread(reader.token, session, resource="ontology")
    current()
    sink = PrivateEvidenceSink(config.private_evidence, root, forbidden_values=(token,))
    sink.ready()
    sink.directory.mkdir(parents=True, exist_ok=True)
    intent = sink.directory / ("native-question-" + config.artifact_digest + ".intent.json")
    try:
        with intent.open("x", encoding="utf-8") as handle:
            handle.write(config.model_dump_json(exclude={"approval", "question"}) + "\n")
    except FileExistsError:
        raise LabError("The one native question invocation is spent; no automatic retry", Status.BLOCKED_CAPABILITY) from None
    endpoint = Endpoint(
        f"https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/{config.ontology_workspace_id}"
        f"/items/{config.ontology_item_id}/ontologyEndpoint", "ontology",
    )
    budget = SourceCallBudget(config.budgets)
    retained = []
    failed = False
    try:
        async with asyncio.timeout(config.budgets.timeout_seconds):
            budget.reserve()
            async with DiscoveredClient.connect(
                endpoint, auth=DelegatedBearer(token), reader_scope=scope,
                reviewed_hashes={TOOL: config.reviewed_tool_hash}, timeout=budget.remaining_seconds(),
                single_attempt=True,
            ) as client:
                current()
                if client.endpoint != endpoint or client.reader_scope != scope:
                    raise LabError("Native answer client target changed", Status.BLOCKED_AUTH)
                if client.schema_hash(TOOL) != config.reviewed_tool_hash:
                    raise LabError("Native question tool schema changed", Status.BLOCKED_CAPABILITY)
                budget.response(client.inventory())
                budget.reserve()

                def capture(tool, request, response, response_kind):
                    current()
                    reference = sink.capture(
                        tool=tool, source_alias="ontology", request=request, response=response,
                        context={
                            "kind": EFFECTS, "binding_hash": binding.binding_hash,
                            "tool_schema_hash": config.reviewed_tool_hash,
                            "producer_code_hash": config.producer_code_hash,
                            "toolset_variant": client.endpoint.toolset_variant,
                        },
                        private_identifiers=private_identifiers(binding), synthetic=False,
                        response_kind=response_kind, native_answer=True,
                    )
                    retained.append(reference.capture_id)
                    budget.response(response)
                    budget.returned(1)
                try:
                    await client.call(
                        TOOL, {"request": config.question}, capture=capture, timeout=budget.remaining_seconds(),
                    )
                except MCPToolError:
                    failed = True
                current()
                if not retained:
                    raise LabError("Native answer original was not retained", Status.BLOCKED_CAPABILITY)
                budget.reserve()
                await client.discover()
                if client.schema_hash(TOOL) != config.reviewed_tool_hash:
                    raise LabError("Native question tool schema changed during capture", Status.BLOCKED_CAPABILITY)
    except TimeoutError:
        raise LabError("Native question exceeded its approved time budget; no retry", Status.TIMED_OUT) from None
    except (httpx.HTTPError, McpError, ExceptionGroup) as error:
        status = mcp_capture_failure_status(error)
        raise LabError("Native question protocol/transport failed; no retry", status) from None
    current()
    return {
        "kind": "NATIVE_ONTOLOGY_ANSWER_CAPTURE_ONLY", "ready": False,
        "status": "NATIVE_TOOL_ERROR_CAPTURED" if failed else "NATIVE_ANSWER_CAPTURED_NEEDS_REVIEW",
        "observed_at": datetime.now(timezone.utc).isoformat(), "capture_hashes": retained,
        "native_question_requests": 1, "application_model_requests": 0,
        "claim": "Native service output only; not a controlled same-model comparison, reader grant or complete source-state proof.",
        "generated_code_executed": False,
    }
