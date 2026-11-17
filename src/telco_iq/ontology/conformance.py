from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .assets import load_asset_module, load_contract

_CHECKER: Any | None = None


def _checker() -> Any:
    global _CHECKER
    if _CHECKER is None:
        _CHECKER = load_asset_module(
            "assets/check_ontology_conformance.py",
            "telco_iq._ontology_conformance_asset",
        )
    return _CHECKER


def check_exported_definition(
    contract: Mapping[str, Any] | Path,
    export_path: Path,
    *,
    expected_models: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    checker = _checker()
    value = (
        json.loads(Path(contract).read_text(encoding="utf-8"))
        if isinstance(contract, (str, Path))
        else dict(contract)
    )
    source = load_contract() if not value else value
    parts = checker.load_definition_parts(Path(export_path))
    contract_entities = {item["name"] for item in source["entities"]}
    tables = checker._parse_tables(parts)
    entities = checker._parse_entities(parts, tables, contract_entities)
    tom_relationships = checker._parse_tom_relationships(parts, tables)
    entity_relationships = checker._parse_entity_relationships(
        parts,
        entities,
        tom_relationships,
        contract_entities,
    )
    explicit_metrics = checker._parse_explicit_metrics(parts)
    rules = checker._parse_rules(parts, contract_entities)
    sections = {
        "entities": checker._compare_entities(source, entities, tables),
        "relationships": checker._compare_relationships(source, entity_relationships),
        "metrics": checker._compare_metrics(
            source,
            tables,
            entities,
            explicit_metrics,
            expected_models,
        ),
        "rules": checker._compare_rules(source, rules),
    }
    summary = {
        "missing": sum(len(section["missing"]) for section in sections.values()),
        "extra": sum(len(section["extra"]) for section in sections.values()),
        "different": sum(len(section["different"]) for section in sections.values()),
    }
    return {
        "conformant": sum(summary.values()) == 0,
        "contract": "packaged service-assurance contract",
        "definitionDir": str(Path(export_path).expanduser().resolve()),
        "summary": summary,
        **sections,
    }
