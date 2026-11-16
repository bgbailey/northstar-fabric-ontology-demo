"""Approval-bound hero stages; plan/dry-run/bind never authenticate.

execute requires a fresh immutable scope, pinned private CLI configuration and
explicit --stage initial|load|models. It cannot start/resize/reassign capacity,
approve bound bytes, generate an ontology, import invented assertions or clean up.
"""

from __future__ import annotations

import argparse
import ast
import base64
import copy
import io
import json
import re
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol

import httpx
from pydantic import Field

from infra.fabric_p0 import Author
from infra.powerbi_refresh import (
    FABRIC_RESOURCE,
    HttpClient,
    PowerBIRefresh,
    SelectedPrincipal,
    query_rows,
    refresh_body,
    validate_principal,
)
from infra.renewal import (
    Artifact,
    LroPolling,
    Ownership,
    Record,
    Renewal,
    body_digest,
    fabric_path,
    local_file,
    private_path,
    read_json,
    sha256,
    utc,
    verified_ownership,
)
from tools import build_hero_packet as builder

ROOT = Path(__file__).resolve().parents[1]
MODELS = ("enterprise", "finance", "network")
CODE = (
    "infra/hero_deploy.py", "infra/powerbi_refresh.py", "infra/fabric_p0.py",
    "infra/renewal.py", "infra/live_transports.py", "infra/stage_renewal.py", "infra/refresh_p0_model.py",
    "tools/build_hero_packet.py", "tools/build_p0_packet.py",
)


def write_new(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write("\n")


def artifact(path: Path) -> dict:
    return {"file": str(path.resolve()), "sha256": sha256(path.read_bytes())}


def parts(body: dict) -> dict[str, bytes]:
    entries = body["definition"]["parts"]
    result = {}
    for entry in entries:
        name = entry["path"]
        if (
            name in result or name.startswith(("/", "\\")) or "\\" in name
            or any(piece in {"", ".", ".."} for piece in name.split("/"))
            or entry["payloadType"] != "InlineBase64"
        ):
            raise RuntimeError("Noncanonical/duplicate definition part")
        result[name] = base64.b64decode(entry["payload"], validate=True)
    return result


@lru_cache(maxsize=64)
def expected_keys(raw: bytes, keys: tuple[str, ...], expected_rows: int) -> None:
    # Cache only immutable source bytes/structure, never authority or source clocks.
    table = builder.pq.read_table(io.BytesIO(raw))
    key_rows = [tuple(row[key] for key in keys) for row in table.to_pylist()]
    if table.num_rows != expected_rows or any(None in row for row in key_rows) or (
        len(set(key_rows)) != len(key_rows)
    ):
        raise RuntimeError("Expected source row/key checks failed")


def packet_contract(packet_dir: Path) -> dict:
    packet_dir = packet_dir.resolve()
    packet = builder.read_packet(packet_dir)  # Rehash ALL actual assets on every call.
    key = (sha256((packet_dir / "packet.json").read_bytes()), tuple(
        (entry["file"], entry["sha256"], entry["bytes"]) for entry in packet["packetFiles"]
    ))
    return copy.deepcopy(_packet_contract(packet_dir, key))


@lru_cache(maxsize=4)
def _packet_contract(packet_dir: Path, content_key: tuple) -> dict:
    packet = builder.read_packet(packet_dir)
    if (
        packet["models"] != list(MODELS) or packet["profile"] != "showcase-v2"
        or packet.get("population", {}).get("generatorRevision") != "showcase-v2.3"
        or packet["binding"]["status"] != "UNRESOLVED_PRIVATE_STEP"
    ):
        raise RuntimeError("Only one unbound showcase-v2.3 Network/Enterprise/Finance population is supported")
    files = [item["file"] for item in packet["packetFiles"]]
    if len(files) != len(set(files)) or any(Path(name).name != name for name in files):
        raise RuntimeError("Packet inventory must have unique local filenames")
    manifest = read_json(packet_dir / "source-manifest.json")
    catalog = read_json(packet_dir / "source-catalog.json")
    if (
        manifest["population"] != packet["population"] or manifest["version"] != "showcase-v2.3"
        or sha256((packet_dir / "source-manifest.json").read_bytes()) != packet["sourceManifestSha256"]
        or manifest["content_sha256"] != packet["sourceContentSha256"]
        or manifest["clocks"]["data"] != packet["dataClocks"]
        or manifest["generator_sha256"] != packet["generatorSha256"]
        or sha256(builder.canonical(manifest)) != packet["structuralManifestHash"]
        or sorted(model["id"] for model in catalog["models"]) != list(MODELS)
    ):
        raise RuntimeError("Packet population/manifest/source clocks differ")
    metadata = {table["id"]: table for table in manifest["tables"]}
    for table in packet["tables"]:
        source = metadata[table["name"]]
        if (table["rows"], table["fileSha256"], table["contentSha256"]) != (
            source["rows"], source["file_sha256"], source["content_sha256"],
        ):
            raise RuntimeError("Packet table is from another source revision")
    if len({t["name"] for t in packet["tables"]}) != len(packet["tables"]) or packet["rows"] != sum(
        t["rows"] for t in packet["tables"]
    ):
        raise RuntimeError("Packet table census differs")
    templates = {entry["alias"]: entry for entry in packet["modelPackets"]}
    if set(templates) != set(MODELS):
        raise RuntimeError("Exact three model templates required")
    inventories = []
    for model in MODELS:
        spec = templates[model]
        raw = (packet_dir / spec["file"]).read_bytes()
        body = read_json(packet_dir / spec["file"])
        decoded = parts(body)
        expected = {
            item["path"]: item["sha256"] for item in packet["sourceModelParts"] if item["model"] == model
        }
        if (
            sha256(raw) != spec["sha256"] or body["displayName"] != spec["displayName"]
            or body["definition"]["format"] != "TMDL" or len(decoded) != spec["parts"]
            or {name: sha256(raw) for name, raw in decoded.items()} != expected
            or not {"definition.pbism", "definition/database.tmdl", "definition/model.tmdl",
                    "definition/expressions.tmdl"} <= decoded.keys()
            or json.loads(decoded["definition.pbism"])["version"] != "4.2"
            or not re.search(rb"compatibilityLevel:\s*1702\b", decoded["definition/database.tmdl"])
            or not re.search(rb"compatibilityMode:\s*powerBI\b", decoded["definition/database.tmdl"])
        ):
            raise RuntimeError("Incomplete/changed source definition or compatibility settings")
        declared = next(item for item in catalog["models"] if item["id"] == model)
        if packet["modelDependencies"][model] != declared["tables"]:
            raise RuntimeError("Source model/table dependency contract differs")
        metrics = [item for item in catalog["metrics"] if item["model"] == model]
        if len(metrics) != 8:
            raise RuntimeError("Eight unchanged source measures per model required")
        inventories.extend({"model": model, "path": name, "sha256": sha256(raw)}
                           for name, raw in decoded.items())
    if sha256(builder.canonical({
        "sourceManifestSha256": packet["sourceManifestSha256"], "catalogSha256": packet["catalogSha256"],
        "models": packet["sourceModelParts"],
    })) != packet["packetRevision"]:
        raise RuntimeError("Packet revision digest differs")
    notebook = read_json(packet_dir / "loader.ipynb")
    template = read_json(packet_dir / "notebook-template.json")
    if parts(template) != {"notebook-content.ipynb": (packet_dir / "loader.ipynb").read_bytes()}:
        raise RuntimeError("Complete notebook differs from template")
    statement = ast.parse("".join(notebook["cells"][0]["source"])).body[1]
    assets = json.loads(ast.literal_eval(statement.value.args[0]))
    if {item["table"] for item in assets} != {item["name"] for item in packet["tables"]}:
        raise RuntimeError("Embedded loader table membership differs")
    for item in assets:
        raw = base64.b64decode(item["payload"], validate=True)
        expected = next(table for table in packet["tables"] if table["name"] == item["table"])
        if sha256(raw) != expected["fileSha256"] or item["rows"] != expected["rows"]:
            raise RuntimeError("Embedded source bytes/counts differ")
        declared = next(table for table in catalog["tables"] if table["id"] == item["table"])
        expected_keys(raw, tuple(declared["primaryKey"]), expected["rows"])
    return packet


def plan(packet_dir: Path, *, output: Path | None = None) -> dict:
    """Pure local validation: no Author, client, subprocess, socket or credentials."""
    if output is not None and output.exists():
        raise FileExistsError("Plan output exists; never overwrite an earlier stage")
    packet_dir = packet_dir.resolve()
    packet = packet_contract(packet_dir)
    report = {
        "schemaVersion": "hero-deployment-plan-v1", "status": "VALIDATED_LOCAL_PLAN_NOT_APPROVED",
        "approved": False, "proofLevel": "LOCAL_STATIC", "liveExecution": "NOT_RUN",
        "packetSha256": sha256((packet_dir / "packet.json").read_bytes()),
        "packetRevision": packet["packetRevision"], "populationSha256": body_digest(packet["population"]),
        "models": list(MODELS), "rows": packet["rows"], "tables": len(packet["tables"]),
        "sourceCompatibilityLevel": 1702, "sourcePbismVersion": "4.2", "sourceMeasureCount": 24,
        "sourceModelParts": packet["sourceModelParts"], "dataClocks": packet["dataClocks"],
        "codeInventory": [{"file": name, "sha256": sha256((ROOT / name).read_bytes())} for name in CODE],
        "initialOperations": [{
            "name": "create-hero-lakehouse", "method": "POST",
            "path": "/v1/workspaces/<WORKSPACE_ID>/items",
            "bodySha256": body_digest(read_json(packet_dir / "lakehouse-template.json")),
            "createsAlias": "hero-lakehouse", "createdType": "Lakehouse",
        }],
        "boundOperations": [{
            "name": "create-" + alias, "file": name, "bodySha256": None,
        } for alias, name in [("hero-loader", "notebook-template.json"), *[
            (item["alias"], item["file"]) for item in packet["modelPackets"]
        ]]],
        "stages": [
            "initial: approved receipt-owned workspace; create/read back only new lakehouse",
            "bind: private accepted-ID copy; STOP for new immutable bound-body approval",
            "load: approved complete notebook create/readback; submit ONCE; poll accepted job",
            "assertions: import reviewed actual Spark stdout/readback; no guessed stdout API",
            "models: exact assertion gate; complete source create/readback; Power BI refresh/query controls",
        ],
        "approvalBreakpoint": "BOUND_LOCALLY_NOT_APPROVED",
        "requiredPrivateScope": "HeroScope.model_json_schema(); scope plus every executed artifact must be approval-pinned",
        "separateGates": ["capacity/resume/shutdown", "certificate", "native Plan/human approval/Act/readback",
                          "paid inference", "cleanup", "publication"],
        "proofLimits": [
            "execute needs fresh exact approval plus shared manual-owner policy OR dedicated shutdown proof.",
            "Capacity must already be Active on its approved SKU/assignment; no capacity change is supported.",
            "Job status is not a source assertion. Public job API exposes no table-assertion/stdout contract here.",
            "Exact part readback fails closed on service normalization/unknown parts until explicitly reviewed.",
            "Selected controls are not DAX certification of all 64 source measures or native/read permissions.",
        ],
    }
    if output is not None:
        output.mkdir(parents=True, exist_ok=False)
        write_new(output / "hero-plan.json", report)
        write_new(output / "hero-scope-schema.json", HeroScope.model_json_schema())
        from infra.live_transports import PrivateCliConfig
        write_new(output / "hero-live-cli-schema.json", PrivateCliConfig.model_json_schema())
        write_new(output / "hero-approval-template.json", {
            "approved": False, "status": "UNSELECTED_PRIVATE_APPROVAL_REQUIRED",
            "packetSha256": report["packetSha256"], "operations": report["initialOperations"],
            "boundBodies": "UNRESOLVED; initial create cannot approve future ID-bound bytes",
            "targets": None, "actor": None, "costEstimateAndCap": None, "UTCWindow": None,
            "ownedTeardownOwner": None, "shutdownEvidence": None,
        })
    return report


class QueryControl(Record):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    model: Literal["enterprise", "finance", "network"]
    homeTable: str
    measure: str
    body: dict
    expectedRows: list[dict] = Field(min_length=1)
    coverage: Literal["KNOWN", "PARTIAL", "UNKNOWN"]
    completeAggregate: bool = True
    matchedRowsField: str = Field(pattern=r"^\[[A-Za-z][A-Za-z0-9]*\]$")
    unknownRowsField: str = Field(pattern=r"^\[[A-Za-z][A-Za-z0-9]*\]$")
    invalidRowsField: str = Field(pattern=r"^\[[A-Za-z][A-Za-z0-9]*\]$")


class JobProof(Record):
    authorization: Artifact
    accepted: Artifact
    completed: Artifact


class HeroScope(Record):
    schemaVersion: Literal["hero-deployment-scope-v1"]
    phase: Literal["initial", "load", "models"]
    packet: Artifact
    populationSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    codeInventory: list[Artifact]
    dependencies: dict[str, Artifact]
    principal: SelectedPrincipal
    capacityId: str
    capacitySku: Literal["F8"] = "F8"
    capacityPolicy: Literal["DEDICATED_CAPACITY", "SHARED_CAPACITY_AS_IS"] = "DEDICATED_CAPACITY"
    lroPolling: LroPolling = Field(default_factory=LroPolling)
    liveConfig: Artifact | None = None
    boundPacket: Artifact | None = None
    bindingMetadata: Artifact | None = None
    loadProof: JobProof | None = None
    sourceAssertions: Artifact | None = None
    controls: list[QueryControl] = Field(default_factory=list)
    refreshBodies: dict[str, dict] = Field(default_factory=dict)
    maximumPolls: int = Field(gt=0, le=100)
    maximumPollSeconds: int = Field(gt=0, le=3600)


def checked_file(private: Path, item: Artifact) -> Path:
    path = Path(item.file)
    path = path.resolve() if path.is_absolute() else local_file(private, item.file)
    if sha256(path.read_bytes()) != item.sha256:
        raise RuntimeError("Scope artifact changed: " + path.name)
    return path


def accepted_dependency(receipt: dict, task: str) -> bool:
    if not isinstance(receipt, dict):
        return False
    identities = [receipt[key] for key in ("task", "task_id") if key in receipt]
    states = [receipt[key] for key in ("status", "acceptanceStatus", "acceptance_status") if key in receipt]
    return (
        bool(identities) and all(identity == task for identity in identities)
        and bool(states) and all(state == "ACCEPTED_LOCAL_ONLY" for state in states)
        and ("accepted" not in receipt or receipt["accepted"] is True)
    )


def validate_scope(private: Path, scope_path: Path, lease: Renewal) -> tuple[HeroScope, dict, dict]:
    private = private_path(private)
    scope_path = private_path(scope_path)
    scope = HeroScope.model_validate(read_json(scope_path))
    validate_principal(lease, scope.principal)
    if scope.lroPolling != lease.approval.lroPolling:
        raise RuntimeError("Scope LRO polling strategy/evidence origin differs from explicit approval")
    if scope.capacityPolicy != lease.approval.capacityPolicy or (
        scope.capacityPolicy == "SHARED_CAPACITY_AS_IS" and (
            lease.approval.sharedCapacityWorkload is None
            or lease.approval.sharedCapacityWorkload.capacityId != scope.capacityId
        )
    ):
        raise RuntimeError("Scope capacity policy/approved shared workload target differs")
    if str(uuid.UUID(scope.capacityId)) != scope.capacityId:
        raise RuntimeError("Canonical approved capacity assignment required")
    pinned = {(str(checked_file(private, item)), item.sha256) for item in lease.approval.artifacts}

    def pinned_file(item: Artifact) -> Path:
        path = checked_file(private, item)
        if (str(path), item.sha256) not in pinned:
            raise RuntimeError("Every executed packet/body/code/receipt must be approval-pinned")
        return path

    if (str(scope_path), sha256(scope_path.read_bytes())) not in pinned:
        raise RuntimeError("Hero scope must be immutable and approval-pinned")
    packet_path = pinned_file(scope.packet)
    if packet_path.name != "packet.json":
        raise RuntimeError("Scope must pin the actual packet.json root, not a companion artifact")
    packet_dir = packet_path.parent
    packet = packet_contract(packet_dir)
    for entry in packet["packetFiles"]:
        pinned_file(Artifact(file=str(packet_dir / entry["file"]), sha256=entry["sha256"]))
    if scope.populationSha256 != body_digest(packet["population"]):
        raise RuntimeError("Private binding population lineage differs")
    if len(scope.codeInventory) != len(CODE) or {
        str(pinned_file(item)) for item in scope.codeInventory
    } != {str(ROOT / name) for name in CODE}:
        raise RuntimeError("Exact executed runner/adapter code inventory required")
    if scope.liveConfig is not None:
        from infra.live_transports import validate_config
        validate_config(private_path(pinned_file(scope.liveConfig)), scope, lease)
    if set(scope.dependencies) != {"TQ-003", "TQ-004", "TQ-005"}:
        raise RuntimeError("Accepted TQ-003/004/005 dependencies required")
    for task, item in scope.dependencies.items():
        if not accepted_dependency(read_json(pinned_file(item)), task):
            raise RuntimeError("Local dependency not accepted; no client construction permitted")
    ownership = lease.approval.ownershipReceipts
    if "workspace" not in ownership:
        raise RuntimeError("An approved accepted-create workspace receipt is required, never P0 UUID/name adoption")
    workspace = verified_ownership(private, ownership["workspace"])
    if workspace["id"] != lease.approval.workspaceId or workspace.get("capacityId") != scope.capacityId:
        raise RuntimeError("Receipt workspace target differs")
    if set(lease.approval.ownedItems.values()) & set(lease.approval.protectedItemIds):
        raise RuntimeError("Protected historical items cannot become hero ownership")
    bound = {}
    if scope.phase != "initial":
        if scope.boundPacket is None or scope.bindingMetadata is None or "hero-lakehouse" not in ownership:
            raise RuntimeError("Private accepted-ID binding and new immutable approval required")
        bound_path = private_path(pinned_file(scope.boundPacket))
        metadata_path = private_path(pinned_file(scope.bindingMetadata))
        if bound_path.name != "bound-packet.json" or metadata_path.name != "hero-binding.json":
            raise RuntimeError("Pin the actual bound-packet and private binding metadata roots")
        bound_dir = bound_path.parent
        bound = read_json(bound_dir / "bound-packet.json")
        metadata = read_json(metadata_path)
        lakehouse = verified_ownership(private, ownership["hero-lakehouse"])
        if (
            bound["sourcePacketSha256"] != scope.packet.sha256
            or bound["status"] != "BOUND_LOCALLY_NOT_APPROVED" or bound["approved"] is not False
            or bound["workspace"] != {k: workspace[k] for k in ("id", "displayName")}
            or bound["lakehouse"] != {k: lakehouse[k] for k in ("id", "displayName")}
            or metadata["populationSha256"] != scope.populationSha256
            or metadata["dataClocks"] != packet["dataClocks"]
            or metadata["sourcePacketSha256"] != scope.packet.sha256
            or metadata["codeInventory"] != [item.model_dump() for item in scope.codeInventory]
            or any(metadata["ownership"][alias] != ownership[alias].model_dump()
                   for alias in ("workspace", "hero-lakehouse"))
        ):
            raise RuntimeError("Bound population/source/receipt identity differs")
        expected_files = {"loader.ipynb", "lakehouse-template.json", "notebook-template.json",
                          *(item["file"] for item in packet["modelPackets"])}
        if (len(bound["files"]) != len(expected_files)
                or {entry["file"] for entry in bound["files"]} != expected_files):
            raise RuntimeError("Bound complete packet file inventory differs")
        for entry in bound["files"]:
            path = private_path(pinned_file(Artifact(
                file=str(bound_dir / entry["file"]), sha256=entry["sha256"],
            )))
            if path.stat().st_size != entry["bytes"]:
                raise RuntimeError("Bound bytes differ")
        replacements = {"<WORKSPACE_ID>": workspace["id"], "<LAKEHOUSE_ID>": lakehouse["id"],
                        "<WORKSPACE_NAME>": workspace["displayName"]}
        for item in packet["modelPackets"]:
            source_body, bound_body = read_json(packet_dir / item["file"]), read_json(bound_dir / item["file"])
            original, actual = parts(source_body), parts(bound_body)
            expected = {}
            for name, raw in original.items():
                if name == "definition/expressions.tmdl":
                    for token, value in replacements.items():
                        raw = raw.replace(token.encode(), value.encode())
                expected[name] = raw
            if actual != expected or (
                {key: value for key, value in source_body.items() if key != "definition"}
                != {key: value for key, value in bound_body.items() if key != "definition"}
            ) or bound_body["definition"]["format"] != source_body["definition"]["format"]:
                raise RuntimeError("Bound model changed source measures/settings/complete parts")
        expected_notebook = (packet_dir / "loader.ipynb").read_bytes()
        for token, value in replacements.items():
            expected_notebook = expected_notebook.replace(token.encode(), value.encode())
        if (bound_dir / "loader.ipynb").read_bytes() != builder.encoded(json.loads(expected_notebook)):
            raise RuntimeError("Bound notebook differs from complete approved packet")
        if parts(read_json(bound_dir / "notebook-template.json")) != {
            "notebook-content.ipynb": (bound_dir / "loader.ipynb").read_bytes(),
        }:
            raise RuntimeError("Bound notebook template differs")
        for file in ("lakehouse-template.json", "notebook-template.json"):
            actual, original = read_json(bound_dir / file), read_json(packet_dir / file)
            if file == "notebook-template.json":
                original["definition"] = actual["definition"]
            if actual != original:
                raise RuntimeError("Bound complete create body differs from packet")
        if metadata["boundBodyDigests"] != {
            entry["file"]: body_digest(read_json(bound_dir / entry["file"]))
            for entry in bound["files"] if entry["file"].endswith("-template.json")
        }:
            raise RuntimeError("Private bound-body digest inventory differs")
        bound["directory"] = bound_dir
    if scope.phase == "models":
        if scope.loadProof is None or scope.sourceAssertions is None:
            raise RuntimeError("Exact accepted job and source assertion receipt required before models")
        for item in (scope.loadProof.authorization, scope.loadProof.accepted,
                     scope.loadProof.completed, scope.sourceAssertions):
            private_path(pinned_file(item))
        if set(scope.refreshBodies) != set(MODELS) or {item.model for item in scope.controls} != set(MODELS):
            raise RuntimeError("All three source refreshes and explicit query controls are required")
        for body in scope.refreshBodies.values():
            refresh_body(body)
        catalog = read_json(packet_dir / "source-catalog.json")
        table_names = {item["id"]: item["displayName"] for item in catalog["tables"]}
        for control in scope.controls:
            metrics = [item for item in catalog["metrics"] if (
                item["model"] == control.model and table_names[item["table"]] == control.homeTable
                and item["name"] == control.measure
            )]
            if len(metrics) != 1:
                raise RuntimeError("Query control measure/home/source identity is not in the exact packet")
            if (
                set(control.body) != {"queries", "serializerSettings"}
                or control.body["serializerSettings"] != {"includeNulls": True}
                or len(control.body["queries"]) != 1
                or set(control.body["queries"][0]) != {"query"}
                or "[" + control.measure.replace("]", "]]") + "]" not in control.body["queries"][0]["query"]
            ):
                raise RuntimeError("Query control must be an exact reviewed single source-measure query")
            if control.completeAggregate and control.coverage != "KNOWN":
                raise RuntimeError("Unknown/partial coverage cannot establish a complete aggregate")
            query = control.body["queries"][0]["query"]
            fields = (control.matchedRowsField, control.unknownRowsField, control.invalidRowsField)
            if len(set(fields)) != 3 or any('"' + field[1:-1] + '"' not in query for field in fields):
                raise RuntimeError("Reviewed query must return separate matched/unknown/invalid source controls")
            table = next(item for item in catalog["tables"] if item["id"] == metrics[0]["table"])
            if table.get("coverage") and (
                "[" + table["coverage"]["column"] + "]" not in query
            ):
                raise RuntimeError("Mapping-dependent measure requires its declared source coverage column")
    allowed = operation_contract(scope, packet, workspace["id"], bound)
    actual = [op.model_dump() for op in lease.approval.operations]
    # Compare executable fields, including default deny switches, not just names.
    from infra.renewal import Operation
    if sorted(actual, key=lambda op: op["name"]) != sorted(
        [Operation.model_validate(op).model_dump() for op in allowed], key=lambda op: op["name"],
    ):
        raise RuntimeError("Hero operation allowlist differs from this exact stage/body contract")
    return scope, packet, bound


def operation_contract(scope: HeroScope, packet: dict, wid: str, bound: dict) -> list[dict]:
    prefix = f"/v1/workspaces/{wid}"
    operations = []

    def op(name, method, path, body=None, **kwargs):
        operations.append({"name": name, "method": method, "path": path,
                           "bodySha256": body_digest(body), **kwargs})

    op("list-items", "GET", prefix + "/items", allowPagination=True)
    op("read-workspace", "GET", prefix)
    op("read-capacities", "GET", "/v1/capacities", allowPagination=True)
    if scope.phase == "initial":
        source = Path(scope.packet.file).parent
        selections = [("hero-lakehouse", "lakehouse-template.json", "items", "Lakehouse")]
    else:
        source = bound["directory"]
        selections = [("hero-loader", "notebook-template.json", "items", "Notebook")] if scope.phase == "load" else [
            (entry["alias"], entry["file"], "semanticModels", "SemanticModel") for entry in packet["modelPackets"]
        ]
    for alias, file, endpoint, kind in selections:
        body = read_json(source / file)
        op("create-" + alias, "POST", prefix + "/" + endpoint, body, allowLro=True,
           createsAlias=alias, createdDisplayName=body["displayName"], createdType=kind)
        op("read-" + alias, "GET", prefix + "/items/{" + alias + "}")
        if kind != "Lakehouse":
            collection, fmt = ("notebooks", "ipynb") if kind == "Notebook" else ("semanticModels", "TMDL")
            op("definition-" + alias, "POST", prefix + f"/{collection}/{{{alias}}}/getDefinition?format={fmt}",
               {}, allowLro=True)
    if scope.phase == "load":
        op("loader-history", "GET", prefix + "/items/{hero-loader}/jobs/instances", allowPagination=True)
        op("run-loader", "POST", prefix + "/items/{hero-loader}/jobs/RunNotebook/instances", allowJobPolling=True)
    if scope.phase == "models":
        for model in MODELS:
            base = f"/v1.0/myorg/groups/{wid}/datasets/{{{model}}}"
            op("refresh-history-" + model, "GET", base + "/refreshes?$top=10", audience="powerbi")
            op("refresh-" + model, "POST", base + "/refreshes", scope.refreshBodies[model], audience="powerbi")
        for control in scope.controls:
            op("control-" + control.name, "POST",
               f"/v1.0/myorg/groups/{wid}/datasets/{{{control.model}}}/executeQueries",
               control.body, audience="powerbi")
    return operations


class FabricTransport(Author):
    """Reuse Author's guarded request/LRO, but NEVER Author's login constructor."""

    def __init__(
        self, lease: Renewal, client: HttpClient, *, principal: SelectedPrincipal,
        token_resource: str, proof_level: Literal["SYNTHETIC_LOCAL", "AUTHOR_SERVICE"],
    ):
        lease.check_current()
        validate_principal(lease, principal)
        if token_resource != FABRIC_RESOURCE:
            raise RuntimeError("Fabric transport token audience mismatch")
        if proof_level not in {"SYNTHETIC_LOCAL", "AUTHOR_SERVICE"}:
            raise RuntimeError("Explicit synthetic/author-service proof level required")
        self.private, self.renewal, self.client = lease.private, lease, client
        self.principal, self.proof_level = principal, proof_level

    def request(self, method, path, *, skill="semantic-model-authoring", **kwargs):
        validate_principal(self.renewal, self.principal)
        return super().request(method, path, skill=skill, **kwargs)

    def save(self, name, value):
        super().save(name, value | {"proofLevel": self.proof_level})


class FabricRequests(Protocol):
    renewal: Renewal
    proof_level: str
    principal: SelectedPrincipal
    def request(self, method: str, path: str, **kwargs) -> httpx.Response: ...
    def completed(self, response: httpx.Response, **kwargs): ...
    def listing(self, path: str, **kwargs) -> list[dict]: ...


class TransportFactory(Protocol):
    def __call__(self, scope: HeroScope, lease: Renewal) -> tuple[FabricRequests, PowerBIRefresh]: ...


class HeroRunner:
    def __init__(
        self, private: Path, scope_file: Path, lease: Renewal, *, transports: TransportFactory,
    ):
        self.scope, self.packet, self.bound = validate_scope(private, scope_file, lease)
        self.lifecycle_policy = lease.verify_lifecycle_policy()
        self.lease, self.private = lease, lease.private
        self.wid = lease.approval.workspaceId
        self.packet_dir = checked_file(self.private, self.scope.packet).parent
        if self.scope.phase == "models" and self.scope.liveConfig is not None:
            self.proof_level = "AUTHOR_SERVICE"
            self.source_gate()
        # The factory is unreachable for unapproved/changed/unaccepted scopes.
        fabric, refresh = transports(self.scope, lease)
        if (
            fabric.renewal is not lease or refresh.lease is not lease
            or fabric.proof_level != refresh.proof_level
            or fabric.principal != self.scope.principal or refresh.principal != self.scope.principal
        ):
            raise RuntimeError("Runner transports are not bound to this exact approval/principal/proof level")
        self.lease, self.fabric, self.refresh = lease, fabric, refresh
        self.wid, self.private = lease.approval.workspaceId, lease.private
        self.proof_level = fabric.proof_level
        self.packet_dir = checked_file(self.private, self.scope.packet).parent

    def create(self, alias: str, body: dict, endpoint: str, kind: str) -> dict:
        op_name = "create-" + alias
        records = [r for r in self.lease._records() if r["operation"] == op_name]
        if len(records) > 1:
            raise RuntimeError("Ambiguous create receipt; no ownership or retry")
        if records:
            record = records[0]
            if record.get("state") != "ACCEPTED" or record.get("receiptValidated") is not True:
                raise RuntimeError("Create outcome unknown/rejected; read-only reconciliation only")
            response = httpx.Response(record["status"], headers={
                key: value for key, value in {
                    "Location": record.get("location"), "x-ms-operation-id": record.get("operationId"),
                    "Retry-After": record.get("retryAfter"),
                }.items() if value is not None
            }, json=record["response"])
        else:
            items = self.fabric.listing(f"/v1/workspaces/{self.wid}/items")
            if any(item["displayName"].casefold() == body["displayName"].casefold() for item in items):
                raise RuntimeError("Name collision; never adopt/update a pre-existing item")
            response = self.fabric.request("POST", f"/v1/workspaces/{self.wid}/{endpoint}", json=body)
        item = self.fabric.completed(response)
        if (
            not isinstance(item, dict) or item.get("type") != kind or item.get("displayName") != body["displayName"]
            or item.get("workspaceId", self.wid) != self.wid
        ):
            raise RuntimeError("Create has no exact accepted type/name/workspace; no ownership")
        records = [r for r in self.lease._records() if r["operation"] == op_name]
        if len(records) != 1 or item.get("id") != records[0].get("createdId"):
            raise RuntimeError("Fabricated returned ID; not derived from approved accepted create")
        current = self.fabric.request("GET", f"/v1/workspaces/{self.wid}/items/{item['id']}")
        if current.status_code != 200 or current.json() != item:
            raise RuntimeError("Item readback differs from its accepted result; no ownership")
        return item

    def definition(self, alias: str, item: dict, body: dict, collection: str, fmt: str) -> dict:
        response = self.fabric.request(
            "POST", f"/v1/workspaces/{self.wid}/{collection}/{item['id']}/getDefinition?format={fmt}", json={},
        )
        observed = self.fabric.completed(response)
        if not isinstance(observed, dict) or parts(observed) != parts(body):
            raise RuntimeError("Complete definition/home/source/settings readback differs or is unknown")
        return observed

    def run(self) -> dict:
        self.lease.check_current()
        capacities = self.fabric.listing("/v1/capacities")
        selected = [item for item in capacities if item.get("id") == self.scope.capacityId]
        if len(selected) != 1 or (
            selected[0].get("state"), selected[0].get("sku")
        ) != ("Active", self.scope.capacitySku):
            raise RuntimeError("Approved capacity is not already Active on its exact SKU; no capacity change permitted")
        current = self.fabric.request("GET", f"/v1/workspaces/{self.wid}").json()
        owned = verified_ownership(self.private, self.lease.approval.ownershipReceipts["workspace"])
        if any(current.get(key) != owned.get(key) for key in ("id", "displayName")) or (
            current.get("capacityId") != self.scope.capacityId
        ):
            raise RuntimeError("Current workspace/approved capacity assignment differs; no mutation")
        if self.scope.phase == "initial":
            item = self.create(
                "hero-lakehouse", read_json(self.packet_dir / "lakehouse-template.json"), "items", "Lakehouse",
            )
            return self.receipt("INITIAL_CREATE_READ_BACK", {"created": item,
                                "next": "Private bind then STOP: BOUND_LOCALLY_NOT_APPROVED"})
        if self.scope.phase == "load":
            return self.load()
        return self.models()

    def receipt(self, status: str, value: dict) -> dict:
        result = {
            "schemaVersion": "hero-stage-receipt-v1", "status": status, "proofLevel": self.proof_level,
            "approvalSha256": self.lease.digest, "packetSha256": self.scope.packet.sha256,
            "populationSha256": self.scope.populationSha256, "phase": self.scope.phase,
            "observedAtUtc": datetime.now(timezone.utc).isoformat(), "liveExecution": (
                "NOT_RUN" if self.proof_level == "SYNTHETIC_LOCAL" else "AUTHOR_SERVICE_ONLY"
            ), "lifecycle": self.lifecycle_policy, **value,
        }
        write_new(self.private / ("hero-stage-" + uuid.uuid4().hex + ".json"), result)
        return result

    def load(self) -> dict:
        body = read_json(self.bound["directory"] / "notebook-template.json")
        item = self.create("hero-loader", body, "items", "Notebook")
        self.definition("hero-loader", item, body, "notebooks", "ipynb")
        prefix = f"/v1/workspaces/{self.wid}/items/{item['id']}"
        records = [r for r in self.lease._records() if r["operation"] == "run-loader"]
        if records:
            record = records[0]
            if record.get("state") != "ACCEPTED" or not record.get("jobPath"):
                raise RuntimeError("Loader outcome unknown; never repeat notebook submission")
            location, delay = record["jobPath"], record.get("retryAfter") or "15"
        else:
            if self.fabric.listing(prefix + "/jobs/instances", skill="spark-cli"):
                raise RuntimeError("Unexpected existing notebook job; never adopt it as this load")
            response = self.fabric.request("POST", prefix + "/jobs/RunNotebook/instances", skill="spark-cli")
            record = next(r for r in self.lease._records() if r["operation"] == "run-loader")
            if response.status_code != 202 or not record.get("jobPath"):
                raise RuntimeError("Loader accepted job identity absent; no retry")
            location, delay = record["jobPath"], record.get("retryAfter") or "15"
        stop = time.monotonic() + self.scope.maximumPollSeconds
        for _ in range(self.scope.maximumPolls):
            self.lease.check_current()
            seconds = int(delay)
            if seconds < 0 or time.monotonic() + seconds >= stop or (
                self.lease.deadline - datetime.now(timezone.utc)
            ).total_seconds() <= seconds:
                raise TimeoutError("Loader poll budget/window expired; cancellation separately gated")
            self.refresh.sleep(seconds)
            response = self.fabric.request("GET", location, skill="spark-cli")
            state = response.json()
            if state.get("id") != location.rsplit("/", 1)[-1] or state.get("itemId") != item["id"]:
                raise RuntimeError("Notebook job readback identity differs")
            if state.get("status") == "Completed" and not state.get("failureReason"):
                return self.receipt("SOURCE_ASSERTIONS_REQUIRED", {
                    "job": state, "notebookBodySha256": body_digest(body),
                    "sourceAssertions": "NOT_PROVEN_BY_JOB_STATUS",
                    "next": "Capture actual complete source assertion receipt; new frozen models-stage approval",
                })
            if state.get("status") not in {"NotStarted", "InProgress"}:
                raise RuntimeError("Notebook failed/deduped/unknown; no source success or automatic retry")
            delay = response.headers.get("Retry-After", "15")
        raise TimeoutError("Notebook polling budget exhausted; monitor accepted job only")

    def source_gate(self) -> dict:
        proof = self.scope.loadProof
        if proof is None or self.scope.sourceAssertions is None:
            raise RuntimeError("Source assertions and accepted notebook proof are mandatory")
        authorization = read_json(checked_file(self.private, proof.authorization))
        accepted = read_json(checked_file(self.private, proof.accepted))
        completed = read_json(checked_file(self.private, proof.completed))
        assertions = read_json(checked_file(self.private, self.scope.sourceAssertions))
        lakehouse = self.lease.approval.ownedItems["hero-lakehouse"]
        loader = self.lease.approval.ownedItems.get("hero-loader")
        prefix = f"/v1/workspaces/{self.wid}/items/{loader}"
        expected_location = prefix + "/jobs/instances/"
        try:
            job_path = fabric_path(accepted["location"])
            job_id = job_path.removeprefix(expected_location)
            operation = next(op for op in authorization["operations"] if op["name"] == accepted["operation"])
            valid = (
                authorization["approved"] is True and body_digest(authorization) == accepted["approvalSha256"]
                and accepted["state"] == "ACCEPTED" and accepted["receiptValidated"] is True
                and accepted["method"] == "POST" and accepted["status"] == 202
                and accepted["path"] == prefix + "/jobs/RunNotebook/instances" == operation["path"].replace(
                    "{hero-loader}", loader,
                )
                and accepted["bodySha256"] == operation["bodySha256"] == body_digest(None)
                and str(uuid.UUID(job_id)) == job_id and job_path == expected_location + job_id
                and completed["proofLevel"] == self.proof_level and completed["method"] == "GET"
                and completed["path"] == job_path and completed["status"] == 200
                and completed["response"]["id"] == job_id and completed["response"]["itemId"] == loader
                and completed["response"]["jobType"] == "RunNotebook"
                and completed["response"]["status"] == "Completed" and not completed["response"].get("failureReason")
                and assertions["schemaVersion"] == "hero-source-assertions-v1"
                and assertions["proofLevel"] == self.proof_level
                and assertions["packetSha256"] == self.scope.packet.sha256
                and assertions["populationSha256"] == self.scope.populationSha256
                and assertions["lakehouseId"] == lakehouse and assertions["loaderId"] == loader
                and assertions["jobId"] == job_id and assertions["jobReceiptSha256"] == proof.completed.sha256
                and assertions["notebookBodySha256"] == body_digest(
                    read_json(self.bound["directory"] / "notebook-template.json"),
                )
            )
            expected = {table["name"]: table for table in self.packet["tables"]}
            rows = assertions["tables"]
            valid = valid and len(rows) == len(expected) and {row["name"] for row in rows} == expected.keys()
            for row in rows:
                table = expected[row["name"]]
                valid = valid and (
                    type(row["rows"]) is int and row["rows"] == table["rows"]
                    and row["expectedFileSha256"] == table["fileSha256"]
                    and row["exactDataReadback"] is True and row["keyChecks"] == "PASSED"
                    and type(row["missingExpectedRows"]) is int and row["missingExpectedRows"] == 0
                    and type(row["unexpectedRows"]) is int and row["unexpectedRows"] == 0
                )
            observed = utc(assertions["observedAtUtc"])
            valid = valid and utc(completed["startedAt"]) <= observed <= datetime.now(timezone.utc)
        except (KeyError, TypeError, ValueError, StopIteration) as error:
            raise RuntimeError("Incomplete actual source assertion/job proof") from error
        if not valid:
            raise RuntimeError("Source assertion/count/key/expected-data/job binding failed")
        return assertions

    def models(self) -> dict:
        assertions = self.source_gate()
        results = {}
        for entry in self.packet["modelPackets"]:
            alias = entry["alias"]
            body = read_json(self.bound["directory"] / entry["file"])
            item = self.create(alias, body, "semanticModels", "SemanticModel")
            self.definition(alias, item, body, "semanticModels", "TMDL")
            state_path = self.private / f"hero-{alias}-refresh-accepted.json"
            if not state_path.exists():
                self.refresh.submit(alias, self.wid, item["id"], self.scope.refreshBodies[alias])
            refreshed = self.refresh.monitor(
                alias, self.wid, item["id"], maximum_polls=self.scope.maximumPolls,
                maximum_seconds=self.scope.maximumPollSeconds,
            )
            controls = []
            for control in [control for control in self.scope.controls if control.model == alias]:
                if control.completeAggregate and control.coverage != "KNOWN":
                    raise RuntimeError("Unknown/partial coverage cannot establish a complete aggregate")
                response, receipt = self.refresh.request(
                    "POST", f"/v1.0/myorg/groups/{self.wid}/datasets/{item['id']}/executeQueries",
                    body=control.body,
                )
                rows = query_rows(response)
                for row in rows:
                    matched, unknown, invalid = (
                        row.get(field) for field in (
                            control.matchedRowsField, control.unknownRowsField, control.invalidRowsField,
                        )
                    )
                    diagnostic_fields = {
                        control.matchedRowsField, control.unknownRowsField, control.invalidRowsField,
                    }
                    if (
                        any(type(value) is not int or value < 0 for value in (matched, unknown, invalid))
                        or unknown != 0 or invalid != 0
                        or (control.completeAggregate and any(
                            value is None for key, value in row.items() if key not in diagnostic_fields
                        ))
                    ):
                        raise RuntimeError("Required source query reports unknown/invalid/null inputs; not complete")
                if rows != control.expectedRows:
                    raise RuntimeError("Required source query control mismatch; model not accepted")
                controls.append({"name": control.name, "homeTable": control.homeTable, "measure": control.measure,
                                 "bodySha256": receipt["bodySha256"], "coverage": control.coverage,
                                 "completeAggregate": control.completeAggregate, "status": "PASSED"})
            # One completion predicate, reached only after every preceding gate.
            results[alias] = {
                "status": "ACCEPTED_FOR_LISTED_CONTROLS_ONLY", "sourceAssertions": "PASSED",
                "completeDefinitionReadback": "EXACT_APPROVED_PARTS", "refresh": refreshed["refresh"],
                "controls": controls, "all64MeasuresEngineCertified": False,
            }
        return self.receipt("THREE_SOURCE_QUERY_CONTROLS_PASSED", {
            "models": results, "sourceAssertionSha256": self.scope.sourceAssertions.sha256,
            "dataClock": self.packet["dataClocks"], "sourceModelClock": {
                alias: value["refresh"]["endTime"] for alias, value in results.items()
            }, "ontologyClock": None, "contextCacheClock": None,
            "nativeOntology": "NOT_RUN_SEPARATE_PLAN_HUMAN_APPROVAL_ACT_READBACK",
            "populationCoverage": "QUERY_SCOPED_NOT_UNIVERSALLY_COMPLETE",
            "sourceAssertionProofLevel": assertions["proofLevel"],
        })


def ownership_spec(private: Path, lease: Renewal, alias: str) -> Ownership:
    """Export only exact accepted create and actual HTTP readback/result receipts."""
    records = [(path, read_json(path)) for path in lease.ledger.glob("*.json")]
    matches = [(path, record) for path, record in records if (
        record.get("approvalSha256") == lease.digest and record["operation"] == "create-" + alias
        and record.get("state") == "ACCEPTED" and record.get("createdId")
    )]
    if len(matches) != 1:
        raise RuntimeError("No single accepted create grants this alias ownership")
    accepted_path, record = matches[0]
    reads = [(path, read_json(path)) for path in private.glob("http-*.json")]
    target = f"/v1/workspaces/{lease.approval.workspaceId}/items/{record['createdId']}"
    readbacks = [(path, value) for path, value in reads if value["method"] == "GET"
                 and value["path"] == target and value["status"] == 200]
    if not readbacks:
        raise RuntimeError("Accepted create lacks an actual item readback")
    spec = {"authorization": artifact(lease.path), "accepted": artifact(accepted_path),
            "readback": artifact(readbacks[-1][0])}
    if record["status"] == 202:
        results = [(path, value) for path, value in reads if value["method"] == "GET"
                   and value["path"] == record.get("resultPath") and value["status"] == 200]
        if not results:
            raise RuntimeError("Accepted LRO has no exact result receipt")
        spec["result"] = artifact(results[-1][0])
    parsed = Ownership.model_validate(spec)
    verified_ownership(private, parsed)
    return parsed


def bind_owned(private: Path, scope_file: Path, lease: Renewal, output: Path) -> dict:
    scope, packet, _ = validate_scope(private, scope_file, lease)
    if scope.phase != "initial":
        raise RuntimeError("Bind only the initial accepted lakehouse; never rebind an approved load/model scope")
    workspace = verified_ownership(lease.private, lease.approval.ownershipReceipts["workspace"])
    ownership = ownership_spec(lease.private, lease, "hero-lakehouse")
    lakehouse = verified_ownership(lease.private, ownership)
    binding = {
        "packetSha256": scope.packet.sha256,
        "workspace": {key: workspace[key] for key in ("id", "displayName")},
        "lakehouse": {key: lakehouse[key] for key in ("id", "displayName")},
    }
    report = builder.bind(checked_file(lease.private, scope.packet).parent, binding, output)
    write_new(private_path(output) / "hero-binding.json", {
        "schemaVersion": "hero-private-binding-v1", "status": "BOUND_LOCALLY_NOT_APPROVED", "approved": False,
        "sourcePacketSha256": scope.packet.sha256, "population": packet["population"],
        "populationSha256": scope.populationSha256, "dataClocks": packet["dataClocks"],
        "sourceModelClock": None, "ontologyClock": None, "contextCacheClock": None,
        "ownership": {"workspace": lease.approval.ownershipReceipts["workspace"].model_dump(),
                      "hero-lakehouse": ownership.model_dump()},
        "codeInventory": [item.model_dump() for item in scope.codeInventory],
        "boundBodyDigests": {
            entry["file"]: body_digest(read_json(output / entry["file"]))
            for entry in report["files"] if entry["file"].endswith("-template.json")
        }, "next": "STOP: new immutable exact bound body/code/packet approval required before transport",
        "liveExecution": "NOT_RUN",
    })
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pure = commands.add_parser("plan", help="Pure validated packet/code plan; no auth or client construction")
    pure.add_argument("--packet", type=Path, required=True, help="Packet directory or its packet.json")
    pure.add_argument("--output", type=Path, help="New unbound local stage directory")
    for name in ("dry-run", "bind", "execute"):
        command = commands.add_parser(name, help=(
            "Validate frozen private approval/scope with zero transport" if name == "dry-run"
            else "Bind accepted IDs then STOP for NEW approval" if name == "bind"
            else "Explicit finite approved stage; pinned private CLI/profile; no capacity or ontology changes"
        ))
        command.add_argument("--private-dir", type=Path, required=True)
        command.add_argument("--approval", type=Path, required=True)
        command.add_argument("--scope", type=Path, required=True)
        if name == "bind":
            command.add_argument("--output", type=Path, required=True, help="New PRIVATE output, never source/OneDrive")
        if name == "execute":
            command.add_argument("--stage", choices=("initial", "load", "models"), required=True)
            command.add_argument("--cli-config", type=Path, required=True, help=(
                "Private expectedUser/expectedTenant/expectedSubscription and explicit CLI/profile; "
                "must match scope.liveConfig and be approval-pinned"
            ))
    args = parser.parse_args(argv)
    if args.command == "plan":
        directory = args.packet.parent if args.packet.name == "packet.json" else args.packet
        report = plan(directory, output=args.output)
    else:
        # Renewal validates local files only. Author.__init__ is never called.
        lease = Renewal(args.private_dir, args.approval)
        if args.command == "bind":
            report = bind_owned(args.private_dir, args.scope, lease, args.output)
        elif args.command == "execute":
            from infra.live_transports import LiveTransports
            with LiveTransports(args.scope, args.cli_config, stage=args.stage) as transports:
                report = HeroRunner(args.private_dir, args.scope, lease, transports=transports).run()
        else:
            scope, _, _ = validate_scope(args.private_dir, args.scope, lease)
            report = {"status": "VALIDATED_DRY_RUN_ZERO_TRANSPORT", "phase": scope.phase,
                      "liveExecution": "NOT_RUN", "authorConstructed": False,
                      "productionBlockedUntil": [
                          "explicit execute --stage matching the immutable scope",
                          "approval-pinned private CLI configuration and reverified selected account",
                          "explicit approved shared retain/manual-owner policy OR dedicated actual shutdown proof",
                          "already Active approved capacity/SKU/workspace assignment",
                          "actual approved source assertions before models; new approval after ID binding",
                      ]}
    if args.command == "execute":
        summary = {key: report[key] for key in (
            "status", "phase", "proofLevel", "liveExecution", "approvalSha256",
        )}
        summary["lifecycle"] = {key: value for key, value in report["lifecycle"].items() if key != "owner"}
        print(json.dumps(summary, indent=2))
        return
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
