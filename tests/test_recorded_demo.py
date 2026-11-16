import base64
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from telco_iq.api import create_app
from telco_iq.catalog import digest
from telco_iq.recorded_demo import RecordedDemo
from tools.build_recorded_demo import build_packet, relationship_rows

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def captures():
    """Synthetic contract fixture, not a live capability receipt."""
    models = {
        "subscriptions": {"id": "fixture-subscriptions", "displayName": "Fixture Subscriptions"},
        "finance": {"id": "fixture-finance", "displayName": "Fixture Finance"},
    }
    entities = []
    parts = []

    def part(path, text):
        parts.append({"path": path, "payloadType": "InlineBase64",
                      "payload": base64.b64encode(text.encode()).decode()})

    for name, model, table in (
        ("Subscription Month", "subscriptions", "Subscription Month"),
        ("Subscription Accounts", "subscriptions", "Accounts"),
        ("Finance Accounts", "finance", "Finance Accounts"),
        ("Account Month", "finance", "Account Month"),
    ):
        entities.append({
            "name": name, "annotations": [{"name": "source_model", "value": models[model]["displayName"]},
                                         {"name": "rule_period", "value": "Use the same month."}],
            "additionalBackingTables": [], "backingTable": table, "description": "Synthetic fixture entity.",
            "keyProperties": ["account_key"], "properties": [{"name": "account_key"}],
        })
        part(f"entities/{name}.tmdl", f"entity '{name}'\r\n")
    for model, table, measure in (
        ("subscriptions", "Subscription Month", "Active Lines EOM"),
        ("finance", "Account Month", "Recognized Service Revenue"),
    ):
        link = {"w": "fixture-workspace", "i": models[model]["id"], "n": models[model]["displayName"],
                "t": table, "m": measure, "d": "Source-owned synthetic measure."}
        part(f"tables/{table}.tmdl", (
            f"table '{table}'\r\n"
            f'\tmeasure \'{measure}\' = EXTERNALMEASURE("{measure}", INTEGER, '
            f'"DirectQuery to AS - fixture-workspace/{models[model]["id"]}")\r\n'
            f"\t\tannotation measure_fixture = {json.dumps(link)}\r\n"
        ))
    part("entityRelationships.tmdl", "\n\n".join(
        f"entityRelationship '{start} to {end}'\n\tfromEntity: '{start}'\n\ttoEntity: '{end}'"
        "\n\tannotation rule = Use the conformed account."
        for start, end in (
            ("Subscription Month", "Subscription Accounts"),
            ("Subscription Accounts", "Finance Accounts"),
            ("Account Month", "Finance Accounts"),
        )
    ))
    context = {
        "lane": "LIVE_DEV_AUTHOR_DIAGNOSTIC", "completedAt": "2026-09-30T21:00:00Z",
        "rawNativeContext": {
            name: {"isError": False, "structuredContent": {"values": values}}
            for name, values in (("list_ontology_entities", entities), ("list_ontology_rules", []))
        },
    }
    captured = {
        "lane": "LIVE_DEV_NATIVE_AGENT_UI", "completedAt": "2026-09-30T21:01:00Z",
        "table": {"headers": ["account_key", "active_lines_eom", "recognized_service_revenue_cents"],
                  "rows": [["1", "0", "0"], ["2", "3", "1002"]]},
        "sourceQueriesDisplayed": [
            "EVALUATE SUMMARIZECOLUMNS('Subscription Month'[account_key], \"lines\", [Active Lines EOM])",
            "EVALUATE SUMMARIZECOLUMNS('Account Month'[account_key], \"revenue\", [Recognized Service Revenue])",
        ],
    }
    return {"definition": {"definition": {"parts": parts}}, "context": context, "captured": captured,
            "question": "A synthetic contract-test question.", "models": models}


def test_normalization_uses_native_captures_and_preserves_zero_and_metric_sources(captures):
    packet = build_packet(**captures)
    assert len(packet.entities) == 4 and len(packet.relationships) == 3
    assert len(packet.metrics) == 2 and packet.native_rule_count == 0
    assert packet.metrics[0].entity_id == "Subscription Month"
    assert packet.metrics[0].home_table == "Subscription Month"
    assert packet.rows[0].recognized_service_revenue_cents == 0
    assert packet.relationships[-1].source == "Account Month"
    assert "fixture-workspace" not in packet.model_dump_json()
    assert "fixture-subscriptions" not in packet.model_dump_json()


def test_unknown_or_partial_native_capture_is_not_a_recorded_success(captures):
    captured = copy.deepcopy(captures)
    captured["context"]["rawNativeContext"]["list_ontology_entities"]["structuredContent"]["truncated"] = True
    with pytest.raises(ValueError, match="changed or failed"):
        build_packet(**captured)
    captures["captured"]["table"]["rows"][0][2] = ""
    with pytest.raises(ValueError, match="blank is not zero"):
        build_packet(**captures)
    with pytest.raises(ValueError, match="endpoints"):
        relationship_rows("entityRelationship Incomplete\n\tfromEntity: Account")


def test_packet_rejects_invented_edges_and_wrong_metric_ownership(captures):
    data = build_packet(**captures).model_dump(mode="json")
    data["focus_entity_ids"] = ["Subscription Month", "Account Month"]
    with pytest.raises(ValueError, match="connection absent"):
        RecordedDemo.model_validate(data)
    data["focus_entity_ids"] = ["Subscription Month", "Subscription Accounts"]
    data["metrics"][0]["source_id"] = "finance"
    with pytest.raises(ValueError, match="source and home"):
        RecordedDemo.model_validate(data)


def test_replay_mode_is_explicit_read_only_and_never_runs_fixture_plans(captures, tmp_path):
    packet = build_packet(**captures)
    path = tmp_path / "recorded.json"
    path.write_text(packet.model_dump_json(), encoding="utf-8")
    client = TestClient(create_app(ROOT, recorded_native=path), base_url="http://localhost:8765")
    assert client.get("/api/health").json()["mode"] == "RECORDED_NATIVE_DEMO"
    assert client.get("/api/health").json()["data_ready"] is False
    health = client.get("/api/health").json()
    assert health["readiness"]["mode"] == packet.mode
    assert health["readiness"]["reader_session"] == "NOT_USED"
    assert health["readiness"]["application_execution"] == "NOT_ESTABLISHED"
    assert health["provenance"]["captured_at"] == packet.model_dump(mode="json")["captured_at"]
    assert health["provenance"]["context_captured_at"] != health["provenance"]["captured_at"]
    assert health["provenance"]["created_at"] is None
    assert health["provenance"]["answer_origin"] == "BUILT_IN_NATIVE_AGENT"
    assert health["context_revision"] == digest(packet.model_dump(mode="json", exclude={"created_at"}))
    assert client.get("/api/native-demo").json()["rows"][1]["recognized_service_revenue_cents"] == 1002
    assert client.get("/api/capabilities").json()["mode"] == "RECORDED_NATIVE_DEMO"
    assert client.get("/api/context").status_code == 409
    assert client.get("/api/catalog").status_code == 409
    plan = {
        "request_id": "synthetic-replay-boundary", "question": "Not executed",
        "principal_scope": {"tenant": "synthetic", "principal": "test", "authorization_revision": "1",
                            "models": [], "mode": "OFFLINE_SYNTHETIC"},
        "as_of": "2026-09-30T00:00:00Z", "ontology_revision": "ignored",
        "metric_selections": [], "result_node": "answer",
        "nodes": [{"id": "answer", "kind": "SynthesizeAnswer", "source": "not-executed"}],
    }
    response = client.post("/api/execute", json=plan, headers={"X-Telco-Local": "submitted-plan"})
    assert response.status_code == 409 and "cannot execute" in response.json()["detail"]
    offline = TestClient(create_app(ROOT), base_url="http://localhost:8765")
    assert offline.get("/api/native-demo").status_code == 409


def test_missing_or_invalid_packet_never_silently_selects_another_mode(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_app(ROOT, recorded_native=tmp_path / "missing.json")
    path = tmp_path / "bad.json"
    path.write_text("{}")
    with pytest.raises(ValueError):
        create_app(ROOT, recorded_native=path)


def test_recording_creation_time_is_optional_and_never_replaces_capture_time(captures):
    packet = build_packet(**captures)
    assert packet.created_at is None
    data = packet.model_dump(mode="json")
    data["created_at"] = "2026-10-14T20:00:00Z"
    recorded = RecordedDemo.model_validate(data)
    assert recorded.captured_at == packet.captured_at
    data["created_at"] = "2026-10-14T20:00:00"
    with pytest.raises(ValueError, match="timezone"):
        RecordedDemo.model_validate(data)
