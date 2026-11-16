"""Execute submitted fixtures; independent expected cents live only in evaluator code."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eval.oracle.reference import q03
from eval.runners.reconcile_sla_fixture import assert_rows, reconcile, source_expectations
from telco_iq.catalog import Catalog, digest
from telco_iq.contracts import JoinNode, Plan, QueryNode, Scope, Status
from telco_iq.errors import LabError
from telco_iq.executor import Executor, validate
from telco_iq.providers import OfflineProvider
from telco_iq.sla_fixture import (
    CONTRACT_METRICS,
    REVIEWED_PERIODS,
    build_sla_plan,
    sla_context,
)
from tools import build_data

ROOT = Path(__file__).resolve().parents[1]
SCOPE = Scope(
    tenant="synthetic", principal="local-sla-test", authorization_revision="1",
    models=["network", "enterprise", "finance"], mode="OFFLINE_SYNTHETIC",
)


@pytest.fixture(scope="module")
def miniature(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sla-source")
    build_data.build("miniature", build_data.SEED, folder)
    return folder


@pytest.fixture
def catalog():
    return Catalog(ROOT / "data" / "contracts" / "catalog.json")


async def execute(folder: Path, catalog: Catalog, **kwargs):
    plan = build_sla_plan(SCOPE, **kwargs)
    result = await Executor(catalog, OfflineProvider(catalog, folder), sla_context(SCOPE)).execute(plan, SCOPE)
    return result


async def intervals(folder: Path, catalog: Catalog, **kwargs):
    plan = build_sla_plan(SCOPE, **kwargs)
    end = next(index for index, node in enumerate(plan.nodes) if node.id == "affected_contract_intervals")
    plan = plan.model_copy(update={
        "nodes": plan.nodes[:end + 1], "result_node": "affected_contract_intervals", "metric_selections": [],
    })
    return await Executor(catalog, OfflineProvider(catalog, folder), sla_context(SCOPE)).execute(plan, SCOPE)


def changed_sources(tmp_path, monkeypatch, *, incident=None, mappings=None, contracts=None, rows=None):
    for method, change in (("incidents", incident), ("mappings", mappings), ("contracts", contracts)):
        if change is not None:
            original = getattr(build_data.Sources, method)

            def replacement(self, original=original, change=change):
                return change(list(original(self)))

            monkeypatch.setattr(build_data.Sources, method, replacement)
    if rows is not None:
        original_rows = build_data.Sources.rows

        def replacement_rows(self, identifier):
            return rows(identifier, list(original_rows(self, identifier)))

        monkeypatch.setattr(build_data.Sources, "rows", replacement_rows)
    folder = tmp_path / "changed-source-facts"
    build_data.build("miniature", build_data.SEED, folder)
    return folder


def test_context_is_offline_identity_review_not_native():
    context = sla_context(SCOPE)
    assert context.mode == "OFFLINE_SYNTHETIC"
    assert "SUBMITTED PLAN FIXTURE" in context.source
    assert any("NOT native" in entry for entry in context.provenance)
    relationships = {relationship.id: relationship for relationship in context.relationships}
    financial = relationships["sla-contract-finance-account-v1"]
    assert (financial.left_table, financial.left_keys) == ("contract", ["account_key"])
    assert (financial.right_table, financial.right_keys) == ("account_month", ["account_key"])
    assert all(relation.right_table != "credit" for relation in context.relationships)
    assert relationships["sla-incident-mapping-v1"].cardinality == "many_to_many"
    assert relationships["sla-contract-settlement-v1"].right_table == "sla_evaluation"


@pytest.mark.parametrize("view", ["contracts", "accounts"])
def test_plan_is_explicit_bounded_and_uses_same_month(catalog, view):
    plan = build_sla_plan(SCOPE, view=view)
    validate(plan, SCOPE, catalog, sla_context(SCOPE))
    assert "SUBMITTED PLAN FIXTURE | OFFLINE_SYNTHETIC | 2026-07" in plan.question
    assert Plan.model_validate_json(plan.model_dump_json()) == plan
    queries = [node for node in plan.nodes if isinstance(node, QueryNode)]
    assert len(queries) <= plan.budgets.max_queries
    for node in queries:
        if node.metrics:
            assert node.date_role == "month"
            assert [(f.column, f.op, f.values) for f in node.filters] == [
                ("month", "eq", ["2026-07-01"]),
            ]
        if node.table in {"incident", "service_site_map", "contract"}:
            assert all(f.column != "month" for f in node.filters)
            assert any(f.op == "lt" for f in node.filters) and any(f.op == "gt" for f in node.filters)
    assert all(node.table != "credit" for node in queries)
    assert all(node.kind not in {"ApplyDeterministicRule", "AggregateResult"} for node in plan.nodes)
    joins = [node for node in plan.nodes if isinstance(node, JoinNode) and node.temporal]
    assert all(node.overlap_start and node.overlap_end for node in joins)
    assert next(node for node in queries if node.table == "site").date_role is None


@pytest.mark.parametrize("period", ["2026-02", "2026-09", "2027-07", "2026-7", "July", ""])
def test_unreviewed_period_is_blocked_not_empty(period):
    with pytest.raises(LabError, match="coverage") as error:
        build_sla_plan(SCOPE, period=period)
    assert error.value.status == Status.BLOCKED_CAPABILITY
    assert REVIEWED_PERIODS == {f"2026-{month:02d}" for month in range(3, 9)}


@pytest.mark.parametrize("keys", [[], [True], [-1], [0], ["1"], list(range(1, 252))])
def test_invalid_incident_selection_requires_clarification(keys):
    with pytest.raises(LabError, match="incident_keys"):
        build_sla_plan(SCOPE, incident_keys=keys)


def test_scope_and_view_are_not_silent_fallbacks():
    with pytest.raises(LabError, match="OFFLINE_SYNTHETIC"):
        sla_context(SCOPE.model_copy(update={"mode": "LIVE_DELEGATED"}))
    with pytest.raises(LabError, match="Network and Enterprise"):
        build_sla_plan(SCOPE.model_copy(update={"models": ["finance"]}))
    limited = SCOPE.model_copy(update={"models": ["network", "enterprise"]})
    assert build_sla_plan(limited).principal_scope == limited
    assert all(relation.right_model != "finance" for relation in sla_context(limited).relationships)
    with pytest.raises(LabError, match="Finance"):
        build_sla_plan(limited, view="accounts")
    with pytest.raises(LabError, match="view"):
        build_sla_plan(SCOPE, view="incidents")


def test_catalogue_credit_grouping_preserves_expressions(catalog):
    expected = {
        "enterprise.eligible_credits": ("SUM('SLA Evaluation'[eligible_credit_cents])", "SUM(eligible_credit_cents)"),
        "enterprise.applied_credits": ("SUM('Applied Credits'[amount_cents])", "SUM(amount_cents)"),
        "enterprise.credit_exposure": (
            "SUM('SLA Evaluation'[eligible_credit_cents]) - SUM('SLA Evaluation'[applied_credit_cents])",
            "SUM(eligible_credit_cents) - SUM(applied_credit_cents)",
        ),
    }
    for metric in CONTRACT_METRICS:
        source = catalog.metrics[metric]
        assert "contract_key" in source["allowedDimensions"]
        assert (source["dax"], source["sql"]) == expected[metric]
        assert source["unit"] == "cents"


@pytest.mark.parametrize("table", ["contract", "service_site_map"])
def test_validity_dimensions_reject_start_month_filter(catalog, table):
    node = QueryNode(
        id="map", kind="QuerySemanticModel", model="enterprise", table=table,
        columns=["contract_key", "valid_from", "valid_to"], expected_grain=[],
        date_role="valid_from",
        filters=[{"column": "month", "op": "eq", "values": ["2026-07-01"]}],
    )
    with pytest.raises(LabError, match="start.month"):
        catalog.validate_query(node)
    # A separately requested start-month analysis is not an effective-period cohort.
    node.date_role = "month"
    catalog.validate_query(node)


async def test_sla_result_retains_actual_fixture_population(miniature, catalog):
    result = await execute(miniature, catalog, view="accounts")
    assert result.status == Status.COMPLETE, result.errors
    manifest = json.loads((miniature / "manifest.json").read_text())
    populations = result.answer.source_populations
    assert {entry.model for entry in populations} == set(SCOPE.models)
    assert {entry.profile for entry in populations} == {manifest["profile"]}
    assert {entry.seed for entry in populations} == {manifest["seed"]}
    assert {entry.generator_revision for entry in populations} == {manifest["version"]}
    assert {entry.manifest_hash for entry in populations} == {digest(manifest)}
    tables = {entry["id"]: entry["content_sha256"] for entry in manifest["tables"]}
    assert all(entry.table_hashes == {
        table: tables[table] for table in catalog.models[entry.model]["tables"]
    } for entry in populations)
    assert all(entry.shared_map_hashes["account"] for entry in populations)
    assert all(entry.table_hashes and entry.manifest_hash for entry in populations)


@pytest.mark.parametrize("field,value", [("profile", None), ("seed", "unknown"), ("tables", [None])])
def test_malformed_population_metadata_blocks_instead_of_crashing(miniature, catalog, monkeypatch, field, value):
    manifest_path = miniature / "manifest.json"
    original = Path.read_text
    manifest = json.loads(original(manifest_path))
    manifest[field] = value

    def read(path, *args, **kwargs):
        return json.dumps(manifest) if path == manifest_path else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(LabError, match="population") as error:
        catalog.fixture_population(miniature, "enterprise")
    assert error.value.status == Status.BLOCKED_CAPABILITY


def test_contract_cohort_filters_exact_measure_home_not_an_inferred_relationship(catalog):
    node = QueryNode(
        id="source", kind="QuerySemanticModel", model="enterprise", table="sla_evaluation",
        metrics=["enterprise.credit_exposure"], group_by=["contract_key", "month"],
        expected_grain=["contract_key", "month"], date_role="month",
        filters=[
            {"column": "contract_key", "op": "in", "values": [1, 2]},
            {"column": "month", "op": "eq", "values": ["2026-07-01"]},
        ],
    )
    dax = catalog.dax(node)
    assert "TREATAS({1, 2}, 'SLA Evaluation'[contract_key])" in dax
    assert "'Contracts'" not in dax and "'Calendar'" not in dax
    assert "[Credit Exposure]" in dax and "SUM(" not in dax
    node.group_by = node.expected_grain = ["site_key"]
    with pytest.raises(LabError, match="Unrecognized column"):
        catalog.dax(node)


async def test_serving_market_is_not_a_finance_home_market_filter(tmp_path, monkeypatch, catalog):
    import pyarrow.parquet as pq

    def rows(identifier, values):
        if identifier == "account_month":
            return [row | {"market_id": "M04"} if row["account_key"] == 1 else row for row in values]
        return values

    folder = changed_sources(tmp_path, monkeypatch, rows=rows)
    result = await execute(folder, catalog, view="accounts", incident_keys=[1])
    assert result.status == Status.COMPLETE, result.errors
    incident = next(row for row in pq.read_table(folder / "incident.parquet").to_pylist() if row["incident_key"] == 1)
    source = next(
        row for row in pq.read_table(folder / "account_month.parquet").to_pylist()
        if row["account_key"] == 1 and row["month"].isoformat() == "2026-07-01"
    )
    assert incident["market_id"] != source["market_id"]
    assert result.answer.rows[0]["finance.recognized_service_revenue"] == source["revenue_cents"]
    finance = next(receipt for receipt in result.receipts if receipt.node_id == "finance")
    assert "market_id" not in finance.query


async def test_july_actual_plans_reconcile_independently(miniature):
    report = await reconcile(miniature)
    assert report["status"] == "PASS"
    expected = report["independent_expected"]
    assert expected["contract_keys"] and expected["account_keys"]
    assert sum(row["enterprise.credit_exposure"] for row in expected["contracts"]) > 0
    assert "sla_evaluation" not in report["source_fact_sha256"]
    for execution in report["executions"].values():
        assert execution["mode"] == "OFFLINE_SYNTHETIC"
        assert execution["receipts"]


async def test_august_empty_is_not_an_unfiltered_financial_total(miniature):
    report = await reconcile(miniature, period="2026-08")
    assert report["independent_expected"]["contract_keys"] == []
    assert set(report["verified_outcomes"].values()) == {"EMPTY_COHORT_BLOCKED_NO_FINANCIAL_QUERY"}
    for execution in report["executions"].values():
        assert execution["status"] == "BLOCKED_CAPABILITY"
        assert execution["answer"] is None
        financial_receipts = [
            receipt for receipt in execution["receipts"]
            if receipt["node_id"] in {"settlement", "applied", "account_sla", "finance"}
        ]
        assert all(receipt["query"] is None for receipt in financial_receipts)


@pytest.mark.parametrize("selection", [[1], [2], [3], [4], [1, 1, 2], [999]])
async def test_selected_incidents_select_contracts_not_marginal_credits(miniature, selection):
    report = await reconcile(miniature, incident_keys=selection)
    whole_month = source_expectations(miniature)["contracts"]
    selected = report["independent_expected"]["contracts"]
    assert all(row in whole_month for row in selected)
    if selection == [999]:
        assert selected == []


async def test_intersections_retain_movement_and_contract_effective_time(miniature, catalog):
    result = await intervals(miniature, catalog)
    assert result.status == Status.COMPLETE, result.errors
    expected = source_expectations(miniature)["membership"]
    observed = [{
        "incident_key": row["incident_key"], "service_key": row["service_key"],
        "contract_key": row["contract_key"],
        "start": row["contract_affected_start"], "end": row["contract_affected_end"],
    } for row in result.answer.rows]
    def key(row):
        return row["incident_key"], row["service_key"], row["contract_key"]

    assert sorted(observed, key=key) == sorted(expected, key=key)
    moved = next(row for row in observed if row["incident_key"] == 1 and row["contract_key"] == 1)
    assert datetime.fromisoformat(moved["end"]) == datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    activated = next(row for row in observed if row["contract_key"] == 2)
    assert datetime.fromisoformat(activated["start"]) == datetime(2026, 7, 15, 6, tzinfo=timezone.utc)


@pytest.mark.parametrize("site,before", [(1, False), (3, True)])
async def test_half_open_movement_boundaries(tmp_path, monkeypatch, site, before):
    split = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)

    def incident(rows):
        row = rows[0] | {"site_key": site, "start": split, "end": split + timedelta(hours=1)}
        if before:
            row.update(start=split - timedelta(hours=1), end=split)
        return [row]

    folder = changed_sources(tmp_path, monkeypatch, incident=incident)
    report = await reconcile(folder, incident_keys=[1])
    assert report["independent_expected"]["contracts"] == []


async def test_cross_month_incident_and_old_mapping_not_dropped(tmp_path, monkeypatch):
    def incident(rows):
        return [rows[0] | {
            "start": datetime(2026, 6, 30, 23, tzinfo=timezone.utc),
            "end": datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
            "month": datetime(2026, 6, 1).date(),
        }]

    folder = changed_sources(tmp_path, monkeypatch, incident=incident)
    report = await reconcile(folder, incident_keys=[1])
    assert report["independent_expected"]["contract_keys"] == [1]
    assert report["independent_expected"]["contracts"][0]["enterprise.eligible_credits"] == 90


async def test_pairwise_overlap_cannot_leak_outside_contract_validity(tmp_path, monkeypatch):
    def contracts(rows):
        return [row | {"valid_from": datetime(2026, 7, 20, tzinfo=timezone.utc)} for row in rows]

    folder = changed_sources(tmp_path, monkeypatch, contracts=contracts)
    report = await reconcile(folder)
    assert report["independent_expected"]["contract_keys"] == []


async def test_overlapping_incidents_do_not_repeat_monthly_cap(miniature, catalog):
    first = await execute(miniature, catalog, incident_keys=[1])
    overlapping = await execute(miniature, catalog, incident_keys=[1, 2, 4])
    assert first.status == overlapping.status == Status.COMPLETE
    assert first.answer.rows == overlapping.answer.rows
    assert len(first.answer.rows) == 1
    assert first.answer.rows[0]["enterprise.eligible_credits"] == 5000


async def test_repeated_account_finance_is_not_duplicated(tmp_path, monkeypatch):
    folder = changed_sources(
        tmp_path, monkeypatch, contracts=lambda rows: [row | {"account_key": 1} for row in rows],
    )
    report = await reconcile(folder)
    expected = report["independent_expected"]
    assert len(expected["contracts"]) == 2
    assert len(expected["accounts"]) == 1
    actual = report["executions"]["accounts"]["answer"]["rows"]
    assert_rows(actual, expected["accounts"], "accounts")
    assert actual[0]["enterprise.credit_exposure"] == sum(
        row["enterprise.credit_exposure"] for row in expected["contracts"]
    )
    assert all(
        not any(key.startswith("finance.") for key in row)
        for row in report["executions"]["contracts"]["answer"]["rows"]
    )


async def test_two_incidents_match_two_services_without_losing_intervals(tmp_path, monkeypatch, catalog):
    folder = changed_sources(
        tmp_path, monkeypatch,
        mappings=lambda rows: [row | {"site_key": 1} if row["service_key"] == 2 else row for row in rows],
    )
    result = await intervals(folder, catalog, incident_keys=[1, 2])
    assert result.status == Status.COMPLETE, result.errors
    assert {(row["incident_key"], row["service_key"]) for row in result.answer.rows} == {
        (1, 1), (1, 2), (2, 1), (2, 2),
    }
    report = await reconcile(folder, incident_keys=[1, 2])
    assert report["independent_expected"]["contract_keys"] == [1, 2]


async def test_cap_is_once_per_contract_after_per_service_threshold(tmp_path, monkeypatch):
    folder = changed_sources(
        tmp_path, monkeypatch,
        mappings=lambda rows: [row | {"contract_key": 1} for row in rows],
        contracts=lambda rows: [rows[0]],
    )
    report = await reconcile(folder)
    rows = report["executions"]["contracts"]["answer"]["rows"]
    assert len(rows) == 1
    assert rows[0]["enterprise.eligible_credits"] == 5000
    assert rows[0]["enterprise.credit_exposure"] == 2500


async def test_independent_evaluator_detects_corrupt_settlement(tmp_path, monkeypatch):
    def rows(name, values):
        if name == "sla_evaluation":
            return [row | {"eligible_credit_cents": row["eligible_credit_cents"] + 1} for row in values]
        return values

    folder = changed_sources(tmp_path, monkeypatch, rows=rows)
    with pytest.raises(AssertionError, match="independent_expected"):
        await reconcile(folder)


async def test_missing_site_is_error_not_empty_cohort(tmp_path, monkeypatch, catalog):
    def rows(name, values):
        return [row for row in values if row["site_key"] != 1] if name == "site" else values

    folder = changed_sources(tmp_path, monkeypatch, rows=rows)
    result = await execute(folder, catalog, incident_keys=[1])
    assert result.status == Status.FAILED
    assert result.answer is None
    assert any("mapping" in error.lower() for error in result.errors)


async def test_missing_contract_link_is_excluded_with_coverage(tmp_path, monkeypatch, catalog):
    folder = changed_sources(
        tmp_path, monkeypatch, contracts=lambda rows: [row for row in rows if row["contract_key"] != 1],
    )
    result = await intervals(folder, catalog, incident_keys=[1])
    assert result.status == Status.COMPLETE, result.errors
    assert result.answer.rows == []
    assert result.answer.coverage["unmapped"] > 0
    result = await execute(folder, catalog, incident_keys=[1])
    assert result.status == Status.PARTIAL
    assert result.answer is None
    assert source_expectations(folder, incident_keys=[1])["missing_contract_links"] == [1]


@pytest.mark.parametrize("table,view", [
    ("sla_evaluation", "contracts"), ("sla_evaluation", "accounts"), ("account_month", "accounts"),
])
async def test_missing_monthly_source_fact_blocks_instead_of_zero(tmp_path, monkeypatch, catalog, table, view):
    def rows(name, values):
        if name == table:
            return [row for row in values if str(row["month"]) != "2026-07-01"]
        return values

    folder = changed_sources(tmp_path, monkeypatch, rows=rows)
    result = await execute(folder, catalog, view=view)
    assert result.status == Status.FAILED
    assert result.answer is None


async def test_below_threshold_preserves_absent_applied_row(tmp_path, monkeypatch):
    def incident(rows):
        return [rows[0] | {"end": rows[0]["start"] + timedelta(minutes=10)}]

    folder = changed_sources(tmp_path, monkeypatch, incident=incident)
    report = await reconcile(folder)
    row = report["executions"]["contracts"]["answer"]["rows"][0]
    assert row["enterprise.eligible_credits"] == row["enterprise.credit_exposure"] == 0
    assert row["enterprise.applied_credits"] is None


@pytest.mark.parametrize("applied", [None, 0, 2500, 5000, 5001, -1], ids=[
    "absent", "zero", "partial", "full", "overapplied", "negative-applied",
])
async def test_signed_settlement_policy_matches_both_independent_oracles(tmp_path, monkeypatch, applied):
    def rows(name, values):
        if name == "sla_evaluation":
            return [
                row | {"applied_credit_cents": 0 if applied is None else applied}
                if row["contract_key"] == 1 and str(row["month"]) == "2026-07-01" else row
                for row in values
            ]
        if name == "applied_credit":
            return [
                row | {"amount_cents": applied}
                if row["contract_key"] == 1 and str(row["month"]) == "2026-07-01" else row
                for row in values
                if not (applied is None and row["contract_key"] == 1
                        and str(row["month"]) == "2026-07-01")
            ]
        return values

    folder = changed_sources(tmp_path, monkeypatch, rows=rows)
    reference = q03(folder)["2026-07:1"]
    expected = source_expectations(folder)
    contract = next(row for row in expected["contracts"] if row["contract_key"] == 1)
    residual = 5000 - (0 if applied is None else applied)
    assert reference["gross_capped_cents"] == contract["enterprise.eligible_credits"] == 5000
    assert reference["applied_cents"] == contract["enterprise.applied_credits"] == applied
    assert reference["remaining_cents"] == contract["enterprise.credit_exposure"] == residual
    reason = (
        "NEGATIVE_APPLIED_CREDITS" if applied is not None and applied < 0
        else "OVERAPPLIED_CREDITS" if applied is not None and applied > 5000 else None
    )
    diagnostics = [] if reason is None else [{
        "classification": "INVALID_SETTLEMENT", "reason": reason,
        "contract_key": 1, "month": "2026-07-01", "eligible_cents": 5000,
        "applied_cents": applied, "credit_exposure_cents": residual,
    }]
    assert reference.get("settlement_diagnostics", []) == expected["settlement_diagnostics"] == diagnostics
    report = await reconcile(folder)
    assert report["arithmetic_reconciliation"] == "PASS"
    assert report["status"] == ("INVALID_SETTLEMENT" if diagnostics else "PASS")
    assert set(report["verified_outcomes"].values()) == {
        "RECONCILED_INVALID_SETTLEMENT" if diagnostics else "COMPLETE_RECONCILED",
    }
    observed = next(
        row for row in report["executions"]["contracts"]["answer"]["rows"] if row["contract_key"] == 1
    )
    assert observed["enterprise.credit_exposure"] == residual
    assert observed["enterprise.applied_credits"] == applied


@pytest.mark.parametrize("amount", [2500, None], ids=["known", "unknown"])
@pytest.mark.parametrize("membership", ["orphan", "expired", "future"])
async def test_posting_source_membership_is_validated_outside_incident_cohort(
    tmp_path, monkeypatch, miniature, amount, membership,
):
    def contracts(values):
        if membership == "orphan":
            return values
        validity = (
            {"valid_to": datetime(2026, 7, 1, tzinfo=timezone.utc)}
            if membership == "expired"
            else {"valid_from": datetime(2026, 8, 1, tzinfo=timezone.utc)}
        )
        return values + [values[0] | {"contract_key": 999} | validity]

    def rows(name, values):
        if name == "applied_credit":
            return values + [values[0] | {"contract_key": 999, "amount_cents": amount}]
        return values

    folder = changed_sources(tmp_path, monkeypatch, contracts=contracts, rows=rows)
    baseline = source_expectations(miniature)
    expected = source_expectations(folder)
    reference = q03(folder)
    assert {key: value for key, value in reference.items() if key != "settlement_diagnostics"} == q03(miniature)
    for field in ("contracts", "accounts", "contract_keys", "account_keys", "membership"):
        assert expected[field] == baseline[field]
    reason = "ORPHAN_APPLIED_CREDIT" if membership == "orphan" else "APPLIED_CREDIT_OUTSIDE_CONTRACT_PERIOD"
    reasons = [("INVALID_SETTLEMENT", reason)]
    if amount is None:
        reasons.append(("INCOMPLETE_SETTLEMENT", "MISSING_APPLIED_AMOUNT"))
    diagnostics = [{
        "classification": classification, "reason": reason,
        "contract_key": 999, "month": "2026-07-01",
        "eligible_cents": None, "applied_cents": amount, "credit_exposure_cents": None,
    } for classification, reason in reasons]
    assert expected["settlement_diagnostics"] == reference.get("settlement_diagnostics", []) == diagnostics
    if amount is None:
        with pytest.raises(AssertionError, match="INCOMPLETE_SETTLEMENT"):
            await reconcile(folder)
    else:
        report = await reconcile(folder)
        assert report["status"] == "INVALID_SETTLEMENT"
        assert report["arithmetic_reconciliation"] == "PASS"
        assert set(report["verified_outcomes"].values()) == {"RECONCILED_INVALID_SETTLEMENT"}


async def test_valid_outside_incident_posting_uses_service_month_overlap_not_posting_time(
    tmp_path, monkeypatch, miniature,
):
    def contracts(values):
        return values + [values[0] | {
            "contract_key": 999,
            "valid_from": datetime(2026, 7, 15, tzinfo=timezone.utc),
            "valid_to": datetime(2026, 8, 1, tzinfo=timezone.utc),
        }]

    def rows(name, values):
        if name == "applied_credit":
            return values + [values[0] | {"contract_key": 999, "amount_cents": 0}]
        return values

    folder = changed_sources(tmp_path, monkeypatch, contracts=contracts, rows=rows)
    assert q03(folder) == q03(miniature)
    assert q03(folder)["2026-07:2"]["applied_cents"] == 5895
    assert source_expectations(folder) == source_expectations(miniature)
    for selection in (None, [1], [999]):
        report = await reconcile(folder, incident_keys=selection)
        assert report["status"] == "PASS"
        assert report["independent_expected"] == source_expectations(miniature, incident_keys=selection)


@pytest.mark.parametrize("amount,classification,reason", [
    (-1, "INVALID_SETTLEMENT", "NEGATIVE_APPLIED_CREDITS"),
    (None, "INCOMPLETE_SETTLEMENT", "MISSING_APPLIED_AMOUNT"),
])
async def test_outside_incident_posting_amount_defect_is_not_hidden(
    tmp_path, monkeypatch, miniature, amount, classification, reason,
):
    def rows(name, values):
        if name == "applied_credit":
            return values + [values[0] | {"contract_key": 999, "amount_cents": amount}]
        return values

    folder = changed_sources(
        tmp_path, monkeypatch, rows=rows,
        contracts=lambda values: values + [values[0] | {"contract_key": 999}],
    )
    expected = source_expectations(folder)
    assert expected["contracts"] == source_expectations(miniature)["contracts"]
    diagnostics = [{
        "classification": classification, "reason": reason, "contract_key": 999, "month": "2026-07-01",
        "eligible_cents": None, "applied_cents": amount, "credit_exposure_cents": None,
    }]
    assert expected["settlement_diagnostics"] == q03(folder)["settlement_diagnostics"] == diagnostics
    if amount is None:
        with pytest.raises(AssertionError, match="INCOMPLETE_SETTLEMENT"):
            await reconcile(folder)
    else:
        report = await reconcile(folder)
        assert report["status"] == "INVALID_SETTLEMENT"
        assert report["arithmetic_reconciliation"] == "PASS"


@pytest.mark.parametrize("unknown_contracts", [{1}, {1, 2}], ids=["partial-account", "unknown-account"])
async def test_missing_applied_amounts_are_incomplete_not_zero(tmp_path, monkeypatch, unknown_contracts):
    def rows(name, values):
        column = {"sla_evaluation": "applied_credit_cents", "applied_credit": "amount_cents"}.get(name)
        if column:
            return [
                row | {column: None}
                if row["contract_key"] in unknown_contracts and str(row["month"]) == "2026-07-01" else row
                for row in values
            ]
        return values

    folder = changed_sources(
        tmp_path, monkeypatch, rows=rows,
        contracts=lambda values: [row | {"account_key": 1} for row in values],
    )
    reference = q03(folder)
    expected = source_expectations(folder)
    assert len(expected["accounts"]) == 1
    assert expected["accounts"][0]["enterprise.credit_exposure"] is None
    diagnostics = []
    for contract in expected["contracts"]:
        key = contract["contract_key"]
        if key not in unknown_contracts:
            continue
        independent = reference[f"2026-07:{key}"]
        assert independent["applied_cents"] is contract["enterprise.applied_credits"] is None
        assert independent["remaining_cents"] is contract["enterprise.credit_exposure"] is None
        assert independent["settlement_diagnostics"] == [{
            "classification": "INCOMPLETE_SETTLEMENT", "reason": "MISSING_APPLIED_AMOUNT",
            "contract_key": key, "month": "2026-07-01",
            "eligible_cents": contract["enterprise.eligible_credits"],
            "applied_cents": None, "credit_exposure_cents": None,
        }]
        diagnostics.extend(independent["settlement_diagnostics"])
    assert expected["settlement_diagnostics"] == diagnostics
    with pytest.raises(AssertionError, match="INCOMPLETE_SETTLEMENT"):
        await reconcile(folder)


async def test_overapplication_diagnostic_survives_net_zero_account_total(tmp_path, monkeypatch):
    def rows(name, values):
        column = {"sla_evaluation": "applied_credit_cents", "applied_credit": "amount_cents"}.get(name)
        if column:
            return [
                row | {column: 10895}
                if row["contract_key"] == 1 and str(row["month"]) == "2026-07-01" else row
                for row in values
            ]
        return values

    folder = changed_sources(
        tmp_path, monkeypatch, rows=rows,
        contracts=lambda values: [row | {"account_key": 1} for row in values],
    )
    report = await reconcile(folder)
    assert report["status"] == "INVALID_SETTLEMENT"
    assert report["arithmetic_reconciliation"] == "PASS"
    expected = report["independent_expected"]
    assert expected["accounts"][0]["enterprise.credit_exposure"] == 0
    assert expected["settlement_diagnostics"] == q03(folder)["2026-07:1"]["settlement_diagnostics"]
    assert expected["settlement_diagnostics"][0]["credit_exposure_cents"] == -5895


def test_archived_july_settlement_values_remain_unchanged(miniature):
    reference = q03(miniature)
    expected = source_expectations(miniature)
    assert set(reference) == {"2026-07:1", "2026-07:2"}
    for key, eligible, applied, residual in [(1, 5000, 2500, 2500), (2, 11790, 5895, 5895)]:
        independent = reference[f"2026-07:{key}"]
        assert independent["gross_capped_cents"] == eligible
        assert independent["applied_cents"] == applied
        assert independent["remaining_cents"] == residual
        assert "settlement_diagnostics" not in independent
    assert sum(row["enterprise.eligible_credits"] for row in expected["contracts"]) == 16790
    assert sum(row["enterprise.applied_credits"] for row in expected["contracts"]) == 8395
    assert sum(row["enterprise.credit_exposure"] for row in expected["contracts"]) == 8395
    assert expected["settlement_diagnostics"] == []


def test_settlement_oracles_do_not_import_each_other():
    for parts in (("eval", "oracle", "reference.py"), ("eval", "runners", "reconcile_sla_fixture.py")):
        tree = ast.parse(ROOT.joinpath(*parts).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("eval")
            elif isinstance(node, ast.Import):
                assert all(not alias.name.startswith("eval") for alias in node.names)


def test_ai_guidance_is_equivalent_unapplied_and_uses_real_names(catalog):
    packet = json.loads((ROOT / "fabric" / "ai-prep" / "sla-model-guidance.json").read_text())
    assert packet["status"] == "DRAFT_NOT_APPLIED"
    assert set(packet["comparisonArms"]) == {"flat", "native"}
    assert all("sharedCrossDomainFacts" in value for value in packet["comparisonArms"].values())
    assert {model["id"] for model in packet["models"]} == {"network", "enterprise", "finance"}
    names = {metric["name"] for metric in catalog.metrics.values()}
    for model in packet["models"]:
        assert model["verifiedAnswers"] == []
        assert sum(map(len, model["instructions"])) < 10000
        for description in model["recommendedDescriptions"]:
            if description["object"].startswith("["):
                assert description["object"][1:-1] in names
    assert packet["evidenceGaps"]


def test_fixture_has_no_source_values_or_evaluator_dependency():
    source = (ROOT / "src" / "telco_iq" / "sla_fixture.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("eval", "tools", "data.generator"))
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"read_text", "read_bytes", "read_table", "read_parquet"}
    assert "gold" not in source.lower()
    assert "5000" not in source and "11790" not in source
