"""Read-only DEV author diagnostics; never an application reader credential fallback."""

import argparse
import asyncio
import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from infra.fabric_p0 import Author
from telco_iq.auth import DelegatedBearer
from telco_iq.evidence import write_receipt
from telco_iq.mcp_client import (
    DiscoveredClient,
    Endpoint,
    load_ontology_snapshot,
    snapshot_endpoint_review,
)

SKILL = "fabriciq-ontology-cli"


def definition_census(definition: dict) -> dict:
    parts = definition["definition"]["parts"]
    paths = [part["path"] for part in parts]
    if len(paths) != len(set(paths)):
        raise ValueError("Ontology definition contains duplicate part paths")
    decoded = {
        part["path"]: base64.b64decode(part["payload"], validate=True).decode("utf-8-sig")
        for part in parts
        if part["payloadType"] == "InlineBase64"
    }
    if len(decoded) != len(parts):
        raise ValueError("Ontology definition includes an unsupported payload type")
    tables = [text for path, text in decoded.items() if path.startswith("tables/")]
    return {
        "parts": len(parts),
        "compatibility_1000000": any(
            line.strip() == "compatibilityLevel: 1000000"
            for line in decoded.get("database.tmdl", "").splitlines()
        ),
        "entity_parts": sum(path.startswith("entities/") for path in paths),
        "metric_parts": sum(path.startswith("metrics/") for path in paths),
        "rule_parts": sum(path.startswith("rules/") for path in paths),
        "entity_relationship_part": "entityRelationships.tmdl" in paths,
        "entity_relationships": len(re.findall(
            r"^entityRelationship ", decoded.get("entityRelationships.tmdl", ""), re.MULTILINE,
        )),
        "source_proxy_measures": sum(
            len(re.findall(r"^\tmeasure .+ = EXTERNALMEASURE\(", text, re.MULTILINE)) for text in tables
        ),
        "source_model_names": sorted({
            match.removesuffix("\r") for text in tables
            for match in re.findall(r"^\s*annotation ONT_ItemName = (.+)$", text, re.MULTILINE)
        }),
        "guidance_annotations": sum(
            len(re.findall(r"^\s*annotation rule_\w+ =", text, re.MULTILINE)) for text in decoded.values()
        ),
        "limitation": (
            "Observed definition census. Top-level metric parts and source-proxy table measures are distinct; "
            "neither counts nor guidance annotations prove numeric execution or native Rule objects."
        ),
    }


async def inspect(author: Author, read_context: bool = False) -> dict:
    workspace = author.owned("workspace")
    ontology = author.owned("ontology")
    observed = author.request(
        "GET", f"/v1/workspaces/{workspace['id']}", skill=SKILL,
    ).json()
    if observed["displayName"] != workspace["displayName"]:
        raise RuntimeError("Owned workspace no longer matches the recorded target")
    items = author.listing(
        f"/v1/workspaces/{workspace['id']}/items?type=Ontology", skill=SKILL,
    )
    matches = [item for item in items if item["id"] == ontology["id"]]
    if len(matches) != 1 or matches[0]["displayName"] != ontology["displayName"]:
        raise RuntimeError("Owned ontology no longer matches the recorded target")
    definition = author.completed(author.request(
        "POST", f"/v1/workspaces/{workspace['id']}/items/{ontology['id']}/getDefinition",
        skill=SKILL, json={},
    ), skill=SKILL)
    author.save("native-ontology-definition-readback.json", definition)
    definition_path = write_receipt(author.private / "native-ontology-definitions", definition)
    census = definition_census(definition)
    endpoint = Endpoint(
        f"https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/{workspace['id']}"
        f"/items/{ontology['id']}/ontologyEndpoint",
        "ontology",
    )
    token = author.az_json(
        "account", "get-access-token", "--resource", "https://api.fabric.microsoft.com",
        "--tenant", author.approval["tenantId"],
    )["accessToken"]
    snapshot = load_ontology_snapshot(
        Path(__file__).resolve().parents[1] / "fabric/ontology/mcp-tools.snapshot.json",
    ) if read_context else None
    reviewed_hashes, reviewed_args = (
        snapshot_endpoint_review(
            snapshot, "ontology_item_endpoint", capabilities=("list_entities", "list_rules"),
        )
        if snapshot else (None, {})
    )
    context = {}
    async with DiscoveredClient.connect(
        endpoint, auth=DelegatedBearer(token),
        reviewed_hashes=reviewed_hashes,
    ) as client:
        inventory = client.inventory()
        if snapshot:
            for capability, fallback in (
                ("list_entities", "list_ontology_entities"),
                ("list_rules", "list_ontology_rules"),
            ):
                name = (
                    client.resolve_ontology_tool(capability)
                    if hasattr(client, "resolve_ontology_tool") else fallback
                )
                result = await client.call(name, reviewed_args.get(name, {}))
                context[name] = result.model_dump(mode="json")
    receipt = {
        "lane": "LIVE_DEV_AUTHOR_DIAGNOSTIC",
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "operations": ["GetWorkspace", "ListOntologyItems", "GetDefinition", "MCPInitialize", "MCPToolsList",
                       *context],
        "cloudMutations": 0,
        "census": census,
        "definitionReceipt": definition_path.name,
        "inventory": inventory,
        "rawNativeContext": context,
        "limitations": [
            "Author diagnostic, not application reader authentication.",
            "No native agent execution or source query was performed by this script.",
            "Raw context is not yet normalized; no source-backed metric proof is inferred from discovery.",
        ],
    }
    path = write_receipt(author.private / "native-ontology-receipts", receipt)
    return {
        "lane": receipt["lane"], "cloudMutations": 0, "census": census,
        "protocol": inventory["protocol"],
        "context_read_tools": list(context),
        "tools": [{"name": item["definition"]["name"], "schema_hash": item["schema_hash"]}
                  for item in inventory["tools"]],
        "privateReceipt": str(path),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--read-context", action="store_true", help="Call only the two reviewed native read tools")
    args = parser.parse_args()
    author = Author(args.private_dir)
    try:
        print(json.dumps(asyncio.run(inspect(author, args.read_context)), indent=2))
    finally:
        author.client.close()


if __name__ == "__main__":
    main()
