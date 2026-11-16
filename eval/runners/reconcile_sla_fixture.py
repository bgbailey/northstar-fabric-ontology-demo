"""Evaluator-only source-fact reconciliation of actual submitted SLA fixture plans."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pyarrow.parquet as pq

from telco_iq.catalog import Catalog
from telco_iq.contracts import Scope, Status
from telco_iq.executor import Executor
from telco_iq.providers import OfflineProvider
from telco_iq.sla_fixture import build_sla_plan, sla_context

ROOT = Path(__file__).resolve().parents[2]
FACT_TABLES = ("incident", "site", "service_site_map", "contract", "applied_credit", "account_month")


def _union_minutes(spans: list[tuple[datetime, datetime]]) -> Fraction:
    events = defaultdict(int)
    for start, end in spans:
        if start < end:
            events[start] += 1
            events[end] -= 1
    active, previous, microseconds = 0, None, 0
    for point in sorted(events):
        if active and previous is not None:
            microseconds += (point - previous) // timedelta(microseconds=1)
        active += events[point]
        previous = point
    return Fraction(microseconds, 60_000_000)


def source_expectations(
    data_path: Path, *, period: str = "2026-07", incident_keys: list[int] | None = None,
) -> dict:
    """Derive signed settlement truth from raw facts; keep defects separate from arithmetic."""
    facts = {name: pq.read_table(data_path / f"{name}.parquet").to_pylist() for name in FACT_TABLES}
    month = date.fromisoformat(period + "-01")
    next_month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    begin = datetime.combine(month, datetime.min.time(), timezone.utc)
    finish = datetime.combine(next_month, datetime.min.time(), timezone.utc)
    contracts = {row["contract_key"]: row for row in facts["contract"]}
    if len(contracts) != len(facts["contract"]):
        raise AssertionError("Oracle requires unique effective contract surrogate keys")
    sites = {row["site_key"] for row in facts["site"]}
    selected = None if incident_keys is None else set(incident_keys)
    selected_contracts, membership = set(), []
    all_spans = defaultdict(list)
    unmatched_incidents, missing_contract_links = set(), set()
    for event in facts["incident"]:
        if event["end"] <= begin or event["start"] >= finish:
            continue
        selected_event = selected is None or event["incident_key"] in selected
        if event["site_key"] not in sites:
            if selected_event:
                raise AssertionError("Selected incident has no physical site registry link")
            continue
        matches = 0
        for mapping in facts["service_site_map"]:
            if event["site_key"] != mapping["site_key"]:
                continue
            contract = contracts.get(mapping["contract_key"])
            if contract is None:
                if selected_event:
                    missing_contract_links.add(mapping["contract_key"])
                continue
            start = max(begin, event["start"], mapping["valid_from"], contract["valid_from"])
            end = min(finish, event["end"], mapping["valid_to"], contract["valid_to"])
            if start >= end:
                continue
            matches += 1
            key = (contract["contract_key"], mapping["service_key"])
            all_spans[key].append((start, end))
            if selected_event:
                selected_contracts.add(contract["contract_key"])
                membership.append(dict(
                    incident_key=event["incident_key"], service_key=mapping["service_key"],
                    contract_key=contract["contract_key"], start=start.isoformat(), end=end.isoformat(),
                ))
        if selected_event and not matches:
            unmatched_incidents.add(event["incident_key"])
    gross = defaultdict(Fraction)
    for (contract_key, _service), spans in all_spans.items():
        terms = contracts[contract_key]
        chargeable_minutes = max(Fraction(), _union_minutes(spans) - terms["threshold_minutes"])
        gross[contract_key] += chargeable_minutes * terms["credit_per_minute_cents"]
    applied = defaultdict(int)
    applied_present, applied_unknown, applied_negative = set(), set(), set()
    for row in facts["applied_credit"]:
        if row["month"] == month:
            key = row["contract_key"]
            applied_present.add(key)
            if row.get("amount_cents") is None:
                applied_unknown.add(key)
            else:
                applied[key] += row["amount_cents"]
                if row["amount_cents"] < 0:
                    applied_negative.add(key)
    contract_rows, settlement_diagnostics = [], []
    account_sla = defaultdict(lambda: [0, 0])
    for key in sorted(selected_contracts | applied_present):
        terms = contracts.get(key)
        eligible = None
        if key in selected_contracts:
            eligible = min(Fraction(terms["credit_cap_cents"]), gross[key])
            if eligible.denominator != 1:
                raise AssertionError("Reviewed source facts must settle to exact integer cents")
            eligible = int(eligible)
        residual = None if key in applied_unknown or eligible is None else eligible - applied[key]
        applied_value = applied[key] if key in applied_present and key not in applied_unknown else None
        issues = []
        if key in applied_present and terms is None:
            issues.append(("INVALID_SETTLEMENT", "ORPHAN_APPLIED_CREDIT"))
        elif key in applied_present and not (terms["valid_from"] < finish and begin < terms["valid_to"]):
            issues.append(("INVALID_SETTLEMENT", "APPLIED_CREDIT_OUTSIDE_CONTRACT_PERIOD"))
        if key in applied_unknown:
            issues.append(("INCOMPLETE_SETTLEMENT", "MISSING_APPLIED_AMOUNT"))
        if key in applied_negative:
            issues.append(("INVALID_SETTLEMENT", "NEGATIVE_APPLIED_CREDITS"))
        if residual is not None and residual < 0:
            issues.append(("INVALID_SETTLEMENT", "OVERAPPLIED_CREDITS"))
        settlement_diagnostics.extend({
            "classification": classification, "reason": reason,
            "contract_key": key, "month": month.isoformat(),
            "eligible_cents": eligible, "applied_cents": applied_value,
            "credit_exposure_cents": residual,
        } for classification, reason in issues)
        if key not in selected_contracts:
            continue
        contract_rows.append({
            "contract_key": key, "account_key": terms["account_key"], "month": month.isoformat(),
            "enterprise.eligible_credits": eligible,
            "enterprise.applied_credits": applied_value,
            "enterprise.credit_exposure": residual,
        })
        account_sla[terms["account_key"]][0] += eligible
        prior_residual = account_sla[terms["account_key"]][1]
        account_sla[terms["account_key"]][1] = (
            None if prior_residual is None or residual is None else prior_residual + residual
        )
    finance = {}
    for row in facts["account_month"]:
        if row["month"] == month and row["account_key"] in account_sla:
            if row["account_key"] in finance:
                raise AssertionError("Duplicate Finance account-month fact")
            finance[row["account_key"]] = row
    if set(finance) != set(account_sla):
        raise AssertionError("Missing Finance account-month fact is not zero revenue")
    account_rows = [{
        "account_key": account, "month": month.isoformat(),
        "enterprise.eligible_credits": amounts[0],
        "enterprise.credit_exposure": amounts[1],
        "finance.recognized_service_revenue": finance[account]["revenue_cents"],
        "finance.contribution_margin": (
            finance[account]["revenue_cents"] - finance[account]["credit_cents"]
            - finance[account]["service_cost_cents"]
        ),
    } for account, amounts in sorted(account_sla.items())]
    return dict(
        contracts=contract_rows, accounts=account_rows,
        contract_keys=sorted(selected_contracts), account_keys=sorted(account_sla),
        membership=membership, unmatched_incidents=sorted(unmatched_incidents),
        missing_contract_links=sorted(missing_contract_links),
        settlement_diagnostics=settlement_diagnostics,
    )


def assert_rows(observed: list[dict], expected: list[dict], view: str) -> None:
    key = "contract_key" if view == "contracts" else "account_key"
    ordered = sorted(observed, key=lambda row: row[key])
    if ordered != expected:
        raise AssertionError({"view": view, "observed": ordered, "independent_expected": expected})
    for row in observed:
        for column, value in row.items():
            if "." in column and value is not None and type(value) is not int:
                raise AssertionError(f"{column} must return integer cents, not {type(value).__name__}")


async def reconcile(
    data_path: Path, *, period: str = "2026-07", incident_keys: list[int] | None = None,
) -> dict:
    expected = source_expectations(data_path, period=period, incident_keys=incident_keys)
    if any(issue["classification"] == "INCOMPLETE_SETTLEMENT"
           for issue in expected["settlement_diagnostics"]):
        raise AssertionError({
            "classification": "INCOMPLETE_SETTLEMENT",
            "settlement_diagnostics": expected["settlement_diagnostics"],
        })
    scope = Scope(
        tenant="synthetic", principal="local-sla-evaluator", authorization_revision="1",
        models=["network", "enterprise", "finance"], mode="OFFLINE_SYNTHETIC",
    )
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    executor = Executor(catalog, OfflineProvider(catalog, data_path), sla_context(scope))
    runs, outcomes = {}, {}
    for view in ("contracts", "accounts"):
        plan = build_sla_plan(scope, period=period, incident_keys=incident_keys, view=view)
        result = await executor.execute(plan, scope)
        if not expected[view] and result.status == Status.BLOCKED_CAPABILITY:
            if result.answer is not None or not any("Empty cohort:" in error for error in result.errors):
                raise AssertionError({"view": view, "errors": result.errors})
            if any(receipt.node_id in {"settlement", "applied", "account_sla", "finance"}
                   and receipt.query is not None for receipt in result.receipts):
                raise AssertionError("Empty cohort executed a downstream financial source query")
            outcomes[view] = "EMPTY_COHORT_BLOCKED_NO_FINANCIAL_QUERY"
        elif result.status != Status.COMPLETE or result.answer is None:
            raise AssertionError({"view": view, "status": result.status, "errors": result.errors})
        else:
            assert_rows(result.answer.rows, expected[view], view)
            outcomes[view] = (
                "RECONCILED_INVALID_SETTLEMENT" if expected["settlement_diagnostics"]
                else "COMPLETE_RECONCILED"
            )
        runs[view] = result.model_dump(mode="json")
    return dict(
        status="INVALID_SETTLEMENT" if expected["settlement_diagnostics"] else "PASS",
        arithmetic_reconciliation="PASS", lane="OFFLINE_SYNTHETIC", kind="SUBMITTED PLAN FIXTURE",
        period=period, incident_keys=incident_keys,
        independent_expected=expected, executions=runs, verified_outcomes=outcomes,
        source_fact_sha256={
            name: hashlib.sha256((data_path / f"{name}.parquet").read_bytes()).hexdigest()
            for name in FACT_TABLES
        },
        limitations=[
            "Local source SQL, not live DAX, native ontology, MCP or agentic planning.",
            "Incident selection limits contracts; financial measures remain whole service-month totals.",
            "Missing applied rows remain null in the display; no Finance credit-to-contract attribution.",
            "Signed Credit Exposure is source-owned; a reconciled negative is invalid settlement, not zero.",
            "Posting validity uses source contract/service-month overlap, not selected-incident membership.",
            "Arithmetic reconciliation covers the selected cohort; posting diagnostics also cover outside postings.",
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "generated" / "miniature")
    parser.add_argument("--period", action="append", help="Default reconciles July and empty August")
    parser.add_argument("--incident-keys", nargs="+", type=int)
    args = parser.parse_args()
    results = [
        asyncio.run(reconcile(args.data, period=period, incident_keys=args.incident_keys))
        for period in (args.period or ["2026-07", "2026-08"])
    ]
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
