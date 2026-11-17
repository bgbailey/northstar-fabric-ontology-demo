"""Deterministic evaluation scoring, trap detection, and statistical intervals (M4-03).

All numeric and set-level metrics are computed with exact arithmetic and explicit
non-parametric resampling; no model-graded scoring is used.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

INT_STRING_RE = re.compile(r"^-?\d+$")


def wilson_interval(
    successes: int,
    total: int,
    z: float = 1.959963984540054,
) -> dict[str, float]:
    """Compute a Wilson score confidence interval for a binomial proportion."""
    if total < 0 or successes < 0 or (total > 0 and successes > total):
        raise ValueError(f"Invalid binomial counts: successes={successes}, total={total}")
    if z < 0.0:
        raise ValueError(f"z must be non-negative, got {z}")
    if total == 0:
        return {"proportion": 0.0, "low": 0.0, "high": 0.0}

    n = float(total)
    p_hat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    radicand = max(0.0, (p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n)
    half = (z * math.sqrt(radicand)) / denom

    proportion = round(p_hat, 6)
    low = round(max(0.0, min(1.0, center - half)), 6)
    high = round(max(0.0, min(1.0, center + half)), 6)
    low = min(low, proportion)
    high = max(high, proportion)
    return {"proportion": proportion, "low": low, "high": high}


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = max(0.0, min(1.0, q)) * (len(sorted_values) - 1)
    lo_idx = math.floor(pos)
    hi_idx = math.ceil(pos)
    if lo_idx == hi_idx:
        return float(sorted_values[lo_idx])
    frac = pos - lo_idx
    return float(sorted_values[lo_idx]) * (1.0 - frac) + float(sorted_values[hi_idx]) * frac


def paired_bootstrap_interval(
    left_by_question: Sequence[float],
    right_by_question: Sequence[float],
    *,
    resamples: int = 5000,
    seed: int = 20261117,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Compute a paired percentile bootstrap interval over per-question score differences."""
    if len(left_by_question) != len(right_by_question):
        raise ValueError(
            f"Paired sequences must have identical length: "
            f"{len(left_by_question)} != {len(right_by_question)}"
        )
    if resamples < 1:
        raise ValueError("resamples must be >= 1")
    if not (0.0 < confidence < 1.0):
        raise ValueError("confidence must be strictly between 0 and 1")

    n = len(left_by_question)
    if n == 0:
        return {"diff": 0.0, "low": 0.0, "high": 0.0, "excludes_zero": False}

    deltas = [
        float(left) - float(right)
        for left, right in zip(left_by_question, right_by_question, strict=True)
    ]
    observed_diff = sum(deltas) / n

    rng = random.Random(seed)
    boot_means = sorted(
        sum(rng.choices(deltas, k=n)) / n
        for _ in range(resamples)
    )
    alpha = 1.0 - confidence
    raw_low = _percentile(boot_means, alpha / 2.0)
    raw_high = _percentile(boot_means, 1.0 - alpha / 2.0)

    diff_rounded = round(observed_diff, 6)
    low_rounded = round(min(raw_low, observed_diff), 6)
    high_rounded = round(max(raw_high, observed_diff), 6)
    excludes_zero = bool(low_rounded > 0.0 or high_rounded < 0.0)
    return {
        "diff": diff_rounded,
        "low": low_rounded,
        "high": high_rounded,
        "excludes_zero": excludes_zero,
    }


def _normalize_key_component(value: Any) -> Any:
    """Normalize key primitives (allowing integer strings in keys without coercing booleans)."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if INT_STRING_RE.match(stripped):
            return int(stripped)
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _extract_key_tuple(row: dict[str, Any], key_cols: Sequence[str]) -> tuple[Any, ...] | None:
    if not isinstance(row, dict):
        return None
    parts: list[Any] = []
    for col in key_cols:
        if col not in row or row[col] is None:
            return None
        parts.append(_normalize_key_component(row[col]))
    return tuple(parts)


def _exact_int_equal(observed: Any, expected: Any) -> bool:
    """Compare integer cents and counts with zero floating-point approximation."""
    if isinstance(observed, bool) or isinstance(expected, bool):
        return False
    if expected is None:
        return observed is None
    if observed is None or isinstance(observed, float):
        return False
    exp_int = int(expected)
    if isinstance(observed, int):
        return observed == exp_int
    if isinstance(observed, str):
        stripped = observed.strip()
        if INT_STRING_RE.match(stripped):
            return int(stripped) == exp_int
    return False


def _canonical_cell_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if INT_STRING_RE.match(stripped):
            return int(stripped)
        return value
    if isinstance(value, float):
        return ("float", value)
    return str(value)


def _canonical_table(
    rows: Sequence[dict[str, Any]],
    key_cols: Sequence[str],
    val_cols: Sequence[str],
) -> tuple[bool, list[tuple[tuple[Any, ...], tuple[Any, ...]]]]:
    """Convert rows into a sorted canonical representation (valid_keys_flag, entries)."""
    entries: list[tuple[tuple[Any, ...], tuple[Any, ...]]] = []
    seen_keys: set[tuple[Any, ...]] = set()
    all_valid = True
    for row in rows:
        key = _extract_key_tuple(row, key_cols)
        if key is None or key in seen_keys:
            all_valid = False
            fallback_key = ("__invalid__", len(entries), repr(row))
            vals = (
                tuple(_canonical_cell_value(row.get(c)) for c in val_cols)
                if isinstance(row, dict)
                else ()
            )
            entries.append((fallback_key, vals))
            continue
        seen_keys.add(key)
        vals = tuple(_canonical_cell_value(row.get(c)) for c in val_cols)
        entries.append((key, vals))
    entries.sort(key=lambda item: repr(item[0]))
    return all_valid, entries


def canonical_table_digest(
    rows: Sequence[dict[str, Any]],
    question: dict[str, Any],
    *,
    status: str = "COMPLETE",
) -> str:
    """Compute a deterministic SHA-256 digest of a run's canonical answer table."""
    if status == "FAILED":
        raw_fail = json.dumps({"status": "FAILED", "rows": list(rows)}, sort_keys=True, default=repr)
        return "FAILED:" + hashlib.sha256(raw_fail.encode("utf-8")).hexdigest()
    key_cols = list(question["answer"]["keys"])
    val_cols = [c["name"] for c in question["answer"]["columns"]]
    valid_flag, entries = _canonical_table(rows, key_cols, val_cols)
    serializable = {
        "valid_keys": valid_flag,
        "entries": [[list(k), list(v)] for k, v in entries],
    }
    encoded = json.dumps(serializable, separators=(",", ":"), default=repr).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def score_run(
    run: dict[str, Any],
    question: dict[str, Any],
    oracle_result: dict[str, Any],
    trap_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Score one evaluation arm run deterministically against the DuckDB oracle and traps."""
    key_cols: list[str] = list(question["answer"]["keys"])
    val_cols: list[str] = [c["name"] for c in question["answer"]["columns"]]

    run_status = str(run.get("status", "COMPLETE"))
    obs_rows_raw = run.get("rows")
    obs_rows: list[dict[str, Any]] = (
        [r for r in obs_rows_raw if isinstance(r, dict)]
        if isinstance(obs_rows_raw, list)
        else []
    )
    malformed_row_entries = (
        not isinstance(obs_rows_raw, list) or len(obs_rows) != len(obs_rows_raw)
    )
    exp_rows: list[dict[str, Any]] = list(oracle_result.get("rows", []))

    obs_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    duplicate_or_missing_key = malformed_row_entries
    for row in obs_rows:
        key = _extract_key_tuple(row, key_cols)
        if key is None or key in obs_by_key:
            duplicate_or_missing_key = True
            continue
        obs_by_key[key] = row

    exp_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in exp_rows:
        key = _extract_key_tuple(row, key_cols)
        if key is not None:
            exp_by_key[key] = row

    obs_key_set = set(obs_by_key.keys())
    exp_key_set = set(exp_by_key.keys())

    if not obs_key_set and not exp_key_set:
        key_precision = 1.0
        key_recall = 1.0
        key_f1 = 1.0
    else:
        overlap = len(obs_key_set & exp_key_set)
        key_precision = overlap / len(obs_key_set) if obs_key_set else 0.0
        key_recall = overlap / len(exp_key_set) if exp_key_set else 0.0
        key_f1 = (
            2.0 * key_precision * key_recall / (key_precision + key_recall)
            if (key_precision + key_recall) > 0.0
            else 0.0
        )

    keys_match = (obs_key_set == exp_key_set) and not duplicate_or_missing_key
    cells_match = True
    null_coerced_to_zero = False
    for key in obs_key_set & exp_key_set:
        obs_row = obs_by_key[key]
        exp_row = exp_by_key[key]
        for col in val_cols:
            exp_val = exp_row.get(col)
            obs_val = obs_row.get(col) if col in obs_row else "__MISSING__"
            if exp_val is None and obs_val in (0, 0.0, "0"):
                null_coerced_to_zero = True
            if obs_val == "__MISSING__" or not _exact_int_equal(obs_val, exp_val):
                cells_match = False

    answer_correct = bool(
        run_status != "FAILED"
        and keys_match
        and cells_match
    )

    traps_hit: list[str] = []
    if not answer_correct and run_status != "FAILED" and trap_results:
        obs_valid, obs_canon = _canonical_table(obs_rows, key_cols, val_cols)
        _, oracle_canon = _canonical_table(exp_rows, key_cols, val_cols)

        exact_trap_hits: list[str] = []
        key_trap_candidates: list[str] = []
        for trap_name, trap_payload in trap_results.items():
            trap_rows = trap_payload.get("rows", [])
            trap_valid, trap_canon = _canonical_table(trap_rows, key_cols, val_cols)
            if obs_valid and trap_valid and obs_canon == trap_canon and trap_canon != oracle_canon:
                exact_trap_hits.append(trap_name)
                continue
            trap_keys = {
                k
                for r in trap_rows
                if (k := _extract_key_tuple(r, key_cols)) is not None
            }
            if (
                obs_valid
                and obs_key_set != exp_key_set
                and trap_keys != exp_key_set
                and obs_key_set == trap_keys
            ):
                key_trap_candidates.append(trap_name)

        traps_hit = exact_trap_hits if exact_trap_hits else key_trap_candidates

    exclusions_expected = bool(
        question["answer"].get("exclusionsExpected", False)
        or len(oracle_result.get("exclusions", [])) > 0
    )
    obs_exclusions = run.get("exclusions")
    has_reported_exclusions = isinstance(obs_exclusions, list) and len(obs_exclusions) > 0

    unknown_handling_correct = bool(
        run_status != "FAILED"
        and not null_coerced_to_zero
        and "missing-as-zero" not in traps_hit
        and (not exclusions_expected or has_reported_exclusions)
    )

    raw_path = run.get("path")
    path_dict = raw_path if isinstance(raw_path, dict) else {}
    models_queried = set(path_dict.get("models_queried") or [])
    required_models = set(question["models"])
    bridges_used = path_dict.get("bridges_used") or []
    validity_checks = path_dict.get("validity_checks") or []

    path_correct = bool(
        run_status != "FAILED"
        and required_models <= models_queried
        and (len(required_models) < 2 or len(bridges_used) > 0)
        and ("R1" not in question.get("rules", []) or len(validity_checks) > 0)
    )

    failure_reasons: list[str] = []
    if not answer_correct:
        if run_status == "FAILED":
            failure_reasons.append("run_failed")
        if duplicate_or_missing_key:
            failure_reasons.append("duplicate_or_malformed_keys")
        if obs_key_set != exp_key_set:
            failure_reasons.append("key_set_mismatch")
        if not cells_match:
            failure_reasons.append("numeric_value_mismatch")
        for trap_name in traps_hit:
            failure_reasons.append(f"trap:{trap_name}")
    if not unknown_handling_correct:
        if exclusions_expected and not has_reported_exclusions:
            failure_reasons.append("missing_expected_exclusions")
        if null_coerced_to_zero or "missing-as-zero" in traps_hit:
            failure_reasons.append("unknown_coerced_to_zero")

    return {
        "question_id": question["id"],
        "level": question["level"],
        "arm": run.get("arm", ""),
        "repeat": int(run.get("repeat", 1)),
        "status": run_status,
        "answer_correct": answer_correct,
        "key_precision": round(key_precision, 6),
        "key_recall": round(key_recall, 6),
        "key_f1": round(key_f1, 6),
        "traps_hit": traps_hit,
        "unknown_handling_correct": unknown_handling_correct,
        "path_correct": path_correct,
        "canonical_table_digest": canonical_table_digest(
            obs_rows, question, status=run_status
        ),
        "failure_reasons": failure_reasons,
    }


def repeat_agreement(
    runs_for_question_arm: Sequence[dict[str, Any]],
    question: dict[str, Any],
    oracle_result: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Measure answer-table stability (modal share) and unanimous pass rate across k repeats."""
    k = len(runs_for_question_arm)
    if k == 0:
        return {"modal_share": 0.0, "pass_all_k": 0.0}

    digests: list[str] = []
    correct_flags: list[bool] = []
    for idx, item in enumerate(runs_for_question_arm):
        existing_digest = item.get("canonical_table_digest")
        if isinstance(existing_digest, str) and existing_digest:
            digests.append(existing_digest)
        else:
            status = str(item.get("status", "COMPLETE"))
            if status == "FAILED":
                digests.append(f"FAILED_RUN_{idx}")
            else:
                rows = item.get("rows") if isinstance(item.get("rows"), list) else []
                digests.append(canonical_table_digest(rows, question, status=status))

        if oracle_result is not None:
            scored = score_run(item, question, oracle_result, {})
            correct_flags.append(bool(scored["answer_correct"]))
        elif "answer_correct" in item:
            correct_flags.append(bool(item["answer_correct"]))
        else:
            correct_flags.append(False)

    counts = Counter(digests)
    modal_share = round(max(counts.values()) / k, 6)
    pass_all_k = 1.0 if all(correct_flags) else 0.0
    return {
        "modal_share": modal_share,
        "pass_all_k": pass_all_k,
    }
