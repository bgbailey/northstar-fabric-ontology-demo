"""Compare a captured native-agent answer and source metric links with independent local facts."""

import argparse
import base64
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from infra.inspect_native_ontology import definition_census
from telco_iq.approval import private_config_path
from telco_iq.catalog import digest
from telco_iq.evidence import write_receipt

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True)
    args = parser.parse_args()
    private = private_config_path(args.private_dir, ROOT)

    def load(path):
        return json.loads(path.read_text(encoding="utf-8-sig"))

    captured = load(private / "native-agent-query-result.json")
    if captured["lane"] != "LIVE_DEV_NATIVE_AGENT_UI":
        raise ValueError("Requires an actual native-agent result, not a fixture answer")
    if captured["table"]["headers"] != ["account_key", "active_lines_eom", "recognized_service_revenue_cents"]:
        raise ValueError("Native answer table shape changed")
    observed = []
    for row in captured["table"]["rows"]:
        if len(row) != 3 or not all(re.fullmatch(r"-?\d+", cell) for cell in row):
            raise ValueError("Native result must contain exact integer keys, counts and cents")
        key, lines, cents = map(int, row)
        observed.append({
            "account_key": key, "subscriptions.active_lines_eom": lines,
            "finance.recognized_service_revenue": cents,
        })
    spec = importlib.util.spec_from_file_location("native_independent_oracle", ROOT / "eval/oracle/reference.py")
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    expected = oracle.two_model(ROOT / "data/fixtures/miniature")
    if sorted(observed, key=lambda row: row["account_key"]) != expected:
        raise AssertionError("Native answer or account population differs from independent source facts")
    if len(captured["sourceQueriesDisplayed"]) != 2:
        raise ValueError("Both actual displayed source queries must be captured")

    definition = load(private / "native-ontology-definition-readback.json")
    catalog = load(ROOT / "data/contracts/catalog.json")
    models = {key: load(private / f"p0-{key}.json") for key in ("subscriptions", "finance")}
    by_id = {item["id"]: key for key, item in models.items()}
    workspace = load(private / "p0-workspace.json")["id"]
    tables = {table["id"]: table["displayName"] for table in catalog["tables"]}
    expected_links = {
        (metric["model"], tables[metric["table"]], metric["name"])
        for metric in catalog["metrics"] if metric["model"] in models
    }
    links = []
    for part in definition["definition"]["parts"]:
        if not part["path"].startswith("tables/"):
            continue
        text = base64.b64decode(part["payload"], validate=True).decode("utf-8-sig")
        proxies = re.findall(r'EXTERNALMEASURE\("([^"]+)", [A-Z]+, "([^"]+)"\)', text)
        for line in text.splitlines():
            if not re.match(r"\s*annotation measure_\S+ = ", line):
                continue
            link = json.loads(line.split(" = ", 1)[1])
            if link["w"] != workspace or link["i"] not in by_id:
                raise AssertionError("Native metric points outside the approved two-model sources")
            model = by_id[link["i"]]
            if link["n"] != models[model]["displayName"]:
                raise AssertionError("Native source display name and item identity disagree")
            if (link["m"], f"DirectQuery to AS - {workspace}/{link['i']}") not in proxies:
                raise AssertionError("Native metric metadata has no matching source-proxy expression")
            links.append((model, link["t"], link["m"]))
    if len(links) != len(set(links)) or set(links) != expected_links:
        raise AssertionError("Native metric identities/home tables differ from the source catalogue")

    receipt = {
        "lane": "LIVE_DEV_NATIVE_AGENT_UI_WITH_API_MCP_READBACK",
        "status": "EXACT_MATCH", "completedAt": datetime.now(timezone.utc).isoformat(),
        "capturedResultHash": digest(captured),
        "definitionHash": digest(definition),
        "census": definition_census(definition),
        "sourceMetricLinks": sorted(links), "observed": observed,
        "sourceQueriesDisplayed": captured["sourceQueriesDisplayed"],
        "accountRows": len(observed),
        "activeLinesEom": sum(row["subscriptions.active_lines_eom"] for row in observed),
        "recognizedServiceRevenueCents": sum(row["finance.recognized_service_revenue"] for row in observed),
        "populationCheck": "Every account and value exactly matches the independent miniature source-fact oracle",
        "limitations": [
            "Built-in native-agent question, not the custom application or GA IQ comparison.",
            "Source DAX is captured from the native response; no hidden native execution internals are inferred.",
            "Business guidance is stored as annotations; native Rule object count is zero.",
            "One two-model question does not demonstrate the three multihop customer stories or an accuracy advantage.",
        ],
    }
    path = write_receipt(private / "native-evaluation", receipt)
    public = ROOT / "eval/validation/observations/native-two-model-reconciliation.json"
    public.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": receipt["status"], "rows": len(observed),
        "activeLinesEom": receipt["activeLinesEom"],
        "recognizedServiceRevenueCents": receipt["recognizedServiceRevenueCents"],
        "sourceMetricLinks": len(links), "privateReceipt": str(path),
    }, indent=2))


if __name__ == "__main__":
    main()
