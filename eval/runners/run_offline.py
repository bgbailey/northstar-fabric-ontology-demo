"""Compare the real local executor with independent facts and persist evaluator-only gold."""

import asyncio
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("independent_reference", ROOT / "eval" / "oracle" / "reference.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


async def run():
    data = ROOT / "data" / "generated" / "miniature"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8765", timeout=60) as client:
        response = await client.get("/api/health")
        response.raise_for_status()
        health = response.json()
        nodes = [{
            "id": key, "kind": "QuerySemanticModel", "model": model, "table": table, "metrics": [metric],
            "group_by": ["account_key"], "filters": [{"column": "month", "op": "eq", "values": ["2026-08-01"]}],
            "date_role": "month", "expected_grain": ["account_key"],
        } for key, model, table, metric in [
            ("lines", "subscriptions", "subscription_month", "subscriptions.active_lines_eom"),
            ("revenue", "finance", "account_month", "finance.recognized_service_revenue"),
        ]]
        nodes.append({
            "id": "answer", "kind": "JoinResults", "left": "lines", "right": "revenue",
            "relationship": "account-month-conformed-v1", "left_keys": ["account_key"], "right_keys": ["account_key"],
            "cardinality": "one_to_one", "unmatched": "error",
        })
        plan = {
            "version": "1.0", "request_id": "persisted-offline-vertical-slice",
            "question": "August active lines and recognized service revenue by account",
            "principal_scope": health["scope"], "as_of": "2026-09-29T00:00:00Z",
            "ontology_revision": health["context_revision"],
            "metric_selections": [n["metrics"][0] for n in nodes[:2]], "nodes": nodes, "result_node": "answer",
        }
        response = await client.post("/api/execute", json=plan, headers={"X-Telco-Local": "submitted-plan"})
        response.raise_for_status()
        result = response.json()
    expected = reference.two_model(data)
    if result["status"] != "COMPLETE" or result["answer"]["rows"] != expected:
        raise AssertionError({"observed": result, "expected": expected})
    output = ROOT / ".local" / "evaluation"
    output.mkdir(parents=True, exist_ok=True)
    receipt = {
        "lane": "OFFLINE_SYNTHETIC",
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "status": "PASS", "source_query_receipts": result["receipts"],
        "observed": result["answer"]["rows"], "independent_expected": expected,
        "independent_oracles": {
            "Q01": reference.q01(data), "Q03": reference.q03(data),
            "Q08": reference.q08(data), "Q14": reference.q14(data),
        },
        "limitations": [
            "Q03/Q08/Q14 source oracle outputs exist; not full live planner executions.",
            "No Fabric DAX, native ontology, live identity or comparative-accuracy claim.",
        ],
    }
    (output / "offline-oracle-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({
        "status": receipt["status"], "rows": len(expected), "query_receipts": len(result["receipts"]),
        "elapsed_ms": result["elapsed_ms"], "receipt": str(output / "offline-oracle-receipt.json"),
    }))


if __name__ == "__main__":
    asyncio.run(run())
