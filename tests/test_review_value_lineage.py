"""Synthetic executed-plan structures only; no source rows, oracle values or runtime grant."""

import pytest

from tools.review_value_lineage import review_column_matches


def query(name):
    return {"id": name, "kind": "QuerySemanticModel", "columns": ["account_key"],
            "group_by": [], "metrics": [name + ".metric"]}


def project(name, source, columns):
    return {"id": name, "kind": "ProjectColumns", "source": source, "columns": columns}


def join(name, left, right, left_key, right_key, kind="JoinResults"):
    return {"id": name, "kind": kind, "left": left, "right": right,
            "left_keys": [left_key], "right_keys": [right_key]}


def test_original_account_key_retains_transitive_source_dimension_across_multiple_renamed_joins():
    plan = {"nodes": [
        query("finance"), query("registry"), query("subscriptions"),
        project("registry_names", "registry", {"registry_key": "account_key"}),
        join("finance_registry", "finance", "registry_names", "account_key", "registry_key"),
        project("subscription_names", "subscriptions",
                {"subscription_key": "account_key", "subscriptions.metric": "subscriptions.metric"}),
        join("all", "finance_registry", "subscription_names", "registry_key", "subscription_key"),
        project("answer", "all", {"customer": "account_key", "churn": "subscriptions.metric"}),
    ]}
    result = review_column_matches(plan)["answer"]
    assert result["customer"] == {
        ("finance", "account_key"), ("registry", "account_key"), ("subscriptions", "account_key"),
    }
    assert result["churn"] == {("subscriptions", "subscriptions.metric")}
    assert ("subscriptions", "subscriptions.metric") not in result["customer"]


def test_semijoin_preserves_only_its_left_review_origins():
    plan = {"nodes": [
        query("left"), query("right"),
        join("cohort", "left", "right", "account_key", "account_key", "SemiJoinCohort"),
    ]}
    result = review_column_matches(plan)["cohort"]
    assert result["account_key"] == {("left", "account_key")}
    assert "right.metric" not in result


def test_unreviewed_duplicate_nonkey_columns_remain_ambiguous():
    plan = {"nodes": [
        query("left"), query("right"),
        project("right_names", "right", {"other_key": "account_key", "left.metric": "right.metric"}),
        join("bad", "left", "right_names", "account_key", "other_key"),
    ]}
    with pytest.raises(RuntimeError, match="duplicate output column"):
        review_column_matches(plan)
