"""Two actual planning/execution arms over the same authorized facts."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from telco_iq.catalog import Catalog
from telco_iq.contracts import Budgets, Context, Contract, ExecutionResult, Plan, QueryNode, Scope, Status
from telco_iq.errors import LabError
from telco_iq.events import ExecutionEvent
from telco_iq.executor import Executor, known_mapped_subset
from telco_iq.live_contracts import ObservedSourceState
from telco_iq.model_provider import ModelCompletionError
from telco_iq.planner import Planner, PlanValidationError, build_planner_payload

logger = logging.getLogger(__name__)
Arm = Literal["baseline", "native"]
ArmExecutionMode = Literal["sequential", "parallel"]


class PlanningBundle(Protocol):
    context: Context

    def verify(self, scope: Scope) -> None: ...


class ComparisonRequest(Contract):
    question: str = Field(min_length=1, max_length=8000)


class ArmResult(Contract):
    arm: Arm
    status: Status
    plan: Plan | None = None
    planning_receipt: dict | None = None
    execution: ExecutionResult | None = None
    errors: list[str] = Field(default_factory=list)
    elapsed_ms: float = Field(ge=0)


@dataclass(frozen=True)
class PreparedArm:
    arm: Arm
    plan: Plan
    receipt: dict
    started: float


class PairEvent(Contract):
    pair_id: str
    arm: Arm
    phase: Literal["planning_started", "plan_ready", "execution", "arm_finished"]
    event: ExecutionEvent | None = None
    plan: Plan | None = None
    outcome: ArmResult | None = None


PairSink = Callable[[PairEvent], Awaitable[None]]


class ComparisonResult(Contract):
    pair_id: str
    question: str
    mode: str
    source_facts_hash: str
    context_revision: str
    provider: str
    model_version: str
    expected_source_states: dict[str, ObservedSourceState]
    arms: list[ArmResult]
    comparability: Literal["NOT_COMPARABLE", "COMMON_SOURCE_STATE"]
    comparability_reasons: list[str]
    elapsed_ms: float = Field(ge=0)
    limitations: list[str]
    arm_execution_mode: ArmExecutionMode = "sequential"


class ComparisonService:
    def __init__(
        self, planner: Planner, catalog: Catalog, scope: Scope, context: Context,
        bundle: PlanningBundle, executor_factory: Callable[[Arm], Executor], *,
        budgets: Budgets,
        session_active: Callable[[], bool],
        source_state_probe: Callable[[], Awaitable[dict[str, ObservedSourceState]]] | None = None,
        source_state_finalize: Callable[[], Awaitable[dict[str, ObservedSourceState] | None]] | None = None,
        arm_execution_mode: ArmExecutionMode = "sequential",
        planned_source_state_probe: Callable[[set[str]], Awaitable[dict[str, ObservedSourceState]]] | None = None,
    ):
        self.planner, self.catalog, self.scope, self.context = planner, catalog, scope, context
        self.bundle, self.executor_factory, self.budgets = bundle, executor_factory, budgets
        self.session_active, self.source_state_probe = session_active, source_state_probe
        self.source_state_finalize = source_state_finalize
        self.planned_source_state_probe = planned_source_state_probe
        if arm_execution_mode not in ("sequential", "parallel"):
            raise ValueError("Unknown comparison arm execution mode")
        self.arm_execution_mode = arm_execution_mode
        if source_state_finalize is not None and source_state_probe is None:
            raise ValueError("Post-pair verification requires actual source preflight")
        if planned_source_state_probe is not None and source_state_finalize is None:
            raise ValueError("Plan-selected preflight requires actual post-pair source verification")

    def active(self) -> None:
        if not self.session_active():
            raise LabError("Reader session ended; no further planning or source calls", Status.BLOCKED_AUTH)
        self.bundle.verify(self.scope)
        if self.bundle.context != self.context:
            raise LabError("Native context changed before comparison", Status.BLOCKED_CAPABILITY)

    async def run(self, question: str, *, on_event: PairSink | None = None) -> ComparisonResult:
        request = ComparisonRequest(question=question)
        question = request.question.strip()
        if not question:
            raise LabError("A business question is required")
        start = perf_counter()
        self.active()
        provider = self.planner.provider
        if provider is None or self.planner.approval is None:
            raise LabError("A real configured and approved application model is required", Status.BLOCKED_CAPABILITY)
        request_guard = getattr(provider, "request_guard", None)
        if self.scope.mode == "LIVE_DELEGATED" and not callable(request_guard):
            raise LabError("Live planning requires a per-invocation reader guard", Status.BLOCKED_CAPABILITY)
        self.planner.approval.authorize(
            self.planner.artifact_digest, self.planner.target_digest, "paid-plan-proposal",
        )
        pair_id = str(uuid4())
        as_of = datetime.now(timezone.utc)
        payloads = [
            build_planner_payload(
                question, self.scope, self.context, self.catalog,
                input_arm=arm, native_snapshot=self.bundle,
                run_id=f"{pair_id}-{arm}", as_of=as_of, budgets=self.budgets,
                column_reference_mode=getattr(self.planner, "column_reference_mode", "exact"),
                source_instructions=getattr(self.planner, "source_instructions", None),
            )
            for arm in ("baseline", "native")
        ]
        if payloads[0]["facts_hash"] != payloads[1]["facts_hash"]:
            raise LabError("Comparison input facts differ; no model calls permitted")
        expected = {}

        async def preflight(models: set[str] | None = None):
            nonlocal expected
            preflight_started = datetime.now(timezone.utc)
            if models is not None:
                if not models or not models <= set(self.scope.models) or self.planned_source_state_probe is None:
                    raise LabError("Plan-selected sources exceed the authorized source scope", Status.BLOCKED_AUTH)
                expected = await self.planned_source_state_probe(models)
                if set(expected) != models:
                    raise LabError("Actual source preflight omits a validated plan source", Status.BLOCKED_CAPABILITY)
            else:
                expected = await self.source_state_probe() if self.source_state_probe else {}
            preflight_completed = datetime.now(timezone.utc)
            self.active()
            if not set(expected) <= set(self.scope.models):
                raise LabError("Source-state evidence exceeds the authorized model scope")
            if any(not preflight_started <= state.observed_at <= preflight_completed for state in expected.values()):
                raise LabError("Source-state evidence was not acquired during this pair preflight",
                               Status.BLOCKED_CAPABILITY)

        if self.planned_source_state_probe is None:
            await preflight()

        async def emit(event: PairEvent):
            if on_event is not None:
                await on_event(event)

        deferred_answers: list[PairEvent] = []

        async def run_arm(
            arm: Arm, prepared: PreparedArm | None = None, *, planning_only=False,
        ) -> ArmResult | PreparedArm:
            arm_start = prepared.started if prepared is not None else perf_counter()
            plan = prepared.plan if prepared is not None else None
            receipt = prepared.receipt if prepared is not None else None
            execution = None
            previous_usage = getattr(provider, "last_receipt", None)

            def usage_receipt():
                current = getattr(provider, "last_receipt", None)
                if current is None or current == previous_usage:
                    return None
                if isinstance(current, BaseModel):
                    return current.model_dump(mode="json")
                return current if isinstance(current, dict) else None

            try:
                self.active()
                if prepared is None:
                    await emit(PairEvent(pair_id=pair_id, arm=arm, phase="planning_started"))
                    with request_guard(self.session_active) if callable(request_guard) else nullcontext():
                        plan, receipt = await self.planner.propose(
                            question, self.scope, self.context, self.catalog, input_arm=arm,
                            native_snapshot=self.bundle, run_id=f"{pair_id}-{arm}", as_of=as_of, budgets=self.budgets,
                        )
                    model_usage = usage_receipt()
                    if model_usage is not None:
                        receipt = {**receipt, "model_usage": model_usage}
                    self.active()
                    if receipt["facts_hash"] != payloads[0]["facts_hash"]:
                        raise LabError("Source/business facts changed during planning")
                    await emit(PairEvent(pair_id=pair_id, arm=arm, phase="plan_ready", plan=plan))
                if plan.clarification_state == "required":
                    outcome = ArmResult(
                        arm=arm, status=Status.AMBIGUOUS, plan=plan, planning_receipt=receipt,
                        errors=["The model requested clarification; no source query ran."],
                        elapsed_ms=(perf_counter() - arm_start) * 1000,
                    )
                else:
                    if planning_only:
                        return PreparedArm(arm, plan, receipt, arm_start)
                    selected = self.executor_factory(arm)
                    if selected.context != self.context:
                        raise LabError("Comparison executors do not share the reviewed context")

                    async def observe(event: ExecutionEvent):
                        self.active()
                        pair_event = PairEvent(pair_id=pair_id, arm=arm, phase="execution", event=event)
                        if self.source_state_finalize is not None and event.operation == "Answer" and event.phase == "completed":
                            deferred_answers.append(pair_event)
                        else:
                            await emit(pair_event)

                    execution = await selected.execute(plan, self.scope, on_event=observe)
                    self.active()
                    outcome = ArmResult(
                        arm=arm, status=execution.status, plan=plan, planning_receipt=receipt,
                        execution=execution, errors=execution.errors,
                        elapsed_ms=(perf_counter() - arm_start) * 1000,
                    )
            except LabError as error:
                if error.status == Status.BLOCKED_AUTH:
                    plan, receipt, execution = None, None, None
                else:
                    if isinstance(error, PlanValidationError):
                        receipt = {
                            **error.receipt, "validation_status": "REJECTED_NOT_EXECUTED",
                            "validation_error": str(error),
                            "rejected_candidate": error.candidate.model_dump(mode="json"),
                        }
                    elif isinstance(error, ModelCompletionError) and error.validation_details:
                        receipt = {
                            **(receipt or {}), "validation_status": "INVALID_MODEL_OUTPUT_NOT_EXECUTED",
                            "output_validation": error.validation_details,
                        }
                    if (model_usage := usage_receipt()) is not None:
                        receipt = {**(receipt or {}), "model_usage": model_usage}
                outcome = ArmResult(
                    arm=arm, status=error.status, plan=plan, planning_receipt=receipt, execution=execution,
                    errors=[str(error)], elapsed_ms=(perf_counter() - arm_start) * 1000,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Comparison arm failed unexpectedly")
                if (model_usage := usage_receipt()) is not None:
                    receipt = {**(receipt or {}), "model_usage": model_usage}
                outcome = ArmResult(
                    arm=arm, status=Status.FAILED, plan=plan, planning_receipt=receipt, execution=execution,
                    errors=["Application comparison failed; see the local server log."],
                    elapsed_ms=(perf_counter() - arm_start) * 1000,
                )
            if self.source_state_finalize is None and not planning_only:
                await emit(PairEvent(pair_id=pair_id, arm=arm, phase="arm_finished", outcome=outcome))
            return outcome

        finalize_required = True
        if self.planned_source_state_probe is not None:
            if self.arm_execution_mode == "parallel":
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(run_arm(arm, planning_only=True)) for arm in ("baseline", "native")]
                prepared = [task.result() for task in tasks]
            else:
                prepared = [await run_arm(arm, planning_only=True) for arm in ("baseline", "native")]
            required = {
                node.model for result in prepared if isinstance(result, PreparedArm)
                for node in result.plan.nodes if isinstance(node, QueryNode)
            }
            finalize_required = bool(required)
            if required:
                await preflight(required)

            async def finish(result: ArmResult | PreparedArm) -> ArmResult:
                if isinstance(result, ArmResult):
                    return result
                outcome = await run_arm(result.arm, result)
                if not isinstance(outcome, ArmResult):
                    raise LabError("Execution returned an unresolved prepared arm", Status.BLOCKED_CAPABILITY)
                return outcome

            if self.arm_execution_mode == "parallel":
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(finish(result)) for result in prepared]
                outcomes = [task.result() for task in tasks]
            else:
                outcomes = [await finish(result) for result in prepared]
        else:
            if self.arm_execution_mode == "parallel":
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(run_arm(arm)) for arm in ("baseline", "native")]
                outcomes = [task.result() for task in tasks]
            else:
                outcomes = [await run_arm(arm) for arm in ("baseline", "native")]
        if self.source_state_finalize is not None and finalize_required:
            try:
                self.active()
                verified_states = await self.source_state_finalize()
                self.active()
            except LabError as error:
                for outcome in outcomes:
                    outcome.status = error.status
                    outcome.errors.append(str(error))
                    if outcome.execution is not None:
                        outcome.execution = outcome.execution.model_copy(update={
                            "status": error.status, "answer": None, "additional_answers": {},
                            "errors": [*outcome.execution.errors, str(error)],
                        })
                    if error.status == Status.BLOCKED_AUTH:
                        outcome.plan, outcome.planning_receipt, outcome.execution = None, None, None
            else:
                if verified_states is not None:
                    verified = {alias: state.model_dump(mode="json") for alias, state in verified_states.items()}
                    for outcome in outcomes:
                        if outcome.planning_receipt is not None:
                            outcome.planning_receipt = {
                                **outcome.planning_receipt, "post_pair_source_states": verified,
                            }
                for event in deferred_answers:
                    await emit(event)
            for outcome in outcomes:
                await emit(PairEvent(pair_id=pair_id, arm=outcome.arm, phase="arm_finished", outcome=outcome))
        elif self.planned_source_state_probe is not None:
            for outcome in outcomes:
                await emit(PairEvent(pair_id=pair_id, arm=outcome.arm, phase="arm_finished", outcome=outcome))
        reasons = []
        queried = set()
        for outcome in outcomes:
            execution = outcome.execution
            views = [execution.answer, *execution.additional_answers.values()] if execution and execution.answer else []
            qualified = bool(views) and outcome.status == Status.PARTIAL and execution.status == Status.PARTIAL and any(
                known_mapped_subset(view) for view in views
            ) and all(
                view.status == Status.COMPLETE or known_mapped_subset(view) for view in views
            )
            if not views or (outcome.status != Status.COMPLETE and not qualified):
                reasons.append(f"{outcome.arm}: no complete executed answer")
                continue
            if execution.mode != "LIVE_DELEGATED" or self.scope.mode != "LIVE_DELEGATED":
                reasons.append(f"{outcome.arm}: not live delegated source execution")
            sources = {receipt.model for receipt in execution.receipts if receipt.model}
            queried.update(sources)
            clocks = [
                clock for view in [execution.answer, *execution.additional_answers.values()]
                for clock in view.source_clocks
            ]
            for source in sources:
                state = expected.get(source)
                observed = [clock for clock in clocks if clock.model == source]
                if state is None or not observed:
                    reasons.append(f"{outcome.arm}/{source}: common observed source state is unproven")
                elif any(clock.data != state.data_revision or clock.model_revision != state.model_revision
                         for clock in observed):
                    reasons.append(f"{outcome.arm}/{source}: source state differs from pair preflight")
        if not queried:
            reasons.append("No source-query evidence was returned")
        return ComparisonResult(
            pair_id=pair_id, question=question, mode=self.scope.mode,
            source_facts_hash=payloads[0]["facts_hash"], context_revision=self.context.revision,
            provider=provider.provider, model_version=provider.model_version,
            expected_source_states=expected, arms=outcomes,
            arm_execution_mode=self.arm_execution_mode,
            comparability="NOT_COMPARABLE" if reasons else "COMMON_SOURCE_STATE",
            comparability_reasons=list(dict.fromkeys(reasons)), elapsed_ms=(perf_counter() - start) * 1000,
            limitations=[
                *(
                    ["Known mapped subsets only: PARTIAL status and explicit exclusions remain; common source state is not a complete population total or population-wide ranking."]
                    if any(outcome.status == Status.PARTIAL for outcome in outcomes) else []
                ),
                *(
                    ["Actual data/model boundary checks cover the union of validated plan query sources after metadata-only planning; rejected/clarification-only proposals execute no business source queries."]
                    if self.planned_source_state_probe is not None else []
                ),
                *(
                    ["Source observations were leased without changing their clocks; complete answers were held until actual post-pair source verification."]
                    if self.source_state_finalize is not None else []
                ),
                "Same facts and common observed source state do not prove an ontology accuracy advantage.",
                (
                    "Arms execute concurrently; elapsed time can be affected by shared-resource contention and service variation."
                    if self.arm_execution_mode == "parallel" else
                    "Arms execute sequentially; elapsed time can be affected by cache warmth and service variation."
                ),
                "Native context provenance and live source execution are distinct evidence.",
                "No submitted-plan fixture or recorded answer is used to route this question.",
            ],
        )
