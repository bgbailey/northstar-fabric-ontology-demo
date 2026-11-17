"""Local eight-model preparation drafts; no source authoring or model calls."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from telco_iq.catalog import Catalog
from tools import build_ai_prep as prep

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "contracts" / "catalog.json"
SLA = ROOT / "fabric" / "ai-prep" / "sla-model-guidance.json"


def test_all_eight_ai_drafts_bind_all_64_measures_to_exact_source_contracts():
    packet = prep.build_packet(CATALOG, SLA)
    catalog = Catalog(CATALOG)
    assert packet["status"] == "DRAFT_NOT_APPLIED"
    assert len(packet["models"]) == len(catalog.models) == 8
    assert {model["id"] for model in packet["models"]} == set(catalog.models)
    bindings = [metric for model in packet["models"] for metric in model["sourceMetricBindings"]]
    assert len(bindings) == 64
    assert {binding["metricId"] for binding in bindings} == set(catalog.metrics)
    assert packet["sourceCatalogRevision"] == catalog.revision
    for model in packet["models"]:
        assert model["status"] == "DRAFT_NOT_APPLIED"
        assert model["purpose"] and model["proposedVocabulary"]
        assert len(model["sourceMetricBindings"]) == 8
        assert model["verifiedAnswers"] == []
        assert 0 < len("\n".join(model["instructions"])) <= 10_000
        for term in model["proposedVocabulary"]:
            assert term["metricId"] in {binding["metricId"] for binding in model["sourceMetricBindings"]}
            assert term["object"] in {binding["object"] for binding in model["sourceMetricBindings"]}
        for binding in model["sourceMetricBindings"]:
            metric = catalog.metrics[binding["metricId"]]
            table = catalog.tables[metric["table"]]
            assert binding["sourceModel"] == metric["model"] == model["id"]
            assert binding["homeTable"] == table["displayName"]
            assert binding["measureName"] == metric["name"]
            assert binding["object"] == prep.metric_reference(catalog, metric)
            assert binding["sourceGrain"] == table["grain"]
            assert binding["dateRole"] == metric["dateColumn"]
            assert binding["allowedDimensions"] == metric["allowedDimensions"]
            assert binding["unit"] == metric["unit"]
            assert binding["aggregation"] == metric["aggregation"]
            assert "dax" not in binding and "sql" not in binding


def test_ai_drafts_are_deterministic_and_keep_the_original_sla_file_and_model_assets_unchanged():
    before = SLA.read_bytes()
    models = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (ROOT / "fabric" / "semantic-models").rglob("*") if path.is_file()
    }
    first = prep.build_packet(CATALOG, SLA)
    assert prep.build_packet(CATALOG, SLA) == first
    generated = ROOT / "fabric" / "ai-prep" / "model-guidance.json"
    assert generated.read_text(encoding="utf-8") == json.dumps(first, indent=2) + "\n"
    assert SLA.read_bytes() == before
    assert first["sourceSlaGuidanceSha256"] == hashlib.sha256(before).hexdigest()
    assert first["generatorSha256"] == hashlib.sha256(Path(prep.__file__).read_bytes()).hexdigest()
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == pin for path, pin in models.items())


def test_ai_draft_fairness_does_not_promote_scenario_defaults_or_change_visibility():
    packet = prep.build_packet(CATALOG, SLA)
    assert packet["comparisonArms"]["flat"] == packet["comparisonArms"]["native"]
    assert packet["aiSchemaReview"]["automaticVisibilityChanges"] is False
    assert packet["scenarioGuidance"]["sla"] == json.loads(SLA.read_bytes())["sharedCrossDomainFacts"]
    assert all("July is the default" not in instruction
               for model in packet["models"] for instruction in model["instructions"])
    assert packet["evidenceGaps"]


@pytest.mark.parametrize("model_id", ["subscriptions", "digital", "care", "commerce", "growth"])
def test_new_ai_drafts_use_scoped_metric_vocabulary_and_qualified_measure_names(model_id):
    packet = prep.build_packet(CATALOG, SLA)
    model = next(model for model in packet["models"] if model["id"] == model_id)
    assert model["proposedVocabulary"]
    for term in model["proposedVocabulary"]:
        assert term["metricId"].startswith(model_id + ".")
        assert term["object"] in {binding["object"] for binding in model["sourceMetricBindings"]}
    assert all(not prep.BRACKET.search(instruction) for instruction in model["instructions"])


@pytest.mark.parametrize("text", [
    "Use 'Sessions'[invented_column].",
    "Use 'Credits'[amount_cents].",
    "Use [Invented Measure].",
])
def test_unknown_and_out_of_source_ai_object_references_fail_explicitly(monkeypatch, text):
    draft = deepcopy(prep.EXTRA["digital"])
    draft["instructions"] = [text]
    monkeypatch.setitem(prep.EXTRA, "digital", draft)
    with pytest.raises(ValueError, match="unknown/out-of-model|unresolved"):
        prep.build_packet(CATALOG, SLA)


def test_vocabulary_cannot_cross_model_scope(monkeypatch):
    draft = deepcopy(prep.EXTRA["subscriptions"])
    draft["vocabulary"]["revenue"] = "finance.recognized_service_revenue"
    monkeypatch.setitem(prep.EXTRA, "subscriptions", draft)
    with pytest.raises(ValueError, match="crosses source-model scope"):
        prep.build_packet(CATALOG, SLA)


def test_ai_instruction_character_limit_includes_shared_and_qualified_text(monkeypatch):
    draft = deepcopy(prep.EXTRA["subscriptions"])
    draft["instructions"] = ["x" * 10_000]
    monkeypatch.setitem(prep.EXTRA, "subscriptions", draft)
    with pytest.raises(ValueError, match="10000-character limit"):
        prep.build_packet(CATALOG, SLA)


@pytest.mark.parametrize("characters", [10_000, 10_001])
def test_ai_instruction_exact_documented_limit(monkeypatch, characters):
    draft = deepcopy(prep.EXTRA["subscriptions"])
    draft["instructions"] = ["x" * (characters - len("\n".join(prep.COMMON)) - 1)]
    monkeypatch.setitem(prep.EXTRA, "subscriptions", draft)
    if characters > 10_000:
        with pytest.raises(ValueError, match="10000-character limit"):
            prep.build_packet(CATALOG, SLA)
    else:
        packet = prep.build_packet(CATALOG, SLA)
        model = next(entry for entry in packet["models"] if entry["id"] == "subscriptions")
        assert len("\n".join(model["instructions"])) == 10_000


def test_source_measure_name_ambiguity_fails_before_qualifying_draft_text(tmp_path):
    catalog = json.loads(CATALOG.read_bytes())
    duplicate = deepcopy(catalog["metrics"][0])
    duplicate["id"] += "_duplicate_name"
    catalog["metrics"].append(duplicate)
    modified = tmp_path / "catalog.json"
    modified.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(ValueError, match="Ambiguous source measure names"):
        prep.build_packet(modified, SLA)


def test_inherited_description_cannot_target_another_source(tmp_path):
    packet = json.loads(SLA.read_bytes())
    packet["models"][0]["recommendedDescriptions"][0]["object"] = "'Credits'[amount_cents]"
    modified = tmp_path / "sla.json"
    modified.write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(ValueError, match="description target is outside"):
        prep.build_packet(CATALOG, modified)


@pytest.mark.parametrize("change", ["status", "duplicate_source", "verified_answer"])
def test_unreviewed_inherited_ai_state_is_not_silently_repackaged(tmp_path, change):
    packet = json.loads(SLA.read_bytes())
    if change == "status":
        packet["status"] = "APPLIED"
    elif change == "duplicate_source":
        packet["models"].append(deepcopy(packet["models"][0]))
    else:
        packet["models"][0]["verifiedAnswers"] = [{"answer": "UNAPPROVED_TEST_ONLY"}]
    modified = tmp_path / "unreviewed-sla.json"
    modified.write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(ValueError, match="unapplied|ambiguous|verified answers"):
        prep.build_packet(CATALOG, modified)


def test_unknown_source_model_needs_a_reviewed_ai_draft(tmp_path):
    catalog = json.loads(CATALOG.read_bytes())
    catalog["models"].append({"id": "unreviewed", "name": "Unreviewed", "tables": []})
    modified = tmp_path / "catalog.json"
    modified.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(ValueError, match="exact source-model catalog"):
        prep.build_packet(modified, SLA)


@pytest.mark.parametrize("parts", [
    ("test.SemanticModel", "draft.json"),
    ("Copilot", "settings.json"),
    ("draft.md",),
])
def test_cli_cannot_write_persisted_model_artifacts(monkeypatch, tmp_path, parts):
    output = tmp_path.joinpath(*parts)
    monkeypatch.setattr(sys, "argv", ["build_ai_prep", "--output", str(output)])
    with pytest.raises(ValueError, match="outside SemanticModel/Copilot"):
        prep.main()
    assert not output.exists()
    if len(parts) > 1:
        assert not output.parent.exists()


@pytest.mark.parametrize("source", ["catalog", "sla-guidance"])
def test_cli_cannot_overwrite_either_input(monkeypatch, tmp_path, source):
    original = CATALOG if source == "catalog" else SLA
    copied = tmp_path / "input.json"
    copied.write_bytes(original.read_bytes())
    before = copied.read_bytes()
    monkeypatch.setattr(sys, "argv", ["build_ai_prep", f"--{source}", str(copied), "--output", str(copied)])
    with pytest.raises(ValueError, match="must not replace"):
        prep.main()
    assert copied.read_bytes() == before


def test_cli_writes_only_the_requested_review_packet(monkeypatch, tmp_path, capsys):
    output = tmp_path / "review" / "model-guidance.json"
    monkeypatch.setattr(sys, "argv", ["build_ai_prep", "--output", str(output)])
    prep.main()
    assert json.loads(output.read_bytes()) == prep.build_packet(CATALOG, SLA)
    report = json.loads(capsys.readouterr().out)
    assert report == {"status": "DRAFT_NOT_APPLIED", "models": 8, "sourceMeasures": 64, "deployed_or_applied": False}


@pytest.mark.parametrize("model_id", ["network", "enterprise", "finance", "care"])
def test_hero_models_tmdl_every_table_measure_and_visible_column_has_concise_description(model_id):
    tables_dir = ROOT / "fabric" / "semantic-models" / f"{model_id}.SemanticModel" / "definition" / "tables"
    tmdl_files = sorted(tables_dir.glob("*.tmdl"))
    assert tmdl_files
    measure_count = 0
    visible_column_count = 0

    for tmdl_path in tmdl_files:
        lines = tmdl_path.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("/// ")
        table_desc = lines[0][4:].strip()
        assert 0 < len(table_desc) <= 200, f"Invalid table description in {tmdl_path}"
        assert lines[1].startswith("table '")

        for idx, line in enumerate(lines):
            if line.startswith("\tmeasure '"):
                measure_count += 1
                assert idx > 0 and lines[idx - 1].startswith("\t/// "), (
                    f"Missing measure description in {tmdl_path}:{idx + 1}"
                )
                desc = lines[idx - 1][5:].strip()
                assert 0 < len(desc) <= 200, f"Measure description >200 chars in {tmdl_path}:{idx}"
            elif line.startswith("\tcolumn '"):
                assert idx > 0 and lines[idx - 1].startswith("\t/// "), (
                    f"Missing column description in {tmdl_path}:{idx + 1}"
                )
                desc = lines[idx - 1][5:].strip()
                assert 0 < len(desc) <= 200, f"Column description >200 chars in {tmdl_path}:{idx}"
                block_end = next(
                    (
                        j
                        for j in range(idx + 1, len(lines))
                        if lines[j].startswith(("\tcolumn ", "\tmeasure ", "\tpartition "))
                    ),
                    len(lines),
                )
                is_hidden = "\t\tisHidden" in lines[idx + 1 : block_end]
                if not is_hidden:
                    visible_column_count += 1
                    assert "at the declared " not in desc, (
                        f"Generic placeholder description on visible column in {tmdl_path}:{idx + 1}"
                    )

    assert measure_count == 8
    assert visible_column_count > 0


def test_no_ai_instruction_mentions_another_model():
    catalog = Catalog(CATALOG)
    all_models = catalog.raw["models"]
    sla_packet = json.loads(SLA.read_text(encoding="utf-8"))
    guidance_packet = json.loads((ROOT / "fabric" / "ai-prep" / "model-guidance.json").read_text(encoding="utf-8"))
    sa_packet = json.loads(
        (ROOT / "fabric" / "ai-prep" / "service-assurance-ai-prep.json").read_text(encoding="utf-8")
    )

    for source_models, key in (
        (sla_packet["models"], "instructions"),
        (guidance_packet["models"], "instructions"),
        (sa_packet["models"], "aiInstructions"),
    ):
        for entry in source_models:
            model_id = entry["id"]
            other_terms = [
                term
                for other in all_models
                if other["id"] != model_id
                for term in (other["id"], other["name"])
            ]
            for instruction in entry[key]:
                for term in other_terms:
                    assert not re.search(rf"\b{re.escape(term)}\b", instruction, re.IGNORECASE), (
                        f"Model '{model_id}' instruction mentions '{term}': {instruction}"
                    )


@pytest.mark.parametrize(
    ("source_kind", "model_id", "bad_instruction"),
    [
        ("extra", "care", "Keep one Finance result per 'Cases'[account_key] and month."),
        ("extra", "digital", "Compare 'Sessions'[Sessions] against Commerce demand."),
        ("sla", "network", "Ask whether outage impact means affected enterprise services."),
        ("sla", "enterprise", "Compare 'Service Month'[Contracted Recurring Revenue] with Finance revenue."),
        ("sla", "finance", "Escalate billing adjustments to Customer Care."),
    ],
)
def test_cross_model_mention_in_ai_instruction_raises_value_error(
    monkeypatch, tmp_path, source_kind, model_id, bad_instruction
):
    if source_kind == "extra":
        draft = deepcopy(prep.EXTRA[model_id])
        draft["instructions"] = [bad_instruction]
        monkeypatch.setitem(prep.EXTRA, model_id, draft)
        with pytest.raises(ValueError, match="subject-area boundary"):
            prep.build_packet(CATALOG, SLA)
    else:
        sla_copy = json.loads(SLA.read_text(encoding="utf-8"))
        target = next(entry for entry in sla_copy["models"] if entry["id"] == model_id)
        target["instructions"] = [bad_instruction]
        sla_path = tmp_path / "sla-cross-model.json"
        sla_path.write_text(json.dumps(sla_copy), encoding="utf-8")
        with pytest.raises(ValueError, match="subject-area boundary"):
            prep.build_packet(CATALOG, sla_path)
        with pytest.raises(ValueError, match="subject-area boundary"):
            prep.build_service_assurance_prep(CATALOG, sla_path)


def test_service_assurance_ai_prep_is_deterministic_hides_technical_columns_and_covers_arms_equally():
    sa_path = ROOT / "fabric" / "ai-prep" / "service-assurance-ai-prep.json"
    first = prep.build_service_assurance_prep(CATALOG, SLA)
    assert prep.build_service_assurance_prep(CATALOG, SLA) == first
    assert sa_path.read_text(encoding="utf-8") == json.dumps(first, indent=2) + "\n"

    assert first["status"] == "DRAFT_READY_FOR_PREP_DATA_FOR_AI"
    assert first["liveApplyTodo"].startswith("TODO(verify-live):")
    assert first["evaluationArms"]["equalPreparation"] is True
    arms = first["evaluationArms"]["arms"]
    assert set(arms) == {"Arm A", "Arm A+", "Arm B"}
    assert len(set(arms.values())) == 1
    assert [m["id"] for m in first["models"]] == ["network", "enterprise", "finance", "care"]

    catalog = Catalog(CATALOG)
    for entry in first["models"]:
        model_id = entry["id"]
        assert 0 < len("\n".join(entry["aiInstructions"])) <= 10_000
        for group in ("tables", "columns", "measures"):
            descriptions = entry["descriptions"][group]
            assert descriptions
            assert all(0 < len(desc) <= 200 for desc in descriptions.values())

        schema = entry["aiDataSchema"]
        assert schema["editingRoute"] == "USER_CONFIGURED_PREP_DATA_FOR_AI"
        assert schema["excludedDiagnosticColumns"] == ["coverage_complete", "expected_minutes"]
        assert len(schema["includedMeasures"]) == 8
        for table_id in catalog.models[model_id]["tables"]:
            table_spec = catalog.tables[table_id]
            display_name = table_spec["displayName"]
            visible = set(schema["visibleColumnsByTable"][display_name])
            hidden = set(schema["hiddenTechnicalColumnsByTable"][display_name])
            assert visible | hidden == set(table_spec["columns"])
            assert not (visible & hidden)
            for col_name, col_kind in table_spec["columns"].items():
                if (
                    col_name.endswith(("_key", "_id"))
                    or col_name in {"coverage_complete", "expected_minutes"}
                    or col_kind == "timestamp"
                    or (col_kind == "int64" and table_id != "calendar_date")
                ):
                    assert col_name in hidden and col_name not in visible
                else:
                    assert col_name in visible and col_name not in hidden

        candidates = entry["verifiedAnswerCandidates"]
        assert len(candidates) >= 2
        visible_refs = {
            f"'{t_name.replace(chr(39), chr(39) * 2)}'[{col}]"
            for t_name, cols in schema["visibleColumnsByTable"].items()
            for col in cols
        }
        for candidate in candidates:
            assert candidate["level"] == "L1"
            assert candidate["editingRoute"] == "USER_CONFIGURED_PREP_DATA_FOR_AI"
            assert candidate["question"] and candidate["triggerPhrases"] and candidate["visualType"]
            assert candidate["targetMeasure"] in schema["includedMeasures"]
            assert set(candidate["groupBy"]) <= visible_refs


def test_cli_writes_both_packets_when_service_assurance_output_is_given(monkeypatch, tmp_path):
    out_all = tmp_path / "review" / "model-guidance.json"
    out_sa = tmp_path / "review" / "service-assurance-ai-prep.json"
    monkeypatch.setattr(
        sys, "argv", ["build_ai_prep", "--output", str(out_all), "--service-assurance-output", str(out_sa)]
    )
    prep.main()
    assert json.loads(out_all.read_bytes()) == prep.build_packet(CATALOG, SLA)
    assert json.loads(out_sa.read_bytes()) == prep.build_service_assurance_prep(CATALOG, SLA)

