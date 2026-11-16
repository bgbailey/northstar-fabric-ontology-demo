"""TQ-007 synthetic transport contracts; never Azure authentication or live proof."""

import json
import shutil
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from infra import hero_deploy as deploy
from infra.fabric_p0 import Author
from infra.powerbi_refresh import FABRIC_RESOURCE, POWERBI_RESOURCE, PowerBIRefresh, query_rows, refresh_body
from infra.renewal import Ownership, Renewal, body_digest, read_json, sha256, stage_execution_window
from tools import build_data, build_hero_packet, build_models

ROOT = Path(__file__).resolve().parents[1]
TENANT = "00000000-0000-0000-0000-000000000101"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000102"
WORKSPACE = "00000000-0000-0000-0000-000000000103"
CAPACITY = "00000000-0000-0000-0000-000000000104"
PROTECTED = "00000000-0000-0000-0000-000000000105"
IDS = {key: f"00000000-0000-0000-0000-{number:012}" for key, number in (
    ("hero-lakehouse", 110), ("hero-loader", 111), ("enterprise", 112), ("finance", 113), ("network", 114),
)}
OPERATION = "00000000-0000-0000-0000-000000000120"
JOB = "00000000-0000-0000-0000-000000000121"
REFRESH = "00000000-0000-0000-0000-000000000122"
COMPETING = "00000000-0000-0000-0000-000000000123"
PREFIX = f"/v1/workspaces/{WORKSPACE}"


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("TQ-007 attempted live network/subprocess/authentication")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture(scope="module")
def packet_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("tq007-synthetic")
    accepted = ROOT / ".local" / "northstar-showcase-v2-2026-10-15-r4" / "hero"
    if accepted.exists():
        shutil.copytree(accepted, root / "packet")
    else:
        build_data.build("showcase-v2", build_data.SEED, root / "data")
        build_models.build(root / "models", root / "contracts", root / "data" / "manifest.json")
        build_hero_packet.build(
            root / "data", root / "packet", showcase=True, model_root=root / "models",
            catalog_path=root / "contracts" / "catalog.json",
        )
    return root / "packet"


def test_plan_reads_actual_full_packet_without_auth_or_transport(packet_dir, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A pure plan constructed Author or a client")

    monkeypatch.setattr(deploy.Author, "__init__", forbidden)
    report = deploy.plan(packet_dir)
    packet = read_json(packet_dir / "packet.json")
    assert report["status"] == "VALIDATED_LOCAL_PLAN_NOT_APPROVED"
    assert report["models"] == ["enterprise", "finance", "network"]
    assert report["packetSha256"] == sha256((packet_dir / "packet.json").read_bytes())
    assert report["rows"] == packet["rows"] == 7181
    assert report["sourceCompatibilityLevel"] == 1702
    assert report["sourcePbismVersion"] == "4.2"
    assert report["sourceMeasureCount"] == 24
    assert report["liveExecution"] == "NOT_RUN"
    assert {"infra/hero_deploy.py", "infra/powerbi_refresh.py"} <= {
        item["file"] for item in report["codeInventory"]
    }
    assert report["initialOperations"][0]["bodySha256"] == body_digest(
        read_json(packet_dir / "lakehouse-template.json"),
    )
    assert all(item["bodySha256"] is None for item in report["boundOperations"])
    assert report["approvalBreakpoint"] == "BOUND_LOCALLY_NOT_APPROVED"


def test_plan_stages_only_new_unapproved_output(packet_dir, tmp_path):
    output = tmp_path / "plan"
    report = deploy.plan(packet_dir, output=output)
    assert read_json(output / "hero-plan.json") == report
    assert read_json(output / "hero-approval-template.json")["approved"] is False
    with pytest.raises(FileExistsError):
        deploy.plan(packet_dir, output=output)


def test_changed_packet_asset_fails_before_output(packet_dir, tmp_path):
    copied = tmp_path / "changed"
    shutil.copytree(packet_dir, copied)
    (copied / "finance-template.json").write_text("{}")
    with pytest.raises((ValueError, RuntimeError), match="changed"):
        deploy.plan(copied, output=tmp_path / "never-created")
    assert not (tmp_path / "never-created").exists()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return deploy.artifact(path)


def seed_workspace(private):
    item = {"id": WORKSPACE, "displayName": "Synthetic_Hero_Workspace", "capacityId": CAPACITY}
    body = {"displayName": item["displayName"], "capacityId": CAPACITY}
    authorization = {"approved": True, "operations": [{
        "name": "create-workspace", "method": "POST", "path": "/v1/workspaces",
        "bodySha256": body_digest(body), "createdDisplayName": item["displayName"],
        "createsAlias": "workspace",
    }]}
    return Ownership.model_validate({
        "authorization": save(private / "workspace-approval.json", authorization),
        "accepted": save(private / "workspace-accepted.json", {
            "approvalSha256": body_digest(authorization), "operation": "create-workspace",
            "state": "ACCEPTED", "receiptValidated": True, "status": 201, "method": "POST",
            "path": "/v1/workspaces", "bodySha256": body_digest(body),
            "createdId": WORKSPACE, "response": item,
        }),
        "readback": save(private / "workspace-readback.json", {
            "method": "GET", "path": PREFIX, "status": 200, "response": item,
            "proofLevel": "SYNTHETIC_LOCAL",
        }),
    })


def configure(private, packet_dir, *, phase="initial", ownership=None, bound=None, proof=None,
              assertions=None, alter_scope=None, alter_approval=None, dependency_files=None,
              capacity_policy="DEDICATED_CAPACITY", lro_polling=None):
    packet = read_json(packet_dir / "packet.json")
    ownership = ownership or {"workspace": seed_workspace(private)}
    dependencies = {}
    for task in ("TQ-003", "TQ-004", "TQ-005"):
        if dependency_files:
            dependencies[task] = deploy.artifact(dependency_files[task])
            continue
        file = private / (task + ".json")
        if not file.exists():
            save(file, {"task": task, "status": "ACCEPTED_LOCAL_ONLY", "proofLevel": "SYNTHETIC_LOCAL"})
        dependencies[task] = deploy.artifact(file)
    scope = {
        "schemaVersion": "hero-deployment-scope-v1", "phase": phase,
        "packet": deploy.artifact(packet_dir / "packet.json"),
        "populationSha256": body_digest(packet["population"]),
        "codeInventory": [deploy.artifact(ROOT / name) for name in deploy.CODE],
        "dependencies": dependencies, "capacityId": CAPACITY, "capacityPolicy": capacity_policy,
        "principal": {
            "tenantId": TENANT, "subscriptionId": SUBSCRIPTION, "authorUser": "synthetic@invalid.test",
            "azureCliExecutable": str(private / "selected-private-az.cmd"),
            "azureConfigDirectory": str(private / "selected-private-az-profile"),
            "fabricResource": FABRIC_RESOURCE, "powerbiResource": POWERBI_RESOURCE,
        }, "maximumPolls": 4, "maximumPollSeconds": 60,
    }
    if bound:
        scope["boundPacket"] = deploy.artifact(bound / "bound-packet.json")
        scope["bindingMetadata"] = deploy.artifact(bound / "hero-binding.json")
    if lro_polling is not None:
        scope["lroPolling"] = lro_polling
    if phase == "models":
        scope["loadProof"] = proof
        scope["sourceAssertions"] = assertions
        scope["refreshBodies"] = {model: {"notifyOption": "NoNotification"} for model in deploy.MODELS}
        catalog = read_json(packet_dir / "source-catalog.json")
        names = {item["id"]: item["displayName"] for item in catalog["tables"]}
        scope["controls"] = []
        for model in deploy.MODELS:
            metric = next(item for item in catalog["metrics"] if item["model"] == model)
            table = next(item for item in catalog["tables"] if item["id"] == metric["table"])
            source = "'" + names[metric["table"]] + "'"
            unknown = (
                f"COUNTROWS(FILTER({source}, {source}[{table['coverage']['column']}] <> TRUE()))"
                if table.get("coverage") else "0"
            )
            scope["controls"].append({
                "name": model + "-selected-measure", "model": model, "homeTable": names[metric["table"]],
                "measure": metric["name"], "body": {
                    "queries": [{"query": (
                        f'EVALUATE ROW("Value", [{metric["name"]}], '
                        f'"RowsMatched", COUNTROWS({source}), "RowsUnknown", {unknown}, "RowsInvalid", 0)'
                    )}],
                    "serializerSettings": {"includeNulls": True},
                }, "expectedRows": [{"[Value]": 7, "[RowsMatched]": 8, "[RowsUnknown]": 0, "[RowsInvalid]": 0}],
                "coverage": "KNOWN", "completeAggregate": True, "matchedRowsField": "[RowsMatched]",
                "unknownRowsField": "[RowsUnknown]", "invalidRowsField": "[RowsInvalid]",
            })
    if alter_scope:
        alter_scope(scope)
    scope_file = private / (phase + "-scope.json")
    save(scope_file, scope)
    files = [scope_file, packet_dir / "packet.json", *[
        packet_dir / item["file"] for item in packet["packetFiles"]
    ], *[ROOT / name for name in deploy.CODE], *[Path(item["file"]) for item in dependencies.values()]]
    if scope.get("liveConfig"):
        files.append(Path(scope["liveConfig"]["file"]))
    for spec in ownership.values():
        files.extend(Path(getattr(spec, field).file) for field in ("authorization", "accepted", "readback", "result")
                     if getattr(spec, field) is not None)
    if bound:
        files.extend(bound.iterdir())
    if proof:
        files.extend(Path(value["file"]) for value in proof.values())
    if assertions:
        files.append(Path(assertions["file"]))
    artifacts = [deploy.artifact(file) for file in dict.fromkeys(files)]
    now = datetime.now(timezone.utc)
    capacity_resource = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/synthetic/providers/Microsoft.Fabric/capacities/test"
    )
    automation = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/synthetic"
        "/providers/Microsoft.Automation/automationAccounts/test"
    )
    parsed = deploy.HeroScope.model_validate(scope)
    approval = {
        "schemaVersion": "fabric-renewal-v1", "approved": True, "confirmationReference": "SYNTHETIC_NOT_AUTHORITY",
        "tenantId": TENANT, "subscriptionId": SUBSCRIPTION, "authorUser": "synthetic@invalid.test",
        "workspaceId": WORKSPACE, "ownedItems": {key: IDS[key] for key in ownership if key != "workspace"},
        "ownershipReceipts": {key: value.model_dump() for key, value in ownership.items()},
        "protectedItemIds": [PROTECTED], "artifacts": artifacts, "artifactSetSha256": body_digest(artifacts),
        "operations": deploy.operation_contract(parsed, packet, WORKSPACE, {"directory": bound}),
        "cost": {"resource": capacity_resource, "currency": "USD", "unitPrice": 1.0, "estimated": 1.0,
                 "cap": 2.0, "basis": "SYNTHETIC test numbers only, not an actual price/cost estimate"},
        "window": {"ttlSeconds": 600, "minimumRemainingSeconds": 1, "shutdownRetrySeconds": 7200,
                   "latestStartUtc": (now + timedelta(minutes=20)).isoformat(),
                   "notAfterUtc": (now + timedelta(hours=1)).isoformat()},
        "shutdown": {"approvalReference": "SYNTHETIC", "capacityResourceId": capacity_resource,
                     "scheduleResourceId": automation + "/schedules/test", "runbookName": "Synthetic",
                     "runbookSha256": "a" * 64, "receiptFile": phase + "-shutdown.json"},
        "teardown": {"owner": "SYNTHETIC owner", "mechanism": "Owned IDs only",
                     "approvalReference": "SYNTHETIC separate cleanup", "ownedOnly": True},
        "maximumRequests": 150,
        "capacityPolicy": capacity_policy,
    }
    if lro_polling is not None:
        approval["lroPolling"] = lro_polling
    if capacity_policy == "SHARED_CAPACITY_AS_IS":
        approval["shutdown"] = None
        approval["sharedCapacityWorkload"] = {
            "approved": True, "approvalReference": "SYNTHETIC explicit shared workload approval",
            "capacityId": CAPACITY, "capacityResourceId": capacity_resource,
            "notAfterUtc": approval["window"]["notAfterUtc"], "owner": approval["teardown"]["owner"],
            "artifactDisposition": "RETAIN", "enforcement": "MANUAL_OWNER_FINITE_STAGES",
            "jobStopAuthority": "SEPARATE_APPROVAL_RECEIPT_OWNED_JOBS_ONLY",
        }
    if alter_approval:
        alter_approval(approval)
    approval_file = private / (phase + "-approval.json")
    save(approval_file, approval)
    window = stage_execution_window(approval_file, private, now=now)
    if approval["shutdown"] is None:
        return Renewal(private, approval_file), scope_file
    deadline = datetime.fromisoformat(window["deadlineUtc"])
    save(private / approval["shutdown"]["receiptFile"], {
        "observedAtUtc": now.isoformat(), "tenantId": TENANT, "subscriptionId": SUBSCRIPTION,
        "runbookSha256": "a" * 64, "inspect": {"status": "Completed", "mutations": 0},
        "schedule": {"id": approval["shutdown"]["scheduleResourceId"], "properties": {
            "isEnabled": True, "frequency": "Hour", "interval": 1, "startTime": window["deadlineUtc"],
            "expiryTime": (deadline + timedelta(seconds=7200)).isoformat(),
        }}, "binding": {"id": automation + "/jobSchedules/" + JOB, "properties": {
            "schedule": {"name": "test"}, "runbook": {"name": "Synthetic"},
            "parameters": {"TargetResourceId": capacity_resource,
                           "TargetSha256": sha256(capacity_resource.lower().encode()), "TenantId": TENANT,
                           "DeadlineUtc": window["deadlineUtc"], "Mode": "SuspendWhenDue"},
        }},
    })
    return Renewal(private, approval_file), scope_file


class SyntheticService:
    def __init__(self, packet_dir):
        self.packet = read_json(packet_dir / "packet.json")
        self.sent, self.items, self.bodies = [], {}, {}
        self.pending, self.polls = None, 0
        self.async_alias = "enterprise"
        self.location_override = None
        self.no_result = False
        self.returned_id = None
        self.collision = False
        self.job_pending = 0
        self.refresh_pending = 0
        self.query_body = {"results": [{"tables": [{"rows": [
            {"[Value]": 7, "[RowsMatched]": 8, "[RowsUnknown]": 0, "[RowsInvalid]": 0},
        ]}]}]}
        self.refresh_location = True
        self.fail_post = False
        self.definition_changed = False
        self.capacity_state = "Active"
        self.capacity_sku = "F8"

    def handle(self, request):
        self.sent.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if request.method == "GET" and path == "/v1/capacities":
            return httpx.Response(200, json={"value": [{
                "id": CAPACITY, "state": self.capacity_state, "sku": self.capacity_sku,
            }]})
        if request.method == "GET" and path == PREFIX:
            return httpx.Response(200, json={
                "id": WORKSPACE, "displayName": "Synthetic_Hero_Workspace", "capacityId": CAPACITY,
            })
        if request.method == "GET" and path == PREFIX + "/items":
            items = list(self.items.values())
            if self.collision:
                items.append({"id": PROTECTED, "type": "Lakehouse", "displayName": self.packet["lakehouseName"]})
            return httpx.Response(200, json={"value": items})
        if request.method == "POST" and path in (PREFIX + "/items", PREFIX + "/semanticModels"):
            if self.fail_post:
                raise httpx.ReadTimeout("SYNTHETIC unknown dispatched outcome", request=request)
            alias = "hero-lakehouse" if body.get("type") == "Lakehouse" else (
                "hero-loader" if body.get("type") == "Notebook" else next(
                    item["alias"] for item in self.packet["modelPackets"] if item["displayName"] == body["displayName"]
                )
            )
            item = {"id": self.returned_id or IDS[alias], "displayName": body["displayName"],
                    "type": body.get("type", "SemanticModel"), "workspaceId": WORKSPACE}
            self.items[alias], self.bodies[item["id"]] = item, body
            if alias == self.async_alias:
                self.pending = item
                return httpx.Response(202, headers={
                    "Location": self.location_override or f"https://api.fabric.microsoft.com/v1/operations/{OPERATION}",
                    "x-ms-operation-id": OPERATION, "Retry-After": "0",
                })
            return httpx.Response(201, json=item)
        if path == f"/v1/operations/{OPERATION}":
            self.polls += 1
            if self.polls == 1:
                return httpx.Response(200, json={"status": "Running"}, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"status": "Succeeded"}, headers={} if self.no_result else {
                "Location": f"https://api.fabric.microsoft.com/v1/operations/{OPERATION}/result",
            })
        if path == f"/v1/operations/{OPERATION}/result":
            return httpx.Response(200, json=self.pending)
        if request.method == "GET" and "/jobs/instances/" in path:
            self.job_pending -= 1
            return httpx.Response(200, json={
                "id": JOB, "itemId": IDS["hero-loader"], "jobType": "RunNotebook",
                "status": "InProgress" if self.job_pending >= 0 else "Completed", "failureReason": None,
            }, headers={"Retry-After": "0"})
        if request.method == "POST" and path.endswith("/jobs/RunNotebook/instances"):
            return httpx.Response(202, headers={
                "Location": f"https://api.fabric.microsoft.com{PREFIX}/items/{IDS['hero-loader']}/jobs/instances/{JOB}",
                "Retry-After": "0",
            })
        if path.endswith("/jobs/instances"):
            return httpx.Response(200, json={"value": []})
        if request.method == "GET" and path.startswith(PREFIX + "/items/"):
            return httpx.Response(200, json=next(item for item in self.items.values() if item["id"] == path.split("/")[-1]))
        if path.endswith("/getDefinition"):
            definition = self.bodies[path.split("/")[-2]]["definition"]
            if self.definition_changed:
                definition = dict(definition, parts=definition["parts"][:-1])
            return httpx.Response(200, json={"definition": definition})
        if request.url.host == "api.powerbi.com":
            if path.endswith("/executeQueries"):
                return httpx.Response(200, json=self.query_body)
            if request.method == "POST":
                return httpx.Response(202, headers={
                    **({"Location": str(request.url) + "/" + REFRESH} if self.refresh_location else {}),
                    "x-ms-request-id": REFRESH, "Retry-After": "0",
                })
            self.refresh_pending -= 1
            submitted = any(r.method == "POST" and r.url.path == path for r in self.sent)
            return httpx.Response(200, json={"value": [{
                "requestId": COMPETING, "status": "Completed", "refreshType": "ViaApi",
                "endTime": datetime.now(timezone.utc).isoformat(),
            }, *([{
                "requestId": REFRESH, "status": "Unknown" if self.refresh_pending >= 0 else "Completed",
                "refreshType": "ViaApi", "endTime": (
                    None if self.refresh_pending >= 0 else datetime.now(timezone.utc).isoformat()
                ),
            }] if submitted else [])]}, headers={"Retry-After": "0"})
        raise AssertionError("Unreviewed synthetic route: " + str(request.url))


def runner(private, scope_file, lease, service):
    def factory(scope, selected_lease):
        fabric_client = httpx.Client(base_url=FABRIC_RESOURCE, transport=httpx.MockTransport(service.handle))
        powerbi_client = httpx.Client(transport=httpx.MockTransport(service.handle))
        fabric = deploy.FabricTransport(
            selected_lease, fabric_client, principal=scope.principal, token_resource=FABRIC_RESOURCE,
            proof_level="SYNTHETIC_LOCAL",
        )
        refresh = PowerBIRefresh(
            selected_lease, powerbi_client, principal=scope.principal, token_resource=POWERBI_RESOURCE,
            proof_level="SYNTHETIC_LOCAL", sleep=lambda _: None,
        )
        return fabric, refresh

    return deploy.HeroRunner(private, scope_file, lease, transports=factory)


@pytest.fixture
def model_stage(tmp_path, packet_dir, monkeypatch):
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    lease, scope_file = configure(tmp_path, packet_dir)
    service = SyntheticService(packet_dir)
    initial = runner(tmp_path, scope_file, lease, service)
    assert initial.run()["status"] == "INITIAL_CREATE_READ_BACK"
    bound = tmp_path / "bound"
    assert deploy.bind_owned(tmp_path, scope_file, lease, bound)["status"] == "BOUND_LOCALLY_NOT_APPROVED"
    ownership = {
        "workspace": lease.approval.ownershipReceipts["workspace"],
        "hero-lakehouse": deploy.ownership_spec(tmp_path, lease, "hero-lakehouse"),
    }
    lease, scope_file = configure(tmp_path, packet_dir, phase="load", ownership=ownership, bound=bound)
    loader = runner(tmp_path, scope_file, lease, service)
    service.job_pending = 1
    assert loader.run()["status"] == "SOURCE_ASSERTIONS_REQUIRED"
    ownership["hero-loader"] = deploy.ownership_spec(tmp_path, lease, "hero-loader")
    accepted = next(path for path in lease.ledger.glob("*.json") if read_json(path)["operation"] == "run-loader")
    completed = next(path for path in tmp_path.glob("http-*.json") if (
        read_json(path)["method"] == "GET" and read_json(path)["path"].endswith("/jobs/instances/" + JOB)
        and read_json(path)["response"]["status"] == "Completed"
    ))
    proof = {"authorization": deploy.artifact(lease.path), "accepted": deploy.artifact(accepted),
             "completed": deploy.artifact(completed)}
    packet = read_json(packet_dir / "packet.json")
    assertions = save(tmp_path / "source-assertions.json", {
        "schemaVersion": "hero-source-assertions-v1", "proofLevel": "SYNTHETIC_LOCAL",
        "packetSha256": sha256((packet_dir / "packet.json").read_bytes()),
        "populationSha256": body_digest(packet["population"]), "lakehouseId": IDS["hero-lakehouse"],
        "loaderId": IDS["hero-loader"], "jobId": JOB, "jobReceiptSha256": proof["completed"]["sha256"],
        "notebookBodySha256": body_digest(read_json(bound / "notebook-template.json")),
        "observedAtUtc": datetime.now(timezone.utc).isoformat(),
        "tables": [{"name": table["name"], "rows": table["rows"], "expectedFileSha256": table["fileSha256"],
                    "exactDataReadback": True, "keyChecks": "PASSED", "missingExpectedRows": 0,
                    "unexpectedRows": 0} for table in packet["tables"]],
    })
    lease, scope_file = configure(
        tmp_path, packet_dir, phase="models", ownership=ownership, bound=bound, proof=proof, assertions=assertions,
    )
    ready = runner(tmp_path, scope_file, lease, service)
    return ready, service, scope_file


def test_all_three_full_part_load_refresh_query_completion_is_synthetic_only(model_stage):
    ready, service, _ = model_stage
    service.refresh_pending = 1
    receipt = ready.run()
    assert receipt["status"] == "THREE_SOURCE_QUERY_CONTROLS_PASSED"
    assert receipt["proofLevel"] == "SYNTHETIC_LOCAL" and receipt["liveExecution"] == "NOT_RUN"
    assert set(receipt["models"]) == set(deploy.MODELS)
    assert all(value["all64MeasuresEngineCertified"] is False for value in receipt["models"].values())
    assert service.polls == 2
    assert sum(r.method == "POST" and "/jobs/RunNotebook/instances" in r.url.path for r in service.sent) == 1
    assert sum(r.method == "POST" and r.url.path.endswith("/refreshes") for r in service.sent) == 3
    assert receipt["ontologyClock"] is None and receipt["contextCacheClock"] is None
    assert any(r.url.host == "api.fabric.microsoft.com" for r in service.sent)
    assert any(r.url.host == "api.powerbi.com" for r in service.sent)


@pytest.mark.parametrize("state,sku", [("Inactive", "F8"), ("Paused", "F8"), ("Active", "F16"), (None, "F8")])
def test_capacity_requires_actual_active_approved_sku_without_mutation(tmp_path, packet_dir, state, sku):
    lease, scope_file = configure(tmp_path, packet_dir)
    service = SyntheticService(packet_dir)
    service.capacity_state, service.capacity_sku = state, sku
    with pytest.raises(RuntimeError, match="capacity"):
        runner(tmp_path, scope_file, lease, service).run()
    assert len(service.sent) == 1 and service.sent[0].url.path == "/v1/capacities"
    assert service.sent[0].method == "GET"


@pytest.mark.parametrize("body", [
    {"error": {"code": "DAX failure"}},
    {"results": [{"error": {"message": "truncated"}, "tables": [{"rows": [{"[Value]": 7}]}]}]},
    {"results": [{"tables": [{"rows": [{"[Value]": 7}], "error": {"code": "limit"}}]}]},
    {"results": [{"tables": [{"rows": [{"[Value]": 7}]}]}], "partial": True},
    {"results": [{"tables": [{"rows": None}]}]},
    {"results": [{"tables": [{"rows": [{"[Value]": 7}]}, {"rows": []}]}]},
])
def test_http200_query_error_partial_unknown_never_completes_model(model_stage, body):
    ready, service, _ = model_stage
    service.query_body = body
    with pytest.raises(RuntimeError, match="Query|query"):
        ready.run()
    assert not any(read_json(path)["status"] == "THREE_SOURCE_QUERY_CONTROLS_PASSED"
                   for path in ready.private.glob("hero-stage-*.json"))


@pytest.mark.parametrize("mutation", [
    lambda receipt: receipt["tables"][0].update(rows=99999),
    lambda receipt: receipt["tables"][0].update(keyChecks="UNKNOWN"),
    lambda receipt: receipt["tables"][0].update(missingExpectedRows=1),
    lambda receipt: receipt.update(jobId=COMPETING),
    lambda receipt: receipt.update(populationSha256="0" * 64),
])
def test_source_gate_rejects_counts_keys_expected_data_job_or_lineage(model_stage, mutation):
    ready, service, _ = model_stage
    path = Path(ready.scope.sourceAssertions.file)
    value = read_json(path)
    mutation(value)
    save(path, value)
    before = len(service.sent)
    with pytest.raises(RuntimeError, match="artifact changed"):
        ready.run()
    assert len(service.sent) == before


@pytest.mark.parametrize("field,value", [
    ("coverage", "UNKNOWN"), ("coverage", "PARTIAL"), ("homeTable", "Invented Home"),
])
def test_unknown_coverage_and_fabricated_measure_home_fail_before_transport(model_stage, field, value):
    ready, service, scope_file = model_stage
    scope = read_json(scope_file)
    scope["controls"][0][field] = value
    save(scope_file, scope)
    before = len(service.sent)
    with pytest.raises(RuntimeError, match="artifact changed"):
        ready.run()
    assert len(service.sent) == before


@pytest.mark.parametrize("location", [
    "https://evil.invalid/v1/operations/" + OPERATION,
    "https://user@api.fabric.microsoft.com/v1/operations/" + OPERATION,
    "http://api.fabric.microsoft.com/v1/operations/" + OPERATION,
    "https://api.fabric.microsoft.com/v1/operations/" + COMPETING,
    "https://api.fabric.microsoft.com/v1/operations/" + OPERATION + "/result",
])
def test_malicious_accepted_location_persists_then_never_polls_or_reposts(tmp_path, packet_dir, location):
    lease, scope_file = configure(tmp_path, packet_dir)
    service = SyntheticService(packet_dir)
    service.async_alias = "hero-lakehouse"
    service.location_override = location
    ready = runner(tmp_path, scope_file, lease, service)
    with pytest.raises(RuntimeError):
        ready.run()
    record = next(iter(lease._records()))
    assert record["state"] == "ACCEPTED" and record["location"] == location
    with pytest.raises(RuntimeError, match="unknown|rejected"):
        ready.run()
    assert sum(r.method == "POST" for r in service.sent) == 1
    assert all("/v1/operations/" not in r.url.path for r in service.sent)


def test_reservation_precedes_exception_survives_restart_and_changed_approval(tmp_path, packet_dir):
    lease, scope_file = configure(tmp_path, packet_dir)
    service = SyntheticService(packet_dir)
    service.fail_post = True
    ready = runner(tmp_path, scope_file, lease, service)
    with pytest.raises(httpx.ReadTimeout):
        ready.run()
    record = next(iter(lease._records()))
    assert record["state"] == "RESERVED_OUTCOME_UNKNOWN"
    reopened = Renewal(tmp_path, lease.path)
    duplicate = runner(tmp_path, scope_file, reopened, service)
    with pytest.raises(RuntimeError, match="unknown"):
        duplicate.run()
    assert sum(r.method == "POST" for r in service.sent) == 1
    # A separately frozen new authorization digest cannot erase that reservation.
    approval = read_json(lease.path)
    approval["confirmationReference"] = "SYNTHETIC renewed approval"
    new_path = tmp_path / "new-approval.json"
    save(new_path, approval)
    stage_execution_window(new_path, tmp_path)
    # Reuse the exact matching fresh shutdown readback, with its new frozen deadline.
    window = read_json(next(path for path in tmp_path.glob("renewal-window-*.json")
                            if read_json(path)["approvalSha256"] == body_digest(approval)))
    guard = read_json(tmp_path / approval["shutdown"]["receiptFile"])
    guard["schedule"]["properties"]["startTime"] = window["deadlineUtc"]
    guard["schedule"]["properties"]["expiryTime"] = (
        datetime.fromisoformat(window["deadlineUtc"]) + timedelta(seconds=7200)
    ).isoformat()
    guard["binding"]["properties"]["parameters"]["DeadlineUtc"] = window["deadlineUtc"]
    guard["observedAtUtc"] = datetime.now(timezone.utc).isoformat()
    save(tmp_path / approval["shutdown"]["receiptFile"], guard)
    renewed = Renewal(tmp_path, new_path)
    with pytest.raises(RuntimeError, match="already attempted"):
        runner(tmp_path, scope_file, renewed, service).run()
    assert sum(r.method == "POST" for r in service.sent) == 1


def test_name_collision_and_protected_returned_id_never_grant_ownership(tmp_path, packet_dir):
    lease, scope_file = configure(tmp_path, packet_dir)
    service = SyntheticService(packet_dir)
    ready = runner(tmp_path, scope_file, lease, service)
    service.collision = True
    with pytest.raises(RuntimeError, match="collision"):
        ready.run()
    assert all(r.method == "GET" for r in service.sent)
    service.collision = False
    service.returned_id = PROTECTED
    with pytest.raises(RuntimeError, match="existing/protected"):
        ready.run()
    assert not lease._derived()[0]


def test_lro_succeeded_without_exposed_result_is_not_ownership(tmp_path, packet_dir, monkeypatch):
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    lease, scope_file = configure(tmp_path, packet_dir)
    service = SyntheticService(packet_dir)
    service.async_alias, service.no_result = "hero-lakehouse", True
    ready = runner(tmp_path, scope_file, lease, service)
    with pytest.raises(RuntimeError, match="without an exposed result"):
        ready.run()
    assert not lease._derived()[0]
    assert sum(r.method == "POST" for r in service.sent) == 1


def test_missing_accepted_refresh_location_never_adopts_competing_history(model_stage):
    ready, service, _ = model_stage
    service.refresh_location = False
    with pytest.raises(RuntimeError, match="Location"):
        ready.run()
    state = read_json(ready.private / "hero-enterprise-refresh-accepted.json")
    assert state["status"] == 202 and state["location"] is None
    before = sum(r.method == "POST" and r.url.path.endswith("/refreshes") for r in service.sent)
    with pytest.raises(RuntimeError, match="Location"):
        ready.run()
    assert sum(r.method == "POST" and r.url.path.endswith("/refreshes") for r in service.sent) == before == 1


def test_changed_definition_readback_never_refreshes_or_accepts(model_stage):
    ready, service, _ = model_stage
    service.definition_changed = True
    with pytest.raises(RuntimeError, match="definition"):
        ready.run()
    assert not any(r.url.host == "api.powerbi.com" for r in service.sent)


@pytest.mark.parametrize("body", [
    {"notifyOption": "NoNotification"},
    {"type": "full", "commitMode": "transactional", "retryCount": 0, "timeout": "00:05:00"},
])
def test_ordinary_or_enhanced_refresh_body_contract(body):
    assert refresh_body(body) == body


@pytest.mark.parametrize("body", [
    {}, {"notifyOption": "NoNotification", "type": "full"},
    {"type": "full", "commitMode": "partialBatch", "retryCount": 0, "timeout": "00:05:00"},
])
def test_unreviewed_refresh_body_fails(body):
    with pytest.raises(RuntimeError):
        refresh_body(body)


def test_query_decoder_complete_shape_only():
    assert query_rows(httpx.Response(200, json={"results": [{"tables": [{"rows": [{"[Value]": 0}]}]}]})) == [
        {"[Value]": 0},
    ]


def test_unapproved_and_private_boundary_fail_without_author_or_transport(tmp_path, packet_dir, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Dry-run attempted Author authentication")

    monkeypatch.setattr(Author, "__init__", forbidden)
    output = tmp_path / "plan"
    deploy.plan(packet_dir, output=output)
    with pytest.raises(RuntimeError, match="confirmation absent"):
        Renewal(tmp_path, output / "hero-approval-template.json")
    with pytest.raises(RuntimeError, match="outside source"):
        Renewal(ROOT / ".local", output / "hero-approval-template.json")
    lease, scope_file = configure(tmp_path, packet_dir)
    deploy.main(["dry-run", "--private-dir", str(tmp_path), "--approval", str(lease.path), "--scope", str(scope_file)])


def reapprove(ready, *, alter_scope=None, alter_approval=None, dependency_files=None):
    return configure(
        ready.private, ready.packet_dir, phase="models", ownership=ready.lease.approval.ownershipReceipts,
        bound=ready.bound["directory"], proof=ready.scope.loadProof.model_dump(),
        assertions=ready.scope.sourceAssertions.model_dump(), alter_scope=alter_scope, alter_approval=alter_approval,
        dependency_files=dependency_files,
    )


@pytest.mark.parametrize("change", [
    lambda scope: scope["controls"][0].update(coverage="UNKNOWN"),
    lambda scope: scope["controls"][0].update(coverage="PARTIAL"),
    lambda scope: scope["controls"][0].update(homeTable="Made Up"),
    lambda scope: scope["controls"][0].update(measure="Made Up"),
    lambda scope: scope.update(populationSha256="0" * 64),
    lambda scope: scope["principal"].update(authorUser="ambient@invalid.test"),
])
def test_even_new_approved_scope_cannot_weaken_semantic_or_identity_gates(model_stage, change):
    ready, service, _ = model_stage
    lease, scope_file = reapprove(ready, alter_scope=change)
    before = len(service.sent)
    with pytest.raises(RuntimeError):
        runner(ready.private, scope_file, lease, service)
    assert len(service.sent) == before


@pytest.mark.parametrize("change", [
    lambda receipt: receipt["tables"][0].update(rows=99999),
    lambda receipt: receipt["tables"][0].update(keyChecks="UNKNOWN"),
    lambda receipt: receipt["tables"][0].update(exactDataReadback=None),
    lambda receipt: receipt["tables"][0].update(missingExpectedRows=1),
    lambda receipt: receipt["tables"][0].update(unexpectedRows=False),
    lambda receipt: receipt.update(jobId=COMPETING),
])
def test_reviewed_but_incomplete_source_assertions_still_fail_one_completion_gate(model_stage, change):
    ready, service, _ = model_stage
    assertion_file = Path(ready.scope.sourceAssertions.file)
    receipt = read_json(assertion_file)
    change(receipt)
    save(assertion_file, receipt)
    ready.scope.sourceAssertions = deploy.Artifact.model_validate(deploy.artifact(assertion_file))
    lease, scope_file = reapprove(ready)
    candidate = runner(ready.private, scope_file, lease, service)
    before = sum(request.method == "POST" for request in service.sent)
    with pytest.raises(RuntimeError, match="Source assertion"):
        candidate.run()
    assert sum(request.method == "POST" for request in service.sent) == before


@pytest.mark.parametrize("kind", ["fabric", "powerbi"])
def test_wrong_audience_and_actual_selected_principal_fail_without_transport(tmp_path, packet_dir, kind):
    lease, scope_file = configure(tmp_path, packet_dir)
    scope = deploy.HeroScope.model_validate(read_json(scope_file))
    client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Wrong binding was dispatched")))
    factory = deploy.FabricTransport if kind == "fabric" else PowerBIRefresh
    with pytest.raises(RuntimeError, match="audience"):
        factory(lease, client, principal=scope.principal,
                token_resource=POWERBI_RESOURCE if kind == "fabric" else FABRIC_RESOURCE,
                proof_level="SYNTHETIC_LOCAL")
    wrong = scope.principal.model_copy(update={"authorUser": "not-selected@invalid.test"})
    with pytest.raises(RuntimeError, match="principal"):
        factory(lease, client, principal=wrong,
                token_resource=FABRIC_RESOURCE if kind == "fabric" else POWERBI_RESOURCE,
                proof_level="SYNTHETIC_LOCAL")


@pytest.mark.parametrize("target", [
    "https://user@api.powerbi.com/v1.0/myorg/groups/" + WORKSPACE + "/datasets/" + IDS["enterprise"] + "/refreshes",
    "http://api.powerbi.com/v1.0/myorg/groups/" + WORKSPACE + "/datasets/" + IDS["enterprise"] + "/refreshes",
    "https://evil.invalid/v1.0/myorg/groups/" + WORKSPACE + "/datasets/" + IDS["enterprise"] + "/refreshes",
    f"/v1.0/myorg/groups/{COMPETING}/datasets/{IDS['enterprise']}/refreshes",
    f"/v1.0/myorg/groups/{WORKSPACE}/datasets/{COMPETING}/refreshes",
    f"/v1.0/myorg/groups/{WORKSPACE}/datasets/{IDS['enterprise']}/../refreshes",
])
def test_powerbi_host_group_dataset_escapes_never_dispatch(model_stage, target):
    ready, service, _ = model_stage
    before = len(service.sent)
    with pytest.raises(RuntimeError):
        ready.refresh.request("POST", target, body={"notifyOption": "NoNotification"})
    assert len(service.sent) == before


def test_renewal_budget_and_expiry_stop_every_new_request(model_stage, monkeypatch):
    ready, service, _ = model_stage
    ready.lease._budget = lambda: (_ for _ in ()).throw(RuntimeError("Approved request budget exhausted"))
    before = len(service.sent)
    with pytest.raises(RuntimeError, match="budget"):
        ready.run()
    assert len(service.sent) == before
    from infra import renewal

    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return ready.lease.deadline + timedelta(seconds=1)

    monkeypatch.setattr(renewal, "datetime", Expired)
    with pytest.raises(RuntimeError, match="expired"):
        ready.run()
    assert len(service.sent) == before


def test_executed_runner_code_is_frozen_before_every_transport(model_stage, monkeypatch):
    ready, service, _ = model_stage
    original = Path.read_bytes
    target = ROOT / "infra" / "hero_deploy.py"

    def changed(path):
        raw = original(path)
        return raw + b"\n" if path == target else raw

    monkeypatch.setattr(Path, "read_bytes", changed)
    before = len(service.sent)
    with pytest.raises(RuntimeError, match="artifact changed"):
        ready.run()
    assert len(service.sent) == before


def test_returned_id_cannot_be_fabricated_after_transport(model_stage, monkeypatch):
    ready, service, _ = model_stage
    original = ready.fabric.completed

    def forged(response, **kwargs):
        body = original(response, **kwargs)
        return dict(body, id=COMPETING) if isinstance(body, dict) and body.get("type") == "SemanticModel" else body

    monkeypatch.setattr(ready.fabric, "completed", forged)
    with pytest.raises(RuntimeError, match="Fabricated"):
        ready.run()
    assert not any(request.url.host == "api.powerbi.com" for request in service.sent)


def test_plan_dryrun_and_initial_binding_never_call_author_auth(tmp_path, packet_dir, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Local coordinator invoked Author login")

    monkeypatch.setattr(Author, "__init__", forbidden)
    lease, scope_file = configure(tmp_path, packet_dir)
    service = SyntheticService(packet_dir)
    ready = runner(tmp_path, scope_file, lease, service)
    ready.run()
    output = tmp_path / "bound"
    bound = deploy.bind_owned(tmp_path, scope_file, lease, output)
    assert bound["approved"] is False and bound["status"] == "BOUND_LOCALLY_NOT_APPROVED"
    prior = len(service.sent)
    with pytest.raises(RuntimeError, match="allowlist"):
        ready.fabric.request("POST", PREFIX + "/items", json=read_json(output / "notebook-template.json"))
    assert len(service.sent) == prior
    assert read_json(output / "hero-binding.json")["populationSha256"] == ready.scope.populationSha256


def test_unaccepted_dependency_never_reaches_delayed_transport_factory(tmp_path, packet_dir):
    save(tmp_path / "TQ-004.json", {"status": "REVIEW_PENDING"})
    lease, scope_file = configure(tmp_path, packet_dir)
    with pytest.raises(RuntimeError, match="dependency not accepted"):
        deploy.HeroRunner(
            tmp_path, scope_file, lease,
            transports=lambda *_: pytest.fail("Blocked dependency constructed clients/auth"),
        )


def test_actual_request_budget_reserves_before_transport_and_cannot_restart(tmp_path, packet_dir):
    lease, scope_file = configure(tmp_path, packet_dir, alter_approval=lambda value: value.update(maximumRequests=3))
    service = SyntheticService(packet_dir)
    with pytest.raises(RuntimeError, match="budget"):
        runner(tmp_path, scope_file, lease, service).run()
    assert len(service.sent) == 3 and all(request.method == "GET" for request in service.sent)
    assert lease._records()[0]["state"] == "RESERVED_OUTCOME_UNKNOWN"
    with pytest.raises(RuntimeError, match="budget"):
        runner(tmp_path, scope_file, Renewal(tmp_path, lease.path), service).run()
    assert len(service.sent) == 3


def test_lro_readonly_recovery_never_repeats_the_create(tmp_path, packet_dir, monkeypatch):
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    lease, scope_file = configure(tmp_path, packet_dir)
    service = SyntheticService(packet_dir)
    service.async_alias, service.no_result = "hero-lakehouse", True
    with pytest.raises(RuntimeError, match="without an exposed result"):
        runner(tmp_path, scope_file, lease, service).run()
    service.no_result = False
    receipt = runner(tmp_path, scope_file, Renewal(tmp_path, lease.path), service).run()
    assert receipt["status"] == "INITIAL_CREATE_READ_BACK"
    assert sum(request.method == "POST" for request in service.sent) == 1
    assert lease._records()[0]["readbackVerified"] is True


def test_expiry_during_accepted_lro_does_not_poll_or_post_again(tmp_path, packet_dir, monkeypatch):
    from infra import renewal

    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    lease, scope_file = configure(tmp_path, packet_dir)
    service = SyntheticService(packet_dir)
    service.async_alias = "hero-lakehouse"
    original = service.handle

    class Expired(datetime):
        @classmethod
        def now(cls, tz=None):
            return lease.deadline + timedelta(seconds=1)

    def expire(request):
        response = original(request)
        if request.url.path == "/v1/operations/" + OPERATION:
            monkeypatch.setattr(renewal, "datetime", Expired)
        return response

    service.handle = expire
    with pytest.raises(RuntimeError, match="expired"):
        runner(tmp_path, scope_file, lease, service).run()
    assert service.polls == 1 and sum(request.method == "POST" for request in service.sent) == 1


def test_unknown_refresh_submission_is_reserved_before_dispatch_and_not_retried(model_stage):
    ready, service, _ = model_stage
    original = ready.refresh.client.request
    posts = []

    def lost(method, url, **kwargs):
        if method == "POST" and url.endswith("/refreshes"):
            assert any(record["operation"] == "refresh-enterprise"
                       and record["state"] == "RESERVED_OUTCOME_UNKNOWN" for record in ready.lease._records())
            posts.append(url)
            raise httpx.ReadTimeout("SYNTHETIC lost refresh acceptance")
        return original(method, url, **kwargs)

    ready.refresh.client.request = lost
    with pytest.raises(httpx.ReadTimeout):
        ready.run()
    with pytest.raises(RuntimeError, match="already attempted"):
        ready.run()
    assert len(posts) == 1


@pytest.mark.parametrize("field,value", [
    ("[RowsUnknown]", 1), ("[RowsUnknown]", None), ("[RowsInvalid]", 1), ("[Value]", None),
])
def test_http200_diagnostics_or_unknown_value_cannot_be_complete(model_stage, field, value):
    ready, service, _ = model_stage
    service.query_body["results"][0]["tables"][0]["rows"][0][field] = value
    with pytest.raises(RuntimeError, match="unknown/invalid/null"):
        ready.run()


def test_refresh_poll_budget_is_bounded_without_resubmission(model_stage):
    ready, service, _ = model_stage
    service.refresh_pending = 100
    with pytest.raises(TimeoutError, match="pending"):
        ready.run()
    assert sum(request.method == "POST" and request.url.path.endswith("/refreshes")
               for request in service.sent) == 1
    receipts = [read_json(path) for path in ready.private.glob("hero-powerbi-http-*.json")]
    assert receipts
    assert all(datetime.fromisoformat(receipt["startedAt"]) <= datetime.fromisoformat(receipt["observedAtUtc"])
               for receipt in receipts)


def test_p0_only_cli_choices_are_preserved():
    source = (ROOT / "infra" / "fabric_p0.py").read_text(encoding="utf-8")
    refresh = (ROOT / "infra" / "refresh_p0_model.py").read_text(encoding="utf-8")
    assert 'choices=["subscriptions", "finance"]' in source
    assert 'choices=["subscriptions", "finance"]' in refresh
    assert "--model network" not in source


@pytest.mark.parametrize("origin", ["", "https://api.powerbi.com",
                                  "https://wabi-us-central-b-primary-redirect.analysis.windows.net"])
def test_refresh_location_keeps_reviewed_regional_origin_as_evidence_not_dispatch_target(origin):
    from infra.powerbi_refresh import canonical_refresh_location
    group, dataset, operation = [f"00000000-0000-0000-0000-{n:012d}" for n in (201, 202, 203)]
    path = PowerBIRefresh.base(group, dataset)
    receipt = {"method": "POST", "status": 202, "path": path, "location": origin + path + "/" + operation}
    request_id, canonical = canonical_refresh_location(
        receipt, group, dataset,
        reviewed_regional_origin="https://wabi-us-central-b-primary-redirect.analysis.windows.net",
    )
    assert request_id == operation and canonical == path + "/" + operation
    assert ("https://api.powerbi.com" + canonical).startswith("https://api.powerbi.com/")
    assert receipt["location"] == origin + path + "/" + operation


@pytest.mark.parametrize("change", [
    "unreviewed-region", "foreign-host", "userinfo", "http", "port", "query", "fragment",
    "different-model", "different-operation", "missing-location", "wrong-request-id",
])
def test_refresh_location_rejects_unreviewed_or_unlinked_regional_locations(change):
    from infra.powerbi_refresh import canonical_refresh_location
    group, dataset, operation = [f"00000000-0000-0000-0000-{n:012d}" for n in (201, 202, 203)]
    path = PowerBIRefresh.base(group, dataset)
    origin = "https://wabi-us-central-b-primary-redirect.analysis.windows.net"
    receipt = {"method": "POST", "status": 202, "path": path, "location": origin + path + "/" + operation}
    if change == "unreviewed-region":
        receipt["location"] = receipt["location"].replace("-b-", "-c-")
    elif change == "foreign-host":
        receipt["location"] = "https://unreviewed.invalid" + path + "/" + operation
    elif change == "userinfo":
        receipt["location"] = "https://synthetic@api.powerbi.com" + path + "/" + operation
    elif change == "http":
        receipt["location"] = receipt["location"].replace("https:", "http:")
    elif change == "port":
        receipt["location"] = origin + ":443" + path + "/" + operation
    elif change == "query":
        receipt["location"] += "?unreviewed=true"
    elif change == "fragment":
        receipt["location"] += "#unreviewed"
    elif change == "different-model":
        receipt["location"] = receipt["location"].replace(dataset, "00000000-0000-0000-0000-000000000299")
    elif change == "different-operation":
        receipt["location"] = receipt["location"].rsplit("/", 1)[0] + "/not-a-request-id"
    elif change == "missing-location":
        receipt["location"] = None
    else:
        receipt["requestId"] = "00000000-0000-0000-0000-000000000299"
    with pytest.raises((RuntimeError, ValueError)):
        canonical_refresh_location(receipt, group, dataset, reviewed_regional_origin=origin)


def test_private_ledger_and_output_paths_cannot_escape_before_transport(tmp_path, packet_dir):
    lease, scope_file = configure(tmp_path, packet_dir)
    service = SyntheticService(packet_dir)
    ready = runner(tmp_path, scope_file, lease, service)
    with pytest.raises(RuntimeError, match="escaped"):
        ready.fabric.save("..\\escaped.json", {"must": "never write"})
    assert not (tmp_path.parent / "escaped.json").exists()
    from infra.renewal import local_file
    with pytest.raises(RuntimeError, match="inside its private"):
        local_file(tmp_path, "..\\renewal-requests")
    assert service.sent == []


def test_approved_companion_hash_cannot_replace_actual_packet_root(tmp_path, packet_dir):
    lease, scope_file = configure(
        tmp_path, packet_dir,
        alter_scope=lambda scope: scope.update(packet=deploy.artifact(packet_dir / "source-manifest.json")),
    )
    with pytest.raises(RuntimeError, match="actual packet.json root"):
        deploy.HeroRunner(
            tmp_path, scope_file, lease,
            transports=lambda *_: pytest.fail("Wrong packet root constructed a client"),
        )


def real_dependency_files():
    return {task: ROOT / ".copilot" / "evidence" / (task + "-acceptance.json")
            for task in ("TQ-003", "TQ-004", "TQ-005")}


def test_review_real_dependency_formats_support_initial_dryrun_and_binding(tmp_path, packet_dir, capsys):
    files = real_dependency_files()
    receipts = {task: read_json(path) for task, path in files.items()}
    assert receipts["TQ-003"]["task"] == "TQ-003"
    assert receipts["TQ-004"]["acceptanceStatus"] == "ACCEPTED_LOCAL_ONLY"
    assert receipts["TQ-005"]["task_id"] == "TQ-005"
    assert receipts["TQ-005"]["acceptance_status"] == "ACCEPTED_LOCAL_ONLY"
    assert "status" not in receipts["TQ-005"]
    lease, scope_file = configure(tmp_path, packet_dir, dependency_files=files)
    scope = read_json(scope_file)
    assert scope["dependencies"] == {task: deploy.artifact(path) for task, path in files.items()}
    deploy.main(["dry-run", "--private-dir", str(tmp_path), "--approval", str(lease.path), "--scope", str(scope_file)])
    assert "VALIDATED_DRY_RUN_ZERO_TRANSPORT" in capsys.readouterr().out
    service = SyntheticService(packet_dir)
    runner(tmp_path, scope_file, lease, service).run()
    bound = tmp_path / "real-dependencies-bound"
    assert deploy.bind_owned(tmp_path, scope_file, lease, bound)["status"] == "BOUND_LOCALLY_NOT_APPROVED"
    ownership = {"workspace": lease.approval.ownershipReceipts["workspace"],
                 "hero-lakehouse": deploy.ownership_spec(tmp_path, lease, "hero-lakehouse")}
    lease, scope_file = configure(
        tmp_path, packet_dir, phase="load", ownership=ownership, bound=bound, dependency_files=files,
    )
    deploy.main(["dry-run", "--private-dir", str(tmp_path), "--approval", str(lease.path), "--scope", str(scope_file)])
    assert "VALIDATED_DRY_RUN_ZERO_TRANSPORT" in capsys.readouterr().out


def test_review_real_dependency_formats_support_models_dryrun(model_stage, capsys):
    ready, service, _ = model_stage
    before = len(service.sent)
    lease, scope_file = reapprove(ready, dependency_files=real_dependency_files())
    deploy.main(["dry-run", "--private-dir", str(ready.private), "--approval", str(lease.path),
                 "--scope", str(scope_file)])
    assert "VALIDATED_DRY_RUN_ZERO_TRANSPORT" in capsys.readouterr().out
    assert len(service.sent) == before


@pytest.mark.parametrize("change", [
    lambda receipt: receipt.update(task="TQ-003"),
    lambda receipt: receipt.update(task_id="TQ-004"),
    lambda receipt: receipt.update(status="REVIEW_PENDING"),
    lambda receipt: receipt.update(acceptanceStatus="IN_PROGRESS"),
    lambda receipt: receipt.update(acceptance_status=None),
    lambda receipt: receipt.update(accepted=False),
    lambda receipt: receipt.clear(),
])
def test_review_conflicting_dependency_identity_or_acceptance_never_constructs_clients(
    tmp_path, packet_dir, change,
):
    files = real_dependency_files()
    receipt = read_json(files["TQ-005"])
    change(receipt)
    save(tmp_path / "unaccepted-wrapper.json", receipt)
    files["TQ-005"] = tmp_path / "unaccepted-wrapper.json"
    lease, scope_file = configure(tmp_path, packet_dir, dependency_files=files)
    with pytest.raises(RuntimeError, match="dependency not accepted"):
        deploy.HeroRunner(
            tmp_path, scope_file, lease,
            transports=lambda *_: pytest.fail("Conflicting dependency reached transport factory"),
        )
