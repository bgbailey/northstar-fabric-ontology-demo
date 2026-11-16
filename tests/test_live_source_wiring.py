"""Synthetic reviewed imports and transport only; never current live proof."""

import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult
from test_mcp_provider import ENVELOPE, QUERY_TOOL, SyntheticSession, setup_provider
from test_native_metric_authority import native_plan
from test_reader_native_context import reader_case

from telco_iq.cache import CacheKey, ScopedCache
from telco_iq.catalog import digest
from telco_iq.contracts import Budgets, Filter, QueryNode, SourcePopulation, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import SourceCallBudget
from telco_iq.live_demo import LiveDemoConfig, ModelTokenIdentity, SourceProofExecutor, SourceProofPlanner
from telco_iq.mcp_client import DiscoveredClient, Endpoint, ReviewedOutput
from telco_iq.mcp_configuration import (
    ColumnConfig,
    CoverageProfileConfig,
    OutputConfig,
    QueryProfileConfig,
    ReviewConfig,
    StateProfileConfig,
    state_probe,
)
from telco_iq.mcp_provider import (
    CaptureReview,
    MCPQueryProvider,
    ReturnedColumn,
    ReviewedCoverageBinding,
    ReviewedSourceBinding,
    bounded_dax,
    coverage_dax,
)
from telco_iq.private_evidence import EvidenceReference
from telco_iq.reader_native_context import acquire_reader_context
from telco_iq.source_proof import PopulationCapture, ReviewedSourceProof


class MemorySink:
    """In-memory retained originals, not a mock claiming private filesystem QA."""

    def __init__(self):
        self.values = {}
        self.config = SimpleNamespace(model_dump=lambda **_: {"synthetic": True})

    def ready(self):
        pass

    def capture(self, **values):
        envelope = deepcopy(values)
        envelope["captured_at"] = datetime.now(timezone.utc).isoformat()
        envelope["lane"] = "SYNTHETIC_TEST" if values["synthetic"] else "LIVE_DELEGATED"
        envelope.setdefault("response_kind", "MCP_TOOL_RESULT")
        key = digest(envelope)
        self.values[key] = envelope
        return EvidenceReference(key, digest(values["request"]), digest(values["response"]), digest({"raw": key}))

    def read(self, key):
        if key not in self.values:
            raise LabError("Private evidence integrity failure", Status.BLOCKED_CAPABILITY)
        return deepcopy(self.values[key])


def document(value, documents):
    original = json.dumps(value, sort_keys=True)
    key = hashlib.sha256(original.encode()).hexdigest()
    documents[key] = original
    return key


async def proof_case(tmp_path):
    snapshot, binding, inputs, _, catalog = reader_case(tmp_path)
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    now = datetime.now(timezone.utc)
    tool_hash = digest(QUERY_TOOL.model_dump(mode="json", exclude_none=True))
    sink, states, captures = MemorySink(), {}, {}
    state_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["data", "model", "complete", "errors"],
        "properties": {
            "data": {"type": "string"}, "model": {"type": "string"},
            "complete": {"type": "boolean"}, "errors": {"type": "array"},
        },
    }
    for alias, identity in binding.sources.items():
        states[alias] = StateProfileConfig(
            identity=identity, schema_hash=tool_hash, output_schema=state_schema,
            review=ReviewConfig(capture_hash="b" * 64, review_reference="synthetic reader fixture",
                                provenance="synthetic_test"),
            dax="EVALUATE 'ReviewedSyntheticState'", data_revision_pointer="/data",
            model_revision_pointer="/model", complete_pointer="/complete", errors_pointer="/errors",
            evidence_meaning="Synthetic source-state capture only",
        )
        response = CallToolResult(content=[], structuredContent={
            "data": "data-" + alias, "model": "model-" + alias, "complete": True, "errors": [],
        })
        captures[alias] = sink.capture(
            tool="ExecuteQuery", source_alias=alias,
            request={"artifactId": str(identity.dataset_id), "daxQueries": [states[alias].dax], "maxRows": 1},
            response=response.model_dump(mode="json"), synthetic=True,
            context={"source": identity.model_dump(mode="json"), "schema_hash": tool_hash,
                     "binding_hash": binding.binding_hash},
        )
    profiles = SimpleNamespace(
        states=states, model_dump=lambda **_: {"states": {alias: state.model_dump(mode="json")
                                                        for alias, state in states.items()}},
    )
    shared = {name: digest(name) for name, table in catalog.tables.items() if table["model"] == "core"}
    declared = {"profile": "synthetic-only", "seed": 7, "identityRevision": "c" * 64, "sharedMapHashes": shared}
    tables = [
        {"id": name, "rows": 1, "content_sha256": digest(name), "file_sha256": digest({"file": name})}
        for name in catalog.tables
    ]
    manifest = {
        "population": declared, "profile": declared["profile"], "seed": 7,
        "generator_sha256": "d" * 64, "tables": tables,
    }
    documents = {}
    manifest_hash = document(manifest, documents)
    source_bodies, model_packets, model_parts = {}, [], []
    for alias in binding.sources:
        definitions = {
            "definition.pbism": '{"version":"4.2"}',
            "definition/database.tmdl": "database\n\tcompatibilityLevel: 1702\n",
            "definition/model.tmdl": "model Synthetic",
            "definition/expressions.tmdl": "source <WORKSPACE_ID> <LAKEHOUSE_ID> <WORKSPACE_NAME>",
            **{f"definition/tables/{catalog.tables[name]['displayName']}.tmdl": "SYNTHETIC TABLE"
               for name in catalog.models[alias]["tables"]},
        }
        body = {"displayName": "Synthetic " + alias, "definition": {
            "format": "TMDL", "parts": [
                {"path": path, "payloadType": "InlineBase64", "payload": base64.b64encode(raw.encode()).decode()}
                for path, raw in definitions.items()
            ],
        }}
        source_bodies[alias] = body
        model_packets.append({"alias": alias, "file": alias + ".json", "sha256": document(body, documents)})
        model_parts += [{"model": alias, "path": path, "sha256": hashlib.sha256(raw.encode()).hexdigest()}
                        for path, raw in definitions.items()]
    notebook_source = {"cells": [{"source": ["SYNTHETIC <WORKSPACE_ID> <LAKEHOUSE_ID> <WORKSPACE_NAME>"]}],
                       "nbformat": 4}
    notebook_template = {"displayName": "Synthetic loader", "definition": {"parts": [{
        "path": "notebook-content.ipynb", "payloadType": "InlineBase64",
        "payload": base64.b64encode((json.dumps(notebook_source, indent=2) + "\n").encode()).decode(),
    }]}}
    notebook_template_hash = document(notebook_template, documents)
    packet = {
        "population": declared, "sourceManifestSha256": manifest_hash, "generatorSha256": "d" * 64,
        "tables": [{"name": table["id"], "rows": 1, "fileSha256": table["file_sha256"]} for table in tables],
        "workspaceName": "<WORKSPACE_NAME>", "lakehouseName": "SyntheticLakehouse",
        "modelPackets": model_packets, "sourceModelParts": model_parts,
        "packetFiles": [{"file": "notebook-template.json", "sha256": notebook_template_hash}],
    }
    packet_hash = document(packet, documents)
    wid = str(next(iter(binding.sources.values())).workspace_id)
    loader, lakehouse, job = [f"00000000-0000-0000-0000-{n:012d}" for n in (40, 41, 42)]
    job_path = f"/v1/workspaces/{wid}/items/{loader}/jobs/instances/{job}"
    source_binding_hash = document({
        "packetSha256": packet_hash, "workspace": {"id": wid, "displayName": "SyntheticWorkspace"},
        "lakehouse": {"id": lakehouse, "displayName": "SyntheticLakehouse"},
    }, documents)
    notebook = deepcopy(notebook_template)
    bound_content = {"cells": [{"source": [f"SYNTHETIC {wid} {lakehouse} SyntheticWorkspace"]}], "nbformat": 4}
    notebook["definition"]["parts"][0]["payload"] = base64.b64encode(
        (json.dumps(bound_content, indent=2) + "\n").encode(),
    ).decode()
    notebook_hash = document(notebook, documents)
    completed_hash = document({
        "proofLevel": "SYNTHETIC_LOCAL", "method": "GET", "path": job_path, "status": 200,
        "startedAt": now.isoformat(),
        "response": {"id": job, "itemId": loader, "jobType": "RunNotebook", "status": "Completed"},
    }, documents)
    load_operation = {
        "name": "synthetic-load", "method": "POST",
        "path": f"/v1/workspaces/{wid}/items/{loader}/jobs/RunNotebook/instances", "bodySha256": digest(None),
    }
    approval = {
        "schemaVersion": "fabric-renewal-v1", "approved": True, "tenantId": binding.scope.tenant,
        "authorUser": "SYNTHETIC_DEPLOYER", "workspaceId": wid,
        "artifacts": [{"sha256": packet_hash}], "window": {"notAfterUtc": (now + timedelta(hours=1)).isoformat()},
        "ownedItems": {"hero-loader": loader, "hero-lakehouse": lakehouse}, "operations": [load_operation],
    }
    approval_hash = document(approval, documents)
    accepted_hash = document({
        "state": "ACCEPTED", "receiptValidated": True, "operation": load_operation["name"],
        "approvalSha256": digest(approval), "method": "POST", "status": 202,
        "path": load_operation["path"], "bodySha256": digest(None), "location": job_path,
    }, documents)
    assertion = {
        "schemaVersion": "hero-source-assertions-v1", "proofLevel": "SYNTHETIC_LOCAL",
        "packetSha256": packet_hash, "populationSha256": digest(declared),
        "observedAtUtc": datetime.now(timezone.utc).isoformat(),
        "loaderId": loader, "lakehouseId": lakehouse, "jobId": job,
        "jobReceiptSha256": completed_hash, "notebookBodySha256": digest(notebook),
        "tables": [{
            "name": table["id"], "rows": 1, "expectedFileSha256": table["file_sha256"],
            "exactDataReadback": True, "keyChecks": "PASSED", "missingExpectedRows": 0, "unexpectedRows": 0,
        } for table in tables],
    }
    assertion_hash = document(assertion, documents)
    receipts, inventories = {}, {}
    for alias, identity in binding.sources.items():
        bound = deepcopy(source_bodies[alias])
        part = next(part for part in bound["definition"]["parts"] if part["path"] == "definition/expressions.tmdl")
        part["payload"] = base64.b64encode(f"source {wid} {lakehouse} SyntheticWorkspace".encode()).decode()
        body_hash = document(bound, documents)
        model_op = {"name": "synthetic-create-" + alias, "method": "POST",
                    "path": f"/v1/workspaces/{wid}/semanticModels", "bodySha256": digest(bound)}
        definition_path = f"/v1/workspaces/{wid}/semanticModels/{identity.dataset_id}/getDefinition?format=TMDL"
        model_auth = {**approval, "operations": [model_op, {
            "name": "synthetic-definition-" + alias, "method": "POST",
            "path": definition_path, "bodySha256": digest({}),
        }]}
        model_auth_hash = document(model_auth, documents)
        item = {"id": str(identity.dataset_id), "type": "SemanticModel", "displayName": bound["displayName"]}
        create_hash = document({
            "state": "ACCEPTED", "receiptValidated": True, "readbackVerified": True,
            "operation": model_op["name"], "approvalSha256": digest(model_auth), "status": 201,
            "method": "POST", "path": model_op["path"], "bodySha256": digest(bound),
            "createdId": str(identity.dataset_id), "createdItem": item, "response": item,
        }, documents)
        item_hash = document({
            "method": "GET", "path": f"/v1/workspaces/{wid}/items/{identity.dataset_id}",
            "status": 200, "response": item, "proofLevel": "SYNTHETIC_LOCAL",
        }, documents)
        inventories[alias] = document({
            "method": "POST", "path": f"/v1/workspaces/{wid}/semanticModels/{identity.dataset_id}/getDefinition?format=TMDL",
            "status": 200, "bodySha256": digest({}),
            "response": {"definition": bound["definition"]}, "proofLevel": "SYNTHETIC_LOCAL",
        }, documents)
        receipts[alias] = {
            "load_accepted": accepted_hash, "load_completed": completed_hash, "notebook_body": notebook_hash,
            "source_binding": source_binding_hash, "model_body": body_hash, "model_item": item_hash,
            "model_create": create_hash, "model_authorization": model_auth_hash,
        }
    sources = {
        alias: PopulationCapture(
            identity=identity,
            population=SourcePopulation(
                model=alias, evidence_kind="REVIEWED_LIVE", binding_hash=digest(identity.model_dump(mode="json")),
                profile="synthetic-only", seed=7, generator_revision="d" * 64, identity_revision="c" * 64,
                manifest_hash=digest(manifest),
                table_hashes={name: digest(name) for name in catalog.models[alias]["tables"]},
                shared_map_hashes=shared,
            ),
            source_schema_hash=inputs["enrollment"].source_schema_hashes[alias],
            data_revision="data-" + alias, model_revision="model-" + alias,
            deployment_packet_hash=packet_hash, source_manifest_file_hash=manifest_hash,
            deployment_approval_hash=approval_hash,
            load_assertion_hash=assertion_hash, model_inventory_hash=inventories[alias],
            deployment_receipts=receipts[alias],
            source_capture_id=captures[alias].capture_id, source_response_hash=captures[alias].response_hash,
        ) for alias, identity in binding.sources.items()
    }
    proof = ReviewedSourceProof(
        proof_kind="SYNTHETIC_TEST", scope=binding.scope, captured_at=now - timedelta(seconds=1),
        reviewed_at=datetime.now(timezone.utc), expires_at=now + timedelta(minutes=5),
        review_reference="Synthetic independent review fixture; no live authority",
        deployment_actor="SYNTHETIC_DEPLOYER",
        catalog_hash=catalog.revision, native_snapshot_hash=snapshot.content_hash,
        enrollment_digest=inputs["enrollment_digest"], source_profiles_hash=digest(profiles.model_dump()),
        context_contract_hash="e" * 64, documents=documents, sources=sources, load_assertion_status="COMPLETE",
    )
    arguments = {
        "scope": binding.scope, "catalog": catalog, "snapshot": snapshot, "enrollment": inputs["enrollment"],
        "profiles": profiles, "context_contract_hash": "e" * 64, "sink": sink, "synthetic": True,
    }
    return proof, arguments, bundle


async def test_private_proof_import_parses_originals_and_allows_independent_clocks(tmp_path):
    proof, arguments, _ = await proof_case(tmp_path)
    imported = ReviewedSourceProof.model_validate_json(proof.model_dump_json())
    records = imported.validate_for(pinned_digest=proof.proof_digest, **arguments)
    assert len(records) == 2 and records[0].model != records[1].model
    assert proof.sources["finance"].data_revision != proof.sources["subscriptions"].data_revision
    assert all(record.evidence_kind == "REVIEWED_LIVE" for record in records)
    with pytest.raises(LabError, match="fixture/test origin"):
        imported.validate_for(pinned_digest=proof.proof_digest, **{**arguments, "synthetic": False})


async def test_real_private_file_and_gateway_configuration_parsing(tmp_path):
    proof, arguments, _ = await proof_case(tmp_path)
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(proof.model_dump_json(), encoding="utf-8")
    declared_repo = tmp_path / "synthetic-repository"
    imported = ReviewedSourceProof.read(proof_path, declared_repo)
    assert imported.validate_for(pinned_digest=proof.proof_digest, **arguments)
    config = LiveDemoConfig(
        model_config_file=tmp_path / "model.json", model_approval_file=tmp_path / "approval.json",
        model_token_identity=ModelTokenIdentity(
            tenant_id="SYNTHETIC", subscription_id="SYNTHETIC", expected_user="SYNTHETIC",
            azure_config_directory=tmp_path / "isolated-auth-not-read",
        ),
        native_snapshot_file=tmp_path / "snapshot.json", native_enrollment_file=tmp_path / "enrollment.json",
        enrollment_digest=proof.enrollment_digest, original_native_receipt_file=tmp_path / "native.json",
        source_profiles_file=tmp_path / "profiles.json", source_proof_file=proof_path,
        source_proof_digest=proof.proof_digest,
    )
    config_path = tmp_path / "live.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    assert LiveDemoConfig.read(config_path, declared_repo) == config
    with pytest.raises(LabError, match="outside repository"):
        ReviewedSourceProof.read(proof_path, tmp_path)
    with pytest.raises(LabError, match="outside repository"):
        LiveDemoConfig.read(config_path, tmp_path)


@pytest.mark.parametrize("failure", [
    "pin", "tenant", "principal", "epoch", "expiry", "profiles", "enrollment", "context", "schema",
    "source", "seed", "map", "generator", "fixture", "document", "capture", "partial", "revision",
])
async def test_private_population_proof_fails_closed(tmp_path, failure):
    proof, arguments, _ = await proof_case(tmp_path)
    entry = proof.sources["finance"]
    pin = proof.proof_digest
    if failure == "pin":
        pin = "0" * 64
    elif failure in {"tenant", "principal", "epoch"}:
        field = "authorization_revision" if failure == "epoch" else failure
        arguments["scope"] = proof.scope.model_copy(update={field: "different"})
    elif failure == "expiry":
        proof.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif failure in {"profiles", "enrollment", "context"}:
        field = {"profiles": "source_profiles_hash", "enrollment": "enrollment_digest",
                 "context": "context_contract_hash"}[failure]
        setattr(proof, field, "0" * 64)
    elif failure == "schema":
        entry.source_schema_hash = "0" * 64
    elif failure == "source":
        entry.population.binding_hash = "0" * 64
    elif failure == "seed":
        entry.population.seed += 1
    elif failure == "map":
        entry.population.shared_map_hashes = {"wrong": "0" * 64}
    elif failure == "generator":
        entry.population.generator_revision = "wrong"
    elif failure == "fixture":
        entry.population.evidence_kind = "LOCAL_FIXTURE"
    elif failure == "document":
        proof.documents[entry.load_assertion_hash] = "{}"
    elif failure == "capture":
        entry.source_capture_id = "0" * 64
    elif failure == "partial":
        raw = json.loads(proof.documents[entry.load_assertion_hash])
        raw["tables"][0]["exactDataReadback"] = False
        entry.load_assertion_hash = document(raw, proof.documents)
    else:
        entry.model_revision = "not-observed"
    if failure != "pin":
        pin = proof.proof_digest
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=pin, **arguments)


@pytest.mark.parametrize("failure", [
    "manifest", "table", "load_job", "load_packet", "model_home", "model_identity", "unrelated_documents",
])
async def test_review_repin_cannot_relabel_unchanged_deployment_evidence(tmp_path, failure):
    proof, arguments, _ = await proof_case(tmp_path)
    entry = proof.sources["finance"]
    if failure == "manifest":
        entry.population.manifest_hash = "0" * 64
    elif failure == "table":
        entry.population.table_hashes = {name: "0" * 64 for name in entry.population.table_hashes}
    elif failure in {"load_job", "load_packet"}:
        assertion = json.loads(proof.documents[entry.load_assertion_hash])
        assertion["jobId" if failure == "load_job" else "packetSha256"] = "0" * 64
        entry.load_assertion_hash = document(assertion, proof.documents)
    elif failure == "model_home":
        inventory = json.loads(proof.documents[entry.model_inventory_hash])
        inventory["response"]["definition"]["parts"].pop()
        entry.model_inventory_hash = document(inventory, proof.documents)
    elif failure == "model_identity":
        item = json.loads(proof.documents[entry.deployment_receipts["model_item"]])
        item["response"]["id"] = "00000000-0000-0000-0000-000000000099"
        entry.deployment_receipts["model_item"] = document(item, proof.documents)
    else:
        entry.deployment_packet_hash = document({"unrelated": "but hash-consistent"}, proof.documents)
    # Authentic source captures and load/inventory evidence remain unchanged in
    # the population-only cases. Re-reviewing the import cannot repair the DAG.
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


async def test_private_proof_accepts_linked_existing_model_lro_result_and_rejects_wrong_result(tmp_path):
    proof, arguments, _ = await proof_case(tmp_path)
    entry = proof.sources["finance"]
    created = json.loads(proof.documents[entry.deployment_receipts["model_create"]])
    operation = "00000000-0000-0000-0000-000000000055"
    result_path = f"/v1/operations/{operation}/result"
    created.update(status=202, operationId=operation, resultPath=result_path,
                   operationPath=result_path.removesuffix("/result"), location=result_path.removesuffix("/result"))
    entry.deployment_receipts["model_create"] = document(created, proof.documents)
    result = {"method": "GET", "path": result_path, "status": 200,
              "proofLevel": "SYNTHETIC_LOCAL", "response": created["createdItem"]}
    entry.deployment_receipts["model_result"] = document(result, proof.documents)
    assert proof.validate_for(pinned_digest=proof.proof_digest, **arguments)
    result["path"] = "/v1/operations/00000000-0000-0000-0000-000000000099/result"
    entry.deployment_receipts["model_result"] = document(result, proof.documents)
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


async def test_private_proof_accepts_existing_definition_lro_and_rejects_unlinked_output(tmp_path):
    proof, arguments, _ = await proof_case(tmp_path)
    entry = proof.sources["finance"]
    inventory = json.loads(proof.documents[entry.model_inventory_hash])
    model_auth = json.loads(proof.documents[entry.deployment_receipts["model_authorization"]])
    operation = "00000000-0000-0000-0000-000000000056"
    operation_path = f"/v1/operations/{operation}"
    accepted = {
        "state": "ACCEPTED", "receiptValidated": True, "method": "POST", "status": 202,
        "path": inventory["path"], "bodySha256": digest({}), "approvalSha256": digest(model_auth),
        "operationId": operation, "operationPath": operation_path, "location": operation_path,
        "resultPath": operation_path + "/result",
    }
    entry.deployment_receipts["definition_accepted"] = document(accepted, proof.documents)
    inventory.update(method="GET", path=operation_path + "/result")
    entry.model_inventory_hash = document(inventory, proof.documents)
    assert proof.validate_for(pinned_digest=proof.proof_digest, **arguments)
    accepted["location"] = "file:" + operation_path
    entry.deployment_receipts["definition_accepted"] = document(accepted, proof.documents)
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


async def coverage_provider(*, matched=2, unknown=0, counts=None, table="sla_evaluation",
                            metric="enterprise.credit_exposure", business=None):
    original, session, _, _ = await setup_provider()
    scope = original.binding.scope.model_copy(update={"models": ["enterprise"]})
    identity = original.binding.sources["finance"]
    binding = original.binding.model_copy(update={"scope": scope, "sources": {"enterprise": identity}})
    client = DiscoveredClient(session, original.client.endpoint, original.client.protocol, reader_scope=scope)
    await client.discover()
    output = original.source_bindings["finance"].output
    source = ReviewedSourceBinding(
        identity, identity.dataset_id, {table: {metric: ReturnedColumn("Cents", "int64", True)}},
        output, CaptureReview("a" * 64, "synthetic source capture", "synthetic_test"),
    )
    control_source = replace(source, columns={table: {
        "matched_source_rows": ReturnedColumn("Matched", "int64", False),
        "unknown_source_rows": ReturnedColumn("Unknown", "int64", False),
    }})
    columns = deepcopy(original.catalog.tables[table]["columns"])
    diagnostic = None
    if table in {"service_month", "sla_evaluation"}:
        columns["coverage_complete"] = "bool"
        diagnostic = {"column": "coverage_complete", "kind": "effective_mapping"}
    now = datetime.now(timezone.utc)
    control = ReviewedCoverageBinding(
        control_source, "c" * 64, columns, diagnostic, now - timedelta(minutes=1), now + timedelta(minutes=5),
    )
    # Round trip the actual private configuration vocabulary, no supplied live profile.
    config = CoverageProfileConfig(
        method="catalog-filtered-raw-counts-v1",
        query=QueryProfileConfig(
            identity=identity, artifact_id=identity.dataset_id,
            columns={table: {name: ColumnConfig(**vars(column))
                             for name, column in control_source.columns[table].items()}},
            output=OutputConfig(**vars(output)),
            review=ReviewConfig(capture_hash="a" * 64, review_reference="synthetic transport fixture",
                                provenance="synthetic_test"),
        ),
        source_schema_hash=control.source_schema_hash, columns=columns, diagnostic=diagnostic,
        reviewed_at=control.reviewed_at, expires_at=control.expires_at,
    )
    parsed = CoverageProfileConfig.model_validate_json(config.model_dump_json()).build()
    control = replace(parsed, source=replace(parsed.source, review=control_source.review))
    sink = MemorySink()
    provider = MCPQueryProvider(
        original.catalog, client, binding, "synthetic-context", source_bindings={"enterprise": source},
        session_active=lambda current: current == scope, state_probe=original.state_probe, synthetic=True,
        coverage_bindings={"enterprise": {table: control}}, source_schema_hashes={"enterprise": "c" * 64},
        evidence_sink=sink, request_budget=SourceCallBudget(Budgets(max_queries=4, max_rows=20)),
    )
    node = QueryNode(
        id="coverage", kind="QuerySemanticModel", model="enterprise", table=table, metrics=[metric],
        expected_grain=[], date_role="month", max_rows=3,
        filters=[Filter(column="month", op="eq", values=["2026-07-01"]),
                 Filter(column="contract_key", op="in", values=[1, 2])],
    )
    def respond(arguments):
        if "VAR __raw" in arguments["daxQueries"][0]:
            return CallToolResult(content=[], structuredContent={
                "rows": [{"Matched": matched, "Unknown": unknown}] if counts is None else counts,
                "complete": True, "total": 1, "errors": [],
            })
        return CallToolResult(content=[], structuredContent={
            "rows": [{"Cents": 42}] if business is None else business, "complete": True, "total": 1, "errors": [],
        })
    session.responses["ExecuteQuery"] = respond
    return provider, session, node, sink


@pytest.mark.parametrize("matched,unknown,status", [
    (2, 0, Status.COMPLETE), (2, 1, Status.PARTIAL), (0, 0, Status.COMPLETE),
])
async def test_coverage_exact_filtered_controls_and_retained_linkage(matched, unknown, status):
    provider, session, node, sink = await coverage_provider(
        matched=matched, unknown=unknown, business=[{"Cents": None}] if not matched else None,
    )
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.status == receipt.status == status
    assert artifact.coverage["unknown_source_rows"] == unknown
    assert len(session.calls) == 2
    control, business = [entry[1] for entry in session.calls]
    assert control["artifactId"] == business["artifactId"]
    assert control["maxRows"] == 1 and business["maxRows"] == node.max_rows + 1
    for expression in ("DATE(2026, 7, 1)", "TREATAS({1, 2}, 'SLA Evaluation'[contract_key])"):
        assert expression in control["daxQueries"][0] and expression in business["daxQueries"][0]
    assert "[Credit Exposure]" not in control["daxQueries"][0]
    originals = list(sink.values.values())
    assert originals[0]["context"]["kind"] == "SOURCE_COVERAGE_QUERY"
    assert originals[1]["context"]["coverage_control"]["capture_id"] in artifact.evidence_ids
    assert originals[0]["context"]["business_query_hash"] == receipt.query_hash
    assert originals[0]["response"]["structuredContent"]["rows"][0]["Unknown"] == unknown


@pytest.mark.parametrize("counts", [
    [], [{"Matched": 1, "Wrong": 0}], [{"Matched": 1, "Unknown": None}],
    [{"Matched": True, "Unknown": 0}], [{"Matched": 1.0, "Unknown": 0}],
    [{"Matched": -1, "Unknown": 0}], [{"Matched": 1, "Unknown": 2}],
    [{"Matched": 1, "Unknown": 0}, {"Matched": 1, "Unknown": 0}],
])
async def test_invalid_coverage_stops_before_business_but_retains_original(counts):
    provider, session, node, sink = await coverage_provider(counts=counts)
    with pytest.raises(LabError):
        await provider.query(node, provider.binding.scope)
    assert len(session.calls) == len(sink.values) == 1


@pytest.mark.parametrize("failure", ["partial", "embedded_error", "isError", "wrong_total"])
async def test_coverage_http_success_never_hides_partial_or_embedded_errors(failure):
    provider, session, node, sink = await coverage_provider()
    result = CallToolResult(content=[], structuredContent={
        "rows": [{"Matched": 2, "Unknown": 0}], "complete": True, "total": 1, "errors": [],
    })
    if failure == "partial":
        result.structuredContent["complete"] = False
    elif failure == "embedded_error":
        result.structuredContent["errors"] = ["synthetic per-query error"]
    elif failure == "isError":
        result.isError = True
    else:
        result.structuredContent["total"] = 2
    session.responses["ExecuteQuery"] = result
    with pytest.raises(LabError):
        await provider.query(node, provider.binding.scope)
    assert len(session.calls) == len(sink.values) == 1


async def test_coverage_known_empty_cannot_certify_a_nonempty_business_amount():
    provider, session, node, sink = await coverage_provider(matched=0, business=[{"Cents": 42}])
    with pytest.raises(LabError, match="known absence"):
        await provider.query(node, provider.binding.scope)
    assert len(session.calls) == len(sink.values) == 2


@pytest.mark.parametrize("failure", [None, "query", "column", "binding", "missing"])
async def test_original_reviewed_coverage_calibration_is_reopened_and_not_hash_only(failure):
    provider, session, node, sink = await coverage_provider()
    profile = provider.coverage_bindings["enterprise"][node.table]
    response = CallToolResult(content=[], structuredContent={
        "rows": [{"Matched": 2, "Unknown": 0}], "complete": True, "total": 1, "errors": [],
    }).model_dump(mode="json")
    context = {
        "kind": "SOURCE_COVERAGE_QUERY", "binding_hash": provider.binding.binding_hash,
        "source": profile.source.identity.model_dump(mode="json"), "source_schema_hash": profile.source_schema_hash,
        "query_node": node.model_dump(mode="json"),
    }
    request = {"artifactId": str(profile.source.artifact_id),
               "daxQueries": [coverage_dax(provider.catalog, node, profile)], "maxRows": 1}
    if failure == "query":
        request["daxQueries"] = ["EVALUATE ROW(\"unfiltered\", 1)"]
    elif failure == "column":
        response["structuredContent"]["rows"] = [{"Wrong": 2, "Unknown": 0}]
    elif failure == "binding":
        context["binding_hash"] = "0" * 64
    reference = sink.capture(
        tool="ExecuteQuery", source_alias=node.model, request=request, response=response,
        context=context, synthetic=True,
    )
    profile = replace(
        profile, review_capture_id=reference.capture_id, reviewed_at=datetime.now(timezone.utc),
        source=replace(profile.source, review=replace(profile.source.review, capture_hash=reference.capture_id)),
    )
    provider.coverage_bindings["enterprise"][node.table] = profile
    provider._configuration_hash = provider._configuration()
    if failure == "missing":
        sink.values.clear()
    if failure is not None:
        with pytest.raises(LabError):
            await provider.query(node, provider.binding.scope)
        assert not session.calls
    else:
        artifact, _ = await provider.query(node, provider.binding.scope)
        assert artifact.status == Status.COMPLETE and len(session.calls) == 2


async def observed_coverage_provider(*, matched=2, unknown=0):
    from telco_iq.mcp_client import ReviewedIQWitnessOutput
    provider, session, node, sink = await coverage_provider(matched=matched, unknown=unknown,
                                                          business=[{"Cents": None}] if not matched else None)
    profile = provider.coverage_bindings["enterprise"][node.table]
    identity = profile.source.identity
    names = ("matched_source_rows", "unknown_source_rows")
    output = ReviewedIQWitnessOutput(
        schema_hash=provider.client.schema_hash("ExecuteQuery"), evidence="Synthetic observed technical-count fixture",
        artifact_id=identity.dataset_id, workspace_id=identity.workspace_id,
        columns={name: "Int64" for name in names}, int64_identity_metrics=(),
    )
    profile = replace(profile, source=replace(profile.source, output=output, columns={node.table: {
        name: ReturnedColumn(name, "int64", False) for name in names
    }}))
    provider.coverage_bindings["enterprise"][node.table] = profile
    provider.client.protocol = "2025-11-25"
    provider._configuration_hash = provider._configuration()
    model = {
        "ArtifactId": str(identity.dataset_id), "Name": "Synthetic source", "Description": "Synthetic only",
        "Url": f"https://app.powerbi.com/groups/{identity.workspace_id}/modeling/{identity.dataset_id}/modelView",
        "IconUrl": "https://app.powerbi.com/synthetic.svg",
    }
    body = {
        "semanticModel": model, "executionResult": {"tables": [{
            "columns": [{"name": "__kind", "type": "String"}] + [{"name": name, "type": "Int64"} for name in names]
                       + [{"name": "__total_rows", "type": "Int64"}],
            "rows": [["count", None, None, 1], ["data", matched, unknown, 1]],
        }]},
    }
    business = session.responses["ExecuteQuery"]
    def respond(arguments):
        if "VAR __raw" not in arguments["daxQueries"][0]:
            return business(arguments)
        return CallToolResult(content=[{"type": "text", "text": json.dumps(body)}],
                              structuredContent={"artifact_citation": model})
    session.responses["ExecuteQuery"] = respond
    return provider, session, node, sink, body, respond


@pytest.mark.parametrize("matched,unknown,status", [
    (2, 0, Status.COMPLETE), (2, 1, Status.PARTIAL), (0, 0, Status.COMPLETE),
])
async def test_observed_witnessed_coverage_controls_keep_filtered_unknowns_and_actual_transport_budget(matched, unknown, status):
    provider, session, node, sink, _, _ = await observed_coverage_provider(matched=matched, unknown=unknown)
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.status == receipt.status == status and artifact.coverage["unknown_source_rows"] == unknown
    assert len(session.calls) == 2 and session.calls[0][1]["maxRows"] == 2
    assert provider.request_budget.rows == 3
    assert "EVALUATE\nUNION(__witness, __data)" in session.calls[0][1]["daxQueries"][0]
    assert session.calls[0][1]["daxQueries"][0].startswith("DEFINE\nVAR __raw")
    assert "TREATAS({1, 2}, 'SLA Evaluation'[contract_key])" in session.calls[0][1]["daxQueries"][0]
    assert "[Credit Exposure]" not in session.calls[0][1]["daxQueries"][0]
    assert next(iter(sink.values.values()))["context"]["kind"] == "SOURCE_COVERAGE_QUERY"


@pytest.mark.parametrize("failure", [None, "query", "schema", "epoch", "witness", "formatted"])
async def test_observed_coverage_reopens_exact_original_compiler_and_never_dispatches_after_invalid_calibration(failure):
    from telco_iq.mcp_provider import compile_coverage_dax
    provider, session, node, sink, body, respond = await observed_coverage_provider()
    profile = provider.coverage_bindings["enterprise"][node.table]
    compiled = compile_coverage_dax(provider.catalog, node, profile.columns, profile.diagnostic)
    request = {"artifactId": str(profile.source.artifact_id), "daxQueries": [compiled.text], "maxRows": 2}
    context = {
        "kind": "SOURCE_COVERAGE_QUERY", "binding_hash": provider.binding.binding_hash,
        "source": profile.source.identity.model_dump(mode="json"), "source_schema_hash": profile.source_schema_hash,
        "schema_hash": provider.client.schema_hash("ExecuteQuery"), "query_node": node.model_dump(mode="json"),
    }
    if failure == "query":
        request["daxQueries"] = ["EVALUATE ROW(\"not_the_control\", 0)"]
    elif failure == "schema":
        context["schema_hash"] = "0" * 64
    elif failure == "epoch":
        context["binding_hash"] = "0" * 64
    elif failure == "witness":
        body["executionResult"]["tables"][0]["rows"][0][-1] = 2
    elif failure == "formatted":
        body["executionResult"]["tables"][0]["rows"][1][1] = "2.00"
    reference = sink.capture(
        tool="ExecuteQuery", source_alias=node.model, request=request,
        response=respond({"daxQueries": [compiled.text]}).model_dump(mode="json", exclude_unset=True),
        context=context, synthetic=True,
    )
    profile = replace(profile, review_capture_id=reference.capture_id, reviewed_at=datetime.now(timezone.utc),
                      source=replace(profile.source, review=replace(profile.source.review, capture_hash=reference.capture_id)))
    provider.coverage_bindings["enterprise"][node.table] = profile
    provider._configuration_hash = provider._configuration()
    if failure is None:
        artifact, _ = await provider.query(node, provider.binding.scope)
        assert artifact.status == Status.COMPLETE and len(session.calls) == 2
    else:
        with pytest.raises(LabError):
            await provider.query(node, provider.binding.scope)
        assert not session.calls


@pytest.mark.parametrize("failure", ["old", "schema", "diagnostic", "column", "home", "identity"])
async def test_coverage_reviews_block_before_any_query(failure):
    provider, session, node, _ = await coverage_provider()
    control = provider.coverage_bindings["enterprise"][node.table]
    if failure == "old":
        control = replace(control, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    elif failure == "schema":
        control = replace(control, source_schema_hash="0" * 64)
    elif failure == "diagnostic":
        control = replace(control, diagnostic=None)
    elif failure == "column":
        control = replace(control, columns={})
    elif failure == "home":
        provider.coverage_bindings["enterprise"] = {"wrong": control}
    else:
        control = replace(control, source=replace(control.source, artifact_id=provider.binding.ontology_item_id))
    if failure != "home":
        provider.coverage_bindings["enterprise"][node.table] = control
    provider._configuration_hash = provider._configuration()
    with pytest.raises(LabError):
        await provider.query(node, provider.binding.scope)
    assert session.calls == []


@pytest.mark.parametrize("table,metric,matched,unknown,value,status", [
    ("applied_credit", "enterprise.applied_credits", 0, 0, None, Status.COMPLETE),
    ("applied_credit", "enterprise.applied_credits", 1, 0, 0, Status.COMPLETE),
    ("applied_credit", "enterprise.applied_credits", 1, 1, None, Status.PARTIAL),
    ("service_month", "enterprise.breached_services", 1, 0, 0, Status.COMPLETE),
    ("service_month", "enterprise.breached_services", 1, 1, None, Status.PARTIAL),
])
async def test_known_absence_zero_and_unknown_are_distinct(table, metric, matched, unknown, value, status):
    provider, _, node, _ = await coverage_provider(
        table=table, metric=metric, matched=matched, unknown=unknown, business=[{"Cents": value}],
    )
    artifact, _ = await provider.query(node, provider.binding.scope)
    assert artifact.rows == [{metric: value}] and artifact.status == status


async def test_controls_budget_and_cancellation_stop_business_without_retry():
    provider, session, node, sink = await coverage_provider()
    provider.request_budget = SourceCallBudget(Budgets(max_queries=1, max_rows=10))
    with pytest.raises(LabError, match="request budget"):
        await provider.query(node, provider.binding.scope)
    assert len(session.calls) == len(sink.values) == 1
    provider, session, node, sink = await coverage_provider()
    active = {"yes": True}
    provider.session_active = lambda _: active["yes"]
    session.on_call = lambda *_: active.update(yes=False)
    with pytest.raises(LabError) as error:
        await provider.query(node, provider.binding.scope)
    assert error.value.status == Status.BLOCKED_AUTH and len(session.calls) == 1


async def test_real_private_proof_native_authority_executor_provider_and_cache(tmp_path, monkeypatch):
    proof, arguments, bundle = await proof_case(tmp_path)
    pin = proof.proof_digest
    records = proof.validate_for(pinned_digest=pin, **arguments)
    budget = SourceCallBudget(Budgets(max_queries=6, max_rows=12))
    def response(request):
        alias = next(alias for alias, source in bundle.binding.sources.items()
                     if str(source.dataset_id) == request["artifactId"])
        if request["daxQueries"] == [arguments["profiles"].states[alias].dax]:
            return CallToolResult(content=[], structuredContent={
                "data": "data-" + alias, "model": "model-" + alias, "complete": True, "errors": [],
            })
        return CallToolResult(content=[], structuredContent={
            "rows": [{"Account": 1, "Amount": 7}], "complete": True, "total": 1, "errors": [],
        })
    session = SyntheticSession([QUERY_TOOL], {"ExecuteQuery": response})
    client = DiscoveredClient(
        session, Endpoint("https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq", "iq"),
        "synthetic-only", reader_scope=bundle.binding.scope,
    )
    await client.discover()
    output = ReviewedOutput(
        client.schema_hash("ExecuteQuery"), "/rows", "/complete", "/total", "/errors", ENVELOPE,
        "Synthetic structured profile, no live codec claimed",
    )
    sources = {
        alias: ReviewedSourceBinding(
            identity, identity.dataset_id, {f"{alias}_fact": {
                "account_key": ReturnedColumn("Account", "int64", False),
                f"{alias}.amount": ReturnedColumn("Amount", "int64", False),
            }}, output, CaptureReview("1" * 64, "synthetic review only", "synthetic_test"),
        ) for alias, identity in bundle.binding.sources.items()
    }
    def check():
        return proof.validate_for(pinned_digest=pin, **arguments)
    observe = state_probe(
        client, arguments["profiles"].states, session_active=lambda scope: scope == bundle.binding.scope,
        evidence_sink=arguments["sink"], synthetic=True, request_budget=budget,
    )
    provider = MCPQueryProvider(
        arguments["catalog"], client, bundle.binding, bundle.context.revision, source_bindings=sources,
        session_active=lambda scope: scope == bundle.binding.scope, state_probe=observe, synthetic=True,
        evidence_sink=arguments["sink"], source_populations=records, population_check=check,
        expected_revisions={alias: (source.data_revision, source.model_revision)
                            for alias, source in proof.sources.items()}, request_budget=budget,
        population_evidence_ids={alias: [pin, source.source_capture_id] for alias, source in proof.sources.items()},
    )
    authority_records = []
    from telco_iq.native_context import validate_native_metric_plan

    def authority(*args, **kwargs):
        authority_records.append(kwargs["source_populations"])
        return validate_native_metric_plan(*args, **kwargs)

    monkeypatch.setattr("telco_iq.live_demo.validate_native_metric_plan", authority)
    executor = SourceProofExecutor(
        arguments["catalog"], provider, bundle.context, source_populations=records,
        native_bundle=bundle, population_check=check,
    )
    plan = native_plan(bundle.snapshot, scope=bundle.binding.scope, context=bundle.context)
    plan.nodes[0].max_rows = 2
    result = await executor.execute(plan, bundle.binding.scope)
    assert result.status == Status.COMPLETE, result.errors
    assert authority_records[0] is records and executor.source_populations is records
    assert provider.source_populations is records
    assert result.answer.source_populations == [proof.sources["subscriptions"].population]
    assert pin in result.answer.evidence_ids
    assert budget.requests == 3 and budget.rows == 3  # state, business, state
    assert len(session.calls) == 3
    key = CacheKey("synthetic", "reader", "epoch", "source", "schema", "data", "ontology", "filters")
    async def allowed(_):
        check()
        return True
    cache = ScopedCache(allowed)
    async def retained():
        return result.answer
    first = await cache.get(key, retained)
    second = await cache.get(key, retained)
    assert first is second and second.source_populations == result.answer.source_populations
    proof.sources["finance"].population.seed += 1
    with pytest.raises(LabError):
        await cache.get(key, retained)
    assert len(session.calls) == 3


async def test_live_planner_uses_the_same_population_authority_and_blocks_exhausted_source_budget(tmp_path, monkeypatch):
    proof, arguments, bundle = await proof_case(tmp_path)
    records = proof.validate_for(pinned_digest=proof.proof_digest, **arguments)
    plan = native_plan(bundle.snapshot, scope=bundle.binding.scope, context=bundle.context)
    model_calls = []
    async def proposed(*args, **kwargs):
        model_calls.append("synthetic proposal, never inference")
        return plan, {"synthetic": True}
    monkeypatch.setattr("telco_iq.live_demo.Planner.propose", proposed)
    budget = SourceCallBudget(Budgets(max_queries=1))
    planner = SourceProofPlanner(
        None, None, "", "", population_check=lambda: records, request_budget=budget,
    )
    _, receipt = await planner.propose(
        "Synthetic", bundle.binding.scope, bundle.context, arguments["catalog"], native_snapshot=bundle,
    )
    assert receipt["metric_bindings"][0]["source_population"] == records[0].model_dump()
    budget.reserve()
    with pytest.raises(LabError, match="request budget"):
        await planner.propose(
            "Synthetic", bundle.binding.scope, bundle.context, arguments["catalog"], native_snapshot=bundle,
        )
    assert len(model_calls) == 1


async def test_simple_contract_count_does_not_inherit_an_unrequired_mapping_guard():
    provider, _, node, _ = await coverage_provider(table="service_month", metric="enterprise.contracted_services")
    profile = provider.coverage_bindings["enterprise"][node.table]
    dax = coverage_dax(provider.catalog, node, profile)
    assert "ISBLANK([contracted])" in dax and "coverage_complete" not in dax


@pytest.mark.parametrize("field,op,values", [
    ("month", "gte", ["2026-07-01"]), ("month", "lt", ["2026-08-01"]),
    ("contract_key", "eq", [2]), ("contract_key", "gt", [1]), ("contract_key", "in", [1, 3]),
])
async def test_coverage_compiler_preserves_the_catalog_filter_context(field, op, values):
    provider, _, node, _ = await coverage_provider()
    node.filters = [Filter(column=field, op=op, values=values)]
    profile = provider.coverage_bindings["enterprise"][node.table]
    query = coverage_dax(provider.catalog, node, profile)
    data = bounded_dax(provider.catalog, node)
    if op in {"eq", "in"}:
        expression = "TREATAS("
    else:
        expression = "FILTER(ALL("
    assert expression in query and expression in data
    assert "__raw" in query and "ISBLANK([eligible_credit_cents])" in query
    assert "ISBLANK([applied_credit_cents])" in query and "[coverage_complete] <> TRUE()" in query
