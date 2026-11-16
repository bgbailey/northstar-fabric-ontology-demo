"""Synthetic known-mapping qualification, never cloud/model calls or population-total proof."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from test_multihop import MemoryProvider, account_relationship, cohort_nodes, context, plan, raw, relation

from telco_iq.catalog import Catalog
from telco_iq.contracts import Scope, Status
from telco_iq.executor import Executor

ROOT = Path(__file__).resolve().parents[1]


def case(*, qualified=True, rank=True, target_partial=False, forged=False):
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    scope = Scope(tenant="synthetic", principal="local", authorization_revision="1",
                  models=list(catalog.models), mode="OFFLINE_SYNTHETIC")
    nodes = cohort_nodes()
    nodes[-1].update(
        columns=[], metrics=["finance.recognized_service_revenue"], group_by=["account_key", "month"],
        expected_grain=["account_key", "month"], completeness="qualified_partial" if qualified else "full",
    )
    if qualified:
        nodes[-1]["cohort_filters"][0]["population"] = "known_mapped_subset"
    nodes += [
        raw("dimension", "finance", "account", ["account_key", "display_name"], ["account_key"]),
        dict(id="enriched", kind="JoinResults", left="target", right="dimension",
             relationship="target-dimension", left_keys=["account_key"], right_keys=["account_key"],
             cardinality="many_to_one", unmatched="error"),
    ]
    if rank:
        nodes.append(dict(id="ranked", kind="RankCandidates", source="enriched",
                          column="finance.recognized_service_revenue", descending=True, limit=10,
                          population="known_mapped_subset"))
    nodes.append(dict(id="answer", kind="SynthesizeAnswer", source="ranked" if rank else "enriched"))
    provider = MemoryProvider(catalog, {
        "source": [{"contract_key": 1, "account_key": 7}, {"contract_key": 2, "account_key": None},
                   {"contract_key": 3, "account_key": 7}],
        "target": [{"account_key": 7, "month": "2026-07-01", "finance.recognized_service_revenue": 250}],
        "dimension": [{"account_key": 7, "display_name": "Synthetic Account"}],
    })
    original = provider.query

    async def observed(node, current):
        value, receipt = await original(node, current)
        if node.id == "source":
            value = value.model_copy(update={"coverage": {"unknown_keys": 2}})
        if node.id == "target" and target_partial:
            value = value.model_copy(update={"status": Status.PARTIAL, "coverage": {"unknown_source_rows": 1}})
        if node.id == "target" and forged:
            value = value.model_copy(update={"population_qualification": "known_mapped_subset"})
        return value, receipt

    provider.query = observed
    ctx = context(scope, [
        account_relationship(),
        relation("target-dimension", ("finance", "account_month"), ("finance", "account"),
                 "account_key", "account_key", cardinality="many_to_one"),
    ])
    return catalog, scope, nodes, provider, ctx


async def test_known_cohort_deduplicates_valid_keys_reports_exclusions_and_stays_partial_through_enrichment_and_rank():
    catalog, scope, nodes, provider, ctx = case()
    result = await Executor(catalog, provider, ctx).execute(plan(scope, nodes), scope)
    assert result.status == Status.PARTIAL, result.errors
    assert result.answer.population_qualification == "known_mapped_subset"
    assert result.answer.rows == [{
        "account_key": 7, "month": "2026-07-01", "finance.recognized_service_revenue": 250,
        "display_name": "Synthetic Account",
    }]
    assert next(node for node in provider.calls if node.id == "target").filters[-1].values == [7]
    exclusion = result.answer.exclusions[0]
    assert exclusion.source_node == "keys"
    assert exclusion.unknown_mapping_observations == 2 and exclusion.null_key_rows == 1
    assert exclusion.evidence_ids == ["receipt-source"]
    assert result.answer.evidence_ids == ["receipt-source", "receipt-target", "receipt-dimension"]
    assert "Ranked known mapped subset only" in result.answer.warnings[-1]
    assert next(receipt for receipt in result.receipts if receipt.node_id == "target").status == Status.PARTIAL


async def test_default_complete_cohort_still_blocks_unknowns_before_target_query():
    catalog, scope, nodes, provider, ctx = case(qualified=False)
    result = await Executor(catalog, provider, ctx).execute(plan(scope, nodes), scope)
    assert result.status == Status.PARTIAL and result.answer is None
    assert "source" in [node.id for node in provider.calls]
    assert "target" not in [node.id for node in provider.calls]
    assert "unknown/unmapped" in result.errors[0]


async def test_known_subset_ranking_requires_explicit_population_mode():
    catalog, scope, nodes, provider, ctx = case()
    nodes[-2].pop("population")
    result = await Executor(catalog, provider, ctx).execute(plan(scope, nodes), scope)
    assert result.status == Status.PARTIAL and result.answer is None
    assert "Incomplete dependency" in result.errors[0]


async def test_unknown_target_source_inputs_cannot_gain_known_row_completeness_or_support_ranking():
    catalog, scope, nodes, provider, ctx = case(target_partial=True)
    result = await Executor(catalog, provider, ctx).execute(plan(scope, nodes), scope)
    assert result.status == Status.PARTIAL and result.answer is None
    assert "Incomplete dependency" in result.errors[0]


async def test_source_provider_cannot_mint_runtime_qualification():
    catalog, scope, nodes, provider, ctx = case(forged=True)
    result = await Executor(catalog, provider, ctx).execute(plan(scope, nodes), scope)
    assert result.status == Status.FAILED and result.answer is None
    assert "cannot mint" in result.errors[0]


async def test_qualified_known_results_still_cannot_be_summed_into_complete_population_totals():
    catalog, scope, nodes, provider, ctx = case(rank=False)
    nodes.insert(-1, dict(id="total", kind="AggregateResult", source="enriched", group_by=[],
                         sums=["finance.recognized_service_revenue"]))
    nodes[-1]["source"] = "total"
    result = await Executor(catalog, provider, ctx).execute(plan(scope, nodes), scope)
    assert result.status == Status.PARTIAL and result.answer is None
    assert "Incomplete dependency" in result.errors[0]


def test_known_cohort_cannot_request_full_population_semantics():
    _, scope, nodes, _, _ = case()
    nodes[2]["completeness"] = "full"
    with pytest.raises(ValidationError, match="qualified_partial"):
        plan(scope, nodes)
