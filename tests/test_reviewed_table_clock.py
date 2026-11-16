"""Table clocks are source-proof content commitments, never equalized model revision strings."""

from copy import deepcopy

import pytest
from test_mcp_provider import fresh_state, setup_provider

from telco_iq.catalog import digest
from telco_iq.contracts import Artifact, Clocks, SourceClock, SourcePopulation, Status
from telco_iq.errors import LabError
from telco_iq.executor import compatible_clocks


async def provider_with_population():
    provider, session, node, active = await setup_provider()
    record = SourcePopulation(
        model="finance", evidence_kind="REVIEWED_LIVE",
        binding_hash=digest(provider.binding.sources["finance"].model_dump(mode="json")),
        profile="SYNTHETIC_EXPLICIT_REVIEW", seed=1, generator_revision="SYNTHETIC",
        identity_revision="a" * 64, manifest_hash="b" * 64,
        table_hashes={"account_month": "c" * 64}, shared_map_hashes={"account": "d" * 64},
    )
    provider.source_populations = [record]
    provider.population_check = lambda: [record.model_copy(deep=True)]
    state = await fresh_state(provider.binding, "finance")
    provider.expected_revisions = {"finance": (state.data_revision, state.model_revision)}
    provider._configuration_hash = provider._configuration()
    return provider, session, node, active


async def test_actual_provider_binds_reviewed_content_without_overwriting_model_clocks():
    provider, _, node, _ = await provider_with_population()
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.source_clocks[0].table_data == "REVIEWED_TABLE_CONTENT:" + "c" * 64
    assert artifact.source_clocks[0].data == receipt.clocks.data == "observed-data"
    assert artifact.source_clocks[0].model_revision == receipt.clocks.model == "observed-model"
    assert "reviewed table content clock=REVIEWED_TABLE_CONTENT:" in receipt.completeness_evidence
    assert artifact.source_populations == provider.source_populations


async def test_legacy_provider_without_an_explicit_current_proof_does_not_invent_table_clocks():
    provider, _, node, _ = await setup_provider()
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.source_clocks[0].table_data is None
    assert "reviewed table content clock" not in receipt.completeness_evidence


@pytest.mark.parametrize("change", ["binding", "table", "shared", "current_revision", "guard", "duplicate"])
async def test_missing_or_conflicting_reviewed_table_binding_blocks_before_source_dispatch(change):
    provider, session, node, _ = await provider_with_population()
    record = provider.source_populations[0]
    if change == "binding":
        record.binding_hash = "f" * 64
    elif change == "table":
        record.table_hashes = {"another_table": "c" * 64}
    elif change == "shared":
        record.shared_map_hashes["account_month"] = "f" * 64
    elif change == "current_revision":
        provider.expected_revisions["finance"] = ("changed", "observed-model")
    elif change == "guard":
        provider.population_check = None
    else:
        provider.source_populations.append(record.model_copy(deep=True))
    provider._configuration_hash = provider._configuration()
    with pytest.raises(LabError):
        await provider.query(node, provider.binding.scope)
    assert not session.calls


async def test_equal_table_content_never_hides_an_actual_during_query_model_state_change():
    provider, _, node, _ = await provider_with_population()
    calls = 0
    async def changed(binding, alias):
        nonlocal calls
        calls += 1
        state = await fresh_state(binding, alias)
        return state if calls == 1 else state.model_copy(update={"data_revision": "changed-data"})
    provider.state_probe = changed
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.status == receipt.status == Status.PARTIAL
    assert any("source state changed" in warning for warning in artifact.warnings)
    assert artifact.source_clocks[0].table_data == "REVIEWED_TABLE_CONTENT:" + "c" * 64


def test_shared_content_and_independent_model_vectors_are_distinct_clocks():
    from telco_iq.contracts import Scope

    scope = Scope(
        tenant="SYNTHETIC", principal="SYNTHETIC", authorization_revision="SYNTHETIC",
        models=["subscriptions", "finance"], mode="OFFLINE_SYNTHETIC",
    )
    def value(model):
        return Artifact(
            id=model, rows=[], status=Status.COMPLETE, scope=scope,
            clocks=Clocks(data=f"data-{model}", model=f"model-{model}", ontology="same", context="same"),
            source_models=[model], source_tables=["account"],
            grain=["account_key"], aggregation={}, evidence_ids=["SYNTHETIC_CLOCK_TEST_ONLY"],
            source_clocks=[SourceClock(
                model=model, table="account", data=f"data-{model}", model_revision=f"model-{model}",
                table_data="REVIEWED_TABLE_CONTENT:" + "a" * 64,
            )],
        )
    left, right = value("subscriptions"), value("finance")
    before = deepcopy((left.model_dump(mode="json"), right.model_dump(mode="json")))
    assert len(compatible_clocks(left, right)) == 2
    assert (left.model_dump(mode="json"), right.model_dump(mode="json")) == before
    right.source_clocks[0].table_data = "REVIEWED_TABLE_CONTENT:" + "b" * 64
    with pytest.raises(LabError, match="Shared source clocks"):
        compatible_clocks(left, right)
    right.source_clocks[0].table_data = left.source_clocks[0].table_data
    right.source_clocks[0].model = "subscriptions"
    right.source_models = ["subscriptions"]
    with pytest.raises(LabError, match="Shared source clocks"):
        compatible_clocks(left, right)
