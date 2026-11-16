"""Author-only, approval-bound P0 deployment. Runtime never imports this module."""

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


class Author:
    def __init__(self, private: Path, *, renewal_approval: Path | None = None):
        self.private = private.resolve()
        if self.private.is_relative_to(ROOT) or "onedrive" in str(self.private).lower():
            raise RuntimeError("Private deployment state must be outside source/OneDrive")
        self.renewal = None
        if renewal_approval is not None:
            if __package__:
                from .renewal import Renewal
            else:
                from renewal import Renewal
            self.renewal = Renewal(self.private, renewal_approval)
        historical = self.private / "p0-approval.json"
        self.approval = load(historical) if historical.exists() else {}
        if self.renewal:
            approved = self.renewal.approval
            self.approval = self.approval | {
                "tenantId": approved.tenantId, "subscriptionId": approved.subscriptionId,
                "authorUser": approved.authorUser, "deadlineUtc": self.renewal.deadline.isoformat(),
            }
        else:
            if self.approval.get("approved") is not True:
                raise RuntimeError("P0 approval absent")
            if datetime.fromisoformat(self.approval["deadlineUtc"]) <= datetime.now(timezone.utc):
                raise RuntimeError("Approved lab deadline elapsed")
        self.azure = shutil.which("az")
        identity = self.az_json("account", "show", "--subscription", self.approval["subscriptionId"])
        if (
            identity["tenantId"] != self.approval["tenantId"]
            or identity["user"]["name"] != self.approval["authorUser"]
            or (self.renewal and identity.get("id") != self.approval["subscriptionId"])
            or (self.renewal and identity["user"].get("type") != "user")
        ):
            raise RuntimeError("DEV author identity boundary mismatch")
        token = self.az_json(
            "account", "get-access-token", "--resource", "https://api.fabric.microsoft.com",
            "--tenant", self.approval["tenantId"],
        )["accessToken"]
        self.client = httpx.Client(
            base_url="https://api.fabric.microsoft.com",
            headers={"Authorization": "Bearer " + token}, timeout=60, follow_redirects=False,
        )

    def az_json(self, *arguments):
        result = subprocess.run(
            [self.azure, *arguments, "--output", "json", "--only-show-errors"],
            capture_output=True, text=True,
        )
        if result.returncode:
            raise RuntimeError("Author CLI operation failed; no credential fallback")
        return json.loads(result.stdout)

    def save(self, name, value):
        base = self.private.resolve()
        path = (base / str(name).replace("\\", "/")).resolve()
        if not path.is_relative_to(base) or path == base:
            raise RuntimeError("Author receipt escaped its private state directory")
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def request(self, method, path, *, skill="spark-cli", **kwargs):
        method = method.upper()
        try:
            from infra.renewal import body_digest, fabric_path
        except ModuleNotFoundError:
            from renewal import body_digest, fabric_path
        path = fabric_path(path)
        renewal = getattr(self, "renewal", None)
        ticket = renewal.begin(method, path, kwargs) if renewal else None
        started = datetime.now(timezone.utc).isoformat()
        for attempt in range(3):
            try:
                if renewal:
                    renewal.check_current()
                    if attempt:
                        renewal._budget()
                response = self.client.request(
                    method, "https://api.fabric.microsoft.com" + path,
                    headers={"x-ms-fabric-skill": skill}, **kwargs,
                )
                break
            except (httpx.ConnectError, httpx.ReadTimeout) as error:
                self.save("transport-" + uuid.uuid4().hex + ".json", {
                    "startedAt": started, "method": method, "path": urlparse(path).path,
                    "attempt": attempt + 1, "errorType": type(error).__name__,
                })
                if method != "GET" or attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = {"format": "UNEXPECTED_NON_JSON", "text": response.text[:2000]}
        else:
            body = None
        self.save("http-" + uuid.uuid4().hex + ".json", {
            "startedAt": started, "method": method, "path": urlparse(path).path,
            "status": response.status_code,
            "requestId": response.headers.get("x-ms-request-id"),
            "operationId": response.headers.get("x-ms-operation-id"),
            "retryAfter": response.headers.get("Retry-After"), "response": body,
            "location": response.headers.get("Location"),
            "bodySha256": body_digest(kwargs.get("json")),
            "approvalSha256": renewal.digest if renewal else None,
        })
        if renewal:
            renewal.observe(ticket, method, path, response, body)
        if response.status_code >= 400:
            raise RuntimeError(f"Fabric {method} {response.status_code}: {json.dumps(body)}")
        return response

    def listing(self, path, *, skill="spark-cli"):
        result, cursor, seen = [], None, set()
        for _ in range(100):
            target = path + ("&" if "?" in path else "?") + urlencode({"continuationToken": cursor}) if cursor else path
            page = self.request("GET", target, skill=skill).json()
            result.extend(page["value"])
            cursor = page.get("continuationToken")
            if not cursor:
                return result
            if cursor in seen:
                raise RuntimeError("Repeated Fabric continuation token")
            seen.add(cursor)
        raise RuntimeError("Pagination budget exceeded")

    def completed(self, response, *, skill="spark-cli", return_result=True):
        if response.status_code in (200, 201):
            return response.json() if response.content else None
        if response.status_code != 202:
            raise RuntimeError("Unexpected create/definition result status")
        try:
            from infra.renewal import LroPolling, operation_location, operation_result, receipt_location
        except ModuleNotFoundError:
            from renewal import LroPolling, operation_location, operation_result, receipt_location
        renewal = getattr(self, "renewal", None)
        policy = renewal.approval.lroPolling if renewal else LroPolling()
        accepted = {
            "operationId": response.headers.get("x-ms-operation-id"),
            "location": response.headers.get("Location"),
        }
        path = receipt_location(accepted, policy)
        stop = time.monotonic() + 300
        while time.monotonic() < stop:
            delay = int(response.headers.get("Retry-After", "5"))
            if delay < 0:
                raise RuntimeError("Invalid Retry-After")
            renewal = getattr(self, "renewal", None)
            if renewal:
                renewal.check_current()
                if (renewal.deadline - datetime.now(timezone.utc)).total_seconds() <= delay:
                    raise TimeoutError("Retry-After exceeds approved execution window")
            if time.monotonic() + delay > stop:
                raise TimeoutError("Retry-After exceeds operation deadline")
            time.sleep(delay)
            response = self.request("GET", path, skill=skill)
            state = response.json()
            if (
                response.status_code != 200 or not isinstance(state, dict)
                or state.get("status") not in {"NotStarted", "Running", "Succeeded", "Failed", "Cancelled"}
                or any(state.get(key) is not None for key in ("error", "errors", "errorCode"))
                or any(state.get(key) is not None and state.get(key) is not False for key in ("partial", "isPartial"))
                or response.headers.get("x-ms-operation-id", accepted["operationId"]) != accepted["operationId"]
            ):
                raise RuntimeError("LRO state/status/header is unknown or incomplete")
            if state["status"] in ("Failed", "Cancelled"):
                raise RuntimeError("Fabric operation failed: " + json.dumps(state))
            if state["status"] == "Succeeded":
                if not return_result:
                    return state
                location = response.headers.get("Location")
                if not location and policy.strategy == "RETURNED_LOCATION":
                    raise RuntimeError("LRO completed without an exposed result; stop/read-only reconcile")
                result_path = operation_result(accepted, response, state, policy)
                result = self.request("GET", result_path, skill=skill)
                if result.status_code != 200 or result.headers.get(
                    "x-ms-operation-id", accepted["operationId"],
                ) != accepted["operationId"]:
                    raise RuntimeError("LRO result status/operation header mismatch")
                body = result.json() if result.content else None
                if not isinstance(body, dict) or any(
                    body.get(key) is not None for key in ("error", "errors", "errorCode")
                ) or any(body.get(key) is not None and body.get(key) is not False for key in ("partial", "isPartial")):
                    raise RuntimeError("LRO result is an error/partial/unknown body")
                return body
            if response.headers.get("Location"):
                operation_location(accepted, response.headers["Location"], policy)
        raise TimeoutError("Fabric operation pending; inspect recorded operation, never duplicate it")

    def owned(self, key):
        return load(self.private / f"p0-{key}.json")

    def create_item(self, key, body, *, endpoint="items", skill="spark-cli"):
        if (self.private / f"p0-{key}.json").exists():
            item = self.owned(key)
            workspace = self.owned("workspace")["id"]
            observed = self.request(
                "GET", f"/v1/workspaces/{workspace}/items/{item['id']}", skill=skill,
            ).json()
            if observed["displayName"] != body["displayName"]:
                raise RuntimeError("Owned item changed; stop instead of overwriting")
            return item
        if (self.private / f"p0-{key}-accepted.json").exists():
            raise RuntimeError("Item creation already accepted; recover its receipt/LRO, never resubmit")
        workspace = self.owned("workspace")["id"]
        items = self.listing(f"/v1/workspaces/{workspace}/items", skill=skill)
        if any(i["displayName"] == body["displayName"] for i in items):
            raise RuntimeError("Item name already exists; no overwrite authorized")
        accepted = self.request(
            "POST", f"/v1/workspaces/{workspace}/{endpoint}", skill=skill, json=body,
        )
        self.save(f"p0-{key}-accepted.json", {
            "status": accepted.status_code, "operationId": accepted.headers.get("x-ms-operation-id"),
            "location": accepted.headers.get("Location"), "retryAfter": accepted.headers.get("Retry-After"),
            "requestSha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
        })
        item = self.completed(accepted, skill=skill)
        if not item or not item.get("id"):
            raise RuntimeError("Creation has no item identity; do not infer ownership by name")
        self.save(f"p0-{key}.json", item)
        readback = self.request("GET", f"/v1/workspaces/{workspace}/items/{item['id']}", skill=skill).json()
        if readback["displayName"] != body["displayName"]:
            raise RuntimeError("Created item readback name differs")
        self.save(f"p0-{key}-readback.json", readback)
        return item

    def resources(self):
        a = self.approval
        capacities = self.listing("/v1/capacities")
        capacity = next(c for c in capacities if c["id"] == a["capacityId"])
        if capacity["state"] != "Active" or capacity["sku"] != "F8":
            raise RuntimeError("Approved capacity not Active F8; no resume/resize authorized")
        if any(w["displayName"] == a["workspaceName"] for w in self.listing("/v1/workspaces")):
            raise RuntimeError("Workspace already exists; no overwrite/adoption authorized")
        workspace = self.request("POST", "/v1/workspaces", json={
            "displayName": a["workspaceName"], "capacityId": a["capacityId"],
            "description": "Synthetic Telco IQ P0; owned lab; deadline " + a["deadlineUtc"],
        }).json()
        self.save("p0-workspace.json", workspace)
        readback = self.request("GET", "/v1/workspaces/" + workspace["id"]).json()
        if readback.get("capacityId") != a["capacityId"]:
            raise RuntimeError("New workspace not assigned to approved capacity")
        self.save("p0-workspace-readback.json", readback)
        lakehouse = self.create_item("lakehouse", {
            "displayName": a["lakehouseName"], "type": "Lakehouse",
            "creationPayload": {"enableSchemas": True},
        })
        print(json.dumps({"workspace": workspace["displayName"], "lakehouse": lakehouse["displayName"],
                          "status": "CREATED_AND_READ_BACK"}))

    def data_load(self):
        a = self.approval
        repair_path = self.private / "p0-repair-approval.json"
        repair = load(repair_path) if repair_path.exists() else None
        expected_hash, max_runs = a["notebookSha256"], a["maxNotebookRuns"]
        if repair:
            if (
                repair.get("approved") is not True or repair["originalNotebookSha256"] != expected_hash
                or repair["notebookId"] != self.owned("notebook")["id"]
                or repair["maximumTotalRuns"] != 2
            ):
                raise RuntimeError("Repair approval does not match the owned notebook/one extra run")
            expected_hash, max_runs = repair["revisedNotebookSha256"], repair["maximumTotalRuns"]
        content = (ROOT / ".local" / "p0-packet" / "Load_TelcoIQ_Miniature.ipynb").read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise RuntimeError("Notebook differs from approved template")
        workspace, lakehouse = self.owned("workspace"), self.owned("lakehouse")
        notebook = json.loads(content)
        notebook["metadata"]["dependencies"] = {"lakehouse": {
            "default_lakehouse": lakehouse["id"],
            "default_lakehouse_workspace_id": workspace["id"],
            "default_lakehouse_name": lakehouse["displayName"],
        }}
        self.save("p0-bound-notebook.ipynb", notebook)
        item = self.create_item("notebook", {
            "displayName": a["notebookName"], "type": "Notebook",
            "definition": {"format": "ipynb", "parts": [{
                "path": "notebook-content.ipynb", "payloadType": "InlineBase64",
                "payload": base64.b64encode(json.dumps(notebook).encode()).decode(),
            }]},
        })
        prefix = f"/v1/workspaces/{workspace['id']}/items/{item['id']}"
        history = self.listing(prefix + "/jobs/instances")
        if len(history) >= max_runs or any(j["status"] in ("NotStarted", "InProgress") for j in history):
            raise RuntimeError("Notebook run count/active job exceeds explicit approval")
        if repair:
            current = self.completed(self.request(
                "POST", f"/v1/workspaces/{workspace['id']}/notebooks/{item['id']}/getDefinition?format=ipynb",
                json={},
            ))
            previous = load(self.private / "p0-notebook-service.ipynb")
            part = next(p for p in current["definition"]["parts"] if p["path"].endswith(".ipynb"))
            if json.loads(base64.b64decode(part["payload"])) != previous:
                raise RuntimeError("Notebook changed since diagnosis; do not overwrite concurrent changes")
            part["payload"] = base64.b64encode(json.dumps(notebook).encode()).decode()
            updated = self.request(
                "POST", f"/v1/workspaces/{workspace['id']}/notebooks/{item['id']}/updateDefinition",
                json={"definition": {"format": "ipynb", "parts": current["definition"]["parts"]}},
            )
            self.completed(updated, return_result=False)
            self.save("p0-notebook-repair-applied.json", {"approvedTemplateSha256": expected_hash})
        response = self.request("POST", prefix + "/jobs/RunNotebook/instances")
        if response.status_code != 202 or not response.headers.get("Location"):
            raise RuntimeError("Notebook submission ambiguous; no retry")
        location = response.headers["Location"]
        if urlparse(location).hostname != "api.fabric.microsoft.com":
            raise RuntimeError("Unexpected notebook job host")
        self.save("p0-notebook-job-submitted.json", {"location": location, "itemId": item["id"]})
        stop = time.monotonic() + a["maxNotebookMinutes"] * 60
        while time.monotonic() < stop:
            delay = int(response.headers.get("Retry-After", "15"))
            if time.monotonic() + delay >= stop:
                break
            time.sleep(delay)
            response = self.request("GET", location)
            state = response.json()
            self.save("p0-notebook-job-latest.json", state)
            if state["status"] == "Completed":
                self.save("p0-notebook-load-success.json", state)
                print(json.dumps({"notebook": "Completed", "sourceReadbackAssertions": "PASSED",
                                  "note": "Notebook assertions execute in real Fabric Spark"}))
                return
            if state["status"] in ("Failed", "Cancelled", "Deduped"):
                raise RuntimeError("Notebook did not complete: " + json.dumps(state))
        cancelled = self.request("POST", location.rstrip("/") + "/cancel")
        self.save("p0-notebook-cancel-requested.json", {"status": cancelled.status_code, "location": location})
        raise TimeoutError("Notebook exceeded approved ten-minute window; cancellation requested")

    def models(self, model_names=("subscriptions", "finance")):
        if not (self.private / "p0-notebook-load-success.json").exists():
            raise RuntimeError("Verified actual Delta load required before model creation")
        wid, lid = self.owned("workspace")["id"], self.owned("lakehouse")["id"]
        uuid.UUID(wid)
        uuid.UUID(lid)
        for model in model_names:
            content = (ROOT / ".local" / "p0-packet" / f"{model}-template.json").read_bytes()
            if hashlib.sha256(content).hexdigest() != self.approval["modelTemplateHashes"][model]:
                raise RuntimeError("Model differs from approved template")
            body = json.loads(content)
            for part in body["definition"]["parts"]:
                text = base64.b64decode(part["payload"]).decode()
                text = text.replace("<WORKSPACE_ID>", wid).replace("<LAKEHOUSE_ID>", lid)
                part["payload"] = base64.b64encode(text.encode()).decode()
            self.save(f"p0-{model}-bound-definition.json", body)
            item = self.create_item(
                model, body, endpoint="semanticModels", skill="semantic-model-authoring",
            )
            print(json.dumps({"model": item["displayName"], "status": "CREATED_AND_READ_BACK"}), flush=True)

    def ontology(self):
        a = self.approval
        item = self.create_item("ontology", {
            "displayName": a["ontologyName"],
            "description": "Disposable generation-2 availability experiment; no claimed source metrics",
        }, endpoint="ontologies", skill="fabriciq-ontology-cli")
        wid = self.owned("workspace")["id"]
        metadata = self.request(
            "GET", f"/v1/workspaces/{wid}/ontologies/{item['id']}", skill="fabriciq-ontology-cli",
        ).json()
        self.save("p0-ontology-generation.json", metadata)
        definition = self.completed(self.request(
            "POST", f"/v1/workspaces/{wid}/ontologies/{item['id']}/getDefinition",
            skill="fabriciq-ontology-cli", json={},
        ), skill="fabriciq-ontology-cli")
        self.save("p0-ontology-definition.json", definition)
        print(json.dumps({"generation": metadata.get("properties", {}).get("generation"),
                          "parts": [p["path"] for p in definition["definition"]["parts"]],
                          "sourceMetrics": "NOT_GENERATED"}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument(
        "--renewal-approval", type=Path,
        help="Separate private confirmed operation allowlist; never extends historical P0 approval",
    )
    parser.add_argument("--phase", choices=["resources", "load", "models", "ontology"], required=True)
    parser.add_argument("--model", choices=["subscriptions", "finance"])
    args = parser.parse_args()
    author = Author(args.private_dir, renewal_approval=args.renewal_approval)
    try:
        {"resources": author.resources, "load": author.data_load,
         "models": lambda: author.models([args.model] if args.model else ("subscriptions", "finance")),
         "ontology": author.ontology}[args.phase]()
    finally:
        author.client.close()


if __name__ == "__main__":
    main()
