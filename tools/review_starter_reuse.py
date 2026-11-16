"""Offline review of real starter receipts; no fixture, oracle, source request or model invocation."""

import argparse
import hashlib
import json
from pathlib import Path

from telco_iq.catalog import Catalog
from telco_iq.comparison import ComparisonResult

METRICS = {
    "subscriptions.active_lines_eom",
    "finance.recognized_service_revenue",
    "finance.contribution_margin",
}


def review(result_file: Path, value_review: dict, catalog: Catalog) -> dict:
    raw = result_file.read_bytes()
    result = ComparisonResult.model_validate_json(raw)
    original_hash = hashlib.sha256(raw).hexdigest()
    if value_review["original_result_sha256"] != original_hash or value_review["pair_id"] != result.pair_id:
        raise ValueError("Numerical review does not match the actual original pair")
    arms = []
    for arm in result.arms:
        if arm.plan is None or arm.execution is None or arm.execution.answer is None:
            arms.append({"arm": arm.arm, "status": str(arm.status), "observed_answer": False})
            continue
        if set(arm.plan.metric_selections) != METRICS or (
            {receipt.model for receipt in arm.execution.receipts} != {"subscriptions", "finance"}
            or any(node.kind in ("AggregateResult", "ApplyDeterministicRule", "ComparePeriods")
                   for node in arm.plan.nodes)
            or any("DEFINE MEASURE" in (receipt.query or "").upper() for receipt in arm.execution.receipts)
        ):
            raise ValueError("Starter must reuse only the three source measures without local replacement calculations")
        checked = next(item for item in value_review["arms"] if item["arm"] == arm.arm)
        if checked["metric_cells_checked"] != len(arm.execution.answer.rows) * len(METRICS):
            raise ValueError("Not every displayed starter measure cell was independently matched to its source receipt")
        identity = next((
            name for name in ("account_key", "account_id")
            if all(row.get(name) is not None for row in arm.execution.answer.rows)
        ), None)
        keys = {row.get(identity) for row in arm.execution.answer.rows} if identity else set()
        registry_nodes = {
            node.id for node in arm.plan.nodes if node.kind == "QuerySemanticModel" and node.table == "account"
            and identity in node.columns and not node.filters and not node.cohort_filters
        }
        registry_counts = [
            receipt.returned_rows for receipt in arm.execution.receipts if receipt.node_id in registry_nodes
            and receipt.status == "COMPLETE"
        ]
        if identity is None or None in keys or len(keys) != len(arm.execution.answer.rows) or not registry_counts or any(
            count != len(keys) for count in registry_counts
        ):
            raise ValueError("Final starter rows do not match the complete registered-account query counts")
        arms.append({
            "arm": arm.arm, "status": str(arm.status), "observed_answer": True,
            "customer_rows": len(arm.execution.answer.rows),
            "source_query_receipts": len(arm.execution.receipts),
            "source_owned_metric_cells_matched": checked["metric_cells_checked"],
            "query_hashes": [receipt.query_hash for receipt in arm.execution.receipts],
            "no_local_replacement_or_inline_measure_definition": True,
            "unique_registered_customer_rows_reconciled": True,
            "registered_customer_identity_column": identity,
        })
    if not any(arm["arm"] == "native" and arm["observed_answer"] and arm["customer_rows"] > 0 for arm in arms):
        raise ValueError("A positive actual native starter answer is required")
    return {
        "kind": "ACTUAL_STARTER_SOURCE_MEASURE_REUSE_REVIEW",
        "pair_id": result.pair_id, "original_result_sha256": original_hash,
        "domains": ["Customers", "Subscriptions", "Finance"],
        "semantic_models": ["subscriptions", "finance"],
        "metrics": [{key: catalog.metrics[metric][key] for key in (
            "id", "model", "table", "name", "aggregation", "unit",
        )} for metric in sorted(METRICS)],
        "comparability": result.comparability, "arms": arms,
        "additional_context": [
            "Reviewed shared customer identity between the two actual account dimensions.",
            "One common completed month; Active Lines EOM is a snapshot, not a sum across months.",
            "Recognized service revenue is distinct from billed charges and booked merchandise.",
        ],
        "internal_rule_consideration_or_general_accuracy_advantage_proven": False,
        "new_model_source_or_network_calls": 0,
        "limitations": [
            "This is an offline numerical/lineage review of actual live originals, not another service run.",
            "Equal answers show governed source-measure reuse, not an exclusive computational ability of ontology.",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--value-review", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = review(args.result, json.loads(args.value_review.read_bytes()), Catalog(args.catalog))
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
    print(json.dumps({"status": "ACTUAL_STARTER_MEASURE_REUSE_VERIFIED", "arms": value["arms"]}, indent=2))


if __name__ == "__main__":
    main()
