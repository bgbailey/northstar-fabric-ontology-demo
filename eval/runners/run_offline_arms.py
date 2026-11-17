"""Run offline scripted evaluation arms over golden questions and emit scorecard-ready JSON."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.arms import ArmName, ArmRunRecord, get_offline_arm  # noqa: E402
from eval.oracle.service_assurance import evaluate_suite, load_questions  # noqa: E402
from eval.runners.run_offline import DEFAULT_SEED, ensure_profile_data  # noqa: E402

VALID_ARM_CHOICES: tuple[ArmName, ...] = ("baseline", "baseline_plus_rules", "ontology")
DEFAULT_QUESTIONS = ROOT / "eval" / "questions" / "service-assurance.jsonl"


def _key_tuple(row: dict[str, Any], keys: list[str]) -> tuple[Any, ...]:
    return tuple(row.get(k) for k in keys)


def _set_metrics(
    observed_rows: list[dict[str, Any]],
    expected_rows: list[dict[str, Any]],
    keys: list[str],
) -> dict[str, float]:
    obs_set = {_key_tuple(r, keys) for r in observed_rows}
    exp_set = {_key_tuple(r, keys) for r in expected_rows}
    if not obs_set and not exp_set:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    overlap = len(obs_set & exp_set)
    precision = overlap / len(obs_set) if obs_set else 0.0
    recall = overlap / len(exp_set) if exp_set else 0.0
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if (precision + recall) > 0.0
        else 0.0
    )
    return {
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }


def _detect_traps(
    run: ArmRunRecord,
    oracle_entry: dict[str, Any],
) -> list[str]:
    hits: list[str] = []
    for trap_name, trap_payload in oracle_entry.get("trap_answers", {}).items():
        if (
            run.rows == trap_payload["rows"]
            and run.exclusions == trap_payload["exclusions"]
        ):
            hits.append(trap_name)
    return hits


def _unknown_handling_ok(
    question: dict[str, Any],
    run: ArmRunRecord,
    oracle_entry: dict[str, Any],
) -> bool:
    expected_excl = bool(question["answer"]["exclusionsExpected"])
    if expected_excl and not run.exclusions:
        return False
    if not expected_excl and run.exclusions:
        return False
    zero_trap = oracle_entry.get("trap_answers", {}).get("missing-as-zero")
    if zero_trap is not None and (
        run.rows == zero_trap["rows"] and run.exclusions == zero_trap["exclusions"]
    ):
        return False
    return True


def _summarize_runs(
    questions: list[dict[str, Any]],
    oracle_by_id: dict[str, dict[str, Any]],
    runs: list[ArmRunRecord],
) -> dict[str, Any]:
    question_by_id = {q["id"]: q for q in questions}
    by_arm_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_level_arm: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    trap_counts: dict[str, Counter[str]] = defaultdict(Counter)

    for run in runs:
        question = question_by_id[run.question_id]
        oracle_entry = oracle_by_id[run.question_id]
        keys = question["answer"]["keys"]
        exact_rows = run.rows == oracle_entry["rows"]
        unknown_ok = _unknown_handling_ok(question, run, oracle_entry)
        answer_correct = exact_rows and unknown_ok
        traps_hit = [] if answer_correct else _detect_traps(run, oracle_entry)
        for trap_name in traps_hit:
            trap_counts[run.arm][trap_name] += 1
        metrics = _set_metrics(run.rows, oracle_entry["rows"], keys)
        scored = {
            "question_id": run.question_id,
            "level": run.level,
            "repeat": run.repeat,
            "answer_correct": answer_correct,
            "exact_rows": exact_rows,
            "unknown_handling_ok": unknown_ok,
            "set_quality": metrics,
            "traps_hit": traps_hit,
            "tool_call_count": run.effort["tool_call_count"],
            "input_tokens": run.effort["input_tokens"],
            "output_tokens": run.effort["output_tokens"],
            "elapsed_ms": run.effort["elapsed_ms"],
        }
        by_arm_records[run.arm].append(scored)
        by_level_arm[run.level][run.arm].append(scored)

    def _aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(items)
        if total == 0:
            return {
                "runs": 0,
                "correct": 0,
                "accuracy": 0.0,
                "unknown_handling_rate": 0.0,
                "mean_f1": 0.0,
                "mean_tool_calls": 0.0,
                "mean_input_tokens": 0.0,
                "mean_output_tokens": 0.0,
            }
        correct = sum(1 for item in items if item["answer_correct"])
        unk_ok = sum(1 for item in items if item["unknown_handling_ok"])
        return {
            "runs": total,
            "correct": correct,
            "accuracy": round(correct / total, 6),
            "unknown_handling_rate": round(unk_ok / total, 6),
            "mean_f1": round(sum(i["set_quality"]["f1"] for i in items) / total, 6),
            "mean_tool_calls": round(sum(i["tool_call_count"] for i in items) / total, 3),
            "mean_input_tokens": round(sum(i["input_tokens"] for i in items) / total, 1),
            "mean_output_tokens": round(sum(i["output_tokens"] for i in items) / total, 1),
        }

    return {
        "by_arm": {arm: _aggregate(items) for arm, items in sorted(by_arm_records.items())},
        "by_level_and_arm": {
            level: {
                arm: _aggregate(items) for arm, items in sorted(arm_map.items())
            }
            for level, arm_map in sorted(by_level_arm.items())
        },
        "trap_hits_by_arm": {
            arm: dict(sorted(counter.items()))
            for arm, counter in sorted(trap_counts.items())
        },
    }


def run_offline_arms(
    questions_path: Path = DEFAULT_QUESTIONS,
    *,
    profile: str = "service-assurance",
    seed: int = DEFAULT_SEED,
    data_dir: Path | None = None,
    arms: list[ArmName] | None = None,
    repeats: int = 1,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Run selected offline arms across all golden questions and write scorecard input."""
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    selected_arms: list[ArmName] = list(arms) if arms is not None else ["baseline", "ontology"]
    questions = load_questions(questions_path)
    folder = ensure_profile_data(profile, seed=seed, data_dir=data_dir)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))

    oracle_suite = evaluate_suite(questions, folder, include_traps=True)
    oracle_by_id = {entry["question_id"]: entry for entry in oracle_suite}

    providers = [get_offline_arm(arm_name) for arm_name in selected_arms]
    run_records: list[ArmRunRecord] = []
    for repeat_idx in range(1, repeats + 1):
        for provider in providers:
            for question in questions:
                run_records.append(
                    provider.run_question(question, folder, repeat=repeat_idx)
                )

    summary = _summarize_runs(questions, oracle_by_id, run_records)
    try:
        q_rel = questions_path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        q_rel = questions_path.name

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "mode": "OFFLINE_SCRIPTED_ARMS",
        "benchmark_claim": False,
        "disclaimer": (
            "Deterministic scripted plans over local synthetic Parquet fixtures for CI "
            "harness and scorecard verification; not a live tenant or LLM benchmark."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "seed": manifest.get("seed", seed),
        "content_sha256": manifest.get("content_sha256"),
        "questions_file": q_rel,
        "question_count": len(questions),
        "arms": list(selected_arms),
        "repeats": repeats,
        "oracle": oracle_suite,
        "runs": [rec.to_dict() for rec in run_records],
        "summary": summary,
    }

    destination = (
        output_path.resolve()
        if output_path is not None
        else ROOT / ".local" / "evaluation" / f"offline-arms-{profile}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix == ".jsonl":
        lines = [json.dumps(rec.to_dict(), separators=(",", ":")) for rec in run_records]
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    else:
        destination.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    print(
        json.dumps(
            {
                "status": "PASS",
                "mode": payload["mode"],
                "profile": profile,
                "question_count": len(questions),
                "arms": list(selected_arms),
                "total_runs": len(run_records),
                "output": destination.name,
            }
        )
    )
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--questions",
        type=Path,
        default=DEFAULT_QUESTIONS,
        help="Path to golden questions JSONL file",
    )
    parser.add_argument(
        "--profile",
        choices=("showcase-v2", "service-assurance"),
        default="service-assurance",
        help="Synthetic data profile to evaluate",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Generator seed")
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help="Optional pre-generated Parquet directory",
    )
    parser.add_argument(
        "--arms",
        nargs="+",
        choices=VALID_ARM_CHOICES,
        default=["baseline", "ontology"],
        help="Offline arms to execute",
    )
    parser.add_argument("--repeats", type=int, default=1, help="Number of repeats per question")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON or JSONL file path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_offline_arms(
        args.questions,
        profile=args.profile,
        seed=args.seed,
        data_dir=args.data,
        arms=[cast(ArmName, a) for a in args.arms],
        repeats=args.repeats,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
