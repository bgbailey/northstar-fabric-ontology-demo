"""Build JSON and static HTML evaluation scorecards from arm run JSONL logs (M4-04)."""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.arms import VALID_ARMS  # noqa: E402
from eval.oracle.service_assurance import (  # noqa: E402
    VALID_LEVELS,
    VALID_TRAPS,
    evaluate_question,
    evaluate_trap,
    load_questions,
)
from eval.runners.run_offline import DEFAULT_SEED, ensure_profile_data  # noqa: E402
from eval.scoring import (  # noqa: E402
    paired_bootstrap_interval,
    repeat_agreement,
    score_run,
    wilson_interval,
)

DEFAULT_QUESTIONS = ROOT / "eval" / "questions" / "service-assurance.jsonl"
DEFAULT_OUTPUT_JSON = ROOT / "artifacts" / "scorecard.json"
DEFAULT_OUTPUT_HTML = ROOT / "artifacts" / "scorecard.html"

ARM_LABELS: dict[str, str] = {
    "baseline": "Arm A — Semantic models only",
    "baseline_plus_rules": "Arm A+ — Semantic models + rules in prompt",
    "ontology": "Arm B — Ontology-grounded",
}

COMPARISON_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("ontology", "baseline", "Arm B vs Arm A"),
    ("ontology", "baseline_plus_rules", "Arm B vs Arm A+"),
    ("baseline_plus_rules", "baseline", "Arm A+ vs Arm A"),
)


def _load_runs_file(runs_path: Path) -> list[dict[str, Any]]:
    raw_text = runs_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        raise ValueError(f"Runs file is empty: {runs_path}")

    if raw_text.startswith("{") and "\n" not in raw_text[:2000]:
        pass

    records_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
    first_char = raw_text[0]
    lines = raw_text.splitlines()
    if first_char == "{" and len(lines) > 1:
        try:
            maybe_doc = json.loads(raw_text)
        except json.JSONDecodeError:
            maybe_doc = None
        if isinstance(maybe_doc, dict) and isinstance(maybe_doc.get("runs"), list):
            for rec in maybe_doc["runs"]:
                if isinstance(rec, dict):
                    key = (
                        str(rec["question_id"]),
                        str(rec["arm"]),
                        int(rec.get("repeat", 1)),
                    )
                    records_by_key[key] = rec
            return list(records_by_key.values())

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            raise ValueError(f"{runs_path}:{line_no}: expected JSON object")
        key = (
            str(parsed["question_id"]),
            str(parsed["arm"]),
            int(parsed.get("repeat", 1)),
        )
        records_by_key[key] = parsed

    if not records_by_key:
        raise ValueError(f"No valid run records found in {runs_path}")
    return list(records_by_key.values())


def _extract_effort_metrics(run: dict[str, Any]) -> tuple[int, int, int, float]:
    tokens = run.get("tokens")
    effort = run.get("effort")
    if isinstance(tokens, dict):
        in_tok = int(tokens.get("input_tokens", 0))
        out_tok = int(tokens.get("output_tokens", 0))
    elif isinstance(effort, dict):
        in_tok = int(effort.get("input_tokens", 0))
        out_tok = int(effort.get("output_tokens", 0))
    else:
        in_tok = 0
        out_tok = 0

    tool_calls = run.get("tool_calls")
    if isinstance(tool_calls, list):
        tc_count = len(tool_calls)
    elif isinstance(effort, dict):
        tc_count = int(effort.get("tool_call_count", 0))
    else:
        tc_count = 0

    if isinstance(run.get("seconds"), (int, float)) and not isinstance(run.get("seconds"), bool):
        secs = float(run["seconds"])
    elif isinstance(effort, dict) and isinstance(effort.get("elapsed_ms"), (int, float)):
        secs = float(effort["elapsed_ms"]) / 1000.0
    else:
        secs = 0.0

    return tc_count, in_tok, out_tok, secs


def _extract_timestamps(run: dict[str, Any]) -> tuple[str | None, str | None]:
    ts = run.get("timestamps")
    if isinstance(ts, dict):
        return (
            str(ts["started_at"]) if ts.get("started_at") else None,
            str(ts["completed_at"]) if ts.get("completed_at") else None,
        )
    return (
        str(run["started_at"]) if run.get("started_at") else None,
        str(run["completed_at"]) if run.get("completed_at") else None,
    )


def _load_conditions_override(raw_arg: str | None) -> dict[str, Any]:
    if not raw_arg:
        return {}
    stripped = raw_arg.strip()
    candidate_path = Path(stripped)
    if candidate_path.is_file():
        parsed = json.loads(candidate_path.read_text(encoding="utf-8"))
    else:
        parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("--conditions-json must decode to a JSON object")
    return parsed


def _compute_differences(
    arm_list: Sequence[str],
    question_ids: Sequence[str],
    per_question_arm_accuracy: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    arm_set = set(arm_list)
    diffs: list[dict[str, Any]] = []
    for left_arm, right_arm, label in COMPARISON_PAIRS:
        if left_arm not in arm_set or right_arm not in arm_set:
            continue
        left_scores = [per_question_arm_accuracy[(qid, left_arm)] for qid in question_ids]
        right_scores = [per_question_arm_accuracy[(qid, right_arm)] for qid in question_ids]
        interval = paired_bootstrap_interval(left_scores, right_scores)
        diffs.append(
            {
                "comparison": label,
                "left_arm": left_arm,
                "right_arm": right_arm,
                "metric": "answer_accuracy",
                "diff": interval["diff"],
                "low": interval["low"],
                "high": interval["high"],
                "excludes_zero": interval["excludes_zero"],
            }
        )
    return diffs


def _format_pct(value: float) -> str:
    return f"{value * 100.0:.1f}%"


def _format_ci(interval: dict[str, Any]) -> str:
    return (
        f"{_format_pct(interval['proportion'])} "
        f"[{_format_pct(interval['low'])}, {_format_pct(interval['high'])}]"
    )


def _format_diff_pp(diff_entry: dict[str, Any]) -> str:
    sign = "+" if diff_entry["diff"] >= 0 else ""
    lo_sign = "+" if diff_entry["low"] >= 0 else ""
    hi_sign = "+" if diff_entry["high"] >= 0 else ""
    return (
        f"{sign}{diff_entry['diff'] * 100.0:.1f} pp "
        f"[{lo_sign}{diff_entry['low'] * 100.0:.1f}, {hi_sign}{diff_entry['high'] * 100.0:.1f}]"
    )


def render_scorecard_html(scorecard: dict[str, Any]) -> str:
    """Render an accessible, responsive, self-contained HTML scorecard."""
    esc = html.escape
    cond = scorecard["conditions"]
    arms: list[str] = list(cond["arms"])
    labels: dict[str, str] = scorecard["arm_labels"]
    exec_badge = (
        "Offline Scripted Fixture"
        if cond["execution_mode"] == "offline_scripted"
        else "Live Demo Tenant"
    )

    arm_rows_html: list[str] = []
    for arm in arms:
        s = scorecard["summary_by_arm"][arm]
        eff = s["effort"]
        arm_rows_html.append(
            "<tr>"
            f"<th scope=\"row\">{esc(labels.get(arm, arm))}</th>"
            f"<td>{s['answer_correct_runs']} / {s['total_runs']}</td>"
            f"<td>{esc(_format_ci(s['answer_accuracy']))}</td>"
            f"<td>{esc(_format_pct(s['mean_key_f1']))}</td>"
            f"<td>{esc(_format_ci(s['path_accuracy']))}</td>"
            f"<td>{esc(_format_ci(s['unknown_handling_accuracy']))}</td>"
            f"<td>{esc(_format_pct(s['mean_repeat_stability']))}</td>"
            f"<td>{esc(_format_pct(s['unanimous_pass_rate']))}</td>"
            f"<td>{eff['mean_tool_calls']:.1f}</td>"
            f"<td>{eff['mean_input_tokens']:.0f} / {eff['mean_output_tokens']:.0f}</td>"
            f"<td>{eff['mean_seconds']:.2f}s</td>"
            "</tr>"
        )

    diff_rows_html: list[str] = []
    for d in scorecard["overall_differences"]:
        verdict = (
            '<span class="badge badge-sig">Excludes zero</span>'
            if d["excludes_zero"]
            else '<span class="badge badge-tie">Includes zero (tie / inconclusive)</span>'
        )
        diff_rows_html.append(
            "<tr>"
            f"<th scope=\"row\">{esc(d['comparison'])}</th>"
            f"<td>{esc(d['metric'])}</td>"
            f"<td>{esc(_format_diff_pp(d))}</td>"
            f"<td>{verdict}</td>"
            "</tr>"
        )

    level_headers = "".join(f"<th scope=\"col\">{esc(labels.get(a, a))}</th>" for a in arms)
    level_rows_html: list[str] = []
    for level_key, level_data in scorecard["by_level"].items():
        cells = "".join(
            f"<td>{esc(_format_ci(level_data['arms'][a]['answer_accuracy']))} "
            f"({level_data['arms'][a]['answer_correct_runs']}/{level_data['arms'][a]['runs']})</td>"
            for a in arms
        )
        diff_summary = "; ".join(
            f"{esc(d['comparison'])}: {esc(_format_diff_pp(d))} "
            f"({'excludes 0' if d['excludes_zero'] else 'includes 0'})"
            for d in level_data["differences"]
        ) or "Single evaluation arm"
        level_rows_html.append(
            "<tr>"
            f"<th scope=\"row\">{esc(level_key)}</th>"
            f"<td>{level_data['question_count']}</td>"
            f"{cells}"
            f"<td>{diff_summary}</td>"
            "</tr>"
        )

    trap_headers = "".join(f"<th scope=\"col\">{esc(t)}</th>" for t in VALID_TRAPS)
    trap_rows_html: list[str] = []
    for arm in arms:
        t_counts = scorecard["summary_by_arm"][arm]["traps_hit"]
        t_cells = "".join(f"<td>{int(t_counts.get(t, 0))}</td>" for t in VALID_TRAPS)
        trap_rows_html.append(
            f"<tr><th scope=\"row\">{esc(labels.get(arm, arm))}</th>{t_cells}</tr>"
        )

    failed_rows_html: list[str] = []
    for item in scorecard["failed_questions"]:
        traps_str = ", ".join(item["traps_hit"]) if item["traps_hit"] else "None"
        reasons_str = ", ".join(item["failure_reasons"]) if item["failure_reasons"] else "None"
        failed_rows_html.append(
            "<tr>"
            f"<td><code>{esc(item['question_id'])}</code></td>"
            f"<td>{esc(item['level'])}</td>"
            f"<td>{esc(labels.get(item['arm'], item['arm']))}</td>"
            f"<td>{item['correct_repeats']} / {item['repeats']}</td>"
            f"<td>{esc(traps_str)}</td>"
            f"<td>{esc(reasons_str)}</td>"
            "</tr>"
        )
    if not failed_rows_html:
        failed_rows_html.append(
            "<tr><td colspan=\"6\">All evaluated questions passed across all repeats.</td></tr>"
        )

    limitations_html = "".join(
        f"<li>{esc(lim)}</li>" for lim in scorecard["limitations"]
    )

    sku_str = esc(str(cond.get("capacity_sku") or "n/a"))
    region_str = esc(str(cond.get("region") or "n/a"))
    model_str = esc(str(cond.get("model_deployment") or "n/a"))
    variant_str = esc(str(cond.get("toolset_variant") or "n/a"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Northstar Service Assurance — Evaluation Scorecard</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --surface: #ffffff;
      --ink: #0f172a;
      --muted: #475569;
      --border: #cbd5e1;
      --accent: #0f4c81;
      --sig-bg: #dcfce7;
      --sig-ink: #14532d;
      --tie-bg: #f1f5f9;
      --tie-ink: #334155;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 2rem 1.25rem;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--ink);
      line-height: 1.5;
    }}
    main {{ max-width: 1240px; margin: 0 auto; }}
    header {{ margin-bottom: 1.75rem; }}
    h1 {{ margin: 0 0 0.5rem; font-size: 1.75rem; }}
    h2 {{ margin: 2rem 0 0.75rem; font-size: 1.25rem; color: var(--accent); }}
    p.lead {{ margin: 0; color: var(--muted); }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 0.75rem;
      margin: 1.25rem 0;
    }}
    .meta-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.75rem 1rem;
    }}
    .meta-card dt {{ font-size: 0.75rem; text-transform: uppercase; color: var(--muted); }}
    .meta-card dd {{ margin: 0.25rem 0 0; font-weight: 600; font-size: 0.95rem; }}
    .table-wrap {{
      overflow-x: auto;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      margin-bottom: 1.5rem;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    caption {{
      text-align: left;
      padding: 0.75rem 1rem;
      font-weight: 600;
      border-bottom: 1px solid var(--border);
      background: #f1f5f9;
    }}
    th, td {{
      padding: 0.65rem 0.85rem;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
    }}
    thead th {{ background: #f8fafc; font-weight: 600; }}
    tbody tr:last-child th, tbody tr:last-child td {{ border-bottom: none; }}
    .badge {{
      display: inline-block;
      padding: 0.15rem 0.55rem;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 600;
    }}
    .badge-sig {{ background: var(--sig-bg); color: var(--sig-ink); }}
    .badge-tie {{ background: var(--tie-bg); color: var(--tie-ink); }}
    ul.limitations {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1rem 1.25rem 1rem 2.25rem;
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Northstar Service Assurance — Evaluation Scorecard</h1>
      <p class="lead">
        Each evaluation arm is scored deterministically against the DuckDB oracle over identical
        questions, data, and repeat counts. Proportions use 95% Wilson intervals; arm comparisons
        use 95% paired bootstrap intervals over questions.
      </p>
    </header>

    <dl class="meta-grid" aria-label="Run conditions">
      <div class="meta-card"><dt>Execution mode</dt><dd>{esc(exec_badge)}</dd></div>
      <div class="meta-card"><dt>Profile &amp; seed</dt><dd>{esc(cond['profile'])} ({cond['seed']})</dd></div>
      <div class="meta-card"><dt>Questions &times; repeats</dt><dd>{cond['question_count']} &times; k={cond['repeats']}</dd></div>
      <div class="meta-card"><dt>Model deployment</dt><dd>{model_str}</dd></div>
      <div class="meta-card"><dt>Toolset variant</dt><dd>{variant_str}</dd></div>
      <div class="meta-card"><dt>Capacity &amp; region</dt><dd>{sku_str} / {region_str}</dd></div>
    </dl>

    <h2>Overall Summary by Evaluation Arm</h2>
    <div class="table-wrap">
      <table>
        <caption>Accuracy, set F1, path &amp; unknown handling, stability, and effort</caption>
        <thead>
          <tr>
            <th scope="col">Evaluation arm</th>
            <th scope="col">Correct runs</th>
            <th scope="col">Answer accuracy (95% Wilson)</th>
            <th scope="col">Mean key F1</th>
            <th scope="col">Path accuracy</th>
            <th scope="col">Unknown handling</th>
            <th scope="col">Stability</th>
            <th scope="col">Pass all k</th>
            <th scope="col">Mean tool calls</th>
            <th scope="col">Mean tokens (in / out)</th>
            <th scope="col">Mean seconds</th>
          </tr>
        </thead>
        <tbody>
          {"".join(arm_rows_html)}
        </tbody>
      </table>
    </div>

    <h2>Paired Bootstrap Differences (95% CI)</h2>
    <div class="table-wrap">
      <table>
        <caption>Per-question paired bootstrap differences in answer accuracy</caption>
        <thead>
          <tr>
            <th scope="col">Comparison</th>
            <th scope="col">Metric</th>
            <th scope="col">Difference [95% CI]</th>
            <th scope="col">Interval check</th>
          </tr>
        </thead>
        <tbody>
          {"".join(diff_rows_html)}
        </tbody>
      </table>
    </div>

    <h2>Breakdown by Question Level</h2>
    <div class="table-wrap">
      <table>
        <caption>Answer accuracy and paired differences across L1–L4, N, and P</caption>
        <thead>
          <tr>
            <th scope="col">Level</th>
            <th scope="col">Questions</th>
            {level_headers}
            <th scope="col">Paired differences</th>
          </tr>
        </thead>
        <tbody>
          {"".join(level_rows_html)}
        </tbody>
      </table>
    </div>

    <h2>Failure Modes (Traps Hit)</h2>
    <div class="table-wrap">
      <table>
        <caption>Count of runs matching each known schema-only failure trap</caption>
        <thead>
          <tr>
            <th scope="col">Evaluation arm</th>
            {trap_headers}
          </tr>
        </thead>
        <tbody>
          {"".join(trap_rows_html)}
        </tbody>
      </table>
    </div>

    <h2>Failed Questions</h2>
    <div class="table-wrap">
      <table>
        <caption>Questions with fewer than k correct repeats</caption>
        <thead>
          <tr>
            <th scope="col">Question ID</th>
            <th scope="col">Level</th>
            <th scope="col">Evaluation arm</th>
            <th scope="col">Correct repeats</th>
            <th scope="col">Traps hit</th>
            <th scope="col">Failure reasons</th>
          </tr>
        </thead>
        <tbody>
          {"".join(failed_rows_html)}
        </tbody>
      </table>
    </div>

    <h2>Limitations</h2>
    <ul class="limitations">
      {limitations_html}
    </ul>
  </main>
</body>
</html>
"""


def build_scorecard(
    *,
    runs_path: Path,
    questions_path: Path = DEFAULT_QUESTIONS,
    profile: str = "service-assurance",
    seed: int = DEFAULT_SEED,
    data_dir: Path | None = None,
    conditions_override: dict[str, Any] | None = None,
    output_json_path: Path | None = DEFAULT_OUTPUT_JSON,
    output_html_path: Path | None = DEFAULT_OUTPUT_HTML,
) -> dict[str, Any]:
    """Score all arm runs against the DuckDB oracle and write ``scorecard.json`` / ``scorecard.html``."""
    questions = load_questions(questions_path)
    question_by_id = {q["id"]: q for q in questions}
    question_order = [q["id"] for q in questions]

    raw_runs = _load_runs_file(runs_path)
    folder = ensure_profile_data(profile, seed=seed, data_dir=data_dir)
    manifest_path = folder / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {"seed": seed}
    )

    oracle_by_qid: dict[str, dict[str, Any]] = {}
    traps_by_qid: dict[str, dict[str, dict[str, Any]]] = {}
    for q in questions:
        qid = q["id"]
        oracle_by_qid[qid] = evaluate_question(q, folder)
        traps_by_qid[qid] = {
            trap_name: evaluate_trap(trap_name, q, folder)
            for trap_name in q["traps"]
        }

    observed_arms_set = {str(r["arm"]) for r in raw_runs if r.get("arm")}
    ordered_arms = [a for a in VALID_ARMS if a in observed_arms_set] + sorted(
        observed_arms_set - set(VALID_ARMS)
    )
    if not ordered_arms:
        raise ValueError("No valid arm names found in runs")

    runs_by_arm_q: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    scored_by_arm_q: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    all_started: list[str] = []
    all_completed: list[str] = []
    deployments: Counter[str] = Counter()
    variants: Counter[str] = Counter()
    repeat_numbers: set[int] = set()

    for run in raw_runs:
        qid = str(run["question_id"])
        if qid not in question_by_id:
            continue
        arm = str(run["arm"])
        rep = int(run.get("repeat", 1))
        repeat_numbers.add(rep)

        started_at, completed_at = _extract_timestamps(run)
        if started_at:
            all_started.append(started_at)
        if completed_at:
            all_completed.append(completed_at)
        if run.get("model_deployment"):
            deployments[str(run["model_deployment"])] += 1
        if run.get("toolset_variant"):
            variants[str(run["toolset_variant"])] += 1

        q = question_by_id[qid]
        scored = score_run(run, q, oracle_by_qid[qid], traps_by_qid[qid])
        runs_by_arm_q[arm][qid].append(run)
        scored_by_arm_q[arm][qid].append(scored)

    repeats_count = max(
        [len(repeat_numbers), 1]
        + [
            len(q_runs)
            for arm_map in scored_by_arm_q.values()
            for q_runs in arm_map.values()
        ]
    )

    model_deployment = deployments.most_common(1)[0][0] if deployments else "unknown"
    toolset_variant = variants.most_common(1)[0][0] if variants else "service_default"
    inferred_exec_mode = (
        "offline_scripted"
        if model_deployment == "scripted-offline-fixture"
        or toolset_variant.startswith("offline")
        else "live_demo_tenant"
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    overrides = dict(conditions_override or {})
    execution_mode = str(overrides.get("execution_mode", inferred_exec_mode))

    conditions: dict[str, Any] = {
        "execution_mode": execution_mode,
        "profile": str(overrides.get("profile", profile)),
        "seed": int(overrides.get("seed", manifest.get("seed", seed))),
        "question_count": len(questions),
        "repeats": int(overrides.get("repeats", repeats_count)),
        "arms": ordered_arms,
        "capacity_sku": overrides.get("capacity_sku"),
        "region": overrides.get("region"),
        "model_deployment": str(overrides.get("model_deployment", model_deployment)),
        "toolset_variant": str(overrides.get("toolset_variant", toolset_variant)),
        "started_at": str(overrides.get("started_at", min(all_started) if all_started else now_iso)),
        "completed_at": str(
            overrides.get("completed_at", max(all_completed) if all_completed else now_iso)
        ),
    }

    per_question_arm_accuracy: dict[tuple[str, str], float] = {}
    per_question_arm_agreement: dict[tuple[str, str], dict[str, float]] = {}

    for arm in ordered_arms:
        for qid in question_order:
            q_scored = scored_by_arm_q[arm].get(qid, [])
            if q_scored:
                mean_acc = sum(1.0 for s in q_scored if s["answer_correct"]) / len(q_scored)
                agreement = repeat_agreement(q_scored, question_by_id[qid])
            else:
                mean_acc = 0.0
                agreement = {"modal_share": 0.0, "pass_all_k": 0.0}
            per_question_arm_accuracy[(qid, arm)] = mean_acc
            per_question_arm_agreement[(qid, arm)] = agreement

    summary_by_arm: dict[str, Any] = {}
    for arm in ordered_arms:
        all_scored = [
            s
            for qid in question_order
            for s in scored_by_arm_q[arm].get(qid, [])
        ]
        all_raw = [
            r
            for qid in question_order
            for r in runs_by_arm_q[arm].get(qid, [])
        ]
        total_runs = len(all_scored)
        correct_runs = sum(1 for s in all_scored if s["answer_correct"])
        path_ok_runs = sum(1 for s in all_scored if s["path_correct"])
        unk_ok_runs = sum(1 for s in all_scored if s["unknown_handling_correct"])

        mean_prec = (
            round(sum(s["key_precision"] for s in all_scored) / total_runs, 6)
            if total_runs
            else 0.0
        )
        mean_rec = (
            round(sum(s["key_recall"] for s in all_scored) / total_runs, 6)
            if total_runs
            else 0.0
        )
        mean_f1 = (
            round(sum(s["key_f1"] for s in all_scored) / total_runs, 6)
            if total_runs
            else 0.0
        )

        stabilities = [per_question_arm_agreement[(qid, arm)]["modal_share"] for qid in question_order]
        pass_all_vals = [per_question_arm_agreement[(qid, arm)]["pass_all_k"] for qid in question_order]

        trap_counter: dict[str, int] = {trap: 0 for trap in VALID_TRAPS}
        for s in all_scored:
            for t_name in s["traps_hit"]:
                trap_counter[t_name] = trap_counter.get(t_name, 0) + 1

        efforts = [_extract_effort_metrics(r) for r in all_raw]
        if efforts:
            mean_tc = round(sum(e[0] for e in efforts) / len(efforts), 3)
            mean_in = round(sum(e[1] for e in efforts) / len(efforts), 1)
            mean_out = round(sum(e[2] for e in efforts) / len(efforts), 1)
            mean_sec = round(sum(e[3] for e in efforts) / len(efforts), 4)
            tot_tokens = sum(e[1] + e[2] for e in efforts)
        else:
            mean_tc = 0.0
            mean_in = 0.0
            mean_out = 0.0
            mean_sec = 0.0
            tot_tokens = 0

        summary_by_arm[arm] = {
            "arm": arm,
            "total_runs": total_runs,
            "question_count": len(questions),
            "repeats": conditions["repeats"],
            "answer_correct_runs": correct_runs,
            "answer_accuracy": wilson_interval(correct_runs, total_runs),
            "mean_key_precision": mean_prec,
            "mean_key_recall": mean_rec,
            "mean_key_f1": mean_f1,
            "path_accuracy": wilson_interval(path_ok_runs, total_runs),
            "unknown_handling_accuracy": wilson_interval(unk_ok_runs, total_runs),
            "mean_repeat_stability": round(sum(stabilities) / len(stabilities), 6),
            "unanimous_pass_rate": round(sum(pass_all_vals) / len(pass_all_vals), 6),
            "traps_hit": trap_counter,
            "effort": {
                "mean_tool_calls": mean_tc,
                "mean_input_tokens": mean_in,
                "mean_output_tokens": mean_out,
                "mean_seconds": mean_sec,
                "total_tokens": tot_tokens,
            },
        }

    by_level: dict[str, Any] = {}
    for level in VALID_LEVELS:
        level_qids = [q["id"] for q in questions if q["level"] == level]
        if not level_qids:
            continue
        level_arms: dict[str, Any] = {}
        for arm in ordered_arms:
            lvl_scored = [
                s
                for qid in level_qids
                for s in scored_by_arm_q[arm].get(qid, [])
            ]
            lvl_runs = len(lvl_scored)
            lvl_correct = sum(1 for s in lvl_scored if s["answer_correct"])
            lvl_path_ok = sum(1 for s in lvl_scored if s["path_correct"])
            lvl_unk_ok = sum(1 for s in lvl_scored if s["unknown_handling_correct"])
            lvl_f1 = (
                round(sum(s["key_f1"] for s in lvl_scored) / lvl_runs, 6)
                if lvl_runs
                else 0.0
            )
            lvl_stab = [
                per_question_arm_agreement[(qid, arm)]["modal_share"] for qid in level_qids
            ]
            lvl_pass_k = [
                per_question_arm_agreement[(qid, arm)]["pass_all_k"] for qid in level_qids
            ]
            lvl_traps: dict[str, int] = {trap: 0 for trap in VALID_TRAPS}
            for s in lvl_scored:
                for t_name in s["traps_hit"]:
                    lvl_traps[t_name] = lvl_traps.get(t_name, 0) + 1

            level_arms[arm] = {
                "runs": lvl_runs,
                "total_runs": lvl_runs,
                "answer_correct_runs": lvl_correct,
                "answer_accuracy": wilson_interval(lvl_correct, lvl_runs),
                "mean_key_f1": lvl_f1,
                "path_accuracy": wilson_interval(lvl_path_ok, lvl_runs),
                "unknown_handling_accuracy": wilson_interval(lvl_unk_ok, lvl_runs),
                "mean_repeat_stability": round(sum(lvl_stab) / len(lvl_stab), 6),
                "unanimous_pass_rate": round(sum(lvl_pass_k) / len(lvl_pass_k), 6),
                "traps_hit": lvl_traps,
            }

        by_level[level] = {
            "question_count": len(level_qids),
            "arms": level_arms,
            "differences": _compute_differences(
                ordered_arms, level_qids, per_question_arm_accuracy
            ),
        }

    overall_differences = _compute_differences(
        ordered_arms, question_order, per_question_arm_accuracy
    )

    failed_questions: list[dict[str, Any]] = []
    for arm in ordered_arms:
        for qid in question_order:
            q_scored = scored_by_arm_q[arm].get(qid, [])
            if not q_scored:
                continue
            correct_k = sum(1 for s in q_scored if s["answer_correct"])
            total_k = len(q_scored)
            if correct_k < total_k:
                unique_traps: list[str] = []
                unique_reasons: list[str] = []
                for s in q_scored:
                    for t_name in s["traps_hit"]:
                        if t_name not in unique_traps:
                            unique_traps.append(t_name)
                    for reason in s["failure_reasons"]:
                        if reason not in unique_reasons:
                            unique_reasons.append(reason)
                failed_questions.append(
                    {
                        "question_id": qid,
                        "level": question_by_id[qid]["level"],
                        "arm": arm,
                        "correct_repeats": correct_k,
                        "repeats": total_k,
                        "traps_hit": unique_traps,
                        "failure_reasons": unique_reasons,
                    }
                )

    if execution_mode == "offline_scripted":
        limitations = [
            "Offline scripted fixtures verify the evaluation runner, scoring math, and scorecard "
            "rendering in CI; they are not a live model or demo tenant benchmark.",
            "Claims of accuracy advantage require a live demo tenant scorecard whose 95% paired "
            "bootstrap interval for the difference excludes zero.",
            "Ties, partial results with explicit exclusions, and failed questions are published "
            "without filtering.",
        ]
    else:
        limitations = [
            "Results reflect the synthetic Northstar Wireless service assurance scenario in the "
            "recorded demo tenant configuration (model deployment, capacity size, and region).",
            "A statement of accuracy benefit is supported only where the 95% paired bootstrap "
            "interval excludes zero; ties and inconclusive intervals are published alongside wins.",
            "Workspace, tenant, and item UUIDs are omitted from published scorecards.",
        ]

    scorecard: dict[str, Any] = {
        "schema_version": "1.0",
        "mode": "RECORDED_SCORECARD",
        "generated_at": now_iso,
        "conditions": conditions,
        "arm_labels": dict(ARM_LABELS),
        "summary_by_arm": summary_by_arm,
        "by_level": by_level,
        "overall_differences": overall_differences,
        "failed_questions": failed_questions,
        "limitations": limitations,
    }

    if output_json_path is not None:
        json_dest = output_json_path.resolve()
        json_dest.parent.mkdir(parents=True, exist_ok=True)
        json_dest.write_text(
            json.dumps(scorecard, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    if output_html_path is not None:
        html_dest = output_html_path.resolve()
        html_dest.parent.mkdir(parents=True, exist_ok=True)
        html_dest.write_text(
            render_scorecard_html(scorecard),
            encoding="utf-8",
            newline="\n",
        )

    return scorecard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        type=Path,
        required=True,
        help="Path to JSONL arm runs file produced by run_arms.py",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        default=DEFAULT_QUESTIONS,
        help="Path to golden questions JSONL file",
    )
    parser.add_argument(
        "--profile",
        default="service-assurance",
        help="Synthetic data profile used to evaluate oracle and traps",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="Synthetic generator seed",
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
        "--conditions-json",
        type=str,
        default=None,
        help="Optional JSON string or JSON file path overriding capacity_sku, region, etc.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=DEFAULT_OUTPUT_JSON,
        help="Output path for scorecard.json",
    )
    parser.add_argument(
        "--output-html",
        type=Path,
        default=DEFAULT_OUTPUT_HTML,
        help="Output path for static scorecard.html",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    overrides = _load_conditions_override(args.conditions_json)
    scorecard = build_scorecard(
        runs_path=args.runs,
        questions_path=args.questions,
        profile=args.profile,
        seed=args.seed,
        data_dir=args.data_dir,
        conditions_override=overrides,
        output_json_path=args.output_json,
        output_html_path=args.output_html,
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "mode": scorecard["mode"],
                "execution_mode": scorecard["conditions"]["execution_mode"],
                "arms": scorecard["conditions"]["arms"],
                "question_count": scorecard["conditions"]["question_count"],
                "repeats": scorecard["conditions"]["repeats"],
                "output_json": args.output_json.name if args.output_json else None,
                "output_html": args.output_html.name if args.output_html else None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
