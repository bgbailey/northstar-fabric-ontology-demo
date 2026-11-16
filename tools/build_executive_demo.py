"""Reconcile an actual native answer with independently reviewed source results; export display-only evidence."""

import argparse
import hashlib
import json
import re
from pathlib import Path

from telco_iq.approval import private_config_path
from telco_iq.catalog import Catalog, digest
from telco_iq.comparison import ComparisonResult
from telco_iq.demo_library import EXECUTIVE_METRICS, ExecutiveAccount, RecordedExecutiveDemo
from telco_iq.mcp_configuration import MCPProfiles
from telco_iq.private_evidence import PrivateEvidenceSink
from telco_iq.reader_native_question import ReaderNativeQuestionConfig
from telco_iq.recorded_ontology import RecordedOntology
from tools.review_starter_reuse import review as review_reuse

ROOT = Path(__file__).resolve().parents[1]
HEADERS = (
    "Account ID", "Account Key", "Common service month", "Active Lines EOM",
    "Recognized Service Revenue", "Contribution Margin",
)


def answer_rows(text: str) -> list[ExecutiveAccount]:
    """Decode only the actually observed six-column native Markdown table, not a promised service schema."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("|") and tuple(
        cell.strip() for cell in line.strip("|").split("|")
    ) == HEADERS]
    if len(starts) != 1:
        raise ValueError("The reviewed native answer table is absent, repeated or changed")
    start = starts[0]
    if start + 1 >= len(lines) or not re.fullmatch(r"\|[\s:|-]+\|", lines[start + 1]):
        raise ValueError("The actual native table separator is missing")
    rows = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        values = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        if len(values) != 6 or values[2] != "2026-07-01" or any(
            not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", values[index]) for index in (1, 3, 4, 5)
        ):
            raise ValueError("Native rows must retain the exact common month and original integer encodings")
        rows.append(ExecutiveAccount(
            account_id=values[0], account_key=int(values[1]), active_lines_eom=int(values[3]),
            recognized_service_revenue_cents=int(values[4]), contribution_margin_cents=int(values[5]),
        ))
    if not rows:
        raise ValueError("A positive actual native answer is required")
    return rows


def source_candidates(pair: ComparisonResult) -> list[ExecutiveAccount]:
    native = next(arm for arm in pair.arms if arm.arm == "native")
    answer = native.execution.answer if native.execution else None
    if native.status != "COMPLETE" or answer is None or pair.comparability != "COMMON_SOURCE_STATE":
        raise ValueError("Independent source reconciliation requires the complete accepted two-model reference")
    rows = []
    for row in answer.rows:
        values = [row.get(metric) for metric in sorted(EXECUTIVE_METRICS)]
        if any(type(value) is not int for value in values):
            raise ValueError("A source null, formatted or missing measure cannot become a native zero")
        if row["subscriptions.active_lines_eom"] == 0 and row["finance.recognized_service_revenue"] > 0 and (
            row["finance.contribution_margin"] < 0
        ):
            rows.append(ExecutiveAccount(
                account_id=row["account_id"], account_key=row["account_key"],
                active_lines_eom=row["subscriptions.active_lines_eom"],
                recognized_service_revenue_cents=row["finance.recognized_service_revenue"],
                contribution_margin_cents=row["finance.contribution_margin"],
            ))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-question-directory", type=Path, required=True)
    parser.add_argument("--source-result", type=Path, required=True)
    parser.add_argument("--value-review", type=Path, required=True)
    parser.add_argument("--source-profiles", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--native-context", type=Path, required=True)
    parser.add_argument("--source-models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    args = parser.parse_args()
    private = private_config_path(args.native_question_directory, ROOT)
    config = ReaderNativeQuestionConfig.model_validate_json((private / "approved-capture-config.json").read_bytes())
    summary = json.loads((private / "summary.json").read_bytes())
    if summary["status"] != "NATIVE_ANSWER_CAPTURED_NEEDS_REVIEW" or len(summary["capture_hashes"]) != 1:
        raise ValueError("Exactly one original successful managed-native answer must be retained")
    sink = PrivateEvidenceSink(config.private_evidence, ROOT)
    record = sink.read(summary["capture_hashes"][0])
    response = record["response"]
    if record["tool"] != "ask_ontology" or record["request"] != {"request": config.question} or response.get("isError"):
        raise ValueError("The native answer does not match the reviewed original request")
    content = response.get("content")
    if not isinstance(content, list) or not content or any(
        item.get("type") != "text" or not isinstance(item.get("text"), str) for item in content
    ):
        raise ValueError("The actual native answer is not the reviewed textual content shape")
    text = "\n".join(item["text"] for item in content)
    rows = answer_rows(text)
    catalog = Catalog(private_config_path(args.catalog, ROOT))
    result_path = private_config_path(args.source_result, ROOT)
    review_path = private_config_path(args.value_review, ROOT)
    reviewed = json.loads(review_path.read_bytes())
    reuse = review_reuse(result_path, reviewed, catalog)
    pair = ComparisonResult.model_validate_json(result_path.read_bytes())
    expected = source_candidates(pair)
    if sorted(rows, key=lambda row: row.account_id) != sorted(expected, key=lambda row: row.account_id):
        raise ValueError("The native answer identities or original metric values differ from complete source evidence")
    models = json.loads(private_config_path(args.source_models, ROOT).read_bytes())
    for model, home, metric in (
        ("subscriptions", "Subscription Month", "Active Lines EOM"),
        ("finance", "Account Month", "Recognized Service Revenue"),
        ("finance", "Account Month", "Contribution Margin"),
    ):
        if not re.search(
            r"\*\*" + re.escape(metric) + r"\*\*.*?" + re.escape(models[model]["displayName"])
            + r".*?home table: `" + re.escape(home) + r"`", text,
        ):
            raise ValueError("The actual native source/home attribution is missing or incorrect")
    profiles = MCPProfiles.read(args.source_profiles, ROOT)
    originals = PrivateEvidenceSink(profiles.private_evidence, ROOT)
    captures = next(arm["source_captures"] for arm in reviewed["arms"] if arm["arm"] == "native")
    native = next(arm for arm in pair.arms if arm.arm == "native")
    verification_queries = []
    for reference in captures:
        original = originals.read(reference)
        if original["tool"] != "ExecuteQuery" or len(original["request"].get("daxQueries", [])) != 1:
            raise ValueError("The independent source verification has no exact original query")
        query = original["request"]["daxQueries"][0]
        verification_queries.append({
            "model": original["source_alias"], "dax": query,
            "dax_sha256": hashlib.sha256(query.encode()).hexdigest(),
            "response_sha256": digest(original["response"]), "observed_at": original["captured_at"],
        })
    ontology = RecordedOntology.model_validate_json(args.native_context.read_bytes())
    if config.native_snapshot_hash != ontology.native_snapshot_hash:
        raise ValueError("The native question was not bound to this separate small native snapshot")
    numerical_review = {
        "kind": "ACTUAL_NATIVE_EXECUTIVE_ANSWER_MATCHED_TO_REVIEWED_SOURCE_ORIGINALS",
        "native_answer_capture_sha256": summary["capture_hashes"][0],
        "native_response_sha256": digest(response),
        "independent_source_result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "independent_source_value_review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
        "source_registered_accounts": reuse["arms"][1]["customer_rows"],
        "matching_native_accounts": len(rows), "focus_metric_cells_matched": len(rows) * 3,
        "source_measure_homes_match": True, "separate_native_snapshot_hash": ontology.native_snapshot_hash,
        "native_and_source_observation_times_remain_separate": True,
        "native_internal_DAX_or_model_identity_exposed": False,
        "new_source_or_model_calls": 0, "oracle_or_fixture_inputs": False,
    }
    review_data = json.dumps(numerical_review, indent=2).encode()
    packet = RecordedExecutiveDemo(
        captured_at=summary["observed_at"], question=config.question, ontology=ontology,
        focus_metric_ids=sorted(EXECUTIVE_METRICS), separate_native_item_verified=True,
        native_answer_origin="MANAGED_NATIVE_SERVICE", native_answer_text=text,
        native_response_sha256=digest(response),
        source_measure_review_sha256=hashlib.sha256(review_data).hexdigest(),
        native_answer_matches_source_values=True, rows=rows,
        source_queries=verification_queries, source_clocks=native.execution.answer.source_clocks,
        limitations=[
            "Actual separate small native ontology and managed-native business answer, not the filtered large ontology.",
            "Source queries shown are independent verification originals from the accepted same-day complete account population; the native service did not expose its internal DAX or execution trace.",
            "Native and independent source observations have separate original times; matching values do not prove a shared live source-state window.",
            "The complete source reference contains 24 registered accounts; only the one matching review candidate is displayed.",
            "All 14 generated source proxies are retained; three focus measures keep the executive story small.",
            "Review candidate, not a causal billing diagnosis, financial action or managed Rule-enforcement proof.",
            "Managed native model identity, usage/cost and automatic source-AI inheritance are not exposed by this answer.",
        ],
    )
    raw = packet.model_dump_json(indent=2)
    forbidden = {
        str(config.ontology_workspace_id), str(config.ontology_item_id), config.scope.tenant, config.scope.principal,
        *[str(identity.dataset_id) for identity in config.sources.values()],
    }
    if any(value.casefold() in raw.casefold() for value in forbidden):
        raise ValueError("Private source/principal/target identities remain in the display packet")
    if args.output.exists() or args.review_output.exists():
        raise ValueError("A dated executive packet or review already exists; never overwrite")
    with args.review_output.open("xb") as handle:
        handle.write(review_data)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(raw)
    if RecordedExecutiveDemo.model_validate_json(args.output.read_bytes()) != packet:
        raise ValueError("The published executive packet failed exact typed readback")
    print(json.dumps({
        "status": "ACTUAL_NATIVE_EXECUTIVE_ANSWER_SOURCE_CHECKED_AND_PUBLISHED",
        "matched_accounts": len(rows), "source_population": reuse["arms"][1]["customer_rows"],
        "source_metric_cells_matched": len(rows) * 3, "new_source_or_model_calls": 0,
    }))


if __name__ == "__main__":
    main()
