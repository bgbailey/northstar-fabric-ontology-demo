"""Planner fairness tests use synthetic metadata and zero paid/provider network calls."""

import asyncio
import json
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from test_native_context import synthetic_capture
from test_native_metric_authority import edge_fixture, native_plan
from test_reader_native_context import reader_case

from telco_iq.approval import Approval
from telco_iq.catalog import Catalog
from telco_iq.context import offline_context
from telco_iq.contracts import Budgets, Filter, Scope, Status
from telco_iq.errors import LabError
from telco_iq.native_context import normalize_native_capture
from telco_iq.planner import Planner, PlanValidationError, build_planner_payload, source_metadata
from telco_iq.reader_native_context import acquire_reader_context

ROOT = Path(__file__).resolve().parents[1]


def test_all_permitted_table_schemas_keys_grains_and_date_roles():
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    scope = Scope(
        tenant="SYNTHETIC", principal="SYNTHETIC", authorization_revision="synthetic",
        models=["subscriptions", "finance"], mode="OFFLINE_SYNTHETIC",
    )
    metadata = source_metadata(catalog, scope)
    expected = set(catalog.models["subscriptions"]["tables"] + catalog.models["finance"]["tables"])
    tables = {table["id"]: table for table in metadata["tables"]}
    assert set(tables) == expected
    assert tables["account"]["models"] == ["subscriptions", "finance"]
    assert "account_key" in tables["account"]["primaryKey"]
    for table_id, spec in tables.items():
        source = catalog.tables[table_id]
        assert spec["columns"] == source["columns"]
        assert spec["grain"] == source["grain"]
        assert spec["primaryKey"] == source["primaryKey"]
        assert {role["column"] for role in spec["date_roles"]} == {
            c for c, dtype in source["columns"].items() if dtype in ("date", "timestamp")
        }
    assert "incident" not in tables
    assert all(m["model"] in scope.models for m in metadata["metrics"])
    assert all("sql" not in metric and "dax" not in metric for metric in metadata["metrics"])


def test_all_eight_model_memberships_keep_all_permitted_schemas():
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    scope = Scope(
        tenant="SYNTHETIC", principal="SYNTHETIC", authorization_revision="synthetic",
        models=list(catalog.models), mode="OFFLINE_SYNTHETIC",
    )
    metadata = source_metadata(catalog, scope)
    expected = {table for model in catalog.models.values() for table in model["tables"]}
    assert {t["id"] for t in metadata["tables"]} == expected
    assert "quarantine" not in expected
    assert {m["id"] for m in metadata["metrics"]} == set(catalog.metrics)
    for table in metadata["tables"]:
        assert table["models"] == [
            model["id"] for model in catalog.raw["models"] if table["id"] in model["tables"]
        ]


def test_flat_and_linked_arms_have_identical_facts_scope_and_execution_contracts(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    baseline, native = [
        build_planner_payload(
            "A synthetic comparison question", kwargs["authorized_scope"], snapshot.context,
            kwargs["catalog"], input_arm=arm, native_snapshot=snapshot,
        )
        for arm in ("baseline", "native")
    ]
    assert baseline["facts_hash"] == native["facts_hash"]
    assert baseline["scope"] == native["scope"]
    assert baseline["metadata"] == native["metadata"]
    assert baseline["context"] == native["context"]
    assert baseline["validation"] == native["validation"]
    assert "native_context" not in baseline
    assert "business_facts" not in native
    reconstructed = {kind: [] for kind in baseline["fact_categories"]}
    for fact in baseline["business_facts"]:
        reconstructed[fact["kind"]].append(fact["value"])
    assert reconstructed == native["native_context"]
    assert reconstructed["formal_native_rules"] == []
    assert len(reconstructed["native_annotation_guidance"]) == 7
    assert len(reconstructed["native_relationships"]) == 3
    assert baseline["capture"] == native["capture"]
    assert baseline["capture"]["reader_verified"] is False
    assert baseline["capture"]["current_reader_metadata"] is None
    assert baseline["capture"]["captured_at"] == snapshot.captured_at.isoformat()
    assert "AUTHOR_ONLY" not in json.dumps([baseline, native])


def test_shared_execution_definitions_are_not_duplicated_and_audit_parts_stay_server_side(tmp_path):
    snapshot, _, catalog = edge_fixture(tmp_path)
    before = snapshot.model_dump(mode="json")
    baseline, native = [
        build_planner_payload("Synthetic", snapshot.context.scope, snapshot.context, catalog,
                              input_arm=arm, native_snapshot=snapshot)
        for arm in ("baseline", "native")
    ]
    assert baseline["facts_hash"] == native["facts_hash"]
    assert baseline["context"] == native["context"]
    assert native["context"]["relationships"] == [
        review.relationship.model_dump(mode="json") for review in snapshot.execution_contracts
    ]
    references = native["native_context"]["reviewed_execution_relationships"]
    assert references == [{"native_relationship": "left-fact-account", "execution_contract_id": "reviewed-left-fact"}]
    assert {reference["execution_contract_id"] for reference in references} == {
        contract["id"] for contract in native["context"]["relationships"]
    }
    for name, source in (("entities", snapshot.entities), ("native_relationships", snapshot.native_relationships),
                         ("source_metric_links", snapshot.metrics)):
        actual = native["native_context"][name]
        assert actual == [{key: value for key, value in entry.items()
                           if key not in {"part", "part_hash", "backing_part_hash"}} for entry in source]
    assert snapshot.model_dump(mode="json") == before
    assert len(json.dumps(native["native_context"])) < len(json.dumps({
        "entities": snapshot.entities, "native_relationships": snapshot.native_relationships,
        "source_metric_links": snapshot.metrics,
        "native_annotation_guidance": snapshot.annotation_guidance,
        "formal_native_rules": snapshot.context.rules,
        "reviewed_execution_relationships": [review.model_dump(mode="json") for review in snapshot.execution_contracts],
    }))


def test_verified_source_ai_instructions_reach_both_arms_identically_and_change_fact_binding(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    instructions = {alias: f"{alias}: use source-owned measures, full source grain and valid periods."
                    for alias in kwargs["authorized_scope"].models}
    plain = build_planner_payload(
        "Synthetic", kwargs["authorized_scope"], snapshot.context, kwargs["catalog"], native_snapshot=snapshot,
    )
    baseline, native = [
        build_planner_payload(
            "Synthetic", kwargs["authorized_scope"], snapshot.context, kwargs["catalog"],
            input_arm=arm, native_snapshot=snapshot, source_instructions=instructions,
        )
        for arm in ("baseline", "native")
    ]
    assert baseline["metadata"] == native["metadata"]
    assert baseline["facts_hash"] == native["facts_hash"] != plain["facts_hash"]
    assert {model["id"]: model["ai_instructions"] for model in baseline["metadata"]["models"]} == instructions
    assert all("ai_instructions" not in model for model in plain["metadata"]["models"])


@pytest.mark.parametrize("instructions", [
    {"finance": "Incomplete model scope."},
    {"unbound": "Not permitted."},
    {"subscriptions": None, "finance": "Source-owned."},
    {"subscriptions": "x" * 10001, "finance": "Source-owned."},
])
def test_partial_unbound_or_unreviewed_instruction_values_do_not_reach_planner(tmp_path, instructions):
    _, _, kwargs = synthetic_capture(tmp_path)
    with pytest.raises(LabError, match="exact permitted"):
        source_metadata(kwargs["catalog"], kwargs["authorized_scope"], source_instructions=instructions)


def test_allowlisted_metadata_excludes_gold_latent_and_author_bodies(tmp_path):
    _, _, kwargs = synthetic_capture(tmp_path)
    catalog = kwargs["catalog"]
    for family in ("models", "tables", "metrics"):
        for item in catalog.raw[family]:
            item.update({
                "gold_answer": "DO_NOT_LEAK_GOLD", "latent": "DO_NOT_LEAK_LATENT",
                "evaluator": "DO_NOT_LEAK_EVALUATOR", "sql": "DO_NOT_LEAK_SQL", "dax": "DO_NOT_LEAK_DAX",
            })
    metadata = source_metadata(catalog, kwargs["authorized_scope"])
    assert "DO_NOT_LEAK" not in json.dumps(metadata)
    catalog.raw["tables"][0]["columns"]["latent_outcome"] = "int64"
    with pytest.raises(LabError, match="Evaluator-only"):
        source_metadata(catalog, kwargs["authorized_scope"])


def test_unobserved_native_arm_and_wrong_scope_fail_without_provider(tmp_path):
    _, _, kwargs = synthetic_capture(tmp_path)
    scope, catalog = kwargs["authorized_scope"], kwargs["catalog"]
    context = offline_context(scope)
    with pytest.raises(LabError, match="not configured"):
        build_planner_payload("Synthetic", scope, context, catalog, input_arm="native")
    wrong = scope.model_copy(update={"principal": "SYNTHETIC_WRONG"})
    with pytest.raises(LabError, match="scope mismatch"):
        build_planner_payload("Synthetic", wrong, context, catalog)
    baseline = build_planner_payload("Synthetic", scope, context, catalog)
    assert baseline["input_arm"] == "baseline"
    assert baseline["business_facts"]


def test_snapshot_catalog_drift_blocks_both_arms(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    kwargs["catalog"].revision = "changed-contract"
    for arm in ("native", "baseline"):
        with pytest.raises(LabError, match="changed"):
            build_planner_payload(
                "Synthetic", kwargs["authorized_scope"], snapshot.context, kwargs["catalog"],
                input_arm=arm, native_snapshot=snapshot,
            )


async def test_missing_provider_is_blocked_capability_with_zero_calls(tmp_path):
    _, _, kwargs = synthetic_capture(tmp_path)
    planner = Planner(None, None, "synthetic-target", "synthetic-artifact")
    with pytest.raises(LabError) as error:
        await planner.propose(
            "Synthetic", kwargs["authorized_scope"], offline_context(kwargs["authorized_scope"]), kwargs["catalog"],
        )
    assert error.value.status == Status.BLOCKED_CAPABILITY


class SyntheticCompletion:
    provider = "SYNTHETIC_UNIT_TEST_ONLY"
    model_version = "SYNTHETIC_NO_MODEL"

    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    async def complete(self, system, payload, schema):
        self.calls.append((system, payload, schema))
        return json.dumps(self.plan)


def synthetic_approval():
    return Approval(
        target_alias="APPROVED_LAB_ONLY", target_digest="SYNTHETIC_TARGET", artifact_digest="SYNTHETIC_ARTIFACT",
        operations=["paid-plan-proposal"], estimated_cost=0, cap=0, currency="USD",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1), ttl_hours=1,
        teardown_owner="SYNTHETIC_TEST", teardown_mechanism="No external resources",
        confirmation_receipt="SYNTHETIC_UNIT_TEST_ONLY_NOT_LIVE_APPROVAL",
    )


async def test_both_arms_use_same_validator_and_schema(tmp_path):
    definition, receipt, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, receipt, **kwargs)
    scope = kwargs["authorized_scope"]
    plan = {
        "request_id": "synthetic-plan", "question": "Synthetic", "principal_scope": scope.model_dump(),
        "as_of": "2026-09-30T00:00:00Z", "ontology_revision": snapshot.context.revision,
        "metric_selections": ["subscriptions.amount"],
        "nodes": [{
            "id": "query", "kind": "QuerySemanticModel", "model": "subscriptions",
            "table": "subscriptions_fact", "metrics": ["subscriptions.amount"],
            "group_by": ["account_key"], "expected_grain": ["account_key"], "date_role": "month",
            "filters": [{"column": "month", "op": "eq", "values": ["2026-08-01"]}],
        }],
        "result_node": "query",
    }
    completion = SyntheticCompletion(plan)
    planner = Planner(completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    proposals = []
    for arm in ("baseline", "native"):
        proposals.append(await planner.propose(
            "Synthetic", scope, snapshot.context, kwargs["catalog"], input_arm=arm, native_snapshot=snapshot,
        ))
    assert proposals[0][0] == proposals[1][0]
    assert completion.calls[0][0] == completion.calls[1][0]
    assert "no additional source ID may repeat" in completion.calls[0][0]
    assert "at most1000rows" in completion.calls[0][0]
    assert "original PARTIAL status" in completion.calls[0][0]
    assert completion.calls[0][2] == completion.calls[1][2]
    assert proposals[0][1]["facts_hash"] == proposals[1][1]["facts_hash"]
    completion.plan = deepcopy(plan)
    completion.plan["nodes"][0]["table"] = "account"
    for arm in ("baseline", "native"):
        with pytest.raises(LabError):
            await planner.propose(
                "Synthetic", scope, snapshot.context, kwargs["catalog"], input_arm=arm, native_snapshot=snapshot,
            )


async def test_expired_approval_never_calls_completion(tmp_path):
    _, _, kwargs = synthetic_capture(tmp_path)
    completion = SyntheticCompletion({})
    approval = synthetic_approval()
    approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    planner = Planner(completion, approval, "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    with pytest.raises(LabError, match="expired"):
        await planner.propose(
            "Synthetic", kwargs["authorized_scope"], offline_context(kwargs["authorized_scope"]), kwargs["catalog"],
        )
    assert completion.calls == []


@pytest.mark.parametrize("failure", ["missing_dependency", "join_cardinality", "snapshot_period"])
async def test_actual_planner_validation_retains_only_rejected_candidate_for_both_arms(tmp_path, failure):
    snapshot, plan, catalog = edge_fixture(tmp_path)
    if failure == "missing_dependency":
        plan.nodes[-1].depends_on = ["undefined_metadata_identifier"]
        expected = "node=join, undefined=undefined_metadata_identifier"
    elif failure == "join_cardinality":
        plan.nodes[-1].cardinality = "one_to_one"
        expected = "cardinality=one_to_one/many_to_one"
    else:
        plan.nodes[0].filters = [
            Filter(column="month", op="gte", values=["2026-08-01"]),
            Filter(column="month", op="lt", values=["2026-09-01"]),
        ]
        expected = "Snapshot requires one period or period grouping"
    returned = plan.model_dump(mode="json")
    completion = SyntheticCompletion(returned)
    planner = Planner(completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    failures = []
    for arm in ("baseline", "native"):
        with pytest.raises(PlanValidationError, match=expected) as error:
            await planner.propose(
                plan.question, plan.principal_scope, snapshot.context, catalog,
                input_arm=arm, native_snapshot=snapshot,
                run_id=plan.request_id, as_of=plan.as_of, budgets=plan.budgets,
            )
        rejected = error.value
        assert rejected.candidate.model_dump(mode="json") == returned
        assert rejected.receipt["answer_generated"] is False
        assert rejected.receipt["input_arm"] == arm
        assert rejected.receipt["model_request_receipt"] is None
        failures.append(rejected)
    assert len(completion.calls) == 2
    assert completion.calls[0][0] == completion.calls[1][0]
    assert completion.calls[0][2] == completion.calls[1][2]
    assert failures[0].receipt["facts_hash"] == failures[1].receipt["facts_hash"]
    completion.plan["nodes"][-1]["id"] = "changed_after_rejection"
    assert all(error.candidate.nodes[-1].id == "join" for error in failures)


async def test_planner_does_not_retain_auth_rejected_candidate(tmp_path):
    snapshot, plan, catalog = edge_fixture(tmp_path)
    returned = plan.model_dump(mode="json")
    returned["principal_scope"]["principal"] = "UNAPPROVED_SYNTHETIC_PRINCIPAL"
    completion = SyntheticCompletion(returned)
    planner = Planner(completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    for arm in ("baseline", "native"):
        with pytest.raises(LabError, match="scope change") as error:
            await planner.propose(
                plan.question, plan.principal_scope, snapshot.context, catalog,
                input_arm=arm, native_snapshot=snapshot,
                run_id=plan.request_id, as_of=plan.as_of, budgets=plan.budgets,
            )
        assert error.value.status == Status.BLOCKED_AUTH
        assert not isinstance(error.value, PlanValidationError)
    assert len(completion.calls) == 2


async def test_server_metadata_is_supplied_enforced_and_preserved_in_both_receipts(tmp_path):
    definition, capture, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, capture, **kwargs)
    plan = native_plan(snapshot)
    completion = SyntheticCompletion(plan.model_dump(mode="json"))
    planner = Planner(completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    results = [
        await planner.propose(
            "Synthetic", plan.principal_scope, snapshot.context, kwargs["catalog"], input_arm=arm,
            native_snapshot=snapshot, run_id=plan.request_id, as_of=plan.as_of, budgets=plan.budgets,
        ) for arm in ("baseline", "native")
    ]
    for _, payload, _ in completion.calls:
        assert payload["run_metadata"] == {
            "request_id": plan.request_id, "as_of": plan.as_of.isoformat(),
            "budgets": plan.budgets.model_dump(mode="json"),
        }
    assert results[0][0] == results[1][0] == plan
    flat, linked = [receipt for _, receipt in results]
    assert flat["input_arm"] == "baseline" and linked["input_arm"] == "native"
    assert flat["principal_scope"] == linked["principal_scope"] == plan.principal_scope.model_dump(mode="json")
    assert flat["native_binding_evidence"] == linked["native_binding_evidence"]
    assert flat["metric_bindings"] == linked["metric_bindings"] == flat["native_binding_evidence"]["metric_bindings"]
    assert flat["facts_hash"] == linked["facts_hash"]
    assert flat["native_binding_evidence"]["metric_bindings"][0]["query_uses"][0]["node_id"] == "query"
    assert "AUTHOR_ONLY" not in json.dumps([flat, linked])


@pytest.mark.parametrize("field", ["request_id", "as_of", "budgets", "question", "ontology_revision"])
@pytest.mark.parametrize("clarification", ["resolved", "required"])
async def test_model_cannot_change_server_run_inputs_even_when_requesting_clarification(tmp_path, field, clarification):
    definition, capture, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, capture, **kwargs)
    plan = native_plan(snapshot)
    returned = plan.model_dump(mode="json")
    returned["clarification_state"] = clarification
    if field == "request_id":
        returned[field] = "MODEL_CHOSEN_ID"
    elif field == "as_of":
        returned[field] = "2026-09-29T00:00:00Z"
    elif field == "budgets":
        returned[field]["max_queries"] = 1
    else:
        returned[field] = "MODEL_CHANGED_INPUT"
    completion = SyntheticCompletion(returned)
    planner = Planner(completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    for arm in ("baseline", "native"):
        with pytest.raises(LabError, match="changed"):
            await planner.propose(
                "Synthetic", plan.principal_scope, snapshot.context, kwargs["catalog"],
                input_arm=arm, native_snapshot=snapshot,
                run_id=plan.request_id, as_of=plan.as_of, budgets=plan.budgets,
            )
    assert len(completion.calls) == 2
    assert completion.plan == returned


@pytest.mark.parametrize("run_inputs", [
    {"run_id": ""}, {"run_id": "   "}, {"as_of": datetime(2026, 10, 1)}, {"budgets": {"max_queries": 100}},
])
def test_invalid_server_metadata_blocks_before_model(tmp_path, run_inputs):
    definition, capture, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, capture, **kwargs)
    with pytest.raises(LabError, match="Server"):
        build_planner_payload(
            "Synthetic", snapshot.context.scope, snapshot.context, kwargs["catalog"],
            native_snapshot=snapshot, **run_inputs,
        )


async def test_reader_session_ended_during_completion_cannot_issue_a_native_plan(tmp_path):
    snapshot, binding, inputs, state, catalog = reader_case(tmp_path)
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    plan = native_plan(snapshot, scope=binding.scope, context=bundle.context)

    class EndingCompletion(SyntheticCompletion):
        async def complete(self, system, payload, schema):
            state["active"] = False
            return await super().complete(system, payload, schema)

    completion = EndingCompletion(plan.model_dump(mode="json"))
    planner = Planner(completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    with pytest.raises(LabError, match="session ended"):
        await planner.propose(
            "Synthetic", binding.scope, bundle.context, catalog, native_snapshot=bundle,
            run_id=plan.request_id, as_of=plan.as_of, budgets=Budgets(),
        )
    assert len(completion.calls) == 1


async def test_live_reader_both_arms_return_native_selection_and_acquisition_evidence(tmp_path):
    snapshot, binding, inputs, _, catalog = reader_case(tmp_path)
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    plan = native_plan(snapshot, scope=binding.scope, context=bundle.context)
    completion = SyntheticCompletion(plan.model_dump(mode="json"))
    planner = Planner(completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    results = [
        await planner.propose(
            "Synthetic", binding.scope, bundle.context, catalog, input_arm=arm, native_snapshot=bundle,
            run_id=plan.request_id, as_of=plan.as_of, budgets=plan.budgets,
        ) for arm in ("baseline", "native")
    ]
    flat, linked = [receipt for _, receipt in results]
    assert flat["facts_hash"] == linked["facts_hash"]
    assert flat["native_binding_evidence"] == linked["native_binding_evidence"]
    proof = flat["native_binding_evidence"]
    assert proof["context_revision"] == bundle.context.revision
    assert proof["authorization_evidence_hashes"] == bundle.evidence_hashes
    assert proof["capture_evidence_hashes"] == snapshot.evidence_hashes
    metric = proof["metric_bindings"][0]
    assert metric["source_dataset_id"] == str(binding.sources["subscriptions"].dataset_id)
    assert snapshot.reader_verified is False


def test_unspecified_offline_metadata_preserves_legacy_callers_without_fake_watermarks(tmp_path):
    definition, capture, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, capture, **kwargs)
    payload = build_planner_payload(
        "Synthetic", snapshot.context.scope, snapshot.context, kwargs["catalog"], native_snapshot=snapshot,
    )
    assert payload["run_metadata"] == {}
    assert "unspecified fields allowed only offline" in payload["run_metadata_policy"]


async def test_distinct_server_arm_ids_preserve_equal_facts_and_metric_bindings(tmp_path):
    definition, capture, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, capture, **kwargs)
    plan = native_plan(snapshot)

    class EchoRunCompletion(SyntheticCompletion):
        async def complete(self, system, payload, schema):
            returned = deepcopy(self.plan)
            returned.update(payload["run_metadata"])
            self.calls.append((system, payload, schema))
            return json.dumps(returned)

    completion = EchoRunCompletion(plan.model_dump(mode="json"))
    planner = Planner(completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    results = [
        await planner.propose(
            "Synthetic", plan.principal_scope, snapshot.context, kwargs["catalog"], input_arm=arm,
            native_snapshot=snapshot, run_id=f"SYNTHETIC_PAIR_{arm}", as_of=plan.as_of, budgets=plan.budgets,
        ) for arm in ("baseline", "native")
    ]
    assert [proposal.request_id for proposal, _ in results] == [
        "SYNTHETIC_PAIR_baseline", "SYNTHETIC_PAIR_native",
    ]
    flat, linked = [receipt for _, receipt in results]
    assert flat["facts_hash"] == linked["facts_hash"]
    assert flat["model_version"] == linked["model_version"]
    assert flat["metric_bindings"] == linked["metric_bindings"]
    assert flat["metric_bindings"] == flat["native_binding_evidence"]["metric_bindings"]
    assert flat["run_metadata"]["request_id"] != linked["run_metadata"]["request_id"]
    for key in ("as_of", "budgets"):
        assert flat["run_metadata"][key] == linked["run_metadata"][key]


async def test_task_local_model_receipts_survive_concurrent_planner_handoff(tmp_path):
    from telco_iq.model_provider import ModelRequestReceipt

    definition, capture, kwargs = synthetic_capture(tmp_path)
    snapshot = normalize_native_capture(definition, capture, **kwargs)
    plan = native_plan(snapshot)

    class ReceiptCompletion(SyntheticCompletion):
        provider = "azure-openai-v1-chat"

        def __init__(self):
            super().__init__(plan.model_dump(mode="json"))
            self.receipt = ContextVar("synthetic_model_receipt", default=None)

        @property
        def last_receipt(self):
            return self.receipt.get()

        async def complete(self, system, payload, schema):
            returned = deepcopy(self.plan)
            returned.update(payload["run_metadata"])
            self.receipt.set(ModelRequestReceipt(
                request_id=str(UUID(int=1 if payload["input_arm"] == "baseline" else 2)),
                approval_id="a" * 64, target_digest="b" * 64, artifact_digest="c" * 64,
                request_hash="d" * 64, response_hash="e" * 64, model_version=self.model_version,
                started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
                outcome="COMPLETE", http_status=200, finish_reason="stop",
                reserved_cost="0", charged_cost="0", cost_state="RECONCILED", currency="USD",
            ))
            await asyncio.sleep(0)
            return json.dumps(returned)

    completion = ReceiptCompletion()
    planner = Planner(completion, synthetic_approval(), "SYNTHETIC_TARGET", "SYNTHETIC_ARTIFACT")
    results = await asyncio.gather(*[
        planner.propose(
            "Synthetic", plan.principal_scope, snapshot.context, kwargs["catalog"], input_arm=arm,
            native_snapshot=snapshot, run_id=f"SYNTHETIC_PAIR_{arm}", as_of=plan.as_of, budgets=plan.budgets,
        ) for arm in ("baseline", "native")
    ])
    assert completion.last_receipt is None
    for index, (proposal, receipt) in enumerate(results, start=1):
        evidence = receipt["model_request_receipt"]
        assert evidence["request_id"] == str(UUID(int=index))
        assert evidence["request_id"] != proposal.request_id
        assert evidence["outcome"] == "COMPLETE"
        assert evidence["charged_cost"] == "0"
