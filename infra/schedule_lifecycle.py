"""Bind the published, inspected runbook to the explicitly approved finite deadline."""

import argparse
import hashlib
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--private-dir", type=Path, required=True)
parser.add_argument("--verify-only", action="store_true")
args = parser.parse_args()
private = args.private_dir.resolve()
root = Path(__file__).resolve().parents[1]
if private.is_relative_to(root) or "onedrive" in str(private).lower():
    raise SystemExit("Private state must remain outside repository and OneDrive")
a = json.loads((private / "lifecycle-approval.json").read_text(encoding="utf-8-sig"))
job = json.loads((private / "inspect-job-final.json").read_text(encoding="utf-8-sig"))
published = json.loads((private / "runbook-published-receipt.json").read_text(encoding="utf-8-sig"))
if not a.get("approved") or job["properties"]["status"] != "Completed" or not published["canonicalContentMatches"]:
    raise SystemExit("Approval, publication or successful Inspect prerequisite missing")
if hashlib.sha256((root / "infra" / "pause-capacity.ps1").read_bytes()).hexdigest() != a["runbookSha256"]:
    raise SystemExit("Approved runbook changed")
if datetime.fromisoformat(a["deadlineUtc"]) <= datetime.now(timezone.utc):
    raise SystemExit("Approved deadline already passed; do not silently extend it")
azure = shutil.which("az")
identity = json.loads(subprocess.check_output([
    azure, "account", "show", "--subscription", a["subscriptionId"], "-o", "json",
], text=True))
if identity["tenantId"] != a["tenantId"] or identity["user"]["name"] != a["authorUser"]:
    raise SystemExit("Author identity boundary mismatch")
token = json.loads(subprocess.check_output([
    azure, "account", "get-access-token", "--resource", "https://management.azure.com",
    "--tenant", a["tenantId"], "-o", "json",
], text=True))["accessToken"]
account = json.loads((private / "deployment-account.json").read_text(encoding="utf-8-sig"))
account_id = account["properties"]["outputs"]["accountId"]["value"]
schedule_name = "TelcoIQ-Deadline-20261001"
schedule_path = account_id + "/schedules/" + schedule_name
binding_id = str(uuid.uuid5(uuid.NAMESPACE_URL, account_id + schedule_name + a["runbookSha256"]))
binding_path = account_id + "/jobSchedules/" + binding_id
version = "?api-version=2024-10-23"
parameters = {
    "TargetResourceId": a["capacityResourceId"],
    "TargetSha256": hashlib.sha256(a["capacityResourceId"].lower().encode()).hexdigest(),
    "TenantId": a["tenantId"], "DeadlineUtc": a["deadlineUtc"], "Mode": "SuspendWhenDue",
}
schedule_body = {
    "name": schedule_name,
    "properties": {
        "description": "Explicitly approved shared F8 deadline, finite hourly retry window",
        "startTime": a["deadlineUtc"], "expiryTime": a["retryExpiryUtc"],
        "frequency": "Hour", "interval": 1, "timeZone": "UTC",
    },
}
binding_body = {
    "properties": {
        "runbook": {"name": a["runbookName"]}, "schedule": {"name": schedule_name},
        "parameters": parameters,
    },
}
with httpx.Client(base_url="https://management.azure.com", headers={"Authorization": "Bearer " + token},
                  timeout=60, follow_redirects=False) as client:
    for label, path, body in [
        ("schedule", schedule_path, schedule_body), ("job-binding", binding_path, binding_body),
    ]:
        existing = client.get(path + version)
        if args.verify_only:
            existing.raise_for_status()
        else:
            if existing.status_code != 404:
                raise RuntimeError(f"{label} target is not absent; refusing overwrite")
            result = client.put(path + version, json=body)
            result.raise_for_status()
            (private / f"{label}-created.json").write_text(json.dumps(result.json(), indent=2))
        result = client.get(path + version)
        result.raise_for_status()
        (private / f"{label}-readback.json").write_text(json.dumps(result.json(), indent=2))
    schedule = json.loads((private / "schedule-readback.json").read_text())["properties"]
    binding = json.loads((private / "job-binding-readback.json").read_text())["properties"]
    if not schedule["isEnabled"] or schedule["frequency"] != "Hour" or schedule["interval"] != 1:
        raise RuntimeError("Schedule state/recurrence differs from approval")
    for field, approved in [("startTime", a["deadlineUtc"]), ("expiryTime", a["retryExpiryUtc"])]:
        if datetime.fromisoformat(schedule[field]) != datetime.fromisoformat(approved):
            raise RuntimeError("Schedule timing differs from approval")
    # Automation normalizes TenantId to tenantId; PowerShell parameter binding is case-insensitive.
    actual_parameters = {key.casefold(): value for key, value in binding["parameters"].items()}
    expected_parameters = {key.casefold(): value for key, value in parameters.items()}
    if (
        len(actual_parameters) != len(binding["parameters"])
        or actual_parameters != expected_parameters
        or binding["runbook"]["name"] != a["runbookName"]
        or binding["schedule"]["name"] != schedule_name
    ):
        raise RuntimeError("Job binding differs from approved target/mode")
    print(json.dumps({
        "schedule": "ENABLED_AND_BOUND", "startTime": schedule["startTime"],
        "nextRun": schedule["nextRun"], "expiryTime": schedule["expiryTime"],
        "actualSuspension": "NOT_RUN", "scope": "Approved exact F8 capacity only",
    }))
