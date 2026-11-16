"""Author-only publishing of the specifically approved lifecycle runbook; no Fabric data access."""

import argparse
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--private-dir", type=Path, required=True)
args = parser.parse_args()
private = args.private_dir.resolve()
root = Path(__file__).resolve().parents[1]
if private.is_relative_to(root) or "onedrive" in str(private).lower():
    raise SystemExit("Private receipts must be outside source and OneDrive")
approval = json.loads((private / "lifecycle-approval.json").read_text(encoding="utf-8-sig"))
if approval.get("approved") is not True:
    raise SystemExit("Explicit lifecycle approval absent")
source = (root / "infra" / "pause-capacity.ps1").read_bytes()
if hashlib.sha256(source).hexdigest() != approval["runbookSha256"]:
    raise SystemExit("Runbook differs from approved bytes")
azure = shutil.which("az")
if not azure:
    raise SystemExit("Azure CLI unavailable")
identity = json.loads(subprocess.check_output([
    azure, "account", "show", "--subscription", approval["subscriptionId"], "-o", "json",
], text=True))
if identity["tenantId"] != approval["tenantId"] or identity["user"]["name"] != approval["authorUser"]:
    raise SystemExit("Author identity boundary mismatch")
token = json.loads(subprocess.check_output([
    azure, "account", "get-access-token", "--resource", "https://management.azure.com",
    "--tenant", approval["tenantId"], "-o", "json",
], text=True))["accessToken"]
account = json.loads((private / "deployment-account.json").read_text(encoding="utf-8-sig"))
account_id = account["properties"]["outputs"]["accountId"]["value"]
base = "https://management.azure.com"
version = "?api-version=2024-10-23"
runbook = account_id + "/runbooks/" + approval["runbookName"]


def save(name, value):
    (private / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


with httpx.Client(headers={"Authorization": "Bearer " + token}, timeout=60, follow_redirects=False) as client:
    def request(method, path, **kwargs):
        url = path if path.startswith("https://") else base + path
        if urlparse(url).hostname != "management.azure.com":
            raise RuntimeError("Refusing to forward author credentials off ARM")
        response = client.request(method, url, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"ARM {method} failed ({response.status_code}): {response.text}")
        return response

    def finish(response):
        if response.status_code != 202:
            return
        location = response.headers.get("Azure-AsyncOperation") or response.headers.get("Location")
        if not location:
            raise RuntimeError("Accepted operation has no poll URL; no blind resubmission")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            delay = int(response.headers.get("Retry-After", "5"))
            if time.monotonic() + delay > deadline:
                raise TimeoutError("Server retry delay exceeds bounded operation deadline")
            time.sleep(delay)
            response = request("GET", location)
            value = response.json() if response.content else {}
            status = value.get("status") if isinstance(value, dict) else None
            if status in ("Failed", "Canceled", "Cancelled"):
                raise RuntimeError(f"ARM operation terminated: {status}")
            if status == "Succeeded" or (response.status_code in (200, 204) and status is None):
                return
        raise TimeoutError("ARM operation not confirmed")

    existing = client.get(base + runbook + version)
    if existing.status_code != 404:
        raise RuntimeError("Runbook target is not absent; inspect before any overwrite")
    created = request("PUT", runbook + version, json={
        "name": approval["runbookName"], "location": approval["location"],
        "properties": {
            "runbookType": "PowerShell", "draft": {}, "logProgress": False, "logVerbose": False,
            "description": "Approved Telco IQ capacity deadline; exact target hash and tenant enforced",
        },
        "tags": {"project": "telco-iq-ontology-lab", "approvedArtifact": approval["codeRevision"]},
    })
    save("runbook-create-receipt.json", created.json())
    content = request("PUT", runbook + "/draft/content" + version,
                      content=source, headers={"Content-Type": "text/plain"})
    finish(content)
    finish(request("POST", runbook + "/publish" + version))
    metadata = request("GET", runbook + version).json()
    if metadata["properties"]["state"] != "Published":
        raise RuntimeError("Runbook is not published")
    published = request("GET", runbook + "/content" + version)
    content_text = published.json() if "application/json" in published.headers.get("Content-Type", "") else published.text
    if not isinstance(content_text, str) or content_text.replace("\r\n", "\n") != source.decode().replace("\r\n", "\n"):
        raise RuntimeError("Published runbook differs from approved source")
    save("runbook-published-receipt.json", {
        "metadata": metadata, "canonicalContentMatches": True,
        "publishedContentSha256": hashlib.sha256(content_text.encode()).hexdigest(),
    })
    job_id = str(uuid.uuid4())
    parameters = {
        "TargetResourceId": approval["capacityResourceId"],
        "TargetSha256": hashlib.sha256(approval["capacityResourceId"].lower().encode()).hexdigest(),
        "TenantId": approval["tenantId"], "DeadlineUtc": approval["deadlineUtc"], "Mode": "Inspect",
    }
    job = account_id + "/jobs/" + job_id
    started = request("PUT", job + version, json={
        "properties": {"runbook": {"name": approval["runbookName"]}, "parameters": parameters},
    }).json()
    save("inspect-job-created.json", started)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        state = request("GET", job + version).json()
        status = state["properties"]["status"]
        if status in ("Completed", "Failed", "Stopped", "Suspended", "Blocked"):
            save("inspect-job-final.json", state)
            output = request("GET", job + "/output" + version).text
            (private / "inspect-job-output.txt").write_text(output, encoding="utf-8")
            if status != "Completed" or "INSPECTED" not in output:
                raise RuntimeError(f"Inspect job did not verify runtime: {status}; private output retained")
            print(json.dumps({"runbook": "Published", "canonicalReadback": "MATCH",
                              "inspectJob": status, "output": output}))
            break
        time.sleep(10)
    else:
        raise TimeoutError("Inspect job pending; use persisted job ID, never duplicate submission")
