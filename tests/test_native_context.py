"""Clearly synthetic generation-2 captures; no private deployment identities."""

import base64
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from telco_iq.catalog import Catalog, digest
from telco_iq.context import load_native_snapshot
from telco_iq.contracts import Relationship, Scope, Status
from telco_iq.errors import LabError
from telco_iq.native_context import (
    ReviewedExecutionRelationship,
    ReviewedModel,
    load_native_bundle,
    normalize_native_capture,
    write_native_snapshot,
)


def synthetic_capture(tmp_path: Path, *, uuid_sources: bool = False, formal_rules: bool = False):
    """Build the observed representation using only conspicuously fictional identities."""
    sources = [
        ReviewedModel(
            alias=alias,
            workspace_id="00000000-0000-0000-0000-000000000001" if uuid_sources else "SYNTHETIC_WORKSPACE",
            dataset_id=(
                f"00000000-0000-0000-0000-{index:012d}" if uuid_sources else f"SYNTHETIC_{alias.upper()}"
            ),
            dataset_name=f"Synthetic {alias}", review_reference="Synthetic unit-test source review",
        )
        for index, alias in enumerate(("subscriptions", "finance"), start=2)
    ]
    catalog_data = {
        "version": "synthetic-test",
        "models": [
            {"id": s.alias, "name": s.dataset_name, "tables": ["account", f"{s.alias}_fact"]}
            for s in sources
        ],
        "tables": [{
            "id": "account", "model": "core", "displayName": "Accounts",
            "grain": ["account_key"], "primaryKey": ["account_key"],
            "columns": {"account_key": "int64", "display_name": "string"},
            "description": "Synthetic registered accounts; names are not keys.",
        }] + [
            {
                "id": f"{s.alias}_fact", "model": s.alias, "displayName": f"{s.alias} Facts",
                "grain": ["account_key", "month"], "primaryKey": ["account_key", "month"],
                "columns": {"account_key": "int64", "month": "date", "value": "int64"},
                "description": "Synthetic monthly facts; one month is not a timestamp interval.",
            }
            for s in sources
        ],
        "metrics": [
            {
                "id": f"{s.alias}.amount", "name": "Amount", "model": s.alias,
                "table": f"{s.alias}_fact", "aggregation": "semi_additive", "unit": "count",
                "description": "Synthetic snapshot; never sum over periods.",
                "dateColumn": "month", "allowedDimensions": ["account_key", "month"],
                "sql": "AUTHOR_ONLY_SQL", "dax": "AUTHOR_ONLY_DAX",
            }
            for s in sources
        ],
    }
    catalog_path = tmp_path / "synthetic-catalog.json"
    catalog_path.write_text(json.dumps(catalog_data), encoding="utf-8")
    catalog = Catalog(catalog_path)
    scope = Scope(
        tenant="SYNTHETIC_LOCAL", principal="SYNTHETIC_REVIEWER", authorization_revision="synthetic-v1",
        models=[s.alias for s in sources], mode="OFFLINE_SYNTHETIC",
    )
    parts = {
        ".platform": json.dumps({"metadata": {"type": "Ontology"}, "config": {"version": "2.0"}}),
        "database.tmdl": "database\n\tcompatibilityLevel: 1000000\n",
        "model.tmdl": "model Model\n\nref namespace default\n",
        "namespaces/default.tmdl": "namespace default\n\tlineageTag: default\n",
    }
    entities, names = [], {}
    for s in sources:
        expression = f"DirectQuery to AS - {s.workspace_id}/{s.dataset_id}"
        parts["expressions.tmdl"] = parts.get("expressions.tmdl", "") + (
            f"expression '{expression}' =\n\tlet\n"
            f'\t\tSource = AnalysisServices.Database("powerbi://api.powerbi.com/v1.0/{s.workspace_id}", '
            f'"{s.dataset_name}"),\n'
            "\t\tCubes = Table.Combine(Source[Data]),\n"
            '\t\tCube = Cubes{[Id="Model", Kind="Cube"]}[Data]\n\tin\n\t\tCube\n\n'
        )
        for table_id in ("account", f"{s.alias}_fact"):
            spec = catalog.tables[table_id]
            backing = f"{s.alias}_{table_id}"
            ename = f"{s.alias} {table_id}"
            names[(s.alias, table_id)] = (ename, backing)
            parts["model.tmdl"] += f"ref table '{backing}'\nref entity '{ename}'\n"
            tmdl = f"table '{backing}'\n"
            if table_id != "account":
                link = {
                    "w": s.workspace_id, "i": s.dataset_id, "n": s.dataset_name,
                    "t": spec["displayName"], "m": "Amount", "d": "Synthetic metric guidance.",
                }
                tmdl += (
                    f'\tmeasure Amount = EXTERNALMEASURE("Amount", INTEGER, "{expression}")\n'
                    f"\t\tannotation MetricId = {s.alias}.amount\n"
                    "\t\tannotation Aggregation = semi_additive\n"
                    f"\t\tannotation measure_Amount = {json.dumps(link)}\n"
                )
            entity_tmdl = f"entity '{ename}'\n\tbackingTable: '{backing}'\n"
            props = []
            for col, dtype in spec["columns"].items():
                native_type = "dateTime" if dtype == "date" else dtype
                tmdl += f"\tcolumn {col}\n\t\tdataType: {native_type}\n\t\tsourceColumn: {col}\n"
                entity_tmdl += (
                    f"\tproperty {col}\n\t\tdataType: {native_type}\n\t\tbackingConfiguration\n"
                    f"\t\t\tvalueColumn: '{backing}'.{col}\n"
                )
                props.append({
                    "name": col, "dataType": native_type, "description": f"Synthetic {col} role.",
                    "lineageTag": f"SYNTHETIC_{backing}_{col}", "annotations": [],
                    "overriddenMetadataFields": [], "synonyms": [],
                    "backingConfiguration": {
                        "type": "default", "valueColumn": {"table": backing, "column": col},
                    },
                })
            keys = ["account_key"] if table_id == "account" else []
            if keys:
                entity_tmdl += "\tkeyProperty: account_key\n"
            annotations = [
                {"name": "source_model", "value": s.dataset_name},
                {"name": "rule_identity", "value": "Synthetic guidance; account keys are not display names."},
            ]
            for annotation in annotations:
                entity_tmdl += f"\tannotation {annotation['name']} = {annotation['value']}\n"
            tmdl += (
                f"\tpartition '{backing}' = entity\n\t\tmode: directQuery\n\t\tsource\n"
                f"\t\t\tentityName: {spec['displayName']}\n\t\t\texpressionSource: '{expression}'\n"
                f"\t\tannotation ONT_WorkspaceId = {s.workspace_id}\n"
                f"\t\tannotation ONT_ItemId = {s.dataset_id}\n"
                "\t\tannotation ONT_ItemKind = Dataset\n"
                f"\t\tannotation ONT_ItemName = {s.dataset_name}\n"
                f"\t\tannotation ONT_SemanticModelDatabase = {s.dataset_name}\n"
                f"\t\tannotation ONT_SemanticModelServer = powerbi://api.powerbi.com/v1.0/{s.workspace_id}\n"
            )
            parts[f"tables/{backing}.tmdl"] = tmdl
            parts[f"entities/{ename}.tmdl"] = entity_tmdl
            entities.append({
                "name": ename, "backingTable": backing, "keyProperties": keys, "properties": props,
                "description": f"Synthetic {table_id} description.", "lineageTag": f"SYNTHETIC_{backing}",
                "additionalBackingTables": [], "annotations": annotations,
                "overriddenMetadataFields": [], "resourceLinks": [], "synonyms": [],
            })
    pairs = [
        ("left-fact-account", ("subscriptions", "subscriptions_fact"), ("subscriptions", "account")),
        ("account-bridge", ("subscriptions", "account"), ("finance", "account")),
        ("right-fact-account", ("finance", "finance_fact"), ("finance", "account")),
    ]
    parts["relationships.tmdl"], parts["entityRelationships.tmdl"] = "", ""
    for rid, left, right in pairs:
        lname, ltable = names[left]
        rname, rtable = names[right]
        parts["relationships.tmdl"] += (
            f"relationship '{rid}_backing'\n\tfromColumn: '{ltable}'.account_key\n"
            f"\ttoColumn: '{rtable}'.account_key\n\n"
        )
        parts["entityRelationships.tmdl"] += (
            f"entityRelationship '{rid}'\n\tfromEntity: '{lname}'\n\ttoEntity: '{rname}'\n"
            f"\tbackingConfiguration\n\t\trelationship: '{rid}_backing'\n"
            "\tannotation rule = Synthetic edge guidance, not an execution policy.\n\n"
        )
    definition = pack(parts)
    tools = []
    for tool_name in ("list_ontology_entities", "list_ontology_rules"):
        schema = {"name": tool_name, "inputSchema": {"type": "object"}, "outputSchema": None}
        tools.append({
            "definition": schema,
            "schema_hash": digest({k: v for k, v in schema.items() if v is not None}),
        })
    receipt = {
        "lane": "LIVE_DEV_AUTHOR_DIAGNOSTIC", "completedAt": "2026-09-30T12:00:00+00:00",
        "definitionReceipt": digest(definition) + ".json",
        "inventory": {
            "lane": "ontology", "protocol": "2025-06-18", "provenance": "ACTUAL_MCP_INITIALIZE_AND_TOOLS_LIST",
            "tools": tools,
        },
        "rawNativeContext": {
            "list_ontology_entities": {"isError": False, "structuredContent": {"values": entities}},
            "list_ontology_rules": {"isError": False, "structuredContent": {"values": []}},
        },
    }
    if formal_rules:
        rule_values = []
        references = [names[("subscriptions", "subscriptions_fact")][0], names[("finance", "finance_fact")][0]]
        for rule_name, statement in (
            ("SyntheticPeriod", "Synthetic period guidance, not an executed source formula."),
            ("SyntheticUnknown", "Synthetic missing observations are not measured zero."),
        ):
            text = (
                f"rule {rule_name}\n\tstatement: {statement}\n\tlineageTag: SYNTHETIC_{rule_name}\n"
                + "".join(f"\n\truleReferencedEntity '{entity}'\n" for entity in references)
            )
            definition["definition"]["parts"].append({
                "path": f"rules/{rule_name}.tmdl", "payloadType": "InlineBase64",
                "payload": base64.b64encode(text.encode()).decode(),
            })
            rule_values.append({
                "lineageTag": f"SYNTHETIC_{rule_name}", "name": rule_name, "description": "",
                "statement": statement, "referencedEntities": [
                    {"entity": entity, "propertyScope": "None"} for entity in references
                ],
                "referencedRelationships": [], "synonyms": [], "annotations": [],
            })
            change_part(definition, receipt, "model.tmdl", lambda value, name=rule_name: value + f"ref rule {name}\n")
        receipt["rawNativeContext"]["list_ontology_rules"]["structuredContent"]["values"] = rule_values
    return definition, receipt, {
        "authorized_scope": scope, "authorization_reference": "Synthetic local test approval, not a live grant",
        "reviewed_models": sources, "catalog": catalog,
    }


def pack(parts: dict[str, str]) -> dict:
    return {"definition": {"parts": [
        {"path": path, "payloadType": "InlineBase64", "payload": base64.b64encode(text.encode()).decode()}
        for path, text in parts.items()
    ]}}


def change_part(definition, receipt, name, change):
    part = next(p for p in definition["definition"]["parts"] if p["path"] == name)
    text = base64.b64decode(part["payload"]).decode()
    part["payload"] = base64.b64encode(change(text).encode()).decode()
    receipt["definitionReceipt"] = digest(definition) + ".json"


def test_capture_preserves_source_home_identity_and_multiedge_provenance(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    assert len(snapshot.entities) == 4
    assert len(snapshot.metrics) == 2
    assert len(snapshot.native_relationships) == 3
    assert len(snapshot.annotation_guidance) == 7
    assert snapshot.context.rules == []
    assert snapshot.context.relationships == []
    assert all(edge["from_cardinality"] is None for edge in snapshot.native_relationships)
    assert not any(
        edge["left_table"].endswith("_fact") and edge["right_table"].endswith("_fact")
        for edge in snapshot.native_relationships
    )
    assert [(m["model"], m["table"], m["entity"]) for m in snapshot.metrics] == [
        ("subscriptions", "subscriptions_fact", "subscriptions subscriptions_fact"),
        ("finance", "finance_fact", "finance finance_fact"),
    ]
    assert all(edge["part_hash"] and edge["backing_part_hash"] for edge in snapshot.native_relationships)
    assert "AUTHOR_ONLY" not in snapshot.model_dump_json()
    assert snapshot.reader_verified is False


def test_reviewed_contract_is_separate_and_cannot_invent_fact_shortcut(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    review = ReviewedExecutionRelationship(
        native_relationship="left-fact-account", review_reference="Synthetic source PK review",
        relationship=Relationship(
            id="reviewed-left-fact", left_model="subscriptions", right_model="subscriptions",
            left_table="subscriptions_fact", right_table="account",
            left_keys=["account_key"], right_keys=["account_key"], cardinality="many_to_one",
            version="synthetic-1", purposes=["Synthetic raw fact to unique account, not implicit aggregation."],
        ),
    )
    snapshot = normalize_native_capture(definition, receipt, **kwargs, reviewed_relationships=[review])
    assert snapshot.context.relationships == [review.relationship]
    assert snapshot.context.rules == []
    assert snapshot.native_relationships[0]["from_cardinality"] is None
    review.relationship.right_model = "finance"
    review.relationship.right_table = "finance_fact"
    with pytest.raises(LabError, match="differs from captured edge"):
        normalize_native_capture(definition, receipt, **kwargs, reviewed_relationships=[review])


@pytest.mark.parametrize("mutation,match", [
    ("source", "Unresolved source ID"),
    ("home", "home-table identity"),
    ("definition_hash", "provenance hash"),
    ("formal_rules", "formal native Rule"),
    ("dto", "Unsupported capture lane"),
    ("values", "structuredContent"),
    ("schema", "tool schema"),
    ("ambiguous_source", "Ambiguous reviewed model"),
    ("scope", "authorized scope"),
    ("additional_backing", "entity shape"),
    ("missing_binding", "Unresolved entity backing"),
    ("unsupported_part", "unsupported definition part"),
    ("source_expression", "source expression"),
    ("raw_dax", "EXTERNALMEASURE"),
    ("property_binding", "property backing"),
    ("missing_provenance", "incomplete native capture"),
    ("duplicate_parts", "duplicate"),
])
def test_unresolved_ambiguous_and_unobserved_shapes_block(tmp_path, mutation, match):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    if mutation == "source":
        change_part(definition, receipt, "tables/subscriptions_account.tmdl",
                    lambda t: t.replace("ONT_ItemId = SYNTHETIC_SUBSCRIPTIONS", "ONT_ItemId = UNKNOWN_ID"))
    elif mutation == "home":
        change_part(definition, receipt, "tables/subscriptions_subscriptions_fact.tmdl",
                    lambda t: t.replace('"t": "subscriptions Facts"', '"t": "Accounts"'))
    elif mutation == "definition_hash":
        receipt["definitionReceipt"] = "unrelated.json"
    elif mutation == "formal_rules":
        receipt["rawNativeContext"]["list_ontology_rules"]["structuredContent"]["values"] = [{"invented": True}]
    elif mutation == "dto":
        receipt["lane"] = "RECORDED_NATIVE_DEMO"
    elif mutation == "values":
        receipt["rawNativeContext"]["list_ontology_entities"]["structuredContent"] = {"entities": []}
    elif mutation == "schema":
        receipt["inventory"]["tools"][0]["schema_hash"] = "unreviewed"
    elif mutation == "ambiguous_source":
        kwargs["reviewed_models"].append(kwargs["reviewed_models"][0])
    elif mutation == "scope":
        kwargs["authorized_scope"].models = ["finance"]
    elif mutation == "additional_backing":
        receipt["rawNativeContext"]["list_ontology_entities"]["structuredContent"]["values"][0][
            "additionalBackingTables"
        ] = ["unobserved"]
    elif mutation == "missing_binding":
        receipt["rawNativeContext"]["list_ontology_entities"]["structuredContent"]["values"][0][
            "backingTable"
        ] = "unobserved"
    elif mutation == "unsupported_part":
        definition["definition"]["parts"].append({
            "path": "metrics/Projected.tmdl", "payloadType": "InlineBase64", "payload": "",
        })
        receipt["definitionReceipt"] = digest(definition) + ".json"
    elif mutation == "source_expression":
        change_part(definition, receipt, "expressions.tmdl",
                    lambda t: t.replace('Synthetic finance"', 'Unknown Model"'))
    elif mutation == "raw_dax":
        change_part(
            definition, receipt, "tables/subscriptions_subscriptions_fact.tmdl",
            lambda t: t.replace(
                'EXTERNALMEASURE("Amount", INTEGER, "DirectQuery to AS - SYNTHETIC_WORKSPACE/SYNTHETIC_SUBSCRIPTIONS")',
                "SUM('subscriptions Facts'[value])",
            ),
        )
    elif mutation == "property_binding":
        receipt["rawNativeContext"]["list_ontology_entities"]["structuredContent"]["values"][0][
            "properties"
        ][0]["backingConfiguration"]["valueColumn"]["column"] = "display_name"
    elif mutation == "missing_provenance":
        del receipt["inventory"]["provenance"]
    elif mutation == "duplicate_parts":
        definition["definition"]["parts"].append(definition["definition"]["parts"][0])
        receipt["definitionReceipt"] = digest(definition) + ".json"
    with pytest.raises(LabError, match=match):
        normalize_native_capture(definition, receipt, **kwargs)


def test_author_capture_is_not_live_reader_authorization(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    kwargs["authorized_scope"].mode = "LIVE_DELEGATED"
    with pytest.raises(LabError) as error:
        normalize_native_capture(definition, receipt, **kwargs)
    assert error.value.status == Status.BLOCKED_AUTH


def test_private_snapshot_roundtrip_scope_and_integrity(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    path = tmp_path / "private" / "snapshot.json"
    repo = tmp_path / "repo"
    write_native_snapshot(snapshot, path, repo=repo)
    assert load_native_bundle(path, kwargs["authorized_scope"]) == snapshot
    assert load_native_snapshot(path, kwargs["authorized_scope"]) == snapshot.context
    wrong = kwargs["authorized_scope"].model_copy(update={"authorization_revision": "changed"})
    with pytest.raises(LabError, match="authorization"):
        load_native_snapshot(path, wrong)
    with pytest.raises(FileExistsError):
        write_native_snapshot(snapshot, path, repo=repo)
    with pytest.raises(LabError, match="outside repository"):
        write_native_snapshot(snapshot, repo / "snapshot.json", repo=repo)
    with pytest.raises(LabError, match="OneDrive"):
        write_native_snapshot(snapshot, tmp_path / "OneDrive" / "snapshot.json", repo=repo)
    raw = json.loads(path.read_text())
    raw["context"]["scope"]["authorization_revision"] = "changed"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(LabError, match="hash"):
        load_native_snapshot(path, wrong)


@pytest.mark.parametrize("change", [
    {"principal": "SYNTHETIC_OTHER_READER"}, {"tenant": "SYNTHETIC_OTHER_TENANT"},
    {"models": ["subscriptions"]}, {"authorization_revision": "revoked"},
    {"mode": "LIVE_DELEGATED"},
])
def test_every_scope_dimension_is_exact(tmp_path, change):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    path = tmp_path / "scope-snapshot.json"
    write_native_snapshot(snapshot, path, repo=tmp_path / "repo")
    with pytest.raises(LabError, match="authorization"):
        load_native_snapshot(path, kwargs["authorized_scope"].model_copy(update=change))


def test_export_cli_is_local_private_and_reports_only_counts_and_hashes(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    inputs = {
        "definition": definition, "receipt": receipt,
        "authorized-scope": kwargs["authorized_scope"].model_dump(),
        "reviewed-models": [m.model_dump() for m in kwargs["reviewed_models"]],
    }
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "tools" / "export_native_context.py")]
    for name, value in inputs.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        command.extend([f"--{name}", str(path)])
    output = tmp_path / "exported-snapshot.json"
    command.extend([
        "--catalog", str(tmp_path / "synthetic-catalog.json"), "--output", str(output),
        "--authorization-reference", "Synthetic unit-test local-only export",
    ])
    result = subprocess.run(command, check=True, capture_output=True, text=True, cwd=root)
    summary = json.loads(result.stdout)
    assert summary["entities"] == 4 and summary["formal_rules"] == 0
    assert "SYNTHETIC_WORKSPACE" not in result.stdout
    assert load_native_snapshot(output, kwargs["authorized_scope"]).mode == "NATIVE_SNAPSHOT"


def test_explicit_native_cardinality_cannot_be_overridden(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    change_part(definition, receipt, "relationships.tmdl",
                lambda t: t.replace("relationship 'left-fact-account_backing'\n",
                                    "relationship 'left-fact-account_backing'\n\tfromCardinality: one\n"))
    review = ReviewedExecutionRelationship(
        native_relationship="left-fact-account", review_reference="Synthetic conflicting review",
        relationship=Relationship(
            id="conflict", left_model="subscriptions", right_model="subscriptions",
            left_table="subscriptions_fact", right_table="account", left_keys=["account_key"],
            right_keys=["account_key"], cardinality="many_to_one", version="1", purposes=["Synthetic"],
        ),
    )
    with pytest.raises(LabError, match="conflicts with explicit"):
        normalize_native_capture(definition, receipt, **kwargs, reviewed_relationships=[review])


def test_legacy_context_exact_scope_invariant_remains(tmp_path):
    from telco_iq.context import offline_context

    _, _, kwargs = synthetic_capture(tmp_path)
    context = offline_context(kwargs["authorized_scope"]).model_copy(update={
        "mode": "NATIVE_SNAPSHOT", "schema_hash": "synthetic-legacy-schema",
        "provenance": ["Synthetic legacy receipt"],
    })
    path = tmp_path / "legacy.json"
    path.write_text(context.model_dump_json(), encoding="utf-8")
    assert load_native_snapshot(path, kwargs["authorized_scope"]) == context
    wrong = deepcopy(kwargs["authorized_scope"])
    wrong.principal = "ANOTHER_SYNTHETIC_READER"
    with pytest.raises(LabError, match="authorization"):
        load_native_snapshot(path, wrong)
