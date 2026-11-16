"""Only local synthetic UUIDs, metadata and probe callbacks; no auth or network."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_native_context import synthetic_capture

import telco_iq.reader_native_context as reader_module
from telco_iq.catalog import digest
from telco_iq.contracts import Budgets, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import ReaderBinding, ReaderProbeReceipt, SourceIdentity
from telco_iq.native_context import NativeSnapshot, native_entities_fingerprint, normalize_native_capture
from telco_iq.planner import build_planner_payload
from telco_iq.reader_native_context import (
    ReaderNativeContext,
    ReviewedReaderEnrollment,
    acquire_reader_context,
    revalidate_reader_context,
)


def reader_case(tmp_path, *, formal_rules=False):
    definition, capture, kwargs = synthetic_capture(tmp_path, uuid_sources=True, formal_rules=formal_rules)
    snapshot = normalize_native_capture(definition, capture, **kwargs)
    scope = kwargs["authorized_scope"].model_copy(update={
        "mode": "LIVE_DELEGATED", "principal": "SYNTHETIC_READER",
        "authorization_revision": "SYNTHETIC_SESSION_EPOCH_1",
    })
    binding = ReaderBinding(
        scope=scope, ontology_workspace_id=UUID(int=10), ontology_item_id=UUID(int=11),
        native_snapshot_hash=snapshot.content_hash,
        sources={
            source.alias: SourceIdentity(workspace_id=source.workspace_id, dataset_id=source.dataset_id)
            for source in snapshot.sources
        },
    )
    now = datetime.now(timezone.utc)
    enrollment = ReviewedReaderEnrollment(
        native_snapshot_hash=snapshot.content_hash,
        ontology_workspace_id=binding.ontology_workspace_id, ontology_item_id=binding.ontology_item_id,
        definition_hash=snapshot.evidence_hashes["definition"],
        native_mcp_receipt_hash=snapshot.evidence_hashes["native_mcp_receipt"],
        sources=binding.sources,
        source_schema_hashes={alias: digest({"SYNTHETIC_SCHEMA": alias}) for alias in binding.sources},
        review_reference="SYNTHETIC explicit review linking original capture and ontology identity",
        reviewed_at=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1),
    )
    state = {"scope": scope.model_copy(deep=True), "active": True, "calls": []}

    async def probe(actual_binding):
        state["calls"].append(actual_binding)
        return ReaderProbeReceipt(
            binding_hash=actual_binding.binding_hash, checked_at=datetime.now(timezone.utc),
            native_entities=deepcopy(capture["rawNativeContext"]["list_ontology_entities"]["structuredContent"]["values"]),
            native_rules=deepcopy(capture["rawNativeContext"]["list_ontology_rules"]["structuredContent"]["values"]),
            source_schema_hashes=enrollment.source_schema_hashes,
            evidence_hashes={"synthetic_actual_reads": digest({
                "binding": actual_binding.binding_hash, "values": capture["rawNativeContext"],
                "schemas": enrollment.source_schema_hashes,
            })},
        )

    inputs = {
        "probe": probe, "session_active": lambda current: state["active"] and current == state["scope"],
        "enrollment": enrollment, "enrollment_digest": enrollment.enrollment_digest,
        "capture_receipt": capture,
    }
    return snapshot, binding, inputs, state, kwargs["catalog"]


async def test_acquisition_retains_author_origin_and_exact_current_reader_proof(tmp_path):
    snapshot, binding, inputs, state, catalog = reader_case(tmp_path)
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    assert len(state["calls"]) == 1
    assert bundle.binding == binding
    assert bundle.snapshot == snapshot
    assert bundle.snapshot.reader_verified is False
    assert bundle.snapshot.context.scope.mode == "OFFLINE_SYNTHETIC"
    assert bundle.context.mode == "NATIVE_SNAPSHOT"
    assert bundle.context.scope == binding.scope
    assert bundle.context.revision != snapshot.context.revision
    assert bundle.metrics == snapshot.metrics
    assert bundle.observed_source_states == {}
    assert bundle.evidence_hashes["reviewed_reader_enrollment"] == inputs["enrollment_digest"]
    assert bundle.evidence_hashes["reader_probe_receipt"] == digest(bundle.probe_receipt.model_dump(mode="json"))
    assert bundle.context.rules == []
    bundle.verify(binding.scope)
    metadata = {"run_id": "SYNTHETIC_RUN", "as_of": datetime.now(timezone.utc), "budgets": Budgets()}
    flat, linked = [
        build_planner_payload(
            "Synthetic", binding.scope, bundle.context, catalog,
            input_arm=arm, native_snapshot=bundle, **metadata,
        ) for arm in ("baseline", "native")
    ]
    assert flat["facts_hash"] == linked["facts_hash"]
    assert flat["metadata"] == linked["metadata"]
    assert flat["capture"]["reader_verified"] is False
    assert flat["capture"] == linked["capture"]
    assert flat["capture"]["reader_verified_and_blockers_describe"] == "ORIGINAL_AUTHOR_CAPTURE_ONLY"
    assert flat["capture"]["current_reader_metadata"] == {
        "verified": True, "checked_at": bundle.probe_receipt.checked_at.isoformat(),
        "verification_scope": "CURRENT_READER_NATIVE_ENTITY_RULE_AND_SOURCE_SCHEMA_ACQUISITION",
        "source_freshness_and_business_execution": "SEPARATE_EXECUTION_GATES_NOT_CLAIMED_HERE",
    }
    assert flat["capture"]["captured_at"] == snapshot.captured_at.isoformat()
    assert snapshot.reader_verified is False and snapshot.blockers == bundle.snapshot.blockers
    assert flat["capture"]["authorization_mode"] == "CURRENT_READER_ACQUISITION"
    assert "AUTHOR_ONLY" not in str([flat, linked])


async def test_actual_metadata_revalidation_renews_only_verification_not_original_context_or_clocks(tmp_path, monkeypatch):
    snapshot, binding, inputs, state, _ = reader_case(tmp_path)
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    original = bundle.probe_receipt
    context, evidence = bundle.context, bundle.evidence_hashes
    clock = [original.checked_at + timedelta(seconds=120)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz is not None else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(reader_module, "datetime", Clock)

    async def actual_probe(current):
        receipt = await inputs["probe"](current)
        return receipt.model_copy(update={"checked_at": clock[0]})

    refreshed = await revalidate_reader_context(bundle, probe=actual_probe, scope=binding.scope,
                                                capture_receipt=inputs["capture_receipt"])
    assert len(state["calls"]) == 2 and refreshed.checked_at == clock[0]
    assert bundle.context == context and bundle.evidence_hashes == evidence
    assert bundle.probe_receipt == original and bundle.snapshot == snapshot
    clock[0] = original.checked_at + timedelta(seconds=301)
    bundle.verify(binding.scope)
    assert bundle.verification_receipt.checked_at == refreshed.checked_at
    clock[0] = refreshed.checked_at + timedelta(seconds=301)
    with pytest.raises(LabError, match="expired"):
        bundle.verify(binding.scope)


@pytest.mark.parametrize("change", ["schema", "entity", "stale", "session", "tamper"])
async def test_revalidation_never_accepts_drift_stale_reads_or_changed_acquisition(
    tmp_path, change, monkeypatch,
):
    snapshot, binding, inputs, state, _ = reader_case(tmp_path)
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    original = bundle.verification_receipt
    clock = [original.checked_at + timedelta(seconds=5)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz is not None else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(reader_module, "datetime", Clock)

    async def actual_probe(current):
        receipt = await inputs["probe"](current)
        receipt = receipt.model_copy(update={"checked_at": clock[0]})
        if change == "schema":
            receipt.source_schema_hashes["finance"] = "f" * 64
        elif change == "entity":
            receipt.native_entities[0]["name"] = "Different synthetic entity"
        elif change == "stale":
            receipt.checked_at = original.checked_at
        elif change == "session":
            state["active"] = False
        else:
            bundle._context.revision = "tampered"
        return receipt

    with pytest.raises(LabError):
        await revalidate_reader_context(bundle, probe=actual_probe, scope=binding.scope,
                                        capture_receipt=inputs["capture_receipt"])
    assert bundle.verification_receipt == original


async def test_actual_entity_and_property_order_changes_do_not_fabricate_native_value_drift(tmp_path):
    snapshot, binding, inputs, _, _ = reader_case(tmp_path)
    original = inputs["probe"]
    async def reordered(actual):
        receipt = await original(actual)
        receipt.native_entities.reverse()
        for entity in receipt.native_entities:
            entity["properties"].reverse()
        return receipt
    inputs["probe"] = reordered
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    bundle.verify(binding.scope)
    assert bundle.probe_receipt.native_entities != inputs["capture_receipt"][
        "rawNativeContext"
    ]["list_ontology_entities"]["structuredContent"]["values"]
    assert bundle.snapshot == snapshot and not bundle.snapshot.reader_verified


@pytest.mark.parametrize("change", ["duplicate-entity", "duplicate-property", "key-order", "property-value"])
def test_named_native_fingerprint_keeps_identity_values_ordered_keys_and_uniqueness_strict(change):
    original = [{
        "name": "Synthetic", "lineageTag": "synthetic-lineage", "keyProperties": ["a", "b"],
        "properties": [{"name": "a", "dataType": "int64"}, {"name": "b", "dataType": "string"}],
        "annotations": [{"name": "first", "value": "one"}, {"name": "second", "value": "two"}],
    }]
    changed = deepcopy(original)
    if change == "duplicate-entity":
        changed.append(deepcopy(changed[0]))
    elif change == "duplicate-property":
        changed[0]["properties"].append(deepcopy(changed[0]["properties"][0]))
    elif change == "key-order":
        changed[0]["keyProperties"].reverse()
    else:
        changed[0]["properties"][0]["dataType"] = "string"
    if change.startswith("duplicate"):
        with pytest.raises(LabError):
            native_entities_fingerprint(changed)
    else:
        assert native_entities_fingerprint(changed) != native_entities_fingerprint(original)


@pytest.mark.parametrize("change", [
    "snapshot", "ontology_workspace", "ontology_item", "source_workspace", "source_dataset",
    "source_alias", "definition", "capture_receipt", "trusted_digest", "enrollment_expired",
])
async def test_wrong_binding_or_enrollment_blocks_before_probe(tmp_path, change):
    snapshot, binding, inputs, state, _ = reader_case(tmp_path)
    if change == "snapshot":
        binding.native_snapshot_hash = "a" * 64
    elif change == "ontology_workspace":
        binding.ontology_workspace_id = UUID(int=20)
    elif change == "ontology_item":
        binding.ontology_item_id = UUID(int=21)
    elif change == "source_workspace":
        binding.sources["finance"].workspace_id = UUID(int=22)
    elif change == "source_dataset":
        binding.sources["finance"].dataset_id = UUID(int=23)
    elif change == "source_alias":
        binding.sources["renamed"] = binding.sources.pop("finance")
        binding.scope.models = ["subscriptions", "renamed"]
        state["scope"] = binding.scope.model_copy(deep=True)
    elif change == "definition":
        inputs["enrollment"].definition_hash = "b" * 64
        inputs["enrollment_digest"] = inputs["enrollment"].enrollment_digest
    elif change == "capture_receipt":
        inputs["capture_receipt"]["completedAt"] = "2026-09-29T00:00:00+00:00"
    elif change == "trusted_digest":
        inputs["enrollment_digest"] = "c" * 64
    elif change == "enrollment_expired":
        inputs["enrollment"].reviewed_at = datetime.now(timezone.utc) - timedelta(hours=2)
        inputs["enrollment"].expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        inputs["enrollment_digest"] = inputs["enrollment"].enrollment_digest
    with pytest.raises(LabError):
        await acquire_reader_context(snapshot, binding, **inputs)
    assert state["calls"] == []


@pytest.mark.parametrize("change,match", [
    ("binding", "different reader binding"),
    ("stale", "stale"),
    ("future", "future"),
    ("entity", "values changed"),
    ("property", "values changed"),
    ("lineage", "values changed"),
    ("rules", "formal native Rule"),
    ("schema", "schema fingerprints"),
    ("schema_missing", "schema fingerprints"),
    ("schema_extra", "schema fingerprints"),
    ("evidence", "bound evidence"),
])
async def test_actual_values_and_current_bound_probe_are_required(tmp_path, change, match):
    snapshot, binding, inputs, _, _ = reader_case(tmp_path)
    original = inputs["probe"]

    async def wrong_probe(actual):
        receipt = await original(actual)
        if change == "binding":
            receipt.binding_hash = "f" * 64
        elif change == "stale":
            receipt.checked_at -= timedelta(seconds=10)
        elif change == "future":
            receipt.checked_at += timedelta(seconds=10)
        elif change == "entity":
            receipt.native_entities[0]["description"] = "Changed meaning"
        elif change == "property":
            receipt.native_entities[0]["properties"][0]["dataType"] = "string"
        elif change == "lineage":
            receipt.native_entities[0]["lineageTag"] = "UNRELATED_NATIVE_ENTITY"
        elif change == "rules":
            receipt.native_rules = [{"statement": "NOT AN OBSERVED FORMAL RULE CODEC"}]
        elif change == "schema":
            receipt.source_schema_hashes["finance"] = "f" * 64
        elif change == "schema_missing":
            del receipt.source_schema_hashes["finance"]
        elif change == "schema_extra":
            receipt.source_schema_hashes["unrelated"] = "e" * 64
        elif change == "evidence":
            receipt.evidence_hashes = {"successful_query": "not-a-hash"}
        return receipt

    inputs["probe"] = wrong_probe
    with pytest.raises(LabError, match=match):
        await acquire_reader_context(snapshot, binding, **inputs)


@pytest.mark.parametrize("when", ["before", "during", "after", "epoch"])
async def test_session_epoch_rechecked_before_after_acquisition_and_before_use(tmp_path, when):
    snapshot, binding, inputs, state, _ = reader_case(tmp_path)
    if when == "before":
        state["active"] = False
    if when == "during":
        original = inputs["probe"]

        async def end_session(actual):
            result = await original(actual)
            state["active"] = False
            return result

        inputs["probe"] = end_session
    if when in ("before", "during"):
        with pytest.raises(LabError) as error:
            await acquire_reader_context(snapshot, binding, **inputs)
    else:
        bundle = await acquire_reader_context(snapshot, binding, **inputs)
        if when == "after":
            state["active"] = False
        else:
            state["scope"].authorization_revision = "SYNTHETIC_SESSION_EPOCH_2"
        with pytest.raises(LabError) as error:
            bundle.verify(binding.scope)
    assert error.value.status == Status.BLOCKED_AUTH
    assert bool(state["calls"]) == (when != "before")


async def test_reader_probe_expires_without_silent_cached_permission(tmp_path, monkeypatch):
    snapshot, binding, inputs, _, _ = reader_case(tmp_path)
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    later = datetime.now(timezone.utc) + timedelta(seconds=301)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(reader_module, "datetime", Later)
    with pytest.raises(LabError, match="expired"):
        bundle.verify(binding.scope)


async def test_reader_properties_are_defensive_copies_not_mutable_grants(tmp_path):
    snapshot, binding, inputs, _, _ = reader_case(tmp_path)
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    bundle.binding.scope.authorization_revision = "FORGED"
    bundle.context.scope.models.clear()
    bundle.metrics[0]["model"] = "unrelated"
    bundle.snapshot.metrics.clear()
    bundle.probe_receipt.native_entities.clear()
    bundle.evidence_hashes.clear()
    bundle.verify(binding.scope)
    assert bundle.binding == binding
    assert bundle.metrics == snapshot.metrics
    with pytest.raises(LabError, match="epoch mismatch"):
        bundle.verify(binding.scope.model_copy(update={"authorization_revision": "FORGED"}))


async def test_no_serialized_reader_verified_flag_or_author_snapshot_can_authorize_live(tmp_path):
    snapshot, binding, inputs, state, catalog = reader_case(tmp_path)
    raw = snapshot.model_dump(mode="json")
    raw["reader_verified"] = True
    with pytest.raises(ValidationError):
        NativeSnapshot.model_validate(raw)
    with pytest.raises(TypeError):
        ReaderNativeContext(reader_verified=True)
    with pytest.raises(LabError):
        build_planner_payload(
            "Synthetic", binding.scope, snapshot.context.model_copy(update={"scope": binding.scope}),
            catalog, native_snapshot=snapshot, input_arm="native",
        )
    with pytest.raises(TypeError, match="enrollment"):
        await acquire_reader_context(
            snapshot, binding, probe=inputs["probe"], session_active=inputs["session_active"],
        )
    assert state["calls"] == []


async def test_boolean_or_untyped_probe_success_cannot_launder_permissions(tmp_path):
    snapshot, binding, inputs, _, _ = reader_case(tmp_path)

    async def permission_boolean(_):
        return True

    inputs["probe"] = permission_boolean
    with pytest.raises(LabError, match="reviewed receipt"):
        await acquire_reader_context(snapshot, binding, **inputs)
    inputs["session_active"] = True
    with pytest.raises(LabError, match="session"):
        await acquire_reader_context(snapshot, binding, **inputs)


async def test_live_payload_requires_all_server_run_inputs(tmp_path):
    snapshot, binding, inputs, _, catalog = reader_case(tmp_path)
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    with pytest.raises(LabError, match="run_id/as_of/budgets"):
        build_planner_payload("Synthetic", binding.scope, bundle.context, catalog, native_snapshot=bundle)


async def test_reviewed_q_typed_fingerprint_blocks_source_drift_with_unchanged_native(tmp_path):
    from telco_iq.mcp_provider import schema_fingerprint

    snapshot, binding, inputs, _, catalog = reader_case(tmp_path)
    reviewed_tables = {
        alias: [
            {
                "name": catalog.tables[table_id]["displayName"],
                "columns": [
                    {"name": name, "data_type": data_type}
                    for name, data_type in catalog.tables[table_id]["columns"].items()
                ],
                "measures": [
                    metric["name"] for metric in catalog.metrics.values()
                    if metric["model"] == alias and metric["table"] == table_id
                ],
            }
            for table_id in catalog.models[alias]["tables"]
        ]
        for alias in binding.sources
    }
    inputs["enrollment"].source_schema_hashes = {
        alias: schema_fingerprint(tables) for alias, tables in reviewed_tables.items()
    }
    inputs["enrollment_digest"] = inputs["enrollment"].enrollment_digest
    bundle = await acquire_reader_context(snapshot, binding, **inputs)
    original = inputs["probe"]
    changed_tables = deepcopy(reviewed_tables["finance"])
    changed_tables[0]["columns"][0]["data_type"] = "SYNTHETIC_CHANGED_TYPE"

    async def source_type_drift(actual):
        receipt = await original(actual)
        assert receipt.native_entities == bundle.probe_receipt.native_entities
        receipt.source_schema_hashes["finance"] = schema_fingerprint(changed_tables)
        return receipt

    inputs["probe"] = source_type_drift
    with pytest.raises(LabError, match="schema fingerprints"):
        await acquire_reader_context(snapshot, binding, **inputs)
