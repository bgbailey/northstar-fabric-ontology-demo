"""Stage a disposable API round-trip of the observed source-proxy representation; no cloud calls."""

import argparse
import base64
import copy
import hashlib
import json
import uuid
from pathlib import Path

from infra.inspect_native_ontology import definition_census
from telco_iq.approval import private_config_path

ROOT = Path(__file__).resolve().parents[1]
NAME = "TelcoIQ_P0_ProxyRoundtrip"


def clone_request(definition: dict, name: str, logical_id: str) -> dict:
    parts = copy.deepcopy(definition["definition"]["parts"])
    platform_parts = [part for part in parts if part["path"] == ".platform"]
    if len(platform_parts) != 1:
        raise ValueError("Expected one native platform part")
    platform = platform_parts[0]
    metadata = json.loads(base64.b64decode(platform["payload"], validate=True))
    if metadata["metadata"]["type"] != "Ontology" or metadata["metadata"]["displayName"] == name:
        raise ValueError("Disposable clone must be a distinct ontology")
    metadata["metadata"]["displayName"] = name
    metadata["config"]["logicalId"] = str(uuid.UUID(logical_id))
    platform["payload"] = base64.b64encode(json.dumps(metadata).encode()).decode()
    return {"displayName": name, "type": "Ontology", "definition": {"parts": parts}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True)
    args = parser.parse_args()
    private = private_config_path(args.private_dir, ROOT)
    definition = json.loads((private / "native-ontology-definition-readback.json").read_text(encoding="utf-8"))
    census = definition_census(definition)
    if (
        not census["compatibility_1000000"] or census["source_proxy_measures"] != 16
        or census["entity_parts"] != 13 or census["entity_relationships"] != 21
        or census["metric_parts"] != 0
    ):
        raise ValueError("Source definition is not the observed two-model source-proxy shape")
    request = clone_request(definition, NAME, str(uuid.uuid4()))
    content = (json.dumps(request, indent=2) + "\n").encode()
    target = private / "native-roundtrip-request.json"
    if target.exists():
        raise FileExistsError("A round-trip packet already exists; do not silently change the approved revision")
    with target.open("xb") as handle:
        handle.write(content)
    report = {
        "status": "STAGED_UNAPPROVED", "cloudMutations": 0,
        "targetName": NAME, "requestSha256": hashlib.sha256(content).hexdigest(),
        "sourceDefinitionSha256": hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest(),
        "sourceCensus": census,
        "operations": [
            "Create one disposable generation-2 ontology using this exact complete definition",
            "Read its definition and compare source proxies, model links, entities and relationships",
            "Read its entities and rules using the two discovered native MCP tools",
            "Delete only the disposable item ID returned by this create after recording the result",
        ],
        "originalOntologyChanges": 0, "sourceChanges": 0, "agentRequests": 0,
        "maximumElapsedMinutes": 15,
        "expiry": "Existing approved lab deadline; no extension",
        "cost": "Existing shared F8 and operational cap; no new resource tier or paid model provider",
        "limitation": "A local payload is not proof of successful service round-trip or metric UI preservation.",
    }
    (private / "native-roundtrip-proposal.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
