"""Local eight-model preparation drafts; no source authoring or model calls."""

from __future__ import annotations

import hashlib
import json
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
