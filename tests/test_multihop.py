"""Focused local runtime regressions; no cloud/model providers or evaluator data."""

import asyncio
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from telco_iq.catalog import Catalog, digest
from telco_iq.contracts import (
    Artifact,
    Clocks,
    Context,
    Filter,
    JoinNode,
    Plan,
    ProjectColumns,
    QueryNode,
    Receipt,
    Relationship,
    ReviewedIntervalOrigin,
    Scope,
    SourceClock,
    SourceColumn,
    SourcePopulation,
    Status,
    TemporalPredicate,
)
from telco_iq.errors import LabError
from telco_iq.executor import BoundedRows, Executor, compatible_clocks, join, project
from telco_iq.providers import OfflineProvider, file_hash

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def catalog():
    return Catalog(ROOT / "data" / "contracts" / "catalog.json")


@pytest.fixture
def scope(catalog):
    return Scope(
        tenant="synthetic", principal="local", authorization_revision="1",
        models=list(catalog.models), mode="OFFLINE_SYNTHETIC",
    )


def context(scope, relationships=()):
    return Context(
        mode="OFFLINE_SYNTHETIC", revision="1", source="synthetic runtime test", scope=scope,
        relationships=list(relationships), rules=[], provenance=["Local structural test fixture"],
    )


def plan(scope, nodes, **kwargs):
    return Plan.model_validate({
        "request_id": "runtime-test", "question": "Explicit synthetic test plan",
        "principal_scope": scope, "as_of": "2026-10-01T00:00:00Z", "ontology_revision": "1",
        "metric_selections": list(dict.fromkeys(m for n in nodes for m in n.get("metrics", []))),
        "nodes": nodes, "result_node": nodes[-1]["id"], **kwargs,
    })


def raw(identifier, model, table, columns, grain, date_role=None, **kwargs):
    return dict(
        id=identifier, kind="QuerySemanticModel", model=model, table=table, columns=columns,
        expected_grain=grain, date_role=date_role, **kwargs,
    )


def projection(identifier, source, **columns):
    return dict(id=identifier, kind="ProjectColumns", source=source, columns=columns)


def relation(identifier, left, right, left_key, right_key, **kwargs):
    return Relationship(
        id=identifier, left_model=left[0], left_table=left[1],
        right_model=right[0], right_table=right[1], left_keys=[left_key], right_keys=[right_key],
        cardinality=kwargs.pop("cardinality", "one_to_one"), version="1", purposes=["Synthetic test"],
        **kwargs,
    )


class MemoryProvider:
    mode = "OFFLINE_SYNTHETIC"

    def __init__(self, catalog, outputs):
        self.catalog, self.outputs, self.calls = catalog, outputs, []
        self.source_populations = [
            SourcePopulation(
                model=model, evidence_kind="LOCAL_FIXTURE", binding_hash=digest({"unit-model": model}),
                profile="explicit-memory-unit", seed=1, generator_revision="memory-unit-1",
                identity_revision=digest("memory-identities"), manifest_hash=digest("memory-manifest"),
                table_hashes={name: digest(name) for name in catalog.models[model]["tables"]},
                shared_map_hashes={"account": digest("memory-shared-accounts")},
            ) for model in catalog.models
        ]

    async def query(self, node, scope):
        self.calls.append(node)
        rows = self.outputs[node.id]
        clocks = Clocks(
            data=f"data-{node.model}", model=f"model-{node.model}", ontology="1", context="1",
        )
        value = Artifact(
            id=node.id, rows=rows, status=Status.COMPLETE, scope=scope, clocks=clocks,
            source_models=[node.model], source_tables=[node.table],
            source_populations=[entry for entry in self.source_populations if entry.model == node.model],
            **self.catalog.query_schema(node).model_dump(),
            source_clocks=[SourceClock(
                model=node.model, table=node.table, data=clocks.data, model_revision=clocks.model,
                table_data=f"table-{node.table}",
            )],
            evidence_ids=[f"receipt-{node.id}"],
        )
        sql, parameters = self.catalog.sql(node)
        return value, Receipt(
            id=value.evidence_ids[0], node_id=node.id, lane=self.mode, tool="SYNTHETIC_TEST",
            model=node.model, query=sql, parameters=parameters, elapsed_ms=0,
            returned_rows=len(rows), status=value.status, completeness_evidence="Explicit in-memory test rows",
            clocks=clocks,
        )


def temporal_nodes():
    return [
        raw("incidents", "network", "incident", ["incident_key", "site_key", "start", "end"],
            ["incident_key"], "start"),
        projection("i", "incidents", incident="incident_key", site="site_key", start="start", end="end"),
        raw("mappings", "enterprise", "service_site_map",
            ["service_key", "site_key", "contract_key", "valid_from", "valid_to"],
            ["service_key", "valid_from"], "valid_from"),
        projection("m", "mappings", service="service_key", mapped_site="site_key",
                   mapped_contract="contract_key", map_start="valid_from", map_end="valid_to"),
        dict(
            id="im", kind="JoinResults", left="i", right="m", relationship="incident-map",
            left_keys=["site"], right_keys=["mapped_site"], cardinality="many_to_many",
            unmatched="exclude", max_fanout=2,
            temporal=dict(left_start="start", left_end="end", right_start="map_start", right_end="map_end"),
            overlap_start="hit_start", overlap_end="hit_end",
        ),
        raw("contracts", "enterprise", "contract",
            ["contract_key", "account_key", "valid_from", "valid_to"], ["contract_key"], "valid_from"),
        projection("c", "contracts", contract="contract_key", account="account_key",
                   contract_start="valid_from", contract_end="valid_to"),
        dict(
            id="imc", kind="JoinResults", left="im", right="c", relationship="map-contract",
            left_keys=["mapped_contract"], right_keys=["contract"], cardinality="many_to_one",
            unmatched="exclude",
            temporal=dict(left_start="hit_start", left_end="hit_end",
                          right_start="contract_start", right_end="contract_end"),
            overlap_start="final_start", overlap_end="final_end",
        ),
    ]


def temporal_relationships():
    return [
        relation(
            "incident-map", ("network", "incident"), ("enterprise", "service_site_map"),
            "site_key", "site_key", cardinality="many_to_many",
            temporal=TemporalPredicate(
                left_start="start", left_end="end", right_start="valid_from", right_end="valid_to",
            ),
        ),
        relation(
            "map-contract", ("enterprise", "service_site_map"), ("enterprise", "contract"),
            "contract_key", "contract_key", cardinality="many_to_one",
            temporal=TemporalPredicate(
                left_start="valid_from", left_end="valid_to", right_start="valid_from", right_end="valid_to",
            ),
        ),
    ]


def timestamp(hour, day=1):
    return f"2026-07-{day:02d}T{hour:02d}:00:00+00:00"


def temporal_outputs():
    return {
        "incidents": [
            dict(incident_key=1, site_key=7, start=timestamp(0), end=timestamp(2)),
            dict(incident_key=2, site_key=7, start=timestamp(1), end=timestamp(3)),
        ],
        "mappings": [
            dict(service_key=1, site_key=7, contract_key=11, valid_from=timestamp(0), valid_to=timestamp(0, 31)),
            dict(service_key=2, site_key=7, contract_key=22, valid_from=timestamp(0), valid_to=timestamp(0, 31)),
        ],
        "contracts": [
            dict(contract_key=11, account_key=1, valid_from=timestamp(1), valid_to=timestamp(2)),
            dict(contract_key=22, account_key=2, valid_from=timestamp(0, 2), valid_to=timestamp(0, 30)),
        ],
    }


async def test_two_by_two_raw_temporal_matches_retain_all_intervals(catalog, scope):
    provider = MemoryProvider(catalog, temporal_outputs())
    executor = Executor(catalog, provider, context(scope, temporal_relationships()))
    result = await executor.execute(plan(scope, temporal_nodes()[:5]), scope)
    assert result.status == Status.COMPLETE, result.errors
    assert {(r["incident"], r["service"]) for r in result.answer.rows} == {(1, 1), (1, 2), (2, 1), (2, 2)}
    assert all(r["hit_start"] == r["start"] and r["hit_end"] == r["end"] for r in result.answer.rows)
    assert result.answer.grain == ["incident", "service", "map_start"]
    assert len(result.answer.source_clocks) == 2
    assert set(result.answer.aggregation.values()) == {"raw"}


async def test_chained_alias_lineage_clips_incident_mapping_contract(catalog, scope):
    provider = MemoryProvider(catalog, temporal_outputs())
    executor = Executor(catalog, provider, context(scope, temporal_relationships()))
    result = await executor.execute(plan(scope, temporal_nodes()), scope)
    assert result.status == Status.COMPLETE, result.errors
    assert len(result.answer.rows) == 2
    assert {r["contract"] for r in result.answer.rows} == {11}
    assert all(r["final_start"] == timestamp(1) and r["final_end"] == timestamp(2) for r in result.answer.rows)
    assert result.answer.source_tables == ["contract", "incident", "service_site_map"]
    assert result.answer.lineage["mapped_contract"] == [
        SourceColumn(model="enterprise", table="service_site_map", column="contract_key"),
    ]
    assert len(result.answer.lineage["final_start"]) == 3
    assert result.answer.grain == ["incident", "service", "map_start"]


def native_hop_temporal_case():
    outputs = temporal_outputs() | {
        "network_sites": [{"site_key": 7}],
        "enterprise_sites": [{"site_key": 7}],
    }
    origin = ReviewedIntervalOrigin(
        model="network", table="incident", grain=["incident_key"], keys=["site_key"],
        via_relationships=["incident-site", "site-identity"],
    )
    relationships = [
        relation("site-identity", ("network", "site"), ("enterprise", "site"), "site_key", "site_key"),
        relation("incident-site", ("network", "incident"), ("network", "site"),
                 "site_key", "site_key", cardinality="many_to_one"),
        relation(
            "site-map", ("enterprise", "site"), ("enterprise", "service_site_map"),
            "site_key", "site_key", cardinality="many_to_many", left_interval_origin=origin,
            temporal=TemporalPredicate(left_start="start", left_end="end", right_start="valid_from", right_end="valid_to"),
        ),
        temporal_relationships()[1],
    ]
    nodes = [
        raw("network_sites", "network", "site", ["site_key"], ["site_key"]),
        projection("ns", "network_sites", network_site="site_key"),
        raw("enterprise_sites", "enterprise", "site", ["site_key"], ["site_key"]),
        projection("es", "enterprise_sites", enterprise_site="site_key"),
        dict(id="sitebridge", kind="JoinResults", left="ns", right="es", relationship="site-identity",
             left_keys=["network_site"], right_keys=["enterprise_site"], cardinality="one_to_one", unmatched="error"),
        *temporal_nodes()[:2],
        dict(id="with_sites", kind="JoinResults", left="i", right="sitebridge", relationship="incident-site",
             left_keys=["site"], right_keys=["network_site"], cardinality="many_to_one", unmatched="error"),
        *temporal_nodes()[2:4],
        {**temporal_nodes()[4], "left": "with_sites", "relationship": "site-map", "left_keys": ["enterprise_site"]},
        *temporal_nodes()[5:],
    ]
    return outputs, relationships, nodes


async def test_carried_incident_interval_follows_actual_dimension_identity_hops_without_fact_shortcut(catalog, scope):
    outputs, relationships, nodes = native_hop_temporal_case()
    result = await Executor(catalog, MemoryProvider(catalog, outputs), context(scope, relationships)).execute(
        plan(scope, nodes), scope,
    )
    assert result.status == Status.COMPLETE, result.errors
    assert len(result.answer.rows) == 2
    assert {row["contract"] for row in result.answer.rows} == {11}
    assert all(row["final_start"] == timestamp(1) and row["final_end"] == timestamp(2) for row in result.answer.rows)
    assert set(result.answer.relationship_history) == {"site-identity", "incident-site", "site-map", "map-contract"}
    assert all(not (r.left_table == "incident" and r.right_table == "service_site_map") for r in relationships)


@pytest.mark.parametrize("change", ["history", "grain", "derived-key", "key-values"])
async def test_carried_interval_cannot_borrow_untraversed_ungrained_or_different_site_time(catalog, scope, change):
    outputs, relationships, nodes = native_hop_temporal_case()
    current = context(scope, relationships)
    executor = Executor(catalog, MemoryProvider(catalog, outputs), current)
    prefix = await executor.execute(plan(scope, nodes[:8]), scope)
    assert prefix.status == Status.COMPLETE, prefix.errors
    left = prefix.answer.model_copy(deep=True)
    right, _ = await executor.provider.query(QueryNode.model_validate(nodes[8]), scope)
    right = project(ProjectColumns.model_validate(nodes[9]), right)
    node = JoinNode.model_validate(nodes[10])
    if change == "history":
        left.relationship_history = []
    elif change == "grain":
        left.grain = ["enterprise_site"]
    elif change == "derived-key":
        left.derived_columns.append("site")
    else:
        left.rows[0]["enterprise_site"] = 99
    with pytest.raises(LabError, match="Carried interval"):
        join(node, left, right, current)


def test_old_relationship_and_result_shapes_omit_unconfigured_interval_origins(catalog):
    old = relation("old", ("finance", "account"), ("subscriptions", "account"), "account_key", "account_key")
    assert "left_interval_origin" not in old.model_dump(mode="json")
    assert "right_interval_origin" not in old.model_dump(mode="json")
    query = QueryNode.model_validate(raw("accounts", "finance", "account", ["account_key"], ["account_key"]))
    assert "relationship_history" not in catalog.query_schema(query).model_dump(mode="json")


async def test_preserved_shared_temporal_key_cannot_authorize_contract_cohort(catalog, scope):
    nodes = temporal_nodes()
    nodes[-2]["columns"]["mapped_contract"] = nodes[-2]["columns"].pop("contract")
    nodes[-1].update(right_keys=["mapped_contract"], unmatched="preserve")
    target = raw(
        "target", "enterprise", "contract", ["contract_key"], ["contract_key"], "valid_from",
        cohort_filters=[dict(source="imc", source_column="mapped_contract", target_column="contract_key")],
    )
    outputs = temporal_outputs() | {"target": [dict(contract_key=11), dict(contract_key=22)]}
    provider = MemoryProvider(catalog, outputs)
    executor = Executor(catalog, provider, context(scope, temporal_relationships()))

    result = await executor.execute(plan(scope, nodes + [target]), scope)
    assert result.status == Status.FAILED, result.answer
    assert "lineage" in result.errors[0]
    assert result.answer is None and provider.calls == [] and result.receipts == []

    preserved = await executor.execute(plan(scope, nodes), scope)
    assert preserved.status == Status.COMPLETE, preserved.errors
    assert len(preserved.answer.rows) == 4
    assert preserved.answer.coverage["unmapped"] == 2
    assert preserved.answer.coverage["unknown_keys"] == 0
    misses = [row for row in preserved.answer.rows if row["mapped_contract"] == 22]
    assert len(misses) == 2
    assert all(
        row["account"] is None and row["final_start"] is None and row["final_end"] is None
        for row in misses
    )
    assert preserved.answer.lineage["mapped_contract"] == [
        SourceColumn(model="enterprise", table="service_site_map", column="contract_key"),
    ]


async def test_excluded_shared_temporal_key_authorizes_only_matched_contract_cohort(catalog, scope):
    nodes = temporal_nodes()
    nodes[-2]["columns"]["mapped_contract"] = nodes[-2]["columns"].pop("contract")
    nodes[-1]["right_keys"] = ["mapped_contract"]
    target = raw(
        "target", "enterprise", "contract", ["contract_key"], ["contract_key"], "valid_from",
        cohort_filters=[dict(source="imc", source_column="mapped_contract", target_column="contract_key")],
    )
    provider = MemoryProvider(catalog, temporal_outputs() | {"target": [dict(contract_key=11)]})
    result = await Executor(catalog, provider, context(scope, temporal_relationships())).execute(
        plan(scope, nodes + [target]), scope,
    )
    assert result.status == Status.COMPLETE, result.errors
    assert result.answer.rows == [dict(contract_key=11)]
    assert provider.calls[-1].id == "target"
    assert provider.calls[-1].filters[-1] == Filter(column="contract_key", op="in", values=[11])
    assert result.answer.coverage["unmapped"] == 2
    assert result.answer.coverage["unknown_keys"] == 0


async def test_preserved_distinct_right_temporal_key_blocks_null_cohort(catalog, scope):
    nodes = temporal_nodes()
    nodes[-1]["unmatched"] = "preserve"
    nodes.append(raw(
        "target", "enterprise", "contract", ["contract_key"], ["contract_key"], "valid_from",
        cohort_filters=[dict(source="imc", source_column="contract", target_column="contract_key")],
    ))
    provider = MemoryProvider(catalog, temporal_outputs())
    result = await Executor(catalog, provider, context(scope, temporal_relationships())).execute(
        plan(scope, nodes), scope,
    )
    assert result.status == Status.PARTIAL and result.answer is None
    assert "missing/null" in result.errors[0]
    assert all(node.id != "target" for node in provider.calls)
    assert all(receipt.node_id != "target" for receipt in result.receipts)


@pytest.mark.parametrize("boundary", [timestamp(3), timestamp(0, 2)])
async def test_broad_mapping_does_not_admit_nonoverlapping_contract(catalog, scope, boundary):
    outputs = temporal_outputs()
    outputs["contracts"][0]["valid_from"] = boundary
    outputs["contracts"][0]["valid_to"] = timestamp(0, 30)
    provider = MemoryProvider(catalog, outputs)
    result = await Executor(catalog, provider, context(scope, temporal_relationships())).execute(
        plan(scope, temporal_nodes()), scope,
    )
    assert result.status == Status.COMPLETE, result.errors
    assert result.answer.rows == []
    assert result.answer.coverage["unknown_keys"] == 0


async def test_chained_join_cannot_revert_to_broad_unclipped_mapping(catalog, scope):
    nodes = temporal_nodes()
    nodes[-1]["temporal"]["left_start"] = "map_start"
    nodes[-1]["temporal"]["left_end"] = "map_end"
    provider = MemoryProvider(catalog, temporal_outputs())
    result = await Executor(catalog, provider, context(scope, temporal_relationships())).execute(
        plan(scope, nodes), scope,
    )
    assert result.status == Status.FAILED and provider.calls == []
    assert "clipped intersection" in result.errors[0]


async def test_derived_intersection_is_not_an_exact_identity_cohort(catalog, scope):
    nodes = temporal_nodes()[:5] + [
        projection("narrowed", "im", clipped="hit_start"),
        raw(
            "target", "enterprise", "service_site_map", ["service_key", "valid_from"],
            ["service_key", "valid_from"], "valid_from",
            cohort_filters=[dict(source="narrowed", source_column="clipped", target_column="valid_from")],
        ),
    ]
    provider = MemoryProvider(catalog, temporal_outputs())
    result = await Executor(catalog, provider, context(scope, temporal_relationships())).execute(
        plan(scope, nodes), scope,
    )
    assert result.status == Status.FAILED and provider.calls == []
    assert "unchanged source-column identity" in result.errors[0]


@pytest.mark.parametrize("change", ["wrong-key", "collision"])
async def test_invalid_join_identity_or_collision_fails_before_query(catalog, scope, change):
    nodes = temporal_nodes()[:5]
    if change == "wrong-key":
        nodes[-1]["left_keys"] = ["incident"]
    else:
        nodes[3]["columns"]["start"] = nodes[3]["columns"].pop("map_start")
        nodes[-1]["temporal"]["right_start"] = "start"
    provider = MemoryProvider(catalog, temporal_outputs())
    result = await Executor(catalog, provider, context(scope, temporal_relationships())).execute(
        plan(scope, nodes), scope,
    )
    assert result.status == Status.FAILED and provider.calls == []
    assert ("lineage" if change == "wrong-key" else "Ambiguous") in result.errors[0]


async def test_many_to_many_materializes_only_within_budget(catalog, scope, monkeypatch):
    provider = MemoryProvider(catalog, temporal_outputs())
    sizes = []
    append = BoundedRows.append

    def observed_append(self, row):
        append(self, row)
        sizes.append(len(self.rows))

    monkeypatch.setattr(BoundedRows, "append", observed_append)
    nodes = temporal_nodes()[:5]
    for node in nodes:
        if node["kind"] == "QuerySemanticModel":
            node["max_rows"] = 10
    result = await Executor(catalog, provider, context(scope, temporal_relationships())).execute(
        plan(scope, nodes, budgets={"max_rows": 10}), scope,
    )
    assert result.status == Status.PARTIAL
    assert sizes == [1, 2]  # Eight source/projection rows consumed the rest of the budget.
    assert "while materializing" in result.errors[0]


def test_byte_budget_rejects_row_before_materializing():
    output = BoundedRows(max_rows=100, max_bytes=8)
    with pytest.raises(LabError, match="while materializing"):
        output.append({"key": "larger than the budget"})
    assert output.rows == []


async def test_fanout_limit_and_governed_many_to_many_are_rejected(catalog, scope):
    provider = MemoryProvider(catalog, temporal_outputs())
    nodes = temporal_nodes()[:5]
    nodes[-1]["max_fanout"] = 1
    executor = Executor(catalog, provider, context(scope, temporal_relationships()))
    result = await executor.execute(plan(scope, nodes), scope)
    assert result.status == Status.FAILED and "fan-out" in result.errors[0]
    left, _ = await provider.query(QueryNode.model_validate(nodes[0]), scope)
    right, _ = await provider.query(QueryNode.model_validate(nodes[2]), scope)
    left = project(ProjectColumns.model_validate(nodes[1]), left)
    right = project(ProjectColumns.model_validate(nodes[3]), right)
    right.aggregation["service"] = "additive"
    with pytest.raises(LabError, match="reviewed RAW"):
        join(JoinNode.model_validate(nodes[-1]), left, right, executor.context)


def cohort_nodes():
    return [
        raw("source", "enterprise", "contract", ["contract_key", "account_key"], ["contract_key"], "valid_from"),
        projection("keys", "source", affected_account="account_key"),
        raw(
            "target", "finance", "account_month", ["account_key"], ["account_key"], "month",
            filters=[dict(column="month", op="eq", values=["2026-07-01"])],
            cohort_filters=[dict(
                source="keys", source_column="affected_account", target_column="account_key",
                relationship="accounts",
            )],
        ),
    ]


def account_relationship(**kwargs):
    return relation(
        "accounts", ("enterprise", "contract"), ("finance", "account_month"),
        "account_key", "account_key", **kwargs,
    )


async def test_cohort_deduplicates_and_preserves_bound_parameters_and_provenance(catalog, scope):
    provider = MemoryProvider(catalog, {
        "source": [dict(contract_key=1, account_key=7), dict(contract_key=2, account_key=7)],
        "target": [dict(account_key=7)],
    })
    events = []
    async def capture(event):
        events.append(event)
    result = await Executor(catalog, provider, context(scope, [account_relationship()])).execute(
        plan(scope, cohort_nodes()), scope, on_event=capture,
    )
    assert result.status == Status.COMPLETE, result.errors
    assert provider.calls[-1].cohort_filters == []
    assert provider.calls[-1].filters[-1].values == [7]
    assert result.receipts[-1].parameters == ["2026-07-01", 7]
    assert result.answer.evidence_ids == ["receipt-source", "receipt-target"]
    assert len(result.answer.source_clocks) == 2
    assert [(e.node_id, e.phase) for e in events] == [
        ("source", "started"), ("source", "completed"), ("keys", "started"), ("keys", "completed"),
        ("target", "started"), ("target", "completed"),
    ]
    assert events[-1].receipt_id == "receipt-target" and events[-1].returned_rows == 1
    assert events[3].receipt_id is None and all(e.elapsed_ms >= 0 for e in events)


@pytest.mark.parametrize("rows", [[], [dict(contract_key=1, account_key=7)]])
@pytest.mark.parametrize("change", ["profile", "seed", "shared_map"])
async def test_destination_population_checked_before_query_even_for_empty_cohort(catalog, scope, rows, change):
    provider = MemoryProvider(catalog, {"source": rows, "target": [dict(account_key=7)]})
    destination = next(entry for entry in provider.source_populations if entry.model == "finance")
    if change == "profile":
        destination.profile = "different-profile-reusing-account-7"
    elif change == "seed":
        destination.seed += 1
    else:
        destination.shared_map_hashes["account"] = digest("different-account-7")
    result = await Executor(catalog, provider, context(scope, [account_relationship()])).execute(
        plan(scope, cohort_nodes()), scope,
    )
    assert result.status == Status.BLOCKED_CAPABILITY and result.answer is None
    assert "population" in result.errors[0]
    assert [node.id for node in provider.calls] == ["source"]
    assert [receipt.node_id for receipt in result.receipts] == ["source"]


async def test_live_execution_never_defaults_to_local_population(catalog, scope):
    live = scope.model_copy(update={"mode": "LIVE_DELEGATED"})
    provider = MemoryProvider(catalog, {"source": [dict(contract_key=1, account_key=7)]})
    result = await Executor(catalog, provider, context(live, [account_relationship()])).execute(
        plan(live, cohort_nodes()), live,
    )
    assert result.status == Status.BLOCKED_CAPABILITY and provider.calls == []
    assert "local fixture provenance" in result.errors[0]


@pytest.mark.parametrize("rows", [[], [dict(contract_key=1, account_key=7)]])
async def test_current_population_cannot_relabel_cached_unknown_result(catalog, scope, rows):
    from telco_iq.cache import CacheKey, ScopedCache

    async def authorize(_):
        return True

    cache = ScopedCache(authorize)
    key = CacheKey("synthetic", "local", "1", "source", "1", "1", "1", "same-keys")

    class CachedProvider(MemoryProvider):
        async def query(self, node, principal):
            async def load():
                value, receipt = await super(CachedProvider, self).query(node, principal)
                value.source_populations = []
                return value, receipt

            await cache.get(key, load)
            return await cache.get(key, load)

    provider = CachedProvider(catalog, {"source": rows})
    result = await Executor(catalog, provider, context(scope, [account_relationship()])).execute(
        plan(scope, cohort_nodes()), scope,
    )
    assert result.status == Status.BLOCKED_CAPABILITY and result.answer is None
    assert "cannot relabel cached output" in result.errors[0]
    assert [node.id for node in provider.calls] == ["source"]


@pytest.mark.parametrize("named", [False, True])
async def test_site_key_cannot_be_transferred_to_account_even_if_values_equal(catalog, scope, named):
    nodes = cohort_nodes()
    nodes[0] = raw("source", "network", "incident", ["incident_key", "site_key"], ["incident_key"], "start")
    nodes[1]["columns"] = {"affected_account": "site_key"}
    if not named:
        nodes[2]["cohort_filters"][0]["relationship"] = None
    provider = MemoryProvider(catalog, {"source": [dict(incident_key=1, site_key=7)], "target": [dict(account_key=7)]})
    result = await Executor(catalog, provider, context(scope, [account_relationship()])).execute(
        plan(scope, nodes), scope,
    )
    assert result.status == Status.FAILED and provider.calls == []
    assert "lineage" in result.errors[0]


async def test_unnamed_cohort_requires_exact_model_table_column(catalog, scope):
    nodes = cohort_nodes()
    nodes[0] = raw("source", "finance", "account_month", ["account_key"], ["account_key"], "month")
    nodes[-1]["cohort_filters"][0]["relationship"] = None
    provider = MemoryProvider(catalog, {"source": [dict(account_key=7)], "target": [dict(account_key=7)]})
    result = await Executor(catalog, provider, context(scope)).execute(plan(scope, nodes), scope)
    assert result.status == Status.COMPLETE, result.errors


@pytest.mark.parametrize("rows, message", [
    ([], "Empty cohort"),
    ([dict(contract_key=1, account_key=None)], "missing/null"),
    ([dict(contract_key=i, account_key=i) for i in range(251)], "key budget"),
])
async def test_empty_unknown_and_excessive_cohorts_never_call_target(catalog, scope, rows, message):
    provider = MemoryProvider(catalog, {"source": rows})
    nodes = cohort_nodes() + [dict(id="answer", kind="SynthesizeAnswer", source="target")]
    events = []
    async def capture(event):
        events.append(event)
    result = await Executor(catalog, provider, context(scope, [account_relationship()])).execute(
        plan(scope, nodes), scope, on_event=capture,
    )
    assert result.status in (Status.BLOCKED_CAPABILITY, Status.PARTIAL)
    assert message in result.errors[0] and result.answer is None
    assert [n.id for n in provider.calls] == ["source"]
    assert [r.node_id for r in result.receipts] == ["source"]
    assert events[-2].node_id == "target" and events[-2].phase == "failed"
    assert events[-2].receipt_id is None
    assert events[-1].phase == "skipped" and events[-1].node_id == "answer"


async def test_period_constraints_survive_projection_and_block_cohort_drift(catalog, scope):
    nodes = cohort_nodes()
    nodes[0]["table"] = "sla_evaluation"
    nodes[0]["date_role"] = "month"
    nodes[0]["filters"] = [dict(column="month", op="eq", values=["2026-06-01"])]
    provider = MemoryProvider(catalog, {})
    relationship = account_relationship(aligned_filters=["month"])
    relationship.left_table = "sla_evaluation"
    result = await Executor(catalog, provider, context(scope, [
        relationship,
    ])).execute(plan(scope, nodes), scope)
    assert result.status == Status.FAILED and provider.calls == []
    assert "periods differ" in result.errors[0]


async def test_aggregate_keeps_alias_lineage_and_single_period_constraints(catalog, scope):
    nodes = [
        dict(
            id="money", kind="QuerySemanticModel", model="finance", table="account_month",
            metrics=["finance.recognized_service_revenue"], group_by=["account_key", "market_id"],
            expected_grain=["account_key", "market_id"], date_role="month",
            filters=[dict(column="month", op="eq", values=["2026-07-01"])],
        ),
        projection("renamed", "money", account="account_key", revenue="finance.recognized_service_revenue"),
        dict(id="summed", kind="AggregateResult", source="renamed", group_by=["account"], sums=["revenue"]),
        raw("other", "subscriptions", "subscription_month", ["account_key"], ["account_key"], "month",
            filters=[dict(column="month", op="eq", values=["2026-07-01"])]),
        dict(
            id="joined", kind="JoinResults", left="summed", right="other", left_keys=["account"],
            right_keys=["account_key"], relationship="monthly", cardinality="one_to_one", unmatched="error",
        ),
    ]
    outputs = {
        "money": [
            {"account_key": 1, "market_id": "a", "finance.recognized_service_revenue": 10},
            {"account_key": 1, "market_id": "b", "finance.recognized_service_revenue": 20},
        ],
        "other": [dict(account_key=1)],
    }
    relationship = relation(
        "monthly", ("finance", "account_month"), ("subscriptions", "subscription_month"),
        "account_key", "account_key", aligned_filters=["month"],
    )
    provider = MemoryProvider(catalog, outputs)
    result = await Executor(catalog, provider, context(scope, [relationship])).execute(plan(scope, nodes), scope)
    assert result.status == Status.COMPLETE, result.errors
    assert result.answer.rows == [dict(account=1, revenue=30, account_key=1)]
    assert result.answer.grain == ["account"]
    assert result.answer.lineage["account"] == [
        SourceColumn(model="finance", table="account_month", column="account_key"),
    ]
    assert len(result.answer.source_filters) == 2


async def test_explicit_source_clocks_allow_unrelated_models_but_reject_shared_drift(catalog, scope):
    provider = MemoryProvider(catalog, {
        "a": [dict(account_key=1)], "b": [dict(site_key=1)], "shared": [dict(account_key=1)],
    })
    a, _ = await provider.query(QueryNode.model_validate(
        raw("a", "finance", "account", ["account_key"], ["account_key"]),
    ), scope)
    b, _ = await provider.query(QueryNode.model_validate(
        raw("b", "network", "site", ["site_key"], ["site_key"]),
    ), scope)
    assert len(compatible_clocks(a, b)) == 2
    shared, _ = await provider.query(QueryNode.model_validate(
        raw("shared", "network", "account", ["account_key"], ["account_key"]),
    ), scope)
    assert len(compatible_clocks(a, shared)) == 2
    shared.source_clocks[0].table_data = "changed"
    with pytest.raises(LabError, match="Shared source clocks"):
        compatible_clocks(a, shared)
    drift = a.model_copy(deep=True)
    drift.source_clocks[0].model_revision = "changed"
    with pytest.raises(LabError, match="Shared source clocks"):
        compatible_clocks(a, drift)
    b.clocks.context = "changed"
    with pytest.raises(LabError, match="Global context/ontology"):
        compatible_clocks(a, b)


async def test_runtime_rejects_reused_source_drift_without_a_join(catalog, scope):
    class DriftingProvider(MemoryProvider):
        async def query(self, node, scope):
            value, receipt = await super().query(node, scope)
            if node.id == "second":
                value.source_clocks[0].data = "changed"
            return value, receipt
    provider = DriftingProvider(catalog, {"first": [dict(account_key=1)], "second": [dict(account_key=1)]})
    nodes = [
        raw("first", "finance", "account", ["account_key"], ["account_key"]),
        raw("second", "finance", "account", ["account_key"], ["account_key"], depends_on=["first"]),
    ]
    result = await Executor(catalog, provider, context(scope)).execute(plan(scope, nodes), scope)
    assert result.status == Status.FAILED and len(result.receipts) == 2
    assert "Shared source clocks" in result.errors[0]


def test_contracts_reject_multiple_cohorts_invalid_intersections_and_empty_in():
    with pytest.raises(ValidationError):
        Filter(column="key", op="in", values=[])
    node = cohort_nodes()[-1]
    node["cohort_filters"] *= 2
    with pytest.raises(ValidationError):
        QueryNode.model_validate(node)
    node = temporal_nodes()[4]
    node.pop("overlap_end")
    with pytest.raises(ValidationError):
        JoinNode.model_validate(node)
    node = temporal_nodes()[4]
    node["temporal"] = None
    with pytest.raises(ValidationError):
        JoinNode.model_validate(node)


def test_gt_compiles_strict_sql_and_dax_and_raw_undated_dimensions(catalog):
    node = QueryNode.model_validate(raw(
        "q", "network", "incident", ["incident_key"], ["incident_key"], "start",
        filters=[dict(column="end", op="gt", values=[timestamp(0)])],
    ))
    sql, params = catalog.sql(node)
    assert '"end" > (?)' in sql and params == [timestamp(0)]
    assert "[end] > DATE(2026, 7, 1) + TIME(0, 0, 0)" in catalog.dax(node)
    catalog.validate_query(QueryNode.model_validate(raw(
        "dimension", "finance", "account", ["account_key"], ["account_key"],
    )))
    with pytest.raises(LabError, match="undated"):
        catalog.validate_query(QueryNode.model_validate(raw(
            "dated", "network", "incident", ["incident_key"], ["incident_key"],
        )))
    with pytest.raises(ValidationError, match="date role"):
        QueryNode.model_validate(dict(
            id="metric", kind="QuerySemanticModel", model="finance", table="account_month",
            metrics=["finance.recognized_service_revenue"], expected_grain=[],
        ))


async def test_real_offline_temporal_query_gt_parameters_and_metadata(catalog, scope, tmp_path):
    source = tmp_path / "incident.parquet"
    table = pa.Table.from_pylist([
        {"incident_key": 1, "start": datetime.fromisoformat(timestamp(0)),
         "end": datetime.fromisoformat(timestamp(1))},
        {"incident_key": 2, "start": datetime.fromisoformat(timestamp(0)),
         "end": datetime.fromisoformat(timestamp(2))},
    ], schema=pa.schema([
        ("incident_key", pa.int64()), ("start", pa.timestamp("us", tz="+00:00")),
        ("end", pa.timestamp("us", tz="+00:00")),
    ]))
    pq.write_table(table, source)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "content_sha256": "synthetic-snapshot",
        "watermarks": {"network": "2026-08-01T00:00:00+00:00"},
        "tables": [{
            "id": "incident", "file": source.name, "bytes": source.stat().st_size,
            "file_sha256": file_hash(source), "content_sha256": "synthetic-incident-content",
        }],
    }))
    nodes = [raw(
        "q", "network", "incident", ["incident_key", "start", "end"], ["incident_key"], "start",
        filters=[dict(column="end", op="gt", values=[timestamp(1)])],
    )]
    result = await Executor(catalog, OfflineProvider(catalog, tmp_path), context(scope)).execute(
        plan(scope, nodes), scope,
    )
    assert result.status == Status.COMPLETE, result.errors
    assert result.answer.rows == [{"incident_key": 2, "start": timestamp(0), "end": timestamp(2)}]
    assert result.receipts[0].parameters == [timestamp(1)]
    assert result.receipts[0].query.endswith(" LIMIT 10001")
    assert result.answer.source_clocks[0].table_data == "synthetic-incident-content"
    assert result.answer.lineage["start"] == [SourceColumn(model="network", table="incident", column="start")]


async def test_unexpected_provider_error_surfaces_with_failed_and_skipped_events(catalog, scope):
    class BrokenProvider:
        mode = "OFFLINE_SYNTHETIC"
        async def query(self, node, scope):
            raise RuntimeError("Synthetic unexpected failure")
    events = []
    async def capture(event):
        events.append(event)
    nodes = [
        raw("q", "finance", "account", ["account_key"], ["account_key"]),
        dict(id="answer", kind="SynthesizeAnswer", source="q"),
    ]
    with pytest.raises(RuntimeError, match="Synthetic unexpected"):
        await Executor(catalog, BrokenProvider(), context(scope)).execute(
            plan(scope, nodes), scope, on_event=capture,
        )
    assert [e.phase for e in events] == ["started", "failed", "skipped"]
    assert all(e.receipt_id is None for e in events)


@pytest.mark.parametrize("cancel_twice", [False, True])
async def test_cancellation_waits_for_owned_query_worker(catalog, scope, tmp_path, cancel_twice):
    entered, exited = threading.Event(), threading.Event()
    class CancellableProvider(OfflineProvider):
        def _query(self, node, scope, cancel, lock, connections):
            entered.set()
            assert cancel.wait(2), "The runtime did not cancel its owned worker"
            time.sleep(0.04)
            exited.set()
            raise LabError("Synthetic interrupted source", Status.TIMED_OUT)
    nodes = [raw("q", "finance", "account", ["account_key"], ["account_key"])]
    events = []
    async def capture(event):
        events.append(event)
    task = asyncio.create_task(Executor(
        catalog, CancellableProvider(catalog, tmp_path), context(scope),
    ).execute(plan(scope, nodes), scope, on_event=capture))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    if cancel_twice:
        await asyncio.sleep(0.01)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()
    assert [e.phase for e in events] == ["started", "failed"]
