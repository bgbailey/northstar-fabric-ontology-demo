"""Offline tests for the review-first TMDL contract scaffold."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.scaffold_contract import ScaffoldContractError, scaffold_contract

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "fabric" / "semantic-models"
CONTRACT = ROOT / "fabric" / "ontology" / "service-assurance.json"


def _write_model(root: Path, alias: str, table_source: str) -> None:
    table_folder = root / f"{alias}.SemanticModel" / "definition" / "tables"
    table_folder.mkdir(parents=True)
    (table_folder / "example.tmdl").write_text(table_source, encoding="utf-8")


def test_actual_tree_generates_all_model_qualified_tables_and_measures():
    draft = scaffold_contract(MODELS)

    assert len(draft["sourceModels"]) == 8
    assert len(draft["entities"]) == 59
    assert len(draft["metrics"]) == 64
    assert draft["generationMode"] == "discovery"
    assert draft["reviewRequired"] is True
    assert all(entity["reviewRequired"] for entity in draft["entities"])
    assert all(metric["reviewRequired"] for metric in draft["metrics"])


def test_hero_models_project_the_exact_curated_entity_and_metric_tuples():
    hero_models = ["network", "enterprise", "finance", "care"]
    discovery = scaffold_contract(MODELS, hero_models)
    draft = scaffold_contract(MODELS, hero_models, CONTRACT)
    reference = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert len(discovery["entities"]) == 31
    assert len(discovery["metrics"]) == 32
    assert draft["sourceModels"] == ["care", "enterprise", "finance", "network"]
    assert draft["generationMode"] == "contract-reference"
    assert [entity["name"] for entity in draft["entities"]] == [
        entity["name"]
        for entity in sorted(
            reference["entities"],
            key=lambda item: (
                item["model"].casefold(),
                item["modelTable"].casefold(),
                item["name"],
            ),
        )
    ]
    assert [
        (metric["name"], metric["model"], metric["modelTable"])
        for metric in draft["metrics"]
    ] == [
        (metric["name"], metric["model"], metric["modelTable"])
        for metric in sorted(
            reference["metrics"],
            key=lambda item: (
                item["model"].casefold(),
                item["modelTable"].casefold(),
                item["name"],
            ),
        )
    ]
    assert len(draft["entities"]) == 17
    assert len(draft["metrics"]) == 14
    assert draft["bridgeCandidates"]
    assert all(
        bridge["reviewRequired"] is True and bridge["status"] == "to review"
        for bridge in draft["bridgeCandidates"]
    )
    assert all("crossesModels" not in bridge for bridge in draft["bridgeCandidates"])


def test_quoted_names_tabs_comments_and_strings_do_not_create_fake_objects(tmp_path):
    _write_model(
        tmp_path,
        "model-a",
        """/// A table with a quoted name.
table 'Orders ''Daily'''
\t/// The only real measure.
\tmeasure 'Gross ''Sales''' = ```
\t\t// measure 'Comment Fake' = 1
\t\t"measure 'String Fake' = 2"
\t\tSUM('Orders ''Daily'''[amount])
\t\t```
\t\tformatString: #,##0

\tcolumn 'customer_key'
\t\tdataType: int64
\t\tsourceColumn: customer_key

\tcolumn 'source label'
\t\tsourceColumn: source_label

\tpartition 'Orders Daily' = entity
\t\tmode: directLake
\t\tsource
\t\t\tentityName: orders_daily
""",
    )
    _write_model(
        tmp_path,
        "model-b",
        """table Customers
\tmeasure Total = COUNTROWS(Customers)
\t\tformatString: #,##0
\tcolumn customer_key
\t\tsourceColumn: customer_key
\tpartition Customers = entity
\t\tmode: directLake
\t\tsource
\t\t\tentityName: customers
""",
    )

    draft = scaffold_contract(tmp_path)

    assert draft["metrics"] == [
        {
            "name": "Gross 'Sales'",
            "model": "model-a",
            "modelTable": "Orders 'Daily'",
            "entity": "model-a_OrdersDaily",
            "description": "The only real measure.",
            "status": "to review",
            "reviewRequired": True,
        },
        {
            "name": "Total",
            "model": "model-b",
            "modelTable": "Customers",
            "entity": "model-b_Customers",
            "description": None,
            "status": "to review",
            "reviewRequired": True,
        },
    ]
    assert len(draft["bridgeCandidates"]) == 1
    bridge = draft["bridgeCandidates"][0]
    assert bridge["columns"] == ["customer_key"]
    assert bridge["from"]["columns"] == ["customer_key"]
    assert bridge["to"]["columns"] == ["customer_key"]
    rendered = json.dumps(draft)
    assert "SUM(" not in rendered
    assert "Comment Fake" not in rendered
    assert "String Fake" not in rendered


def test_deterministic_cli_output_and_explicit_model_selection(tmp_path):
    output = tmp_path / "draft.json"
    command = [
        sys.executable,
        str(ROOT / "tools" / "scaffold_contract.py"),
        "--models",
        str(MODELS),
        "--include-model",
        "network",
        "--include-model",
        "finance",
        "--output",
        str(output),
    ]

    subprocess.run(command, check=True, capture_output=True, text=True)
    first = output.read_bytes()
    subprocess.run(command, check=True, capture_output=True, text=True)

    assert output.read_bytes() == first
    parsed = json.loads(first)
    assert parsed["sourceModels"] == ["finance", "network"]
    assert all(
        entity["model"] in {"finance", "network"} for entity in parsed["entities"]
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("table Example\n\tcolumn id\n\t\tsourceColumn: id\n", "no measures"),
        (
            "table Example\n"
            "\tmeasure Bad = 1\n"
            "\tcolumn id = CALCULATE(1)\n"
            "\t\tsourceColumn: id\n",
            "calculated columns are unsupported",
        ),
        (
            "table Example\n"
            "\tmeasure Bad = ```\n"
            "\t\tCOUNTROWS(Example)\n",
            "Unterminated triple-backtick expression",
        ),
    ],
)
def test_unsupported_or_empty_tmdl_fails_clearly(tmp_path, source, message):
    _write_model(tmp_path, "broken", source)

    with pytest.raises(ScaffoldContractError, match=message):
        scaffold_contract(tmp_path)


def test_cli_requires_a_file_or_stdout(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "scaffold_contract.py"),
            "--models",
            str(MODELS),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--output is required unless --json requests stdout" in result.stderr
    assert not result.stdout
