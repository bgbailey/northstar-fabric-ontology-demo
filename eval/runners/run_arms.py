"""Execute offline scripted or live MCP evaluation arms with resume and budget ceilings (M4-02)."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.arms import (  # noqa: E402
    DEFAULT_IQ_MCP_URL,
    ONTOLOGY_MCP_URL_TEMPLATE,
    VALID_ARMS,
    ArmName,
    ArmProvider,
    ArmRunRecord,
    LiveApprovalError,
    LiveEvalApproval,
    get_live_arm,
    get_offline_arm,
)
from eval.oracle.service_assurance import load_questions  # noqa: E402
from eval.runners.run_offline import DEFAULT_SEED, ensure_profile_data  # noqa: E402

DEFAULT_QUESTIONS = ROOT / "eval" / "questions" / "service-assurance.jsonl"
DEFAULT_OUTPUT = ROOT / "artifacts" / "arms-runs.jsonl"
EXIT_CEILING_REACHED = 2


def parse_arms_argument(raw_values: Sequence[str] | None) -> list[ArmName]:
    """Parse comma-separated and/or repeated arm arguments in canonical order."""
    if not raw_values:
        return list(VALID_ARMS)
    parsed: list[ArmName] = []
    for chunk in raw_values:
        for item in chunk.split(","):
            token = item.strip()
            if not token:
                continue
            if token not in VALID_ARMS:
                raise ValueError(
                    f"Unsupported arm {token!r}; expected one of {list(VALID_ARMS)}"
                )
            typed = cast(ArmName, token)
            if typed not in parsed:
                parsed.append(typed)
    if not parsed:
        raise ValueError("At least one evaluation arm must be selected")
    return parsed


def load_existing_runs(
    output_path: Path,
    *,
    retry_failed: bool = False,
) -> tuple[set[tuple[str, str, int]], float]:
    """Inspect an existing JSONL output file for completed (question_id, arm, repeat) keys."""
    completed: set[tuple[str, str, int]] = set()
    prior_spend_usd = 0.0
    if not output_path.is_file():
        return completed, prior_spend_usd

    for raw_line in output_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        qid = record.get("question_id")
        arm = record.get("arm")
        rep = record.get("repeat")
        status = record.get("status")
        cost = record.get("cost_usd", 0.0)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            prior_spend_usd += float(cost)
        if not isinstance(qid, str) or not isinstance(arm, str) or not isinstance(rep, int):
            continue
        if retry_failed and status == "FAILED":
            completed.discard((qid, arm, rep))
        else:
            completed.add((qid, arm, rep))
    return completed, prior_spend_usd


def record_to_jsonl_dict(
    record: ArmRunRecord,
    *,
    cost_usd: float,
) -> dict[str, Any]:
    """Convert an ``ArmRunRecord`` into the M4-02 JSONL line contract."""
    input_tokens = int(record.effort.get("input_tokens", 0))
    output_tokens = int(record.effort.get("output_tokens", 0))
    elapsed_ms = float(record.effort.get("elapsed_ms", 0.0))
    return {
        "question_id": record.question_id,
        "level": record.level,
        "persona": record.persona,
        "arm": record.arm,
        "repeat": record.repeat,
        "status": record.status,
        "rows": record.rows,
        "exclusions": record.exclusions,
        "path": record.path,
        "tool_calls": record.tool_calls,
        "tokens": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "seconds": round(elapsed_ms / 1000.0, 6),
        "cost_usd": round(cost_usd, 8),
        "model_deployment": record.model_deployment,
        "toolset_variant": record.toolset_variant,
        "timestamps": {
            "started_at": record.started_at,
            "completed_at": record.completed_at,
        },
    }


def _build_dry_run_plan(
    *,
    mode: str,
    profile: str,
    questions: list[dict[str, Any]],
    arms: list[ArmName],
    repeats: int,
    planned_total: int,
    completed_count: int,
    remaining_count: int,
    prior_spend_usd: float,
    max_spend_usd: float,
    max_seconds: float,
    concurrency: int,
    approval: LiveEvalApproval | None,
    endpoint_preview: str | None,
    env: dict[str, str],
) -> dict[str, Any]:
    if mode == "offline":
        toolset_variant = "offline-local-v1"
        model_deployment = "scripted-offline-fixture"
        endpoints = {
            "execution": "local_duckdb_parquet_fixture",
            "iq_mcp_url": None,
            "ontology_mcp_url": None,
            "foundry_responses_url": None,
        }
    elif approval is not None:
        toolset_variant = approval.toolset_variant
        model_deployment = approval.model_deployment
        endpoints = {
            "foundry_responses_url": approval.foundry_responses_url,
            "iq_mcp_url": approval.iq_mcp_url,
            "ontology_mcp_url": approval.ontology_mcp_url,
        }
    else:
        ws_preview = env.get("FABRIC_WORKSPACE_ID", "<WORKSPACE_ID>")
        ont_preview = env.get("FABRIC_ONTOLOGY_ITEM_ID", "<ONTOLOGY_ITEM_ID>")
        foundry_preview = (
            endpoint_preview
            or env.get(
                "FOUNDRY_RESPONSES_URL",
                "https://<account>.openai.azure.com/openai/v1/responses",
            )
        )
        toolset_variant = env.get("FABRIC_IQ_TOOLSET_VARIANT", "service_default")
        model_deployment = env.get("FOUNDRY_MODEL_NAME", "gpt-5-mini")
        endpoints = {
            "foundry_responses_url": foundry_preview,
            "iq_mcp_url": DEFAULT_IQ_MCP_URL,
            "ontology_mcp_url": ONTOLOGY_MCP_URL_TEMPLATE.format(
                workspace_id=ws_preview,
                ontology_item_id=ont_preview,
            ),
        }

    calls_by_arm: dict[str, dict[str, Any]] = {}
    for arm in arms:
        uses_ontology = arm == "ontology"
        calls_by_arm[arm] = {
            "planned_runs": len(questions) * repeats,
            "servers": ["ontology_mcp", "fabric_iq"] if uses_ontology else ["fabric_iq"],
        }

    return {
        "status": "DRY_RUN",
        "mode": mode,
        "profile": profile,
        "question_count": len(questions),
        "arms": list(arms),
        "repeats": repeats,
        "total_planned_runs": planned_total,
        "already_completed_runs": completed_count,
        "remaining_runs": remaining_count,
        "prior_spend_usd": round(prior_spend_usd, 6),
        "max_spend_usd": max_spend_usd,
        "max_seconds": max_seconds,
        "concurrency": concurrency,
        "model_deployment": model_deployment,
        "toolset_variant": toolset_variant,
        "endpoints": endpoints,
        "calls_by_arm": calls_by_arm,
    }


def run_arms(
    *,
    questions_path: Path = DEFAULT_QUESTIONS,
    arms: Sequence[ArmName] = VALID_ARMS,
    repeats: int = 5,
    mode: str = "offline",
    profile: str = "service-assurance",
    seed: int = DEFAULT_SEED,
    data_dir: Path | None = None,
    output_path: Path = DEFAULT_OUTPUT,
    approval_path: Path | None = None,
    endpoint_preview: str | None = None,
    max_spend_usd: float = 10.0,
    max_seconds: float = 1800.0,
    concurrency: int = 1,
    retry_failed: bool = False,
    dry_run: bool = False,
    provider_factory: Callable[[ArmName], ArmProvider] | None = None,
    cost_calculator: Callable[[ArmRunRecord], float] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run selected evaluation arms and return ``(exit_code, summary_dict)``."""
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    if mode not in ("offline", "live"):
        raise ValueError(f"Unsupported mode: {mode!r}")

    selected_arms = list(arms)
    if not selected_arms:
        raise ValueError("At least one arm is required")

    t_start = time.perf_counter()
    questions = load_questions(questions_path)
    completed_keys, prior_spend_usd = load_existing_runs(
        output_path, retry_failed=retry_failed
    )

    planned_tasks: list[tuple[dict[str, Any], ArmName, int]] = []
    for repeat_idx in range(1, repeats + 1):
        for question in questions:
            for arm_name in selected_arms:
                planned_tasks.append((question, arm_name, repeat_idx))

    remaining_tasks = [
        (question, arm_name, repeat_idx)
        for (question, arm_name, repeat_idx) in planned_tasks
        if (question["id"], arm_name, repeat_idx) not in completed_keys
    ]

    env_map = dict(os.environ) if env is None else dict(env)
    approval: LiveEvalApproval | None = None
    effective_max_spend = float(max_spend_usd)
    effective_max_seconds = float(max_seconds)

    if mode == "live":
        if dry_run:
            if approval_path is not None:
                approval = LiveEvalApproval.from_file(approval_path, root=ROOT)
                effective_max_spend = min(effective_max_spend, approval.max_spend_usd)
                effective_max_seconds = min(effective_max_seconds, approval.max_seconds)
        else:
            if approval_path is None:
                raise LiveApprovalError(
                    "Live mode requires --approval pointing to a private approval JSON outside ROOT"
                )
            approval = LiveEvalApproval.from_file(approval_path, root=ROOT)
            effective_max_spend = min(effective_max_spend, approval.max_spend_usd)
            effective_max_seconds = min(effective_max_seconds, approval.max_seconds)

    if dry_run:
        plan = _build_dry_run_plan(
            mode=mode,
            profile=profile,
            questions=questions,
            arms=selected_arms,
            repeats=repeats,
            planned_total=len(planned_tasks),
            completed_count=len(planned_tasks) - len(remaining_tasks),
            remaining_count=len(remaining_tasks),
            prior_spend_usd=prior_spend_usd,
            max_spend_usd=effective_max_spend,
            max_seconds=effective_max_seconds,
            concurrency=concurrency,
            approval=approval,
            endpoint_preview=endpoint_preview,
            env=env_map,
        )
        return 0, plan

    folder = (
        ensure_profile_data(profile, seed=seed, data_dir=data_dir)
        if mode == "offline"
        else (data_dir.resolve() if data_dir is not None else ROOT)
    )

    created_live_providers: list[Any] = []
    providers_by_arm: dict[ArmName, ArmProvider] = {}
    for arm_name in selected_arms:
        if provider_factory is not None:
            providers_by_arm[arm_name] = provider_factory(arm_name)
        elif mode == "offline":
            providers_by_arm[arm_name] = get_offline_arm(arm_name)
        else:
            assert approval is not None
            live_arm = get_live_arm(arm_name, approval)
            created_live_providers.append(live_arm)
            providers_by_arm[arm_name] = live_arm

    def _compute_cost(rec: ArmRunRecord) -> float:
        if cost_calculator is not None:
            return cost_calculator(rec)
        if approval is not None:
            return approval.cost_for_tokens(
                int(rec.effort.get("input_tokens", 0)),
                int(rec.effort.get("output_tokens", 0)),
            )
        return 0.0

    cumulative_spend_usd = prior_spend_usd
    written_this_invocation = 0
    ceiling_reason: str | None = None
    write_lock = threading.Lock()
    stop_event = threading.Event()

    def _check_ceiling_locked() -> str | None:
        elapsed = time.perf_counter() - t_start
        if cumulative_spend_usd >= effective_max_spend:
            return "max_spend_usd_reached"
        if elapsed >= effective_max_seconds:
            return "max_seconds_reached"
        return None

    output_resolved = output_path.resolve()
    output_resolved.parent.mkdir(parents=True, exist_ok=True)

    try:
        if remaining_tasks:
            with write_lock:
                initial_reason = _check_ceiling_locked()
                if initial_reason is not None:
                    ceiling_reason = initial_reason
                    stop_event.set()

        if remaining_tasks and not stop_event.is_set():
            with output_resolved.open("a", encoding="utf-8", newline="\n") as handle:
                task_queue: queue.Queue[tuple[dict[str, Any], ArmName, int]] = queue.Queue()
                for task_item in remaining_tasks:
                    task_queue.put(task_item)

                def _worker() -> None:
                    nonlocal cumulative_spend_usd, written_this_invocation, ceiling_reason
                    while not stop_event.is_set():
                        try:
                            question, arm_name, repeat_idx = task_queue.get_nowait()
                        except queue.Empty:
                            return
                        try:
                            with write_lock:
                                pre_reason = _check_ceiling_locked()
                                if pre_reason is not None:
                                    if ceiling_reason is None:
                                        ceiling_reason = pre_reason
                                    stop_event.set()
                                    return

                            provider = providers_by_arm[arm_name]
                            record = provider.run_question(
                                question, folder, repeat=repeat_idx
                            )
                            run_cost = _compute_cost(record)
                            line_obj = record_to_jsonl_dict(record, cost_usd=run_cost)
                            serialized = json.dumps(line_obj, separators=(",", ":"))

                            with write_lock:
                                handle.write(serialized + "\n")
                                handle.flush()
                                cumulative_spend_usd += run_cost
                                written_this_invocation += 1
                                post_reason = _check_ceiling_locked()
                                if post_reason is not None:
                                    if ceiling_reason is None:
                                        ceiling_reason = post_reason
                                    stop_event.set()
                                    return
                        finally:
                            task_queue.task_done()

                if concurrency == 1:
                    _worker()
                else:
                    worker_count = min(concurrency, len(remaining_tasks))
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=worker_count
                    ) as pool:
                        futures = [pool.submit(_worker) for _ in range(worker_count)]
                        for fut in futures:
                            fut.result()
    finally:
        for live_prov in created_live_providers:
            close_fn = getattr(live_prov, "close", None)
            if callable(close_fn):
                close_fn()

    elapsed_total = round(time.perf_counter() - t_start, 6)
    total_completed = (len(planned_tasks) - len(remaining_tasks)) + written_this_invocation

    if ceiling_reason is not None:
        summary = {
            "status": "CEILING_REACHED",
            "reason": ceiling_reason,
            "mode": mode,
            "profile": profile,
            "question_count": len(questions),
            "arms": selected_arms,
            "repeats": repeats,
            "written_runs": written_this_invocation,
            "completed_runs": total_completed,
            "total_planned_runs": len(planned_tasks),
            "cumulative_spend_usd": round(cumulative_spend_usd, 6),
            "max_spend_usd": effective_max_spend,
            "elapsed_seconds": elapsed_total,
            "max_seconds": effective_max_seconds,
            "output": output_resolved.name,
        }
        return EXIT_CEILING_REACHED, summary

    summary = {
        "status": "PASS",
        "mode": mode,
        "profile": profile,
        "question_count": len(questions),
        "arms": selected_arms,
        "repeats": repeats,
        "written_runs": written_this_invocation,
        "completed_runs": total_completed,
        "total_planned_runs": len(planned_tasks),
        "cumulative_spend_usd": round(cumulative_spend_usd, 6),
        "elapsed_seconds": elapsed_total,
        "output": output_resolved.name,
    }
    return 0, summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--questions",
        type=Path,
        default=DEFAULT_QUESTIONS,
        help="Path to golden questions JSONL file",
    )
    parser.add_argument(
        "--arms",
        nargs="+",
        action="extend",
        default=None,
        help="Comma-separated or space-separated arms (baseline,baseline_plus_rules,ontology)",
    )
    parser.add_argument(
        "--repeats",
        "-k",
        type=int,
        default=5,
        help="Number of repeats per question and arm (default: 5)",
    )
    parser.add_argument(
        "--mode",
        choices=("offline", "live"),
        default="offline",
        help="Execution mode: offline scripted fixtures or live MCP + Responses API",
    )
    parser.add_argument(
        "--profile",
        choices=("showcase-v2", "service-assurance"),
        default="service-assurance",
        help="Synthetic data profile for offline evaluation",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="Deterministic synthetic generator seed",
    )
    parser.add_argument(
        "--data-dir",
        "--data",
        dest="data_dir",
        type=Path,
        default=None,
        help="Optional pre-generated Parquet directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path to output JSONL file",
    )
    parser.add_argument(
        "--approval",
        type=Path,
        default=None,
        help="Path to private live approval JSON outside repository root",
    )
    parser.add_argument(
        "--endpoint-preview",
        type=str,
        default=None,
        help="Optional Foundry Responses endpoint preview URL for --mode live --dry-run",
    )
    parser.add_argument(
        "--max-spend-usd",
        type=float,
        default=10.0,
        help="Cumulative spend ceiling in USD (default: 10.0)",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=1800.0,
        help="Wall-clock time ceiling in seconds (default: 1800.0)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Worker concurrency (default: 1)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-run previously recorded FAILED items when resuming",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print deterministic call plan and ceilings without calling any live service",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        selected_arms = parse_arms_argument(args.arms)
        exit_code, summary = run_arms(
            questions_path=args.questions,
            arms=selected_arms,
            repeats=args.repeats,
            mode=args.mode,
            profile=args.profile,
            seed=args.seed,
            data_dir=args.data_dir,
            output_path=args.output,
            approval_path=args.approval,
            endpoint_preview=args.endpoint_preview,
            max_spend_usd=args.max_spend_usd,
            max_seconds=args.max_seconds,
            concurrency=args.concurrency,
            retry_failed=args.retry_failed,
            dry_run=args.dry_run,
        )
    except (LiveApprovalError, ValueError) as exc:
        print(
            json.dumps({"status": "ERROR", "error": str(exc)}),
            file=sys.stderr,
        )
        return 1

    stream = sys.stdout if exit_code == 0 else sys.stderr
    print(json.dumps(summary, indent=2 if args.dry_run else None), file=stream)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
