"""Check an exported Fabric IQ ontology TMDL definition against an ontology contract."""

# TODO(verify-live): G1, G2 — Export the ontology definition from the demo tenant via getDefinition
# and run check_ontology_conformance.py (never write an exported definition back over a working
# ontology; see R-15).

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TMDL_NAME = r"(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_#-]*)"
TMDL_LOOSE_NAME = r"(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_ #.-]*)"
COLUMN_REF_RE = re.compile(rf"^\s*({TMDL_NAME})\.({TMDL_NAME})\s*$")
EXTERNAL_MEASURE_RE = re.compile(
    r'^\s*EXTERNALMEASURE\(\s*"((?:[^"]|"")+)"\s*,\s*'
    r"(?:INTEGER|DOUBLE|CURRENCY|STRING|BOOLEAN|DATETIME|VARIANT)\s*,\s*"
    r'"((?:[^"]|"")+)"\s*\)\s*$'
)


@dataclass
class _Block:
    text: str
    children: list[_Block] = field(default_factory=list)
    description: str = ""


@dataclass(frozen=True)
class ParsedTable:
    name: str
    entity_name: str | None
    source_model_hint: str | None
    columns: dict[str, str]
    measures: list[ParsedMeasure]


@dataclass(frozen=True)
class ParsedMeasure:
    name: str
    table_name: str
    expression: str
    is_source_link: bool
    external_measure_name: str | None


@dataclass(frozen=True)
class ParsedEntity:
    name: str
    raw_name: str
    backing_table: str | None
    key_columns: list[str]
    source_model_hint: str | None


@dataclass(frozen=True)
class ParsedTomRelationship:
    name: str
    from_table: str
    to_table: str
    from_columns: list[str]
    to_columns: list[str]


@dataclass(frozen=True)
class ParsedEntityRelationship:
    name: str
    from_entity: str
    to_entity: str
    backing_relationship: str | None
    backing_type: str
    join_columns: list[str] | None
    backing_error: str | None


@dataclass(frozen=True)
class ParsedRule:
    name: str
    statement: str
    referenced_entities: list[str]


def _unquote(raw: str) -> str:
    value = raw.strip()
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    return value


def _normalize_entity_name(raw_name: str, contract_entities: set[str]) -> str:
    cleaned = _unquote(raw_name)
    if cleaned in contract_entities:
        return cleaned
    if cleaned.startswith("default#"):
        stripped = cleaned.split("#", 1)[1]
        return stripped
    if "#" in cleaned:
        _, local_name = cleaned.split("#", 1)
        if local_name in contract_entities:
            return local_name
    return cleaned


def _parse_blocks(text: str) -> list[_Block]:
    root = _Block("")
    stack: list[tuple[int, _Block]] = [(-1, root)]
    comments: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        stripped = raw_line.lstrip()
        if stripped.startswith("///"):
            comments.append(stripped[3:].strip())
            continue
        expanded = raw_line.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip())
        while stack[-1][0] >= indent:
            stack.pop()
        node = _Block(stripped, description=" ".join(comments))
        comments = []
        stack[-1][1].children.append(node)
        stack.append((indent, node))
    return root.children


def _children(node: _Block, prefix: str) -> list[_Block]:
    return [child for child in node.children if child.text.startswith(prefix)]


def _optional_value(node: _Block, key: str) -> str | None:
    matches = _children(node, f"{key}:")
    if not matches:
        return None
    return matches[0].text.split(":", 1)[1].strip()


def _annotations(node: _Block) -> dict[str, str]:
    result: dict[str, str] = {}
    for child in _children(node, "annotation "):
        match = re.match(r"^annotation\s+([A-Za-z0-9_.-]+)\s*=\s*(.+)$", child.text)
        if match:
            result[match.group(1)] = match.group(2).strip()
    return result


def _parse_column_ref(value: str) -> tuple[str, str] | None:
    match = COLUMN_REF_RE.match(value)
    if not match:
        return None
    return _unquote(match.group(1)), _unquote(match.group(2))


def load_definition_parts(definition_dir: Path) -> dict[str, str]:
    if definition_dir.is_file():
        return _decode_envelope_json(definition_dir)

    if not definition_dir.is_dir():
        raise FileNotFoundError(f"Definition directory does not exist: {definition_dir}")

    nested_dir = definition_dir / "definition"
    scan_dir = (
        nested_dir
        if nested_dir.is_dir() and any(nested_dir.rglob("*.tmdl"))
        else definition_dir
    )

    parts: dict[str, str] = {}
    for file_path in sorted(scan_dir.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(scan_dir).as_posix()
        if rel == ".platform" or file_path.suffix.lower() == ".tmdl":
            parts[rel] = file_path.read_text(encoding="utf-8-sig")

    if parts:
        return parts

    for candidate_name in ("getDefinition.json", "definition.json"):
        envelope_file = definition_dir / candidate_name
        if envelope_file.is_file():
            return _decode_envelope_json(envelope_file)

    return parts


def _decode_envelope_json(envelope_path: Path) -> dict[str, str]:
    payload = json.loads(envelope_path.read_text(encoding="utf-8"))
    raw_parts = (
        payload["definition"]["parts"]
        if "definition" in payload and isinstance(payload["definition"], dict)
        else payload.get("parts", [])
    )
    parts: dict[str, str] = {}
    for entry in raw_parts:
        path = entry["path"]
        payload_type = entry.get("payloadType", "InlineBase64")
        if payload_type == "InlineBase64":
            decoded = base64.b64decode(entry["payload"]).decode("utf-8-sig")
        else:
            decoded = str(entry["payload"])
        parts[path] = decoded
    return parts


def _parse_tables(parts: dict[str, str]) -> dict[str, ParsedTable]:
    tables: dict[str, ParsedTable] = {}
    for path, text in sorted(parts.items()):
        if not path.startswith("tables/") or not path.endswith(".tmdl"):
            continue
        for block in _parse_blocks(text):
            if not block.text.startswith("table "):
                continue
            table_name = _unquote(block.text[len("table ") :])
            columns: dict[str, str] = {}
            for col_block in _children(block, "column "):
                col_name = _unquote(col_block.text[len("column ") :])
                source_col = _optional_value(col_block, "sourceColumn")
                columns[col_name] = _unquote(source_col) if source_col else col_name

            entity_name: str | None = None
            source_model_hint: str | None = None
            for part_block in _children(block, "partition "):
                part_annotations = _annotations(part_block)
                source_model_hint = (
                    part_annotations.get("ONT_ItemName")
                    or part_annotations.get("ONT_SemanticModelDatabase")
                    or source_model_hint
                )
                for src_block in part_block.children:
                    if src_block.text == "source":
                        raw_entity = _optional_value(src_block, "entityName")
                        if raw_entity:
                            entity_name = _unquote(raw_entity)

            measures: list[ParsedMeasure] = []
            for m_block in _children(block, "measure "):
                m_match = re.match(rf"^measure\s+({TMDL_LOOSE_NAME})\s*=\s*(.+)$", m_block.text)
                if not m_match:
                    continue
                m_name = _unquote(m_match.group(1))
                expr = m_match.group(2).strip()
                ext_match = EXTERNAL_MEASURE_RE.match(expr)
                measures.append(
                    ParsedMeasure(
                        name=m_name,
                        table_name=table_name,
                        expression=expr,
                        is_source_link=ext_match is not None,
                        external_measure_name=(
                            ext_match.group(1).replace('""', '"') if ext_match else None
                        ),
                    )
                )

            tables[table_name] = ParsedTable(
                name=table_name,
                entity_name=entity_name,
                source_model_hint=source_model_hint,
                columns=columns,
                measures=measures,
            )
    return tables


def _parse_entities(
    parts: dict[str, str],
    tables: dict[str, ParsedTable],
    contract_entities: set[str],
) -> dict[str, ParsedEntity]:
    entities: dict[str, ParsedEntity] = {}
    for path, text in sorted(parts.items()):
        if not path.startswith("entities/") or not path.endswith(".tmdl"):
            continue
        for block in _parse_blocks(text):
            if not block.text.startswith("entity "):
                continue
            raw_name = _unquote(block.text[len("entity ") :])
            norm_name = _normalize_entity_name(raw_name, contract_entities)
            raw_backing = _optional_value(block, "backingTable")
            backing_table = _unquote(raw_backing) if raw_backing else None
            table_obj = tables.get(backing_table) if backing_table else None

            prop_to_column: dict[str, str] = {}
            for prop_block in _children(block, "property "):
                prop_name = _unquote(prop_block.text[len("property ") :])
                mapped_col = prop_name
                for cfg in prop_block.children:
                    if cfg.text == "backingConfiguration":
                        val_col = _optional_value(cfg, "valueColumn")
                        if val_col:
                            parsed_ref = _parse_column_ref(val_col)
                            if parsed_ref:
                                ref_table, ref_col = parsed_ref
                                ref_table_obj = tables.get(ref_table)
                                mapped_col = (
                                    ref_table_obj.columns.get(ref_col, ref_col)
                                    if ref_table_obj
                                    else ref_col
                                )
                if table_obj and mapped_col in table_obj.columns:
                    mapped_col = table_obj.columns[mapped_col]
                prop_to_column[prop_name] = mapped_col

            raw_keys = [
                _unquote(child.text.split(":", 1)[1])
                for child in _children(block, "keyProperty:")
            ]
            entity_annotations = _annotations(block)
            if not raw_keys and "keyColumns" in entity_annotations:
                raw_keys = [
                    item.strip()
                    for item in entity_annotations["keyColumns"].split(",")
                    if item.strip()
                ]
            key_columns = [prop_to_column.get(key_name, key_name) for key_name in raw_keys]

            entities[norm_name] = ParsedEntity(
                name=norm_name,
                raw_name=raw_name,
                backing_table=backing_table,
                key_columns=key_columns,
                source_model_hint=entity_annotations.get("source_model"),
            )
    return entities


def _parse_tom_relationships(
    parts: dict[str, str],
    tables: dict[str, ParsedTable],
) -> dict[str, ParsedTomRelationship]:
    text = parts.get("relationships.tmdl", "")
    if not text:
        return {}
    tom_rels: dict[str, ParsedTomRelationship] = {}
    for block in _parse_blocks(text):
        if not block.text.startswith("relationship "):
            continue
        rel_name = _unquote(block.text[len("relationship ") :])
        from_table = ""
        to_table = ""
        from_cols: list[str] = []
        to_cols: list[str] = []
        for f_child in _children(block, "fromColumn:"):
            ref = _parse_column_ref(f_child.text.split(":", 1)[1])
            if ref:
                from_table, col = ref
                t_obj = tables.get(from_table)
                from_cols.append(t_obj.columns.get(col, col) if t_obj else col)
        for t_child in _children(block, "toColumn:"):
            ref = _parse_column_ref(t_child.text.split(":", 1)[1])
            if ref:
                to_table, col = ref
                t_obj = tables.get(to_table)
                to_cols.append(t_obj.columns.get(col, col) if t_obj else col)
        tom_rels[rel_name] = ParsedTomRelationship(
            name=rel_name,
            from_table=from_table,
            to_table=to_table,
            from_columns=from_cols,
            to_columns=to_cols,
        )
    return tom_rels


def _parse_entity_relationships(
    parts: dict[str, str],
    entities: dict[str, ParsedEntity],
    tom_rels: dict[str, ParsedTomRelationship],
    contract_entities: set[str],
) -> list[ParsedEntityRelationship]:
    text = parts.get("entityRelationships.tmdl", "")
    if not text:
        return []
    results: list[ParsedEntityRelationship] = []
    for block in _parse_blocks(text):
        if not block.text.startswith("entityRelationship "):
            continue
        er_name = _unquote(block.text[len("entityRelationship ") :])
        raw_from = _optional_value(block, "fromEntity") or ""
        raw_to = _optional_value(block, "toEntity") or ""
        from_entity = _normalize_entity_name(raw_from, contract_entities)
        to_entity = _normalize_entity_name(raw_to, contract_entities)

        backing_rel: str | None = None
        backing_type = "none"
        join_columns: list[str] | None = None
        backing_error: str | None = None

        cfg_blocks = [c for c in block.children if c.text == "backingConfiguration"]
        if cfg_blocks:
            cfg = cfg_blocks[0]
            explicit_type = _optional_value(cfg, "type")
            rel_ref = _optional_value(cfg, "relationship")
            if rel_ref:
                backing_type = explicit_type or "relationship"
                backing_rel = _unquote(rel_ref)
                tom = tom_rels.get(backing_rel)
                if tom is None:
                    backing_error = f"backing relationship {backing_rel!r} not found in relationships.tmdl"
                else:
                    if tom.from_columns != tom.to_columns:
                        backing_error = (
                            f"mismatched column names {tom.from_columns} vs {tom.to_columns}"
                        )
                    join_columns = tom.from_columns
                    left_ent = entities.get(from_entity)
                    right_ent = entities.get(to_entity)
                    if left_ent and right_ent and left_ent.backing_table and right_ent.backing_table:
                        expected_tables = {left_ent.backing_table, right_ent.backing_table}
                        actual_tables = {tom.from_table, tom.to_table}
                        if actual_tables != expected_tables:
                            backing_error = (
                                f"backing relationship tables {sorted(actual_tables)} do not match "
                                f"entity backing tables {sorted(expected_tables)}"
                            )
            elif explicit_type == "table":
                backing_type = "table"
                from_rel_name = _optional_value(cfg, "fromRelationship")
                to_rel_name = _optional_value(cfg, "toRelationship")
                from_tom = tom_rels.get(_unquote(from_rel_name)) if from_rel_name else None
                to_tom = tom_rels.get(_unquote(to_rel_name)) if to_rel_name else None
                if from_tom is None or to_tom is None:
                    backing_error = "junction relationship missing fromRelationship or toRelationship"
                else:
                    join_columns = from_tom.to_columns or from_tom.from_columns

        results.append(
            ParsedEntityRelationship(
                name=er_name,
                from_entity=from_entity,
                to_entity=to_entity,
                backing_relationship=backing_rel,
                backing_type=backing_type,
                join_columns=join_columns,
                backing_error=backing_error,
            )
        )
    return results


def _parse_explicit_metrics(parts: dict[str, str]) -> list[ParsedMeasure]:
    explicit: list[ParsedMeasure] = []
    for path, text in sorted(parts.items()):
        if not path.startswith("metrics/") or not path.endswith(".tmdl"):
            continue
        for block in _parse_blocks(text):
            if not block.text.startswith("metric "):
                continue
            m_name = _unquote(block.text[len("metric ") :])
            expr_text = "explicit_metric"
            for expr_block in _children(block, "expression"):
                dialect = _optional_value(expr_block, "dialect") or "unknown"
                formula = _optional_value(expr_block, "expression") or ""
                expr_text = f"{dialect}:{formula}"
            explicit.append(
                ParsedMeasure(
                    name=m_name,
                    table_name="<top-level-metric>",
                    expression=expr_text,
                    is_source_link=False,
                    external_measure_name=None,
                )
            )
    return explicit


def _parse_rules(parts: dict[str, str], contract_entities: set[str]) -> dict[str, ParsedRule]:
    rules: dict[str, ParsedRule] = {}
    for path, text in sorted(parts.items()):
        if not path.startswith("rules/") or not path.endswith(".tmdl"):
            continue
        for block in _parse_blocks(text):
            if not block.text.startswith("rule "):
                continue
            r_name = _unquote(block.text[len("rule ") :])
            statement = _optional_value(block, "statement") or ""
            referenced: list[str] = []
            for ref_block in _children(block, "ruleReferencedEntity "):
                raw_ent = ref_block.text[len("ruleReferencedEntity ") :]
                referenced.append(_normalize_entity_name(raw_ent, contract_entities))
            rules[r_name] = ParsedRule(
                name=r_name,
                statement=statement,
                referenced_entities=referenced,
            )
    return rules


def _compare_entities(
    contract: dict[str, Any],
    actual_entities: dict[str, ParsedEntity],
    tables: dict[str, ParsedTable],
) -> dict[str, list[Any]]:
    expected_map = {item["name"]: item for item in contract["entities"]}
    missing = sorted(expected_map.keys() - actual_entities.keys())
    extra = sorted(actual_entities.keys() - expected_map.keys())
    different: list[dict[str, Any]] = []

    for name in sorted(expected_map.keys() & actual_entities.keys()):
        exp = expected_map[name]
        act = actual_entities[name]
        reasons: list[str] = []

        valid_table_names = {
            candidate
            for candidate in (
                exp["table"],
                exp.get("modelTable"),
                exp["name"],
                f"{exp['model']}_{exp['table']}",
            )
            if candidate
        }
        if not act.backing_table:
            reasons.append("missing backingTable")
        elif tables and act.backing_table not in tables:
            reasons.append(f"backingTable {act.backing_table!r} not found in tables/*.tmdl")
        else:
            table_obj = tables.get(act.backing_table)
            resolved_source_table = (
                table_obj.entity_name
                if table_obj and table_obj.entity_name
                else act.backing_table
            )
            if resolved_source_table not in valid_table_names and act.backing_table not in valid_table_names:
                reasons.append(
                    f"backingTable resolves to {resolved_source_table!r}, expected one of "
                    f"{sorted(valid_table_names)}"
                )
            model_hint = act.source_model_hint or (table_obj.source_model_hint if table_obj else None)
            if model_hint and exp["model"].lower() not in model_hint.lower():
                reasons.append(
                    f"source model annotation {model_hint!r} does not match {exp['model']!r}"
                )

        if "key" in exp and act.key_columns != exp["key"]:
            reasons.append(f"key columns {act.key_columns} != expected {exp['key']}")

        if reasons:
            different.append({"name": name, "reasons": reasons})

    return {"missing": missing, "extra": extra, "different": different}


def _expected_rel_id(rel: dict[str, Any]) -> str:
    return f"{rel['from']} -> {rel['to']}"


def _relationship_join_matches(exp: dict[str, Any], act: ParsedEntityRelationship) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if act.backing_error:
        reasons.append(act.backing_error)
    if "on" in exp and act.join_columns is not None and act.join_columns != exp["on"]:
        reasons.append(f"join columns {act.join_columns} != expected {exp['on']}")
    return (len(reasons) == 0), reasons


def _compare_relationships(
    contract: dict[str, Any],
    actual_rels: list[ParsedEntityRelationship],
) -> dict[str, list[Any]]:
    expected_rels = list(contract["relationships"])
    unmatched_exp = set(range(len(expected_rels)))
    unmatched_act = set(range(len(actual_rels)))
    different: list[dict[str, Any]] = []

    # Pass 1: exact (fromEntity, toEntity) and conformant join columns
    for exp_idx in sorted(unmatched_exp):
        exp = expected_rels[exp_idx]
        for act_idx in sorted(unmatched_act):
            act = actual_rels[act_idx]
            if (act.from_entity, act.to_entity) == (exp["from"], exp["to"]):
                ok, _ = _relationship_join_matches(exp, act)
                if ok:
                    unmatched_exp.remove(exp_idx)
                    unmatched_act.remove(act_idx)
                    break

    # Pass 2: exact (fromEntity, toEntity) with differing join columns or backing error
    for exp_idx in sorted(list(unmatched_exp)):
        exp = expected_rels[exp_idx]
        for act_idx in sorted(list(unmatched_act)):
            act = actual_rels[act_idx]
            if (act.from_entity, act.to_entity) == (exp["from"], exp["to"]):
                _, reasons = _relationship_join_matches(exp, act)
                different.append(
                    {
                        "relationship": _expected_rel_id(exp),
                        "actualName": act.name,
                        "reasons": reasons or ["relationship backing differs"],
                    }
                )
                unmatched_exp.remove(exp_idx)
                unmatched_act.remove(act_idx)
                break

    # Pass 3: modified target/direction on a named or same-source relationship
    for exp_idx in sorted(list(unmatched_exp)):
        exp = expected_rels[exp_idx]
        canonical_slug = f"{exp['from']}_to_{exp['to']}".lower()
        candidate_act: int | None = None
        for act_idx in sorted(unmatched_act):
            act = actual_rels[act_idx]
            if act.name.lower() == canonical_slug:
                candidate_act = act_idx
                break
        if candidate_act is None:
            for act_idx in sorted(unmatched_act):
                act = actual_rels[act_idx]
                if (act.from_entity, act.to_entity) == (exp["to"], exp["from"]):
                    candidate_act = act_idx
                    break
        if candidate_act is None:
            for act_idx in sorted(unmatched_act):
                act = actual_rels[act_idx]
                if act.from_entity == exp["from"]:
                    candidate_act = act_idx
                    break
        if candidate_act is not None:
            act = actual_rels[candidate_act]
            reasons = [
                f"endpoints ({act.from_entity} -> {act.to_entity}) != expected "
                f"({exp['from']} -> {exp['to']})"
            ]
            _, join_reasons = _relationship_join_matches(exp, act)
            reasons.extend(join_reasons)
            different.append(
                {
                    "relationship": _expected_rel_id(exp),
                    "actualName": act.name,
                    "reasons": reasons,
                }
            )
            unmatched_exp.remove(exp_idx)
            unmatched_act.remove(candidate_act)

    missing = [_expected_rel_id(expected_rels[idx]) for idx in sorted(unmatched_exp)]
    extra = [
        f"{actual_rels[idx].from_entity} -> {actual_rels[idx].to_entity} ({actual_rels[idx].name})"
        for idx in sorted(unmatched_act)
    ]
    return {"missing": missing, "extra": extra, "different": different}


def _compare_metrics(
    contract: dict[str, Any],
    tables: dict[str, ParsedTable],
    actual_entities: dict[str, ParsedEntity],
    explicit_metrics: list[ParsedMeasure],
) -> dict[str, list[Any]]:
    backing_to_entity = {
        ent.backing_table: ent.name for ent in actual_entities.values() if ent.backing_table
    }
    actual_by_name: dict[str, list[tuple[ParsedMeasure, str | None]]] = {}
    for table in tables.values():
        owner_entity = backing_to_entity.get(table.name)
        for measure in table.measures:
            actual_by_name.setdefault(measure.name, []).append((measure, owner_entity))
    for exp_metric in explicit_metrics:
        actual_by_name.setdefault(exp_metric.name, []).append((exp_metric, None))

    expected_metrics = {item["name"]: item for item in contract.get("metrics", [])}
    missing = sorted(expected_metrics.keys() - actual_by_name.keys())
    extra = (
        []
        if contract.get("preserveAllGeneratedSourceProxies")
        else sorted(actual_by_name.keys() - expected_metrics.keys())
    )
    different: list[dict[str, Any]] = []

    for m_name in sorted(expected_metrics.keys() & actual_by_name.keys()):
        exp = expected_metrics[m_name]
        entries = actual_by_name[m_name]
        reasons: list[str] = []
        if len(entries) > 1:
            reasons.append(f"metric defined {len(entries)} times across definition parts")
        measure, owner_entity = entries[0]
        if not measure.is_source_link:
            reasons.append(
                f"metric uses custom or explicit expression ({measure.expression!r}) instead of "
                "EXTERNALMEASURE source measure link"
            )
        elif measure.external_measure_name != exp["name"]:
            reasons.append(
                f"EXTERNALMEASURE targets {measure.external_measure_name!r} != expected {exp['name']!r}"
            )
        if owner_entity != exp["entity"]:
            reasons.append(
                f"home entity {owner_entity!r} != expected {exp['entity']!r}"
            )
        if reasons:
            different.append({"name": m_name, "reasons": reasons})

    return {"missing": missing, "extra": extra, "different": different}


def _compare_rules(
    contract: dict[str, Any],
    actual_rules: dict[str, ParsedRule],
) -> dict[str, list[Any]]:
    all_entity_names = sorted(item["name"] for item in contract["entities"])
    expected_rules = list(contract["rules"])
    matched_actual_names: set[str] = set()
    missing: list[str] = []
    different: list[dict[str, Any]] = []

    for exp in expected_rules:
        rule_name = exp["name"]
        rule_id = exp.get("id")
        act = actual_rules.get(rule_name)
        if act is not None:
            matched_actual_names.add(rule_name)
        elif rule_id and rule_id in actual_rules:
            act = actual_rules[rule_id]
            matched_actual_names.add(rule_id)
        else:
            missing.append(rule_name)
            continue

        reasons: list[str] = []
        if act.statement.strip() != exp["statement"].strip():
            reasons.append("rule statement differs from contract")
        if "linkedTo" in exp:
            expected_targets = (
                all_entity_names if exp["linkedTo"] == ["*"] else sorted(exp["linkedTo"])
            )
            actual_targets = sorted(act.referenced_entities)
            if actual_targets != expected_targets:
                reasons.append(
                    f"referenced entities {actual_targets} != expected {expected_targets}"
                )
        if reasons:
            different.append({"name": rule_name, "reasons": reasons})

    extra = sorted(actual_rules.keys() - matched_actual_names)
    return {"missing": missing, "extra": extra, "different": different}


def check_conformance(contract_path: Path, definition_dir: Path) -> dict[str, Any]:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    parts = load_definition_parts(definition_dir)
    contract_entities = {item["name"] for item in contract["entities"]}

    tables = _parse_tables(parts)
    entities = _parse_entities(parts, tables, contract_entities)
    tom_rels = _parse_tom_relationships(parts, tables)
    entity_rels = _parse_entity_relationships(parts, entities, tom_rels, contract_entities)
    explicit_metrics = _parse_explicit_metrics(parts)
    rules = _parse_rules(parts, contract_entities)

    entity_diff = _compare_entities(contract, entities, tables)
    rel_diff = _compare_relationships(contract, entity_rels)
    metric_diff = _compare_metrics(contract, tables, entities, explicit_metrics)
    rule_diff = _compare_rules(contract, rules)

    sections = {
        "entities": entity_diff,
        "relationships": rel_diff,
        "metrics": metric_diff,
        "rules": rule_diff,
    }
    missing_total = sum(len(sec["missing"]) for sec in sections.values())
    extra_total = sum(len(sec["extra"]) for sec in sections.values())
    different_total = sum(len(sec["different"]) for sec in sections.values())
    conformant = (missing_total + extra_total + different_total) == 0

    return {
        "conformant": conformant,
        "contract": str(contract_path),
        "definitionDir": str(definition_dir),
        "summary": {
            "missing": missing_total,
            "extra": extra_total,
            "different": different_total,
        },
        **sections,
    }


def format_text_report(report: dict[str, Any]) -> str:
    status = "CONFORMANT" if report["conformant"] else "NON-CONFORMANT"
    summary = report["summary"]
    lines = [
        f"Ontology conformance: {status}",
        f"Contract: {report['contract']}",
        f"Definition: {report['definitionDir']}",
        (
            f"Summary: missing={summary['missing']}, extra={summary['extra']}, "
            f"different={summary['different']}"
        ),
    ]
    for section_name in ("entities", "relationships", "metrics", "rules"):
        section = report[section_name]
        if not (section["missing"] or section["extra"] or section["different"]):
            lines.append(f"[{section_name}] ok")
            continue
        lines.append(f"[{section_name}]")
        for item in section["missing"]:
            lines.append(f"  MISSING: {item}")
        for item in section["extra"]:
            lines.append(f"  EXTRA: {item}")
        for diff in section["different"]:
            label = diff.get("name") or diff.get("relationship")
            joined = "; ".join(diff["reasons"])
            lines.append(f"  DIFFERENT: {label} ({joined})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True, help="Path to ontology contract JSON.")
    parser.add_argument(
        "--definition-dir",
        type=Path,
        required=True,
        help="Path to exported ontology TMDL definition directory (or getDefinition JSON file).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="output_format",
        help="Report output format (default: text).",
    )
    args = parser.parse_args(argv)

    if not args.contract.is_file():
        print(f"Contract file not found: {args.contract}", file=sys.stderr)
        return 2
    if not args.definition_dir.exists():
        print(f"Definition path not found: {args.definition_dir}", file=sys.stderr)
        return 2

    report = check_conformance(args.contract, args.definition_dir)
    if args.output_format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(format_text_report(report))
    return 0 if report["conformant"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
