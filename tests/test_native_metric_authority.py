"""Native metric authority uses only synthetic source captures and plans."""

from copy import deepcopy

import pytest
from test_native_context import change_part, synthetic_capture

from telco_iq.catalog import digest
from telco_iq.contracts import ContextNode, JoinNode, Plan, QueryNode, Relationship, Status
from telco_iq.errors import LabError
from telco_iq.native_context import (
    ReviewedExecutionRelationship,
    normalize_native_capture,
    validate_native_metric_plan,
)
from telco_iq.planner import build_planner_payload


def native_plan(snapshot, *, scope=None, context=None):
    return Plan(
        request_id="SYNTHETIC_PLAN", question="Synthetic",
        principal_scope=scope or snapshot.context.scope, as_of="2026-09-30T00:00:00Z",
        ontology_revision=(context or snapshot.context).revision,
        metric_selections=["subscriptions.amount"],
        nodes=[QueryNode(
            id="query", kind="QuerySemanticModel", model="subscriptions", table="subscriptions_fact",
            metrics=["subscriptions.amount"], group_by=["account_key"], expected_grain=["account_key"],
            date_role="month", filters=[{"column": "month", "op": "eq", "values": ["2026-08-01"]}],
        )],
        result_node="query",
    )


def rehash(snapshot):
    snapshot.content_hash = digest(snapshot.model_dump(mode="json", exclude={"content_hash"}))


@pytest.mark.parametrize("datatype", ["INTEGER", "DOUBLE", "VARIANT"])
def test_reviewed_externalmeasure_datatypes_keep_source_identity_and_calculation_owner(tmp_path, datatype):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    change_part(
        definition, receipt, "tables/subscriptions_subscriptions_fact.tmdl",
        lambda text: text.replace('EXTERNALMEASURE("Amount", INTEGER,', f'EXTERNALMEASURE("Amount", {datatype},', 1),
    )
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    proof = validate_native_metric_plan(native_plan(snapshot), snapshot, kwargs["catalog"])
    binding = proof["metric_bindings"][0]
    assert binding["source_model"] == "subscriptions"
    assert binding["source_measure"] == "Amount" and binding["home_table"] == "subscriptions_fact"
    assert binding["calculation_owner"] == "source_semantic_model"
    assert snapshot.metrics[0]["representation"] == "EXTERNALMEASURE"
    assert kwargs["catalog"].metrics["subscriptions.amount"]["dax"] == "AUTHOR_ONLY_DAX"


@pytest.mark.parametrize("datatype", ["UNKNOWN", "ANY", "VARIANT()", "VARIANT + 1"])
def test_unknown_or_expression_proxy_datatypes_still_fail_closed(tmp_path, datatype):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    change_part(
        definition, receipt, "tables/subscriptions_subscriptions_fact.tmdl",
        lambda text: text.replace('EXTERNALMEASURE("Amount", INTEGER,', f'EXTERNALMEASURE("Amount", {datatype},', 1),
    )
    with pytest.raises(LabError, match="source EXTERNALMEASURE proxy required"):
        normalize_native_capture(definition, receipt, **kwargs)


def test_metric_binding_is_selected_before_execution_and_retains_source_calculation(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    before = deepcopy(kwargs["catalog"].raw)
    plan = native_plan(snapshot)
    evidence = validate_native_metric_plan(plan, snapshot, kwargs["catalog"])
    assert evidence["snapshot_hash"] == snapshot.content_hash
    assert evidence["capture_evidence_hashes"] == snapshot.evidence_hashes
    assert evidence["relationship_bindings"] == []
    assert evidence["metric_bindings"] == [{
        "metric_id": "subscriptions.amount", "native_entity": "subscriptions subscriptions_fact",
        "source_model": "subscriptions", "source_workspace_id": "SYNTHETIC_WORKSPACE",
        "source_dataset_id": "SYNTHETIC_SUBSCRIPTIONS", "source_dataset_name": "Synthetic subscriptions",
        "home_table": "subscriptions_fact", "source_measure": "Amount",
        "calculation_owner": "source_semantic_model",
        "native_part": snapshot.metrics[0]["part"], "native_part_hash": snapshot.metrics[0]["part_hash"],
        "source_review_reference": "Synthetic unit-test source review",
        "query_uses": [{"node_id": "query", "query_contract_hash": digest(plan.nodes[0].model_dump(mode="json"))}],
    }]
    assert kwargs["catalog"].raw == before
    assert "AUTHOR_ONLY" not in str(evidence)


def test_explicit_clarification_is_ambiguous_not_a_missing_query_metric_error(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    plan = native_plan(snapshot)
    plan.clarification_state = "required"
    plan.nodes = [ContextNode(id="context", kind="ReadModelSchema", references=["subscriptions"])]
    plan.result_node = "context"
    with pytest.raises(LabError, match="Question needs explicit clarification") as rejected:
        validate_native_metric_plan(plan, snapshot, kwargs["catalog"])
    assert rejected.value.status == Status.AMBIGUOUS


@pytest.mark.parametrize("field,value", [
    ("model", "finance"), ("table", "account"), ("name", "Invented measure"),
    ("id", "invented.id"), ("entity", "finance finance_fact"),
    ("part_hash", "0" * 64), ("calculation_owner", "application"),
])
def test_wrong_native_source_home_name_id_or_capture_binding_blocks(tmp_path, field, value):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    snapshot.metrics[0][field] = value
    rehash(snapshot)
    with pytest.raises(LabError):
        validate_native_metric_plan(native_plan(snapshot), snapshot, kwargs["catalog"])


def test_static_catalogue_metric_not_exposed_native_is_not_selectable_in_either_arm(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    catalog = kwargs["catalog"]
    extra = deepcopy(catalog.raw["metrics"][0])
    extra.update({"id": "subscriptions.catalog_only", "name": "Not native"})
    catalog.raw["metrics"].append(extra)
    catalog.metrics[extra["id"]] = extra
    catalog.revision = digest(catalog.raw)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    plan = native_plan(snapshot)
    plan.metric_selections = plan.nodes[0].metrics = [extra["id"]]
    for arm in ("baseline", "native"):
        payload = build_planner_payload(
            "Synthetic", snapshot.context.scope, snapshot.context, catalog,
            input_arm=arm, native_snapshot=snapshot,
        )
        assert extra["id"] not in [metric["id"] for metric in payload["metadata"]["metrics"]]
        assert {table["id"] for table in payload["metadata"]["tables"]} == set(catalog.tables)
        with pytest.raises(LabError, match="not exposed"):
            validate_native_metric_plan(plan, snapshot, catalog)


@pytest.mark.parametrize("change", ["unselected", "unused_selection", "duplicate", "query_source", "query_home", "context"])
def test_native_selection_must_match_actual_query_use(tmp_path, change):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    plan = native_plan(snapshot)
    if change == "unselected":
        plan.metric_selections = []
    elif change == "unused_selection":
        plan.metric_selections.append("finance.amount")
    elif change == "duplicate":
        plan.metric_selections.append("subscriptions.amount")
    elif change == "query_source":
        plan.nodes[0].model = "finance"
    elif change == "query_home":
        plan.nodes[0].table = "account"
    elif change == "context":
        plan.nodes.insert(0, ContextNode(id="select", kind="SelectMetric", references=["finance.amount"]))
        plan.nodes[1].depends_on = ["select"]
    with pytest.raises(LabError):
        validate_native_metric_plan(plan, snapshot, kwargs["catalog"])


def edge_fixture(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    review = ReviewedExecutionRelationship(
        native_relationship="left-fact-account", review_reference="SYNTHETIC reviewed source keys",
        relationship=Relationship(
            id="reviewed-left-fact", left_model="subscriptions", right_model="subscriptions",
            left_table="subscriptions_fact", right_table="account",
            left_keys=["account_key"], right_keys=["account_key"], cardinality="many_to_one",
            version="synthetic-1", purposes=["Synthetic raw fact to unique account"],
        ),
    )
    snapshot = normalize_native_capture(definition, receipt, **kwargs, reviewed_relationships=[review])
    plan = native_plan(snapshot)
    plan.nodes += [
        QueryNode(
            id="accounts", kind="QuerySemanticModel", model="subscriptions", table="account",
            columns=["account_key", "display_name"], expected_grain=["account_key"],
        ),
        JoinNode(
            id="join", kind="JoinResults", left="query", right="accounts", relationship=review.relationship.id,
            left_keys=["account_key"], right_keys=["account_key"],
            cardinality="many_to_one", unmatched="error",
        ),
    ]
    plan.result_node = "join"
    return snapshot, plan, kwargs["catalog"]


def test_each_used_edge_has_native_capture_and_execution_review_proof(tmp_path):
    snapshot, plan, catalog = edge_fixture(tmp_path)
    proof = validate_native_metric_plan(plan, snapshot, catalog)
    assert len(proof["relationship_bindings"]) == 1
    edge = proof["relationship_bindings"][0]
    assert edge["native_relationship"] == "left-fact-account"
    assert edge["review_reference"] == "SYNTHETIC reviewed source keys"
    assert edge["execution_relationship"] == "reviewed-left-fact"
    assert edge["node_id"] == "join"
    assert edge["native_part_hash"] == snapshot.native_relationships[0]["part_hash"]
    assert edge["backing_part_hash"] == snapshot.native_relationships[0]["backing_part_hash"]


@pytest.mark.parametrize("change", ["unreviewed", "invented_edge", "wrong_lineage", "cohort", "cardinality"])
def test_invented_edges_and_wrong_reviewed_query_bindings_block(tmp_path, change):
    snapshot, plan, catalog = edge_fixture(tmp_path)
    if change == "unreviewed":
        plan.nodes[-1].relationship = "account-bridge"
    elif change == "invented_edge":
        snapshot.execution_contracts[0].native_relationship = "invented-fact-shortcut"
        rehash(snapshot)
    elif change == "wrong_lineage":
        plan.nodes[-1].right_keys = ["display_name"]
    elif change == "cohort":
        from telco_iq.contracts import CohortFilter

        plan.nodes[0].cohort_filters = [CohortFilter(
            source="accounts", source_column="account_key", target_column="account_key",
            relationship="invented-cohort-shortcut",
        )]
    elif change == "cardinality":
        plan.nodes[-1].cardinality = "one_to_one"
    with pytest.raises(LabError):
        validate_native_metric_plan(plan, snapshot, catalog)


@pytest.mark.parametrize("wrong_binding", [False, True])
def test_population_binds_actual_private_source_not_just_alias(tmp_path, wrong_binding):
    from telco_iq.contracts import SourcePopulation

    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    populations = [
        SourcePopulation(
            model=source.alias, evidence_kind="LOCAL_FIXTURE", profile="synthetic-unit", seed=1,
            binding_hash=digest({
                "workspace_id": source.workspace_id,
                "dataset_id": source.dataset_id + "-WRONG_MODEL" if wrong_binding else source.dataset_id,
            }),
            generator_revision="synthetic-unit-1", identity_revision=digest("identities"),
            manifest_hash=digest("manifest"),
            table_hashes={entity["table"]: digest(entity["table"]) for entity in snapshot.entities},
            shared_map_hashes={"account": digest("shared-map")},
        ) for source in snapshot.sources
    ]
    if wrong_binding:
        with pytest.raises(LabError, match="actual source"):
            validate_native_metric_plan(
                native_plan(snapshot), snapshot, kwargs["catalog"], source_populations=populations,
            )
    else:
        proof = validate_native_metric_plan(
            native_plan(snapshot), snapshot, kwargs["catalog"], source_populations=populations,
        )
        assert proof["metric_bindings"][0]["source_population"]["binding_hash"] == populations[0].binding_hash
