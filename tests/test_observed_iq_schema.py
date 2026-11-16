"""Observed schema protocol with synthetic identities; never a live-reader grant."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from pydantic import TypeAdapter
from test_reader_probes import setup_probe

from telco_iq.catalog import digest
from telco_iq.errors import LabError
from telco_iq.mcp_client import observed_iq_schema
from telco_iq.mcp_configuration import (
    MCPProfiles,
    ObservedIQSchemaProfileConfig,
    ReviewConfig,
    SchemaProfileConfig,
    SourcePreparationConfig,
)
from telco_iq.mcp_provider import CaptureReview, ReviewedObservedIQSchemaProfile, schema_fingerprint
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink

ROOT = Path(__file__).resolve().parents[1]


def case(identity):
    model = {
        "ArtifactId": str(identity.dataset_id), "Name": "Synthetic Finance", "Description": "Synthetic only",
        "Url": f"https://app.powerbi.com/groups/{identity.workspace_id}/modeling/{identity.dataset_id}/modelView",
        "IconUrl": "https://app.powerbi.com/synthetic-icon.svg",
    }
    data = {
        "semanticModel": model,
        "schema": {
            "Tables": [
                {
                    "Name": "Account Month", "Description": "One source-owned account-month scope.",
                    "Columns": [
                        {"Name": "account_key", "Description": "Conformed identity.", "Type": "Integer",
                         "FormatString": "#,##0"},
                        {"Name": "month", "Description": "Service month.", "Type": "DateTime",
                         "FormatString": "yyyy-MM-dd"},
                    ],
                    "Measures": [{
                        "Name": "Recognized Service Revenue", "Description": "Source-owned amount in cents.",
                        "Type": "Integer", "DisplayFolder": "Finance", "FormatString": "#,##0 cents",
                    }],
                },
                {
                    "Name": "Accounts", "Description": "Conformed account identity.",
                    "Columns": [
                        {"Name": "account_key", "Description": "Conformed identity.", "Type": "Integer"},
                        {"Name": "consent", "Description": "Synthetic customer attribute, not OAuth consent.",
                         "Type": "Boolean"},
                    ],
                },
            ],
            "ActiveRelationships": [{
                "PK": "'Accounts'[account_key]", "FK": "'Account Month'[account_key]",
                "UnidirectionalFilter": "'Accounts' filters 'Account Month'",
            }],
        },
    }
    def result(value=data, citation=None):
        return CallToolResult(
            content=[{"type": "text", "text": json.dumps(value)}],
            structuredContent={"artifact_citation": deepcopy(model if citation is None else citation)},
        )
    profile = ReviewedObservedIQSchemaProfile(
        schema_hash="a" * 64, review=CaptureReview("b" * 64, "Synthetic observed schema only", "synthetic_test"),
        artifact_argument="artifactId", arguments={},
        projection_reference="Observed AI projection only, not exhaustive physical model or source data.",
    )
    return profile, data, model, result


async def test_observed_schema_text_and_citation_bind_exact_source_and_preserve_actual_type_spellings():
    _, binding, _, _, _ = await setup_probe()
    identity = binding.sources["finance"]
    profile, data, _, result = case(identity)
    profile.verify("a" * 64, synthetic=True)
    value = result()
    before = value.model_dump_json()
    actual = profile.normalize(value, identity)
    assert actual == schema_fingerprint([
        {"name": "Account Month", "columns": [
            {"name": "account_key", "data_type": "Integer"}, {"name": "month", "data_type": "DateTime"},
        ], "measures": ["Recognized Service Revenue"]},
        {"name": "Accounts", "columns": [
            {"name": "account_key", "data_type": "Integer"}, {"name": "consent", "data_type": "Boolean"},
        ], "measures": []},
    ])
    assert value.model_dump_json() == before
    data["schema"]["Tables"].reverse()
    assert profile.normalize(result(data), identity) == actual


@pytest.mark.parametrize("instructions", ["", "Use source-owned service-month measures.", "x" * 10000])
async def test_observed_source_ai_instructions_are_preserved_and_bound_into_schema_fingerprint(instructions):
    _, binding, _, _, _ = await setup_probe()
    identity = binding.sources["finance"]
    profile, data, _, result = case(identity)
    original = profile.normalize(result(data), identity)
    data["schema"]["CustomInstructions"] = instructions
    value = result(data)
    before = value.model_dump_json()
    assert observed_iq_schema(value, identity.dataset_id, identity.workspace_id)["CustomInstructions"] == instructions
    assert profile.normalize(value, identity) == digest({"schema": original, "CustomInstructions": instructions})
    assert value.model_dump_json() == before
    data["schema"]["Tables"].reverse()
    assert profile.normalize(result(data), identity) == profile.normalize(value, identity)
    data["schema"]["CustomInstructions"] += " "
    if len(instructions) < 10000:
        assert profile.normalize(result(data), identity) != profile.normalize(value, identity)
    else:
        with pytest.raises(LabError, match="instructions"):
            profile.normalize(result(data), identity)


@pytest.mark.parametrize("instructions", [None, True, 42, [], {}, "x" * 10001])
async def test_observed_source_ai_instructions_reject_unknown_type_or_oversized_text(instructions):
    _, binding, _, _, _ = await setup_probe()
    identity = binding.sources["finance"]
    profile, data, _, result = case(identity)
    data["schema"]["CustomInstructions"] = instructions
    with pytest.raises(LabError, match="instructions"):
        profile.normalize(result(data), identity)


async def test_source_ai_instruction_drift_is_visible_in_the_actual_reader_probe():
    probe, binding, _, source, _ = await setup_probe()
    profile, data, _, result = case(binding.sources["finance"])
    data["schema"]["CustomInstructions"] = "Service-month amounts are source-owned."
    source.responses["GetSemanticModelSchema"] = result(data)
    probe.schema_profiles["finance"] = replace(profile, schema_hash=probe.source_client.schema_hash("GetSemanticModelSchema"))
    first = await probe(binding)
    data["schema"]["CustomInstructions"] = "Posting-month amounts are source-owned."
    source.responses["GetSemanticModelSchema"] = result(data)
    second = await probe(binding)
    assert first.source_schema_hashes["finance"] != second.source_schema_hashes["finance"]
    assert not first.observed_source_states and not second.observed_source_states


@pytest.mark.parametrize("change", [
    "artifact", "workspace-url", "artifact-url", "url-host", "url-http", "url-query", "url-fragment",
    "citation-artifact", "citation-description",
])
async def test_observed_schema_rejects_unbound_source_or_disagreeing_citation(change):
    _, binding, _, _, _ = await setup_probe()
    identity = binding.sources["finance"]
    profile, data, model, result = case(identity)
    citation = deepcopy(model)
    if change == "artifact":
        data["semanticModel"]["ArtifactId"] = "00000000-0000-0000-0000-000000000099"
    elif change == "workspace-url":
        data["semanticModel"]["Url"] = model["Url"].replace(str(identity.workspace_id), "unbound")
    elif change == "artifact-url":
        data["semanticModel"]["Url"] = model["Url"].replace(str(identity.dataset_id), "unbound")
    elif change == "url-host":
        data["semanticModel"]["Url"] = model["Url"].replace("app.powerbi.com", "unreviewed.test")
    elif change == "url-http":
        data["semanticModel"]["Url"] = model["Url"].replace("https:", "http:")
    elif change == "url-query":
        data["semanticModel"]["Url"] += "?scope=other"
    elif change == "url-fragment":
        data["semanticModel"]["Url"] += "#other"
    elif change == "citation-artifact":
        citation["ArtifactId"] = "00000000-0000-0000-0000-000000000099"
    else:
        citation["Description"] = "Different source."
    with pytest.raises(LabError):
        profile.normalize(result(data, citation), identity)


@pytest.mark.parametrize("change", [
    "error", "partial", "unknown-schema-field", "missing-tables", "empty-tables", "unknown-table-field",
    "column-type", "measure-type", "missing-column-type", "duplicate-table", "duplicate-column",
    "duplicate-measure", "unknown-relationship-field", "non-string-relationship",
])
async def test_observed_schema_does_not_invent_completeness_types_or_missing_names(change):
    _, binding, _, _, _ = await setup_probe()
    identity = binding.sources["finance"]
    profile, data, _, result = case(identity)
    schema = data["schema"]
    table = schema["Tables"][0]
    if change in {"error", "partial"}:
        data[change] = True
    elif change == "unknown-schema-field":
        schema["isComplete"] = True
    elif change == "missing-tables":
        schema.pop("Tables")
    elif change == "empty-tables":
        schema["Tables"] = []
    elif change == "unknown-table-field":
        table["isComplete"] = True
    elif change == "column-type":
        table["Columns"][0]["Type"] = "Unreviewed"
    elif change == "measure-type":
        table["Measures"][0]["Type"] = "Unreviewed"
    elif change == "missing-column-type":
        table["Columns"][0].pop("Type")
    elif change == "duplicate-table":
        schema["Tables"].append(deepcopy(table))
    elif change == "duplicate-column":
        table["Columns"].append(deepcopy(table["Columns"][0]))
    elif change == "duplicate-measure":
        table["Measures"].append(deepcopy(table["Measures"][0]))
    elif change == "unknown-relationship-field":
        schema["ActiveRelationships"][0]["cardinality"] = "guessed"
    else:
        schema["ActiveRelationships"][0]["PK"] = None
    with pytest.raises(LabError):
        profile.normalize(result(data), identity)


async def test_observed_schema_refuses_duplicate_json_keys_and_remote_tool_errors():
    _, binding, _, _, _ = await setup_probe()
    identity = binding.sources["finance"]
    profile, _, _, result = case(identity)
    value = result()
    value.content[0].text = value.content[0].text.replace('"schema":', '"schema": {}, "schema":', 1)
    with pytest.raises(LabError, match="duplicate"):
        profile.normalize(value, identity)
    value = result()
    value.isError = True
    with pytest.raises(LabError):
        profile.normalize(value, identity)


@pytest.mark.parametrize("change", ["schema", "argument", "filter", "reference", "synthetic-live"])
async def test_observed_schema_profile_requires_exact_review_and_unfiltered_argument(change):
    _, binding, _, _, _ = await setup_probe()
    profile, _, _, _ = case(binding.sources["finance"])
    if change == "schema":
        profile = replace(profile, schema_hash="c" * 64)
    elif change == "argument":
        profile = replace(profile, artifact_argument="dataset")
    elif change == "filter":
        profile = replace(profile, arguments={"queries": ["filtered"]})
    elif change == "reference":
        profile = replace(profile, projection_reference=" ")
    with pytest.raises(LabError):
        profile.verify("a" * 64, synthetic=change != "synthetic-live")


async def test_author_shape_review_can_calibrate_format_but_is_not_a_reader_grant():
    _, binding, _, _, _ = await setup_probe()
    profile, _, _, _ = case(binding.sources["finance"])
    profile = replace(profile, review=CaptureReview(
        "b" * 64, "Author format calibration only; actual current probe still required", "author_shape_capture",
    ))
    profile.verify("a" * 64, synthetic=False)
    assert profile.review.provenance == "author_shape_capture"


async def test_reader_probe_uses_observed_projection_without_creating_a_source_state():
    probe, binding, _, source, _ = await setup_probe()
    profile, _, _, result = case(binding.sources["finance"])
    source.responses["GetSemanticModelSchema"] = result()
    probe.schema_profiles["finance"] = replace(profile, schema_hash=probe.source_client.schema_hash("GetSemanticModelSchema"))
    receipt = await probe(binding)
    assert set(receipt.source_schema_hashes) == {"finance"} and not receipt.observed_source_states
    assert source.calls == [("GetSemanticModelSchema", {"artifactId": str(binding.sources["finance"].dataset_id)})]


async def test_old_structured_profile_and_explicit_observed_profile_both_load_without_inference():
    probe, binding, _, _, _ = await setup_probe()
    adapter = TypeAdapter(SchemaProfileConfig | ObservedIQSchemaProfileConfig)
    original = probe.schema_profiles["finance"]
    old = adapter.validate_python(asdict(original))
    assert isinstance(old, SchemaProfileConfig) and old.build() == original
    profile, _, _, _ = case(binding.sources["finance"])
    observed = adapter.validate_python({"kind": "observed_iq_schema_v1", **asdict(profile)})
    assert isinstance(observed, ObservedIQSchemaProfileConfig) and observed.build() == profile


@pytest.mark.parametrize("change", [
    None, "text-drift", "wrong-alias", "wrong-source", "author-shape", "capture-mismatch", "missing-instructions",
])
async def test_runtime_source_instructions_require_original_reader_capture_and_exact_enrolled_schema(tmp_path, change):
    _, binding, _, _, _ = await setup_probe()
    identity = binding.sources["finance"]
    profile, data, _, result = case(identity)
    instructions = "Use source-owned service-month amounts; missing data is not zero."
    data["schema"]["CustomInstructions"] = instructions
    expected = profile.normalize(result(data), identity)
    if change == "text-drift":
        data["schema"]["CustomInstructions"] = "Use posting month."
    elif change == "missing-instructions":
        data["schema"].pop("CustomInstructions")
    evidence = PrivateEvidenceConfig(
        directory=tmp_path / "synthetic-private-captures",
        approval_reference="SYNTHETIC_UNIT_TEST_NOT_LIVE_AUTHORITY",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        max_capture_bytes=50000, max_total_bytes=500000, max_records=10,
    )
    sink = PrivateEvidenceSink(evidence, ROOT)
    captured = sink.capture(
        tool="GetSemanticModelSchema", source_alias="unbound" if change == "wrong-alias" else "finance",
        request={"artifactId": str(identity.dataset_id)}, response=result(data).model_dump(mode="json", exclude_unset=True),
        context={
            "kind": "CURRENT_READER_NATIVE_SCHEMA_METADATA_ONLY", "tool_schema_hash": profile.schema_hash,
            "source": {} if change == "wrong-source" else identity.model_dump(mode="json"),
        },
        private_identifiers={}, synthetic=False, response_kind="MCP_TOOL_RESULT", metadata=True,
    )
    configured = ObservedIQSchemaProfileConfig(
        kind="observed_iq_schema_v1", schema_hash=profile.schema_hash,
        review=ReviewConfig(
            capture_hash="f" * 64 if change == "capture-mismatch" else captured.capture_id,
            review_reference="SYNTHETIC fixture of actual reader-capture shape",
            provenance="author_shape_capture" if change == "author-shape" else "reader_capture",
        ),
        projection_reference=profile.projection_reference,
    )
    profiles = MCPProfiles(
        native_hashes={}, native_arguments={}, sources={}, schemas={"finance": configured},
        preparation=SourcePreparationConfig(evidence=evidence, capture_hashes={"finance": captured.capture_id}),
    )
    if change is None:
        assert profiles.verified_source_instructions(ROOT, binding.sources, {"finance": expected}) == {
            "finance": instructions,
        }
        with pytest.raises(LabError, match="exact permitted"):
            profiles.verified_source_instructions(ROOT, {}, {})
    else:
        with pytest.raises(LabError):
            profiles.verified_source_instructions(ROOT, binding.sources, {"finance": expected})


def test_old_profiles_without_source_preparation_keep_their_serialized_contract():
    profiles = MCPProfiles(native_hashes={}, native_arguments={}, sources={}, schemas={})
    assert "preparation" not in profiles.model_dump(mode="json")
    assert profiles.verified_source_instructions(ROOT, {}, {}) == {}
