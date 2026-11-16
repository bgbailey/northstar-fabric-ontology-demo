"""Independent evaluator: stdlib arithmetic on source facts; no runtime imports."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pyarrow.parquet as pq


def table(path: Path, name: str) -> list[dict]:
    return pq.read_table(path / f"{name}.parquet").to_pylist()


def month(value: object) -> str:
    return str(value)[:7]


def timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time(), timezone.utc)
    else:
        result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("Source temporal contract requires timezone")
    return result


def q01(path: Path) -> dict:
    grouped = defaultdict(lambda: [0, 0])
    for row in table(path, "subscription_month"):
        key = (month(row["month"]), row["market_id"])
        grouped[key][0] += row["disconnect"]
        grouped[key][1] += row["opening_active"]
    result = {}
    for (period, market), (disconnects, opening) in sorted(grouped.items()):
        result[f"{period}:{market}"] = {
            "disconnects": disconnects, "opening_active": opening,
            "churn_rate": float(Fraction(disconnects, opening)) if opening else None,
        }
    return result


def sweep_minutes(intervals: list[tuple[datetime, datetime]]) -> float:
    events = defaultdict(int)
    for start, end in intervals:
        if end <= start:
            continue
        events[start] += 1
        events[end] -= 1
    active, prior, seconds = 0, None, 0.0
    for point in sorted(events):
        if active and prior is not None:
            seconds += (point - prior).total_seconds()
        active += events[point]
        prior = point
    return seconds / 60


def q03(path: Path) -> dict:
    """Keep signed cohort cents; off-cohort defects use top-level settlement_diagnostics metadata."""
    incidents = table(path, "incident")
    mappings = table(path, "service_site_map")
    contracts = table(path, "contract")
    credits = table(path, "applied_credit")
    intervals = defaultdict(list)
    parameters = {}
    for event in incidents:
        for mapping in mappings:
            if event["site_key"] != mapping["site_key"]:
                continue
            for contract in contracts:
                if mapping["contract_key"] != contract["contract_key"]:
                    continue
                start = max(timestamp(event["start"]), timestamp(mapping["valid_from"]),
                            timestamp(contract["valid_from"]))
                end = min(timestamp(event["end"]), timestamp(mapping["valid_to"]),
                          timestamp(contract["valid_to"]))
                cursor = start
                while cursor < end:
                    after_month = (cursor.replace(day=28) + timedelta(days=4)).replace(
                        day=1, hour=0, minute=0, second=0, microsecond=0
                    )
                    bound = min(after_month, end)
                    key = (contract["contract_key"], mapping["service_key"], month(cursor))
                    intervals[key].append((cursor, bound))
                    parameters[key] = contract
                    cursor = bound
    exposure, downtime = defaultdict(float), defaultdict(float)
    for key, spans in intervals.items():
        contract = parameters[key]
        minutes = sweep_minutes(spans)
        group = (key[0], key[2])
        downtime[group] += minutes
        exposure[group] += max(0, minutes - contract["threshold_minutes"]) * contract["credit_per_minute_cents"]
    postings = defaultdict(list)
    for credit in credits:
        postings[(credit["contract_key"], month(credit["month"]))].append(credit.get("amount_cents"))
    result, outside_cohort_diagnostics = {}, []
    for contract_key, period in sorted(exposure.keys() | postings.keys()):
        key = (contract_key, period)
        versions = [c for c in contracts if c["contract_key"] == contract_key]
        begin = datetime.fromisoformat(period + "-01").replace(tzinfo=timezone.utc)
        finish = (begin.replace(day=28) + timedelta(days=4)).replace(day=1)
        amounts = postings[key]
        unknown = any(amount is None for amount in amounts)
        applied = None if unknown else sum(amounts)
        capped = None
        if key in exposure:
            caps = {c["credit_cap_cents"] for c in versions}
            if len(caps) != 1:
                raise ValueError("Versioned contracts need distinct surrogate contract keys")
            capped = min(exposure[key], next(iter(caps)))
        remaining = None if unknown or capped is None else round(capped - applied)
        issues = []
        if amounts and not versions:
            issues.append(("INVALID_SETTLEMENT", "ORPHAN_APPLIED_CREDIT"))
        elif amounts and not any(
            timestamp(c["valid_from"]) < finish and begin < timestamp(c["valid_to"]) for c in versions
        ):
            issues.append(("INVALID_SETTLEMENT", "APPLIED_CREDIT_OUTSIDE_CONTRACT_PERIOD"))
        if unknown:
            issues.append(("INCOMPLETE_SETTLEMENT", "MISSING_APPLIED_AMOUNT"))
        if any(amount is not None and amount < 0 for amount in amounts):
            issues.append(("INVALID_SETTLEMENT", "NEGATIVE_APPLIED_CREDITS"))
        if remaining is not None and remaining < 0:
            issues.append(("INVALID_SETTLEMENT", "OVERAPPLIED_CREDITS"))
        diagnostics = [{
            "classification": classification, "reason": reason,
            "contract_key": contract_key, "month": period + "-01",
            "eligible_cents": None if capped is None else round(capped),
            "applied_cents": applied if amounts else None, "credit_exposure_cents": remaining,
        } for classification, reason in issues]
        if key not in exposure:
            outside_cohort_diagnostics.extend(diagnostics)
            continue
        row = {
            "union_service_minutes": downtime[key],
            "gross_capped_cents": round(capped), "applied_cents": applied if amounts else None,
            "remaining_cents": remaining,
        }
        if diagnostics:
            row["settlement_diagnostics"] = diagnostics
        result[f"{period}:{contract_key}"] = row
    if outside_cohort_diagnostics:
        # Evaluator metadata, not additional incident-cohort rows or zero-valued settlements.
        result["settlement_diagnostics"] = outside_cohort_diagnostics
    return result


def q08(path: Path) -> dict:
    active = defaultdict(set)
    for row in table(path, "subscription_month"):
        if row["active_eom"]:
            active[month(row["month"])].add(row["account_key"])
    periods = sorted(active)[-3:]
    return {
        "unqualified_question": "AMBIGUOUS",
        "periods": periods,
        "eom_active_accounts": len(active[periods[-1]]),
        "average_monthly_active_accounts": sum(len(active[p]) for p in periods) / len(periods),
        "distinct_active_at_month_end_anytime": len(set().union(*(active[p] for p in periods))),
        "limitation": "Monthly snapshots do not prove served-anytime between snapshots",
    }


def q14(path: Path) -> dict:
    mappings = table(path, "billing_account_map")
    matched, unmapped, invalid_numeric_matches = 0, 0, 0
    for row in table(path, "account_month"):
        at = timestamp(row["month"])
        found = [m for m in mappings if m["billing_key"] == row["billing_key"]
                 and timestamp(m["valid_from"]) <= at < timestamp(m["valid_to"])]
        if len(found) > 1:
            raise ValueError("Ambiguous identity mapping")
        if not found:
            unmapped += 1
        else:
            if found[0]["account_key"] != row["account_key"]:
                raise ValueError("Mapping points to a different canonical account")
            matched += 1
            invalid_numeric_matches += row["billing_key"] != found[0]["account_key"]
    return {
        "matched": matched, "unmapped": unmapped,
        "rows_where_numeric_equality_is_wrong": invalid_numeric_matches,
        "direct_key_equality": "REJECT",
    }


def two_model(path: Path, period: str = "2026-08") -> list[dict]:
    lines, revenue = defaultdict(int), defaultdict(int)
    for row in table(path, "subscription_month"):
        if month(row["month"]) == period:
            lines[row["account_key"]] += row["active_eom"]
    for row in table(path, "account_month"):
        if month(row["month"]) == period:
            revenue[row["account_key"]] += row["revenue_cents"]
    if set(lines) != set(revenue):
        raise ValueError("Independent expected accounts do not align")
    return [{
        "account_key": account, "subscriptions.active_lines_eom": lines[account],
        "finance.recognized_service_revenue": revenue[account],
    } for account in sorted(lines)]
