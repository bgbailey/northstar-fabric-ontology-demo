"""Synthetic column-program compilation; never executes retained live rejected proposals."""

import re
from copy import deepcopy

import pytest
from test_multihop import MemoryProvider
from test_native_context import synthetic_capture
from test_planner_inputs import SyntheticCompletion, synthetic_approval

from telco_iq.contracts import (
    AggregateNode,
    CohortFilter,
    Context,
    ContextNode,
    Filter,
    JoinNode,
    Plan,
    ProjectColumns,
    QueryNode,
    RankNode,
    RuleNode,
    Status,
    TemporalPredicate,
)
from telco_iq.errors import LabError
from telco_iq.executor import Executor, validate
from telco_iq.native_context import (
    ReviewedExecutionRelationship,
    normalize_native_capture,
    validate_native_metric_plan,
)
from telco_iq.plan_binding import bind_plan_columns
from telco_iq.planner import Planner, PlanValidationError, build_planner_payload


def case(tmp_path, qualified):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    reviews = []
    for model, edge, stem in (
        ("subscriptions", "left-fact-account", "sub"),
        ("finance", "right-fact-account", "fin"),
    ):
        for reverse in (False, True):
            reviews.append(ReviewedExecutionRelationship(
                native_relationship=edge, review_reference="SYNTHETIC explicit source-PK traversal review",
                relationship={
                    "id": stem + ("-reverse" if reverse else "-forward"),
                    "left_model": model, "right_model": model,
                    "left_table": "account" if reverse else f"{model}_fact",
                    "right_table": f"{model}_fact" if reverse else "account",
                    "left_keys": ["account_key"], "right_keys": ["account_key"],
                    "cardinality": "one_to_many" if reverse else "many_to_one",
                    "version": "synthetic-reviewed-1", "purposes": ["SYNTHETIC exact captured native edge"],
                },
            ))
    reviews.append(ReviewedExecutionRelationship(
        native_relationship="account-bridge", review_reference="SYNTHETIC exact registered account bridge",
        relationship={
            "id": "bridge", "left_model": "subscriptions", "right_model": "finance",
            "left_table": "account", "right_table": "account", "left_keys": ["account_key"],
            "right_keys": ["account_key"], "cardinality": "one_to_one",
            "version": "synthetic-reviewed-1", "purposes": ["SYNTHETIC reviewed cross-source identity"],
        },
    ))
    snapshot = normalize_native_capture(definition, receipt, **kwargs, reviewed_relationships=reviews)
    def ref(model, table):
        return f"{model}.{table}.account_key" if qualified else "account_key"

    nodes = [
        QueryNode(
            id="population", kind="QuerySemanticModel", model="subscriptions", table="account",
            columns=["account_key"], expected_grain=["account_key"],
        ),
        QueryNode(
            id="sub-values", kind="QuerySemanticModel", model="subscriptions", table="subscriptions_fact",
            metrics=["subscriptions.amount"], group_by=["account_key"], expected_grain=["account_key"],
            date_role="month", filters=[{"column": "month", "op": "eq", "values": ["2026-08-01"]}],
        ),
        JoinNode(
            id="first", kind="JoinResults", left="population", right="sub-values",
            left_keys=[ref("subscriptions", "account")], right_keys=[ref("subscriptions", "subscriptions_fact")],
            relationship="sub-reverse", cardinality="one_to_many", unmatched="preserve", max_fanout=1,
        ),
        QueryNode(
            id="fin-accounts", kind="QuerySemanticModel", model="finance", table="account",
            columns=["account_key"], expected_grain=["account_key"],
        ),
        ProjectColumns(
            id="fin-keys", kind="ProjectColumns", source="fin-accounts",
            columns=({"finance_key": ref("finance", "account")} if qualified else {
                "account_key": "account_key", "finance_key": "account_key",
            }),
        ),
        JoinNode(
            id="aligned", kind="JoinResults", left="first", right="fin-keys",
            left_keys=[ref("subscriptions", "account")],
            right_keys=[ref("finance", "account") if qualified else "account_key"],
            relationship="bridge", cardinality="one_to_one", unmatched="preserve", max_fanout=1,
        ),
        QueryNode(
            id="fin-values", kind="QuerySemanticModel", model="finance", table="finance_fact",
            metrics=["finance.amount"], group_by=["account_key"], expected_grain=["account_key"],
            date_role="month", filters=[{"column": "month", "op": "eq", "values": ["2026-08-01"]}],
        ),
        JoinNode(
            id="combined", kind="JoinResults", left="aligned", right="fin-values",
            left_keys=["finance_key"], right_keys=[ref("finance", "finance_fact")],
            relationship="fin-reverse", cardinality="one_to_many", unmatched="preserve", max_fanout=1,
        ),
    ]
    plan = Plan(
        request_id="SYNTHETIC_BINDING", question="Synthetic", principal_scope=snapshot.context.scope,
        as_of="2026-10-20T00:00:00Z", ontology_revision=snapshot.context.revision,
        metric_selections=["subscriptions.amount", "finance.amount"], nodes=nodes, result_node="combined",
    )
    return plan, snapshot, kwargs["catalog"]


@pytest.mark.parametrize("qualified", [False, True])
async def test_column_binding_executes_same_business_graph_with_exact_source_metrics(tmp_path, qualified):
    plan, snapshot, catalog = case(tmp_path, qualified)
    before = deepcopy(plan.model_dump(mode="json"))
    with pytest.raises(LabError):
        validate(plan, plan.principal_scope, catalog, snapshot.context)
    bound, evidence = bind_plan_columns(plan, catalog, snapshot.context)
    assert plan.model_dump(mode="json") == before
    assert bound.metric_selections == plan.metric_selections
    assert [node.model_dump(mode="json") for node in bound.nodes if isinstance(node, QueryNode)] == [
        node.model_dump(mode="json") for node in validate_query_order(plan)
    ]
    proof = validate_native_metric_plan(bound, snapshot, catalog)
    assert len(proof["metric_bindings"]) == 2 and len(proof["relationship_bindings"]) == 3
    assert len(evidence["generated_projection_nodes"]) >= 2
    assert evidence["source_queries_at_compilation"] == 0
    assert evidence["model_proposal"] == before
    provider = MemoryProvider(catalog, {
        "population": [{"account_key": 1}, {"account_key": 2}, {"account_key": 3}],
        "fin-accounts": [{"account_key": 1}, {"account_key": 2}, {"account_key": 3}],
        "sub-values": [{"account_key": 1, "subscriptions.amount": 5},
                       {"account_key": 2, "subscriptions.amount": 0}],
        "fin-values": [{"account_key": 1, "finance.amount": 17},
                       {"account_key": 2, "finance.amount": 0}],
    })
    result = await Executor(
        catalog, provider, snapshot.context, source_populations=provider.source_populations,
    ).execute(bound, bound.principal_scope)
    assert result.status == Status.COMPLETE
    assert len(result.receipts) == 4 and len(provider.calls) == 4
    assert [
        {key: row[key] for key in ("account_key", "subscriptions.amount", "finance.amount")}
        for row in result.answer.rows
    ] == [
        {"account_key": 1, "subscriptions.amount": 5, "finance.amount": 17},
        {"account_key": 2, "subscriptions.amount": 0, "finance.amount": 0},
        {"account_key": 3, "subscriptions.amount": None, "finance.amount": None},
    ]
    assert result.answer.coverage["unmapped"] == 1


def validate_query_order(plan):
    from telco_iq.executor import plan_order
    return [node for node in plan_order(plan) if isinstance(node, QueryNode)]


@pytest.mark.parametrize("change", [
    "missing", "ambiguous", "wrong_identity", "wrong_cardinality", "wrong_period",
    "scope", "unselected_metric", "nonadditive_sum", "unused_node", "cycle",
])
def test_binding_does_not_repair_business_authority_or_guess_references(tmp_path, change):
    plan, snapshot, catalog = case(tmp_path, True)
    if change == "missing":
        plan.nodes[4].columns = {"finance_key": "finance.account.missing_key"}
    elif change == "ambiguous":
        plan.nodes[4].columns["second_finance_key"] = "finance.account.account_key"
    elif change == "wrong_identity":
        plan.nodes[5].left_keys = ["subscriptions.subscriptions_fact.account_key"]
    elif change == "wrong_cardinality":
        plan.nodes[2].cardinality = "one_to_one"
    elif change == "wrong_period":
        plan.nodes[1].filters = [
            Filter(column="month", op="gte", values=["2026-08-01"]),
            Filter(column="month", op="lt", values=["2026-09-01"]),
        ]
    elif change == "scope":
        plan.principal_scope = plan.principal_scope.model_copy(update={"principal": "SYNTHETIC_OTHER"})
    elif change == "unselected_metric":
        plan.metric_selections = ["subscriptions.amount"]
    elif change == "nonadditive_sum":
        plan.nodes.append(AggregateNode(
            id="illegal-total", kind="AggregateResult", source=plan.result_node,
            group_by=[], sums=["subscriptions.amount"],
        ))
        plan.result_node = "illegal-total"
    elif change == "unused_node":
        plan.nodes.append(ContextNode(id="unused", kind="ReadModelSchema", references=["subscriptions"]))
    else:
        plan.nodes[0].depends_on = ["combined"]
    before = deepcopy(plan.model_dump(mode="json"))
    with pytest.raises(LabError):
        bind_plan_columns(plan, catalog, snapshot.context)
    assert plan.model_dump(mode="json") == before


def test_compilation_preserves_canonical100node_limit(tmp_path):
    plan, snapshot, catalog = case(tmp_path, False)
    extra = [
        ContextNode(id=f"schema-{index}", kind="ReadModelSchema", references=["subscriptions"])
        for index in range(100 - len(plan.nodes))
    ]
    plan.nodes[0].depends_on = [node.id for node in extra]
    plan.nodes.extend(extra)
    assert len(plan.nodes) == 100
    with pytest.raises(LabError, match="canonical plan contract limits"):
        bind_plan_columns(plan, catalog, snapshot.context)


def test_generated_node_names_obey64character_contract_and_do_not_shadow_model_nodes(tmp_path):
    plan, snapshot, catalog = case(tmp_path, True)
    old = plan.nodes[2].id
    plan.nodes[2].id = "J" * 64
    plan.nodes[5].left = plan.nodes[2].id
    assert old == "first"
    reserved = ContextNode(
        id=("columns_" + plan.nodes[2].id)[:64], kind="ReadModelSchema", references=["subscriptions"],
    )
    plan.nodes[0].depends_on = [reserved.id]
    plan.nodes.append(reserved)
    bound, evidence = bind_plan_columns(plan, catalog, snapshot.context)
    generated = evidence["generated_projection_nodes"]
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name) for name in generated)
    assert not set(generated) & {node.id for node in plan.nodes}
    assert len(bound.nodes) == len(plan.nodes) + len(generated)


def test_qualified_values_and_ranking_preserve_source_measure_names_and_derived_flags(tmp_path):
    plan, snapshot, catalog = case(tmp_path, True)
    plan.nodes.extend([
        ProjectColumns(
            id="values", kind="ProjectColumns", source=plan.result_node,
            columns={
                "key": "subscriptions.account.account_key",
                "sub_value": "subscriptions.subscriptions_fact.subscriptions.amount",
                "fin_value": "finance.finance_fact.finance.amount",
            },
        ),
        RuleNode(
            id="difference", kind="ComparePeriods", source="values", operation="difference",
            arguments=["finance.finance_fact.finance.amount", "subscriptions.subscriptions_fact.subscriptions.amount"],
            output="delta",
        ),
        RankNode(
            id="ranked", kind="RankCandidates", source="difference",
            column="finance.finance_fact.finance.amount", limit=2,
        ),
    ])
    plan.result_node = "ranked"
    bound, _ = bind_plan_columns(plan, catalog, snapshot.context)
    nodes = {node.id: node for node in bound.nodes}
    assert nodes["difference"].arguments == ["fin_value", "sub_value"]
    assert nodes["ranked"].column == "fin_value"
    assert nodes["values"].columns["fin_value"] == "finance.amount"
    assert nodes["difference"].operation == "difference" and nodes["ranked"].limit == 2
    validate_native_metric_plan(bound, snapshot, catalog)


def test_qualified_cohort_source_preserves_exact_reviewed_target_and_query_context(tmp_path):
    plan, snapshot, catalog = case(tmp_path, True)
    plan.nodes[1].cohort_filters = [CohortFilter(
        source="population", source_column="subscriptions.account.account_key",
        target_column="account_key", relationship="sub-reverse",
    )]
    bound, _ = bind_plan_columns(plan, catalog, snapshot.context)
    query = next(node for node in bound.nodes if node.id == "sub-values")
    assert query.cohort_filters[0].source_column == "account_key"
    assert query.cohort_filters[0].target_column == "account_key"
    assert query.cohort_filters[0].relationship == "sub-reverse"
    assert query.metrics == plan.nodes[1].metrics and query.filters == plan.nodes[1].filters
    validate_native_metric_plan(bound, snapshot, catalog)


def test_qualified_temporal_bounds_survive_right_column_namespacing(tmp_path):
    plan, snapshot, catalog = case(tmp_path, True)
    for table in ("account", "finance_fact"):
        catalog.tables[table]["columns"].update(start="timestamp", end="timestamp")
    temporal = TemporalPredicate(left_start="start", left_end="end", right_start="start", right_end="end")
    context = Context(
        mode="OFFLINE_SYNTHETIC", scope=plan.principal_scope, revision="SYNTHETIC_TIME",
        source="SYNTHETIC temporal compiler test, not native metadata",
        rules=[], provenance=["SYNTHETIC_ONLY"], relationships=[{
            "id": "window", "left_model": "subscriptions", "right_model": "finance",
            "left_table": "account", "right_table": "finance_fact",
            "left_keys": ["account_key"], "right_keys": ["account_key"],
            "cardinality": "one_to_many", "temporal": temporal,
            "version": "SYNTHETIC", "purposes": ["SYNTHETIC half-open interval"],
        }],
    )
    candidate = plan.model_copy(update={
        "ontology_revision": context.revision, "metric_selections": [], "result_node": "windowed", "nodes": [
            QueryNode(
                id="left", kind="QuerySemanticModel", model="subscriptions", table="account",
                columns=["account_key", "start", "end"], expected_grain=["account_key"], date_role="start",
            ),
            QueryNode(
                id="right", kind="QuerySemanticModel", model="finance", table="finance_fact",
                columns=["account_key", "month", "start", "end"], expected_grain=["account_key", "month"],
                date_role="month",
            ),
            JoinNode(
                id="windowed", kind="JoinResults", left="left", right="right", relationship="window",
                left_keys=["subscriptions.account.account_key"], right_keys=["finance.finance_fact.account_key"],
                cardinality="one_to_many", unmatched="preserve", temporal={
                    "left_start": "subscriptions.account.start", "left_end": "subscriptions.account.end",
                    "right_start": "finance.finance_fact.start", "right_end": "finance.finance_fact.end",
                }, overlap_start="clipped_start", overlap_end="clipped_end",
            ),
        ],
    })
    bound, _ = bind_plan_columns(candidate, catalog, context)
    joined = bound.nodes[-1]
    assert joined.temporal.left_start == "start"
    assert joined.temporal.right_start != "start"
    assert joined.overlap_start == "clipped_start" and joined.overlap_end == "clipped_end"
    assert joined.cardinality == "one_to_many" and joined.max_fanout == 1
    validate(bound, bound.principal_scope, catalog, context)


async def test_mode_is_opt_in_and_fair_and_keeps_original_model_proposal(tmp_path):
    plan, snapshot, catalog = case(tmp_path, True)
    completion = SyntheticCompletion(plan.model_dump(mode="json"))
    exact = Planner(completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    with pytest.raises(PlanValidationError):
        await exact.propose("Synthetic", plan.principal_scope, snapshot.context, catalog, native_snapshot=snapshot)
    planner = Planner(
        completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT",
        column_reference_mode="source_qualified",
    )
    values = [
        await planner.propose(
            "Synthetic", plan.principal_scope, snapshot.context, catalog, input_arm=arm, native_snapshot=snapshot,
        ) for arm in ("baseline", "native")
    ]
    assert values[0][0] == values[1][0]
    assert values[0][1]["facts_hash"] == values[1][1]["facts_hash"]
    assert values[0][1]["column_binding"] == values[1][1]["column_binding"]
    assert values[0][1]["column_binding"]["model_proposal"] == plan.model_dump(mode="json")
    assert completion.calls[1][1]["column_binding_policy"] == completion.calls[2][1]["column_binding_policy"]
    assert "column_binding_policy" not in completion.calls[0][1]
    assert build_planner_payload(
        "Synthetic", plan.principal_scope, snapshot.context, catalog, native_snapshot=snapshot,
    )["facts_hash"] == values[0][1]["facts_hash"]


async def test_opt_in_mode_is_snapshotted_before_model_await(tmp_path):
    plan, snapshot, catalog = case(tmp_path, True)
    class ChangingCompletion(SyntheticCompletion):
        async def complete(self, *args):
            planner.column_reference_mode = "exact"
            return await super().complete(*args)
    planner = Planner(
        ChangingCompletion(plan.model_dump(mode="json")), synthetic_approval(),
        "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT", column_reference_mode="source_qualified",
    )
    _, receipt = await planner.propose(
        "Synthetic", plan.principal_scope, snapshot.context, catalog, native_snapshot=snapshot,
    )
    assert receipt["column_binding"]["mode"] == "source_qualified"
