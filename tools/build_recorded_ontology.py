"""Project actual native metadata for display; omit target IDs and never create a reader grant."""

import argparse
import json
from pathlib import Path

from telco_iq.native_context import NativeSnapshot
from telco_iq.recorded_ontology import RecordedOntology


def project(snapshot: NativeSnapshot) -> dict:
    snapshot.verify(snapshot.context.scope)
    value = {
        "captured_at": snapshot.captured_at.isoformat(),
        "native_snapshot_hash": snapshot.content_hash,
        "provenance": "ACTUAL_NATIVE_DEFINITION_AND_METADATA_CAPTURE",
        "sources": sorted({entity["model"] for entity in snapshot.entities}),
        "entities": [{
            "id": entity["id"], "model": entity["model"], "table": entity["table"],
            "name": entity["backing_table"], "description": entity["description"],
            "keys": entity["keys"],
        } for entity in snapshot.entities],
        "relationships": [{
            "id": edge["id"], "source": edge["from_entity"], "target": edge["to_entity"],
            "description": edge["description"],
        } for edge in snapshot.native_relationships],
        "metrics": [{
            key: metric[key] for key in ("id", "model", "table", "entity", "name", "description", "representation")
        } for metric in snapshot.metrics],
        "rules": [{
            "id": rule["id"], "statement": rule["statement"],
            "referenced_entities": json.loads(rule["referenced_entities"]),
            "property_scope": rule["property_scope"],
        } for rule in snapshot.context.rules],
        "execution_relationships": {
            item.relationship.id: item.native_relationship for item in snapshot.execution_contracts
        },
        "limitations": [
            "Definition graph, not a materialized instance graph.",
            "Highlighted paths are only completed application operations with reviewed native relationship mappings.",
            "Formal Rules are captured metadata; listing does not prove managed ask_ontology consideration.",
            "Native scalar keys are not substituted for the complete source primary/validity grains.",
        ],
    }
    return RecordedOntology.model_validate(value).model_dump(mode="json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = NativeSnapshot.model_validate_json(args.snapshot.read_bytes())
    value = project(snapshot)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
    print(f"Actual native display projection: {len(value['entities'])} entities, "
          f"{len(value['relationships'])} relationships, {len(value['metrics'])} source proxies, "
          f"{len(value['rules'])} Rules. No source or model call.")


if __name__ == "__main__":
    main()
