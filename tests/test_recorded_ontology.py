"""Synthetic display metadata only, not a native service availability test."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from telco_iq.recorded_ontology import RecordedOntology


def packet():
    return {
        "captured_at": "2026-10-20T12:00:00Z", "native_snapshot_hash": "a" * 64,
        "provenance": "ACTUAL_NATIVE_DEFINITION_AND_METADATA_CAPTURE", "sources": ["finance", "care"],
        "entities": [
            {"id": "Finance", "model": "finance", "table": "account", "name": "Accounts",
             "description": "UNIT ONLY", "keys": ["account_key"]},
            {"id": "Care", "model": "care", "table": "account", "name": "Accounts",
             "description": "UNIT ONLY", "keys": ["account_key"]},
        ],
        "relationships": [{"id": "bridge", "source": "Finance", "target": "Care", "description": "UNIT ONLY"}],
        "metrics": [{"id": "finance.amount", "model": "finance", "table": "account", "entity": "Finance",
                     "name": "Amount", "description": "UNIT ONLY", "representation": "EXTERNALMEASURE"}],
        "rules": [{"id": "Scope", "statement": "UNIT ONLY", "referenced_entities": ["Finance"],
                   "property_scope": "None"}],
        "execution_relationships": {"reviewed-contract": "bridge"},
        "limitations": ["UNIT ONLY, not a materialized graph or current reader grant."],
    }


def test_native_display_keeps_captured_source_home_and_entity_only_rules():
    value = RecordedOntology.model_validate(packet())
    assert value.metrics[0].model == "finance"
    assert value.rules[0].property_scope == "None"
    assert "principal_scope" not in value.model_dump_json()


@pytest.mark.parametrize("change", ["wrong-home", "edge", "rule", "mapping", "duplicate", "scope", "clock"])
def test_native_display_rejects_invented_or_mislabelled_authority(change):
    value = deepcopy(packet())
    if change == "wrong-home":
        value["metrics"][0]["model"] = "care"
    elif change == "edge":
        value["relationships"][0]["target"] = "Missing"
    elif change == "rule":
        value["rules"][0]["referenced_entities"] = ["Missing"]
    elif change == "mapping":
        value["execution_relationships"]["reviewed-contract"] = "Missing"
    elif change == "duplicate":
        value["entities"].append(deepcopy(value["entities"][0]))
    elif change == "scope":
        value["rules"][0]["property_scope"] = "All"
    else:
        value["captured_at"] = "2026-10-20T12:00:00"
    with pytest.raises(ValidationError):
        RecordedOntology.model_validate(value)
