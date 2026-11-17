"""M2-06 and M2-07 offline tests for service-assurance golden questions, oracle, traps, and arms."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from eval.arms import (
    ArmRunRecord,
    ScriptedBaselineArm,
    ScriptedOntologyArm,
    get_offline_arm,
)
from eval.oracle.service_assurance import (
    VALID_LEVELS,
    VALID_RULES,
    VALID_TRAPS,
    evaluate_question,
    evaluate_trap,
    load_questions,
    validate_question_record,
)
from eval.runners import run_offline, run_offline_arms
from tools import build_data

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = ROOT / "eval" / "questions" / "service-assurance.jsonl"
CONTRACT_PATH = ROOT / "fabric" / "ontology" / "service-assurance.json"


@pytest.fixture(scope="module")
def generated_profiles(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("m2-06-eval-profiles")
    folders: dict[str, Path] = {}
    for profile in ("showcase-v2", "service-assurance"):
        target = base / profile
        build_data.build(profile, build_data.SEED, target)
        folders[profile] = target
    return folders


@pytest.fixture(scope="module")
def golden_questions() -> list[dict[str, Any]]:
    return load_questions(QUESTIONS_PATH)


def test_golden_questions_schema_levels_and_appendix_b_hero_record(
    golden_questions: list[dict[str, Any]],
) -> None:
    assert len(golden_questions) >= 24
    counts = Counter(q["level"] for q in golden_questions)
    assert set(VALID_LEVELS) <= set(counts)
    for level in VALID_LEVELS:
        assert counts[level] >= 3, f"Expected multiple questions for level {level}"

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    valid_metrics = {item["name"] for item in contract["metrics"]}

    for question in golden_questions:
        validate_question_record(question)
        for column in question["answer"]["columns"]:
            assert column["metric"] in valid_metrics, (question["id"], column["metric"])
            assert column["type"] == "integer", (question["id"], column["name"])

    all_rules_covered = {rule for q in golden_questions for rule in q["rules"]}
    all_traps_covered = {trap for q in golden_questions for trap in q["traps"]}
    assert all_rules_covered == set(VALID_RULES)
    assert all_traps_covered == set(VALID_TRAPS)

    hero = next(q for q in golden_questions if q["id"] == "SA-L4-001")
    assert hero == {
        "id": "SA-L4-001",
        "level": "L4",
        "models": ["network", "enterprise", "finance", "care"],
        "question": (
            "Which enterprise customers were served from sites hit by July 2026 incidents, "
            "what SLA credit exposure do we carry on their contracts, how much revenue and "
            "margin is at stake, and who already has escalated cases?"
        ),
        "parameters": {"month": "2026-07", "severity": "major"},
        "persona": "default",
        "answer": {
            "keys": ["account_key"],
            "columns": [
                {"name": "credit_exposure_cents", "metric": "Credit Exposure", "type": "integer"},
                {
                    "name": "recognized_service_revenue_cents",
                    "metric": "Recognized Service Revenue",
                    "type": "integer",
                },
                {
                    "name": "contribution_margin_cents",
                    "metric": "Contribution Margin",
                    "type": "integer",
                },
                {"name": "escalated_cases", "metric": "Escalated Cases", "type": "integer"},
            ],
            "orderBy": ["credit_exposure_cents desc"],
            "exclusionsExpected": True,
        },
        "rules": ["R1", "R2", "R3", "R4", "R5"],
        "traps": [
            "site-key-only",
            "credit-per-incident",
            "revenue-fan-out",
            "missing-as-zero",
        ],
    }


@pytest.mark.parametrize("profile", ["showcase-v2", "service-assurance"])
def test_run_offline_produces_expected_answers_for_both_profiles(
    generated_profiles: dict[str, Path],
    golden_questions: list[dict[str, Any]],
    tmp_path: Path,
    profile: str,
) -> None:
    folder = generated_profiles[profile]
    out_file = tmp_path / f"oracle-{profile}.json"
    exit_code = run_offline.main(
        [
            "--questions",
            str(QUESTIONS_PATH),
            "--profile",
            profile,
            "--data",
            str(folder),
            "--output",
            str(out_file),
        ]
    )
    assert exit_code == 0
    receipt = json.loads(out_file.read_text(encoding="utf-8"))
    assert receipt["status"] == "PASS"
    assert receipt["profile"] == profile
    assert receipt["question_count"] == len(golden_questions)

    by_id = {entry["question_id"]: entry for entry in receipt["results"]}
    assert len(by_id) == len(golden_questions)

    for question in golden_questions:
        entry = by_id[question["id"]]
        expected_excl = question["answer"]["exclusionsExpected"]
        assert bool(entry["exclusions"]) is expected_excl, question["id"]
        assert entry["status"] == ("PARTIAL" if expected_excl else "COMPLETE")
        for row in entry["rows"]:
            for col in question["answer"]["columns"]:
                assert type(row[col["name"]]) is int

    hero = by_id["SA-L4-001"]
    hero_by_acct = {row["account_key"]: row for row in hero["rows"]}
    assert hero_by_acct[1] == {
        "account_key": 1,
        "credit_exposure_cents": 13580,
        "recognized_service_revenue_cents": 9000,
        "contribution_margin_cents": -1400,
        "escalated_cases": 0,
    }
    assert hero_by_acct[2] == {
        "account_key": 2,
        "credit_exposure_cents": 5700,
        "recognized_service_revenue_cents": 24000,
        "contribution_margin_cents": 0,
        "escalated_cases": 1,
    }
    assert 6 not in hero_by_acct
    assert any(
        excl.get("account_key") == 6 and excl.get("reason") == "incomplete_mapping_coverage"
        for excl in hero["exclusions"]
    )
    assert any(
        excl.get("incident_key") == 7 and excl.get("reason") == "unmapped_incident_window"
        for excl in hero["exclusions"]
    )

    empty_aug = by_id["SA-N-001"]
    assert empty_aug["rows"] == []
    assert empty_aug["exclusions"] == [
        {"entity": "incident", "month": "2026-08", "reason": "no_recorded_incidents_in_period"}
    ]

    applied_q = by_id["SA-N-003"]
    applied_by_ck = {row["contract_key"]: row["applied_credits_cents"] for row in applied_q["rows"]}
    assert applied_by_ck[2] == 0
    assert 1 not in applied_by_ck and 5 not in applied_by_ck
    assert {
        excl["contract_key"]
        for excl in applied_q["exclusions"]
        if excl.get("reason") == "missing_applied_credit_row"
    } >= {1, 5}

    east_hero = by_id["SA-P-001"]
    assert [row["account_key"] for row in east_hero["rows"]] == [1, 5]
    assert east_hero["rows"][0]["credit_exposure_cents"] == 5000
    assert east_hero["rows"][1]["credit_exposure_cents"] == 90


@pytest.mark.parametrize("profile", ["showcase-v2", "service-assurance"])
@pytest.mark.parametrize("trap_name", VALID_TRAPS)
def test_oracle_differs_from_every_trap_on_designed_cases(
    generated_profiles: dict[str, Path],
    golden_questions: list[dict[str, Any]],
    profile: str,
    trap_name: str,
) -> None:
    folder = generated_profiles[profile]
    designed = [q for q in golden_questions if trap_name in q["traps"]]
    assert len(designed) >= 10, f"Expected broad coverage for trap {trap_name}"

    for question in designed:
        oracle_ans = evaluate_question(question, folder)
        trap_ans = evaluate_trap(trap_name, question, folder)
        assert (
            trap_ans["rows"] != oracle_ans["rows"]
            or trap_ans["exclusions"] != oracle_ans["exclusions"]
        ), f"{profile} {question['id']} did not differ on trap {trap_name}"


@pytest.mark.parametrize("profile", ["showcase-v2", "service-assurance"])
def test_specific_trap_signatures_match_hero_scenario_failure_modes(
    generated_profiles: dict[str, Path],
    golden_questions: list[dict[str, Any]],
    profile: str,
) -> None:
    folder = generated_profiles[profile]
    q_by_id = {q["id"]: q for q in golden_questions}

    # 1. site-key-only: pulls non-overlapping services 12 & 13 (and 7/8 accounts in SA)
    l2_svc = q_by_id["SA-L2-001"]
    oracle_svcs = {r["service_key"] for r in evaluate_question(l2_svc, folder)["rows"]}
    trap_svcs = {r["service_key"] for r in evaluate_trap("site-key-only", l2_svc, folder)["rows"]}
    assert {10, 12, 13}.isdisjoint(oracle_svcs)
    assert {10, 12, 13} <= trap_svcs
    if profile == "service-assurance":
        hero = q_by_id["SA-L4-001"]
        oracle_accts = {r["account_key"] for r in evaluate_question(hero, folder)["rows"]}
        trap_accts = {r["account_key"] for r in evaluate_trap("site-key-only", hero, folder)["rows"]}
        assert {7, 8}.isdisjoint(oracle_accts)
        assert {7, 8} <= trap_accts

    # 2. credit-per-incident: attributes contract 1's monthly credit across multiple incidents
    hero = q_by_id["SA-L4-001"]
    oracle_hero_1 = next(
        r for r in evaluate_question(hero, folder)["rows"] if r["account_key"] == 1
    )
    trap_cpi_1 = next(
        r for r in evaluate_trap("credit-per-incident", hero, folder)["rows"] if r["account_key"] == 1
    )
    assert oracle_hero_1["credit_exposure_cents"] == 13580
    assert trap_cpi_1["credit_exposure_cents"] == 23580

    # 3. revenue-fan-out: multiplies account 1 revenue across its exposed service-contract rows
    trap_fan_1 = next(
        r for r in evaluate_trap("revenue-fan-out", hero, folder)["rows"] if r["account_key"] == 1
    )
    assert oracle_hero_1["recognized_service_revenue_cents"] == 9000
    assert trap_fan_1["recognized_service_revenue_cents"] > 9000

    # 4. display-name-or-billing-key: merges Northstar Business on display_name & mismatches billing_key
    l3_rev = q_by_id["SA-L3-001"]
    trap_id_1 = next(
        r
        for r in evaluate_trap("display-name-or-billing-key", l3_rev, folder)["rows"]
        if r["account_key"] == 1
    )
    assert trap_id_1["recognized_service_revenue_cents"] == 33000

    l2_bmap = q_by_id["SA-L2-004"]
    oracle_bmap = {
        r["account_key"]: r["recognized_service_revenue_cents"]
        for r in evaluate_question(l2_bmap, folder)["rows"]
    }
    trap_bmap = {
        r["account_key"]: r["recognized_service_revenue_cents"]
        for r in evaluate_trap("display-name-or-billing-key", l2_bmap, folder)["rows"]
    }
    assert trap_bmap[2] == oracle_bmap[1]
    assert trap_bmap[1] == 0

    # 5. missing-as-zero: coerces account 6 NULL exposure to 0 and sums open_cases_eod across days
    trap_zero_hero = evaluate_trap("missing-as-zero", hero, folder)
    assert trap_zero_hero["exclusions"] == []
    assert any(
        r["account_key"] == 6 and r["credit_exposure_cents"] == 0 for r in trap_zero_hero["rows"]
    )
    l1_eod = q_by_id["SA-L1-006"]
    oracle_eod_m01 = next(
        r["open_cases_eod"]
        for r in evaluate_question(l1_eod, folder)["rows"]
        if r["market_id"] == "M01"
    )
    trap_eod_m01 = next(
        r["open_cases_eod"]
        for r in evaluate_trap("missing-as-zero", l1_eod, folder)["rows"]
        if r["market_id"] == "M01"
    )
    assert trap_eod_m01 > oracle_eod_m01


def test_offline_arms_end_to_end_and_cli_scorecard_payload(
    generated_profiles: dict[str, Path],
    golden_questions: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    folder = generated_profiles["service-assurance"]
    out_path = tmp_path / "offline-arms-result.json"

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "eval" / "runners" / "run_offline_arms.py"),
            "--questions",
            str(QUESTIONS_PATH),
            "--profile",
            "service-assurance",
            "--data",
            str(folder),
            "--output",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    cli_summary = json.loads(proc.stdout)
    assert cli_summary["status"] == "PASS"
    assert cli_summary["arms"] == ["baseline", "ontology"]
    assert cli_summary["total_runs"] == len(golden_questions) * 2

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["mode"] == "OFFLINE_SCRIPTED_ARMS"
    assert payload["benchmark_claim"] is False
    assert payload["question_count"] == len(golden_questions)
    assert len(payload["runs"]) == len(golden_questions) * 2

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    valid_entities = {e["name"] for e in contract["entities"]}

    for run_dict in payload["runs"]:
        record = ArmRunRecord(**run_dict)
        assert record.arm in ("baseline", "ontology")
        assert record.effort["tool_call_count"] == len(record.tool_calls) > 0
        assert record.effort["input_tokens"] > 0
        assert record.effort["output_tokens"] > 0
        assert record.effort["elapsed_ms"] >= 0.0
        assert record.path["models_queried"]
        if record.arm == "ontology":
            assert set(record.path["entities_traversed"]) <= valid_entities
            if len(record.path["models_queried"]) > 1:
                assert len(record.path["bridges_used"]) >= 1

    summary = payload["summary"]
    assert summary["by_level_and_arm"]["L1"]["baseline"]["accuracy"] == 1.0
    assert summary["by_level_and_arm"]["L1"]["ontology"]["accuracy"] == 1.0
    assert summary["by_arm"]["ontology"]["accuracy"] == 1.0
    assert summary["by_arm"]["ontology"]["unknown_handling_rate"] == 1.0
    assert summary["by_arm"]["baseline"]["accuracy"] < summary["by_arm"]["ontology"]["accuracy"]
    assert set(summary["trap_hits_by_arm"]["baseline"]) == set(VALID_TRAPS)

    # Also verify direct API & JSONL mode on showcase-v2 plus baseline_plus_rules
    sc_jsonl = tmp_path / "offline-arms-showcase.jsonl"
    sc_payload = run_offline_arms.run_offline_arms(
        QUESTIONS_PATH,
        profile="showcase-v2",
        data_dir=generated_profiles["showcase-v2"],
        output_path=sc_jsonl,
    )
    assert sc_payload["summary"]["by_arm"]["ontology"]["accuracy"] == 1.0
    assert len(sc_jsonl.read_text(encoding="utf-8").strip().splitlines()) == (
        len(golden_questions) * 2
    )
    assert isinstance(get_offline_arm("baseline"), ScriptedBaselineArm)
    assert isinstance(get_offline_arm("ontology"), ScriptedOntologyArm)
    plus_record = get_offline_arm("baseline_plus_rules").run_question(
        golden_questions[0], folder, repeat=1
    )
    assert plus_record.arm == "baseline_plus_rules"


def test_strict_boundary_between_runtime_and_service_assurance_eval() -> None:
    for path in (ROOT / "src" / "telco_iq").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            )
            assert not any(
                mod == "eval" or mod.startswith("eval.") for mod in modules
            ), f"{path.name} imports eval: {modules}"

    oracle_path = ROOT / "eval" / "oracle" / "service_assurance.py"
    oracle_tree = ast.parse(oracle_path.read_text(encoding="utf-8"))
    for node in ast.walk(oracle_tree):
        modules = (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        )
        assert not any(
            mod.startswith(("telco_iq", "src", "tools", "eval.arms")) for mod in modules
        ), f"Oracle must stay independent, found import: {modules}"
