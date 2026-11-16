"""Synthetic archive fixtures only; never native service or live business-answer evidence."""

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_recorded_comparison import packet as comparison_fixture
from test_recorded_ontology import packet as ontology_fixture

from telco_iq.api import create_app
from telco_iq.demo_library import RecordedDemoLibrary, RecordedExecutiveDemo

ROOT = Path(__file__).resolve().parents[1]


def executive_fixture():
    ontology = {
        "captured_at": "2026-10-28T12:00:00Z", "native_snapshot_hash": "d" * 64,
        "provenance": "ACTUAL_NATIVE_DEFINITION_AND_METADATA_CAPTURE",
        "sources": ["subscriptions", "finance"],
        "entities": [
            {"id": entity, "model": model, "table": table, "name": entity,
             "description": "SYNTHETIC UNIT ONLY", "keys": ["account_key"] if table == "account" else []}
            for entity, model, table in (
                ("SubAccounts", "subscriptions", "account"), ("SubMonth", "subscriptions", "subscription_month"),
                ("FinAccounts", "finance", "account"), ("FinMonth", "finance", "account_month"),
            )
        ],
        "relationships": [
            {"id": name, "source": source, "target": target, "description": "SYNTHETIC UNIT ONLY"}
            for name, source, target in (
                ("lines", "SubMonth", "SubAccounts"), ("identity", "SubAccounts", "FinAccounts"),
                ("money", "FinMonth", "FinAccounts"),
            )
        ],
        "metrics": [
            {"id": metric, "model": model, "table": table, "entity": entity,
             "name": metric, "description": "SYNTHETIC UNIT ONLY", "representation": "EXTERNALMEASURE"}
            for metric, model, table, entity in (
                ("subscriptions.active_lines_eom", "subscriptions", "subscription_month", "SubMonth"),
                ("finance.recognized_service_revenue", "finance", "account_month", "FinMonth"),
                ("finance.contribution_margin", "finance", "account_month", "FinMonth"),
            )
        ],
        "rules": [], "execution_relationships": {}, "limitations": ["SYNTHETIC UNIT ONLY"],
    }
    return {
        "captured_at": "2026-10-28T12:02:00Z", "question": "SYNTHETIC NOT EXECUTED",
        "ontology": ontology, "separate_native_item_verified": True,
        "focus_metric_ids": [metric["id"] for metric in ontology["metrics"]],
        "native_answer_origin": "MANAGED_NATIVE_SERVICE", "native_answer_text": "SYNTHETIC UNIT ONLY, NOT A SERVICE ANSWER",
        "native_response_sha256": "a" * 64, "source_measure_review_sha256": "b" * 64,
        "native_answer_matches_source_values": True,
        "rows": [{
            "account_id": "SYNTHETIC_ACCOUNT", "account_key": 1, "active_lines_eom": 0,
            "recognized_service_revenue_cents": 123, "contribution_margin_cents": -17,
        }],
        "source_queries": [{
            "model": model, "dax": text, "dax_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "response_sha256": "c" * 64, "observed_at": "2026-10-28T12:01:00Z",
        } for model, text in (("subscriptions", "SYNTHETIC QUERY A"), ("finance", "SYNTHETIC QUERY B"))],
        "source_clocks": [{
            "model": model, "table": "subscription_month" if model == "subscriptions" else "account_month",
            "data": "SYNTHETIC", "model_revision": "SYNTHETIC",
        } for model in ("subscriptions", "finance")],
        "limitations": ["SYNTHETIC UNIT ONLY; no service, reader or causal claim."],
    }


def library_fixture():
    full = comparison_fixture.__wrapped__()
    ontology = ontology_fixture()
    ontology["sources"] = ["finance", "care", "subscriptions", "network", "enterprise", "digital", "commerce", "growth"]
    ontology["entities"].extend({
        "id": model, "model": model, "table": "account", "name": model,
        "description": "SYNTHETIC UNIT ONLY", "keys": ["account_key"],
    } for model in ("subscriptions", "network", "enterprise", "digital", "commerce", "growth"))
    full["ontology"] = ontology
    full["entries"][0]["result"]["arms"][1]["execution"]["answer"]["clocks"]["ontology"] = "a" * 64
    return {
        "exported_at": "2026-10-28T12:03:00Z", "executive": executive_fixture(), "customer360": full,
        "limitations": ["SYNTHETIC UNIT ONLY; no new query on selection."],
    }


def test_executive_retains_extra_original_proxies_while_focusing_three_trusted_measures():
    value = executive_fixture()
    extra = deepcopy(value["ontology"]["metrics"][1])
    extra["id"] = "finance.extra_original_measure"
    value["ontology"]["metrics"].append(extra)
    parsed = RecordedExecutiveDemo.model_validate(value)
    assert len(parsed.ontology.entities) == 4 and len(parsed.ontology.metrics) == 4
    assert len(parsed.focus_metric_ids) == 3


@pytest.mark.parametrize("change", ["large-slice", "unverified", "missing-metric", "duplicate-row", "missing-owner", "query", "clock"])
def test_executive_archive_refuses_relabelled_slices_unknown_values_or_missing_provenance(change):
    value = executive_fixture()
    if change == "large-slice":
        value["separate_native_item_verified"] = False
    elif change == "unverified":
        value["native_answer_matches_source_values"] = False
    elif change == "missing-metric":
        value["ontology"]["metrics"].pop()
    elif change == "duplicate-row":
        value["rows"].append(deepcopy(value["rows"][0]))
    elif change == "missing-owner":
        value["source_queries"].pop()
    elif change == "query":
        value["source_queries"][0]["dax"] = "CHANGED"
    else:
        value["captured_at"] = "2026-10-28T12:02:00"
    with pytest.raises(ValidationError):
        RecordedExecutiveDemo.model_validate(value)


def test_demo_library_keeps_both_different_native_definitions_and_original_dates():
    value = RecordedDemoLibrary.model_validate(library_fixture())
    assert value.executive.ontology.native_snapshot_hash != value.customer360.ontology.native_snapshot_hash
    assert value.executive.captured_at != value.exported_at
    value.verify_private_identifiers_absent({"SYNTHETIC_PRIVATE_ID_NOT_PRESENT"})
    with pytest.raises(ValueError, match="Private target"):
        value.verify_private_identifiers_absent({"SYNTHETIC_ACCOUNT"})


def test_two_stream_library_never_grants_live_reader_or_fixture_execution(tmp_path):
    path = tmp_path / "synthetic-library.json"
    path.write_text(RecordedDemoLibrary.model_validate(library_fixture()).model_dump_json(), encoding="utf-8")
    client = TestClient(create_app(ROOT, recorded_library=path), base_url="http://localhost:8765")
    health = client.get("/api/health").json()
    assert health["mode"] == "RECORDED_DEMO_LIBRARY"
    assert not health["authenticated"] and health["scope"] is None and not health["data_ready"]
    assert client.get("/api/demo-library").json()["executive"]["separate_native_item_verified"] is True
    for endpoint in ("/api/catalog", "/api/context", "/api/native-demo", "/api/recorded-comparisons"):
        assert client.get(endpoint).status_code == 409
    plan = {
        "request_id": "synthetic-library-execution-boundary", "question": "Not executed",
        "principal_scope": {"tenant": "synthetic", "principal": "test", "authorization_revision": "1",
                            "models": [], "mode": "OFFLINE_SYNTHETIC"},
        "as_of": "2026-09-30T00:00:00Z", "ontology_revision": "ignored",
        "metric_selections": [], "result_node": "answer",
        "nodes": [{"id": "answer", "kind": "SynthesizeAnswer", "source": "not-executed"}],
    }
    for endpoint in ("/api/execute", "/api/execute/stream"):
        response = client.post(endpoint, json=plan, headers={"X-Telco-Local": "submitted-plan"})
        assert response.status_code == 409 and "cannot execute" in response.json()["detail"]
    assert client.post("/api/compare/stream", json={"question": "Not executed"},
                       headers={"X-Telco-Local": "submitted-plan"}).status_code == 409
    offline = TestClient(create_app(ROOT), base_url="http://localhost:8765")
    assert offline.get("/api/demo-library").status_code == 409
    with pytest.raises(ValueError, match="cannot be combined"):
        create_app(ROOT, recorded_library=path, recorded_comparisons=path)


def test_a_large_capture_cannot_be_substituted_for_both_native_streams():
    value = library_fixture()
    value["customer360"]["ontology"]["native_snapshot_hash"] = "d" * 64
    value["customer360"]["entries"][0]["result"]["arms"][1]["execution"]["answer"]["clocks"]["ontology"] = "d" * 64
    with pytest.raises(ValidationError, match="distinct actual"):
        RecordedDemoLibrary.model_validate(value)
