"""One-shot approved reader query calibration, distinct from metadata and answer execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from mcp.shared.exceptions import McpError
from pydantic import Field, model_validator

from telco_iq.approval import Approval, private_config_path
from telco_iq.auth import DelegatedBearer
from telco_iq.catalog import Catalog, digest
from telco_iq.contracts import Budgets, Contract, QueryNode, Scope, Sha256, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import ReaderBinding, SourceCallBudget, SourceIdentity
from telco_iq.mcp_client import (
    DiscoveredClient,
    Endpoint,
    MCPToolError,
    mcp_capture_failure_status,
    observed_iq_transport_rows,
)
from telco_iq.mcp_provider import _validate_dax_filters, compile_coverage_dax
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink, private_identifiers

EFFECTS = "CURRENT_READER_WITNESSED_QUERY_CALIBRATION_ONLY"


def producer_code_hash():
    root = Path(__file__).parent
    return digest({
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "reader_query_capture.py", "source_observation.py", "api.py", "private_evidence.py",
            "mcp_client.py", "mcp_provider.py", "catalog.py", "contracts.py", "live_contracts.py", "auth.py",
        )
    })


class ReaderQueryCalibrationConfig(Contract):
    scope: Scope
    ontology_workspace_id: UUID
    ontology_item_id: UUID
    native_snapshot_hash: Sha256
    sources: dict[str, SourceIdentity] = Field(min_length=1)
    catalog_hash: Sha256
    catalog_file: Path | None = Field(default=None, exclude_if=lambda value: value is None)
    queries: dict[str, QueryNode]
    additional_queries: dict[str, list[QueryNode]] = Field(default_factory=dict, exclude_if=lambda value: not value)
    coverage_queries: dict[str, list[QueryNode]] = Field(default_factory=dict, exclude_if=lambda value: not value)
    coverage_diagnostics: dict[str, dict | None] = Field(default_factory=dict, exclude_if=lambda value: not value)
    source_schema_hashes: dict[str, Sha256] = Field(default_factory=dict, exclude_if=lambda value: not value)
    aliases: dict[str, dict[str, str]]
    int64_identity_metrics: dict[str, tuple[str, ...]]
    float64_identity_metrics: dict[str, tuple[str, ...]] = Field(default_factory=dict, exclude_if=lambda value: not value)
    query_tool_hash: Sha256
    producer_code_hash: Sha256
    budgets: Budgets
    private_evidence: PrivateEvidenceConfig
    approval: Approval
    effects: Literal["CURRENT_READER_WITNESSED_QUERY_CALIBRATION_ONLY"]

    @model_validator(mode="after")
    def fixed_queries(self):
        if any(set(values) != set(self.sources) for values in (
            self.queries, self.aliases, self.int64_identity_metrics,
        )):
            raise ValueError("Each exact source requires one explicitly reviewed compiler query")
        for alias, node in self.queries.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", alias) or alias == "ontology" or (
                node.model != alias or node.depends_on or node.cohort_filters or not 2 <= node.max_rows <= 1000
            ):
                raise ValueError("Calibration requires independent source-bound queries within the MCP row limit")
        if not set(self.float64_identity_metrics) <= set(self.sources):
            raise ValueError("Float64 calibration identities exceed the exact source scope")
        if not (set(self.additional_queries) | set(self.coverage_queries)) <= set(self.sources):
            raise ValueError("Additional calibration queries exceed the exact source scope")
        if not set(self.coverage_queries) <= set(self.source_schema_hashes) <= set(self.sources):
            raise ValueError("Coverage calibration requires exact reviewed source-schema fingerprints")
        coverage_ids = {node.id for nodes in self.coverage_queries.values() for node in nodes}
        if set(self.coverage_diagnostics) != coverage_ids:
            raise ValueError("Each coverage query needs its explicit reviewed diagnostic or absent-diagnostic declaration")
        if any(not nodes for groups in (self.additional_queries, self.coverage_queries) for nodes in groups.values()):
            raise ValueError("Explicit additional/coverage calibration lists must be nonempty")
        names = {node.id for node in self.queries.values()}
        if len(names) != len(self.queries):
            raise ValueError("Calibration query node identities must be unique across sources")
        for alias in set(self.additional_queries) | set(self.coverage_queries):
            queries = [*self.additional_queries.get(alias, []), *self.coverage_queries.get(alias, [])]
            if not queries or len(queries) > 32:
                raise ValueError("Additional calibration query list must be finite and nonempty")
            for node in queries:
                if node.model != alias or node.depends_on or node.cohort_filters or (
                    not 2 <= node.max_rows <= 1000 or node.id in names
                ):
                    raise ValueError("Additional calibration query source/node/bounds are not independent")
                names.add(node.id)
        if any(node.max_rows != 2 for nodes in self.coverage_queries.values() for node in nodes):
            raise ValueError("Coverage calibration requires exactly two transport rows including the count witness")
        self.binding()
        return self

    def binding(self):
        return ReaderBinding(
            scope=self.scope, ontology_workspace_id=self.ontology_workspace_id, ontology_item_id=self.ontology_item_id,
            native_snapshot_hash=self.native_snapshot_hash, sources=self.sources,
        )

    @property
    def artifact_digest(self):
        return digest(self.model_dump(mode="json", exclude={"approval"}))

    @property
    def target_digest(self):
        return digest(self.binding().model_dump(mode="json"))


async def capture_reader_query_calibration(reader, config: ReaderQueryCalibrationConfig, root: Path, session):
    scope = session.scope().model_copy(update={"models": sorted(config.sources)})

    def current():
        config.approval.authorize(config.artifact_digest, config.target_digest, "reader-source-query-calibration")
        if config.producer_code_hash != producer_code_hash():
            raise LabError("Reviewed reader query calibration producer changed", Status.BLOCKED_CAPABILITY)
        if config.scope != scope or not reader.active(session) or session.scope().model_copy(
            update={"models": sorted(config.sources)},
        ) != scope or any(
            reader.config.model_ids.get(alias) != source.dataset_id for alias, source in config.sources.items()
        ):
            raise LabError("Query calibration belongs to another current source reader", Status.BLOCKED_AUTH)

    current()
    catalog = Catalog(
        private_config_path(config.catalog_file, root) if config.catalog_file is not None
        else root / "data" / "contracts" / "catalog.json"
    )
    if catalog.revision != config.catalog_hash:
        raise LabError("Reviewed query calibration catalog changed", Status.BLOCKED_CAPABILITY)
    compiled = {}
    coverage_ids = {node.id for nodes in config.coverage_queries.values() for node in nodes}
    multiple_queries = bool(config.additional_queries or config.coverage_queries)
    source_queries = {
        alias: [config.queries[alias], *config.additional_queries.get(alias, []), *config.coverage_queries.get(alias, [])]
        for alias in config.sources
    }
    for alias, queries in source_queries.items():
        for node in queries:
            _validate_dax_filters(catalog, node)
            if node.id in coverage_ids:
                compiled[node.id] = compile_coverage_dax(
                    catalog, node, catalog.tables[node.table]["columns"], config.coverage_diagnostics[node.id],
                )
                continue
            names = node.group_by + node.metrics if node.metrics else node.columns
            if not set(names) <= config.aliases[alias].keys():
                raise LabError("Calibration aliases do not cover the exact query projection", Status.BLOCKED_CAPABILITY)
            compiled[node.id] = catalog.compile_dax(
                node, max_rows=node.max_rows, witnessed=True,
                aliases=({name: config.aliases[alias][name] for name in names}
                         if multiple_queries else config.aliases[alias]),
                int64_identity_metrics=config.int64_identity_metrics[alias],
                float64_identity_metrics=config.float64_identity_metrics.get(alias, ()),
            )
    binding = config.binding()
    store = PrivateEvidenceSink(config.private_evidence, root)
    store.ready()
    store.directory.mkdir(parents=True, exist_ok=True)
    try:
        with (store.directory / "reader-query-calibration.intent.json").open("x", encoding="utf-8") as handle:
            json.dump({
                "artifact_digest": config.artifact_digest, "target_digest": config.target_digest,
                "binding_hash": binding.binding_hash, "reserved_at": datetime.now(timezone.utc).isoformat(),
                "automatic_retry": False,
            }, handle, sort_keys=True)
    except FileExistsError:
        raise LabError("Reader query calibration intent is already spent; no automatic retry",
                       Status.BLOCKED_CAPABILITY) from None

    captures, outcomes = [], []
    budget = SourceCallBudget(config.budgets)
    endpoint = Endpoint("https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq", "iq")
    try:
        async with asyncio.timeout(config.budgets.timeout_seconds):
            current()
            token = await asyncio.to_thread(reader.token, session, resource="iq")
            current()
            sink = PrivateEvidenceSink(config.private_evidence, root, forbidden_values=(token,))
            sink.ready()
            budget.reserve()
            try:
                async with DiscoveredClient.connect(
                    endpoint, auth=DelegatedBearer(token), reviewed_hashes={"ExecuteQuery": config.query_tool_hash},
                    reader_scope=scope, timeout=budget.remaining_seconds(), single_attempt=True,
                ) as client:
                    def current_client():
                        current()
                        if client.reader_scope != scope or client.endpoint != endpoint:
                            raise LabError("Reader query calibration client scope changed", Status.BLOCKED_AUTH)
                        if client.schema_hash("ExecuteQuery") != config.query_tool_hash:
                            raise LabError("Reviewed calibration query tool schema changed", Status.BLOCKED_CAPABILITY)

                    current_client()
                    inventory = client.inventory()
                    reference = sink.capture(
                        tool="MCPInventory", source_alias="iq", request={"operation": "tools/list"},
                        response=inventory, context={
                            "kind": "CURRENT_READER_MCP_INVENTORY_PROJECTION", "binding_hash": binding.binding_hash,
                            "calibration_effect": EFFECTS, "code_hash": config.producer_code_hash,
                        },
                        private_identifiers=private_identifiers(binding), synthetic=False,
                        response_kind="MCP_INVENTORY_PROJECTION", metadata=True,
                    )
                    captures.append(reference.capture_id)
                    budget.response(inventory)
                    calls = [(alias, node) for alias in sorted(config.sources) for node in source_queries[alias]]
                    for alias, node in calls:
                        current_client()
                        arguments = {
                            "artifactId": str(config.sources[alias].dataset_id),
                            "daxQueries": [compiled[node.id].text], "maxRows": node.max_rows,
                        }
                        retained = []

                        def capture(tool, request, response, response_kind):
                            current_client()
                            reference = sink.capture(
                                tool=tool, source_alias=alias, request=request, response=response,
                                context={
                                    "kind": "SOURCE_COVERAGE_QUERY" if node.id in coverage_ids else "SOURCE_BUSINESS_QUERY",
                                    "calibration_effect": EFFECTS,
                                    "binding_hash": binding.binding_hash,
                                    "source": config.sources[alias].model_dump(mode="json"),
                                    "query_node": node.model_dump(mode="json"), "schema_hash": config.query_tool_hash,
                                    "code_hash": config.producer_code_hash,
                                    **({"source_schema_hash": config.source_schema_hashes[alias]}
                                       if node.id in coverage_ids else {}),
                                },
                                private_identifiers=private_identifiers(binding), synthetic=False,
                                response_kind=response_kind,
                            )
                            captures.append(reference.capture_id)
                            retained.append(reference.capture_id)
                            budget.response(response)
                            if response_kind == "MCP_TOOL_RESULT" and not response.get("isError", False):
                                budget.returned(observed_iq_transport_rows(response))

                        budget.reserve()
                        try:
                            await client.call("ExecuteQuery", arguments, capture=capture)
                        except MCPToolError:
                            current_client()
                            if not retained:
                                raise LabError("Failed calibration query original was not retained",
                                               Status.BLOCKED_CAPABILITY) from None
                            outcomes.append({
                                "source_alias": alias, "status": "TOOL_ERROR", "capture_hashes": retained,
                                **({"query_node_id": node.id} if multiple_queries else {}),
                            })
                            continue
                        current_client()
                        if not retained:
                            raise LabError("Original calibration query result was not retained",
                                           Status.BLOCKED_CAPABILITY)
                        outcomes.append({
                            "source_alias": alias, "status": "CAPTURED_NEEDS_REVIEW", "capture_hashes": retained,
                            **({"query_node_id": node.id} if multiple_queries else {}),
                        })
                    budget.reserve()
                    await client.discover()
                    current_client()
            except (LabError, httpx.HTTPError, McpError, ExceptionGroup) as error:
                current()
                outcomes.append({
                    "resource": "iq", "status": mcp_capture_failure_status(error),
                    "detail": "Approved query calibration was blocked; retained originals do not grant access.",
                })
    except TimeoutError:
        raise LabError("Reader query calibration exceeded its approved time budget", Status.TIMED_OUT) from None
    current()
    complete = len(outcomes) == sum(len(nodes) for nodes in source_queries.values()) and all(
        outcome.get("status") == "CAPTURED_NEEDS_REVIEW" for outcome in outcomes
    )
    return {
        "kind": "SOURCE_QUERY_CALIBRATION_CAPTURE_ONLY", "capture_purpose": EFFECTS, "ready": False,
        "status": "CALIBRATION_CAPTURED_NOT_READER_GRANT" if complete else "PARTIAL_QUERY_CALIBRATION_CAPTURE",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "capture_hashes": captures, "outcomes": outcomes,
        "claim": "Raw current-reader compiler calibration only; no decoded business answer or reader/source-state grant.",
    }
