import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from telco_iq.api import create_app
from telco_iq.approval import Approval
from telco_iq.catalog import Catalog
from telco_iq.comparison import ComparisonService
from telco_iq.contracts import (
    Artifact,
    Budgets,
    Clocks,
    CohortExclusion,
    Context,
    ExecutionResult,
    Plan,
    Receipt,
    Scope,
    SourceClock,
    Status,
)
from telco_iq.errors import LabError
from telco_iq.events import ExecutionEvent
from telco_iq.live_contracts import ObservedSourceState
from telco_iq.model_provider import ModelCompletionError
from telco_iq.planner import PlanValidationError

ROOT = Path(__file__).resolve().parents[1]


def setup(monkeypatch, *, include_finance=True, native_failure=False):
    """Synthetic injected collaborators, never a claim of live source/provider execution."""
    scope = Scope(tenant="synthetic", principal="unit", authorization_revision="unit-epoch",
                  models=["subscriptions", "finance"], mode="LIVE_DELEGATED")
    context = Context(mode="NATIVE_SNAPSHOT", revision="unit-context", source="synthetic contract fixture",
                      scope=scope, relationships=[], rules=[], provenance=["synthetic-unit-only"])
    calls = []

    class Bundle:
        def verify(self, current):
            assert current == scope

    bundle = Bundle()
    bundle.context = context
    approval = Approval(
        target_alias="APPROVED_LAB_ONLY", target_digest="unit-target", artifact_digest="unit-artifact",
        operations=["paid-plan-proposal"], estimated_cost=0, cap=1, currency="USD",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1), ttl_hours=1,
        teardown_owner="unit", teardown_mechanism="unit", confirmation_receipt="synthetic-unit-only",
    )

    class PlannerFixture:
        @staticmethod
        @contextmanager
        def request_guard(check):
            if not check():
                raise LabError("Synthetic reader guard rejected", Status.BLOCKED_AUTH)
            yield

        provider = SimpleNamespace(provider="SYNTHETIC_UNIT_PROVIDER", model_version="unit-model",
                                   request_guard=request_guard.__func__)
        target_digest = "unit-target"
        artifact_digest = "unit-artifact"

        async def propose(self, question, received_scope, received_context, catalog, **kwargs):
            arm = kwargs["input_arm"]
            calls.append((arm, question, kwargs["as_of"], kwargs["budgets"]))
            if native_failure and arm == "native":
                raise LabError("Synthetic refusal; no model service was called")
            model = "subscriptions" if arm == "baseline" else "finance"
            table, metric = (
                ("subscription_month", "subscriptions.active_lines_eom") if model == "subscriptions"
                else ("account_month", "finance.recognized_service_revenue")
            )
            plan = Plan(
                request_id=kwargs["run_id"], question=question, principal_scope=scope,
                as_of=kwargs["as_of"], ontology_revision=context.revision,
                metric_selections=[metric], budgets=kwargs["budgets"],
                nodes=[{"id": "query", "kind": "QuerySemanticModel", "model": model, "table": table,
                        "metrics": [metric], "date_role": "month", "group_by": ["account_key"],
                        "expected_grain": ["account_key"]}], result_node="query",
            )
            return plan, {"facts_hash": "same-unit-facts", "metric_bindings": [{"id": "synthetic"}]}

    planner = PlannerFixture()
    planner.approval = approval

    class ExecutorFixture:
        async def execute(self, plan, current_scope, *, on_event):
            model = plan.nodes[0].model
            clocks = Clocks(data="d1", model="m1", ontology="o1", context=context.revision)
            receipt = Receipt(id="unit-" + model, node_id="query", lane="LIVE_DELEGATED", tool="unit",
                              model=model, query="unit only", elapsed_ms=1, returned_rows=1,
                              status=Status.COMPLETE, completeness_evidence="synthetic unit-only",
                              clocks=clocks)
            await on_event(ExecutionEvent(run_id=plan.request_id, node_id="query", operation="Answer",
                                         phase="completed", lane="LIVE_DELEGATED", status=Status.COMPLETE))
            return ExecutionResult(
                request_id=plan.request_id, status=Status.COMPLETE, mode="LIVE_DELEGATED", receipts=[receipt],
                elapsed_ms=1, answer=Artifact(
                    id="answer", rows=[{"account_key": 1}], status=Status.COMPLETE, scope=scope, clocks=clocks,
                    source_models=[model], source_tables=[plan.nodes[0].table], grain=["account_key"],
                    aggregation={}, evidence_ids=[receipt.id],
                    source_clocks=[SourceClock(model=model, table=plan.nodes[0].table, data="d1", model_revision="m1")],
                ),
            )

    def executor_factory(arm):
        instance = ExecutorFixture()
        instance.context = context
        return instance

    async def state_probe():
        return {model: ObservedSourceState(data_revision="d1", model_revision="m1", evidence_hash="a" * 64,
                                           observed_at=datetime.now(timezone.utc))
                for model in (scope.models if include_finance else ["subscriptions"])}

    monkeypatch.setattr("telco_iq.comparison.build_planner_payload", lambda *args, **kwargs: {"facts_hash": "same-unit-facts"})
    service = ComparisonService(
        planner, Catalog(ROOT / "data/contracts/catalog.json"), scope, context, bundle, executor_factory,
        budgets=Budgets(), session_active=lambda: True, source_state_probe=state_probe,
    )
    return service, calls


async def test_comparison_uses_two_generated_plans_same_facts_and_actual_events(monkeypatch):
    service, calls = setup(monkeypatch)
    events = []

    async def collect(event):
        events.append(event)

    result = await service.run("Synthetic paired-controller test", on_event=collect)
    assert [call[0] for call in calls] == ["baseline", "native"]
    assert calls[0][1:] == calls[1][1:]
    assert result.arms[0].plan.request_id != result.arms[1].plan.request_id
    assert result.comparability == "COMMON_SOURCE_STATE"
    assert len([event for event in events if event.phase == "execution"]) == 2
    assert all(arm.planning_receipt["metric_bindings"] for arm in result.arms)
    assert result.arm_execution_mode == "sequential"


@pytest.mark.parametrize("mode", ["parallel", "sequential"])
async def test_plan_selected_boundaries_follow_both_plans_and_cover_the_exact_actual_source_union(monkeypatch, mode):
    service, calls = setup(monkeypatch)
    service.arm_execution_mode = mode
    phases, selected = [], []

    async def preflight(models):
        assert len(calls) == 2
        assert not any(event.phase == "execution" for event in phases)
        selected.append(set(models))
        return await service.source_state_probe()

    async def finalize():
        assert selected == [{"subscriptions", "finance"}]
        assert not any(event.phase == "arm_finished" for event in phases)
        return await service.source_state_probe()

    async def collect(event):
        phases.append(event)

    service.planned_source_state_probe = preflight
    service.source_state_finalize = finalize
    result = await service.run("Synthetic plan-selected boundary check", on_event=collect)
    assert result.comparability == "COMMON_SOURCE_STATE"
    assert all(arm.status == Status.COMPLETE and arm.execution.answer is not None for arm in result.arms)
    assert len([event for event in phases if event.phase == "planning_started"]) == 2
    assert len([event for event in phases if event.phase == "arm_finished"]) == 2


async def test_initial_pair_fact_binding_uses_the_same_applied_source_instructions_as_the_planner(monkeypatch):
    service, _ = setup(monkeypatch)
    instructions = {"subscriptions": "Synthetic applied source guidance.",
                    "finance": "Synthetic source-owned Finance guidance."}
    service.planner.source_instructions = instructions
    observed = []

    def payload(*args, **kwargs):
        observed.append(kwargs.get("source_instructions"))
        return {"facts_hash": "same-unit-facts"}

    monkeypatch.setattr("telco_iq.comparison.build_planner_payload", payload)
    result = await service.run("Synthetic source-AI parity regression")
    assert observed == [instructions, instructions]
    assert result.comparability == "COMMON_SOURCE_STATE"


async def test_qualified_partial_views_can_share_source_state_without_becoming_complete_totals(monkeypatch):
    service, _ = setup(monkeypatch)
    original_factory = service.executor_factory

    def factory(arm):
        instance = original_factory(arm)
        original = instance.execute

        async def execute(*args, **kwargs):
            result = await original(*args, **kwargs)
            result.status = Status.PARTIAL
            result.answer = result.answer.model_copy(update={
                "status": Status.PARTIAL, "population_qualification": "known_mapped_subset",
                "exclusions": [CohortExclusion(
                    source_node="synthetic-cohort", source_column="account_key",
                    unknown_mapping_observations=2, null_key_rows=1, evidence_ids=["synthetic-exclusion"],
                )],
            })
            return result

        instance.execute = execute
        return instance

    service.executor_factory = factory
    result = await service.run("Synthetic qualified known-mapped source-state comparison")
    assert result.comparability == "COMMON_SOURCE_STATE"
    assert all(arm.status == Status.PARTIAL and arm.execution.answer.status == Status.PARTIAL for arm in result.arms)
    assert any("not a complete population total" in warning for warning in result.limitations)


async def test_plan_source_selection_excludes_failed_arm_and_never_drops_a_valid_plan_source(monkeypatch):
    service, _ = setup(monkeypatch, native_failure=True)
    selected = []

    async def preflight(models):
        selected.append(set(models))
        states = await service.source_state_probe()
        return {model: states[model] for model in models}

    async def finalize():
        return await service.source_state_probe()

    service.planned_source_state_probe = preflight
    service.source_state_finalize = finalize
    result = await service.run("Synthetic one valid source")
    assert selected == [{"subscriptions"}]
    assert set(result.expected_source_states) == {"subscriptions"}
    assert result.arms[0].status == Status.COMPLETE
    assert result.arms[1].execution is None and result.comparability == "NOT_COMPARABLE"


@pytest.mark.parametrize("clarification", [False, True])
async def test_failed_or_clarification_only_proposals_do_not_spend_data_boundary_requests(monkeypatch, clarification):
    service, _ = setup(monkeypatch)
    original = service.planner.propose

    async def reject(*args, **kwargs):
        if clarification:
            plan, receipt = await original(*args, **kwargs)
            plan.clarification_state = "required"
            return plan, receipt
        raise ModelCompletionError("HTTP_ERROR", "Synthetic model429; no source query")

    async def forbidden(*args):
        pytest.fail("Rejected metadata-only proposals cannot trigger business-data pre/postflight")

    service.planner.propose = reject
    service.planned_source_state_probe = forbidden
    service.source_state_finalize = forbidden
    events = []

    async def collect(event):
        events.append(event)

    result = await service.run("Synthetic rejected plans", on_event=collect)
    assert result.expected_source_states == {}
    expected = Status.AMBIGUOUS if clarification else Status.FAILED
    assert all(arm.status == expected and arm.execution is None for arm in result.arms)
    assert len([event for event in events if event.phase == "arm_finished"]) == 2
    assert result.comparability == "NOT_COMPARABLE"


async def test_missing_plan_selected_state_stops_before_any_business_execution(monkeypatch):
    service, _ = setup(monkeypatch)

    async def missing(models):
        assert models == {"subscriptions", "finance"}
        states = await service.source_state_probe()
        return {"subscriptions": states["subscriptions"]}

    service.planned_source_state_probe = missing
    service.source_state_finalize = service.source_state_probe
    events = []

    async def collect(event):
        events.append(event)

    with pytest.raises(LabError, match="omits a validated plan source"):
        await service.run("Synthetic missing required source", on_event=collect)
    assert not any(event.phase in {"execution", "arm_finished"} for event in events)


async def test_parallel_arms_overlap_keep_task_local_receipts_and_withhold_answers_until_postflight(monkeypatch):
    service, calls = setup(monkeypatch)
    service.arm_execution_mode = "parallel"
    original = service.planner.propose
    started, release = set(), asyncio.Event()
    current_usage = ContextVar("synthetic_parallel_receipt", default=None)
    previous = service.planner.provider

    class Provider:
        provider = previous.provider
        model_version = previous.model_version
        request_guard = staticmethod(previous.request_guard)

        @property
        def last_receipt(self):
            return current_usage.get()

    service.planner.provider = Provider()

    async def propose(*args, **kwargs):
        value = await original(*args, **kwargs)
        arm = kwargs["input_arm"]
        current_usage.set({"synthetic_arm": arm})
        started.add(arm)
        if len(started) == 2:
            release.set()
        await release.wait()
        if arm == "baseline":
            await asyncio.sleep(0)
        return value

    service.planner.propose = propose
    events = []

    async def collect(event):
        events.append(event)

    async def finalize():
        assert started == {"baseline", "native"}
        assert not any(event.phase == "arm_finished" for event in events)
        assert not any(event.event and event.event.operation == "Answer" for event in events)
        return await service.source_state_probe()

    service.source_state_finalize = finalize
    async with asyncio.timeout(2):
        result = await service.run("Synthetic concurrent comparison", on_event=collect)
    assert [outcome.arm for outcome in result.arms] == ["baseline", "native"]
    assert result.arm_execution_mode == "parallel" and result.comparability == "COMMON_SOURCE_STATE"
    assert len(calls) == 2 and calls[0][1:] == calls[1][1:]
    assert [outcome.planning_receipt["model_usage"]["synthetic_arm"] for outcome in result.arms] == [
        "baseline", "native",
    ]
    assert current_usage.get() is None
    assert len([event for event in events if event.phase == "arm_finished"]) == 2


async def test_parallel_cancellation_joins_both_arms_without_publishing_or_finishing_postflight(monkeypatch):
    service, _ = setup(monkeypatch)
    service.arm_execution_mode = "parallel"
    started, ended, ready = set(), set(), asyncio.Event()
    events = []

    async def propose(*args, **kwargs):
        arm = kwargs["input_arm"]
        started.add(arm)
        if len(started) == 2:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.add(arm)

    async def collect(event):
        events.append(event)

    async def finalize():
        pytest.fail("Cancelled arms cannot start postflight or publish answers")

    service.planner.propose = propose
    service.source_state_finalize = finalize
    task = asyncio.create_task(service.run("Synthetic cancellation", on_event=collect))
    await asyncio.wait_for(ready.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended == {"baseline", "native"}
    assert not any(event.phase in {"plan_ready", "arm_finished"} for event in events)


async def test_rejected_model_candidates_are_diagnostics_never_executable_plans_or_answers(monkeypatch):
    service, _ = setup(monkeypatch)
    original = service.planner.propose
    async def reject(*args, **kwargs):
        candidate, receipt = await original(*args, **kwargs)
        raise PlanValidationError(LabError("Missing dependency: synthetic missing node"), candidate, receipt)
    service.planner.propose = reject
    result = await service.run("Synthetic rejected-plan retention")
    assert result.comparability == "NOT_COMPARABLE"
    for arm in result.arms:
        assert arm.plan is None and arm.execution is None and arm.status == Status.FAILED
        assert arm.planning_receipt["validation_status"] == "REJECTED_NOT_EXECUTED"
        assert arm.planning_receipt["rejected_candidate"]["nodes"]
        assert arm.planning_receipt["validation_error"].startswith("Missing dependency")


async def test_invalid_model_output_retains_safe_categories_never_a_candidate_or_executable_answer(monkeypatch):
    service, _ = setup(monkeypatch)
    details = [{"kind": "typed_plan", "path": ["nodes", 0, "QuerySemanticModel"], "code": "value_error"}]

    async def reject(*args, **kwargs):
        raise ModelCompletionError("OUTPUT_INVALID", "Synthetic invalid output", validation_details=details)

    service.planner.propose = reject
    result = await service.run("Synthetic shape diagnostics")
    for arm in result.arms:
        assert arm.plan is None and arm.execution is None
        assert arm.planning_receipt["validation_status"] == "INVALID_MODEL_OUTPUT_NOT_EXECUTED"
        assert arm.planning_receipt["output_validation"] == details
        assert "rejected_candidate" not in arm.planning_receipt


@pytest.mark.parametrize("failure", [None, Status.PARTIAL, Status.TIMED_OUT, Status.BLOCKED_AUTH])
async def test_boundary_mode_withholds_complete_arm_answers_until_actual_final_verification(monkeypatch, failure):
    service, _ = setup(monkeypatch)
    original_factory = service.executor_factory

    def with_separate_view(arm):
        instance = original_factory(arm)
        original_execute = instance.execute

        async def execute(*args, **kwargs):
            execution = await original_execute(*args, **kwargs)
            execution.additional_answers = {"Separate source-grain view": execution.answer}
            return execution

        instance.execute = execute
        return instance

    service.executor_factory = with_separate_view
    events, reconciled = [], False
    async def finalize():
        nonlocal reconciled
        assert not any(event.phase == "arm_finished" for event in events)
        assert not any(event.phase == "execution" and event.event.operation == "Answer"
                       and event.event.phase == "completed" for event in events)
        if failure is not None:
            raise LabError("Synthetic actual post-pair verification failed", failure)
        reconciled = True
        return await service.source_state_probe()
    async def collect(event):
        if event.phase == "arm_finished":
            assert reconciled or failure is not None
        events.append(event)
    service.source_state_finalize = finalize
    result = await service.run("Synthetic boundary check", on_event=collect)
    assert len([event for event in events if event.phase == "arm_finished"]) == 2
    if failure is None:
        assert result.comparability == "COMMON_SOURCE_STATE"
        assert all(arm.execution.answer is not None for arm in result.arms)
        assert all(arm.execution.additional_answers for arm in result.arms)
        assert all(set(arm.planning_receipt["post_pair_source_states"]) == set(service.scope.models)
                   for arm in result.arms)
        assert len([event for event in events if event.phase == "execution"]) == 2
    else:
        assert result.comparability == "NOT_COMPARABLE"
        assert all(arm.status == failure and arm.errors for arm in result.arms)
        assert all(arm.execution is None or arm.execution.answer is None for arm in result.arms)
        assert all(arm.execution is None or not arm.execution.additional_answers for arm in result.arms)
        assert not any(event.phase == "execution" for event in events)
        if failure == Status.BLOCKED_AUTH:
            assert all(arm.plan is None and arm.planning_receipt is None for arm in result.arms)


async def test_disjoint_queried_source_sets_do_not_make_overlap_comparison_vacuously_true(monkeypatch):
    service, _ = setup(monkeypatch, include_finance=False)
    result = await service.run("Synthetic disjoint-source test")
    assert result.comparability == "NOT_COMPARABLE"
    assert any("finance" in reason for reason in result.comparability_reasons)


async def test_one_refusal_is_preserved_without_fake_answer_or_retry(monkeypatch):
    service, calls = setup(monkeypatch, native_failure=True)
    result = await service.run("Synthetic refusal test")
    assert len(calls) == 2
    assert result.arms[1].execution is None and result.arms[1].errors
    assert result.comparability == "NOT_COMPARABLE"


async def test_ended_session_or_missing_provider_blocks_before_planning(monkeypatch):
    service, calls = setup(monkeypatch)
    service.session_active = lambda: False
    with pytest.raises(LabError, match="session ended"):
        await service.run("Not sent")
    assert not calls
    service.session_active = lambda: True
    service.planner.provider = None
    with pytest.raises(LabError, match="real configured"):
        await service.run("Not sent")
    assert not calls


async def test_source_preflight_can_take_longer_than_five_seconds_without_false_noncomparability(monkeypatch):
    service, calls = setup(monkeypatch)
    base = datetime.now(timezone.utc)
    times = iter([base, base + timedelta(seconds=1), base + timedelta(seconds=12)])
    monkeypatch.setattr("telco_iq.comparison.datetime", SimpleNamespace(now=lambda tz: next(times)))

    async def delayed_state():
        return {
            model: ObservedSourceState(
                data_revision="d1", model_revision="m1", evidence_hash="a" * 64,
                observed_at=base + timedelta(seconds=10),
            )
            for model in service.scope.models
        }

    service.source_state_probe = delayed_state
    result = await service.run("Synthetic slow-preflight test")
    assert len(calls) == 2 and result.comparability == "COMMON_SOURCE_STATE"


async def test_stale_preflight_observation_blocks_before_spending(monkeypatch):
    service, calls = setup(monkeypatch)

    async def stale_state():
        return {"finance": ObservedSourceState(
            data_revision="d1", model_revision="m1", evidence_hash="a" * 64,
            observed_at=datetime.now(timezone.utc) - timedelta(seconds=20),
        )}

    service.source_state_probe = stale_state
    with pytest.raises(LabError, match="during this pair preflight"):
        await service.run("Not sent to provider")
    assert not calls


def test_unconfigured_api_explains_blockers_without_fixture_fallback():
    client = TestClient(create_app(ROOT), base_url="http://localhost:8765")
    readiness = client.get("/api/comparison/readiness").json()
    assert readiness["ready"] is False and readiness["blockers"]
    response = client.post("/api/compare/stream", json={"question": "Do not route to a fixture"},
                           headers={"X-Telco-Local": "submitted-plan"})
    assert response.status_code == 409
    assert response.json()["status"] == "BLOCKED_CAPABILITY"


@pytest.mark.parametrize("signed_in", [False, True])
def test_health_projects_configured_readiness_without_tokens_probes_or_private_identity(signed_in):
    from telco_iq.auth import AuthError

    session = SimpleNamespace(csrf="unit-csrf", epoch="unit-session-epoch")

    class LocalAuth:
        def session(self, key):
            if not signed_in:
                raise AuthError("Sign in with an approved DEV identity")
            return session

        def token(self, *args, **kwargs):
            pytest.fail("Health/readiness must not acquire a resource token")

    class LocalGateway:
        def readiness(self):
            return {"ready": True, "status": "READY_FOR_CURRENT_READER_PROBE", "blockers": [],
                    "private_extra": "private-config-id", "claim": "private@example.invalid"}

        def prepare(self, *args):
            pytest.fail("Health/readiness must not probe or execute")

    client = TestClient(create_app(ROOT, reader_auth=LocalAuth(), live_demo=LocalGateway()),
                        base_url="http://localhost:8765")
    health = client.get("/api/health").json()
    readiness = client.get("/api/comparison/readiness").json()
    assert health["mode"] == "LIVE_CONFIGURED"
    assert health["scope"] is None
    assert health["context_revision"] == "CURRENT_READER_PROBE_REQUIRED"
    assert health["readiness"] == readiness
    assert readiness["configuration"] == "CONFIGURED"
    assert readiness["ready"] is signed_in
    assert readiness["native_source_probe"] == "UNKNOWN_NOT_PROBED"
    assert readiness["application_execution"] == "NOT_ESTABLISHED"
    assert readiness["demonstration"] == "NOT_ESTABLISHED"
    assert "private-config-id" not in str(health)
    assert "private@example.invalid" not in str(readiness)
    assert client.get("/api/capabilities").json()["mode"] == health["mode"]
    assert client.get("/api/context").status_code == (409 if signed_in else 401)


@pytest.mark.parametrize("blocker", [
    "Reader credential window expired; renew it explicitly.",
    "Private evidence approval expired or clock has no timezone",
    "Live source requests require explicitly approved bounded private evidence retention.",
    "Approval expired or has no timezone",
    "Every enrolled source requires actual reviewed query and schema profiles.",
])
def test_configured_readiness_preserves_real_local_blockers_and_never_becomes_ready(blocker):
    class LocalAuth:
        def session(self, key):
            return SimpleNamespace(csrf="unit-csrf", epoch="unit-epoch")

    class LocalGateway:
        def readiness(self):
            return {"ready": False, "status": "BLOCKED_CAPABILITY", "blockers": [blocker]}

    client = TestClient(create_app(ROOT, reader_auth=LocalAuth(), live_demo=LocalGateway()),
                        base_url="http://localhost:8765")
    health = client.get("/api/health").json()
    assert health["mode"] == "LIVE_CONFIGURED"
    assert health["readiness"]["ready"] is False
    assert health["readiness"]["configuration"] == "BLOCKED"
    assert blocker in health["readiness"]["blockers"]


def test_anonymous_readiness_redacts_unknown_operator_details_and_expired_session():
    from telco_iq.auth import AuthError

    class LocalAuth:
        def session(self, key):
            raise AuthError("The approved reader session window has expired")

    class LocalGateway:
        def readiness(self):
            return {"ready": False, "status": "private-status-id",
                    "blockers": ["Private path C:\\private\\selected-user@example.invalid.json"]}

    client = TestClient(create_app(ROOT, reader_auth=LocalAuth(), live_demo=LocalGateway()),
                        base_url="http://localhost:8765")
    health = client.get("/api/health").json()
    assert health["authenticated"] is False and health["csrf_token"] is None
    assert health["readiness"]["reader_session"] == "BLOCKED_AUTH"
    assert "The approved reader session window has expired" in health["readiness"]["blockers"]
    assert "selected-user@" not in str(health) and "private-status-id" not in str(health)


def test_configured_pair_route_streams_both_real_controller_arms_with_injected_unit_collaborators(
    monkeypatch, tmp_path,
):
    service, calls = setup(monkeypatch)
    session = SimpleNamespace(object_id="synthetic-unit-reader")

    class AuthFixture:
        config = SimpleNamespace(receipt_directory=tmp_path)

        def session(self, key):
            return session

        def csrf(self, current, supplied):
            assert current is session and supplied == "unit-csrf"

        def active(self, current):
            return current is session

    class GatewayFixture:
        def readiness(self):
            return {"ready": True, "status": "UNIT_ONLY", "blockers": []}

        @asynccontextmanager
        async def prepare(self, current):
            assert current is session
            yield service

    client = TestClient(create_app(ROOT, reader_auth=AuthFixture(), live_demo=GatewayFixture()),
                        base_url="http://localhost:8765")
    response = client.post("/api/compare/stream", json={"question": "Explicit synthetic gateway test"},
                           headers={"X-CSRF-Token": "unit-csrf"})
    assert response.status_code == 200
    frames = []
    for frame in response.text.strip().split("\n\n"):
        lines = frame.splitlines()
        frames.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    assert frames[-1][0] == "comparison"
    assert len(frames[-1][1]["arms"]) == 2
    outcomes = [value["outcome"] for name, value in frames if name == "pair" and value["outcome"] is not None]
    assert len(outcomes) == 2
    for streamed, final in zip(outcomes, frames[-1][1]["arms"], strict=True):
        assert streamed["arm"] == final["arm"]
        assert streamed["execution"] == final["execution"]
        receipts = streamed["execution"]["receipts"]
        assert len(receipts) == 1
        assert receipts[0]["parameters"] is None
        assert Receipt.model_validate(receipts[0]).parameters is None
        assert streamed["execution"]["answer"]["rows"] == [{"account_key": 1}]
    assert [call[0] for call in calls] == ["baseline", "native"]
    saved = list((tmp_path / session.object_id).glob("*.json"))
    assert len(saved) == 1
    saved_result = json.loads(saved[0].read_text(encoding="utf-8"))["result"]
    assert saved_result["pair_id"] == frames[-1][1]["pair_id"]
    for persisted, final in zip(saved_result["arms"], frames[-1][1]["arms"], strict=True):
        assert persisted["execution"]["receipts"] == final["execution"]["receipts"]
