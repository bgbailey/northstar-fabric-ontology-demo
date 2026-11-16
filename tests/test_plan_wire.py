"""Strict response-format transport only; synthetic plans, no live service calls."""

import json
from copy import deepcopy

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from test_model_provider import SCHEMA, config_at, envelope, provider_at

from telco_iq.contracts import AnswerNode, Plan, Scope
from telco_iq.model_provider import ModelCompletionError
from telco_iq.plan_wire import (
    SERVER_FIELDS,
    decode_strict_plan,
    local_plan_constraints,
    server_plan_fields,
    strict_plan_schema,
    strict_wire_guidance,
)


def fixture_plan():
    return Plan(
        request_id="SYNTHETIC_PLAN", question="Synthetic serialization check",
        principal_scope=Scope(
            tenant="SYNTHETIC", principal="SYNTHETIC", authorization_revision="SYNTHETIC",
            models=["subscriptions"], mode="OFFLINE_SYNTHETIC",
        ),
        as_of="2026-10-20T00:00:00Z", ontology_revision="SYNTHETIC", metric_selections=[],
        nodes=[
            {"id": "source", "kind": "QuerySemanticModel", "model": "subscriptions", "table": "account",
             "columns": ["account_key"], "expected_grain": ["account_key"]},
            {"id": "projection", "kind": "ProjectColumns", "source": "source",
             "columns": {"registered_account": "account_key"}},
        ],
        result_node="projection",
    )


def wire_plan():
    value = fixture_plan().model_dump(mode="json")
    value["nodes"][1]["columns"] = [{"output": "registered_account", "source": "account_key"}]
    for key in SERVER_FIELDS:
        del value[key]
    return value


def request_payload():
    value = fixture_plan().model_dump(mode="json")
    return {
        "question": value["question"], "scope": value["principal_scope"],
        "context": {"revision": value["ontology_revision"]},
        "run_metadata": {key: value[key] for key in ("request_id", "as_of", "budgets")},
    }


@pytest.mark.parametrize(("additional", "code"), [
    ({"Another": "primary"}, "answer_view_source_repeated"),
    ({"First": "other", "Second": "other"}, "answer_view_source_repeated"),
    ({" ": "other"}, "answer_view_label_invalid"),
    ({"x" * 81: "other"}, "answer_view_label_invalid"),
])
def test_answer_view_validation_has_safe_specific_reason_codes(additional, code):
    with pytest.raises(ValidationError) as error:
        AnswerNode(id="answer", kind="SynthesizeAnswer", source="primary", additional_sources=additional)
    assert error.value.errors(include_input=False)[0]["type"] == code


def test_plan_wire_obeys_reviewed_service_subset_and_original_plan_is_unchanged():
    original = deepcopy(Plan.model_json_schema())
    schema = strict_plan_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(wire_plan())
    unsupported = {
        "oneOf", "discriminator", "default", "title", "const", "minimum", "maximum",
        "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems", "minLength", "maxLength",
        "pattern", "format", "minProperties",
    }
    objects = []

    def inspect(value):
        if isinstance(value, dict):
            assert not unsupported & value.keys()
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert value["required"] == list(value["properties"])
                objects.append(value)
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    inspect(schema)
    assert sum(len(value["properties"]) for value in objects) == 86
    assert not set(schema["properties"]) & SERVER_FIELDS
    assert Plan.model_json_schema() == original
    assert schema == strict_plan_schema()


def test_projection_pairs_decode_losslessly_without_changing_input_or_execution_contract():
    value = wire_plan()
    before = deepcopy(value)
    decoded = decode_strict_plan(value, server_plan_fields(request_payload()))
    assert decoded == fixture_plan().model_dump(mode="json")
    assert Plan.model_validate(decoded) == fixture_plan()
    assert value == before


def test_separate_source_grain_views_decode_losslessly_and_remain_explicit_node_references():
    value = wire_plan()
    value["nodes"].append({
        "id": "answer", "kind": "SynthesizeAnswer", "source": "source", "depends_on": [],
        "additional_sources": [{"label": "Account priority", "source": "projection"}],
    })
    value["result_node"] = "answer"
    Draft202012Validator(strict_plan_schema()).validate(value)
    decoded = decode_strict_plan(value, server_plan_fields(request_payload()))
    plan = Plan.model_validate(decoded)
    assert plan.nodes[-1].additional_sources == {"Account priority": "projection"}
    value["nodes"][-1]["additional_sources"].append({"label": "Account priority", "source": "source"})
    with pytest.raises(ValueError, match="Separate answer wire"):
        decode_strict_plan(value, server_plan_fields(request_payload()))


@pytest.mark.parametrize("pairs", [
    [{"output": "duplicate", "source": "account_key"}, {"output": "duplicate", "source": "other"}],
    [{"output": "", "source": "account_key"}],
    [{"output": "account", "source": ""}],
    [{"output": "account", "source": "account_key", "extra": "unreviewed"}],
    {"account": "account_key"},
    [None],
])
def test_invalid_projection_encoding_is_not_repaired_or_silently_overwritten(pairs):
    value = wire_plan()
    value["nodes"][1]["columns"] = pairs
    with pytest.raises(ValueError, match="Projection wire"):
        decode_strict_plan(value, server_plan_fields(request_payload()))


async def test_actual_provider_sends_strict_response_format_and_returns_canonical_plan(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=envelope(content=json.dumps(wire_plan())))

    config = config_at(tmp_path, output_mode="json_schema")
    provider = provider_at(tmp_path, config=config, handler=handler)
    text = await provider.complete("Synthetic typed-plan guidance", request_payload(), Plan.model_json_schema())
    assert Plan.model_validate_json(text) == fixture_plan()
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "telco_iq_plan_v1", "strict": True, "schema": strict_plan_schema()},
    }
    assert "alias pairs" in body["messages"][0]["content"]
    assert not {"tools", "temperature", "api-version", "max_tokens"} & body.keys()
    assert provider.last_receipt.outcome == "COMPLETE" and provider.last_receipt.cost_state == "RECONCILED"
    persisted = config.ledger_path.read_bytes().decode("latin1")
    assert "registered_account" not in persisted and "SYNTHETIC_UNIT_TOKEN" not in persisted


async def test_strict_mode_other_schemas_fail_before_token_or_spend(tmp_path):
    provider = provider_at(tmp_path, config=config_at(tmp_path, output_mode="json_schema"))
    with pytest.raises(ModelCompletionError, match="requires the Plan"):
        await provider.complete("Synthetic", {}, SCHEMA)
    assert not provider.config.ledger_path.exists()


async def test_server_metadata_is_snapshotted_before_awaiting_the_model(tmp_path):
    payload = request_payload()

    async def handler(request):
        sent = json.loads(request.content)
        assert json.loads(sent["messages"][1]["content"]) == request_payload()
        payload["question"] = "CHANGED_AFTER_DISPATCH"
        payload["run_metadata"]["request_id"] = "CHANGED_AFTER_DISPATCH"
        payload["run_metadata"]["budgets"]["max_queries"] = 1
        payload["scope"]["models"].append("UNAPPROVED_AFTER_DISPATCH")
        return httpx.Response(200, json=envelope(content=json.dumps(wire_plan())))

    provider = provider_at(tmp_path, config=config_at(tmp_path, output_mode="json_schema"), handler=handler)
    returned = await provider.complete("Synthetic", payload, Plan.model_json_schema())
    assert Plan.model_validate_json(returned) == fixture_plan()


@pytest.mark.parametrize("change", ["extra_field", "missing_field", "row_limit", "duplicate_alias", "empty_alias"])
async def test_service_wire_and_original_typed_constraints_remain_enforced_without_paid_repair(tmp_path, change):
    value = wire_plan()
    if change == "extra_field":
        value["unreviewed"] = "PRIVATE_TEST_VALUE"
    elif change == "missing_field":
        del value["hypotheses"]
    elif change == "row_limit":
        value["nodes"][0]["max_rows"] = 100001
    elif change == "duplicate_alias":
        value["nodes"][1]["columns"] *= 2
    else:
        value["nodes"][1]["columns"][0]["output"] = ""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=envelope(content=json.dumps(value)))

    provider = provider_at(tmp_path, config=config_at(tmp_path, output_mode="json_schema"), handler=handler)
    with pytest.raises(ModelCompletionError) as error:
        await provider.complete("Synthetic", request_payload(), Plan.model_json_schema())
    assert error.value.code == "OUTPUT_INVALID" and "PRIVATE_TEST" not in str(error.value)
    assert len(calls) == 1 and provider.budget_snapshot()["requests"] == 1
    assert provider.last_receipt.cost_state == "RECONCILED"
    assert error.value.validation_details
    assert "PRIVATE_TEST" not in json.dumps(error.value.validation_details)


def test_wire_does_not_remove_original_local_numeric_bounds():
    value = wire_plan()
    value["nodes"][0]["max_rows"] = 100001
    Draft202012Validator(strict_plan_schema()).validate(value)
    with pytest.raises(ValidationError):
        Plan.model_validate(decode_strict_plan(value, server_plan_fields(request_payload())))


def test_local_constraints_are_derived_from_the_original_contract_not_guessed_or_dropped():
    schema = Plan.model_json_schema()
    local = local_plan_constraints()
    assert local["QueryNode.id"]["pattern"] == schema["$defs"]["QueryNode"]["properties"]["id"]["pattern"]
    assert local["Plan.nodes"] == {"maxItems": 100, "minItems": 1}
    assert local["QueryNode.max_rows"]["maximum"] == 100000
    assert local["ProjectColumns.columns"]["minProperties"] == 1
    assert not any(key.startswith(("Scope.", "Budgets.")) for key in local)
    assert not any(f"Plan.{name}" in local for name in SERVER_FIELDS)
    assert json.dumps(local, sort_keys=True, separators=(",", ":")) in strict_wire_guidance()
    assert sum(len(value.get("properties", {})) for value in strict_plan_schema()["$defs"].values()) > 0


async def test_canonical_union_errors_retain_the_precise_typed_field_without_its_private_value(tmp_path):
    value = wire_plan()
    value["nodes"][1]["id"] = "PRIVATE_TEST_INVALID_NODE.ID"
    config = config_at(tmp_path, output_mode="json_schema")
    provider = provider_at(
        tmp_path, config=config,
        handler=lambda request: httpx.Response(200, json=envelope(content=json.dumps(value))),
    )
    with pytest.raises(ModelCompletionError) as error:
        await provider.complete("Synthetic", request_payload(), Plan.model_json_schema())
    details = error.value.validation_details
    assert details == [{
        "kind": "typed_plan", "path": ["nodes", 1, "ProjectColumns", "id"],
        "code": "string_pattern_mismatch",
    }]
    assert "PRIVATE_TEST" not in json.dumps(details)
    assert provider.budget_snapshot()["requests"] == 1


@pytest.mark.parametrize("field", sorted(SERVER_FIELDS))
def test_model_cannot_supply_even_matching_server_owned_fields(field):
    value = wire_plan()
    owned = server_plan_fields(request_payload())
    value[field] = owned[field]
    with pytest.raises(ValueError, match="server-owned"):
        decode_strict_plan(value, owned)


@pytest.mark.parametrize("change", ["missing_metadata", "naive_time", "wrong_budget", "missing_revision"])
async def test_invalid_server_metadata_is_refused_before_spending(tmp_path, change):
    value = request_payload()
    if change == "missing_metadata":
        del value["run_metadata"]
    elif change == "naive_time":
        value["run_metadata"]["as_of"] = "2026-10-20T00:00:00"
    elif change == "wrong_budget":
        value["run_metadata"]["budgets"]["timeout_seconds"] = 301
    else:
        del value["context"]["revision"]
    provider = provider_at(tmp_path, config=config_at(tmp_path, output_mode="json_schema"))
    with pytest.raises(ModelCompletionError, match="server-owned request metadata"):
        await provider.complete("Synthetic", value, Plan.model_json_schema())
    assert not provider.config.ledger_path.exists()
