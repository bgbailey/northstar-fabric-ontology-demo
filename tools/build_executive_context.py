"""Project actual small-item TMDL/MCP into display metadata; never create a reader grant or business answer."""

import argparse
import base64
import json
import re
from pathlib import Path

from telco_iq.approval import private_config_path
from telco_iq.catalog import Catalog, digest
from telco_iq.recorded_ontology import RecordedOntology
from tools.build_recorded_demo import relationship_rows, tmdl_name

ROOT = Path(__file__).resolve().parents[1]


def project(definition: dict, metadata: dict, models: dict, catalog: Catalog) -> RecordedOntology:
    if metadata["lane"] != "LIVE_DEV_AUTHOR_DIAGNOSTIC" or set(models) != {"subscriptions", "finance"}:
        raise ValueError("Actual separate two-source author metadata is required, not serialized reader authority")
    entries = definition["definition"]["parts"]
    parts = {
        part["path"]: base64.b64decode(part["payload"], validate=True).decode("utf-8-sig")
        for part in entries if part["payloadType"] == "InlineBase64"
    }
    if len(parts) != len(entries):
        raise ValueError("Actual definition parts must be complete, unique and decoded")
    raw = metadata["rawNativeContext"]
    for tool in ("list_ontology_entities", "list_ontology_rules"):
        if raw[tool]["isError"] or set(raw[tool]["structuredContent"]) != {"values"}:
            raise ValueError("Unknown or failed native metadata cannot produce a display projection")
    by_id = {model["id"]: alias for alias, model in models.items()}
    tables = {}
    links = []
    for path, text in parts.items():
        if not path.startswith("tables/"):
            continue
        declared, = re.findall(r"^table (.+)$", text, re.MULTILINE)
        name = tmdl_name(declared)
        item_ids = re.findall(r"^\s+annotation ONT_ItemId = (.+)$", text, re.MULTILINE)
        source_tables = re.findall(r"^\s+entityName: (.+)$", text, re.MULTILINE)
        if len(item_ids) != 1 or item_ids[0].strip() not in by_id or len(source_tables) != 1:
            raise ValueError("Actual source-backed table identity is absent or ambiguous")
        alias = by_id[item_ids[0].strip()]
        source_name = tmdl_name(source_tables[0])
        homes = [table for table in catalog.models[alias]["tables"]
                 if catalog.tables[table]["displayName"] == source_name]
        if len(homes) != 1:
            raise ValueError("Actual native backing table is outside its original semantic model")
        tables[name] = (alias, homes[0], text)
        for line in text.splitlines():
            if line.strip().startswith("annotation measure_") and " = " in line:
                links.append((name, json.loads(line.split(" = ", 1)[1])))
    entities = []
    for entity in raw["list_ontology_entities"]["structuredContent"]["values"]:
        if entity["additionalBackingTables"] or entity["backingTable"] not in tables:
            raise ValueError("Unreviewed additional or absent native backing table")
        alias, table, _ = tables[entity["backingTable"]]
        entities.append({
            "id": entity["name"], "model": alias, "table": table,
            "name": catalog.tables[table]["displayName"], "description": entity["description"],
            "keys": entity["keyProperties"],
        })
    if len(entities) != 4 or len({entity["id"] for entity in entities}) != 4:
        raise ValueError("The actual executive item must contain its four unique native entities")
    metrics = []
    for backing, link in links:
        alias, table, text = tables[backing]
        if link["i"] != models[alias]["id"] or link["n"] != models[alias]["displayName"] or (
            link["w"] != models[alias]["workspaceId"] or link["t"] != catalog.tables[table]["displayName"]
        ):
            raise ValueError("Actual metric home/source metadata disagrees with its backing table")
        selected = [(identifier, metric) for identifier, metric in catalog.metrics.items()
                    if (metric["model"], metric["table"], metric["name"]) == (alias, table, link["m"])]
        if len(selected) != 1:
            raise ValueError("Actual native proxy is not an existing approved source measure")
        connection = "DirectQuery to AS - " + link["w"] + "/" + link["i"]
        if not re.search(
            r'EXTERNALMEASURE\("' + re.escape(link["m"]) + r'", [A-Z]+, "' + re.escape(connection) + r'"\)', text,
        ):
            raise ValueError("The actual native source link lacks its original EXTERNALMEASURE proxy")
        owners = [entity for entity in entities if (entity["model"], entity["table"]) == (alias, table)]
        if len(owners) != 1:
            raise ValueError("Actual source metric entity is missing or ambiguous")
        metrics.append({
            "id": selected[0][0], "model": alias, "table": table, "entity": owners[0]["id"],
            "name": link["m"], "description": link["d"], "representation": "EXTERNALMEASURE",
        })
    rules = []
    for rule in raw["list_ontology_rules"]["structuredContent"]["values"]:
        if rule["referencedRelationships"] or any(
            reference["propertyScope"] != "None" for reference in rule["referencedEntities"]
        ):
            raise ValueError("Only the actually reviewed entity-only native Rule shape is projected")
        rules.append({
            "id": rule["name"], "statement": rule["statement"], "property_scope": "None",
            "referenced_entities": [reference["entity"] for reference in rule["referencedEntities"]],
        })
    return RecordedOntology.model_validate({
        "captured_at": metadata["completedAt"],
        "native_snapshot_hash": digest({"definition": definition, "native_metadata": metadata}),
        "provenance": "ACTUAL_NATIVE_DEFINITION_AND_METADATA_CAPTURE",
        "sources": sorted(models), "entities": entities,
        "relationships": [
            {"id": edge["id"], "source": edge["source"], "target": edge["target"], "description": ""}
            for edge in relationship_rows(parts["entityRelationships.tmdl"])
        ],
        "metrics": metrics, "rules": rules, "execution_relationships": {},
        "limitations": [
            "Separate actual small native definition, not a filtered Customer360 snapshot or an instance graph.",
            "All generated original source proxies are retained; the executive presentation focuses on three measures.",
            "Captured linked Rules do not prove managed consideration or deterministic enforcement.",
            "No application execution traversal, current reader grant or business answer is created by this projection.",
        ],
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-directory", type=Path, required=True)
    parser.add_argument("--source-models", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    directory = private_config_path(args.private_directory, ROOT)
    models = json.loads(private_config_path(args.source_models, ROOT).read_bytes())
    models = {alias: models[alias] for alias in ("subscriptions", "finance")}
    value = project(
        json.loads((directory / "actual-small-definition.json").read_bytes()),
        json.loads((directory / "actual-native-metadata.json").read_bytes()), models,
        Catalog(private_config_path(args.catalog, ROOT)),
    )
    text = value.model_dump_json(indent=2)
    identifiers = {model[key] for model in models.values() for key in ("id", "workspaceId")}
    if any(identifier.casefold() in text.casefold() for identifier in identifiers):
        raise ValueError("A private source/workspace identifier remains in the display projection")
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(text)
    print(json.dumps({
        "status": "ACTUAL_SMALL_NATIVE_DISPLAY_PROJECTION_ONLY",
        "entities": len(value.entities), "relationships": len(value.relationships),
        "source_proxies": len(value.metrics), "formal_Rules": len(value.rules),
        "business_source_queries_or_model_calls": 0,
    }))


if __name__ == "__main__":
    main()
