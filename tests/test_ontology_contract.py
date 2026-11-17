"""Offline validation tests for ontology contracts and generated authoring prompts."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from tools import build_ontology_prompts

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "ontology-contract-v1.schema.json"
EXECUTIVE_CONTRACT_PATH = ROOT / "fabric" / "ontology" / "executive-demo.json"
SERVICE_ASSURANCE_CONTRACT_PATH = ROOT / "fabric" / "ontology" / "service-assurance.json"
PROMPTS_PATH = ROOT / "fabric" / "ontology" / "service-assurance.prompts.md"
SEMANTIC_MODELS_DIR = ROOT / "fabric" / "semantic-models"
CATALOG_PATH = ROOT / "data" / "contracts" / "catalog.json"

DAX_EXPRESSION_RE = re.compile(
    r"\b(?:SUM|CALCULATE|DIVIDE|COUNTROWS|DISTINCTCOUNT|AVERAGE|MEDIAN|EXTERNALMEASURE|"
    r"HASONEVALUE|KEEPFILTERS)\s*\(|\bEVALUATE\b"
)
TMDL_NAME = r"(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_ -]*)"
TABLE_HEADER_RE = re.compile(rf"^table\s+({TMDL_NAME})\s*$", re.MULTILINE)
COLUMN_HEADER_RE = re.compile(rf"^\tcolumn\s+({TMDL_NAME})\s*$", re.MULTILINE)
MEASURE_HEADER_RE = re.compile(rf"^\tmeasure\s+({TMDL_NAME})\s*=", re.MULTILINE)
ENTITY_NAME_RE = re.compile(r"^\t\t\tentityName:\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ParsedTmdlTable:
    file_stem: str
    model_table: str
    entity_name: str | None
    columns: frozenset[str]
    measures: frozenset[str]


def _unquote_tmdl(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("'") and stripped.endswith("'"):
        return stripped[1:-1].replace("''", "'")
    return stripped


def load_model_tmdl(models_root: Path = SEMANTIC_MODELS_DIR) -> dict[str, dict[str, ParsedTmdlTable]]:
    parsed: dict[str, dict[str, ParsedTmdlTable]] = {}
    for model_dir in sorted(models_root.glob("*.SemanticModel")):
        model_id = model_dir.name.removesuffix(".SemanticModel")
        tables_dir = model_dir / "definition" / "tables"
        model_tables: dict[str, ParsedTmdlTable] = {}
        for tmdl_file in sorted(tables_dir.glob("*.tmdl")):
            text = tmdl_file.read_text(encoding="utf-8")
            table_match = TABLE_HEADER_RE.search(text)
            if table_match is None:
                raise ValueError(f"Missing table header in {tmdl_file}")
            entity_match = ENTITY_NAME_RE.search(text)
            columns = frozenset(_unquote_tmdl(m.group(1)) for m in COLUMN_HEADER_RE.finditer(text))
            measures = frozenset(_unquote_tmdl(m.group(1)) for m in MEASURE_HEADER_RE.finditer(text))
            model_tables[tmdl_file.stem] = ParsedTmdlTable(
                file_stem=tmdl_file.stem,
                model_table=_unquote_tmdl(table_match.group(1)),
                entity_name=_unquote_tmdl(entity_match.group(1)) if entity_match else None,
                columns=columns,
                measures=measures,
            )
        parsed[model_id] = model_tables
    return parsed


def validate_ontology_contract(
    contract: dict[str, Any],
    *,
    schema: dict[str, Any],
    tmdl_models: dict[str, dict[str, ParsedTmdlTable]],
    catalog: dict[str, Any],
) -> None:
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(contract), key=lambda err: list(err.path))
    if errors:
        raise ValueError(f"Schema validation failed: {errors[0].message}")

    serialized = json.dumps(contract)
    dax_match = DAX_EXPRESSION_RE.search(serialized)
    if dax_match:
        raise ValueError(f"Contract contains forbidden DAX expression token: {dax_match.group(0)}")

    source_models = set(contract["sourceModels"])
    missing_models = source_models - tmdl_models.keys()
    if missing_models:
        raise ValueError(f"Unknown source semantic models: {sorted(missing_models)}")

    entities_by_name: dict[str, dict[str, Any]] = {}
    entity_tmdl: dict[str, ParsedTmdlTable] = {}

    for entity in contract["entities"]:
        ename = entity["name"]
        if ename in entities_by_name:
            raise ValueError(f"Duplicate entity name: {ename}")
        model_id = entity["model"]
        if model_id not in source_models:
            raise ValueError(f"Entity {ename} references model {model_id!r} outside sourceModels")
        table_stem = entity["table"]
        tmdl_table = tmdl_models[model_id].get(table_stem)
        if tmdl_table is None:
            raise ValueError(f"Entity {ename} table {table_stem!r} does not exist in {model_id} TMDL")
        if tmdl_table.entity_name is not None and tmdl_table.entity_name != table_stem:
            raise ValueError(
                f"Entity {ename} table {table_stem!r} disagrees with partition entityName "
                f"{tmdl_table.entity_name!r}"
            )
        if "modelTable" in entity and entity["modelTable"] != tmdl_table.model_table:
            raise ValueError(
                f"Entity {ename} modelTable {entity['modelTable']!r} does not match TMDL table "
                f"{tmdl_table.model_table!r}"
            )
        for key_col in entity.get("key", []):
            if key_col not in tmdl_table.columns:
                raise ValueError(
                    f"Entity {ename} key column {key_col!r} missing in {model_id}.{table_stem}"
                )
        for span_field in ("interval", "validity"):
            if span_field in entity:
                for bound in ("from", "to"):
                    col = entity[span_field][bound]
                    if col not in tmdl_table.columns:
                        raise ValueError(
                            f"Entity {ename} {span_field}.{bound} column {col!r} missing in "
                            f"{model_id}.{table_stem}"
                        )
        entities_by_name[ename] = entity
        entity_tmdl[ename] = tmdl_table

    for rel in contract["relationships"]:
        left_name = rel["from"]
        right_name = rel["to"]
        if left_name not in entities_by_name:
            raise ValueError(f"Relationship source {left_name!r} is not defined in entities")
        if right_name not in entities_by_name:
            raise ValueError(f"Relationship target {right_name!r} is not defined in entities")
        left_entity = entities_by_name[left_name]
        right_entity = entities_by_name[right_name]
        if "crossesModels" in rel:
            expected_cross = left_entity["model"] != right_entity["model"]
            if rel["crossesModels"] != expected_cross:
                raise ValueError(
                    f"Relationship {left_name} -> {right_name} crossesModels={rel['crossesModels']} "
                    f"disagrees with entity models ({left_entity['model']}, {right_entity['model']})"
                )
        for join_col in rel.get("on", []):
            if join_col not in entity_tmdl[left_name].columns:
                raise ValueError(
                    f"Relationship {left_name} -> {right_name} join column {join_col!r} missing in "
                    f"{left_name}"
                )
            if join_col not in entity_tmdl[right_name].columns:
                raise ValueError(
                    f"Relationship {left_name} -> {right_name} join column {join_col!r} missing in "
                    f"{right_name}"
                )

    for metric in contract.get("metrics", []):
        mname = metric["name"]
        ent_name = metric["entity"]
        if ent_name not in entities_by_name:
            raise ValueError(f"Metric {mname!r} references unknown entity {ent_name!r}")
        home_entity = entities_by_name[ent_name]
        if metric["model"] != home_entity["model"]:
            raise ValueError(f"Metric {mname!r} model {metric['model']!r} mismatches {ent_name}")
        tmdl_table = entity_tmdl[ent_name]
        if metric["modelTable"] != tmdl_table.model_table:
            raise ValueError(
                f"Metric {mname!r} modelTable {metric['modelTable']!r} mismatches TMDL table "
                f"{tmdl_table.model_table!r}"
            )
        if mname not in tmdl_table.measures:
            raise ValueError(
                f"Metric {mname!r} does not exist as a measure in "
                f"{metric['model']}.{home_entity['table']} ({tmdl_table.model_table})"
            )
        for grain_col in metric["grain"]:
            if grain_col not in tmdl_table.columns:
                raise ValueError(
                    f"Metric {mname!r} grain column {grain_col!r} missing in "
                    f"{metric['model']}.{home_entity['table']}"
                )

    catalog_metrics = {item["id"]: item for item in catalog.get("metrics", [])}
    for metric_id in contract.get("sourceMetrics", []):
        spec = catalog_metrics.get(metric_id)
        if spec is None:
            raise ValueError(f"sourceMetrics entry {metric_id!r} not found in catalog.json")
        tmdl_table = tmdl_models.get(spec["model"], {}).get(spec["table"])
        if tmdl_table is None or spec["name"] not in tmdl_table.measures:
            raise ValueError(
                f"sourceMetrics entry {metric_id!r} measure {spec['name']!r} missing in TMDL"
            )

    for rule in contract["rules"]:
        if rule["statement"].strip() == "See hero-scenario.md":
            raise ValueError(f"Rule {rule['name']!r} still contains placeholder statement")
        for target in rule.get("linkedTo", []):
            if target not in entities_by_name:
                raise ValueError(
                    f"Rule {rule['name']!r} links to unknown entity type {target!r}"
                )


@pytest.fixture(scope="module")
def validation_bundle() -> tuple[dict[str, Any], dict[str, dict[str, ParsedTmdlTable]], dict[str, Any]]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    tmdl_models = load_model_tmdl()
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return schema, tmdl_models, catalog


@pytest.mark.parametrize(
    "contract_path",
    [EXECUTIVE_CONTRACT_PATH, SERVICE_ASSURANCE_CONTRACT_PATH],
    ids=["executive-demo", "service-assurance"],
)
def test_both_ontology_contracts_validate_against_schema_and_tmdl(
    contract_path: Path,
    validation_bundle: tuple[dict[str, Any], dict[str, dict[str, ParsedTmdlTable]], dict[str, Any]],
) -> None:
    schema, tmdl_models, catalog = validation_bundle
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    validate_ontology_contract(contract, schema=schema, tmdl_models=tmdl_models, catalog=catalog)


def test_service_assurance_contract_covers_full_hero_scenario() -> None:
    contract = json.loads(SERVICE_ASSURANCE_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["sourceModels"] == ["network", "enterprise", "finance", "care"]
    assert len(contract["entities"]) == 17
    assert len(contract["metrics"]) == 14
    assert [rule["id"] for rule in contract["rules"]] == ["R1", "R2", "R3", "R4", "R5"]
    assert contract["checks"] == ["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
    hop_numbers = sorted({rel["hop"] for rel in contract["relationships"] if "hop" in rel})
    assert hop_numbers == [1, 2, 3, 4, 5, 6, 7]
    entity_names = {entity["name"] for entity in contract["entities"]}
    r5 = next(rule for rule in contract["rules"] if rule["id"] == "R5")
    assert set(r5["linkedTo"]) == entity_names


@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    [
        ("entity_table", "does not exist in network TMDL"),
        ("entity_model_table", "does not match TMDL table"),
        ("entity_key_column", "key column 'nonexistent_key' missing"),
        ("entity_interval_column", "interval.from column 'bad_start' missing"),
        ("relationship_join_column", "join column 'missing_join_key' missing"),
        ("metric_measure_name", "does not exist as a measure"),
        ("metric_grain_column", "grain column 'invented_grain' missing"),
        ("rule_linked_entity", "links to unknown entity type"),
        ("dax_in_rule", "forbidden DAX expression"),
        ("schema_extra_property", "Schema validation failed"),
    ],
)
def test_mutated_contract_fails_validation(
    mutation: str,
    expected_message: str,
    validation_bundle: tuple[dict[str, Any], dict[str, dict[str, ParsedTmdlTable]], dict[str, Any]],
) -> None:
    schema, tmdl_models, catalog = validation_bundle
    contract = copy.deepcopy(json.loads(SERVICE_ASSURANCE_CONTRACT_PATH.read_text(encoding="utf-8")))

    if mutation == "entity_table":
        contract["entities"][0]["table"] = "missing_incident_table"
    elif mutation == "entity_model_table":
        contract["entities"][0]["modelTable"] = "Wrong Incidents Display"
    elif mutation == "entity_key_column":
        contract["entities"][0]["key"] = ["nonexistent_key"]
    elif mutation == "entity_interval_column":
        contract["entities"][0]["interval"]["from"] = "bad_start"
    elif mutation == "relationship_join_column":
        contract["relationships"][0]["on"] = ["missing_join_key"]
    elif mutation == "metric_measure_name":
        contract["metrics"][0]["name"] = "Invented Measure Name"
    elif mutation == "metric_grain_column":
        contract["metrics"][0]["grain"] = ["invented_grain"]
    elif mutation == "rule_linked_entity":
        contract["rules"][0]["linkedTo"] = ["unknown_Entity"]
    elif mutation == "dax_in_rule":
        contract["rules"][0]["statement"] = "Never write SUM('Incidents'[incident_key]) in rules."
    elif mutation == "schema_extra_property":
        contract["unexpectedTopLevelField"] = True

    with pytest.raises(ValueError, match=expected_message):
        validate_ontology_contract(contract, schema=schema, tmdl_models=tmdl_models, catalog=catalog)


def test_prompts_pack_is_reproducible_and_free_of_dax() -> None:
    assert build_ontology_prompts.main(["--check"]) == 0
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    assert DAX_EXPRESSION_RE.search(text) is None
    for heading in (
        "### Step 1: Network (`network`)",
        "### Step 2: Enterprise services (`enterprise`)",
        "### Step 3: Finance (`finance`)",
        "### Step 4: Care (`care`)",
        "## Human review checklist before Act mode",
        "### L1:",
        "### L2:",
        "### L3:",
        "### L4:",
    ):
        assert heading in text


def test_prompts_check_fails_when_contract_changes_without_regenerating(tmp_path: Path) -> None:
    mutated_contract = json.loads(SERVICE_ASSURANCE_CONTRACT_PATH.read_text(encoding="utf-8"))
    mutated_contract["businessQuestion"] = "Updated question without regenerating prompts?"
    contract_copy = tmp_path / "service-assurance.json"
    prompts_copy = tmp_path / "service-assurance.prompts.md"
    contract_copy.write_text(json.dumps(mutated_contract, indent=2) + "\n", encoding="utf-8", newline="\n")
    prompts_copy.write_text(PROMPTS_PATH.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")

    exit_code = build_ontology_prompts.main(
        ["--check", "--contract", str(contract_copy), "--output", str(prompts_copy)]
    )
    assert exit_code == 1

    assert (
        build_ontology_prompts.main(
            ["--contract", str(contract_copy), "--output", str(prompts_copy)]
        )
        == 0
    )
    assert (
        build_ontology_prompts.main(
            ["--check", "--contract", str(contract_copy), "--output", str(prompts_copy)]
        )
        == 0
    )
