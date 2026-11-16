"""Synthetic pair-boundary leases preserve evidence clocks and require actual reconciliation."""

from datetime import datetime, timedelta, timezone

import pytest

from telco_iq.contracts import Budgets, Scope, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import (
    ObservedSourceState,
    ReaderBinding,
    ReaderSourceStateLease,
    SourceCallBudget,
    SourceIdentity,
)
from telco_iq.mcp_provider import _observed_state


def case():
    scope = Scope(tenant="synthetic", principal="synthetic-reader", authorization_revision="synthetic-epoch",
                  models=["finance", "subscriptions"], mode="LIVE_DELEGATED")
    binding = ReaderBinding(
        scope=scope, ontology_workspace_id="00000000-0000-0000-0000-000000000001",
        ontology_item_id="00000000-0000-0000-0000-000000000002", native_snapshot_hash="a" * 64,
        sources={name: SourceIdentity(workspace_id="00000000-0000-0000-0000-000000000001",
                                     dataset_id=f"00000000-0000-0000-0000-{index:012d}")
                 for index, name in enumerate(scope.models, start=3)},
    )
    observed = datetime.now(timezone.utc) - timedelta(seconds=1)
    states = {name: ObservedSourceState(data_revision="synthetic-data", model_revision="synthetic-model",
                                        evidence_hash="b" * 64, observed_at=observed) for name in binding.sources}
    lease = ReaderSourceStateLease(binding, lambda current: current == scope)
    return binding, states, lease


def test_isolated_planning_suspends_only_source_work_time_not_counts_or_evidence_clocks(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("telco_iq.live_contracts.perf_counter", lambda: clock[0])
    budget = SourceCallBudget(Budgets(timeout_seconds=5, max_queries=4, max_rows=10, max_bytes=100))
    budget.reserve()
    budget.returned(2)
    budget.response_bytes(10)
    clock[0] = 1
    with budget.suspend_for_planning():
        clock[0] = 10
        assert budget.remaining_seconds() == 4
        with pytest.raises(LabError, match="cannot run"):
            budget.reserve()
        with budget.suspend_for_planning():
            clock[0] = 20
            assert budget.remaining_seconds() == 4
        clock[0] = 30
    assert (budget.requests, budget.rows, budget.bytes) == (1, 2, 10)
    assert budget.remaining_seconds() == 4
    budget.reserve()
    clock[0] = 34
    with pytest.raises(LabError, match="time budget exhausted"):
        budget.check()


def test_planning_failure_restores_source_timer_without_resetting_any_allowance(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("telco_iq.live_contracts.perf_counter", lambda: clock[0])
    budget = SourceCallBudget(Budgets(timeout_seconds=5))
    with pytest.raises(RuntimeError, match="Synthetic"):
        with budget.suspend_for_planning():
            clock[0] = 100
            raise RuntimeError("Synthetic failed planning")
    budget.reserve()
    assert budget.requests == 1 and budget.remaining_seconds() == 5
    clock[0] = 106
    with pytest.raises(LabError):
        budget.reserve()


def test_phase_local_deadlines_do_not_reset_any_shared_request_row_or_byte_allowance(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("telco_iq.live_contracts.perf_counter", lambda: clock[0])
    budget = SourceCallBudget(
        Budgets(timeout_seconds=5, max_queries=5, max_rows=10, max_bytes=100), phase_local_timeouts=True,
    )
    budget.reserve()
    budget.returned(2)
    budget.response_bytes(10)
    for phase in ("preflight", "execution", "postflight"):
        clock[0] += 4
        budget.begin_phase(phase)
        assert budget.remaining_seconds() == 5
        assert (budget.requests, budget.rows, budget.bytes) == (1, 2, 10)
    budget.reserve()
    clock[0] += 6
    with pytest.raises(LabError, match="postflight phase") as error:
        budget.check()
    assert error.value.status == Status.TIMED_OUT


@pytest.mark.parametrize("phase", ["metadata", "execution", "postflight", "unknown"])
def test_source_timing_cannot_skip_repeat_or_invent_a_phase(phase):
    budget = SourceCallBudget(Budgets(), phase_local_timeouts=True)
    with pytest.raises(LabError, match="out of order"):
        budget.begin_phase(phase)
    budget.begin_phase("preflight")
    with pytest.raises(LabError, match="out of order"):
        budget.begin_phase("preflight")


def test_expired_phase_or_planning_cannot_renew_source_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("telco_iq.live_contracts.perf_counter", lambda: clock[0])
    budget = SourceCallBudget(Budgets(timeout_seconds=5), phase_local_timeouts=True)
    with budget.suspend_for_planning():
        with pytest.raises(LabError, match="out of order"):
            budget.begin_phase("preflight")
    clock[0] = 6
    with pytest.raises(LabError, match="metadata phase"):
        budget.begin_phase("preflight")
    ordinary = SourceCallBudget(Budgets())
    with pytest.raises(LabError, match="unconfigured"):
        ordinary.begin_phase("preflight")


async def test_only_explicit_typed_lease_reuses_original_observation_without_freshening_clock():
    binding, states, lease = case()
    lease.seed(states, datetime.now(timezone.utc) - timedelta(seconds=2))
    for _ in range(3):
        result = await _observed_state(lease, binding, "finance")
        assert result == states["finance"] and result is not lease.states["finance"]
    async def old_value(current, alias):
        return states[alias]
    with pytest.raises(LabError, match="fresh"):
        await _observed_state(old_value, binding, "finance")
    assert lease.states["finance"].observed_at == states["finance"].observed_at


async def test_explicit_plan_source_subset_cannot_query_or_reconcile_an_unplanned_source():
    binding, states, lease = case()
    lease.seed({"finance": states["finance"]}, datetime.now(timezone.utc) - timedelta(seconds=2),
               required_models={"finance"})
    assert await lease(binding, "finance") == states["finance"]
    with pytest.raises(LabError, match="another reader/source"):
        await lease(binding, "subscriptions")
    started = datetime.now(timezone.utc)
    after = {name: value.model_copy(update={"observed_at": datetime.now(timezone.utc)}, deep=True)
             for name, value in states.items()}
    with pytest.raises(LabError, match="complete post-pair"):
        lease.reconcile(after, started)
    lease.reconcile({"finance": after["finance"]}, started)
    assert lease.states["finance"].observed_at == states["finance"].observed_at


@pytest.mark.parametrize("required", [set(), {"unapproved"}, {"finance", "subscriptions"}])
def test_explicit_subset_requires_every_planned_source_and_never_broadens_scope(required):
    _, states, lease = case()
    with pytest.raises(LabError, match="complete pair preflight"):
        lease.seed({"finance": states["finance"]}, datetime.now(timezone.utc) - timedelta(seconds=2),
                   required_models=required)

@pytest.mark.parametrize("change", ["unseeded", "scope", "logout", "tampered", "expired"])
async def test_lease_cannot_broaden_reader_modify_evidence_or_extend_freshness(monkeypatch, change):
    binding, states, lease = case()
    if change != "unseeded":
        lease.seed(states, datetime.now(timezone.utc) - timedelta(seconds=2))
    current = binding
    if change == "scope":
        current = binding.model_copy(update={"scope": binding.scope.model_copy(update={"authorization_revision": "other"})})
    elif change == "logout":
        lease.session_active = lambda _: False
    elif change == "tampered":
        lease.states["finance"].data_revision = "changed"
    elif change == "expired":
        later = datetime.now(timezone.utc) + timedelta(minutes=6)
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return later if tz is not None else later.replace(tzinfo=None)
        monkeypatch.setattr("telco_iq.live_contracts.datetime", Later)
    with pytest.raises(LabError):
        await lease(current, "finance")


@pytest.mark.parametrize("change", ["missing", "future", "before-preflight", "stale"])
def test_seed_requires_complete_actual_recent_preflight(change):
    _, states, lease = case()
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    if change == "missing":
        states.pop("finance")
    elif change == "future":
        states["finance"].observed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    elif change == "before-preflight":
        started = datetime.now(timezone.utc)
    else:
        started -= timedelta(minutes=10)
        states["finance"].observed_at -= timedelta(minutes=6)
    with pytest.raises(LabError):
        lease.seed(states, started)


@pytest.mark.parametrize("change", ["data", "model", "missing", "not-fresh"])
def test_post_pair_unknown_or_changed_state_cannot_be_complete(change):
    _, states, lease = case()
    lease.seed(states, datetime.now(timezone.utc) - timedelta(seconds=2))
    started = datetime.now(timezone.utc)
    after = {name: value.model_copy(update={"observed_at": datetime.now(timezone.utc)}, deep=True)
             for name, value in states.items()}
    if change in {"data", "model"}:
        setattr(after["finance"], change + "_revision", "changed")
    elif change == "missing":
        after.pop("finance")
    else:
        after["finance"].observed_at = states["finance"].observed_at
    with pytest.raises(LabError) as error:
        lease.reconcile(after, started)
    assert error.value.status == Status.PARTIAL


def test_equal_revisions_reconcile_only_with_new_real_observation_clocks():
    _, states, lease = case()
    lease.seed(states, datetime.now(timezone.utc) - timedelta(seconds=2))
    started = datetime.now(timezone.utc)
    after = {name: value.model_copy(update={"observed_at": datetime.now(timezone.utc)}, deep=True)
             for name, value in states.items()}
    lease.reconcile(after, started)
    assert all(lease.states[name].observed_at == original.observed_at for name, original in states.items())
