"""Synthetic copies of the observed entity-only formal Rule shape; no private source IDs or network."""

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from test_native_context import change_part, synthetic_capture
from test_reader_native_context import reader_case

from telco_iq.catalog import digest
from telco_iq.contracts import Budgets
from telco_iq.errors import LabError
from telco_iq.native_context import native_rules_fingerprint, normalize_native_capture
from telco_iq.planner import build_planner_payload
from telco_iq.reader_native_context import acquire_reader_context


def raw_rules(receipt):
    return receipt["rawNativeContext"]["list_ontology_rules"]["structuredContent"]["values"]


def test_actual_shape_is_formal_rule_evidence_not_annotation_or_calculation(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path, formal_rules=True)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    snapshot.verify(snapshot.context.scope)
    assert [rule["id"] for rule in snapshot.context.rules] == ["SyntheticPeriod", "SyntheticUnknown"]
    assert snapshot.annotation_guidance
    assert all(rule["native_part"].startswith("rules/") for rule in snapshot.context.rules)
    assert all(rule["property_scope"] == "None" for rule in snapshot.context.rules)
    assert all(rule["referenced_relationships"] == "[]" for rule in snapshot.context.rules)
    assert all(set(json.loads(rule["referenced_entities"])) <= {entity["id"] for entity in snapshot.entities}
               for rule in snapshot.context.rules)
    assert all(metric["calculation_owner"] == "source_semantic_model" for metric in snapshot.metrics)
    assert "not execution" in " ".join(snapshot.context.provenance)
    assert snapshot.reader_verified is False


def test_rule_and_named_entity_reference_order_is_not_semantic_drift(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path, formal_rules=True)
    before = normalize_native_capture(definition, receipt, **kwargs)
    original_hash = native_rules_fingerprint(raw_rules(receipt))
    raw_rules(receipt).reverse()
    for rule in raw_rules(receipt):
        rule["referencedEntities"].reverse()
    assert native_rules_fingerprint(raw_rules(receipt)) == original_hash
    after = normalize_native_capture(definition, receipt, **kwargs)
    assert after.context.rules == before.context.rules


@pytest.mark.parametrize("change", [
    "statement", "lineage", "extra_field", "missing_field", "unresolved_entity", "duplicate_entity",
    "properties_all", "properties_selected", "properties_null", "relationship_ref", "annotation",
    "synonym", "duplicate_rule", "duplicate_lineage", "empty_statement", "oversize_statement",
])
def test_unknown_changed_or_ambiguous_rule_scope_fails_closed(tmp_path, change):
    definition, receipt, kwargs = synthetic_capture(tmp_path, formal_rules=True)
    values = raw_rules(receipt)
    rule = values[0]
    if change == "statement":
        rule["statement"] += " Changed."
    elif change == "lineage":
        rule["lineageTag"] = "SYNTHETIC_WRONG_LINEAGE"
    elif change == "extra_field":
        rule["expression"] = "NOT_SOURCE_ARITHMETIC"
    elif change == "missing_field":
        del rule["annotations"]
    elif change == "unresolved_entity":
        rule["referencedEntities"][0]["entity"] = "SYNTHETIC_UNOBSERVED"
    elif change == "duplicate_entity":
        rule["referencedEntities"].append(deepcopy(rule["referencedEntities"][0]))
    elif change.startswith("properties_"):
        rule["referencedEntities"][0]["propertyScope"] = {
            "properties_all": "All", "properties_selected": "Selected", "properties_null": None,
        }[change]
    elif change == "relationship_ref":
        rule["referencedRelationships"] = ["SYNTHETIC_UNREVIEWED_EDGE"]
    elif change == "annotation":
        rule["annotations"] = [{"name": "SYNTHETIC", "value": "Unknown"}]
    elif change == "synonym":
        rule["synonyms"] = ["SYNTHETIC_UNKNOWN"]
    elif change == "duplicate_rule":
        values.append(deepcopy(rule))
    elif change == "duplicate_lineage":
        values[1]["lineageTag"] = rule["lineageTag"]
    elif change == "empty_statement":
        rule["statement"] = ""
    else:
        rule["statement"] = "x" * 4001
    with pytest.raises(LabError):
        normalize_native_capture(definition, receipt, **kwargs)


@pytest.mark.parametrize("change", ["missing_part", "extra_part", "header", "statement", "reference", "property_scope", "expression"])
def test_definition_and_mcp_formal_rules_must_match_exactly(tmp_path, change):
    definition, receipt, kwargs = synthetic_capture(tmp_path, formal_rules=True)
    if change == "missing_part":
        definition["definition"]["parts"] = [
            part for part in definition["definition"]["parts"] if part["path"] != "rules/SyntheticPeriod.tmdl"
        ]
        receipt["definitionReceipt"] = digest(definition) + ".json"
    elif change == "extra_part":
        raw_rules(receipt).pop(0)
    else:
        def mutate(text):
            if change == "header":
                return text.replace("rule SyntheticPeriod", "rule SYNTHETIC_OTHER")
            if change == "statement":
                return text.replace("Synthetic period guidance", "Changed period guidance")
            if change == "reference":
                return text.replace("'subscriptions subscriptions_fact'", "'SYNTHETIC_UNOBSERVED'")
            if change == "property_scope":
                return text + "\t\tpropertyScope: All\n"
            return text + "\texpression: NOT_AUTHORIZED\n"
        change_part(definition, receipt, "rules/SyntheticPeriod.tmdl", mutate)
    with pytest.raises(LabError):
        normalize_native_capture(definition, receipt, **kwargs)


def test_missing_redundant_entity_source_annotation_uses_verified_backing_identity(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path, formal_rules=True)
    entities = receipt["rawNativeContext"]["list_ontology_entities"]["structuredContent"]["values"]
    for entity in entities:
        annotation = next(value for value in entity["annotations"] if value["name"] == "source_model")
        entity["annotations"].remove(annotation)
        change_part(
            definition, receipt, f"entities/{entity['name']}.tmdl",
            lambda text, value=annotation["value"]: text.replace(f"\tannotation source_model = {value}\n", ""),
        )
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    assert {entity["model"] for entity in snapshot.entities} == {"subscriptions", "finance"}
    assert len(snapshot.context.rules) == 2
    assert all(metric["calculation_owner"] == "source_semantic_model" for metric in snapshot.metrics)


async def test_current_reader_rules_are_verified_and_both_arms_receive_identical_facts(tmp_path):
    snapshot, binding, inputs, state, catalog = reader_case(tmp_path, formal_rules=True)
    original = inputs["probe"]
    async def reordered(actual):
        receipt = await original(actual)
        receipt.native_rules.reverse()
        for rule in receipt.native_rules:
            rule["referencedEntities"].reverse()
        return receipt
    inputs["probe"] = reordered
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    assert len(state["calls"]) == 1 and len(bundle.context.rules) == 2
    bundle.verify(binding.scope)
    flat, linked = [build_planner_payload(
        "Synthetic", binding.scope, bundle.context, catalog, input_arm=arm, native_snapshot=bundle,
        run_id="SYNTHETIC_RULE_RUN", as_of=datetime.now(timezone.utc), budgets=Budgets(),
    ) for arm in ("baseline", "native")]
    assert flat["facts_hash"] == linked["facts_hash"]
    assert flat["metadata"] == linked["metadata"]
    assert snapshot.reader_verified is False


@pytest.mark.parametrize("change", ["statement", "scope", "reference", "removed"])
async def test_reader_rule_values_cannot_drift_or_disappear(tmp_path, change):
    snapshot, binding, inputs, _, _ = reader_case(tmp_path, formal_rules=True)
    original = inputs["probe"]
    async def wrong(actual):
        receipt = await original(actual)
        if change == "statement":
            receipt.native_rules[0]["statement"] += " Drifted."
        elif change == "scope":
            receipt.native_rules[0]["referencedEntities"][0]["propertyScope"] = "All"
        elif change == "reference":
            receipt.native_rules[0]["referencedEntities"][0]["entity"] = "SYNTHETIC_OTHER"
        else:
            receipt.native_rules.clear()
        return receipt
    inputs["probe"] = wrong
    with pytest.raises(LabError):
        await acquire_reader_context(snapshot, binding, **inputs)
