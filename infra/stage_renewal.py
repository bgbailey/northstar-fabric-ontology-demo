"""LOCAL ONLY: stage an unapproved proposal/schema or freeze a confirmed execution window.

No confirmation command, login, credential read, Graph call, certificate rotation,
cloud call or capacity resume is implemented here. 'window' only records UTC bounds;
it does NOT verify the separately approved shutdown schedule.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from infra.renewal import Approval, body_digest, sha256, stage_execution_window
from tools.build_hero_packet import read_packet

ROOT = Path(__file__).resolve().parents[1]


def stage(packet_dir: Path, output: Path, *, model_proposal: Path | None = None) -> dict:
    if output.exists():
        raise FileExistsError("Proposal output exists; stage a new revision instead of overwriting")
    packet_dir = packet_dir.resolve()
    packet = read_packet(packet_dir)
    model_proposal = model_proposal or ROOT / "infra" / "azure-openai-proposal.example.json"
    source_files = [packet_dir / "packet.json", *[
        packet_dir / entry["file"] for entry in packet["packetFiles"]
    ]]
    code_files = [ROOT / name for name in (
        "infra/fabric_p0.py", "infra/renewal.py", "infra/stage_renewal.py",
        "infra/hero_deploy.py", "infra/powerbi_refresh.py", "infra/refresh_p0_model.py",
        "tools/build_hero_packet.py", "tools/build_p0_packet.py", "infra/pause-capacity.ps1",
    )]
    artifacts = [{"file": str(path), "sha256": sha256(path.read_bytes())}
                 for path in [*source_files, *code_files]]
    m_reference = {
        "owner": "M lane", "file": str(model_proposal),
        "sha256": sha256(model_proposal.read_bytes()) if model_proposal.exists() else None,
        "selection": "NEW DEV Azure OpenAI deployment proposal; no existing deployment assumed",
        "approval": "SEPARATE_UNSELECTED", "status": "PRESENT" if model_proposal.exists() else "NOT_YET_STAGED",
    }
    if m_reference["sha256"]:
        artifacts.append({"file": m_reference["file"], "sha256": m_reference["sha256"]})
    authorization = {
        "schemaVersion": "fabric-renewal-v1", "approved": False, "confirmationReference": None,
        "tenantId": None, "subscriptionId": None, "authorUser": None, "workspaceId": None,
        "ownedItems": {}, "protectedItemIds": [],
        "artifacts": artifacts, "artifactSetSha256": body_digest(artifacts),
        "operations": [], "recoveryReceipts": [],
        "cost": {"resource": None, "currency": None, "unitPrice": None, "estimated": None, "cap": None,
                 "basis": "Select actual Fabric resource/rate, active time including retry tail, and approved cap"},
        "window": {
            "ttlSeconds": None, "minimumRemainingSeconds": None, "shutdownRetrySeconds": None,
            "latestStartUtc": None, "notAfterUtc": None,
        },
        "shutdown": {
            "approvalReference": None, "capacityResourceId": None, "scheduleResourceId": None,
            "runbookName": None, "runbookSha256": sha256((ROOT / "infra" / "pause-capacity.ps1").read_bytes()),
            "receiptFile": "renewal-shutdown-readback.json",
        },
        "teardown": {
            "owner": None, "mechanism": "Delete only new receipt-owned hero IDs and verify absence; retain old proof",
            "approvalReference": None, "ownedOnly": True,
        },
    }
    operation_plan = [{
        "name": "create-hero-lakehouse", "method": "POST",
        "path": "/v1/workspaces/<WORKSPACE_ID>/items",
        "template": "lakehouse-template.json",
        "bodySha256": body_digest(json.loads((packet_dir / "lakehouse-template.json").read_bytes())),
        "createsAlias": "hero-lakehouse", "createdDisplayName": packet["lakehouseName"],
        "allowLro": True,
    }]
    for alias, template, name, endpoint in [
        ("hero-loader", "notebook-template.json", packet["notebookName"], "items"),
        *((model["alias"], model["file"], model["displayName"], "semanticModels")
          for model in packet["modelPackets"]),
    ]:
        operation_plan.append({
            "name": "create-" + alias, "method": "POST",
            "path": "/v1/workspaces/<WORKSPACE_ID>/" + endpoint, "template": template,
            "templateSha256": sha256((packet_dir / template).read_bytes()),
            "bodySha256": None, "binding": "Private resolved body must be reviewed and rehashed before approval",
            "createsAlias": alias, "createdDisplayName": name, "allowLro": True,
        })
    proposal = {
        "schemaVersion": "renewal-proposal-v1", "approved": False, "status": "STAGED_BLOCKED_UNAPPROVED",
        "cloudMutations": 0, "paidCalls": 0, "credentialsRead": False,
        "sourcePacket": {"file": str(packet_dir / "packet.json"),
                         "sha256": sha256((packet_dir / "packet.json").read_bytes()),
                         "revision": packet["packetRevision"], "profile": packet["profile"]},
        "authorizationTemplate": authorization,
        "operationPlan": operation_plan,
        "heroCoordinator": {
            "plan": "python -m infra.hero_deploy plan --packet <packet-directory> --output <new-stage>",
            "execution": "No live execute/auth factory; HeroRunner accepts reviewed injected transports only",
            "initialScope": "Only new lakehouse create/readback in an exact accepted-receipt-owned workspace",
            "bindingBreakpoint": "BOUND_LOCALLY_NOT_APPROVED; initial create approval cannot authorize bound bytes",
            "loadScope": "Fresh immutable bound notebook/body/code approval; one accepted loader job",
            "modelScope": "Actual source assertion/job proof, complete definitions, separate Power BI audience",
            "sourceAssertionCapture": "Private reviewed Spark output/readback import; no invented stdout API",
        },
        "readbackPlan": [
            "Explicit exact-workspace GET listing (allowPagination only for continuationToken)",
            "Explicit GET item and POST getDefinition requests, including canonical empty-body digest",
            "Optional {create-alias} path segments bind ONLY to IDs from that approved accepted create",
            "LRO GET status derives from actual accepted Location/ID; result GET only if the status exposes Location",
            "Notebook job GET location derives ONLY from the actual accepted notebook receipt",
            "Expired-run LRO recovery requires separately reviewed recoveryReceipts file/hash; never another create",
            "Cancellation/deletion require their own exact allowlist entries; no blanket cleanup permission",
        ],
        "shutdownRenewal": {
            "approved": False, "status": "SEPARATE_APPROVAL_REQUIRED",
            "historicalLifecycleApprovalChanges": 0,
            "steps": [
                "Select exact capacity and Automation schedule/runbook/binding targets, costs and TTL",
                "Freeze one UTC execution window at actual preflight, under approved latestStart/notAfter bounds",
                "Stage and confirm a separate new schedule/binding artifact for that exact absolute deadline",
                "Execute only after separate approval; read back enabled schedule, binding, target and Inspect result",
                "Save actual fresh evidence; Renewal.verify_shutdown_guard() must pass immediately BEFORE resume",
                "Do not execute old schedule_lifecycle.py by editing its historical approval",
            ],
            "evidenceContract": {
                "observedAtUtc": "actual readback timestamp, after window start and at most 300 seconds old",
                "tenantId": "exact approved tenant", "subscriptionId": "exact approved subscription",
                "runbookSha256": "approved published runbook digest",
                "inspect": {"status": "Completed", "mutations": 0},
                "schedule": "actual ARM GET object: id and properties isEnabled/startTime/expiryTime/frequency/interval",
                "binding": "actual ARM GET object: id and properties schedule/runbook/parameters",
            },
            "resumeImplementedHere": False,
        },
        "bffCertificateRenewal": {
            "approved": False, "status": "SEPARATE_APPROVAL_REQUIRED",
            "targetApplicationId": None, "certificateNotAfterUtc": None,
            "steps": [
                "Select exact existing DEV BFF application/current expected user; inspect public certificate metadata",
                "Stage a new bounded certificate and exact Graph public-key update for separate confirmation",
                "Rotate only after approval; verify public key validity and retire the exact obsolete key at handoff",
                "Reauthenticate and verify delegated reader access separately; no automatic new consent",
            ],
            "warning": "Editing local expires_at alone does not renew the actual BFF certificate",
            "graphCalls": 0, "certificateRotation": "NOT_RUN",
        },
        "modelProviderProposal": m_reference,
        "blockers": [
            "Exact tenant/subscription/current expected DEV user/workspace and protected historical IDs unselected",
            "Actual resource, current unit price, cost estimate/cap and TTL are missing; no zero-cost claim",
            "Execution latest-start/absolute UTC bound/minimum remaining time/retry tail are unselected",
            "Bound notebook/model body digests and exact operation/readback/teardown scopes unconfirmed",
            "New shutdown schedule/binding approval and fresh actual guard evidence are absent",
            "Separate BFF certificate renewal approval/readback is absent; no Graph/certificate operation ran",
            "M-lane NEW DEV Azure OpenAI resource/model/deployment/price/budget approval remains separate",
        ],
        "evidenceGaps": packet["evidenceGaps"],
        "interfaces": {
            "author": "Author(private: Path, *, renewal_approval: Path | None = None)",
            "schema": "renewal-schema.json; Approval.model_json_schema(); proposal approved:false cannot execute",
            "bodyDigest": "SHA256 UTF-8 json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False)",
            "emptyBody": "Use digest of null for no JSON body; digest of {} is different",
            "window": "stage_execution_window(approval_path, private); exclusive persistent file; never shifts on retry",
            "resumeGuard": "Renewal(private, approval_path).verify_shutdown_guard(); before any separately approved resume",
            "binding": "tools.build_hero_packet.bind(packet_dir, binding, NEW_PRIVATE_OUTPUT)",
            "requests": "Only json and timeout kwargs accepted in renewed mode; put exact query in path",
            "noResubmit": "Private renewal-requests ledger reserves mutations before transport; accepted/unknown stay reserved",
            "recoveries": "recoveryReceipts[{file,sha256}] explicitly authorizes GET status/result only for those accepted HTTP receipts",
        },
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (("renewal-proposal.json", proposal), ("renewal-schema.json", Approval.model_json_schema())):
        (output / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return proposal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    proposal = commands.add_parser("proposal", help="Stage approved:false proposal with exact local packet hashes")
    proposal.add_argument("--packet", type=Path, required=True)
    proposal.add_argument("--output", type=Path, help="New local directory, never overwrite")
    proposal.add_argument("--model-proposal", type=Path, help="Reference M-lane proposal; do not duplicate it")
    window = commands.add_parser("window", help="LOCAL: freeze UTC window from a separately confirmed private approval")
    window.add_argument("--approval", type=Path, required=True)
    window.add_argument("--private-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "window":
        print(json.dumps(stage_execution_window(args.approval, args.private_dir), indent=2))
    else:
        output = args.output or ROOT / ".local" / (
            "renewal-proposal-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        )
        report = stage(args.packet, output, model_proposal=args.model_proposal)
        print(json.dumps({"proposal": str(output / "renewal-proposal.json"),
                          "status": report["status"], "blockers": report["blockers"]}, indent=2))


if __name__ == "__main__":
    main()
