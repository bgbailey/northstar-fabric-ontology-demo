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
from telco_iq.demo_library import (
    RecordedAgentSurface,
    RecordedDemoLibrary,
    RecordedExecutiveDemo,
    RecordedScorecard,
    RecordedServiceAssuranceDemo,
)
from tools.build_agent_surface_captures import surface_filename
from tools.build_service_assurance_stream import update_library

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
    for endpoint in (
        "/api/catalog",
        "/api/context",
        "/api/native-demo",
        "/api/recorded-comparisons",
        "/api/agent-surface",
        "/api/scorecard",
    ):
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


def test_committed_demo_library_serves_four_model_service_assurance_stream():
    committed_path = ROOT / "recordings" / "northstar-demo-library-v2-2026-10-28.json"
    library = RecordedDemoLibrary.read(committed_path)
    assert library.service_assurance is not None
    sa = library.service_assurance
    assert sa.mode == "RECORDED_SERVICE_ASSURANCE"
    assert sa.profile == "service-assurance"
    assert sa.period == "2026-07"
    assert sa.ontology.sources == ["network", "enterprise", "finance", "care"]
    assert len(sa.ontology.entities) == 17
    assert len(sa.ontology.relationships) == 17
    assert len(sa.ontology.metrics) == 14
    assert {rule.id for rule in sa.ontology.rules} == {"R1", "R2", "R3", "R4", "R5"}
    assert [hop.hop for hop in sa.hops] == [1, 2, 3, 4, 5, 6, 7]
    assert [hop.hop for hop in sa.hops if hop.crosses_models] == [2, 6, 7]
    assert [item.model_id for item in sa.model_evidence] == ["network", "enterprise", "finance", "care"]

    top = sa.ranked_accounts[0]
    assert top.account_key == 1
    assert top.credit_exposure_cents == 13580
    assert top.recognized_service_revenue_cents == 9000
    assert top.contribution_margin_cents == -1400
    ranked_keys = {row.account_key for row in sa.ranked_accounts}
    assert 6 not in ranked_keys and 9 not in ranked_keys
    assert any("account_key=6" in item.subject and item.rule_id == "R5" for item in sa.exclusions)
    assert any("service_key=12" in item.subject and item.rule_id == "R1" for item in sa.exclusions)
    assert any("contract_key=10" in item.subject and item.rule_id == "R1" for item in sa.exclusions)

    client = TestClient(create_app(ROOT, recorded_library=committed_path), base_url="http://localhost:8765")
    payload = client.get("/api/demo-library").json()
    assert payload["service_assurance"]["mode"] == "RECORDED_SERVICE_ASSURANCE"
    assert len(payload["service_assurance"]["hops"]) == 7
    assert len(payload["service_assurance"]["ranked_accounts"]) == len(sa.ranked_accounts)


def test_service_assurance_contract_rejects_malformed_stream_and_builder_is_deterministic(tmp_path):
    committed_path = ROOT / "recordings" / "northstar-demo-library-v2-2026-10-28.json"
    copy_path = tmp_path / "library-copy.json"
    copy_path.write_bytes(committed_path.read_bytes())

    work_dir = tmp_path / "work"
    update_library(library_path=copy_path, work_dir=work_dir)
    first_bytes = copy_path.read_bytes()
    update_library(library_path=copy_path, work_dir=work_dir)
    assert copy_path.read_bytes() == first_bytes
    assert first_bytes == committed_path.read_bytes()

    raw = RecordedDemoLibrary.read(copy_path).service_assurance.model_dump(mode="json")
    bad_order = deepcopy(raw)
    bad_order["ranked_accounts"][0], bad_order["ranked_accounts"][1] = (
        bad_order["ranked_accounts"][1],
        bad_order["ranked_accounts"][0],
    )
    with pytest.raises(ValidationError):
        RecordedServiceAssuranceDemo.model_validate(bad_order)

    bad_sources = deepcopy(raw)
    bad_sources["ontology"]["sources"] = ["network", "enterprise", "finance"]
    bad_sources["ontology"]["entities"] = [
        entity for entity in bad_sources["ontology"]["entities"] if entity["model"] != "care"
    ]
    with pytest.raises(ValidationError):
        RecordedServiceAssuranceDemo.model_validate(bad_sources)

    dup_surfaces = deepcopy(raw)
    dup_surfaces["agent_surfaces"].append(deepcopy(dup_surfaces["agent_surfaces"][0]))
    with pytest.raises(ValidationError, match="repeat surface_id"):
        RecordedServiceAssuranceDemo.model_validate(dup_surfaces)


def test_four_sanitized_agent_surface_recordings_and_screenshots_play_standalone_and_in_library():
    committed_library = RecordedDemoLibrary.read(
        ROOT / "recordings" / "northstar-demo-library-v2-2026-10-28.json"
    )
    assert committed_library.service_assurance is not None
    embedded = {item.surface_id: item for item in committed_library.service_assurance.agent_surfaces}
    expected_provenances = {
        "fabric-ontology-mcp": "LIVE_DEMO_TENANT_SANITIZED_CAPTURE",
        "foundry-agent": "LIVE_DEMO_TENANT_SANITIZED_CAPTURE",
        "github-copilot-vscode": "STAGED_SURFACE_PREVIEW_CAPTURE",
        "copilot-studio-m365": "STAGED_SURFACE_PREVIEW_CAPTURE",
    }
    assert set(embedded) == set(expected_provenances)

    plan = {
        "request_id": "synthetic-surface-execution-boundary",
        "question": "Not executed",
        "principal_scope": {
            "tenant": "synthetic",
            "principal": "test",
            "authorization_revision": "1",
            "models": [],
            "mode": "OFFLINE_SYNTHETIC",
        },
        "as_of": "2026-09-30T00:00:00Z",
        "ontology_revision": "ignored",
        "metric_selections": [],
        "result_node": "answer",
        "nodes": [{"id": "answer", "kind": "SynthesizeAnswer", "source": "not-executed"}],
    }

    for surface_id, expected_provenance in expected_provenances.items():
        surface_path = ROOT / "recordings" / surface_filename(surface_id)
        assert surface_path.is_file(), f"Missing standalone surface recording: {surface_path}"
        surface = RecordedAgentSurface.read(surface_path)
        assert surface == embedded[surface_id]
        assert surface.provenance == expected_provenance
        turns_by_level = {turn.level: turn for turn in surface.turns}
        assert set(turns_by_level) == {"L2", "L4"}
        assert turns_by_level["L2"].row_count == 53 and turns_by_level["L2"].exclusions_reported == 2
        assert turns_by_level["L4"].row_count == 46 and turns_by_level["L4"].exclusions_reported == 3
        surface.verify_private_identifiers_absent({"SYNTHETIC_ABSENT_TOKEN"})

        client = TestClient(create_app(ROOT, recorded_library=surface_path), base_url="http://localhost:8765")
        health = client.get("/api/health").json()
        assert health["mode"] == "RECORDED_AGENT_SURFACE"
        assert not health["authenticated"] and health["scope"] is None and not health["data_ready"]
        served = client.get("/api/agent-surface")
        assert served.status_code == 200
        assert served.json()["surface_id"] == surface_id
        for blocked_get in (
            "/api/demo-library",
            "/api/catalog",
            "/api/context",
            "/api/native-demo",
            "/api/recorded-comparisons",
        ):
            assert client.get(blocked_get).status_code == 409
        for blocked_post in ("/api/execute", "/api/execute/stream"):
            resp = client.post(blocked_post, json=plan, headers={"X-Telco-Local": "submitted-plan"})
            assert resp.status_code == 409 and "cannot execute" in resp.json()["detail"]

        image_path = ROOT / "docs" / "agents" / "images" / f"{surface_id}.png"
        assert image_path.is_file(), f"Missing viewer screenshot: {image_path}"
        raw_png = image_path.read_bytes()
        assert raw_png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(raw_png) > 20_480

    foundry = embedded["foundry-agent"]
    with pytest.raises(ValueError, match="Private target"):
        foundry.verify_private_identifiers_absent({"gpt-5-mini"})

    missing_l4 = foundry.model_dump(mode="json")
    missing_l4["turns"] = [turn for turn in missing_l4["turns"] if turn["level"] == "L2"]
    with pytest.raises(ValidationError):
        RecordedAgentSurface.model_validate(missing_l4)

    unzoned = foundry.model_dump(mode="json")
    unzoned["captured_at"] = "2026-11-17T11:55:31"
    with pytest.raises(ValidationError):
        RecordedAgentSurface.model_validate(unzoned)


def scorecard_fixture(execution_mode: str = "offline_scripted") -> dict:
    return {
        "schema_version": "1.0",
        "mode": "RECORDED_SCORECARD",
        "generated_at": "2026-11-17T13:30:00Z",
        "conditions": {
            "execution_mode": execution_mode,
            "profile": "service-assurance",
            "seed": 20260929,
            "question_count": 27,
            "repeats": 5,
            "arms": ["baseline", "baseline_plus_rules", "ontology"],
            "capacity_sku": "F8",
            "region": "Central US",
            "model_deployment": "gpt-5-mini",
            "toolset_variant": "service_default",
            "started_at": "2026-11-17T13:00:00Z",
            "completed_at": "2026-11-17T13:20:00Z",
        },
        "arm_labels": {
            "baseline": "Arm A - Semantic models only",
            "baseline_plus_rules": "Arm A+ - Semantic models + rules in prompt",
            "ontology": "Arm B - Ontology-grounded",
        },
        "summary_by_arm": {
            "baseline": {
                "arm": "baseline",
                "total_runs": 135,
                "question_count": 27,
                "repeats": 5,
                "answer_correct_runs": 30,
                "answer_accuracy": {"proportion": 0.2222, "low": 0.1603, "high": 0.2995},
                "mean_key_precision": 0.44,
                "mean_key_recall": 0.52,
                "mean_key_f1": 0.4767,
                "path_accuracy": {"proportion": 0.2222, "low": 0.1603, "high": 0.2995},
                "unknown_handling_accuracy": {"proportion": 0.25, "low": 0.14, "high": 0.4},
                "mean_repeat_stability": 0.84,
                "unanimous_pass_rate": 0.2222,
                "traps_hit": {
                    "site-key-only": 25,
                    "credit-per-incident": 20,
                    "revenue-fan-out": 15,
                    "display-name-or-billing-key": 10,
                    "missing-as-zero": 20,
                },
                "effort": {
                    "mean_tool_calls": 9.8,
                    "mean_input_tokens": 4120.0,
                    "mean_output_tokens": 810.0,
                    "mean_seconds": 15.6,
                    "total_tokens": 665550,
                },
            },
            "baseline_plus_rules": {
                "arm": "baseline_plus_rules",
                "total_runs": 135,
                "question_count": 27,
                "repeats": 5,
                "answer_correct_runs": 93,
                "answer_accuracy": {"proportion": 0.6889, "low": 0.6065, "high": 0.7608},
                "mean_key_precision": 0.78,
                "mean_key_recall": 0.81,
                "mean_key_f1": 0.7947,
                "path_accuracy": {"proportion": 0.6889, "low": 0.6065, "high": 0.7608},
                "unknown_handling_accuracy": {"proportion": 0.75, "low": 0.6, "high": 0.86},
                "mean_repeat_stability": 0.91,
                "unanimous_pass_rate": 0.6296,
                "traps_hit": {
                    "site-key-only": 5,
                    "credit-per-incident": 5,
                    "revenue-fan-out": 4,
                    "display-name-or-billing-key": 6,
                    "missing-as-zero": 5,
                },
                "effort": {
                    "mean_tool_calls": 8.6,
                    "mean_input_tokens": 6450.0,
                    "mean_output_tokens": 740.0,
                    "mean_seconds": 14.2,
                    "total_tokens": 970650,
                },
            },
            "ontology": {
                "arm": "ontology",
                "total_runs": 135,
                "question_count": 27,
                "repeats": 5,
                "answer_correct_runs": 135,
                "answer_accuracy": {"proportion": 1.0, "low": 0.9723, "high": 1.0},
                "mean_key_precision": 1.0,
                "mean_key_recall": 1.0,
                "mean_key_f1": 1.0,
                "path_accuracy": {"proportion": 1.0, "low": 0.9723, "high": 1.0},
                "unknown_handling_accuracy": {"proportion": 1.0, "low": 0.9723, "high": 1.0},
                "mean_repeat_stability": 1.0,
                "unanimous_pass_rate": 1.0,
                "traps_hit": {
                    "site-key-only": 0,
                    "credit-per-incident": 0,
                    "revenue-fan-out": 0,
                    "display-name-or-billing-key": 0,
                    "missing-as-zero": 0,
                },
                "effort": {
                    "mean_tool_calls": 5.2,
                    "mean_input_tokens": 1850.0,
                    "mean_output_tokens": 420.0,
                    "mean_seconds": 8.4,
                    "total_tokens": 306450,
                },
            },
        },
        "by_level": {
            "L1": {
                "question_count": 6,
                "arms": {
                    "baseline": {
                        "runs": 30,
                        "answer_correct_runs": 30,
                        "answer_accuracy": {"proportion": 1.0, "low": 0.8865, "high": 1.0},
                        "mean_key_f1": 1.0,
                        "path_accuracy": {"proportion": 1.0, "low": 0.8865, "high": 1.0},
                        "unknown_handling_accuracy": {"proportion": 1.0, "low": 0.8865, "high": 1.0},
                        "mean_repeat_stability": 1.0,
                    },
                    "baseline_plus_rules": {
                        "runs": 30,
                        "answer_correct_runs": 30,
                        "answer_accuracy": {"proportion": 1.0, "low": 0.8865, "high": 1.0},
                        "mean_key_f1": 1.0,
                        "path_accuracy": {"proportion": 1.0, "low": 0.8865, "high": 1.0},
                        "unknown_handling_accuracy": {"proportion": 1.0, "low": 0.8865, "high": 1.0},
                        "mean_repeat_stability": 1.0,
                    },
                    "ontology": {
                        "runs": 30,
                        "answer_correct_runs": 30,
                        "answer_accuracy": {"proportion": 1.0, "low": 0.8865, "high": 1.0},
                        "mean_key_f1": 1.0,
                        "path_accuracy": {"proportion": 1.0, "low": 0.8865, "high": 1.0},
                        "unknown_handling_accuracy": {"proportion": 1.0, "low": 0.8865, "high": 1.0},
                        "mean_repeat_stability": 1.0,
                    },
                },
                "differences": [
                    {
                        "comparison": "ontology_vs_baseline",
                        "left_arm": "ontology",
                        "right_arm": "baseline",
                        "metric": "answer_accuracy",
                        "diff": 0.0,
                        "low": 0.0,
                        "high": 0.0,
                        "excludes_zero": False,
                    }
                ],
            }
        },
        "overall_differences": [
            {
                "comparison": "ontology_vs_baseline",
                "left_arm": "ontology",
                "right_arm": "baseline",
                "metric": "answer_accuracy",
                "diff": 0.7778,
                "low": 0.6296,
                "high": 0.9259,
                "excludes_zero": True,
            }
        ],
        "failed_questions": [
            {
                "question_id": "SA-L2-001",
                "level": "L2",
                "arm": "baseline",
                "correct_repeats": 0,
                "repeats": 5,
                "traps_hit": ["site-key-only"],
                "failure_reasons": ["key_mismatch", "trap:site-key-only"],
            }
        ],
        "limitations": ["Synthetic evaluation fixture only; not a live run."],
    }


def test_scorecard_contract_validates_bounds_and_embeds_cleanly_in_demo_library():
    for mode in ("offline_scripted", "live_demo_tenant"):
        raw = scorecard_fixture(mode)
        raw["harmless_extra_metadata"] = "ignored_cleanly"
        parsed = RecordedScorecard.model_validate(raw)
        assert parsed.mode == "RECORDED_SCORECARD"
        assert parsed.conditions.execution_mode == mode
        assert parsed.summary_by_arm["ontology"].answer_accuracy.proportion == 1.0
        parsed.verify_private_identifiers_absent({"SYNTHETIC_ABSENT_SECRET"})
        with pytest.raises(ValueError, match="Private target"):
            parsed.verify_private_identifiers_absent({"gpt-5-mini"})

    with_scorecard = library_fixture()
    with_scorecard["scorecard"] = scorecard_fixture("live_demo_tenant")
    parsed_library = RecordedDemoLibrary.model_validate(with_scorecard)
    assert parsed_library.scorecard is not None
    assert parsed_library.scorecard.conditions.execution_mode == "live_demo_tenant"

    without_scorecard = RecordedDemoLibrary.model_validate(library_fixture())
    assert "scorecard" not in without_scorecard.model_dump(mode="json")

    for mutation in (
        lambda d: d.update(generated_at="2026-11-17T13:30:00"),
        lambda d: d["conditions"].update(execution_mode="unknown_mode"),
        lambda d: d["conditions"].update(started_at="2026-11-17T13:00:00"),
        lambda d: d["summary_by_arm"].pop("baseline"),
        lambda d: d["summary_by_arm"]["ontology"]["answer_accuracy"].update(low=1.0, proportion=0.9),
        lambda d: d["summary_by_arm"]["ontology"].update(answer_correct_runs=136),
        lambda d: d["overall_differences"][0].update(low=0.9, diff=0.7778, high=0.95),
        lambda d: d["failed_questions"][0].update(correct_repeats=6),
        lambda d: d.update(limitations=[]),
    ):
        broken = deepcopy(scorecard_fixture())
        mutation(broken)
        with pytest.raises(ValidationError):
            RecordedScorecard.model_validate(broken)


def test_standalone_scorecard_endpoint_serves_validated_json_and_blocks_execution(tmp_path):
    scorecard_path = tmp_path / "scorecard.json"
    scorecard_path.write_text(
        RecordedScorecard.model_validate(scorecard_fixture("live_demo_tenant")).model_dump_json(),
        encoding="utf-8",
    )
    client = TestClient(create_app(ROOT, recorded_library=scorecard_path), base_url="http://localhost:8765")

    health = client.get("/api/health").json()
    assert health["mode"] == "RECORDED_SCORECARD"
    assert not health["authenticated"] and health["scope"] is None and not health["data_ready"]
    assert health["readiness"]["status"] == "RECORDED_ONLY"
    assert client.get("/api/capabilities").json()["mode"] == "RECORDED_SCORECARD"

    served = client.get("/api/scorecard")
    assert served.status_code == 200
    payload = served.json()
    assert payload["mode"] == "RECORDED_SCORECARD"
    assert payload["conditions"]["execution_mode"] == "live_demo_tenant"
    assert payload["overall_differences"][0]["excludes_zero"] is True

    for blocked_get in (
        "/api/demo-library",
        "/api/agent-surface",
        "/api/catalog",
        "/api/context",
        "/api/native-demo",
        "/api/recorded-comparisons",
        "/api/fixtures/sla",
    ):
        assert client.get(blocked_get).status_code == 409

    plan = {
        "request_id": "synthetic-scorecard-execution-boundary",
        "question": "Not executed",
        "principal_scope": {
            "tenant": "synthetic",
            "principal": "test",
            "authorization_revision": "1",
            "models": [],
            "mode": "OFFLINE_SYNTHETIC",
        },
        "as_of": "2026-09-30T00:00:00Z",
        "ontology_revision": "ignored",
        "metric_selections": [],
        "result_node": "answer",
        "nodes": [{"id": "answer", "kind": "SynthesizeAnswer", "source": "not-executed"}],
    }
    for blocked_post in ("/api/execute", "/api/execute/stream"):
        resp = client.post(blocked_post, json=plan, headers={"X-Telco-Local": "submitted-plan"})
        assert resp.status_code == 409 and "cannot execute" in resp.json()["detail"]
    assert client.post(
        "/api/compare/stream",
        json={"question": "Not executed"},
        headers={"X-Telco-Local": "submitted-plan"},
    ).status_code == 409

    offline = TestClient(create_app(ROOT), base_url="http://localhost:8765")
    assert offline.get("/api/scorecard").status_code == 409


