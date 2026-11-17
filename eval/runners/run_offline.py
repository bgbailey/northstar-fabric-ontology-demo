"""Compare the real local executor with independent facts and persist evaluator-only gold."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "independent_reference", ROOT / "eval" / "oracle" / "reference.py"
)
assert spec is not None and spec.loader is not None
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)

DEFAULT_SEED = 20260929
QUESTION_PROFILES = ("miniature", "smoke", "showcase-v2", "service-assurance", "all")


def ensure_profile_data(profile: str, seed: int = DEFAULT_SEED, data_dir: Path | None = None) -> Path:
    """Return an existing verified profile directory or generate synthetic Parquet files locally."""
    if data_dir is not None:
        return data_dir.resolve()
    target = ROOT / ".local" / "generated" / f"{profile}-{seed}"
    manifest_path = target / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("profile") == profile and manifest.get("seed") == seed:
            return target
    if target.exists():
        shutil.rmtree(target)
    from tools.build_data import build

    build(profile, seed, target)
    return target


def run_questions_oracle(
    questions_path: Path,
    *,
    profile: str = "service-assurance",
    seed: int = DEFAULT_SEED,
    data_dir: Path | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Evaluate a golden question JSONL file across one or both data profiles."""
    from eval.oracle.service_assurance import evaluate_suite, load_questions

    if profile == "all" and data_dir is not None:
        raise ValueError("--data cannot be combined with --profile all")
    selected_profiles = ("showcase-v2", "service-assurance") if profile == "all" else (profile,)
    questions = load_questions(questions_path)

    profiles_payload: dict[str, Any] = {}
    for prof in selected_profiles:
        folder = ensure_profile_data(prof, seed=seed, data_dir=data_dir)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        suite_results = evaluate_suite(questions, folder, include_traps=True)
        profiles_payload[prof] = {
            "profile": prof,
            "seed": manifest.get("seed", seed),
            "content_sha256": manifest.get("content_sha256"),
            "question_count": len(suite_results),
            "results": suite_results,
        }

    destination = (
        output_path.resolve()
        if output_path is not None
        else ROOT / ".local" / "evaluation" / f"offline-oracle-{profile}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        q_rel = questions_path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        q_rel = questions_path.name
    receipt: dict[str, Any] = {
        "schema_version": "1.0",
        "mode": "OFFLINE_ORACLE",
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "status": "PASS",
        "questions_file": q_rel,
        "question_count": len(questions),
        "profiles": profiles_payload,
        "limitations": [
            "Independent DuckDB oracle over synthetic Parquet fixtures; not live Fabric DAX.",
            "No tenant, live MCP endpoint, or comparative model-accuracy claim.",
        ],
    }
    if len(selected_profiles) == 1:
        only_prof = selected_profiles[0]
        receipt["profile"] = only_prof
        receipt["results"] = profiles_payload[only_prof]["results"]

    destination.write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    summary = {
        "status": receipt["status"],
        "mode": receipt["mode"],
        "profiles": list(selected_profiles),
        "question_count": len(questions),
        "receipt": destination.name,
    }
    print(json.dumps(summary))
    return receipt


async def run() -> None:
    data = ROOT / "data" / "generated" / "miniature"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8765", timeout=60) as client:
        response = await client.get("/api/health")
        response.raise_for_status()
        health = response.json()
        nodes = [{
            "id": key, "kind": "QuerySemanticModel", "model": model, "table": table,
            "metrics": [metric], "group_by": ["account_key"],
            "filters": [{"column": "month", "op": "eq", "values": ["2026-08-01"]}],
            "date_role": "month", "expected_grain": ["account_key"],
        } for key, model, table, metric in [
            ("lines", "subscriptions", "subscription_month", "subscriptions.active_lines_eom"),
            ("revenue", "finance", "account_month", "finance.recognized_service_revenue"),
        ]]
        nodes.append({
            "id": "answer", "kind": "JoinResults", "left": "lines", "right": "revenue",
            "relationship": "account-month-conformed-v1", "left_keys": ["account_key"],
            "right_keys": ["account_key"], "cardinality": "one_to_one", "unmatched": "error",
        })
        plan = {
            "version": "1.0", "request_id": "persisted-offline-vertical-slice",
            "question": "August active lines and recognized service revenue by account",
            "principal_scope": health["scope"], "as_of": "2026-09-29T00:00:00Z",
            "ontology_revision": health["context_revision"],
            "metric_selections": [n["metrics"][0] for n in nodes[:2]],
            "nodes": nodes, "result_node": "answer",
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
    (output / "offline-oracle-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps({
        "status": receipt["status"], "rows": len(expected), "query_receipts": len(result["receipts"]),
        "elapsed_ms": result["elapsed_ms"], "receipt": str(output / "offline-oracle-receipt.json"),
    }))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--questions",
        type=Path,
        default=None,
        help="Path to golden questions JSONL file (runs standalone DuckDB oracle when set)",
    )
    parser.add_argument(
        "--profile",
        choices=QUESTION_PROFILES,
        default="service-assurance",
        help="Synthetic data profile to evaluate when --questions is provided",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Generator seed")
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help="Optional pre-generated Parquet directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON receipt path when --questions is provided",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.questions is not None:
        run_questions_oracle(
            args.questions,
            profile=args.profile,
            seed=args.seed,
            data_dir=args.data,
            output_path=args.output,
        )
        return 0
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
