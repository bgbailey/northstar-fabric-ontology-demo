"""Supplemental reverse traversal reviews; no new native objects or live source calls."""

from copy import deepcopy

import pytest
from test_executor import artifact, joining
from test_native_context import synthetic_capture

from telco_iq.contracts import JoinNode, Plan, QueryNode, Relationship, TemporalPredicate
from telco_iq.errors import LabError
from telco_iq.executor import join
from telco_iq.native_context import (
    ReviewedExecutionRelationship,
    normalize_native_capture,
    validate_native_metric_plan,
)


def reviews():
    forward = ReviewedExecutionRelationship(
        native_relationship="left-fact-account",
        review_reference="SYNTHETIC exact source-PK forward review, not native cardinality",
        relationship=Relationship(
            id="forward", left_model="subscriptions", right_model="subscriptions",
            left_table="subscriptions_fact", right_table="account",
            left_keys=["account_key"], right_keys=["account_key"], cardinality="many_to_one",
            version="synthetic-source-pk-1", purposes=["SYNTHETIC exact captured edge"],
        ),
    )
    reverse = ReviewedExecutionRelationship(
        native_relationship=forward.native_relationship,
        review_reference="SYNTHETIC explicitly reviewed inverse of the same captured edge",
        relationship=Relationship(
            id="reverse", left_model="subscriptions", right_model="subscriptions",
            left_table="account", right_table="subscriptions_fact",
            left_keys=["account_key"], right_keys=["account_key"], cardinality="one_to_many",
            version=forward.relationship.version,
            purposes=["SYNTHETIC population-preserving reverse traversal, not a new native edge"],
        ),
    )
    return forward, reverse


def test_explicit_forward_and_reverse_reviews_preserve_native_objects_and_metric_authority(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    forward, reverse = reviews()
    before = deepcopy((definition, receipt))
    original = normalize_native_capture(definition, receipt, **kwargs, reviewed_relationships=[forward])
    snapshot = normalize_native_capture(definition, receipt, **kwargs, reviewed_relationships=[forward, reverse])
    assert snapshot.native_relationships == original.native_relationships
    assert snapshot.entities == original.entities and snapshot.metrics == original.metrics
    assert snapshot.captured_at == original.captured_at
    assert snapshot.evidence_hashes["definition"] == original.evidence_hashes["definition"]
    assert snapshot.content_hash != original.content_hash
    assert len(snapshot.context.relationships) == 2
    assert (definition, receipt) == before
    snapshot.verify(snapshot.context.scope)
    plan = Plan(
        request_id="SYNTHETIC", question="SYNTHETIC reverse traversal",
        principal_scope=snapshot.context.scope, ontology_revision=snapshot.context.revision,
        as_of="2026-10-20T00:00:00Z", metric_selections=["subscriptions.amount"],
        nodes=[
            QueryNode(
                id="registered", kind="QuerySemanticModel", model="subscriptions", table="account",
                columns=["account_key"], expected_grain=["account_key"],
            ),
            QueryNode(
                id="metric", kind="QuerySemanticModel", model="subscriptions", table="subscriptions_fact",
                metrics=["subscriptions.amount"], group_by=["account_key"], expected_grain=["account_key"],
                date_role="month", filters=[{"column": "month", "op": "eq", "values": ["2026-08-01"]}],
            ),
            JoinNode(
                id="joined", kind="JoinResults", left="registered", right="metric", relationship="reverse",
                left_keys=["account_key"], right_keys=["account_key"], cardinality="one_to_many",
                unmatched="preserve", max_fanout=1,
            ),
        ],
        result_node="joined",
    )
    proof = validate_native_metric_plan(plan, snapshot, kwargs["catalog"])
    assert proof["relationship_bindings"][0]["execution_traversal"] == "reverse"
    assert proof["relationship_bindings"][0]["native_relationship"] == forward.native_relationship
    assert proof["metric_bindings"][0]["calculation_owner"] == "source_semantic_model"


@pytest.mark.parametrize("change", [
    "missing_forward", "duplicate_forward", "duplicate_reverse", "invented_edge", "wrong_endpoint",
    "wrong_cardinality", "wrong_filter", "wrong_version", "invented_temporal", "wrong_one_side_pk",
])
def test_reverse_execution_does_not_authorize_invented_or_unreviewed_contracts(tmp_path, change):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    forward, reverse = reviews()
    entries = [forward, reverse]
    if change == "missing_forward":
        entries = [reverse]
    elif change == "duplicate_forward":
        other = forward.model_copy(deep=True)
        other.relationship.id = "duplicate-forward"
        entries.append(other)
    elif change == "duplicate_reverse":
        other = reverse.model_copy(deep=True)
        other.relationship.id = "duplicate-reverse"
        entries.append(other)
    elif change == "invented_edge":
        reverse.native_relationship = "invented-fact-shortcut"
    elif change == "wrong_endpoint":
        reverse.relationship.right_model = "finance"
    elif change == "wrong_cardinality":
        reverse.relationship.cardinality = "one_to_one"
    elif change == "wrong_filter":
        reverse.relationship.aligned_filters = ["month"]
    elif change == "wrong_version":
        reverse.relationship.version = "different"
    elif change == "invented_temporal":
        reverse.relationship.temporal = TemporalPredicate(
            left_start="start", left_end="end", right_start="start", right_end="end",
        )
    else:
        kwargs["catalog"].tables["account"]["primaryKey"] = ["display_name"]
    with pytest.raises(LabError):
        normalize_native_capture(definition, receipt, **kwargs, reviewed_relationships=entries)


def test_population_preservation_keeps_absent_measure_unknown_and_retains_fanout_guard():
    node, context = joining(cardinality="one_to_many", unmatched="preserve")
    left = artifact([{"id": 1}, {"id": 2}, {"id": 3}])
    right = artifact([{"key": 2, "amount": 0}], "right", {"amount": "semi_additive"})
    result = join(node, left, right, context)
    assert result.rows == [
        {"id": 1, "key": None, "amount": None},
        {"id": 2, "key": 2, "amount": 0},
        {"id": 3, "key": None, "amount": None},
    ]
    assert result.coverage["unmapped"] == 2
    duplicate = artifact([{"key": 2, "amount": 0}, {"key": 2, "amount": 5}], "right")
    with pytest.raises(LabError, match="fan-out"):
        join(node, left, duplicate, context)
