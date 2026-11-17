"""Create a review-first ontology-contract draft from local TMDL folders."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


class ScaffoldContractError(ValueError):
    """Raised when a model or reference contract cannot be safely scaffolded."""


@dataclass(frozen=True)
class Column:
    name: str
    source_column: str | None
    is_key: bool = False


@dataclass(frozen=True)
class Measure:
    name: str
    description: str | None


@dataclass
class TmdlTable:
    model: str
    name: str
    source_file: Path
    source_table: str | None = None
    description: str | None = None
    columns: list[Column] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    storage_modes: set[str] = field(default_factory=set)


@dataclass
class TmdlModel:
    alias: str
    folder: Path
    tables: list[TmdlTable]


def _name_alias(folder: Path) -> str:
    suffix = ".SemanticModel"
    return folder.name[: -len(suffix)] if folder.name.endswith(suffix) else folder.name


def _discover_model_folders(root: Path) -> list[Path]:
    if not root.exists():
        raise ScaffoldContractError(f"Models path does not exist: {root}")
    if not root.is_dir():
        raise ScaffoldContractError(f"Models path is not a folder: {root}")

    roots = [root] if (root / "definition").is_dir() else [
        path.parent for path in root.rglob("definition") if path.is_dir()
    ]
    unique = {path.resolve(): path for path in roots}
    folders = sorted(unique.values(), key=lambda path: _name_alias(path).casefold())
    if not folders:
        raise ScaffoldContractError(f"No semantic-model TMDL definition folders found under {root}")
    return folders


def _without_comments(
    line: str,
    in_block_comment: bool,
    in_fence: bool,
) -> tuple[str, bool, bool, bool]:
    if in_fence:
        end = line.find("```")
        if end < 0:
            return "", in_block_comment, True, False
        return line[end + 3 :], in_block_comment, False, False

    output: list[str] = []
    quote: str | None = None
    index = 0
    started_fence = False
    while index < len(line):
        char = line[index]
        following = line[index + 1] if index + 1 < len(line) else ""
        if in_block_comment:
            if char == "*" and following == "/":
                in_block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is not None:
            output.append(char)
            if char == quote:
                if following == quote:
                    output.append(following)
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "/" and following == "/":
            break
        if char == "/" and following == "*":
            in_block_comment = True
            index += 2
            continue
        if line.startswith("```", index):
            in_fence = True
            started_fence = True
            break
        if char in {"'", '"'}:
            quote = char
        output.append(char)
        index += 1
    return "".join(output), in_block_comment, in_fence, started_fence


def _check_tmdl_lexing(path: Path) -> None:
    in_block_comment = False
    in_fence = False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ScaffoldContractError(f"Unsupported non-UTF-8 TMDL in {path.name}") from error
    for line in lines:
        _, in_block_comment, in_fence, _ = _without_comments(
            line, in_block_comment, in_fence
        )
    if in_block_comment or in_fence:
        state = "comment" if in_block_comment else "triple-backtick expression"
        raise ScaffoldContractError(f"Unterminated {state} in {path.name}")


def _parse_declaration_name(text: str, path: Path, line_number: int) -> tuple[str, str]:
    value = text.strip()
    if not value:
        raise ScaffoldContractError(f"{path.name}:{line_number}: missing TMDL object name")
    if value.startswith("'"):
        name: list[str] = []
        index = 1
        while index < len(value):
            if value[index] != "'":
                name.append(value[index])
                index += 1
                continue
            if index + 1 < len(value) and value[index + 1] == "'":
                name.append("'")
                index += 2
                continue
            return "".join(name), value[index + 1 :].strip()
        raise ScaffoldContractError(
            f"{path.name}:{line_number}: unterminated quoted TMDL object name"
        )
    match = re.match(r"([^\s=]+)(.*)", value)
    if match is None:
        raise ScaffoldContractError(f"{path.name}:{line_number}: invalid TMDL object name")
    return match.group(1), match.group(2).strip()


def _indent(line: str) -> int:
    prefix = line[: len(line) - len(line.lstrip(" \t"))]
    return len(prefix.expandtabs(4))


def _parse_property_name(text: str, path: Path, line_number: int) -> str:
    name, remainder = _parse_declaration_name(text, path, line_number)
    if remainder:
        raise ScaffoldContractError(
            f"{path.name}:{line_number}: unsupported trailing content after {name!r}"
        )
    return name


def _parse_table(path: Path, model: str) -> TmdlTable:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ScaffoldContractError(f"Unsupported non-UTF-8 TMDL in {path.name}") from error

    table: TmdlTable | None = None
    pending_description: list[str] = []
    in_block_comment = False
    in_fence = False
    current_column_index: int | None = None
    current_column_indent: int | None = None

    for line_number, raw in enumerate(lines, start=1):
        stripped = raw.lstrip(" \t")
        if not in_block_comment and not in_fence and stripped.startswith("///"):
            pending_description.append(stripped[3:].strip())
            continue
        code, in_block_comment, in_fence, started_fence = _without_comments(
            raw, in_block_comment, in_fence
        )
        code = code.rstrip()
        if not code.strip():
            continue
        indentation = _indent(code)
        body = code.strip()

        table_match = re.match(r"table\s+(.+)$", body, re.IGNORECASE)
        if table_match:
            if indentation != 0:
                raise ScaffoldContractError(
                    f"{path.name}:{line_number}: table declaration must be top-level"
                )
            if table is not None:
                raise ScaffoldContractError(
                    f"{path.name}:{line_number}: multiple tables in one TMDL file are unsupported"
                )
            name, remainder = _parse_declaration_name(
                table_match.group(1), path, line_number
            )
            if remainder.startswith("="):
                raise ScaffoldContractError(
                    f"{path.name}:{line_number}: calculated tables are unsupported"
                )
            table = TmdlTable(
                model=model,
                name=name,
                source_file=path,
                description=" ".join(pending_description) or None,
            )
            pending_description = []
            continue

        if table is None:
            raise ScaffoldContractError(
                f"{path.name}:{line_number}: content appears before a table declaration"
            )

        measure_match = re.match(r"measure\s+(.+)$", body, re.IGNORECASE)
        if measure_match:
            name, remainder = _parse_declaration_name(
                measure_match.group(1), path, line_number
            )
            if not remainder.startswith("="):
                raise ScaffoldContractError(
                    f"{path.name}:{line_number}: measure {name!r} has no DAX assignment"
                )
            if not remainder[1:].strip() and not started_fence:
                raise ScaffoldContractError(
                    f"{path.name}:{line_number}: multiline DAX must use a triple-backtick fence"
                )
            table.measures.append(
                Measure(name=name, description=" ".join(pending_description) or None)
            )
            pending_description = []
            current_column_index = None
            current_column_indent = None
            continue

        column_match = re.match(r"column\s+(.+)$", body, re.IGNORECASE)
        if column_match:
            name, remainder = _parse_declaration_name(
                column_match.group(1), path, line_number
            )
            if remainder.startswith("="):
                raise ScaffoldContractError(
                    f"{path.name}:{line_number}: calculated columns are unsupported"
                )
            table.columns.append(Column(name=name, source_column=None))
            current_column_index = len(table.columns) - 1
            current_column_indent = indentation
            pending_description = []
            continue

        if (
            current_column_index is not None
            and current_column_indent is not None
            and indentation <= current_column_indent
        ):
            current_column_index = None
            current_column_indent = None

        if current_column_index is not None and re.match(
            r"sourceColumn\s*:", body, re.IGNORECASE
        ):
            source = body.split(":", 1)[1].strip()
            source_column = _parse_property_name(source, path, line_number)
            column = table.columns[current_column_index]
            table.columns[current_column_index] = Column(
                name=column.name,
                source_column=source_column,
                is_key=column.is_key,
            )
            continue

        if current_column_index is not None and body.lower().rstrip(":") == "iskey":
            column = table.columns[current_column_index]
            table.columns[current_column_index] = Column(
                name=column.name,
                source_column=column.source_column,
                is_key=True,
            )
            continue

        if re.match(r"isKey\s*:", body, re.IGNORECASE) and current_column_index is not None:
            value = body.split(":", 1)[1].strip().lower()
            if value == "true":
                column = table.columns[current_column_index]
                table.columns[current_column_index] = Column(
                    name=column.name,
                    source_column=column.source_column,
                    is_key=True,
                )
            continue

        if re.match(r"entityName\s*:", body, re.IGNORECASE):
            source = body.split(":", 1)[1].strip()
            source_table = _parse_property_name(source, path, line_number)
            if table.source_table and table.source_table != source_table:
                raise ScaffoldContractError(
                    f"{path.name}:{line_number}: multiple partition entity names are unsupported"
                )
            table.source_table = source_table
            continue

        if re.match(r"mode\s*:", body, re.IGNORECASE):
            table.storage_modes.add(body.split(":", 1)[1].strip().lower())

        partition_match = re.match(r"partition\s+(.+)$", body, re.IGNORECASE)
        if partition_match:
            _, remainder = _parse_declaration_name(
                partition_match.group(1), path, line_number
            )
            if remainder.lower().startswith("= calculated"):
                raise ScaffoldContractError(
                    f"{path.name}:{line_number}: calculated partitions are unsupported"
                )

    if in_block_comment or in_fence:
        state = "comment" if in_block_comment else "triple-backtick expression"
        raise ScaffoldContractError(f"{path.name}: unterminated {state}")
    if table is None:
        raise ScaffoldContractError(f"{path.name}: no table declaration found")
    if not table.columns:
        raise ScaffoldContractError(f"{path.name}: table {table.name!r} has no columns")
    return table


def parse_models(root: Path, include_models: Sequence[str] | None = None) -> list[TmdlModel]:
    folders = _discover_model_folders(root)
    lookup: dict[str, Path] = {}
    for folder in folders:
        for alias in (_name_alias(folder), folder.name):
            if alias.casefold() in lookup:
                raise ScaffoldContractError(f"Duplicate model name found: {alias}")
            lookup[alias.casefold()] = folder

    selected: list[Path]
    if include_models:
        selected_by_path: dict[Path, Path] = {}
        for requested in include_models:
            folder = lookup.get(requested.casefold())
            if folder is None:
                available = ", ".join(sorted({_name_alias(item) for item in folders}))
                raise ScaffoldContractError(
                    f"Unknown --include-model {requested!r}; available models: {available}"
                )
            selected_by_path[folder.resolve()] = folder
        selected = list(selected_by_path.values())
    else:
        selected = folders

    models: list[TmdlModel] = []
    for folder in selected:
        definition = folder / "definition"
        all_tmdl = sorted(definition.rglob("*.tmdl"), key=lambda path: str(path).casefold())
        if not all_tmdl:
            raise ScaffoldContractError(f"Model {_name_alias(folder)!r} has no TMDL files")
        for path in all_tmdl:
            _check_tmdl_lexing(path)
        table_files = sorted(
            (definition / "tables").glob("*.tmdl"), key=lambda path: path.name.casefold()
        )
        if not table_files:
            raise ScaffoldContractError(f"Model {_name_alias(folder)!r} has no table TMDL")
        tables = [_parse_table(path, _name_alias(folder)) for path in table_files]
        models.append(TmdlModel(alias=_name_alias(folder), folder=folder, tables=tables))

    models.sort(key=lambda model: model.alias.casefold())
    if not models:
        raise ScaffoldContractError("No models were selected")
    if not any(table for model in models for table in model.tables):
        raise ScaffoldContractError("Selected models contain no tables")
    if not any(table.measures for model in models for table in model.tables):
        raise ScaffoldContractError("Selected models contain no measures")
    return models


def _entity_name(model: str, table: str) -> str:
    compact = "".join(char for char in table if char.isalnum())
    return f"{model}_{compact or 'Table'}"


def _entity_for_table(table: TmdlTable) -> dict:
    columns = [
        {"name": column.name, "sourceColumn": column.source_column}
        for column in sorted(table.columns, key=lambda item: item.name.casefold())
    ]
    key_candidates = sorted(
        {
            column.source_column
            for column in table.columns
            if column.source_column
            and (column.is_key or column.source_column.casefold().endswith("_key"))
        }
    )
    return {
        "name": _entity_name(table.model, table.name),
        "logicalName": table.name,
        "model": table.model,
        "table": table.source_table,
        "modelTable": table.name,
        "description": table.description,
        "columns": columns,
        "keyCandidates": key_candidates,
        "status": "to review",
        "reviewRequired": True,
    }


def _load_reference_contract(path: Path) -> dict:
    if not path.is_file():
        raise ScaffoldContractError(f"Reference contract does not exist: {path}")
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScaffoldContractError(f"Reference contract is not valid JSON: {path}") from error
    if not isinstance(contract, dict):
        raise ScaffoldContractError("Reference contract must be a JSON object")
    for required in ("sourceModels", "entities", "metrics"):
        if not isinstance(contract.get(required), list):
            raise ScaffoldContractError(
                f"Reference contract is missing required array {required!r}"
            )
    return contract


def _project_reference(
    models: list[TmdlModel], contract: dict
) -> tuple[list[TmdlTable], list[dict], list[dict]]:
    selected_aliases = {model.alias.casefold(): model.alias for model in models}
    contract_aliases = {
        alias.casefold() for alias in contract["sourceModels"] if isinstance(alias, str)
    }
    if not contract_aliases:
        raise ScaffoldContractError("Reference contract sourceModels contains no model names")
    missing = sorted(contract_aliases - selected_aliases.keys(), key=str.casefold)
    if missing:
        raise ScaffoldContractError(
            "Reference contract requires models not selected: " + ", ".join(missing)
        )

    table_lookup = {
        (model.alias, table.name): table
        for model in models
        for table in model.tables
    }
    selected_tables: dict[tuple[str, str], TmdlTable] = {}
    entities: list[dict] = []
    for entry in contract["entities"]:
        if not isinstance(entry, dict):
            raise ScaffoldContractError("Reference contract entities must be objects")
        model = entry.get("model")
        model_table = entry.get("modelTable")
        if not isinstance(model, str) or not isinstance(model_table, str):
            raise ScaffoldContractError(
                "Reference contract entities require model and modelTable strings"
            )
        table = table_lookup.get((model, model_table))
        if table is None:
            raise ScaffoldContractError(
                f"Reference entity table is absent from TMDL: {model}.{model_table}"
            )
        if table.source_table and entry.get("table") != table.source_table:
            raise ScaffoldContractError(
                f"Reference entity source table disagrees with TMDL: "
                f"{model}.{model_table}"
            )
        selected_tables[(model, model_table)] = table
        entities.append(copy.deepcopy(entry))

    metrics: list[dict] = []
    for entry in contract["metrics"]:
        if not isinstance(entry, dict):
            raise ScaffoldContractError("Reference contract metrics must be objects")
        identity = (entry.get("model"), entry.get("modelTable"), entry.get("name"))
        if not all(isinstance(item, str) for item in identity):
            raise ScaffoldContractError(
                "Reference contract metrics require name, model and modelTable strings"
            )
        table = table_lookup.get((identity[0], identity[1]))
        if table is None or not any(measure.name == identity[2] for measure in table.measures):
            raise ScaffoldContractError(
                f"Reference metric is absent from TMDL: {identity[0]}.{identity[1]}[{identity[2]}]"
            )
        selected_tables[(identity[0], identity[1])] = table
        metrics.append(copy.deepcopy(entry))
    if not entities or not metrics:
        raise ScaffoldContractError("Reference contract projection contains no entities or metrics")

    entities.sort(key=lambda item: (item["model"].casefold(), item["modelTable"].casefold(), item["name"]))
    metrics.sort(key=lambda item: (item["model"].casefold(), item["modelTable"].casefold(), item["name"]))
    return (
        sorted(
            selected_tables.values(),
            key=lambda table: (table.model.casefold(), table.name.casefold()),
        ),
        entities,
        metrics,
    )


def _key_columns(table: TmdlTable) -> dict[str, bool]:
    return {
        column.source_column: column.is_key
        for column in table.columns
        if column.source_column
        and (column.source_column.casefold().endswith("_key") or column.is_key)
    }


def _bridge_candidates(tables: list[TmdlTable], entity_names: dict[tuple[str, str], str]) -> list[dict]:
    bridges: list[dict] = []
    ordered = sorted(tables, key=lambda table: (table.model.casefold(), table.name.casefold()))
    for left_index, left in enumerate(ordered):
        left_keys = _key_columns(left)
        for right in ordered[left_index + 1 :]:
            if left.model.casefold() == right.model.casefold():
                continue
            right_keys = _key_columns(right)
            matching = sorted(set(left_keys) & set(right_keys), key=str.casefold)
            if not matching:
                continue
            reasons = [
                "matching key-like sourceColumn name ending in _key: " + column
                for column in matching
                if column.casefold().endswith("_key")
            ]
            declared = [column for column in matching if left_keys[column] and right_keys[column]]
            if declared:
                reason = "matching declared isKey sourceColumn"
                if len(declared) > 1:
                    reason = "matching composite isKey sourceColumns"
                reasons.append(reason + ": " + ", ".join(declared))
            left_entity = entity_names[(left.model, left.name)]
            right_entity = entity_names[(right.model, right.name)]
            bridges.append(
                {
                    "from": {
                        "model": left.model,
                        "modelTable": left.name,
                        "entity": left_entity,
                        "columns": matching,
                    },
                    "to": {
                        "model": right.model,
                        "modelTable": right.name,
                        "entity": right_entity,
                        "columns": matching,
                    },
                    "columns": matching,
                    "reasons": reasons,
                    "status": "to review",
                    "reviewRequired": True,
                }
            )
    return bridges


def scaffold_contract(
    models_root: Path,
    include_models: Sequence[str] | None = None,
    contract_path: Path | None = None,
) -> dict:
    """Parse selected models and return a deterministic review-required draft."""
    contract = _load_reference_contract(contract_path) if contract_path else None
    if contract:
        requested = list(dict.fromkeys(include_models or contract["sourceModels"]))
    else:
        requested = list(include_models) if include_models else None
    models = parse_models(models_root, requested)

    if contract:
        tables, entities, metrics = _project_reference(models, contract)
        mode = "contract-reference"
    else:
        mode = "discovery"
        tables = sorted(
            [table for model in models for table in model.tables],
            key=lambda table: (table.model.casefold(), table.name.casefold()),
        )
        entities = [_entity_for_table(table) for table in tables]
        entity_lookup = {
            (table.model, table.name): entity["name"]
            for table, entity in zip(tables, entities, strict=True)
        }
        metrics = []
        for table in tables:
            for measure in sorted(table.measures, key=lambda item: item.name.casefold()):
                metrics.append(
                    {
                        "name": measure.name,
                        "model": table.model,
                        "modelTable": table.name,
                        "entity": entity_lookup[(table.model, table.name)],
                        "description": measure.description,
                        "status": "to review",
                        "reviewRequired": True,
                    }
                )
        entities.sort(
            key=lambda item: (
                item["model"].casefold(),
                item["modelTable"].casefold(),
                item["name"],
            )
        )
        metrics.sort(
            key=lambda item: (
                item["model"].casefold(),
                item["modelTable"].casefold(),
                item["name"],
            )
        )

    entity_names = {
        (entity["model"], entity["modelTable"]): entity["name"] for entity in entities
    }
    bridges = _bridge_candidates(tables, entity_names)
    output: dict = {
        "schemaVersion": "ontology-contract-draft-0.1",
        "generationMode": mode,
        "reviewRequired": True,
        "sourceModels": [model.alias for model in models],
        "entities": entities,
        "metrics": metrics,
        "bridgeCandidates": bridges,
        "reviewNotes": [
            "Bridge candidates are hypotheses, not ontology relationships.",
            "Review identity, temporal validity and metric grain; do not infer them from names.",
            "Source DAX expressions are intentionally not copied into this draft.",
        ],
    }
    if contract and contract_path:
        output["referenceContract"] = {
            "file": contract_path.name,
            "schemaVersion": contract.get("schemaVersion"),
            "purpose": (
                "Projection and validation only; curated logical, key, validity, grain, "
                "additivity and unit metadata is not inferred from TMDL."
            ),
        }
    return output


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a review-first ontology contract draft from local Power BI semantic-model "
            "TMDL folders."
        )
    )
    parser.add_argument("--models", required=True, type=Path, help="semantic-model folder or parent")
    parser.add_argument(
        "--include-model",
        action="append",
        help="model alias to include; repeat for three or four source models",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        help=(
            "reference contract whose curated entities and metrics are projected and validated "
            "against TMDL"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="output JSON path; use '-' (or omit with --json) for stdout",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the draft JSON to stdout when --output is omitted",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    arguments = parser.parse_args(argv)
    if arguments.output is None and not arguments.json:
        parser.error("--output is required unless --json requests stdout")
    try:
        draft = scaffold_contract(
            models_root=arguments.models,
            include_models=arguments.include_model,
            contract_path=arguments.contract,
        )
        rendered = json.dumps(draft, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
        if arguments.output is None or str(arguments.output) == "-":
            sys.stdout.write(rendered)
        else:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(rendered, encoding="utf-8")
    except ScaffoldContractError as error:
        parser.exit(2, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
