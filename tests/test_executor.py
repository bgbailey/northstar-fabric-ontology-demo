import asyncio
from dataclasses import replace

import pytest

from telco_iq.cache import CacheKey, ScopedCache
from telco_iq.contracts import (
    AggregateNode,
    Artifact,
    Clocks,
    Context,
    JoinNode,
    ProjectColumns,
    Relationship,
    RuleNode,
    Scope,
    SourceColumn,
    Status,
    TemporalPredicate,
)
from telco_iq.errors import LabError
from telco_iq.executor import aggregate, join, project, rule

SCOPE = Scope(
    tenant="synthetic", principal="local", authorization_revision="1",
    models=["left", "right"], mode="OFFLINE_SYNTHETIC",
)


def artifact(rows, model="left", aggregation=None, status=Status.COMPLETE):
    return Artifact(
        id=model, rows=rows, status=status, scope=SCOPE,
        clocks=Clocks(data="1", model="1", ontology="1", context="1"),
        source_models=[model], source_tables=[model], grain=["id"],
        source_populations=[population(model)],
        aggregation=aggregation or {}, evidence_ids=[f"receipt-{model}"],
    )


def joining(cardinality="many_to_one", temporal=None, unmatched="error"):
    relation = Relationship(
        id="approved-map", left_model="left", right_model="right",
        left_table="left", right_table="right", left_keys=["id"], right_keys=["key"],
        cardinality=cardinality, temporal=temporal, version="1", purposes=["test"],
    )
    context = Context(
        mode="OFFLINE_SYNTHETIC", revision="1", source="test", scope=SCOPE,
        relationships=[relation], rules=[], provenance=["synthetic test"],
    )
    node = JoinNode(
        id="joined", kind="JoinResults", left="left", right="right",
        relationship=relation.id, left_keys=["id"], right_keys=["key"],
        cardinality=cardinality, unmatched=unmatched, temporal=temporal,
    )
    return node, context


def test_temporal_half_open_reused_key():
    temporal = TemporalPredicate(left_start="start", left_end="end", right_start="from", right_end="to")
    node, context = joining(temporal=temporal)
    left = artifact([{"id": 1, "start": "2026-06-01T00:00:00Z", "end": "2026-06-02T00:00:00Z"}])
    right = artifact([
        {"key": 1, "from": "2026-05-01T00:00:00Z", "to": "2026-06-01T00:00:00Z", "account": "old"},
        {"key": 1, "from": "2026-06-01T00:00:00Z", "to": "2026-07-01T00:00:00Z", "account": "new"},
    ], "right")
    result = join(node, left, right, context)
    assert result.rows[0]["account"] == "new"
    assert result.evidence_ids == ["receipt-left", "receipt-right"]


def test_overlapping_mapping_fails():
    node, context = joining()
    with pytest.raises(LabError, match="fan-out"):
        join(node, artifact([{"id": 1}]), artifact([{"key": 1}, {"key": 1}], "right"), context)


def test_null_keys_never_match():
    node, context = joining(unmatched="exclude")
    result = join(node, artifact([{"id": None}]), artifact([{"key": None}], "right"), context)
    assert result.rows == []
    assert result.coverage["unmapped"] == 1


@pytest.mark.parametrize("kind", ["JoinResults", "ResolveIdentity"])
def test_one_to_one_identity_bridge_preserves_repeated_dimension_identity_at_proven_fact_grain(kind):
    node, context = joining(cardinality="one_to_one")
    node.kind = kind
    left = artifact([{"id": 1, "event": 10}, {"id": 1, "event": 11}]).model_copy(update={
        "grain": ["event"],
        "lineage": {
            "id": [SourceColumn(model="left", table="left", column="id")],
            "event": [SourceColumn(model="left", table="events", column="event")],
        },
        "relationship_history": ["synthetic-fact-to-dimension"],
    })
    result = join(node, left, artifact([{"key": 1}], "right"), context)
    assert result.rows == [{"id": 1, "event": 10, "key": 1}, {"id": 1, "event": 11, "key": 1}]
    assert result.grain == ["event"]
    assert result.coverage["matched"] == 2 and result.coverage["unknown_keys"] == 0


@pytest.mark.parametrize("change", ["no-history", "no-lineage", "unknown-grain", "raw-grain", "duplicate-fact", "right-fanout"])
@pytest.mark.parametrize("kind", ["JoinResults", "ResolveIdentity"])
def test_identity_bridge_never_uses_enrichment_to_hide_unknown_grain_raw_duplicates_or_fanout(change, kind):
    node, context = joining(cardinality="one_to_one")
    node.kind = kind
    left = artifact([{"id": 1, "event": 10}, {"id": 1, "event": 11}]).model_copy(update={
        "grain": ["event"], "relationship_history": ["synthetic-fact-to-dimension"],
        "lineage": {
            "id": [SourceColumn(model="left", table="left", column="id")],
            "event": [SourceColumn(model="left", table="events", column="event")],
        },
    })
    right = artifact([{"key": 1}], "right")
    if change == "no-history":
        left.relationship_history = []
    elif change == "no-lineage":
        left.lineage = {}
    elif change == "unknown-grain":
        left.grain = None
    elif change == "raw-grain":
        left.grain = ["id"]
    elif change == "duplicate-fact":
        left.rows[1]["event"] = 10
    else:
        right.rows.append({"key": 1})
    with pytest.raises(LabError):
        join(node, left, right, context)


def test_unauthorized_source_not_empty():
    node, context = joining()
    with pytest.raises(LabError) as error:
        join(node, artifact([{"id": 1}]), artifact([], "right", status=Status.BLOCKED_AUTH), context)
    assert error.value.status == Status.BLOCKED_AUTH


@pytest.mark.parametrize("aggregation", ["semi_additive", "non_additive", "raw"])
def test_illegal_measure_recombination(aggregation):
    source = artifact([{"id": 1, "measure": 5}], aggregation={"measure": aggregation})
    node = AggregateNode(kind="AggregateResult", id="sum", source="left", group_by=[], sums=["measure"])
    with pytest.raises(LabError, match="Non-additive"):
        aggregate(node, source)


def test_fanout_taints_repeated_right_metrics():
    node, context = joining()
    source = join(
        node, artifact([{"id": 1, "line": 1}, {"id": 1, "line": 2}]),
        artifact([{"key": 1, "revenue": 100}], "right", {"revenue": "additive"}), context,
    )
    assert source.aggregation["revenue"] == "non_additive"


def test_projection_cannot_launder_repeated_measure_into_additive_total():
    node, context = joining()
    joined = join(
        node, artifact([{"id": 1, "line": 1}, {"id": 1, "line": 2}]),
        artifact([{"key": 1, "revenue": 100}], "right", {"revenue": "additive"}), context,
    )
    renamed = project(ProjectColumns(
        kind="ProjectColumns", id="renamed", source="joined", columns={"money": "revenue", "line": "line"},
    ), joined)
    assert renamed.aggregation["money"] == "non_additive"
    assert renamed.grain is None
    with pytest.raises(LabError, match="Non-additive"):
        aggregate(AggregateNode(
            kind="AggregateResult", id="total", source="renamed", group_by=[], sums=["money"],
        ), renamed)


def test_union_overlapping_intervals():
    source = artifact([
        {"id": 1, "start": "2026-06-01T00:00:00Z", "end": "2026-06-01T01:00:00Z"},
        {"id": 1, "start": "2026-06-01T00:30:00Z", "end": "2026-06-01T02:00:00Z"},
    ])
    node = RuleNode(
        kind="ApplyDeterministicRule", id="union", source="left",
        operation="union_minutes", output="downtime", arguments=["start", "end"], group_by=["id"],
    )
    assert rule(node, source).rows == [{"id": 1, "downtime": 120}]


def test_weighted_ratio_not_average_ratios():
    source = artifact([{"n": 9, "d": 10}, {"n": 1, "d": 100}], aggregation={"n": "additive", "d": "additive"})
    totals = aggregate(AggregateNode(
        kind="AggregateResult", id="sum", source="left", group_by=[], sums=["n", "d"]
    ), source)
    output = rule(RuleNode(
        kind="ApplyDeterministicRule", id="ratio", source="sum",
        operation="ratio", output="rate", arguments=["n", "d"],
    ), totals)
    assert output.rows[0]["rate"] == 10 / 110
    assert output.rows[0]["rate"] != (0.9 + 0.01) / 2


def test_missing_operand_is_not_zero():
    with pytest.raises(LabError, match="missing is not zero"):
        rule(RuleNode(
            kind="ApplyDeterministicRule", id="ratio", source="left",
            operation="ratio", output="rate", arguments=["n", "d"],
        ), artifact([{"n": None, "d": 10}]))


def test_changed_clock_blocks_join():
    node, context = joining()
    right = artifact([{"key": 1}], "right")
    right.clocks.data = "changed"
    with pytest.raises(LabError, match="clocks"):
        join(node, artifact([{"id": 1}]), right, context)


@pytest.mark.asyncio
async def test_30_simultaneous_misses_and_principal_isolation():
    async def authorize(_):
        return True
    cache = ScopedCache(authorize)
    key = CacheKey("dev", "one", "1", "model", "s1", "d1", "o1", "filter")
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return calls

    results = await asyncio.gather(*(cache.get(key, load) for _ in range(30)))
    assert results == [1] * 30
    assert await cache.get(replace(key, principal="two"), load) == 2
    assert await cache.get(replace(key, data_revision="d2"), load) == 3
    assert await cache.get(replace(key, schema_revision="s2"), load) == 4
    assert await cache.get(replace(key, ontology_revision="o2"), load) == 5


@pytest.mark.asyncio
async def test_revocation_during_inflight_never_repopulates():
    allowed = True
    started, finish = asyncio.Event(), asyncio.Event()

    async def authorize(_):
        return allowed

    async def load():
        started.set()
        await finish.wait()
        return "protected"

    cache = ScopedCache(authorize)
    key = CacheKey("dev", "one", "1", "model", "s", "d", "o", "f")
    task = asyncio.create_task(cache.get(key, load))
    await started.wait()
    allowed = False
    cache.invalidate("dev", "one")
    finish.set()
    with pytest.raises(LabError, match="Authorization changed"):
        await task
    assert cache.values == {}
    with pytest.raises(LabError, match="authorization denied"):
        await cache.get(key, load)


def population(model, tables=None):
    from telco_iq.catalog import digest
    from telco_iq.contracts import SourcePopulation

    return SourcePopulation(
        model=model, evidence_kind="LOCAL_FIXTURE", binding_hash=digest({"fixture": model}),
        profile="synthetic-unit", seed=1, generator_revision="unit-generator-1",
        identity_revision=digest("unit-identities"), manifest_hash=digest("unit-manifest"),
        table_hashes={name: digest(name) for name in (tables or [model])},
        shared_map_hashes={"account": digest("unit-account-map")},
    )


@pytest.mark.parametrize("field,value", [
    ("profile", "smoke"), ("seed", 2), ("generator_revision", "other-generator"),
    ("identity_revision", "a" * 64),
    ("shared_map_hashes", {"account": "b" * 64}),
])
def test_population_mismatch_rejects_reused_keys_and_equal_clocks(field, value):
    node, context = joining()
    left, right = artifact([{"id": 1}]), artifact([{"key": 1}], "right")
    left.source_populations = [population("left")]
    right.source_populations = [population("right").model_copy(update={field: value})]
    with pytest.raises(LabError, match="population"):
        join(node, left, right, context)


def test_compatible_population_preserves_independent_source_clocks():
    from telco_iq.contracts import SourceClock

    node, context = joining()
    left, right = artifact([{"id": 1}]), artifact([{"key": 1}], "right")
    for value, timestamp in ((left, "2026-07-01"), (right, "2026-07-02")):
        model = value.source_models[0]
        value.source_populations = [population(model)]
        value.clocks.data = timestamp
        value.clocks.model = f"model-{model}"
        value.source_clocks = [SourceClock(
            model=model, table=model, data=timestamp, model_revision=value.clocks.model,
        )]
    result = join(node, left, right, context)
    assert result.rows == [{"id": 1, "key": 1}]
    assert len(result.source_populations) == len(result.source_clocks) == 2


@pytest.mark.parametrize("empty", [False, True])
async def test_cached_missing_population_is_not_compatible_even_when_empty(empty):
    node, context = joining()
    left = artifact([] if empty else [{"id": 1}])
    right = artifact([] if empty else [{"key": 1}], "right")
    right.source_populations = []
    left.source_populations = [population("left")]

    async def authorize(_):
        return True

    async def load():
        return right

    cache = ScopedCache(authorize)
    key = CacheKey("dev", "one", "1", "right", "1", "1", "1", "1")
    await cache.get(key, load)
    cached = await cache.get(key, load)
    with pytest.raises(LabError, match="population"):
        join(node, left, cached, context)


@pytest.mark.parametrize("rows", [[], [{"id": 1}]])
def test_unknown_destination_population_blocks_before_empty_cohort_resolution(rows):
    from telco_iq.contracts import QueryNode
    from telco_iq.executor import resolve_cohort

    class SourceCatalog:
        tables = {"right": {"columns": {"key": "int64"}}}

    source = artifact(rows)
    source.source_populations = [population("left")]
    node = QueryNode(
        kind="QuerySemanticModel", id="target", model="right", table="right",
        columns=["key"], expected_grain=["key"],
        cohort_filters=[{"source": "left", "source_column": "id", "target_column": "key"}],
    )
    with pytest.raises(LabError, match="population"):
        resolve_cohort(node, {"left": source}, SourceCatalog(), 10)
