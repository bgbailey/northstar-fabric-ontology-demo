"""Approval-bound disposable ontology API round-trip; never updates the working ontology."""

import argparse
import asyncio
import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from infra.fabric_p0 import Author, load
from infra.inspect_native_ontology import SKILL, definition_census
from telco_iq.auth import DelegatedBearer
from telco_iq.evidence import write_receipt
from telco_iq.mcp_client import (
    DiscoveredClient,
    Endpoint,
    load_ontology_snapshot,
    snapshot_endpoint_review,
)

ROOT = Path(__file__).resolve().parents[1]


def non_platform_parts(definition: dict) -> dict[str, str]:
    return {
        part["path"]: base64.b64decode(part["payload"], validate=True)
        .decode("utf-8-sig").replace("\r\n", "\n").strip()
        for part in definition["definition"]["parts"] if part["path"] != ".platform"
    }


async def probe(author: Author) -> dict:
    private = author.private
    approval = load(private / "native-roundtrip-approval.json")
    if not approval["approved"] or datetime.now(timezone.utc) >= datetime.fromisoformat(approval["deadlineUtc"]):
        raise RuntimeError("Disposable API-test approval absent or expired")
    payload = (private / approval["requestFile"]).read_bytes()
    if hashlib.sha256(payload).hexdigest() != approval["requestSha256"]:
        raise RuntimeError("Request differs from the approved clone payload")
    body = json.loads(payload)
    workspace = author.owned("workspace")["id"]
    original = author.owned("ontology")["id"]
    if (
        workspace != approval["workspaceId"] or body["displayName"] != approval["targetName"]
        or approval["tenantId"] != author.approval["tenantId"]
    ):
        raise RuntimeError("Disposable test target mismatch")
    marker = private / "native-roundtrip-create-attempt.json"
    owned_path = private / "p0-ontology-roundtrip.json"
    if marker.exists() or owned_path.exists():
        raise RuntimeError("Create was already attempted; recover its operation rather than resubmit")
    capacity = next(c for c in author.listing("/v1/capacities", skill=SKILL)
                    if c["id"] == author.approval["capacityId"])
    if capacity["state"] != "Active" or capacity["sku"] != "F8":
        raise RuntimeError("Approved capacity is not Active F8; this probe cannot resume or resize it")
    started = time.monotonic()

    def definition(item):
        return author.completed(author.request(
            "POST", f"/v1/workspaces/{workspace}/items/{item}/getDefinition", skill=SKILL, json={},
        ), skill=SKILL)

    before = definition(original)
    if non_platform_parts(before) != non_platform_parts({"definition": body["definition"]}):
        raise RuntimeError("Working ontology changed since packet staging; fresh review required")
    result = {
        "lane": "LIVE_DEV_DISPOSABLE_REST_ROUNDTRIP",
        "status": "FAILED_OR_INCOMPLETE", "cleanup": "NOT_CREATED",
        "requestSha256": approval["requestSha256"], "agentRequests": 0,
        "originalOntologyMutations": 0,
    }
    item = None
    try:
        with marker.open("x", encoding="utf-8") as handle:
            json.dump({"startedAt": datetime.now(timezone.utc).isoformat(),
                       "requestSha256": approval["requestSha256"]}, handle)
        result["cleanup"] = "CREATION_OUTCOME_UNKNOWN"
        item = author.create_item("ontology-roundtrip", body, skill=SKILL)
        if item["id"] == original:
            raise RuntimeError("Create unexpectedly returned original item identity; deletion forbidden")
        result["cleanup"] = "PENDING"
        observed = definition(item["id"])
        definition_path = write_receipt(private / "native-roundtrip-definitions", observed)
        expected_parts, actual_parts = non_platform_parts(before), non_platform_parts(observed)
        result["changedParts"] = sorted(
            key for key in expected_parts.keys() | actual_parts.keys()
            if expected_parts.get(key) != actual_parts.get(key)
        )
        result["census"] = definition_census(observed)
        result["definitionReceipt"] = definition_path.name
        result["exactNonPlatformMatch"] = not result["changedParts"]
        if time.monotonic() - started >= approval["maximumElapsedMinutes"] * 60:
            raise TimeoutError("Test budget elapsed; cleanup the owned clone")
        snapshot = load_ontology_snapshot(ROOT / "fabric/ontology/mcp-tools.snapshot.json")
        reviewed_hashes, reviewed_args = snapshot_endpoint_review(
            snapshot, "ontology_item_endpoint", capabilities=("list_entities", "list_rules"),
        )
        token = author.az_json(
            "account", "get-access-token", "--resource", "https://api.fabric.microsoft.com",
            "--tenant", approval["tenantId"],
        )["accessToken"]
        endpoint = Endpoint(
            f"https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/{workspace}"
            f"/items/{item['id']}/ontologyEndpoint", "ontology",
        )
        async with DiscoveredClient.connect(
            endpoint, auth=DelegatedBearer(token), reviewed_hashes=reviewed_hashes,
        ) as client:
            raw = {}
            for capability, fallback in (
                ("list_entities", "list_ontology_entities"),
                ("list_rules", "list_ontology_rules"),
            ):
                name = (
                    client.resolve_ontology_tool(capability)
                    if hasattr(client, "resolve_ontology_tool") else fallback
                )
                raw[name] = (await client.call(name, reviewed_args.get(name, {}))).model_dump(mode="json")
        context_path = write_receipt(private / "native-roundtrip-context", raw)
        result["contextReceipt"] = context_path.name
        result["originalUnchanged"] = non_platform_parts(definition(original)) == expected_parts
        result["status"] = "PASS" if result["exactNonPlatformMatch"] and result["originalUnchanged"] else "FAIL"
    finally:
        try:
            if item is None and owned_path.exists():
                item = load(owned_path)
            if item is not None and item["id"] != original:
                outcome = result["status"]
                result["status"] = "CLEANUP_PENDING"
                if item["displayName"] != approval["targetName"]:
                    raise RuntimeError("Returned clone name differs; no deletion authorized")
                response = author.request(
                    "DELETE", f"/v1/workspaces/{workspace}/items/{item['id']}", skill=SKILL,
                )
                if response.status_code == 202:
                    author.completed(response, skill=SKILL, return_result=False)
                for _ in range(6):
                    remaining = author.listing(f"/v1/workspaces/{workspace}/items?type=Ontology", skill=SKILL)
                    if all(entry["id"] != item["id"] for entry in remaining):
                        result["cleanup"] = "DELETED_VERIFIED"
                        result["status"] = outcome
                        break
                    time.sleep(5)
                else:
                    raise RuntimeError("Clone deletion not verified; inspect owned item, no broader cleanup")
        finally:
            result["completedAt"] = datetime.now(timezone.utc).isoformat()
            result["elapsedSeconds"] = time.monotonic() - started
            receipt = write_receipt(private / "native-roundtrip-receipts", result)
            print(json.dumps(result | {"privateReceipt": str(receipt)}, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True)
    args = parser.parse_args()
    author = Author(args.private_dir)
    try:
        result = asyncio.run(probe(author))
        if result["status"] != "PASS" or result["cleanup"] != "DELETED_VERIFIED":
            raise RuntimeError("Disposable round-trip did not preserve the definition and verify cleanup")
    finally:
        author.client.close()


if __name__ == "__main__":
    main()
