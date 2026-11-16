"""Convert actual private native captures into a display-only, identifier-free demo packet."""

import argparse
import base64
import json
import re
from pathlib import Path

from telco_iq.approval import private_config_path
from telco_iq.catalog import digest
from telco_iq.recorded_demo import RecordedDemo

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def tmdl_name(value):
    value = value.strip()
    if value.startswith("'"):
        if not value.endswith("'"):
            raise ValueError("Unsupported TMDL identifier")
        return value[1:-1].replace("''", "'")
    return value


def relationship_rows(text):
    result = []
    current = None
    for line in text.splitlines():
        if line.startswith("entityRelationship "):
            current = {"id": tmdl_name(line[len("entityRelationship "):]), "guidance": []}
            result.append(current)
        elif current is not None:
            field = line.strip()
            if field.startswith("fromEntity: "):
                current["source"] = tmdl_name(field[len("fromEntity: "):])
            elif field.startswith("toEntity: "):
                current["target"] = tmdl_name(field[len("toEntity: "):])
            elif re.match(r"annotation rule(?:_\w+)? = ", field):
                current["guidance"].append(field.split(" = ", 1)[1])
    if any("source" not in edge or "target" not in edge for edge in result):
        raise ValueError("Native relationship shape changed; do not invent endpoints")
    return result


def build_packet(definition, context, captured, question, models):
    if context["lane"] != "LIVE_DEV_AUTHOR_DIAGNOSTIC" or captured["lane"] != "LIVE_DEV_NATIVE_AGENT_UI":
        raise ValueError("Only actual native capture lanes can produce recorded-live content")
    parts = {
        part["path"]: base64.b64decode(part["payload"], validate=True).decode("utf-8-sig")
        for part in definition["definition"]["parts"] if part["payloadType"] == "InlineBase64"
    }
    if len(parts) != len(definition["definition"]["parts"]):
        raise ValueError("Duplicate paths or unsupported native payload type")
    raw = context["rawNativeContext"]
    for name in ("list_ontology_entities", "list_ontology_rules"):
        if raw[name]["isError"] or set(raw[name]["structuredContent"]) != {"values"}:
            raise ValueError("Native MCP result changed or failed; re-review its shape")
    by_name = {model["displayName"]: key for key, model in models.items()}
    entities = []
    for entity in raw["list_ontology_entities"]["structuredContent"]["values"]:
        annotations = {item["name"]: item["value"] for item in entity["annotations"]}
        if annotations.get("source_model") not in by_name or entity["additionalBackingTables"]:
            raise ValueError("Source binding is outside the reviewed two-model capture shape")
        entities.append({
            "id": entity["name"], "source_id": by_name[annotations["source_model"]],
            "backing_table": entity["backingTable"], "description": entity["description"],
            "keys": entity["keyProperties"], "properties": [prop["name"] for prop in entity["properties"]],
            "guidance": [value for key, value in annotations.items() if key.startswith("rule_")],
        })
    definition_entities = {
        tmdl_name(match) for path, text in parts.items() if path.startswith("entities/")
        for match in re.findall(r"^entity (.+)$", text, re.MULTILINE)
    }
    if definition_entities != {entity["id"] for entity in entities}:
        raise ValueError("MCP entities and the captured definition do not agree")
    metrics = []
    for path, text in parts.items():
        if not path.startswith("tables/"):
            continue
        table, = re.findall(r"^table (.+)$", text, re.MULTILINE)
        table = tmdl_name(table)
        for line in text.splitlines():
            if not re.match(r"\s*annotation measure_\S+ = ", line):
                continue
            link = json.loads(line.split(" = ", 1)[1])
            source = by_name[link["n"]]
            if link["i"] != models[source]["id"]:
                raise ValueError("Metric model name and item identity disagree")
            if not re.search(
                r'EXTERNALMEASURE\("' + re.escape(link["m"]) + r'", [A-Z]+, "DirectQuery to AS - '
                + re.escape(link["w"] + "/" + link["i"]) + r'"\)', text,
            ):
                raise ValueError("Native metric metadata is not backed by the captured source proxy")
            homes = [e for e in entities if e["source_id"] == source and e["backing_table"] == table]
            if len(homes) != 1:
                raise ValueError("Metric home entity is ambiguous or absent")
            metrics.append({
                "id": f"{source}:{link['t']}:{link['m']}", "entity_id": homes[0]["id"],
                "source_id": source, "home_table": link["t"], "name": link["m"], "description": link["d"],
            })
    if captured["table"]["headers"] != ["account_key", "active_lines_eom", "recognized_service_revenue_cents"]:
        raise ValueError("Unreviewed native answer columns")
    rows = []
    for row in captured["table"]["rows"]:
        if len(row) != 3 or not all(re.fullmatch(r"-?\d+", value) for value in row):
            raise ValueError("Recorded answer must preserve exact integer values; blank is not zero")
        rows.append(dict(zip(captured["table"]["headers"], map(int, row), strict=True)))
    queries = []
    for dax in captured["sourceQueriesDisplayed"]:
        candidates = [
            source for source, table in (("subscriptions", "Subscription Month"), ("finance", "Account Month"))
            if f"'{table}'[" in dax
        ]
        if len(candidates) != 1:
            raise ValueError("Captured source query cannot be assigned to one reviewed source")
        queries.append({"source_id": candidates[0], "dax": dax})
    if {query["source_id"] for query in queries} != set(models) or len(queries) != 2:
        raise ValueError("The two-source recorded answer requires both displayed DAX queries")
    return RecordedDemo.model_validate({
        "captured_at": captured["completedAt"], "context_captured_at": context["completedAt"],
        "question": question, "sources": [{"id": key, "name": value["displayName"]} for key, value in models.items()],
        "entities": entities, "relationships": relationship_rows(parts["entityRelationships.tmdl"]),
        "metrics": metrics, "native_rule_count": len(raw["list_ontology_rules"]["structuredContent"]["values"]),
        "focus_entity_ids": ["Subscription Month", "Subscription Accounts", "Finance Accounts", "Account Month"],
        "queries": queries, "rows": rows,
        "source_hashes": {"definition": digest(definition), "native_mcp": digest(context), "native_answer": digest(captured)},
        "limitations": [
            "Recorded native-agent answer and API/MCP metadata, not a live application query.",
            "Graph is the native definition. Its account-path overlay explains the recorded answer; it is not an internal execution trace or an instance graph.",
            "Business guidance is captured as annotations; native Rule objects are counted separately.",
            "No paired GA IQ baseline was captured. No comparative accuracy or performance advantage is claimed.",
            "Only captured rows are displayed. This viewer does not rerun source completeness checks.",
        ],
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--context-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    private = private_config_path(args.private_dir, ROOT)
    context_path = private_config_path(args.context_receipt, ROOT)
    context = load(context_path)
    definition_name = context["definitionReceipt"]
    if Path(definition_name).name != definition_name:
        raise ValueError("Definition receipt must be one captured filename")
    definition = load(private / "native-ontology-definitions" / definition_name)
    models = {key: load(private / f"p0-{key}.json") for key in ("subscriptions", "finance")}
    packet = build_packet(
        definition, context, load(private / "native-agent-query-result.json"),
        load(private / "native-agent-query-submitted.json")["prompt"], models,
    )
    content = packet.model_dump_json(indent=2) + "\n"
    approval = load(private / "p0-approval.json")
    identifiers = [approval[key] for key in ("tenantId", "subscriptionId", "authorUser", "capacityId")]
    identifiers += [item["id"] for item in models.values()]
    identifiers += [load(private / f"p0-{key}.json")["id"] for key in ("workspace", "ontology", "lakehouse")]
    if any(value.lower() in content.lower() for value in identifiers):
        raise ValueError("Private deployment identifier remains in the display packet")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(json.dumps({"mode": packet.mode, "entities": len(packet.entities), "relationships": len(packet.relationships),
                      "metrics": len(packet.metrics), "rows": len(packet.rows), "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
