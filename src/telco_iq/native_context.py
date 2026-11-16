"""Read the reviewed generation-2 capture shape, never the recorded display DTO.

This is a deliberately bounded decoder, not a general TMDL interpreter. Author
getDefinition receipts are diagnostic evidence, not delegated-reader grants.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, ValidationError

from telco_iq.approval import private_config_path
from telco_iq.catalog import Catalog, compatible_populations, digest
from telco_iq.contracts import (
    Context,
    ContextNode,
    Contract,
    JoinNode,
    Plan,
    QueryNode,
    Relationship,
    Scope,
    SourcePopulation,
    Status,
)
from telco_iq.errors import LabError

if TYPE_CHECKING:
    from telco_iq.reader_native_context import ReaderNativeContext

DEFINITION_CONTRACT = (
    "https://learn.microsoft.com/en-us/rest/api/fabric/articles/"
    "item-management/definitions/ontology-definition"
)
TOOLS = ("list_ontology_entities", "list_ontology_rules")
NAME = r"(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_ -]*)"
COLUMN_REF = re.compile(rf"({NAME})\.({NAME})")
PROXY = re.compile(
    r'EXTERNALMEASURE\("((?:[^"]|"")+)", (INTEGER|DOUBLE|CURRENCY|STRING|BOOLEAN|DATETIME|VARIANT), '
    r'"DirectQuery to AS - ([^"/]+)/([^"/]+)"\)'
)


class ReviewedModel(Contract):
    alias: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_name: str = Field(min_length=1)
    review_reference: str = Field(min_length=1)


class ReviewedExecutionRelationship(Contract):
    """A separate reviewed traversal of one captured native edge, never a shortcut."""

    native_relationship: str = Field(min_length=1)
    relationship: Relationship
    review_reference: str = Field(min_length=1)


class NativeSnapshot(Contract):
    snapshot_version: Literal["native-planning-v1"] = "native-planning-v1"
    capture_lane: Literal["LIVE_DEV_AUTHOR_DIAGNOSTIC"]
    captured_at: datetime
    authorization_reference: str = Field(min_length=1)
    reader_verified: Literal[False] = False
    context: Context
    sources: list[ReviewedModel]
    entities: list[dict]
    native_relationships: list[dict]
    metrics: list[dict]
    annotation_guidance: list[dict]
    execution_contracts: list[ReviewedExecutionRelationship]
    evidence_hashes: dict[str, str]
    blockers: list[str]
    content_hash: str = ""

    def verify(self, authorized_scope: Scope) -> None:
        if self.context.scope != authorized_scope:
            raise LabError("Snapshot authorization does not match current reader", Status.BLOCKED_AUTH)
        if authorized_scope.mode != "OFFLINE_SYNTHETIC":
            raise LabError(
                "Author diagnostic capture is not live-reader authorization; reader verification NOT_RUN",
                Status.BLOCKED_AUTH,
            )
        if (
            self.context.mode != "NATIVE_SNAPSHOT" or not self.context.provenance
            or not self.context.schema_hash or self.captured_at.tzinfo is None
            or set(self.evidence_hashes) != {
                "definition", "native_mcp_receipt", "catalog", "reviewed_models", "execution_reviews",
            }
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.evidence_hashes.values())
            or self.context.relationships != [review.relationship for review in self.execution_contracts]
            or {source.alias for source in self.sources} != set(authorized_scope.models)
            or self.content_hash != digest(self.model_dump(mode="json", exclude={"content_hash"}))
        ):
            raise LabError("Native snapshot schema, provenance or content hash is invalid", Status.BLOCKED_CAPABILITY)
        _normalized_rules(self.context.rules, {entity["id"] for entity in self.entities})


@dataclass
class _Block:
    text: str
    children: list[_Block] = field(default_factory=list)
    description: str = ""


def _fail(message: str) -> None:
    raise LabError(message, Status.BLOCKED_CAPABILITY)


def _name(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(NAME, value):
        _fail("Unsupported TMDL identifier")
    return value[1:-1].replace("''", "'") if value.startswith("'") else value


def _blocks(text: str) -> list[_Block]:
    root = _Block("")
    stack = [(-1, root)]
    comments: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("///"):
            comments.append(stripped[3:].strip())
            continue
        indent = len(line.expandtabs(4)) - len(line.lstrip().expandtabs(4))
        while stack[-1][0] >= indent:
            stack.pop()
        node = _Block(stripped, description=" ".join(comments))
        comments = []
        stack[-1][1].children.append(node)
        stack.append((indent, node))
    return root.children


def _children(node: _Block, prefix: str) -> list[_Block]:
    return [child for child in node.children if child.text.startswith(prefix)]


def _one(nodes: list, description: str):
    if len(nodes) != 1:
        _fail(f"Missing or ambiguous {description}")
    return nodes[0]


def _value(node: _Block, key: str) -> str:
    child = _one(_children(node, key + ": "), key)
    if child.children:
        _fail(f"Unsupported nested {key}")
    return child.text[len(key) + 2:]


def _optional(node: _Block, key: str) -> str | None:
    return _value(node, key) if _children(node, key + ": ") else None


def _known(node: _Block, prefixes: tuple[str, ...]) -> None:
    if any(not child.text.startswith(prefixes) for child in node.children):
        _fail("Unsupported captured TMDL shape; review new fields before use")


def _annotations(node: _Block) -> dict[str, str]:
    result = {}
    for child in _children(node, "annotation "):
        match = re.fullmatch(r"annotation ([A-Za-z0-9_]+) = (.+)", child.text)
        if not match or child.children or match[1] in result:
            _fail("Unsupported or duplicate annotation")
        result[match[1]] = match[2]
    return result


def _annotation_values(values: list) -> dict[str, str]:
    result = {}
    for item in values:
        if (
            not isinstance(item, dict) or set(item) != {"name", "value"}
            or not isinstance(item["name"], str) or not isinstance(item["value"], str)
            or item["name"] in result
        ):
            _fail("Unsupported or duplicate MCP annotation")
        result[item["name"]] = item["value"]
    return result


def _reference(value: str) -> tuple[str, str]:
    match = COLUMN_REF.fullmatch(value)
    if not match:
        _fail("Unsupported relationship/property column reference")
    return _name(match[1]), _name(match[2])


def _unique(items: list[dict], key: str) -> dict[str, dict]:
    result = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get(key), str) or item[key] in result:
            _fail("Missing or ambiguous captured object identity")
        result[item[key]] = item
    return result


def native_entities_fingerprint(values: list[dict]) -> str:
    """Only uniquely named entity/property collections are unordered; retain every field/value."""
    entities = _unique(deepcopy(values), "name")
    for entity in entities.values():
        properties = _unique(entity.get("properties"), "name")
        entity["properties"] = [properties[name] for name in sorted(properties)]
    return digest([entities[name] for name in sorted(entities)])


def _raw_rules(values: list[dict]) -> list[dict]:
    if not isinstance(values, list):
        _fail("Unsupported formal native Rule collection")
    if any(not isinstance(rule, dict) or not isinstance(rule.get("name"), str) for rule in values):
        _fail("Unsupported formal native Rule identity")
    rules = _unique(deepcopy(values), "name")
    lineages = set()
    for rule in rules.values():
        if set(rule) != {
            "lineageTag", "name", "description", "statement", "referencedEntities",
            "referencedRelationships", "synonyms", "annotations",
        } or any(not isinstance(rule[key], str) for key in ("lineageTag", "name", "description", "statement")):
            _fail("Unsupported formal native Rule shape")
        if (
            not rule["name"].strip() or not rule["lineageTag"].strip() or not rule["statement"].strip()
            or len(rule["statement"]) > 4000 or rule["lineageTag"] in lineages
            or rule["referencedRelationships"] != [] or rule["synonyms"] != [] or rule["annotations"] != []
            or not isinstance(rule["referencedEntities"], list) or not rule["referencedEntities"]
        ):
            _fail("Unreviewed formal native Rule values or reference kinds")
        lineages.add(rule["lineageTag"])
        references = {}
        for reference in rule["referencedEntities"]:
            if (
                not isinstance(reference, dict) or set(reference) != {"entity", "propertyScope"}
                or not isinstance(reference["entity"], str) or not reference["entity"].strip()
                or reference["entity"] in references or reference["propertyScope"] != "None"
            ):
                _fail("Unreviewed formal native Rule entity/property scope")
            references[reference["entity"]] = reference
        rule["referencedEntities"] = [references[name] for name in sorted(references)]
    return [rules[name] for name in sorted(rules)]


def native_rules_fingerprint(values: list[dict]) -> str:
    """Only reviewed named Rules/entity references are unordered; None is not all properties."""
    return digest(_raw_rules(values))


def _normalized_rules(values: list[dict[str, str]], entity_ids: set[str]) -> None:
    rules = _unique(values, "id")
    for rule in rules.values():
        if set(rule) != {
            "id", "statement", "description", "referenced_entities", "property_scope",
            "referenced_relationships", "native_part", "native_part_hash",
        } or any(not isinstance(value, str) for value in rule.values()):
            _fail("Unsupported normalized formal native Rule")
        if (
            not rule["id"].strip() or not rule["statement"].strip() or len(rule["statement"]) > 4000
            or rule["property_scope"] != "None" or rule["referenced_relationships"] != "[]"
            or rule["native_part"] != f"rules/{rule['id']}.tmdl"
            or not re.fullmatch(r"[0-9a-f]{64}", rule["native_part_hash"])
        ):
            _fail("Normalized formal native Rule provenance/scope differs")
        try:
            references = json.loads(rule["referenced_entities"])
        except json.JSONDecodeError as error:
            raise LabError("Invalid formal native Rule entity references", Status.BLOCKED_CAPABILITY) from error
        if (
            not isinstance(references, list) or not references
            or any(not isinstance(reference, str) or reference not in entity_ids for reference in references)
            or len(set(references)) != len(references)
            or references != sorted(references)
        ):
            _fail("Unresolved or ambiguous formal native Rule entity references")


def _parts(definition: dict) -> dict[str, str]:
    if set(definition) != {"definition"} or set(definition["definition"]) != {"parts"}:
        _fail("Unsupported getDefinition envelope")
    parts = {}
    for part in definition["definition"]["parts"]:
        if set(part) != {"path", "payload", "payloadType"} or part["payloadType"] != "InlineBase64":
            _fail("Unsupported definition part encoding")
        path = part["path"]
        if (
            not isinstance(path, str) or path in parts
            or not re.fullmatch(
                r"(?:\.platform|(?:database|model|relationships|entityRelationships|expressions)\.tmdl"
                r"|(?:tables|entities|namespaces|rules)/[^/\\]+\.tmdl)", path
            )
        ):
            _fail("Unknown, duplicate or unsupported definition part; projected metrics/rules need review")
        parts[path] = base64.b64decode(part["payload"], validate=True).decode("utf-8-sig")
    required = {".platform", "database.tmdl", "model.tmdl", "namespaces/default.tmdl"}
    if not required <= parts.keys():
        _fail("Incomplete generation-2 definition")
    platform = json.loads(parts[".platform"])
    if platform["metadata"]["type"] != "Ontology" or platform["config"]["version"] != "2.0":
        _fail("Not a generation-2 ontology definition")
    database = _one(_blocks(parts["database.tmdl"]), "database")
    if database.text != "database" or _value(database, "compatibilityLevel") != "1000000":
        _fail("Unsupported ontology compatibility level")
    namespace = _one(_blocks(parts["namespaces/default.tmdl"]), "default namespace")
    _known(namespace, ("lineageTag: ",))
    if namespace.text != "namespace default" or any(
        path.startswith("namespaces/") and path != "namespaces/default.tmdl" for path in parts
    ):
        _fail("Additional namespace shapes require review")
    model = _blocks(parts["model.tmdl"])
    if (
        not model or model[0].text != "model Model" or model[0].children
        or any(not node.text.startswith(("ref table ", "ref entity ", "ref rule ", "ref namespace default"))
               or node.children for node in model[1:])
        or sum(node.text == "ref namespace default" for node in model) != 1
    ):
        _fail("Unsupported model definition shape")
    refs = [tuple(node.text[4:].split(" ", 1)) for node in model[1:]]
    resolved_refs = [(kind, _name(name)) for kind, name in refs]
    expected_refs = {
        (kind, path.split("/", 1)[1][:-5])
        for path in parts for folder, kind in (("tables/", "table"), ("entities/", "entity"), ("rules/", "rule"))
        if path.startswith(folder)
    } | {("namespace", "default")}
    if len(set(resolved_refs)) != len(resolved_refs) or set(resolved_refs) != expected_refs:
        _fail("Model references disagree with captured definition parts")
    return parts


def _captured_values(receipt: dict, definition: dict) -> tuple[list, list, str]:
    if receipt.get("lane") != "LIVE_DEV_AUTHOR_DIAGNOSTIC":
        _fail("Unsupported capture lane; display DTOs are not executable native evidence")
    if receipt.get("definitionReceipt") != digest(definition) + ".json":
        _fail("Definition provenance hash does not match the native receipt")
    inventory = receipt["inventory"]
    if (
        inventory["lane"] != "ontology" or inventory["protocol"] != "2025-06-18"
        or inventory["provenance"] != "ACTUAL_MCP_INITIALIZE_AND_TOOLS_LIST"
    ):
        _fail("Missing actual native MCP inventory provenance")
    schemas = {}
    for tool in inventory["tools"]:
        definition = tool["definition"]
        name = definition["name"]
        # Tool.model_dump(exclude_none=True) is the inventory's hash contract.
        clean = {key: value for key, value in definition.items() if value is not None}
        if name not in TOOLS or name in schemas or digest(clean) != tool["schema_hash"]:
            _fail("Unreviewed or inconsistent native tool schema")
        schemas[name] = tool["schema_hash"]
    if set(schemas) != set(TOOLS) or set(receipt["rawNativeContext"]) != set(TOOLS):
        _fail("Incomplete native tool capture")
    values = {}
    for name in TOOLS:
        result = receipt["rawNativeContext"][name]
        if (
            result["isError"] is not False or set(result["structuredContent"]) != {"values"}
            or not isinstance(result["structuredContent"]["values"], list)
        ):
            _fail("Unsupported native structuredContent.values shape")
        values[name] = result["structuredContent"]["values"]
    rules = _raw_rules(values["list_ontology_rules"])
    return values["list_ontology_entities"], rules, digest(schemas)


def _source_expression(node: _Block, server: str, database: str) -> None:
    def lines(block: _Block) -> list[str]:
        return [block.text] + [line for child in block.children for line in lines(child)]

    body = [line for child in node.children if not child.text.startswith("lineageTag: ")
            for line in lines(child)]
    server, database = server.replace('"', '""'), database.replace('"', '""')
    expected = [
        "let", f'Source = AnalysisServices.Database("{server}", "{database}"),',
        "Cubes = Table.Combine(Source[Data]),", 'Cube = Cubes{[Id="Model", Kind="Cube"]}[Data]',
        "in", "Cube",
    ]
    if body != expected:
        _fail("Unreviewed DirectQuery source expression or inconsistent Dataset identity")


def _tables(parts: dict[str, str], sources: list[ReviewedModel], catalog: Catalog) -> dict:
    identities = {(s.workspace_id, s.dataset_id): s for s in sources}
    tables = {}
    for path, text in parts.items():
        if not path.startswith("tables/"):
            continue
        table = _one(_blocks(text), "backing table")
        if not table.text.startswith("table "):
            _fail("Unsupported backing table")
        name = _name(table.text[6:])
        if name in tables or path != f"tables/{name}.tmdl":
            _fail("Duplicate or mismatched backing table")
        _known(table, ("lineageTag: ", "column ", "measure ", "partition "))
        partition = _one(_children(table, "partition "), "Dataset partition")
        _known(partition, ("mode: ", "source", "annotation "))
        if not partition.text.endswith(" = entity") or _value(partition, "mode") != "directQuery":
            _fail("Only captured Dataset/DirectQuery backing is reviewed")
        annotations = _annotations(partition)
        source = identities.get((annotations["ONT_WorkspaceId"], annotations["ONT_ItemId"]))
        if source is None:
            _fail("Unresolved source ID; explicit reviewed model mapping required")
        if (
            annotations["ONT_ItemKind"] != "Dataset" or annotations["ONT_ItemName"] != source.dataset_name
            or annotations["ONT_SemanticModelDatabase"] != source.dataset_name
            or annotations["ONT_SemanticModelServer"] != f"powerbi://api.powerbi.com/v1.0/{source.workspace_id}"
        ):
            _fail("Dataset source identity annotations disagree")
        binding = _one([c for c in partition.children if c.text == "source"], "partition source")
        _known(binding, ("entityName: ", "expressionSource: "))
        source_table = _value(binding, "entityName")
        expression = _name(_value(binding, "expressionSource"))
        expected_expression = f"DirectQuery to AS - {source.workspace_id}/{source.dataset_id}"
        if expression != expected_expression:
            _fail("DirectQuery expression and Dataset identity disagree")
        expressions = [
            n for n in _blocks(parts.get("expressions.tmdl", ""))
            if n.text == f"expression '{expected_expression}' ="
        ]
        _source_expression(
            _one(expressions, "source expression"), annotations["ONT_SemanticModelServer"], source.dataset_name,
        )
        matches = [
            catalog.tables[t] for t in catalog.models[source.alias]["tables"]
            if catalog.tables[t]["displayName"] == source_table
        ]
        spec = _one(matches, "source table membership")
        columns = {}
        for col in _children(table, "column "):
            _known(col, ("dataType: ", "lineageTag: ", "sourceColumn: ", "formatString: "))
            col_name = _name(col.text[7:])
            source_column = _value(col, "sourceColumn")
            dtype = _value(col, "dataType")
            expected = {"date": "dateTime", "timestamp": "dateTime", "bool": "boolean"}.get(
                spec["columns"].get(source_column), spec["columns"].get(source_column),
            )
            if col_name in columns or expected is None or dtype != expected:
                _fail("Ambiguous or incompatible source column binding")
            columns[col_name] = source_column
        if set(columns.values()) != set(spec["columns"]) or len(set(columns.values())) != len(columns):
            _fail("Incomplete/ambiguous backing schema; no unobserved columns may be authorized")
        tables[name] = {
            "node": table, "source": source, "table": spec["id"], "source_table": source_table,
            "columns": columns, "part": path, "part_hash": digest(text),
        }
    return tables


def _entities(values: list, parts: dict[str, str], tables: dict) -> tuple[list[dict], list[dict]]:
    raw = _unique(values, "name")
    definitions = {}
    for path, text in parts.items():
        if not path.startswith("entities/"):
            continue
        node = _one(_blocks(text), "entity")
        if not node.text.startswith("entity "):
            _fail("Unsupported entity declaration")
        name = _name(node.text[7:])
        if name in definitions or path != f"entities/{name}.tmdl":
            _fail("Duplicate or mismatched entity")
        definitions[name] = (node, path, digest(text))
    if set(raw) != set(definitions):
        _fail("MCP entities and definition do not agree")
    entities, guidance = [], []
    for name, entity in raw.items():
        allowed = {
            "name", "description", "lineageTag", "backingTable", "keyProperties", "properties",
            "additionalBackingTables", "annotations", "overriddenMetadataFields", "resourceLinks", "synonyms",
        }
        if set(entity) != allowed or any(
            entity[k] for k in ("additionalBackingTables", "overriddenMetadataFields", "resourceLinks")
        ):
            _fail("Unsupported native entity shape")
        node, path, part_hash = definitions[name]
        _known(node, ("lineageTag: ", "backingTable: ", "keyProperty: ", "property ", "synonym ", "annotation "))
        backing_name = _name(_value(node, "backingTable"))
        if backing_name != entity["backingTable"] or backing_name not in tables:
            _fail("Unresolved entity backing table")
        backing = tables[backing_name]
        annotations = _annotation_values(entity["annotations"])
        if annotations != _annotations(node) or annotations.get("source_model") not in (
            None, backing["source"].dataset_name,
        ):
            _fail("Entity source annotations disagree with Dataset identity")
        keys = [_name(c.text[len("keyProperty: "):]) for c in _children(node, "keyProperty: ")]
        if keys != entity["keyProperties"] or len(set(keys)) != len(keys):
            _fail("Entity key metadata differs between captures")
        properties = _unique(entity["properties"], "name")
        property_nodes = {}
        for prop in _children(node, "property "):
            pname = _name(prop.text[9:])
            if pname in property_nodes:
                _fail("Duplicate property")
            property_nodes[pname] = prop
        if set(properties) != set(property_nodes) or not set(keys) <= properties.keys():
            _fail("Unresolved entity property/key")
        normalized_properties = []
        for pname, prop in properties.items():
            if set(prop) != {
                "annotations", "backingConfiguration", "dataType", "description", "lineageTag",
                "name", "overriddenMetadataFields", "synonyms",
            } or prop["annotations"] or prop["overriddenMetadataFields"]:
                _fail("Unsupported native property shape")
            pnode = property_nodes[pname]
            _known(pnode, ("lineageTag: ", "dataType: ", "backingConfiguration", "synonym "))
            config = _one([c for c in pnode.children if c.text == "backingConfiguration"], "property binding")
            _known(config, ("valueColumn: ",))
            table_name, col_name = _reference(_value(config, "valueColumn"))
            expected = {"type": "default", "valueColumn": {"table": table_name, "column": col_name}}
            if (
                prop["backingConfiguration"] != expected or table_name != backing_name
                or col_name not in backing["columns"] or prop["dataType"] != _value(pnode, "dataType")
            ):
                _fail("Unresolved or conflicting property backing")
            normalized_properties.append({
                "name": pname, "source_column": backing["columns"][col_name], "data_type": prop["dataType"],
                "description": prop["description"], "synonyms": prop["synonyms"],
            })
        for key, value in annotations.items():
            if key == "rule" or key.startswith("rule_"):
                guidance.append({
                    "kind": "native_annotation", "target_kind": "entity", "target": name,
                    "name": key, "statement": value, "part": path, "part_hash": part_hash,
                })
        entities.append({
            "id": name, "model": backing["source"].alias, "table": backing["table"],
            "backing_table": backing_name, "description": entity["description"],
            "keys": keys, "properties": normalized_properties, "synonyms": entity["synonyms"],
            "part": path, "part_hash": part_hash, "backing_part_hash": backing["part_hash"],
        })
        mapped = [p["source_column"] for p in normalized_properties]
        if len(set(mapped)) != len(mapped) or set(mapped) != set(backing["columns"].values()):
            _fail("Incomplete or ambiguous entity property bindings")
    backing_names = [e["backing_table"] for e in entities]
    if len(set(backing_names)) != len(backing_names) or set(backing_names) != set(tables):
        _fail("Incomplete or ambiguous entity-to-table bindings")
    return entities, guidance


def _rules(values: list[dict], parts: dict[str, str], entities: list[dict]) -> list[dict[str, str]]:
    raw = {rule["name"]: rule for rule in _raw_rules(values)}
    definitions = {}
    for path, text in parts.items():
        if not path.startswith("rules/"):
            continue
        node = _one(_blocks(text), "formal native Rule")
        if not node.text.startswith("rule "):
            _fail("Unsupported formal native Rule declaration")
        name = _name(node.text[5:])
        if name in definitions or path != f"rules/{name}.tmdl":
            _fail("Duplicate or mismatched formal native Rule")
        _known(node, ("statement: ", "lineageTag: ", "ruleReferencedEntity "))
        definitions[name] = (node, path, digest(text))
    if set(definitions) != set(raw):
        _fail("MCP formal Rules and definition do not agree")
    normalized = []
    for name in sorted(raw):
        rule = raw[name]
        node, path, part_hash = definitions[name]
        references = []
        for reference in _children(node, "ruleReferencedEntity "):
            if reference.children:
                _fail("Unreviewed formal native Rule property reference scope")
            references.append(_name(reference.text[len("ruleReferencedEntity "):]))
        if (
            _value(node, "statement") != rule["statement"] or node.description != rule["description"]
            or _value(node, "lineageTag") != rule["lineageTag"]
            or len(set(references)) != len(references)
            or set(references) != {reference["entity"] for reference in rule["referencedEntities"]}
        ):
            _fail("Formal native Rule statement/identity/references differ between captures")
        normalized.append({
            "id": name, "statement": rule["statement"], "description": rule["description"],
            "referenced_entities": json.dumps(sorted(references), separators=(",", ":")),
            "property_scope": "None", "referenced_relationships": "[]",
            "native_part": path, "native_part_hash": part_hash,
        })
    _normalized_rules(normalized, {entity["id"] for entity in entities})
    return normalized


def _metrics(tables: dict, entities: list[dict], catalog: Catalog) -> list[dict]:
    metrics = []
    seen = set()
    for backing_name, backing in tables.items():
        for measure in _children(backing["node"], "measure "):
            _known(measure, ("formatString: ", "displayFolder: ", "lineageTag: ", "sourceLineageTag: ", "annotation "))
            match = re.fullmatch(rf"measure ({NAME}) = (.+)", measure.text)
            if not match or not (proxy := PROXY.fullmatch(match[2])):
                _fail("Unsupported measure: source EXTERNALMEASURE proxy required, not author DAX")
            annotations = _annotations(measure)
            links = [value for key, value in annotations.items() if key.startswith("measure_")]
            link = json.loads(_one(links, "source measure link"))
            if set(link) != {"w", "i", "n", "t", "m", "d"}:
                _fail("Unsupported measure source-link annotation")
            source = backing["source"]
            if (
                (link["w"], link["i"], link["n"], link["t"])
                != (source.workspace_id, source.dataset_id, source.dataset_name, backing["source_table"])
                or (proxy[1].replace('""', '"'), proxy[3], proxy[4])
                != (link["m"], source.workspace_id, source.dataset_id)
                or _name(match[1]) != link["m"]
            ):
                _fail("Metric source/home-table identity mismatch")
            spec = _one([
                m for m in catalog.metrics.values()
                if (m["model"], m["table"], m["name"]) == (source.alias, backing["table"], link["m"])
            ], "reviewed source metric")
            if (
                annotations.get("MetricId") != spec["id"]
                or annotations.get("Aggregation") != spec["aggregation"] or spec["id"] in seen
            ):
                _fail("Conflicting or ambiguous source metric contract")
            home = _one([e for e in entities if e["backing_table"] == backing_name], "metric home entity")
            seen.add(spec["id"])
            metrics.append({
                "id": spec["id"], "model": source.alias, "table": backing["table"], "entity": home["id"],
                "name": link["m"], "description": link["d"], "representation": "EXTERNALMEASURE",
                "calculation_owner": "source_semantic_model",
                "part": backing["part"], "part_hash": backing["part_hash"],
            })
    return metrics


def _relationships(parts: dict[str, str], entities: list[dict], tables: dict) -> tuple[list[dict], list[dict]]:
    by_entity = _unique(entities, "id")
    backing = {}
    for node in _blocks(parts.get("relationships.tmdl", "")):
        if not node.text.startswith("relationship "):
            _fail("Unsupported backing relationship")
        _known(node, (
            "fromColumn: ", "toColumn: ", "fromCardinality: ", "toCardinality: ", "type: ",
            "isActive: ", "crossFilteringBehavior: ", "joinOnDateBehavior: ", "lineageTag: ",
        ))
        name = _name(node.text[13:])
        if name in backing or _optional(node, "type") not in (None, "singleColumn"):
            _fail("Duplicate/unsupported backing relationship")
        backing[name] = node
    result, guidance, used = [], [], set()
    for node in _blocks(parts.get("entityRelationships.tmdl", "")):
        if not node.text.startswith("entityRelationship "):
            _fail("Unsupported native relationship")
        _known(node, (
            "label: ", "lineageTag: ", "fromEntity: ", "toEntity: ", "backingConfiguration",
            "synonym ", "annotation ",
        ))
        name = _name(node.text[len("entityRelationship "):])
        left = by_entity.get(_name(_value(node, "fromEntity")))
        right = by_entity.get(_name(_value(node, "toEntity")))
        if left is None or right is None:
            _fail("Unresolved native relationship endpoint")
        config = _one([c for c in node.children if c.text == "backingConfiguration"], "relationship binding")
        _known(config, ("relationship: ",))
        reference = _name(_value(config, "relationship"))
        if reference not in backing or reference in used:
            _fail("Unresolved or ambiguous native relationship backing")
        used.add(reference)
        bnode = backing[reference]
        ltable, lcol = _reference(_value(bnode, "fromColumn"))
        rtable, rcol = _reference(_value(bnode, "toColumn"))
        if ltable != left["backing_table"] or rtable != right["backing_table"]:
            _fail("Native edge and backing relationship endpoints disagree")
        if lcol not in tables[ltable]["columns"] or rcol not in tables[rtable]["columns"]:
            _fail("Unobserved native relationship column")
        cardinalities = [_optional(bnode, k) for k in ("fromCardinality", "toCardinality")]
        if any(c not in (None, "one", "many") for c in cardinalities):
            _fail("Unsupported native cardinality")
        path = "entityRelationships.tmdl"
        for key, value in _annotations(node).items():
            if key == "rule" or key.startswith("rule_"):
                guidance.append({
                    "kind": "native_annotation", "target_kind": "relationship", "target": name,
                    "name": key, "statement": value, "part": path, "part_hash": digest(parts[path]),
                })
        result.append({
            "id": name, "from_entity": left["id"], "to_entity": right["id"],
            "left_model": left["model"], "right_model": right["model"],
            "left_table": left["table"], "right_table": right["table"],
            "left_keys": [tables[ltable]["columns"][lcol]], "right_keys": [tables[rtable]["columns"][rcol]],
            "from_cardinality": cardinalities[0], "to_cardinality": cardinalities[1],
            "cardinality_evidence": "captured explicit values; absent values are elided defaults, NOT inferred",
            "backing_relationship": reference, "description": node.description,
            "synonyms": [_name(c.text[8:]) for c in _children(node, "synonym ")],
            "is_active": _optional(bnode, "isActive"),
            "cross_filtering": _optional(bnode, "crossFilteringBehavior"),
            "join_on_date": _optional(bnode, "joinOnDateBehavior"),
            "part": path, "part_hash": digest(parts[path]),
            "backing_part_hash": digest(parts["relationships.tmdl"]),
        })
    _unique(result, "id")
    if used != backing.keys():
        _fail("Unlinked backing relationships require explicit review")
    return result, guidance


def _edge_traversal(relationship: Relationship, edge: dict) -> str | None:
    fields = ("model", "table", "keys")
    if all(getattr(relationship, f"{side}_{field}") == edge[f"{side}_{field}"]
           for side in ("left", "right") for field in fields):
        return "forward"
    if all(getattr(relationship, f"{side}_{field}") == edge[f"{other}_{field}"]
           for side, other in (("left", "right"), ("right", "left")) for field in fields):
        return "reverse"
    return None


def _execution_relationships(
    reviews: list[ReviewedExecutionRelationship], edges: list[dict], catalog: Catalog,
) -> list[Relationship]:
    by_id = _unique(edges, "id")
    review_by_id = {item.relationship.id: item for item in reviews}
    ids, used, result = set(), set(), []
    for review in reviews:
        edge = by_id.get(review.native_relationship)
        relationship = review.relationship
        if edge is None or relationship.id in ids:
            _fail("Execution contract must reference one unique captured edge direction, never a fabricated shortcut")
        traversal = _edge_traversal(relationship, edge)
        if traversal is None:
            _fail("Reviewed execution binding differs from captured edge")
        if (review.native_relationship, traversal) in used:
            _fail("Execution contract must reference one unique captured edge direction, never a fabricated shortcut")
        if traversal == "reverse":
            forward = [
                item.relationship for item in reviews if item.native_relationship == review.native_relationship
                and _edge_traversal(item.relationship, edge) == "forward"
            ]
            if len(forward) != 1:
                _fail("Reverse traversal requires one explicit forward execution review")
            base = forward[0]
            temporal = None if base.temporal is None else {
                "left_start": base.temporal.right_start, "left_end": base.temporal.right_end,
                "right_start": base.temporal.left_start, "right_end": base.temporal.left_end,
            }
            if (
                relationship.cardinality != "_to_".join(reversed(base.cardinality.split("_to_")))
                or (relationship.temporal.model_dump() if relationship.temporal else None) != temporal
                or relationship.aligned_filters != base.aligned_filters
                or relationship.null_behavior != base.null_behavior
                or relationship.version != base.version
                or relationship.left_interval_origin != base.right_interval_origin
                or relationship.right_interval_origin != base.left_interval_origin
            ):
                _fail("Reverse traversal must exactly mirror the forward cardinality/temporal/filter contract")
        if (
            not relationship.purposes or not relationship.version or edge["is_active"] == "false"
            or edge["join_on_date"] not in (None, "dateAndTime")
        ):
            _fail("Execution contract is unreviewed or native relationship inactive")
        declared = (edge["from_cardinality"], edge["to_cardinality"])
        if traversal == "reverse":
            declared = declared[::-1]
        expected = tuple(relationship.cardinality.split("_to_"))
        if any(value is not None and value != wanted for value, wanted in zip(declared, expected, strict=True)):
            _fail("Execution cardinality conflicts with explicit native cardinality")
        for side in ("left", "right"):
            table = catalog.tables[getattr(relationship, f"{side}_table")]
            keys = getattr(relationship, f"{side}_keys")
            if expected[0 if side == "left" else 1] == "one" and set(keys) != set(table["primaryKey"]):
                _fail("Reviewed one-side execution keys do not match the source primary key")
            if relationship.temporal:
                origin = getattr(relationship, f"{side}_interval_origin")
                interval_table = table
                if origin is not None:
                    if (
                        origin.model not in catalog.models
                        or origin.table not in catalog.models[origin.model]["tables"]
                        or not origin.via_relationships or not origin.keys
                        or len(set(origin.grain)) != len(origin.grain)
                        or origin.grain != catalog.tables[origin.table]["primaryKey"]
                        or len(set(origin.keys)) != len(origin.keys)
                        or not set(origin.keys) <= catalog.tables[origin.table]["columns"].keys()
                        or len(set(origin.via_relationships)) != len(origin.via_relationships)
                    ):
                        _fail("Carried interval source model/table/grain/path is unreviewed")
                    position = (origin.model, origin.table)
                    endpoint = (getattr(relationship, f"{side}_model"), getattr(relationship, f"{side}_table"))
                    incoming = None
                    for support_id in origin.via_relationships:
                        support = review_by_id.get(support_id)
                        if support is None or support.relationship.id == relationship.id or (
                            support.native_relationship not in by_id or support.relationship.temporal is not None
                            or _edge_traversal(support.relationship, by_id[support.native_relationship]) is None
                        ):
                            _fail("Carried interval requires reviewed non-temporal captured identity edges")
                        step = support.relationship
                        if position == (step.left_model, step.left_table):
                            source_keys = step.left_keys
                            next_position, endpoint_keys = (step.right_model, step.right_table), step.right_keys
                            target_cardinality = step.cardinality.split("_to_")[1]
                        elif position == (step.right_model, step.right_table):
                            source_keys = step.right_keys
                            next_position, endpoint_keys = (step.left_model, step.left_table), step.left_keys
                            target_cardinality = step.cardinality.split("_to_")[0]
                        else:
                            _fail("Carried interval identity traversal path is disconnected")
                        if (
                            source_keys != (origin.keys if incoming is None else incoming)
                            or endpoint_keys != catalog.tables[next_position[1]]["primaryKey"]
                            or target_cardinality != "one"
                            or [catalog.tables[position[1]]["columns"][key] for key in source_keys]
                            != [catalog.tables[next_position[1]]["columns"][key] for key in endpoint_keys]
                        ):
                            _fail("Carried interval path must preserve a typed, uniquely matched identity")
                        position, incoming = next_position, endpoint_keys
                    if position != endpoint or endpoint_keys != keys:
                        _fail("Carried interval path does not reach the exact native endpoint identity")
                    interval_table = catalog.tables[origin.table]
                for bound in ("start", "end"):
                    column = getattr(relationship.temporal, f"{side}_{bound}")
                    if interval_table["columns"].get(column) != "timestamp":
                        _fail("Execution temporal predicate requires reviewed source timestamp columns")
            if not set(relationship.aligned_filters) <= table["columns"].keys():
                _fail("Execution aligned filters are absent from source schema")
        ids.add(relationship.id)
        used.add((review.native_relationship, traversal))
        result.append(relationship)
    return result


def normalize_native_capture(
    definition: dict, receipt: dict, *, authorized_scope: Scope, authorization_reference: str,
    reviewed_models: list[ReviewedModel], catalog: Catalog,
    reviewed_relationships: list[ReviewedExecutionRelationship] | None = None,
) -> NativeSnapshot:
    """Normalize locally with an explicit caller-owned grant, never derive a grant from a capture.

    The currently reviewed author capture can only bind an OFFLINE_SYNTHETIC
    diagnostic scope. A delegated reader adapter/evidence contract is NOT_RUN.
    """
    if authorized_scope.mode != "OFFLINE_SYNTHETIC":
        raise LabError("Author capture cannot prove live-reader authorization", Status.BLOCKED_AUTH)
    if not authorization_reference.strip() or not reviewed_models:
        _fail("Explicit local authorization and reviewed model mappings are required")
    aliases = [m.alias for m in reviewed_models]
    identities = [(m.workspace_id, m.dataset_id) for m in reviewed_models]
    if len(set(aliases)) != len(aliases) or len(set(identities)) != len(identities):
        _fail("Ambiguous reviewed model identity mapping")
    if (
        set(aliases) != set(authorized_scope.models) or not set(aliases) <= catalog.models.keys()
        or len(set(authorized_scope.models)) != len(authorized_scope.models)
    ):
        raise LabError("Reviewed source models do not match the authorized scope", Status.BLOCKED_AUTH)
    if not isinstance(definition, dict) or not isinstance(receipt, dict):
        _fail("Unsupported native capture envelope")
    try:
        parts = _parts(definition)
        values, raw_rules, schema_hash = _captured_values(receipt, definition)
        captured_at = datetime.fromisoformat(receipt["completedAt"])
        if captured_at.tzinfo is None:
            _fail("Capture provenance requires a timezone")
        tables = _tables(parts, reviewed_models, catalog)
        if {t["source"].alias for t in tables.values()} != set(aliases):
            _fail("Reviewed source mapping has no observed backing")
        entities, guidance = _entities(values, parts, tables)
        rules = _rules(raw_rules, parts, entities)
        metrics = _metrics(tables, entities, catalog)
        edges, edge_guidance = _relationships(parts, entities, tables)
        reviews = reviewed_relationships or []
        relationships = _execution_relationships(reviews, edges, catalog)
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise LabError("Unsupported or incomplete native capture shape", Status.BLOCKED_CAPABILITY) from error
    hashes = {
        "definition": digest(definition), "native_mcp_receipt": digest(receipt),
        "catalog": catalog.revision, "reviewed_models": digest([m.model_dump() for m in reviewed_models]),
        "execution_reviews": digest([r.model_dump() for r in reviews]),
    }
    revision = digest({"evidence": hashes, "scope": authorized_scope.model_dump()})
    context = Context(
        mode="NATIVE_SNAPSHOT", revision=revision, scope=authorized_scope,
        source="Generation-2 author diagnostic capture; explicit local normalization, NOT reader proof",
        schema_hash=schema_hash, relationships=relationships, rules=rules,
        provenance=[
            f"getDefinition:{hashes['definition']}", f"native_mcp:{hashes['native_mcp_receipt']}",
            f"captured_at:{captured_at.isoformat()}", f"reviewed_source_contract:{catalog.revision}",
            (
                f"Formal native Rules: {len(rules)}; captured entity-only planning guidance, not execution "
                "policies or proof of managed ask_ontology rule consideration."
                if rules else
                "Formal native Rules: 0; annotations are guidance, not native Rules or execution policies."
            ),
            "Context.relationships are supplemental reviewed execution contracts for captured edges.",
            "Native edges retain multi-edge provenance; no transitive fact-to-fact edge is synthesized.",
            DEFINITION_CONTRACT,
        ],
    )
    snapshot = NativeSnapshot(
        capture_lane=receipt["lane"], captured_at=captured_at,
        authorization_reference=authorization_reference, context=context, sources=reviewed_models,
        entities=entities, native_relationships=edges, metrics=metrics,
        annotation_guidance=guidance + edge_guidance, execution_contracts=reviews, evidence_hashes=hashes,
        blockers=[
            "Live-reader verification NOT_RUN; author getDefinition is not a reader grant.",
            "Source-data/model freshness and live execution NOT_RUN; capture time is not a data watermark.",
            "Unreviewed native edges are planning metadata only, not executable joins.",
            "Absent TOM cardinalities remain elided/unspecified; no default cardinality is guessed.",
        ],
    )
    snapshot.content_hash = digest(snapshot.model_dump(mode="json", exclude={"content_hash"}))
    snapshot.verify(authorized_scope)
    return snapshot


def write_native_snapshot(snapshot: NativeSnapshot, path: Path, *, repo: Path) -> Path:
    """Persist only to a caller-selected private, non-OneDrive, non-repository path."""
    snapshot.verify(snapshot.context.scope)
    target = private_config_path(path, repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(snapshot.model_dump_json(indent=2) + "\n")
    return target


def load_native_bundle(path: Path, authorized_scope: Scope) -> NativeSnapshot:
    try:
        snapshot = NativeSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise LabError("Unreviewed native snapshot schema", Status.BLOCKED_CAPABILITY) from error
    snapshot.verify(authorized_scope)
    return snapshot


def planning_snapshot(
    bundle: NativeSnapshot | ReaderNativeContext, scope: Scope,
) -> NativeSnapshot:
    """Resolve only the explicit diagnostic or acquired-reader contract."""
    from telco_iq.reader_native_context import ReaderNativeContext

    if not isinstance(bundle, (NativeSnapshot, ReaderNativeContext)):
        raise LabError("A diagnostic snapshot or acquired reader bundle is required", Status.BLOCKED_AUTH)
    bundle.verify(scope)
    return bundle if isinstance(bundle, NativeSnapshot) else bundle.snapshot


def validate_native_metric_plan(
    plan: Plan, bundle: NativeSnapshot | ReaderNativeContext, catalog: Catalog,
    *, source_populations: list[SourcePopulation] | None = None,
) -> dict:
    """Validate before execution and return actual metric/edge binding evidence.

    Query-node hashes identify planned uses, not fabricated source execution
    receipts. The executor's real receipts can subsequently be joined by node ID.
    The same validator applies to the equal-information flat and linked arms.
    """
    from telco_iq.executor import validate

    snapshot = planning_snapshot(bundle, plan.principal_scope)
    context = bundle.context
    if (
        snapshot.evidence_hashes["catalog"] != catalog.revision
        or catalog.revision != digest(catalog.raw)
        or plan.ontology_revision != context.revision
    ):
        _fail("Native snapshot/source contract or context revision changed; replan")
    if plan.clarification_state == "required":
        validate(plan, plan.principal_scope, catalog, context)
    metrics = _unique(snapshot.metrics, "id")
    entities = _unique(snapshot.entities, "id")
    sources = {source.alias: source for source in snapshot.sources}
    populations = {}
    if source_populations is not None:
        reviewed_populations = compatible_populations(source_populations, source_populations)
        populations = {entry.model: entry for entry in reviewed_populations}
        if set(populations) != set(sources):
            _fail("Native source population provenance does not cover actual bindings")
        for alias, source in sources.items():
            population = populations[alias]
            if population.binding_hash != digest({
                "workspace_id": source.workspace_id, "dataset_id": source.dataset_id,
            }):
                _fail("Native source population does not bind the actual source model")
            if plan.principal_scope.mode == "LIVE_DELEGATED" and population.evidence_kind != "REVIEWED_LIVE":
                _fail("Live native source population cannot use synthetic provenance")
    selected = set(plan.metric_selections)
    queries = [node for node in plan.nodes if isinstance(node, QueryNode)]
    used = {metric for node in queries for metric in node.metrics}
    if len(selected) != len(plan.metric_selections) or selected != used:
        _fail("Native metric selections must exactly match source query use")
    bindings = []
    for metric_id in sorted(selected):
        metric, spec = metrics.get(metric_id), catalog.metrics.get(metric_id)
        if metric is None or spec is None:
            _fail("Selected metric is not exposed by the native snapshot")
        if any(metric.get(key) != spec[key] for key in ("id", "model", "table", "name")):
            _fail("Native metric source/home-table/name/ID differs from source contract")
        entity = entities.get(metric.get("entity"))
        source = sources.get(metric["model"])
        if (
            entity is None or source is None
            or (entity["model"], entity["table"]) != (metric["model"], metric["table"])
            or metric.get("calculation_owner") != "source_semantic_model"
            or metric.get("representation") != "EXTERNALMEASURE"
            or metric.get("part") != f"tables/{entity['backing_table']}.tmdl"
            or metric.get("part_hash") != entity["backing_part_hash"]
            or not re.fullmatch(r"[0-9a-f]{64}", metric.get("part_hash", ""))
        ):
            _fail("Native metric has no observed source/home-entity capture binding")
        uses = [node for node in queries if metric_id in node.metrics]
        if any((node.model, node.table) != (metric["model"], metric["table"]) for node in uses):
            _fail("Native metric source/home-table does not match query use")
        bindings.append({
            "metric_id": metric_id, "native_entity": metric["entity"],
            "source_model": source.alias, "source_workspace_id": source.workspace_id,
            "source_dataset_id": source.dataset_id, "source_dataset_name": source.dataset_name,
            "home_table": metric["table"], "source_measure": metric["name"],
            "calculation_owner": metric["calculation_owner"],
            "native_part": metric["part"], "native_part_hash": metric["part_hash"],
            "source_review_reference": source.review_reference,
            "query_uses": [
                {"node_id": node.id, "query_contract_hash": digest(node.model_dump(mode="json"))}
                for node in uses
            ],
            **({"source_population": populations[source.alias].model_dump()} if populations else {}),
        })
    for node in plan.nodes:
        if isinstance(node, ContextNode) and node.kind == "SelectMetric" and not set(node.references) <= selected:
            _fail("SelectMetric references must resolve selected native metrics with source query use")
    relationships = _execution_relationships(snapshot.execution_contracts, snapshot.native_relationships, catalog)
    if context.relationships != relationships:
        _fail("Context relationships differ from captured per-edge execution reviews")
    reviews = {review.relationship.id: review for review in snapshot.execution_contracts}
    edges = _unique(snapshot.native_relationships, "id")
    edge_evidence = []
    for node in plan.nodes:
        names = (
            [node.relationship] if isinstance(node, JoinNode)
            else [f.relationship for f in node.cohort_filters if f.relationship]
            if isinstance(node, QueryNode) else []
        )
        for name in names:
            review = reviews.get(name)
            if review is None:
                _fail("Join/cohort reference lacks a reviewed captured native edge")
            edge = edges[review.native_relationship]
            edge_evidence.append({
                "node_id": node.id, "execution_relationship": name,
                "native_relationship": review.native_relationship,
                "execution_traversal": _edge_traversal(review.relationship, edge),
                "review_reference": review.review_reference,
                "execution_contract": review.relationship.model_dump(mode="json"),
                "native_part": edge["part"], "native_part_hash": edge["part_hash"],
                "backing_part_hash": edge["backing_part_hash"],
            })
    if plan.clarification_state == "resolved":
        validate(plan, plan.principal_scope, catalog, context)
    return {
        "snapshot_hash": snapshot.content_hash,
        "context_revision": context.revision, "capture_lane": snapshot.capture_lane,
        "captured_at": snapshot.captured_at.isoformat(),
        "capture_evidence_hashes": dict(snapshot.evidence_hashes),
        "authorization_evidence_hashes": dict(bundle.evidence_hashes),
        "metric_bindings": bindings, "relationship_bindings": edge_evidence,
    }
