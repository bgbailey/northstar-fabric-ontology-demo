"""Real source DAX + completeness controls + shared deterministic join + independent oracle.

This is a labelled author-only REST diagnostic, not MCP or a browser identity proof.
"""

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx

from telco_iq.catalog import Catalog, digest
from telco_iq.context import offline_context
from telco_iq.contracts import Artifact, Clocks, JoinNode, QueryNode, Scope, Status
from telco_iq.executor import join

ROOT = Path(__file__).resolve().parents[2]


def json_file(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def rows(payload):
    if "error" in payload or "errors" in payload:
        raise RuntimeError("Source returned an error, not empty data")
    result, = payload["results"]
    if "error" in result or "errors" in result:
        raise RuntimeError("Source result is partial or failed")
    table, = result["tables"]
    if "error" in table or "errors" in table:
        raise RuntimeError("Source table is partial or failed")
    return table["rows"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", type=Path, required=True)
    args = parser.parse_args()
    private = args.private_dir.resolve()
    if private.is_relative_to(ROOT) or "onedrive" in str(private).lower():
        raise RuntimeError("Private live/evaluator receipts must stay outside source and OneDrive")
    approval = json_file(private / "p0-approval.json")
    if not approval.get("approved") or datetime.fromisoformat(approval["deadlineUtc"]) <= datetime.now(timezone.utc):
        raise RuntimeError("P0 approval absent or expired")
    azure = shutil.which("az")
    identity = json.loads(subprocess.check_output([
        azure, "account", "show", "--subscription", approval["subscriptionId"], "-o", "json",
    ], text=True))
    if identity["tenantId"] != approval["tenantId"] or identity["user"]["name"] != approval["authorUser"]:
        raise RuntimeError("DEV author identity mismatch")
    token = json.loads(subprocess.check_output([
        azure, "account", "get-access-token", "--resource", "https://analysis.windows.net/powerbi/api",
        "--tenant", approval["tenantId"], "-o", "json",
    ], text=True))["accessToken"]
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    workspace = json_file(private / "p0-workspace.json")["id"]
    sources, trace, source_states = {}, [], {}
    stamp = datetime.now(timezone.utc).isoformat()
    with httpx.Client(headers={"Authorization": "Bearer " + token}, timeout=45, follow_redirects=False) as client:
        for model, table, metric in [
            ("subscriptions", "subscription_month", "subscriptions.active_lines_eom"),
            ("finance", "account_month", "finance.recognized_service_revenue"),
        ]:
            item = json_file(private / f"p0-{model}.json")["id"]
            endpoint = f"https://api.powerbi.com/v1.0/myorg/datasets/{item}/executeQueries"
            history_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace}/datasets/{item}/refreshes"
            before = client.get(history_url, params={"$top": 10})
            before.raise_for_status()
            node = QueryNode(
                id=model, kind="QuerySemanticModel", model=model, table=table,
                metrics=[metric], group_by=["account_key"],
                filters=[{"column": "month", "op": "eq", "values": ["2026-08-01"]}],
                date_role="month", expected_grain=["account_key"], max_rows=1000,
            )
            query = catalog.dax(node)
            # This evaluator's input is the fixed, inspected fixture query, not arbitrary planner text.
            expression = query.split("\n", 1)[1].rsplit("\nORDER BY ", 1)[0]
            control = 'EVALUATE ROW("__row_count", COUNTROWS(' + expression + "))"
            start = perf_counter()
            response = client.post(endpoint, json={
                "queries": [{"query": query}], "serializerSettings": {"includeNulls": True},
            })
            response.raise_for_status()
            returned = rows(response.json())
            count_response = client.post(endpoint, json={
                "queries": [{"query": control}], "serializerSettings": {"includeNulls": True},
            })
            count_response.raise_for_status()
            control_rows = rows(count_response.json())
            count = control_rows[0]["[__row_count]"]
            after = client.get(history_url, params={"$top": 10})
            after.raise_for_status()
            if before.json() != after.json():
                raise RuntimeError("Source refresh state changed during retrieval; no total claim")
            if count != len(returned) or count > node.max_rows:
                raise RuntimeError("Source control count does not prove complete bounded output")
            name = catalog.tables[table]["displayName"]
            normalized = [{
                "account_key": row[f"{name}[account_key]"], metric: row[f"[{metric}]"],
            } for row in returned]
            sources[model] = (table, metric, normalized)
            source_states[model] = digest(after.json())
            trace.append({
                "modelAlias": model, "lane": "POWER_BI_REST_DIAGNOSTIC_NOT_MCP",
                "query": query, "controlQuery": control, "rows": len(returned), "sourceControlRows": count,
                "elapsedMsIncludingControlAndStateCheck": (perf_counter() - start) * 1000,
                "requestId": response.headers.get("requestid"),
                "sourceRefreshStateHash": source_states[model],
                "completeness": "Source COUNTROWS control matched; refresh history unchanged",
            })
    scope = Scope(
        tenant="APPROVED_LAB_ALIAS", principal="VERIFIED_DEV_AUTHOR",
        authorization_revision="author-only-diagnostic", models=list(sources), mode="LIVE_DELEGATED",
    )
    clocks = Clocks(
        data=json_file(ROOT / "data" / "fixtures" / "miniature" / "manifest.json")["content_sha256"],
        model=digest({"definitions": approval["modelTemplateHashes"], "refreshes": source_states}),
        ontology="NOT_USED", context="approved-supplemental-identity-contract",
    )
    artifacts = {}
    for model, (table, metric, data) in sources.items():
        artifacts[model] = Artifact(
            id=model, rows=data, status=Status.COMPLETE, scope=scope, clocks=clocks,
            source_models=[model], source_tables=[table], grain=["account_key"],
            aggregation={metric: catalog.metrics[metric]["aggregation"]},
            evidence_ids=[digest(next(t for t in trace if t["modelAlias"] == model))],
        )
    context = offline_context(scope)
    combined = join(JoinNode(
        id="reconciled", kind="JoinResults", left="subscriptions", right="finance",
        relationship="account-month-conformed-v1", left_keys=["account_key"], right_keys=["account_key"],
        cardinality="one_to_one", unmatched="error",
    ), artifacts["subscriptions"], artifacts["finance"], context)
    spec = importlib.util.spec_from_file_location("independent_live_oracle", ROOT / "eval" / "oracle" / "reference.py")
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    expected = oracle.two_model(ROOT / "data" / "fixtures" / "miniature")
    if combined.rows != expected:
        raise AssertionError("Actual source DAX/join differs from independent source-fact oracle")
    receipt = {
        "observedAt": stamp, "status": "PASS", "lane": "POWER_BI_REST_DIAGNOSTIC_NOT_MCP",
        "identityMode": "CLI_DELEGATED_AUTHOR_NOT_BROWSER_OBO",
        "contextMode": "SUPPLEMENTAL_APP_MAPPING_NOT_NATIVE_ONTOLOGY",
        "queries": trace, "observed": combined.rows, "independentExpected": expected,
        "coverage": combined.coverage,
        "limits": "Single miniature author case. No reader RLS/OLS, live MCP, native context or accuracy-benefit claim.",
    }
    payload = json.dumps(receipt, sort_keys=True, indent=2)
    name = "p0-live-oracle-" + hashlib.sha256(payload.encode()).hexdigest()[:16] + ".json"
    (private / name).write_text(payload + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "EXACT_MATCH", "rows": len(combined.rows),
        "activeLines": sum(row["subscriptions.active_lines_eom"] for row in combined.rows),
        "recognizedServiceRevenueCents": sum(row["finance.recognized_service_revenue"] for row in combined.rows),
        "sourceDaxCalls": 4, "coverage": combined.coverage, "receipt": str(private / name),
        "lane": receipt["lane"],
    }, indent=2))


if __name__ == "__main__":
    main()
