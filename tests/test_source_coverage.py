"""Actual fixture queries, not evaluator arithmetic or live Fabric proof."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from telco_iq.api import create_app
from telco_iq.catalog import Catalog
from telco_iq.context import offline_context
from telco_iq.contracts import Plan, QueryNode, Scope, Status
from telco_iq.executor import Executor
from telco_iq.providers import OfflineProvider, file_hash
from tools import build_data, build_models

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("source-coverage")
    folder = root / "data"
    build_data.build("showcase-v2", build_data.SEED, folder)
    build_models.build(root / "models", root / "contracts", folder / "manifest.json")
    return root, folder


@pytest.fixture(params=["ordinary-app-catalog", "generated-catalog"])
def catalog(sources, request):
    path = ROOT / "data" if request.param == "ordinary-app-catalog" else sources[0]
    return Catalog(path / "contracts" / "catalog.json")


def query(table, *, metrics=(), columns=(), filters=(), group_by=()):
    return QueryNode(
        id="source", kind="QuerySemanticModel", model="enterprise", table=table,
        metrics=list(metrics), columns=list(columns), group_by=list(group_by),
        expected_grain=list(group_by or columns), date_role="month",
        filters=[{"column": "month", "op": "eq", "values": ["2026-07-01"]}, *filters],
    )


def selected(column, *values):
    return {"column": column, "op": "in", "values": list(values)}


def coverage_query(receipt):
    return json.loads(receipt.completeness_evidence.split("; selected raw-source coverage: ", 1)[1])["query"]


async def execute(catalog, folder, node, *, additional=()):
    scope = Scope(
        tenant="synthetic", principal="tq004", authorization_revision="1",
        mode="OFFLINE_SYNTHETIC", models=list(catalog.models),
    )
    context = offline_context(scope)
    plan = Plan(
        request_id="tq004-source-coverage", question="Selected source coverage",
        principal_scope=scope, as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
        ontology_revision=context.revision, metric_selections=node.metrics,
        nodes=[*additional, node], result_node=node.id,
    )
    return await Executor(catalog, OfflineProvider(catalog, folder), context).execute(plan, scope)


@pytest.mark.parametrize("table,metrics", [
    ("service_month", ["enterprise.sla_compliance", "enterprise.breached_services"]),
    ("sla_evaluation", ["enterprise.eligible_credits", "enterprise.credit_exposure"]),
])
async def test_account_mixed_unknown_never_complete(sources, catalog, table, metrics):
    node = query(
        table, metrics=metrics, group_by=["account_key", "month"], filters=[selected("account_key", 6)],
    )
    refused = await execute(catalog, sources[1], node)
    assert refused.status == Status.PARTIAL and refused.answer is None
    result = await execute(catalog, sources[1], node.model_copy(update={"completeness": "qualified_partial"}))
    assert result.status == Status.PARTIAL
    assert result.receipts[0].status == Status.PARTIAL
    assert result.answer.coverage["unknown_source_rows"] == 1
    assert any("unknown" in warning.lower() for warning in result.answer.warnings)


async def test_unknown_service_is_distinct_from_unaffected_service(sources, catalog):
    columns = ["service_key", "month", "monitored_minutes", "breached"]
    unknown = await execute(catalog, sources[1], query(
        "service_month", columns=columns, filters=[selected("service_key", 10)],
    ).model_copy(update={"completeness": "qualified_partial"}))
    assert unknown.status == Status.PARTIAL
    assert unknown.answer.rows[0]["breached"] is None
    assert unknown.answer.rows[0]["monitored_minutes"] == 43200
    known = await execute(catalog, sources[1], query(
        "service_month", columns=columns, filters=[selected("service_key", 11)],
    ))
    assert known.status == Status.COMPLETE
    assert known.answer.rows[0]["breached"] == 0
    assert known.answer.rows[0]["monitored_minutes"] == 44640


@pytest.mark.parametrize("table,metric,filter_column,values,expected", [
    ("sla_evaluation", "enterprise.credit_exposure", "contract_key", [7], 0),
    ("sla_evaluation", "enterprise.eligible_credits", "contract_key", [1, 2], 13580),
    ("sla_evaluation", "enterprise.credit_exposure", "contract_key", [5], 0),
    ("service_month", "enterprise.sla_compliance", "service_key", [11], 1),
    ("service_month", "enterprise.breached_services", "service_key", [11], 0),
    ("applied_credit", "enterprise.applied_credits", "contract_key", [2], 0),
    ("applied_credit", "enterprise.applied_credits", "contract_key", [1], None),
])
async def test_selected_complete_controls(sources, catalog, table, metric, filter_column, values, expected):
    result = await execute(catalog, sources[1], query(
        table, metrics=[metric], filters=[selected(filter_column, *values)],
    ))
    assert result.status == Status.COMPLETE
    assert result.answer.rows == [{metric: expected}]
    assert result.answer.coverage.get("unknown_source_rows", 0) == 0


async def test_null_posting_is_not_absence_or_complete_known_subtotal(sources, catalog, tmp_path):
    folder = tmp_path / "data"
    shutil.copytree(sources[1], folder)
    path = folder / "applied_credit.parquet"
    table = pq.read_table(path)
    rows = table.to_pylist()
    row = next(row for row in rows if row["contract_key"] == 2 and row["month"].month == 7)
    row["amount_cents"] = None
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    metadata = next(entry for entry in manifest["tables"] if entry["id"] == "applied_credit")
    metadata.update(bytes=path.stat().st_size, file_sha256=file_hash(path))
    manifest_path.write_text(json.dumps(manifest))
    node = query(
        "applied_credit", metrics=["enterprise.applied_credits"], filters=[selected("contract_key", 2, 3)],
    )
    refused = await execute(catalog, folder, node)
    assert refused.status == Status.PARTIAL and refused.answer is None
    result = await execute(catalog, folder, node.model_copy(update={"completeness": "qualified_partial"}))
    assert result.status == Status.PARTIAL
    assert result.answer.coverage["unknown_source_rows"] == 1
    assert result.answer.rows == [{"enterprise.applied_credits": 5700}]


@pytest.mark.parametrize("table,metrics,columns", [
    ("sla_evaluation", ["enterprise.eligible_credits", "enterprise.credit_exposure"], []),
    ("sla_evaluation", [], ["contract_key", "month", "eligible_credit_cents", "applied_credit_cents"]),
    ("service_month", ["enterprise.monitored_service_minutes"], []),
])
async def test_raw_and_scalar_queries_share_filtered_coverage(sources, catalog, table, metrics, columns):
    node = query(table, metrics=metrics, columns=columns, filters=[selected("account_key", 6)])
    result = await execute(catalog, sources[1], node.model_copy(update={"completeness": "qualified_partial"}))
    assert result.status == Status.PARTIAL
    assert result.answer.coverage["unknown_source_rows"] == 1
    assert '"account_key" IN (?)' in coverage_query(result.receipts[0])


async def test_resolved_cohort_not_whole_profile_is_checked(sources, catalog):
    cohort = query(
        "service_month", columns=["service_key"], filters=[selected("service_key", 11)],
    ).model_copy(update={"id": "cohort"})
    node = QueryNode.model_validate(
        query("service_month", metrics=["enterprise.sla_compliance"]).model_dump() | {
            "depends_on": ["cohort"],
            "cohort_filters": [{
                "source": "cohort", "source_column": "service_key", "target_column": "service_key",
            }],
        },
    )
    result = await execute(catalog, sources[1], node, additional=[cohort])
    assert result.status == Status.COMPLETE
    assert result.answer.rows == [{"enterprise.sla_compliance": 1}]
    assert result.answer.coverage["unknown_source_rows"] == 0
    assert '"service_key" IN (?)' in coverage_query(result.receipts[-1])


async def test_nullable_unrelated_business_field_is_not_unknown_coverage(sources, catalog):
    node = QueryNode(
        id="source", kind="QuerySemanticModel", model="digital", table="session",
        columns=["session_key", "account_key"], expected_grain=["session_key"], date_role="month",
        filters=[selected("account_key", 1)],
    )
    result = await execute(catalog, sources[1], node)
    assert result.status == Status.COMPLETE
    node = node.model_copy(update={"filters": []})
    result = await execute(catalog, sources[1], node)
    assert result.status == Status.COMPLETE
    assert any(row["account_key"] is None for row in result.answer.rows)


@pytest.mark.parametrize("table,metric", [
    ("sla_evaluation", "enterprise.credit_exposure"),
    ("service_month", "enterprise.sla_compliance"),
])
def test_actual_app_data_override_refuses_mixed_unknown(sources, table, metric, monkeypatch):
    import telco_iq.api
    saved = []
    monkeypatch.setattr(telco_iq.api, "write_receipt", lambda path, value: saved.append(value))
    client = TestClient(create_app(ROOT, data=sources[1]), base_url="http://localhost:8765")
    context = client.get("/api/context").json()
    node = query(table, metrics=[metric], filters=[selected("account_key", 6)])
    plan = {
        "request_id": "tq004-actual-app", "question": "Source coverage, not a complete subtotal",
        "principal_scope": context["scope"], "as_of": "2026-09-29T00:00:00Z",
        "ontology_revision": context["revision"], "metric_selections": [metric],
        "nodes": [node.model_dump(mode="json")], "result_node": "source",
    }
    response = client.post("/api/execute", json=plan, headers={"X-Telco-Local": "submitted-plan"})
    assert response.status_code == 200
    assert response.json()["status"] == "PARTIAL" and response.json()["answer"] is None
    assert saved and saved[0]["result"]["status"] == "PARTIAL"


def test_measure_contracts_and_diagnostic_columns_are_separate(sources):
    ordinary = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    generated = Catalog(sources[0] / "contracts" / "catalog.json")
    assert generated.metrics == ordinary.metrics and len(generated.metrics) == 64
    service = pq.read_table(sources[1] / "service_month.parquet").to_pylist()
    unknown = next(row for row in service if row["service_key"] == 10 and row["month"].month == 7)
    unaffected = next(row for row in service if row["service_key"] == 11 and row["month"].month == 7)
    assert unknown["expected_minutes"] == unaffected["expected_minutes"] == 44640
    assert unknown["coverage_complete"] is False and unaffected["coverage_complete"] is True


@pytest.mark.parametrize("service,expected_status", [(10, Status.PARTIAL), (11, Status.COMPLETE)])
async def test_other_filter_operators_select_actual_rows(sources, catalog, service, expected_status):
    node = query(
        "service_month", metrics=["enterprise.breached_services"],
        filters=[
            {"column": "service_key", "op": "gt", "values": [service - 1]},
            {"column": "service_key", "op": "gte", "values": [service]},
            {"column": "service_key", "op": "lt", "values": [service + 1]},
        ],
    ).model_copy(update={"completeness": "qualified_partial"})
    result = await execute(catalog, sources[1], node)
    assert result.status == expected_status
    assert result.answer.coverage["matched_source_rows"] == 1
    assert result.answer.rows[0]["enterprise.breached_services"] == (None if service == 10 else 0)


@pytest.fixture(params=["r1", "r2"])
def historical_showcase(request):
    folder = ROOT / ".local" / f"northstar-showcase-v2-2026-10-15-{request.param}" / "data"
    assert (folder / "manifest.json").is_file(), "Historical migration regression requires preserved showcase archives"
    return folder


async def test_historical_showcase_economics_requires_mapping_proof(historical_showcase):
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    node = query(
        "sla_evaluation", metrics=["enterprise.eligible_credits", "enterprise.credit_exposure"],
        group_by=["account_key", "month"], filters=[selected("account_key", 6)],
    )
    result = await execute(catalog, historical_showcase, node)
    assert result.status == Status.BLOCKED_CAPABILITY
    assert result.answer is None and not result.receipts
    assert "mapping coverage proof absent" in result.errors[0]


def test_actual_app_historical_override_requires_mapping_proof(historical_showcase, monkeypatch):
    import telco_iq.api
    saved = []
    monkeypatch.setattr(telco_iq.api, "write_receipt", lambda path, value: saved.append(value))
    client = TestClient(create_app(ROOT, data=historical_showcase), base_url="http://localhost:8765")
    context = client.get("/api/context").json()
    metrics = ["enterprise.eligible_credits", "enterprise.credit_exposure"]
    node = query(
        "sla_evaluation", metrics=metrics,
        group_by=["account_key", "month"], filters=[selected("account_key", 6)],
    )
    response = client.post("/api/execute", json={
        "request_id": "tq004-historical-app", "question": "Historical mapping proof, not a complete subtotal",
        "principal_scope": context["scope"], "as_of": "2026-09-29T00:00:00Z",
        "ontology_revision": context["revision"], "metric_selections": metrics,
        "nodes": [node.model_dump(mode="json")], "result_node": "source",
    }, headers={"X-Telco-Local": "submitted-plan"})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "BLOCKED_CAPABILITY"
    assert result["answer"] is None and not result["receipts"]
    assert "mapping coverage proof absent" in result["errors"][0]
    assert saved and saved[0]["result"]["status"] == "BLOCKED_CAPABILITY"


@pytest.mark.parametrize("declared_diagnostic", [False, True])
async def test_showcase_revision_is_not_mapping_proof(historical_showcase, tmp_path, declared_diagnostic):
    folder = tmp_path / "data"
    shutil.copytree(historical_showcase, folder)
    path = folder / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["version"] = "showcase-v2.3"
    if declared_diagnostic:
        table = next(table for table in manifest["tables"] if table["id"] == "sla_evaluation")
        table["coverage"] = {"column": "coverage_complete", "kind": "effective_mapping"}
    path.write_text(json.dumps(manifest))
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    node = query(
        "sla_evaluation", metrics=["enterprise.eligible_credits", "enterprise.credit_exposure"],
        filters=[selected("account_key", 6)],
    )
    result = await execute(catalog, folder, node)
    assert result.status == Status.BLOCKED_CAPABILITY
    assert result.answer is None and not result.receipts
    assert "coverage" in result.errors[0]


@pytest.mark.parametrize("source,contract_key", [("r4", 7), ("miniature", 1)])
async def test_actual_r4_and_legacy_miniature_economic_controls(source, contract_key, monkeypatch):
    import telco_iq.api
    folder = (
        ROOT / ".local" / "northstar-showcase-v2-2026-10-15-r4" / "data"
        if source == "r4" else ROOT / "data" / "fixtures" / "miniature"
    )
    fact = next(
        row for row in pq.read_table(folder / "sla_evaluation.parquet").to_pylist()
        if row["contract_key"] == contract_key and row["month"].isoformat() == "2026-07-01"
    )
    metric = "enterprise.eligible_credits"
    node = query(
        "sla_evaluation", metrics=[metric], group_by=["contract_key", "month"],
        filters=[selected("contract_key", contract_key)],
    )
    expected = [{"contract_key": contract_key, "month": "2026-07-01", metric: fact["eligible_credit_cents"]}]
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    result = await execute(catalog, folder, node)
    assert result.status == Status.COMPLETE and result.answer.rows == expected
    monkeypatch.setattr(telco_iq.api, "write_receipt", lambda path, value: None)
    client = TestClient(create_app(ROOT, data=folder), base_url="http://localhost:8765")
    context = client.get("/api/context").json()
    response = client.post("/api/execute", json={
        "request_id": "tq004-migration-control", "question": "Covered source economic control",
        "principal_scope": context["scope"], "as_of": "2026-09-29T00:00:00Z",
        "ontology_revision": context["revision"], "metric_selections": [metric],
        "nodes": [node.model_dump(mode="json")], "result_node": "source",
    }, headers={"X-Telco-Local": "submitted-plan"})
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETE"
    assert response.json()["answer"]["rows"] == expected
