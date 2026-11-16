"""Actual P0 producer code with synthetic files/HTTP only; never a live receipt."""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from test_live_source_wiring import document, proof_case

from infra.fabric_p0 import Author
from telco_iq.catalog import Catalog, digest
from telco_iq.errors import LabError
from tools import build_p0_packet

ROOT = Path(__file__).resolve().parents[1]


async def legacy_case(tmp_path, monkeypatch, *, repaired=False):
    proof, arguments, _ = await proof_case(tmp_path)
    proof.documents = {}
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    arguments["catalog"] = catalog
    proof.catalog_hash = catalog.revision
    stage = tmp_path / ".local" / "p0-packet"
    packet = build_p0_packet.build(ROOT / "data" / "fixtures" / "miniature", stage)
    manifest_path = ROOT / "data" / "fixtures" / "miniature" / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    tables = {table["id"]: table for table in manifest["tables"]}
    wid = str(next(iter(proof.sources.values())).identity.workspace_id)
    lid, nid, jid = [f"00000000-0000-0000-0000-{value:012d}" for value in (71, 72, 73)]
    workspace = {"id": wid, "displayName": packet["workspaceName"]}
    lakehouse = {"id": lid, "displayName": packet["lakehouseName"], "type": "Lakehouse"}
    notebook = {"id": nid, "displayName": packet["notebookName"], "type": "Notebook"}
    job_path = f"/v1/workspaces/{wid}/items/{nid}/jobs/instances/{jid}"
    completed = {"id": jid, "itemId": nid, "jobType": "RunNotebook", "status": "Completed", "failureReason": None}
    author = object.__new__(Author)  # Never invoke authentication/ambient CLI.
    author.private = tmp_path / "synthetic-private"
    author.private.mkdir()
    author.renewal = None
    author.approval = {
        "approved": True, "tenantId": proof.scope.tenant, "authorUser": proof.deployment_actor,
        "workspaceName": packet["workspaceName"], "lakehouseName": packet["lakehouseName"],
        "notebookName": packet["notebookName"], "notebookSha256": packet["notebookSha256"],
        "modelTemplateHashes": {item["model"]: item["templateSha256"] for item in packet["modelPackets"]},
        "deadlineUtc": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "maxNotebookRuns": 1, "maxNotebookMinutes": 10,
    }
    service_definition = {}
    submissions = {"count": 0}

    def request(method, url, **kwargs):
        nonlocal job_path
        path = urlsplit(url).path
        headers = {}
        if method == "GET" and path.endswith("/jobs/instances"):
            body, status = {"value": []}, 200
        elif method == "GET" and path == job_path:
            body, status = completed, 200
        elif method == "GET" and path.endswith("/items"):
            body, status = {"value": []}, 200
        elif method == "GET" and path.endswith("/" + nid):
            body, status = notebook, 200
        elif method == "POST" and path.endswith("/items"):
            body, status = notebook, 201
        elif method == "POST" and path.endswith("/jobs/RunNotebook/instances"):
            submissions["count"] += 1
            current_job = f"00000000-0000-0000-0000-{72 + submissions['count']:012d}"
            job_path = f"/v1/workspaces/{wid}/items/{nid}/jobs/instances/{current_job}"
            completed["id"] = current_job
            body, status = None, 202
            headers["Location"] = "https://api.fabric.microsoft.com" + job_path
            headers["Retry-After"] = "0"
        elif method == "POST" and path.endswith("/getDefinition"):
            body, status = service_definition, 200
        elif method == "POST" and path.endswith("/updateDefinition"):
            body, status = {}, 200
        elif method == "POST" and path.endswith("/semanticModels"):
            alias = next(alias for alias in proof.sources if alias.title() in kwargs["json"]["displayName"])
            identity = proof.sources[alias].identity
            body, status = {"id": str(identity.dataset_id), "displayName": kwargs["json"]["displayName"],
                            "type": "SemanticModel", "workspaceId": wid}, 201
        else:
            alias = next(alias for alias, entry in proof.sources.items() if path.endswith(str(entry.identity.dataset_id)))
            body, status = {"id": str(proof.sources[alias].identity.dataset_id),
                            "displayName": f"TelcoIQ_{alias.title()}_P0", "type": "SemanticModel", "workspaceId": wid}, 200
        return httpx.Response(status, json=body, headers=headers)

    author.client = SimpleNamespace(request=request)
    monkeypatch.setattr("infra.fabric_p0.ROOT", tmp_path)
    monkeypatch.setattr("infra.fabric_p0.time.sleep", lambda _: None)
    author.save("p0-workspace.json", workspace)
    author.save("p0-lakehouse.json", lakehouse)
    author.data_load()
    if repaired:
        previous = json.loads((author.private / "p0-bound-notebook.ipynb").read_bytes())
        author.save("p0-notebook-service.ipynb", previous)
        service_definition.update({"definition": {"parts": [{
            "path": "notebook-content.ipynb", "payloadType": "InlineBase64",
            "payload": base64.b64encode(json.dumps(previous).encode()).decode(),
        }]}})
        notebook_file = stage / "Load_TelcoIQ_Miniature.ipynb"
        revised = json.loads(notebook_file.read_bytes())
        revised["cells"][1]["source"].append("\n# Explicitly approved synthetic repair.\n")
        notebook_file.write_bytes((json.dumps(revised, indent=2) + "\n").encode())
        revised_hash = hashlib.sha256(notebook_file.read_bytes()).hexdigest()
        author.save("p0-repair-approval.json", {
            "approved": True, "originalNotebookSha256": packet["notebookSha256"],
            "notebookId": nid, "maximumTotalRuns": 2, "revisedNotebookSha256": revised_hash,
        })
        author.data_load()
    author.models()

    def imported(path):
        original = path.read_bytes().decode("utf-8")
        key = hashlib.sha256(original.encode()).hexdigest()
        proof.documents[key] = original
        return key

    def receipt(name):
        return imported(author.private / name)

    http = [(path, json.loads(path.read_bytes())) for path in author.private.glob("http-*.json")]
    def http_receipt(method, path, *, body_hash=None):
        return imported(next(file for file, value in sorted(http, key=lambda pair: pair[1]["startedAt"], reverse=True) if
                             value["method"] == method and value["path"] == path
                             and (body_hash is None or value["bodySha256"] == body_hash)))

    packet_hash, manifest_hash = imported(stage / "packet.json"), imported(manifest_path)
    for name in ("finance-template.json", "subscriptions-template.json", "Load_TelcoIQ_Miniature.ipynb"):
        imported(stage / name)
    approval_hash = document(author.approval, proof.documents)
    load_hash = receipt("p0-notebook-load-success.json")
    common = {
        "workspace_item": receipt("p0-workspace.json"), "lakehouse_item": receipt("p0-lakehouse.json"),
        "notebook_item": receipt("p0-notebook.json"),
        "notebook_template": imported(stage / "Load_TelcoIQ_Miniature.ipynb"),
        "notebook_body": receipt("p0-bound-notebook.ipynb"),
        "notebook_create": receipt("p0-notebook-accepted.json"),
        "notebook_http_create": http_receipt("POST", f"/v1/workspaces/{wid}/items"),
        "load_accepted": receipt("p0-notebook-job-submitted.json"), "load_completed": load_hash,
        "load_http_submitted": http_receipt("POST", f"/v1/workspaces/{wid}/items/{nid}/jobs/RunNotebook/instances"),
        "load_http_completed": http_receipt("GET", job_path),
    }
    if repaired:
        common.update({
            "notebook_repair_approval": receipt("p0-repair-approval.json"),
            "notebook_repair_applied": receipt("p0-notebook-repair-applied.json"),
            "notebook_definition": http_receipt("POST", f"/v1/workspaces/{wid}/notebooks/{nid}/getDefinition"),
            "notebook_update": http_receipt("POST", f"/v1/workspaces/{wid}/notebooks/{nid}/updateDefinition"),
        })
    selected = {name for alias in proof.sources for name in catalog.models[alias]["tables"]}
    shared = {name for name in selected if (catalog.tables[name]["model"] == "core" and name != "quarantine")
              or name.endswith("_map") or name in {"subscription", "contract"}}
    identity_hash = digest({name: {key: catalog.tables[name][key] for key in ("columns", "primaryKey")}
                            for name in sorted(shared)})
    for alias, entry in proof.sources.items():
        body_path = author.private / f"p0-{alias}-bound-definition.json"
        body = json.loads(body_path.read_bytes())
        item_hash = receipt(f"p0-{alias}-readback.json")
        entry.receipt_kind = "LEGACY_P0"
        entry.deployment_packet_hash, entry.source_manifest_file_hash = packet_hash, manifest_hash
        entry.deployment_approval_hash, entry.load_assertion_hash = approval_hash, load_hash
        entry.model_inventory_hash = item_hash
        entry.deployment_receipts = {
            **common, "model_body": imported(body_path), "model_create": receipt(f"p0-{alias}-accepted.json"),
            "model_item": item_hash, "model_http_create": http_receipt(
                "POST", f"/v1/workspaces/{wid}/semanticModels", body_hash=digest(body),
            ),
        }
        entry.population = entry.population.model_copy(update={
            "profile": "miniature", "seed": manifest["seed"],
            "generator_revision": "LEGACY_P0_NOTEBOOK_SHA256:" + common["notebook_template"],
            "identity_revision": identity_hash, "manifest_hash": digest(manifest),
            "shared_map_hashes": {name: tables[name]["content_sha256"] for name in sorted(shared)},
            "table_hashes": {name: tables[name]["content_sha256"] for name in catalog.models[alias]["tables"]},
        })
    proof.reviewed_at = datetime.now(timezone.utc)
    return proof, arguments


async def test_original_p0_producer_receipts_activate_only_the_reviewed_legacy_import(tmp_path, monkeypatch):
    proof, arguments = await legacy_case(tmp_path, monkeypatch)
    records = proof.validate_for(pinned_digest=proof.proof_digest, **arguments)
    assert {record.model for record in records} == {"subscriptions", "finance"}
    packet = json.loads(proof.documents[proof.sources["finance"].deployment_packet_hash])
    assert "sourceModelParts" not in packet and "population" not in packet
    assert all(entry.receipt_kind == "LEGACY_P0" for entry in proof.sources.values())


async def test_original_p0_approved_repair_contract_is_not_forced_through_hero(tmp_path, monkeypatch):
    proof, arguments = await legacy_case(tmp_path, monkeypatch, repaired=True)
    assert proof.validate_for(pinned_digest=proof.proof_digest, **arguments)
    entry = proof.sources["finance"]
    repair = json.loads(proof.documents[entry.deployment_receipts["notebook_repair_approval"]])
    repair["notebookId"] = "00000000-0000-0000-0000-000000000099"
    entry.deployment_receipts["notebook_repair_approval"] = document(repair, proof.documents)
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


@pytest.mark.parametrize("failure", [
    "source", "manifest", "table", "shared", "seed", "job", "binding", "model", "path", "body", "notebook", "kind",
])
async def test_legacy_p0_cannot_repin_wrong_population_or_original_receipt_links(tmp_path, monkeypatch, failure):
    proof, arguments = await legacy_case(tmp_path, monkeypatch)
    entry = proof.sources["finance"]
    if failure in {"manifest", "table", "shared", "seed"}:
        if failure == "manifest":
            entry.population.manifest_hash = "0" * 64
        elif failure == "table":
            entry.population.table_hashes = {key: "0" * 64 for key in entry.population.table_hashes}
        elif failure == "shared":
            entry.population.shared_map_hashes = {key: "0" * 64 for key in entry.population.shared_map_hashes}
        else:
            entry.population.seed += 1
    elif failure == "source":
        entry.population.binding_hash = "0" * 64
    elif failure == "kind":
        entry.receipt_kind = "HERO_V1"
    else:
        key = {"job": "load_completed", "binding": "notebook_body", "model": "model_item",
               "path": "model_http_create", "body": "model_http_create", "notebook": "notebook_template"}[failure]
        value = json.loads(proof.documents[entry.deployment_receipts[key]])
        if failure in {"job", "model"}:
            value["id"] = "00000000-0000-0000-0000-000000000099"
        elif failure == "binding":
            value["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"] = "00000000-0000-0000-0000-000000000099"
        elif failure == "path":
            value["path"] = "/v1/workspaces/00000000-0000-0000-0000-000000000099/semanticModels"
        elif failure == "body":
            value["bodySha256"] = "0" * 64
        else:
            value["cells"][0]["source"] = ["ASSETS = []"]
        entry.deployment_receipts[key] = document(value, proof.documents)
        if failure == "job":
            entry.load_assertion_hash = entry.deployment_receipts[key]
        elif failure == "model":
            entry.model_inventory_hash = entry.deployment_receipts[key]
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


@pytest.mark.parametrize("failure", [None, "full_request", "approval", "source", "body"])
async def test_real_author_path_only_definition_receipt_requires_exact_approved_full_request(
    tmp_path, failure,
):
    proof, arguments, _ = await proof_case(tmp_path)
    assert all("receipt_kind" not in entry.model_dump(mode="json") for entry in proof.sources.values())
    entry = proof.sources["finance"]
    original = json.loads(proof.documents[entry.model_inventory_hash])
    approval = json.loads(proof.documents[entry.deployment_receipts["model_authorization"]])
    captured = []
    author = object.__new__(Author)
    author.renewal = SimpleNamespace(
        digest=digest(approval), begin=lambda *args: None, check_current=lambda: None,
        observe=lambda *args: None,
    )
    author.save = lambda name, value: captured.append(value | {"proofLevel": "SYNTHETIC_LOCAL"})
    author.client = SimpleNamespace(request=lambda *args, **kwargs: httpx.Response(200, json=original["response"]))
    author.request("POST", original["path"], json={})
    actual = captured[0]
    assert actual["path"] == urlsplit(original["path"]).path and "?" not in actual["path"]
    if failure == "full_request":
        approval["operations"][1]["path"] = original["path"].replace("TMDL", "TMSL")
        entry.deployment_receipts["model_authorization"] = document(approval, proof.documents)
        # Keep the already valid create-operation link independently consistent.
        created = json.loads(proof.documents[entry.deployment_receipts["model_create"]])
        created["approvalSha256"] = digest(approval)
        entry.deployment_receipts["model_create"] = document(created, proof.documents)
        actual["approvalSha256"] = digest(approval)
    elif failure == "approval":
        actual["approvalSha256"] = "0" * 64
    elif failure == "source":
        actual["path"] = actual["path"].replace(str(entry.identity.dataset_id), "00000000-0000-0000-0000-000000000099")
    elif failure == "body":
        actual["bodySha256"] = digest(None)
    entry.model_inventory_hash = document(actual, proof.documents)
    if failure is None:
        assert proof.validate_for(pinned_digest=proof.proof_digest, **arguments)
    else:
        with pytest.raises(LabError):
            proof.validate_for(pinned_digest=proof.proof_digest, **arguments)
