import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from telco_iq.api import create_app
from telco_iq.catalog import Catalog
from telco_iq.context import offline_context
from telco_iq.contracts import Plan, Scope, Status
from telco_iq.executor import Executor
from telco_iq.providers import OfflineProvider

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def fixture_data(tmp_path_factory):
    output = tmp_path_factory.mktemp("miniature")
    subprocess.run([
        sys.executable, str(ROOT / "tools" / "build_data.py"), "--profile", "miniature",
        "--seed", "20260929", "--output", str(output),
    ], check=True, cwd=ROOT, capture_output=True, text=True)
    return output


@pytest.fixture(scope="module")
def oracle():
    spec = importlib.util.spec_from_file_location("independent_oracle", ROOT / "eval" / "oracle" / "reference.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scope(catalog):
    return Scope(
        tenant="SYNTHETIC_LOCAL", principal="LOCAL_PROCESS",
        authorization_revision="offline-v1", models=list(catalog.models), mode="OFFLINE_SYNTHETIC",
    )


def example(catalog):
    def query(key, model, table, metric):
        return {
            "id": key, "kind": "QuerySemanticModel", "model": model, "table": table,
            "metrics": [metric], "group_by": ["account_key"],
            "filters": [{"column": "month", "op": "eq", "values": ["2026-08-01"]}],
            "date_role": "month", "expected_grain": ["account_key"], "max_rows": 1000,
        }
    return {
        "version": "1.0", "request_id": "independent-two-model-fixture",
        "question": "August active lines and recognized service revenue by account",
        "principal_scope": scope(catalog).model_dump(), "as_of": "2026-09-29T00:00:00Z",
        "ontology_revision": "offline-v1",
        "metric_selections": ["subscriptions.active_lines_eom", "finance.recognized_service_revenue"],
        "nodes": [
            query("lines", "subscriptions", "subscription_month", "subscriptions.active_lines_eom"),
            query("revenue", "finance", "account_month", "finance.recognized_service_revenue"),
            {"id": "aligned", "kind": "JoinResults", "left": "lines", "right": "revenue",
             "relationship": "account-month-conformed-v1", "left_keys": ["account_key"],
             "right_keys": ["account_key"], "cardinality": "one_to_one", "unmatched": "error"},
            {"id": "answer", "kind": "SynthesizeAnswer", "source": "aligned"},
        ],
        "result_node": "answer",
    }


def runner(data):
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    principal = scope(catalog)
    executor = Executor(catalog, OfflineProvider(catalog, data), offline_context(principal))
    return catalog, principal, executor


async def test_two_model_actual_local_queries_match_independent_source_oracle(fixture_data, oracle):
    catalog, principal, executor = runner(fixture_data)
    result = await executor.execute(Plan.model_validate(example(catalog)), principal)
    assert result.status == Status.COMPLETE, result.errors
    assert result.answer.rows == oracle.two_model(fixture_data)
    assert len(result.receipts) == 2
    assert all(r.lane == "OFFLINE_SYNTHETIC" and r.query for r in result.receipts)
    assert result.answer.evidence_ids == [r.id for r in result.receipts]


async def test_separate_answer_views_preserve_each_source_grain_values_and_evidence(fixture_data):
    catalog, principal, executor = runner(fixture_data)
    plan = example(catalog)
    plan["nodes"] = [*plan["nodes"][:2], {
        "id": "answer", "kind": "SynthesizeAnswer", "source": "lines",
        "additional_sources": {"Account revenue": "revenue"},
    }]
    plan["nodes"][0]["group_by"] = ["account_key", "month"]
    plan["nodes"][0]["expected_grain"] = ["account_key", "month"]
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.COMPLETE, result.errors
    assert result.answer.grain == ["account_key", "month"]
    separate = result.additional_answers["Account revenue"]
    assert separate.grain == ["account_key"] and separate.source_models == ["finance"]
    assert separate.status == Status.COMPLETE
    assert len(separate.rows) == len(result.answer.rows) == 10
    assert all(set(row) == {"account_key", "finance.recognized_service_revenue"} for row in separate.rows)
    assert all("finance.recognized_service_revenue" not in row for row in result.answer.rows)
    assert separate.evidence_ids == [next(receipt.id for receipt in result.receipts if receipt.model == "finance")]
    assert result.answer.evidence_ids == [
        next(receipt.id for receipt in result.receipts if receipt.model == "subscriptions")
    ]
    assert [clock.model for clock in separate.source_clocks] == ["finance"]


async def test_partial_separate_view_cannot_become_a_complete_multi_view_answer(fixture_data, monkeypatch):
    catalog, principal, executor = runner(fixture_data)
    original = executor.provider.query

    async def partial_finance(node, current):
        value, receipt = await original(node, current)
        if node.model == "finance":
            value = value.model_copy(update={"status": Status.PARTIAL, "warnings": ["Synthetic partial source"]})
            receipt = receipt.model_copy(update={"status": Status.PARTIAL})
        return value, receipt

    monkeypatch.setattr(executor.provider, "query", partial_finance)
    plan = example(catalog)
    plan["nodes"] = [*plan["nodes"][:2], {
        "id": "answer", "kind": "SynthesizeAnswer", "source": "lines",
        "additional_sources": {"Account revenue": "revenue"},
    }]
    plan["nodes"][1]["completeness"] = "qualified_partial"
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.PARTIAL and result.answer.status == Status.COMPLETE
    assert result.additional_answers["Account revenue"].status == Status.PARTIAL
    assert result.additional_answers["Account revenue"].warnings == ["Synthetic partial source"]


@pytest.mark.parametrize("change", ["undefined", "context", "cycle", "budget"])
async def test_separate_view_dependencies_and_query_budget_remain_fail_closed(fixture_data, change):
    catalog, principal, executor = runner(fixture_data)
    plan = example(catalog)
    plan["nodes"] = [*plan["nodes"][:2], {
        "id": "answer", "kind": "SynthesizeAnswer", "source": "lines",
        "additional_sources": {"Account revenue": "revenue"},
    }]
    if change in ("undefined", "cycle"):
        plan["nodes"][-1]["additional_sources"]["Account revenue"] = (
            "not-declared" if change == "undefined" else "answer"
        )
    elif change == "context":
        plan["nodes"][1] = {"id": "revenue", "kind": "ReadModelSchema", "references": ["finance"]}
    else:
        plan["budgets"] = {"max_queries": 1}
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.FAILED
    assert result.answer is None and not result.additional_answers and not result.receipts


async def test_snapshot_time_sum_and_mismatched_periods_rejected(fixture_data):
    catalog, principal, executor = runner(fixture_data)
    plan = example(catalog)
    plan["nodes"][0]["filters"] = []
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.FAILED
    assert not result.receipts
    plan = example(catalog)
    plan["nodes"][1]["filters"][0]["values"] = ["2026-07-01"]
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.FAILED
    assert not result.receipts
    assert "periods differ" in result.errors[0]


async def test_denied_source_and_browser_scope_forgery_fail_before_query(fixture_data):
    catalog, principal, executor = runner(fixture_data)
    forged = example(catalog)
    forged["principal_scope"]["principal"] = "ADMIN"
    result = await executor.execute(Plan.model_validate(forged), principal)
    assert result.status == Status.BLOCKED_AUTH and not result.receipts
    denied = principal.model_copy(update={"models": ["subscriptions"]})
    executor.context = offline_context(denied)
    plan = example(catalog)
    plan["principal_scope"] = denied.model_dump()
    result = await executor.execute(Plan.model_validate(plan), denied)
    assert result.status == Status.BLOCKED_AUTH and not result.receipts


async def test_truncation_cannot_produce_joined_total(fixture_data):
    catalog, principal, executor = runner(fixture_data)
    plan = example(catalog)
    plan["nodes"][0]["max_rows"] = 1
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.PARTIAL
    assert result.answer is None
    assert len(result.receipts) == 1


async def test_missing_join_projection_never_becomes_empty_success(fixture_data):
    catalog, principal, executor = runner(fixture_data)
    plan = example(catalog)
    plan["nodes"][0]["group_by"] = []
    plan["nodes"][0]["expected_grain"] = []
    plan["nodes"][2]["unmatched"] = "exclude"
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.FAILED and not result.receipts
    assert "not projected" in result.errors[0]


async def test_ambiguity_no_query_execution(fixture_data):
    catalog, principal, executor = runner(fixture_data)
    plan = example(catalog)
    plan["question"] = "Active customers over the quarter?"
    plan["clarification_state"] = "required"
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.AMBIGUOUS and not result.receipts


async def test_q01_all_period_market_cohorts_match_independent_oracle(fixture_data, oracle):
    catalog, principal, executor = runner(fixture_data)
    query = {
        "id": "churn", "kind": "QuerySemanticModel", "model": "subscriptions", "table": "subscription_month",
        "metrics": ["subscriptions.opening_active_lines", "subscriptions.disconnects", "subscriptions.churn_rate"],
        "group_by": ["month", "market_id"], "date_role": "month", "expected_grain": ["month", "market_id"],
    }
    raw = example(catalog) | {"nodes": [query], "result_node": "churn", "metric_selections": query["metrics"]}
    result = await executor.execute(Plan.model_validate(raw), principal)
    assert result.status == Status.COMPLETE, result.errors
    expected = oracle.q01(fixture_data)
    for row in result.answer.rows:
        answer = expected[f"{row['month'][:7]}:{row['market_id']}"]
        assert row["subscriptions.opening_active_lines"] == answer["opening_active"]
        assert row["subscriptions.disconnects"] == answer["disconnects"]
        if answer["churn_rate"] is None:
            assert row["subscriptions.churn_rate"] is None
        else:
            assert row["subscriptions.churn_rate"] == pytest.approx(answer["churn_rate"])


def test_oracles_q03_q08_q14_have_nontrivial_coverage(fixture_data, oracle):
    sla = oracle.q03(fixture_data)
    assert sla and any(row["gross_capped_cents"] > 0 for row in sla.values())
    assert all(row["remaining_cents"] >= 0 for row in sla.values())
    active = oracle.q08(fixture_data)
    assert active["unqualified_question"] == "AMBIGUOUS"
    assert active["distinct_active_at_month_end_anytime"] >= active["eom_active_accounts"]
    mapping = oracle.q14(fixture_data)
    assert mapping["matched"] > 0
    assert mapping["rows_where_numeric_equality_is_wrong"] > 0


def test_fastapi_routes_do_not_pretend_planner_or_login(fixture_data):
    client = TestClient(create_app(ROOT, fixture_data), base_url="http://localhost:8765")
    initial_capabilities = client.get("/api/capabilities").json()["experiments"]
    health = client.get("/api/health")
    assert health.json()["mode"] == "OFFLINE_SYNTHETIC"
    assert health.json()["live_auth"] == "NOT_CONFIGURED"
    assert client.post("/api/execute", json={}).status_code == 403
    headers = {"X-Telco-Local": "submitted-plan"}
    assert client.post("/api/plan", json={"question": "revenue"}, headers=headers).status_code == 409
    assert client.post("/api/plan", json={}, headers=headers | {"Origin": "https://untrusted.test"}).status_code == 403
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    result = client.post("/api/execute", json=example(catalog), headers=headers)
    assert result.status_code == 200
    assert result.json()["status"] == "COMPLETE", result.text
    assert client.get("/api/capabilities").json()["experiments"] == initial_capabilities


async def test_source_manifest_change_propagates_without_context_refresh(fixture_data):
    catalog, principal, executor = runner(fixture_data)
    first = await executor.execute(Plan.model_validate(example(catalog)), principal)
    path = fixture_data / "manifest.json"
    original = path.read_text()
    source = fixture_data / "account_month.parquet"
    original_bytes = source.read_bytes()
    manifest = json.loads(original)
    try:
        original_table = pq.read_table(source)
        records = original_table.to_pylist()
        changed_account = next(r["account_key"] for r in records if str(r["month"])[:7] == "2026-08")
        for row in records:
            if row["account_key"] == changed_account and str(row["month"])[:7] == "2026-08":
                row["revenue_cents"] += 123
        pq.write_table(pa.Table.from_pylist(records, schema=original_table.schema), source)
        for entry in manifest["tables"]:
            if entry["id"] == "account_month":
                entry["file_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
                entry["bytes"] = source.stat().st_size
                entry["content_sha256"] = hashlib.sha256(json.dumps(records, default=str).encode()).hexdigest()
        manifest["content_sha256"] = hashlib.sha256(json.dumps(
            {t["id"]: t["content_sha256"] for t in manifest["tables"]},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        path.write_text(json.dumps(manifest))
        second = await executor.execute(Plan.model_validate(example(catalog)), principal)
        assert first.receipts[0].clocks.data != second.receipts[0].clocks.data
        assert first.receipts[0].clocks.model == second.receipts[0].clocks.model
        assert first.receipts[0].clocks.context == second.receipts[0].clocks.context
        before = next(r for r in first.answer.rows if r["account_key"] == changed_account)
        after = next(r for r in second.answer.rows if r["account_key"] == changed_account)
        assert after["finance.recognized_service_revenue"] - before["finance.recognized_service_revenue"] == 123
    finally:
        path.write_text(original)
        source.write_bytes(original_bytes)


async def test_incomplete_source_watermark_never_implies_zero(fixture_data):
    catalog, principal, executor = runner(fixture_data)
    plan = example(catalog)
    for node in plan["nodes"][:2]:
        node["filters"][0]["values"] = ["2026-09-01"]
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.PARTIAL and result.answer is None


@pytest.mark.parametrize("same_size", [False, True])
async def test_unannounced_source_change_is_explicit_failure(fixture_data, same_size):
    catalog, principal, executor = runner(fixture_data)
    source = fixture_data / "account_month.parquet"
    original = source.read_bytes()
    try:
        changed = original[:-1] + bytes([original[-1] ^ 1]) if same_size else original + b"changed"
        source.write_bytes(changed)
        result = await executor.execute(Plan.model_validate(example(catalog)), principal)
        assert result.status == Status.FAILED
        assert "manifest refresh" in result.errors[0]
    finally:
        source.write_bytes(original)


async def test_independent_oracle_detects_broken_denominator(fixture_data, oracle, tmp_path):
    catalog, principal, executor = runner(fixture_data)
    raw = json.loads(json.dumps(catalog.raw))
    for metric in raw["metrics"]:
        if metric["id"] == "subscriptions.churn_rate":
            metric["sql"] = "0"
    path = tmp_path / "mutated-catalog.json"
    path.write_text(json.dumps(raw))
    altered = Catalog(path)
    executor.catalog = altered
    executor.provider = OfflineProvider(altered, fixture_data)
    query = {
        "id": "churn", "kind": "QuerySemanticModel", "model": "subscriptions",
        "table": "subscription_month", "metrics": ["subscriptions.churn_rate"],
        "group_by": ["month", "market_id"], "date_role": "month", "expected_grain": ["month", "market_id"],
    }
    plan = example(catalog) | {"nodes": [query], "result_node": "churn", "metric_selections": query["metrics"]}
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.COMPLETE
    gold = oracle.q01(fixture_data)
    mismatches = [
        row for row in result.answer.rows
        if row["subscriptions.churn_rate"] != gold[f"{row['month'][:7]}:{row['market_id']}"]["churn_rate"]
    ]
    assert mismatches, "Oracle must detect deliberately incorrect runtime arithmetic"


async def test_timeout_returns_explicit_status(fixture_data):
    catalog, principal, executor = runner(fixture_data)
    provider = executor.provider

    class SlowProvider:
        mode = "OFFLINE_SYNTHETIC"

        async def query(self, node, scope):
            await asyncio.sleep(1)
            return await provider.query(node, scope)

    executor.provider = SlowProvider()
    plan = example(catalog)
    plan["budgets"] = {"timeout_seconds": 0.001}
    result = await executor.execute(Plan.model_validate(plan), principal)
    assert result.status == Status.TIMED_OUT
