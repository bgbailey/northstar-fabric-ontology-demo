"""Synthetic recording contracts only; no live service, model or source queries."""

from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_recorded_ontology import packet as ontology_packet

from telco_iq.api import create_app
from telco_iq.recorded_comparison import RecordedComparisons

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def packet():
    clocks = {"data": "unit-data", "model": "unit-model", "ontology": "unit-ontology", "context": "unit-context"}
    answer = {
        "rows": [{"account_key": 1, "finance.contribution_margin": 17}],
        "status": "PARTIAL", "grain": ["account_key"], "evidence_ids": ["unit-receipt"],
        "coverage": {"unknown_keys": 1}, "warnings": ["SYNTHETIC UNIT ONLY: known mapped subset."],
        "clocks": clocks, "source_clocks": [{
            "model": "finance", "table": "account_month", "data": "unit-data", "model_revision": "unit-model",
        }],
        "population_qualification": "known_mapped_subset", "exclusions": [{
            "source_node": "unit-cohort", "source_column": "account_key", "unknown_mapping_observations": 1,
            "null_key_rows": 0, "evidence_ids": ["unit-receipt"],
        }],
    }
    receipt = {
        "id": "unit-receipt", "node_id": "query", "lane": "LIVE_DELEGATED", "tool": "UNIT-ONLY",
        "model": "finance", "query": "SYNTHETIC UNIT QUERY, NOT EXECUTED", "elapsed_ms": 1,
        "returned_rows": 1, "status": "PARTIAL", "completeness_evidence": "Synthetic contract fixture",
        "clocks": clocks, "warnings": [],
    }
    execution = {
        "request_id": "unit-native", "status": "PARTIAL", "mode": "LIVE_DELEGATED", "answer": answer,
        "receipts": [receipt], "errors": [], "elapsed_ms": 2,
    }
    result = {
        "pair_id": "unit-pair", "question": "SYNTHETIC recording contract test",
        "mode": "LIVE_DELEGATED", "source_facts_hash": "unit-facts", "context_revision": "unit-context",
        "provider": "SYNTHETIC_UNIT_PROVIDER", "model_version": "SYNTHETIC_UNIT_VERSION",
        "arms": [
            {"arm": "baseline", "status": "FAILED", "plan": None, "planning_receipt": None,
             "execution": None, "errors": ["Synthetic explicit failure"], "elapsed_ms": 1},
            {"arm": "native", "status": "PARTIAL", "plan": {"nodes": [
                {"id": "query", "kind": "QuerySemanticModel", "model": "finance"},
            ]}, "planning_receipt": None, "execution": execution, "errors": [], "elapsed_ms": 3},
        ],
        "comparability": "NOT_COMPARABLE", "comparability_reasons": ["Synthetic baseline failed"],
        "limitations": ["UNIT ONLY, not a service capability observation."],
    }
    return {
        "schema_version": "1.0", "mode": "RECORDED_LIVE_COMPARISON", "exported_at": "2026-10-21T13:00:00Z",
        "entries": [{
            "title": "Synthetic story", "run_started_at": "2026-10-20T12:00:00Z",
            "original_result_sha256": "a" * 64, "original_server_receipt_sha256": "b" * 64,
            "source_value_review_sha256": "c" * 64, "result": result,
            "events": {"baseline": [], "native": []},
        }],
        "limitations": ["UNIT ONLY: explicit historical playback, no current source authorization."],
    }


def test_recorded_pair_retains_partial_failure_and_four_clocks_without_a_reader_grant(packet):
    value = RecordedComparisons.model_validate(packet)
    native = value.entries[0].result.arms[1]
    assert native.execution.answer.status == "PARTIAL"
    assert native.execution.answer.exclusions[0].unknown_mapping_observations == 1
    assert native.execution.answer.clocks.data == "unit-data"
    assert value.entries[0].result.arms[0].status == "FAILED"
    assert value.entries[0].run_started_at != value.exported_at
    assert "principal_scope" not in value.model_dump_json()


@pytest.mark.parametrize("mutation", ["complete_qualification", "unknown_source", "no_native", "duplicate_arm", "scope"])
def test_recorded_pair_rejects_relabelled_or_executable_outputs(packet, mutation):
    entry = packet["entries"][0]
    native = entry["result"]["arms"][1]
    if mutation == "complete_qualification":
        native["execution"]["answer"]["status"] = "COMPLETE"
    elif mutation == "unknown_source":
        native["execution"]["answer"]["coverage"]["unknown_source_rows"] = 1
    elif mutation == "no_native":
        native["execution"]["answer"] = None
    elif mutation == "duplicate_arm":
        entry["result"]["arms"][0]["arm"] = "native"
    else:
        native["plan"]["principal_scope"] = {"principal": "SYNTHETIC SHOULD NOT BE EXPORTED"}
    with pytest.raises(ValidationError):
        RecordedComparisons.model_validate(packet)


def test_recorded_packet_checks_private_identifiers_before_publication(packet):
    value = RecordedComparisons.model_validate(packet)
    value.verify_private_identifiers_absent({"SYNTHETIC_PRIVATE_TARGET_NOT_PRESENT"})
    with pytest.raises(ValueError, match="Private target"):
        value.verify_private_identifiers_absent({"unit-pair"})


def test_recorded_comparison_mode_is_explicit_and_every_execution_path_is_blocked(packet, tmp_path):
    path = tmp_path / "recorded-comparisons.json"
    path.write_text(RecordedComparisons.model_validate(packet).model_dump_json(), encoding="utf-8")
    client = TestClient(create_app(ROOT, recorded_comparisons=path), base_url="http://localhost:8765")
    health = client.get("/api/health").json()
    assert health["mode"] == "RECORDED_LIVE_COMPARISON" and health["scope"] is None
    assert not health["authenticated"] and not health["readiness"]["ready"]
    data = client.get("/api/recorded-comparisons").json()
    assert data["entries"][0]["run_started_at"] == "2026-10-20T12:00:00Z"
    for endpoint in ("/api/catalog", "/api/context", "/api/native-demo"):
        assert client.get(endpoint).status_code == 409
    assert client.post("/api/compare/stream", json={"question": "Never execute"},
                       headers={"X-Telco-Local": "submitted-plan"}).status_code == 409
    offline = TestClient(create_app(ROOT), base_url="http://localhost:8765")
    assert offline.get("/api/recorded-comparisons").status_code == 409
    with pytest.raises(ValueError, match="cannot be combined"):
        create_app(ROOT, recorded_native=path, recorded_comparisons=path)


def test_recording_never_relabels_naive_timestamps_or_duplicates_pairs(packet):
    original = deepcopy(packet)
    packet["entries"][0]["run_started_at"] = "2026-10-20T12:00:00"
    with pytest.raises(ValidationError, match="zoned"):
        RecordedComparisons.model_validate(packet)
    original["entries"].append(deepcopy(original["entries"][0]))
    with pytest.raises(ValidationError, match="repeat"):
        RecordedComparisons.model_validate(original)


def test_recorded_definition_must_match_the_original_answer_clock(packet):
    packet["ontology"] = ontology_packet()
    with pytest.raises(ValidationError, match="ontology clock"):
        RecordedComparisons.model_validate(packet)
    packet["entries"][0]["result"]["arms"][1]["execution"]["answer"]["clocks"]["ontology"] = "a" * 64
    value = RecordedComparisons.model_validate(packet)
    assert value.ontology.metrics[0].name == "Amount"
