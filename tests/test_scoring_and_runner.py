"""Unit and integration tests for M4-02 runner, M4-03 scoring, and M4-04 scorecard."""

from __future__ import annotations

import copy
import json
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from eval.arms import (
    CONTRACT_PATH,
    ArmName,
    LiveAgentArm,
    LiveApprovalError,
    LiveEvalApproval,
    ScriptedBaselinePlusRulesArm,
    build_live_system_instructions,
    build_live_tools,
    get_offline_arm,
    parse_mcp_http_response,
)
from eval.oracle.service_assurance import (
    VALID_LEVELS,
    VALID_MODELS,
    VALID_TRAPS,
    evaluate_question,
    evaluate_trap,
    load_questions,
)
from eval.report import build_scorecard
from eval.runners import run_arms
from eval.scoring import (
    paired_bootstrap_interval,
    repeat_agreement,
    score_run,
    wilson_interval,
)
from tools import build_data

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = ROOT / "eval" / "questions" / "service-assurance.jsonl"


def _synthetic_uuid(index: int) -> str:
    """Construct a deterministic non-zero UUID at runtime without literal GUIDs in source."""
    return str(uuid.UUID(int=10_000 + index))


def _write_approval_file(
    path: Path,
    *,
    approved: bool = True,
    expires_delta_seconds: float = 3600.0,
    max_spend_usd: float = 5.0,
    max_seconds: float = 600.0,
    toolset_variant: str = "service_default",
) -> Path:
    now = datetime.now(timezone.utc)
    payload = {
        "approved": approved,
        "operation": "northstar-m4-live-eval",
        "expires_at": (now + timedelta(seconds=expires_delta_seconds)).isoformat(),
        "max_spend_usd": max_spend_usd,
        "max_seconds": max_seconds,
        "workspace_id": _synthetic_uuid(1),
        "ontology_item_id": _synthetic_uuid(2),
        "semantic_models": {
            "network": _synthetic_uuid(11),
            "enterprise": _synthetic_uuid(12),
            "finance": _synthetic_uuid(13),
            "care": _synthetic_uuid(14),
        },
        "foundry_responses_url": "https://synthetic.openai.azure.com/openai/v1/responses",
        "model_deployment": "gpt-5-mini",
        "input_cost_per_1m_usd": 0.25,
        "output_cost_per_1m_usd": 2.0,
        "toolset_variant": toolset_variant,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


@pytest.fixture(scope="module")
def sa_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("m4-sa-data") / "service-assurance"
    build_data.build("service-assurance", build_data.SEED, folder)
    return folder


@pytest.fixture(scope="module")
def questions() -> list[dict[str, Any]]:
    return load_questions(QUESTIONS_PATH)


def test_wilson_interval_bounds_and_edge_cases() -> None:
    empty = wilson_interval(0, 0)
    assert empty == {"proportion": 0.0, "low": 0.0, "high": 0.0}

    zero_hits = wilson_interval(0, 27)
    assert zero_hits["proportion"] == 0.0
    assert zero_hits["low"] == 0.0
    assert 0.10 < zero_hits["high"] < 0.16

    all_hits = wilson_interval(27, 27)
    assert all_hits["proportion"] == 1.0
    assert all_hits["high"] == 1.0
    assert 0.85 < all_hits["low"] < 0.90

    mid = wilson_interval(15, 30)
    assert mid["proportion"] == 0.5
    assert 0.32 < mid["low"] < mid["proportion"] < mid["high"] < 0.68

    with pytest.raises(ValueError):
        wilson_interval(5, 4)


def test_paired_bootstrap_interval_tie_and_clear_separation() -> None:
    tie_left = [1.0, 1.0, 0.0, 1.0, 0.0, 1.0]
    tie_right = [1.0, 1.0, 0.0, 1.0, 0.0, 1.0]
    tie_res = paired_bootstrap_interval(tie_left, tie_right, resamples=2000, seed=20261117)
    assert tie_res == {"diff": 0.0, "low": 0.0, "high": 0.0, "excludes_zero": False}

    win_left = [1.0] * 25 + [0.0, 0.0]
    win_right = [1.0] * 6 + [0.0] * 21
    win_res = paired_bootstrap_interval(win_left, win_right, resamples=5000, seed=20261117)
    assert win_res["diff"] == round(19 / 27, 6)
    assert win_res["low"] > 0.45
    assert win_res["high"] <= 1.0
    assert win_res["excludes_zero"] is True

    loss_res = paired_bootstrap_interval(win_right, win_left, resamples=5000, seed=20261117)
    assert loss_res["diff"] == round(-19 / 27, 6)
    assert loss_res["high"] < -0.45
    assert loss_res["excludes_zero"] is True


def test_score_run_exact_cents_partial_overlap_empty_cohort_and_traps(
    sa_data_dir: Path,
    questions: list[dict[str, Any]],
) -> None:
    q_by_id = {q["id"]: q for q in questions}
    hero = q_by_id["SA-L4-001"]
    oracle_hero = evaluate_question(hero, sa_data_dir)
    traps_hero = {t: evaluate_trap(t, hero, sa_data_dir) for t in hero["traps"]}

    # 1. Exact oracle match (including string-serialized key lookup)
    exact_run = {
        "arm": "ontology",
        "repeat": 1,
        "status": "PARTIAL",
        "rows": [
            {
                **row,
                "account_key": str(row["account_key"]),
            }
            for row in copy.deepcopy(oracle_hero["rows"])
        ],
        "exclusions": copy.deepcopy(oracle_hero["exclusions"]),
        "path": {
            "models_queried": ["network", "enterprise", "finance", "care"],
            "bridges_used": [{"from": "a", "to": "b", "on": ["account_key"], "crossesModels": True}],
            "validity_checks": ["incident_assignment_contract_utc_overlap"],
            "rules_applied": ["R1", "R2", "R3", "R4", "R5"],
        },
    }
    scored_exact = score_run(exact_run, hero, oracle_hero, traps_hero)
    assert scored_exact["answer_correct"] is True
    assert scored_exact["key_precision"] == 1.0
    assert scored_exact["key_recall"] == 1.0
    assert scored_exact["key_f1"] == 1.0
    assert scored_exact["traps_hit"] == []
    assert scored_exact["unknown_handling_correct"] is True
    assert scored_exact["path_correct"] is True

    # 2. Off-by-1 cent mismatch AND float approximation rejection
    off_by_one = copy.deepcopy(exact_run)
    off_by_one["rows"] = copy.deepcopy(oracle_hero["rows"])
    off_by_one["rows"][0]["credit_exposure_cents"] += 1
    scored_off = score_run(off_by_one, hero, oracle_hero, traps_hero)
    assert scored_off["answer_correct"] is False
    assert scored_off["key_f1"] == 1.0
    assert "numeric_value_mismatch" in scored_off["failure_reasons"]

    float_cell = copy.deepcopy(exact_run)
    float_cell["rows"] = copy.deepcopy(oracle_hero["rows"])
    float_cell["rows"][0]["credit_exposure_cents"] = float(
        float_cell["rows"][0]["credit_exposure_cents"]
    )
    assert score_run(float_cell, hero, oracle_hero, traps_hero)["answer_correct"] is False

    # 3. Partial key overlap precision/recall/F1
    first_two_oracle = oracle_hero["rows"][:2]
    fake_extra = {
        **copy.deepcopy(oracle_hero["rows"][0]),
        "account_key": 999999,
    }
    partial_run = {
        **copy.deepcopy(exact_run),
        "rows": [first_two_oracle[0], fake_extra],
    }
    scored_partial = score_run(partial_run, hero, oracle_hero, traps_hero)
    raw_prec = 1 / 2
    raw_rec = 1 / len(oracle_hero["rows"])
    expected_prec = round(raw_prec, 6)
    expected_rec = round(raw_rec, 6)
    expected_f1 = round(2 * raw_prec * raw_rec / (raw_prec + raw_rec), 6)
    assert scored_partial["answer_correct"] is False
    assert scored_partial["key_precision"] == expected_prec
    assert scored_partial["key_recall"] == expected_rec
    assert scored_partial["key_f1"] == expected_f1

    # 4. Empty cohort question SA-N-001 -> precision = recall = f1 = 1.0
    empty_q = q_by_id["SA-N-001"]
    oracle_empty = evaluate_question(empty_q, sa_data_dir)
    traps_empty = {t: evaluate_trap(t, empty_q, sa_data_dir) for t in empty_q["traps"]}
    empty_ok_run = {
        "arm": "ontology",
        "repeat": 1,
        "status": "PARTIAL",
        "rows": [],
        "exclusions": copy.deepcopy(oracle_empty["exclusions"]),
        "path": {
            "models_queried": list(empty_q["models"]),
            "bridges_used": [{"from": "n", "to": "e", "on": ["site_key"], "crossesModels": True}],
            "validity_checks": ["incident_assignment_contract_utc_overlap"],
        },
    }
    scored_empty = score_run(empty_ok_run, empty_q, oracle_empty, traps_empty)
    assert scored_empty["answer_correct"] is True
    assert scored_empty["key_precision"] == 1.0
    assert scored_empty["key_recall"] == 1.0
    assert scored_empty["key_f1"] == 1.0
    assert scored_empty["unknown_handling_correct"] is True

    # Missing exclusions when exclusionsExpected is True -> unknown_handling_correct False
    empty_no_excl = {**copy.deepcopy(empty_ok_run), "exclusions": []}
    assert score_run(empty_no_excl, empty_q, oracle_empty, traps_empty)["unknown_handling_correct"] is False

    # Null cell in oracle turned into 0 -> unknown_handling_correct False
    synthetic_null_oracle = {
        "rows": [{**copy.deepcopy(oracle_hero["rows"][0]), "credit_exposure_cents": None}],
        "exclusions": copy.deepcopy(oracle_hero["exclusions"]),
    }
    zero_substituted_run = {
        **copy.deepcopy(exact_run),
        "rows": [{**copy.deepcopy(oracle_hero["rows"][0]), "credit_exposure_cents": 0}],
    }
    scored_null_zero = score_run(zero_substituted_run, hero, synthetic_null_oracle, {})
    assert scored_null_zero["answer_correct"] is False
    assert scored_null_zero["unknown_handling_correct"] is False

    # 5. Known trap answer detection: site-key-only, revenue-fan-out, missing-as-zero
    for trap_name in ("site-key-only", "revenue-fan-out", "missing-as-zero"):
        trap_payload = traps_hero[trap_name]
        trap_run = {
            "arm": "baseline",
            "repeat": 1,
            "status": "COMPLETE",
            "rows": copy.deepcopy(trap_payload["rows"]),
            "exclusions": copy.deepcopy(trap_payload["exclusions"]),
            "path": {"models_queried": list(hero["models"]), "bridges_used": [], "validity_checks": []},
        }
        scored_trap = score_run(trap_run, hero, oracle_hero, traps_hero)
        assert scored_trap["answer_correct"] is False
        assert trap_name in scored_trap["traps_hit"]
        assert scored_trap["path_correct"] is False
        if trap_name == "missing-as-zero":
            assert scored_trap["unknown_handling_correct"] is False


def test_repeat_agreement_modal_share_and_unanimous_pass(
    questions: list[dict[str, Any]],
) -> None:
    l1_q = questions[0]
    row_a = [{"market_id": "M01", "major_incidents": 3}]
    row_b = [{"market_id": "M01", "major_incidents": 4}]
    repeats = [
        {"rows": row_a, "answer_correct": True},
        {"rows": row_a, "answer_correct": True},
        {"rows": row_a, "answer_correct": True},
        {"rows": row_b, "answer_correct": False},
        {"rows": row_b, "answer_correct": False},
    ]
    agr = repeat_agreement(repeats, l1_q)
    assert agr["modal_share"] == 0.6
    assert agr["pass_all_k"] == 0.0

    unanimous = [{"rows": row_a, "answer_correct": True} for _ in range(5)]
    agr_all = repeat_agreement(unanimous, l1_q)
    assert agr_all == {"modal_share": 1.0, "pass_all_k": 1.0}


def test_scripted_baseline_plus_rules_arm_prompt_token_overhead(
    sa_data_dir: Path,
    questions: list[dict[str, Any]],
) -> None:
    baseline_arm = get_offline_arm("baseline")
    plus_arm = get_offline_arm("baseline_plus_rules")
    ontology_arm = get_offline_arm("ontology")
    assert isinstance(plus_arm, ScriptedBaselinePlusRulesArm)

    hero = next(q for q in questions if q["id"] == "SA-L4-001")
    rec_a = baseline_arm.run_question(hero, sa_data_dir, repeat=1)
    rec_ap = plus_arm.run_question(hero, sa_data_dir, repeat=1)
    rec_b = ontology_arm.run_question(hero, sa_data_dir, repeat=1)

    assert rec_ap.rows == rec_b.rows
    assert all(call["server"] == "fabric_iq" for call in rec_ap.tool_calls)
    assert rec_ap.effort["input_tokens"] >= rec_a.effort["input_tokens"] + 950 * 4
    assert rec_ap.effort["input_tokens"] > rec_b.effort["input_tokens"]


def test_run_arms_offline_k3_resume_and_scorecard_end_to_end(
    sa_data_dir: Path,
    questions: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    out_jsonl = tmp_path / "offline-k3.jsonl"
    code, summary = run_arms.run_arms(
        questions_path=QUESTIONS_PATH,
        arms=("baseline", "baseline_plus_rules", "ontology"),
        repeats=3,
        mode="offline",
        profile="service-assurance",
        data_dir=sa_data_dir,
        output_path=out_jsonl,
    )
    assert code == 0
    expected_total = len(questions) * 3 * 3
    assert expected_total == 243
    assert summary["written_runs"] == expected_total
    lines = out_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == expected_total

    # Verify JSONL schema on first line
    sample = json.loads(lines[0])
    required_keys = {
        "question_id",
        "level",
        "persona",
        "arm",
        "repeat",
        "status",
        "rows",
        "exclusions",
        "path",
        "tool_calls",
        "tokens",
        "seconds",
        "cost_usd",
        "model_deployment",
        "toolset_variant",
        "timestamps",
    }
    assert required_keys <= sample.keys()
    assert sample["tokens"]["total_tokens"] == (
        sample["tokens"]["input_tokens"] + sample["tokens"]["output_tokens"]
    )

    # Test resume after interruption: keep first 10 lines with a sentinel timestamp
    resumed_jsonl = tmp_path / "resumed-k3.jsonl"
    sentinel_lines: list[str] = []
    for raw in lines[:10]:
        obj = json.loads(raw)
        obj["timestamps"]["started_at"] = "2026-11-17T00:00:00+00:00"
        sentinel_lines.append(json.dumps(obj, separators=(",", ":")))
    resumed_jsonl.write_text("\n".join(sentinel_lines) + "\n", encoding="utf-8", newline="\n")

    call_log: list[tuple[str, str, int]] = []

    def tracking_factory(arm_name: ArmName) -> Any:
        inner = get_offline_arm(arm_name)

        class _Wrapper:
            arm = inner.arm
            mode = inner.mode

            def run_question(
                self, question: dict[str, Any], data_dir: Path, *, repeat: int = 1
            ) -> Any:
                call_log.append((question["id"], arm_name, repeat))
                return inner.run_question(question, data_dir, repeat=repeat)

        return _Wrapper()

    res_code, res_summary = run_arms.run_arms(
        questions_path=QUESTIONS_PATH,
        arms=("baseline", "baseline_plus_rules", "ontology"),
        repeats=3,
        mode="offline",
        profile="service-assurance",
        data_dir=sa_data_dir,
        output_path=resumed_jsonl,
        provider_factory=tracking_factory,
    )
    assert res_code == 0
    assert res_summary["written_runs"] == expected_total - 10
    assert res_summary["completed_runs"] == expected_total
    assert len(call_log) == expected_total - 10

    first_ten_keys = {
        (json.loads(raw)["question_id"], json.loads(raw)["arm"], json.loads(raw)["repeat"])
        for raw in sentinel_lines
    }
    assert first_ten_keys.isdisjoint(set(call_log))

    after_lines = resumed_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(after_lines) == expected_total
    assert after_lines[:10] == sentinel_lines

    # Build scorecard.json and scorecard.html from the 243-line JSONL run
    sc_json = tmp_path / "scorecard.json"
    sc_html = tmp_path / "scorecard.html"
    cli_code = build_scorecard.main(
        [
            "--runs",
            str(resumed_jsonl),
            "--questions",
            str(QUESTIONS_PATH),
            "--profile",
            "service-assurance",
            "--data-dir",
            str(sa_data_dir),
            "--conditions-json",
            json.dumps({"capacity_sku": "F8", "region": "centralus"}),
            "--output-json",
            str(sc_json),
            "--output-html",
            str(sc_html),
        ]
    )
    assert cli_code == 0
    scorecard = json.loads(sc_json.read_text(encoding="utf-8"))
    assert scorecard["schema_version"] == "1.0"
    assert scorecard["mode"] == "RECORDED_SCORECARD"
    assert scorecard["conditions"]["execution_mode"] == "offline_scripted"
    assert scorecard["conditions"]["question_count"] == len(questions)
    assert scorecard["conditions"]["repeats"] == 3
    assert scorecard["conditions"]["arms"] == ["baseline", "baseline_plus_rules", "ontology"]
    assert scorecard["conditions"]["capacity_sku"] == "F8"
    assert scorecard["conditions"]["region"] == "centralus"

    assert set(scorecard["by_level"]) == set(VALID_LEVELS)
    assert len(scorecard["overall_differences"]) == 3
    ont_vs_base = next(
        d for d in scorecard["overall_differences"]
        if d["left_arm"] == "ontology" and d["right_arm"] == "baseline"
    )
    assert ont_vs_base["excludes_zero"] is True
    assert ont_vs_base["diff"] > 0.7

    # L1 control questions must be a tie across all arms
    for diff_entry in scorecard["by_level"]["L1"]["differences"]:
        assert diff_entry["diff"] == 0.0
        assert diff_entry["excludes_zero"] is False

    # Validate against Lane 3's RecordedScorecard contract in telco_iq.demo_library
    from telco_iq.demo_library import RecordedScorecard

    validated = RecordedScorecard.read(sc_json)
    assert validated.mode == "RECORDED_SCORECARD"
    assert set(validated.summary_by_arm) == {"baseline", "baseline_plus_rules", "ontology"}
    assert set(scorecard["summary_by_arm"]["baseline"]["traps_hit"]) == set(VALID_TRAPS)
    assert len(scorecard["failed_questions"]) > 0

    html_text = sc_html.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html_text
    assert "Arm B — Ontology-grounded" in html_text
    assert "Excludes zero" in html_text
    assert "SA-L4-001" in html_text


def test_run_arms_enforces_time_and_spend_ceilings(
    sa_data_dir: Path,
    tmp_path: Path,
) -> None:
    # 1. Tiny time ceiling stops cleanly and exits non-zero (code 2)
    time_out = tmp_path / "ceiling-time.jsonl"
    exit_time = run_arms.main(
        [
            "--mode",
            "offline",
            "--questions",
            str(QUESTIONS_PATH),
            "--data-dir",
            str(sa_data_dir),
            "--repeats",
            "2",
            "--max-seconds",
            "0.0001",
            "--output",
            str(time_out),
        ]
    )
    assert exit_time == 2

    # 2. Spend ceiling stops cleanly and exits non-zero (code 2)
    spend_out = tmp_path / "ceiling-spend.jsonl"
    code_spend, spend_summary = run_arms.run_arms(
        questions_path=QUESTIONS_PATH,
        arms=("baseline", "ontology"),
        repeats=2,
        mode="offline",
        data_dir=sa_data_dir,
        output_path=spend_out,
        max_spend_usd=0.0001,
        cost_calculator=lambda _rec: 0.00006,
    )
    assert code_spend == 2
    assert spend_summary["status"] == "CEILING_REACHED"
    assert spend_summary["reason"] == "max_spend_usd_reached"
    assert spend_summary["written_runs"] == 2


def test_live_dry_run_never_touches_network_and_missing_approval_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def _forbid_connect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Network socket connect is forbidden during dry-run and approval checks")

    monkeypatch.setattr(socket.socket, "connect", _forbid_connect)
    monkeypatch.setattr(socket, "create_connection", _forbid_connect)

    # 1. Live --dry-run without approval file prints call plan and exits 0
    dry_code = run_arms.main(
        [
            "--mode",
            "live",
            "--dry-run",
            "--questions",
            str(QUESTIONS_PATH),
            "--repeats",
            "5",
            "--max-spend-usd",
            "7.5",
            "--max-seconds",
            "900",
            "--output",
            str(tmp_path / "live-dry.jsonl"),
        ]
    )
    assert dry_code == 0
    captured = capsys.readouterr()
    plan = json.loads(captured.out)
    assert plan["status"] == "DRY_RUN"
    assert plan["mode"] == "live"
    assert plan["total_planned_runs"] == 27 * 3 * 5
    assert plan["max_spend_usd"] == 7.5
    assert plan["max_seconds"] == 900.0
    assert "iq_mcp_url" in plan["endpoints"]
    assert "ontology_mcp_url" in plan["endpoints"]

    # 2. Live without --approval fails closed before touching network
    no_app_code = run_arms.main(
        [
            "--mode",
            "live",
            "--questions",
            str(QUESTIONS_PATH),
            "--output",
            str(tmp_path / "live-fail.jsonl"),
        ]
    )
    assert no_app_code != 0

    # 3. Approval path inside repository ROOT is rejected
    inside_repo = ROOT / ".local" / "test-illegal-approval.json"
    with pytest.raises(LiveApprovalError, match="outside the repository root"):
        LiveEvalApproval.from_file(inside_repo, root=ROOT)

    # 4. Unapproved or expired approval files fail closed
    unapproved_file = _write_approval_file(tmp_path / "unapproved.json", approved=False)
    with pytest.raises(LiveApprovalError, match="approved"):
        LiveEvalApproval.from_file(unapproved_file, root=ROOT)

    expired_file = _write_approval_file(
        tmp_path / "expired.json", approved=True, expires_delta_seconds=-10.0
    )
    with pytest.raises(LiveApprovalError, match="expired"):
        LiveEvalApproval.from_file(expired_file, root=ROOT)


def test_live_agent_arm_instructions_tools_sse_and_self_correction(
    questions: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    east_q = next(q for q in questions if q["id"] == "SA-P-001")
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    inst_a = build_live_system_instructions("baseline", east_q)
    inst_ap = build_live_system_instructions("baseline_plus_rules", east_q)
    inst_b = build_live_system_instructions("ontology", east_q)

    assert 'market_id = "M01"' in inst_a
    for rule in contract["rules"]:
        assert rule["statement"] not in inst_a
        assert rule["statement"] in inst_ap
        assert rule["statement"] not in inst_b

    assert [t["name"] for t in build_live_tools("baseline")] == [
        "get_semantic_model_schema",
        "execute_dax_query",
    ]
    assert [t["name"] for t in build_live_tools("baseline_plus_rules")] == [
        "get_semantic_model_schema",
        "execute_dax_query",
    ]
    assert [t["name"] for t in build_live_tools("ontology")] == [
        "get_semantic_model_schema",
        "execute_dax_query",
        "list_ontology_entities",
        "list_ontology_rules",
        "get_ontology_relationships_and_metrics",
    ]

    # Verify SSE parser handles multi-event SSE responses cleanly
    sse_body = (
        ": keepalive\n"
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
        'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\\"ok\\":true}"}]}}\n\n'
    )
    sse_resp = httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        text=sse_body,
    )
    parsed_sse = parse_mcp_http_response(sse_resp)
    assert parsed_sse["id"] == 2
    assert "result" in parsed_sse

    # Full LiveAgentArm loop with MockTransport: ontology tools + DAX syntax error + self-correction
    approval_file = _write_approval_file(
        tmp_path / "live-approval.json",
        toolset_variant="Fabric.Routing.FabricIQ.V2",
    )
    approval = LiveEvalApproval.from_file(approval_file, root=ROOT)
    assert set(approval.semantic_models) == set(VALID_MODELS)

    responses_turn = 0
    seen_iq_variants: list[str | None] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal responses_turn
        url = str(request.url)
        body = json.loads(request.content.decode("utf-8"))

        if url.endswith("/ontologyEndpoint"):
            method = body.get("method")
            if method == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "ont-sess-1"},
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
                )
            if method == "notifications/initialized":
                return httpx.Response(202, text="")
            tool_name = body["params"]["name"]
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=(
                    f'data: {{"jsonrpc":"2.0","id":{body["id"]},'
                    f'"result":{{"content":[{{"type":"text","text":"{{\\"tool\\":\\"{tool_name}\\"}}"}}]}}}}\n\n'
                ),
            )

        if url.endswith("/mcp/fabriciq"):
            seen_iq_variants.append(request.headers.get("X-Variants"))
            method = body.get("method")
            if method == "initialize":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-11-25"}},
                )
            if method == "notifications/initialized":
                return httpx.Response(202, text="")
            dax = body["params"]["arguments"]["daxQueries"][0]
            if "BAD_SYNTAX" in dax:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": "DAX syntax error near BAD_SYNTAX"}],
                        },
                    },
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=(
                    f'data: {{"jsonrpc":"2.0","id":{body["id"]},'
                    '"result":{"content":[{"type":"text","text":"{\\"rows\\":[[\\"M01\\",2]]}"}]}}\n\n'
                ),
            )

        if "/responses" in url:
            responses_turn += 1
            if responses_turn == 1:
                return httpx.Response(
                    200,
                    json={
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "c1",
                                "name": "list_ontology_entities",
                                "arguments": "{}",
                            },
                            {
                                "type": "function_call",
                                "call_id": "c2",
                                "name": "list_ontology_rules",
                                "arguments": "{}",
                            },
                            {
                                "type": "function_call",
                                "call_id": "c3",
                                "name": "execute_dax_query",
                                "arguments": json.dumps(
                                    {"model": "network", "dax_query": "EVALUATE BAD_SYNTAX"}
                                ),
                            },
                        ],
                        "usage": {"input_tokens": 500, "output_tokens": 90},
                    },
                )
            if responses_turn == 2:
                return httpx.Response(
                    200,
                    json={
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "c4",
                                "name": "execute_dax_query",
                                "arguments": json.dumps(
                                    {"model": "network", "dax_query": "EVALUATE 'Incidents'"}
                                ),
                            }
                        ],
                        "usage": {"input_tokens": 620, "output_tokens": 70},
                    },
                )
            final_json = json.dumps(
                {
                    "rows": [{"market_id": "M01", "major_incidents": "2"}],
                    "exclusions": [],
                    "bridges_used": [],
                    "validity_checks": [],
                    "rules_applied": [],
                }
            )
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": final_json}],
                        }
                    ],
                    "usage": {"input_tokens": 700, "output_tokens": 60},
                },
            )

        raise AssertionError(f"Unexpected URL: {url}")

    l1_q = questions[0]
    with httpx.Client(transport=httpx.MockTransport(mock_handler)) as mock_client:
        live_arm = LiveAgentArm(
            "ontology",
            approval,
            http_client=mock_client,
            token_provider=lambda _res: "synthetic-test-token",
        )
        record = live_arm.run_question(l1_q, repeat=1)

    assert record.status == "COMPLETE"
    assert record.model_deployment == "gpt-5-mini"
    assert record.toolset_variant == "Fabric.Routing.FabricIQ.V2"
    assert record.effort["input_tokens"] == 500 + 620 + 700
    assert record.effort["output_tokens"] == 90 + 70 + 60
    assert record.effort["tool_call_count"] == 4
    assert [c["tool"] for c in record.tool_calls] == [
        "list_ontology_entities",
        "list_ontology_rules",
        "ExecuteQuery",
        "ExecuteQuery",
    ]
    assert all(v == "Fabric.Routing.FabricIQ.V2" for v in seen_iq_variants)
