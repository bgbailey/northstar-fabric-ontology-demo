"""UNIT ONLY: synthetic endpoints, approvals, pricing and MockTransport; zero live calls."""

import asyncio
import json
import multiprocessing
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import httpx
import pytest
from pydantic import ValidationError

from telco_iq.approval import Approval
from telco_iq.contracts import Plan, Status
from telco_iq.errors import LabError
from telco_iq.model_provider import (
    REPO,
    TOKEN_SCOPE,
    AzureOpenAICompletion,
    AzureOpenAIConfig,
    ModelCompletionError,
)

SCHEMA = {
    "type": "object", "properties": {"answer": {"type": "integer", "minimum": 1}},
    "required": ["answer"], "additionalProperties": False,
}


async def unit_token():
    return "SYNTHETIC_UNIT_TOKEN_NEVER_REAL"


def config_at(tmp_path, **updates):
    values = {
        "state": "CONFIGURED", "config_path": tmp_path / "unit-private.json",
        "ledger_path": tmp_path / "unit-usage.sqlite3", "artifact_context_digest": "a" * 64,
        "endpoint": "https://unit-test-no-live.openai.azure.com", "deployment": "UNIT-ONLY",
        "model_version": "UNIT-ONLY-version", "output_mode": "json_object",
        "model_context_window_tokens": 1000, "max_input_tokens": 1000, "max_output_tokens": 100,
        "max_request_bytes": 100_000, "max_response_bytes": 100_000, "max_output_bytes": 50_000,
        "timeout_seconds": 2, "max_requests": 4, "input_price_per_million": "1",
        "output_price_per_million": "2", "currency": "USD", "operational_cap": "0.01",
        "capability_evidence_url": "https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/models",
        "capability_evidence_digest": "b" * 64,
        "pricing_evidence_url": "https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/",
        "pricing_evidence_digest": "c" * 64,
    }
    return AzureOpenAIConfig.model_validate({**values, **updates})


def approval_for(config, **updates):
    return Approval.model_validate({
        "target_alias": "APPROVED_LAB_ONLY", "target_digest": config.target_digest,
        "artifact_digest": config.artifact_digest, "operations": ["paid-plan-proposal"],
        "estimated_cost": float(config.operational_cap), "cap": float(config.operational_cap),
        "currency": config.currency, "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "ttl_hours": 1, "teardown_owner": "UNIT-ONLY", "teardown_mechanism": "UNIT-ONLY-NO-RESOURCES",
        "confirmation_receipt": "UNIT-ONLY-NOT-REAL-APPROVAL", **updates,
    })


def envelope(content='{"answer":1}', **updates):
    return {
        "id": "UNIT-ONLY-response", "object": "chat.completion", "model": "UNIT-ONLY-version",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": content, "refusal": None,
        }}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        **updates,
    }


def provider_at(tmp_path, *, handler=None, config=None, approval=None):
    config = config or config_at(tmp_path)
    handler = handler or (lambda request: httpx.Response(200, json=envelope()))
    return AzureOpenAICompletion(
        config, approval or approval_for(config), token_provider=unit_token,
        transport=httpx.MockTransport(handler),
    )


async def test_v1_json_contract_usage_and_sanitized_persistent_receipt(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope())

    provider = provider_at(tmp_path, handler=handler)
    assert await provider.complete("Synthetic system", {"private_text": "UNIT_PROMPT_ONLY"}, SCHEMA) == '{"answer":1}'
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://unit-test-no-live.openai.azure.com/openai/v1/chat/completions"
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer SYNTHETIC_UNIT_TOKEN_NEVER_REAL"
    assert request.headers["accept-encoding"] == "identity"
    body = json.loads(request.content)
    assert body["model"] == "UNIT-ONLY"
    assert body["response_format"] == {"type": "json_object"}
    assert body["n"] == 1 and body["stream"] is False and body["store"] is False
    assert body["max_completion_tokens"] == 100
    assert "JSON" in body["messages"][0]["content"]
    assert json.dumps(SCHEMA, separators=(",", ":")) in body["messages"][0]["content"]
    assert not {"tools", "temperature", "api-version", "max_tokens"} & body.keys()
    receipt = provider.last_receipt
    assert receipt.outcome == "COMPLETE" and receipt.cost_state == "RECONCILED"
    assert receipt.usage.prompt_tokens == 11 and receipt.usage.completion_tokens == 7
    assert receipt.reserved_cost == Decimal("0.0012")
    assert receipt.charged_cost == Decimal("0.000025")
    assert provider.request_receipt(receipt.request_id) == receipt
    assert provider.provider == "azure-openai-v1-chat" and provider.model_version == "UNIT-ONLY-version"
    serialized = receipt.model_dump_json() + provider.config.ledger_path.read_bytes().decode("latin1")
    for sensitive in ("SYNTHETIC_UNIT_TOKEN", "UNIT_PROMPT_ONLY", "Synthetic system", '"answer"'):
        assert sensitive not in serialized
    assert provider.budget_snapshot()["committed_cost"] == "0.000025"
    assert TOKEN_SCOPE == "https://ai.azure.com/.default"


@pytest.mark.parametrize("host", ["openai.azure.com", "services.ai.azure.com"])
async def test_only_supported_azure_origins(tmp_path, host):
    config = config_at(tmp_path, endpoint=f"https://unit-test-no-live.{host}")
    provider = provider_at(tmp_path, config=config)
    assert await provider.complete("Unit", {}, SCHEMA)


@pytest.mark.parametrize("endpoint", [
    "http://unit.openai.azure.com", "https://openai.azure.com",
    "https://unit.openai.azure.com.evil.example", "https://unit.openai.azure.com@evil.example",
    "https://user@unit.openai.azure.com", "https://unit.openai.azure.com:443",
    "https://unit.openai.azure.com/", "https://unit.openai.azure.com/openai/v1",
    "https://unit.openai.azure.com?api-version=preview", "https://unit.openai.azure.com#fragment",
    "https://unit.openai.azure.com\\evil", "https://127.0.0.1", "https://sub.unit.openai.azure.com",
])
def test_reject_url_forwarding(tmp_path, endpoint):
    with pytest.raises(ValidationError, match="exact Azure"):
        config_at(tmp_path, endpoint=endpoint)


@pytest.mark.parametrize("updates", [
    {"config_path": REPO / ".local" / "not-private.json"},
    {"ledger_path": REPO / ".local" / "not-private.sqlite3"},
    {"config_path": Path("relative.json")},
    {"ledger_path": Path("relative.sqlite3")},
    {"ledger_path": Path(r"\\server\share\usage.sqlite3")},
])
def test_configuration_requires_private_local_paths(tmp_path, updates):
    with pytest.raises((ValidationError, LabError)):
        config_at(tmp_path, **updates)


def test_private_sync_and_different_ledger_directories_rejected(tmp_path):
    with pytest.raises(LabError, match="outside repository"):
        config_at(tmp_path, config_path=tmp_path / "OneDrive" / "private.json")
    with pytest.raises(ValidationError, match="one private"):
        config_at(tmp_path, ledger_path=tmp_path / "other" / "usage.sqlite3")


@pytest.mark.parametrize("updates", [
    {"max_input_tokens": 999}, {"input_price_per_million": "0"},
    {"output_price_per_million": "-1"}, {"input_price_per_million": "NaN"},
    {"timeout_seconds": float("nan")}, {"max_requests": True}, {"max_output_tokens": 1001},
    {"output_mode": "unreviewed_mode"}, {"capability_evidence_digest": ""},
    {"capability_evidence_url": "https://untrusted.example/context"},
    {"state": "CONFIGURED", "deployment": None}, {"model_version": "NOT_SELECTED"},
])
def test_no_heuristic_or_unreviewed_capability_configuration(tmp_path, updates):
    with pytest.raises(ValidationError):
        config_at(tmp_path, **updates)


def test_digest_pins_target_artifact_context_paths_limits_and_prices(tmp_path):
    original = config_at(tmp_path)
    for updates in (
        {"deployment": "OTHER-UNIT"}, {"model_version": "OTHER-version"},
        {"endpoint": "https://different-unit.openai.azure.com"},
    ):
        updated = config_at(tmp_path, **updates)
        assert original.target_digest != updated.target_digest
        assert original.artifact_digest != updated.artifact_digest
    for updates in (
        {"artifact_context_digest": "d" * 64}, {"max_requests": 3},
        {"output_price_per_million": "3"}, {"ledger_path": tmp_path / "different.sqlite3"},
        {"max_output_bytes": 40_000}, {"operational_cap": "0.005"},
    ):
        updated = config_at(tmp_path, **updates)
        assert original.target_digest == updated.target_digest
        assert original.artifact_digest != updated.artifact_digest


def test_column_protocol_default_preserves_digest_and_opt_in_is_approval_bound(tmp_path):
    from telco_iq.catalog import digest

    original = config_at(tmp_path, output_mode="json_schema")
    legacy = original.model_dump(mode="json", exclude={"column_reference_mode"})
    assert original.artifact_digest == digest({"contract": "azure-openai-json-plan-v1", **legacy})
    qualified = config_at(tmp_path, output_mode="json_schema", column_reference_mode="source_qualified")
    assert qualified.target_digest == original.target_digest
    assert qualified.artifact_digest != original.artifact_digest
    with pytest.raises(ValidationError, match="require reviewed strict output"):
        config_at(tmp_path, column_reference_mode="source_qualified")
    with pytest.raises(ValidationError):
        config_at(tmp_path, column_reference_mode="guess_or_repair")


async def test_staged_default_cannot_obtain_token_or_dispatch(tmp_path):
    calls = []
    config = config_at(tmp_path, state="STAGED", endpoint=None, deployment=None, model_version=None)

    async def forbidden_token():
        calls.append("token")
        raise AssertionError("No token acquisition while staged")

    provider = AzureOpenAICompletion(config, approval_for(config), token_provider=forbidden_token)
    with pytest.raises(ModelCompletionError, match="STAGED"):
        await provider.complete("Unit", {}, SCHEMA)
    assert calls == [] and provider.last_receipt is None
    assert not config.ledger_path.exists() and provider.model_version == "UNCONFIGURED"


@pytest.mark.parametrize("approval_updates", [
    {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
    {"target_digest": "wrong-target"}, {"artifact_digest": "wrong-artifact"},
    {"target_alias": "OTHER"}, {"operations": ["create-resource"]},
    {"currency": "EUR"}, {"cap": 0.0001}, {"cap": float("inf")}, {"estimated_cost": float("nan")},
])
async def test_approval_rejects_before_token_or_network(tmp_path, approval_updates):
    config = config_at(tmp_path)
    calls = []

    async def forbidden():
        calls.append("token")
        raise AssertionError

    with pytest.raises((LabError, ValidationError)):
        provider = AzureOpenAICompletion(config, approval_for(config, **approval_updates), token_provider=forbidden)
        await provider.complete("Unit", {}, SCHEMA)
    assert not calls and not config.ledger_path.exists()


async def test_expiry_rechecked_after_token_before_reservation(tmp_path):
    config = config_at(tmp_path)
    approval = approval_for(config, expires_at=datetime.now(timezone.utc) + timedelta(milliseconds=30))

    async def delayed_token():
        await asyncio.sleep(0.06)
        return await unit_token()

    provider = AzureOpenAICompletion(config, approval, token_provider=delayed_token)
    with pytest.raises(LabError, match="expired"):
        await provider.complete("Unit", {}, SCHEMA)
    assert not config.ledger_path.exists()


@pytest.mark.parametrize("token_fails", [False, True])
async def test_reader_revoked_while_token_pending_blocks_before_spend(tmp_path, token_fails):
    config = config_at(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    active, requests = [True], []

    async def held_token():
        entered.set()
        await release.wait()
        if token_fails:
            raise RuntimeError("PRIVATE_TOKEN_FAILURE")
        return await unit_token()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope())

    provider = AzureOpenAICompletion(
        config, approval_for(config), token_provider=held_token, transport=httpx.MockTransport(handler),
    )

    async def propose():
        with provider.request_guard(lambda: active[0]):
            with pytest.raises(ModelCompletionError) as error:
                await provider.complete("Unit", {}, SCHEMA)
            assert provider.last_receipt is None
            return error.value

    task = asyncio.create_task(propose())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        active[0] = False
    finally:
        release.set()
    error = await asyncio.wait_for(task, 2)
    assert error.status == Status.BLOCKED_AUTH and error.code == "REQUEST_REVOKED"
    assert not requests and not config.ledger_path.exists()
    assert provider.budget_snapshot()["requests"] == 0
    assert Decimal(provider.budget_snapshot()["committed_cost"]) == 0


async def test_reader_revoked_during_client_entry_blocks_before_reservation(tmp_path):
    active, requests = [True], []
    entered, release = asyncio.Event(), asyncio.Event()

    class HeldEntryTransport(httpx.MockTransport):
        async def __aenter__(self):
            entered.set()
            await release.wait()
            return await super().__aenter__()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope())

    config = config_at(tmp_path)
    provider = AzureOpenAICompletion(
        config, approval_for(config), token_provider=unit_token, transport=HeldEntryTransport(handler),
    )

    async def propose():
        with provider.request_guard(lambda: active[0]):
            with pytest.raises(ModelCompletionError) as error:
                await provider.complete("Unit", {}, SCHEMA)
            assert provider.last_receipt is None
            return error.value

    task = asyncio.create_task(propose())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        active[0] = False
    finally:
        release.set()
    error = await asyncio.wait_for(task, 2)
    assert error.status == Status.BLOCKED_AUTH and error.code == "REQUEST_REVOKED"
    assert not requests and not config.ledger_path.exists()


async def test_final_reader_guard_rolls_back_undispatched_reservation(tmp_path, monkeypatch):
    active, requests = [True], []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope())

    provider = provider_at(tmp_path, handler=handler)
    reserve = provider._ledger.reserve

    @contextmanager
    def revoke_while_reserving(request_hash):
        with reserve(request_hash) as receipt:
            active[0] = False
            yield receipt

    monkeypatch.setattr(provider._ledger, "reserve", revoke_while_reserving)
    with provider.request_guard(lambda: active[0]):
        with pytest.raises(ModelCompletionError) as error:
            await provider.complete("Unit", {}, SCHEMA)
    assert error.value.status == Status.BLOCKED_AUTH
    assert not requests and provider.last_receipt is None
    assert provider.budget_snapshot()["requests"] == 0
    assert Decimal(provider.budget_snapshot()["committed_cost"]) == 0
    restarted = provider_at(tmp_path, config=provider.config, approval=provider._approval)
    assert restarted.budget_snapshot()["requests"] == 0


async def test_two_concurrent_reader_guards_are_isolated_on_shared_provider(tmp_path):
    active = {"revoked": True, "valid": True}
    entered, release = asyncio.Event(), asyncio.Event()
    token_entries, requests = [], []

    async def held_token():
        token_entries.append(True)
        if len(token_entries) == 2:
            entered.set()
        await release.wait()
        return await unit_token()

    def handler(request):
        body = json.loads(request.content)
        requests.append(json.loads(body["messages"][1]["content"])["reader"])
        return httpx.Response(200, json=envelope())

    config = config_at(tmp_path)
    provider = AzureOpenAICompletion(
        config, approval_for(config), token_provider=held_token, transport=httpx.MockTransport(handler),
    )

    async def arm(reader):
        with provider.request_guard(lambda: active[reader]):
            try:
                result = await provider.complete("Unit", {"reader": reader}, SCHEMA)
            except ModelCompletionError as error:
                assert error.status == Status.BLOCKED_AUTH
                return error.code, provider.last_receipt
            return result, provider.last_receipt

    revoked_task, valid_task = [asyncio.create_task(arm(reader)) for reader in ("revoked", "valid")]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        active["revoked"] = False
    finally:
        release.set()
    revoked, valid = await asyncio.wait_for(asyncio.gather(revoked_task, valid_task), 2)
    assert revoked == ("REQUEST_REVOKED", None)
    assert valid[0] == '{"answer":1}' and valid[1].outcome == "COMPLETE"
    assert requests == ["valid"] and provider.last_receipt is None
    assert provider.budget_snapshot()["requests"] == 1
    assert provider.request_receipt(valid[1].request_id) == valid[1]


async def test_nested_reader_guards_cannot_weaken_outer_and_restore_on_exit(tmp_path):
    provider = provider_at(tmp_path)
    active = [False]
    with provider.request_guard(lambda: active[0]):
        with provider.request_guard(lambda: True):
            with pytest.raises(ModelCompletionError) as error:
                await provider.complete("Unit", {}, SCHEMA)
        assert error.value.status == Status.BLOCKED_AUTH
        active[0] = True
        with provider.request_guard(lambda: False):
            with pytest.raises(ModelCompletionError):
                await provider.complete("Unit", {}, SCHEMA)
        assert not provider.config.ledger_path.exists()
        assert await provider.complete("Unit", {}, SCHEMA) == '{"answer":1}'
        active[0] = False
    # The exited guard must not leak into a subsequent explicitly approved non-reader call.
    assert await provider.complete("Unit", {}, SCHEMA) == '{"answer":1}'
    assert provider.budget_snapshot()["requests"] == 2


@pytest.mark.parametrize("result", [False, None, 1, "true"])
async def test_reader_guard_requires_true_without_token_or_spend(tmp_path, result):
    config = config_at(tmp_path)
    tokens = []

    async def token():
        tokens.append(True)
        return await unit_token()

    provider = AzureOpenAICompletion(config, approval_for(config), token_provider=token)
    with provider.request_guard(lambda: result):
        with pytest.raises(ModelCompletionError) as error:
            await provider.complete("Unit", {}, SCHEMA)
    assert error.value.status == Status.BLOCKED_AUTH
    assert not tokens and not config.ledger_path.exists()


async def test_reader_guard_exceptions_are_sanitized_and_scope_restores(tmp_path):
    provider = provider_at(tmp_path)

    def invalid_guard():
        raise LabError("PRIVATE_READER_DETAILS", Status.BLOCKED_AUTH)

    with provider.request_guard(invalid_guard):
        with pytest.raises(ModelCompletionError) as error:
            await provider.complete("Unit", {}, SCHEMA)
    assert error.value.code == "REQUEST_GUARD_FAILED" and error.value.status == Status.BLOCKED_AUTH
    assert "PRIVATE_READER_DETAILS" not in str(error.value)
    assert provider.last_receipt is None and not provider.config.ledger_path.exists()
    assert await provider.complete("Unit", {}, SCHEMA) == '{"answer":1}'


async def test_reader_guard_rejects_async_checks_before_spend(tmp_path):
    provider = provider_at(tmp_path)

    async def async_guard():
        return True

    with pytest.raises(ModelCompletionError, match="synchronous"):
        with provider.request_guard(async_guard):
            pytest.fail("Async guard must not enter")
    with provider.request_guard(lambda: async_guard()):
        with pytest.raises(ModelCompletionError) as error:
            await provider.complete("Unit", {}, SCHEMA)
    assert error.value.status == Status.BLOCKED_AUTH and error.value.code == "REQUEST_GUARD_INVALID"
    assert provider.last_receipt is None and not provider.config.ledger_path.exists()


async def test_reader_guard_cancellation_during_token_wait_never_reserves(tmp_path):
    entered = asyncio.Event()

    async def held_token():
        entered.set()
        await asyncio.Event().wait()

    config = config_at(tmp_path)
    provider = AzureOpenAICompletion(config, approval_for(config), token_provider=held_token)

    async def propose():
        try:
            with provider.request_guard(lambda: True):
                await provider.complete("Unit", {}, SCHEMA)
        except asyncio.CancelledError:
            assert provider.last_receipt is None
            raise

    task = asyncio.create_task(propose())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not config.ledger_path.exists()


@pytest.mark.parametrize("bad_token", ["", "bad\r\nAuthorization: injected", " "])
async def test_invalid_token_never_dispatches_or_reserves(tmp_path, bad_token):
    async def token():
        return bad_token

    config = config_at(tmp_path)
    provider = AzureOpenAICompletion(config, approval_for(config), token_provider=token)
    with pytest.raises(ModelCompletionError, match="invalid bearer"):
        await provider.complete("Unit", {}, SCHEMA)
    assert not config.ledger_path.exists()


async def test_token_provider_exception_is_sanitized_without_spend(tmp_path):
    async def token():
        raise RuntimeError("SECRET_TOKEN_FROM_PROVIDER")

    config = config_at(tmp_path)
    provider = AzureOpenAICompletion(config, approval_for(config), token_provider=token)
    with pytest.raises(ModelCompletionError) as error:
        await provider.complete("Unit", {}, SCHEMA)
    assert "SECRET" not in str(error.value)
    assert not config.ledger_path.exists()


@pytest.mark.parametrize("schema", [
    {"$ref": "https://untrusted.example/schema"},
    {"$id": "https://untrusted.example/schema", "type": "object"},
    {"$dynamicRef": "#anything"}, {"type": "not_a_type"},
])
async def test_invalid_or_remote_schema_rejected_before_spend(tmp_path, schema):
    provider = provider_at(tmp_path)
    with pytest.raises(ModelCompletionError, match="schema|Schema"):
        await provider.complete("Unit", {}, schema)
    assert not provider.config.ledger_path.exists()


async def test_oversized_input_and_nonfinite_data_never_spend(tmp_path):
    provider = provider_at(tmp_path, config=config_at(tmp_path, max_request_bytes=600))
    with pytest.raises(ModelCompletionError, match="byte limit"):
        await provider.complete("Unit", {"text": "x" * 1000}, SCHEMA)
    with pytest.raises(ModelCompletionError, match="finite JSON"):
        await provider.complete("Unit", {"value": float("nan")}, SCHEMA)
    assert not provider.config.ledger_path.exists()


@pytest.mark.parametrize("http_status", [301, 302, 307, 400, 401, 403, 429, 500, 503])
async def test_http_errors_redirects_no_retry_and_keep_reservation(tmp_path, http_status):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(
            http_status, headers={"location": "https://untrusted.example", "Retry-After": "7"}, text="PRIVATE_ERROR",
        )

    provider = provider_at(tmp_path, handler=handler)
    with pytest.raises(ModelCompletionError) as error:
        await provider.complete("Unit", {}, SCHEMA)
    assert "PRIVATE" not in str(error.value) and len(calls) == 1
    receipt = provider.last_receipt
    assert receipt.outcome == "HTTP_ERROR" and receipt.http_status == http_status
    assert receipt.cost_state == "RESERVED" and receipt.charged_cost == receipt.reserved_cost
    assert receipt.retry_after_seconds == (7 if http_status == 429 else None)
    assert provider.budget_snapshot()["requests"] == 1


@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
async def test_optional_reviewed_reasoning_effort_is_bound_and_forwarded_without_changing_the_default(tmp_path, effort):
    config = config_at(tmp_path, **({"reasoning_effort": effort} if effort is not None else {}))
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=envelope())

    provider = provider_at(tmp_path, config=config, handler=handler)
    await provider.complete("Unit", {}, SCHEMA)
    if effort is None:
        assert "reasoning_effort" not in bodies[0] and "reasoning_effort" not in config.model_dump(mode="json")
    else:
        assert bodies[0]["reasoning_effort"] == effort
        baseline = config.model_copy(update={"reasoning_effort": None})
        assert config.artifact_digest != baseline.artifact_digest
    assert provider.last_receipt.outcome == "COMPLETE"


async def test_explicit_provider_pacing_serializes_new_requests_without_any_automatic_retry(tmp_path):
    starts, active, maximum = [], 0, 0

    async def handler(request):
        nonlocal active, maximum
        starts.append(asyncio.get_running_loop().time())
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.005)
        active -= 1
        return httpx.Response(200, json=envelope())

    config = config_at(tmp_path, min_request_interval_seconds=0.03)
    provider = provider_at(tmp_path, config=config, handler=handler)
    await asyncio.gather(*(provider.complete("Unit", {"arm": index}, SCHEMA) for index in range(2)))
    assert len(starts) == provider.budget_snapshot()["requests"] == 2
    assert starts[1] - starts[0] >= 0.03
    assert config.artifact_digest != config.model_copy(update={"min_request_interval_seconds": 0}).artifact_digest


async def test_admission_spacing_does_not_hold_the_gate_until_a_slow_response_finishes(tmp_path):
    starts, second_started = [], asyncio.Event()

    async def handler(request):
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            await second_started.wait()
        else:
            second_started.set()
        return httpx.Response(200, json=envelope())

    provider = provider_at(tmp_path, config=config_at(tmp_path, min_request_interval_seconds=0.03), handler=handler)
    async with asyncio.timeout(1):
        await asyncio.gather(*(provider.complete("Unit", {"arm": index}, SCHEMA) for index in range(2)))
    assert len(starts) == 2 and starts[1] - starts[0] >= 0.03
    assert provider.budget_snapshot()["requests"] == 2

async def test_pacing_wait_is_inside_existing_timeout_and_has_no_reservation_until_dispatch(tmp_path):
    config = config_at(tmp_path, min_request_interval_seconds=0.1, timeout_seconds=0.15)
    provider = provider_at(tmp_path, config=config)
    await provider.complete("Unit", {}, SCHEMA)
    provider._next_dispatch = perf_counter() + 1
    with pytest.raises(ModelCompletionError, match="timed out"):
        await provider.complete("Unit", {}, SCHEMA)
    assert provider.budget_snapshot()["requests"] == 1
    assert provider.last_receipt is None


def test_optional_admission_budget_preserves_default_serialization_and_binds_new_terms(tmp_path):
    default = config_at(tmp_path)
    absent = config_at(tmp_path, admission_timeout_seconds=None)
    split = config_at(tmp_path, admission_timeout_seconds=3)
    assert "admission_timeout_seconds" not in default.model_dump(mode="json")
    assert default.artifact_digest == absent.artifact_digest
    assert default.maximum_invocation_seconds == default.timeout_seconds
    assert split.maximum_invocation_seconds == 5
    assert split.artifact_digest != default.artifact_digest


@pytest.mark.parametrize("admission", [0, -1, 301, float("nan"), float("inf"), True])
def test_invalid_admission_budget_is_rejected(tmp_path, admission):
    with pytest.raises(ValidationError):
        config_at(tmp_path, admission_timeout_seconds=admission)


async def test_split_admission_budget_gives_each_dispatched_arm_its_full_http_budget(tmp_path):
    starts = []

    async def handler(request):
        starts.append(perf_counter())
        await asyncio.sleep(0.7)
        return httpx.Response(200, json=envelope())

    provider = provider_at(tmp_path, config=config_at(
        tmp_path, min_request_interval_seconds=0.6, admission_timeout_seconds=1.5, timeout_seconds=1.0,
    ), handler=handler)

    async def complete(arm):
        await provider.complete("Unit", {"arm": arm}, SCHEMA)
        return provider.last_receipt

    receipts = await asyncio.gather(complete("baseline"), complete("native"))
    assert len(starts) == 2 and starts[1] - starts[0] >= 0.6
    assert all(receipt.outcome == "COMPLETE" for receipt in receipts)
    assert len({receipt.request_id for receipt in receipts}) == 2
    assert receipts[1].admission_wait_ms >= 500
    assert all(receipt.http_elapsed_ms >= 600 for receipt in receipts)
    assert all(provider.request_receipt(receipt.request_id) == receipt for receipt in receipts)
    assert provider.budget_snapshot()["reserved_requests"] == 0


async def test_split_admission_expiry_never_dispatches_or_reserves(tmp_path):
    calls = []
    provider = provider_at(tmp_path, config=config_at(
        tmp_path, min_request_interval_seconds=0.03, admission_timeout_seconds=0.08, timeout_seconds=0.2,
    ), handler=lambda request: calls.append(request))
    provider._next_dispatch = perf_counter() + 1
    with pytest.raises(ModelCompletionError) as error:
        await provider.complete("Unit", {}, SCHEMA)
    assert error.value.code == "ADMISSION_TIMED_OUT"
    assert not calls and provider.last_receipt is None
    assert not provider.config.ledger_path.exists()
    assert not provider._dispatch_gate.locked()


async def test_split_admission_budget_also_bounds_token_acquisition(tmp_path):
    async def slow_token():
        await asyncio.sleep(0.2)
        return "SYNTHETIC_UNIT_TOKEN_NEVER_REAL"

    provider = provider_at(tmp_path, config=config_at(
        tmp_path, admission_timeout_seconds=0.05, timeout_seconds=0.3,
    ))
    provider._token_provider = slow_token
    with pytest.raises(ModelCompletionError) as error:
        await provider.complete("Unit", {}, SCHEMA)
    assert error.value.code == "ADMISSION_TIMED_OUT"
    assert provider.last_receipt is None and not provider.config.ledger_path.exists()


async def test_split_http_timeout_retains_reservation_and_measured_phase_durations(tmp_path):
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=envelope())

    provider = provider_at(tmp_path, config=config_at(
        tmp_path, admission_timeout_seconds=1, timeout_seconds=0.06,
    ), handler=handler)
    with pytest.raises(ModelCompletionError) as error:
        await provider.complete("Unit", {}, SCHEMA)
    assert error.value.code == "TIMED_OUT_UNKNOWN" and len(calls) == 1
    receipt = provider.last_receipt
    assert receipt.outcome == "TIMED_OUT_UNKNOWN" and receipt.cost_state == "RESERVED"
    assert receipt.charged_cost == receipt.reserved_cost
    assert receipt.admission_wait_ms is not None and receipt.http_elapsed_ms >= 40
    assert provider.request_receipt(receipt.request_id) == receipt


async def test_actual_retry_after_only_delays_a_separate_explicit_new_invocation(tmp_path):
    starts = []

    def handler(request):
        starts.append(asyncio.get_running_loop().time())
        if len(starts) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json=envelope())

    provider = provider_at(tmp_path, config=config_at(tmp_path, min_request_interval_seconds=0.02), handler=handler)
    with pytest.raises(ModelCompletionError, match="HTTP 429"):
        await provider.complete("Unit", {}, SCHEMA)
    assert len(starts) == 1 and provider.last_receipt.retry_after_seconds == 1
    await provider.complete("Unit", {}, SCHEMA)
    assert len(starts) == 2 and starts[1] - starts[0] >= 1
    assert provider.budget_snapshot()["requests"] == 2


@pytest.mark.parametrize("mutation,code,reconciled", [
    (lambda e: e.update(model="WRONG"), "MODEL_MISMATCH", False),
    (lambda e: e.update(choices=[]), "RESPONSE_INVALID", False),
    (lambda e: e["choices"].append(deepcopy(e["choices"][0])), "RESPONSE_INVALID", False),
    (lambda e: e.update(usage=None), "USAGE_INVALID", False),
    (lambda e: e["usage"].update(total_tokens=999), "USAGE_INVALID", False),
    (lambda e: e["usage"].update(prompt_tokens=True), "USAGE_INVALID", False),
    (lambda e: e["choices"][0].update(finish_reason="length"), "OUTPUT_INCOMPLETE", True),
    (lambda e: e["choices"][0].update(finish_reason="content_filter"), "CONTENT_FILTERED", True),
    (lambda e: e["choices"][0].update(finish_reason=[]), "FINISH_INVALID", True),
    (lambda e: e["choices"][0]["message"].update(refusal="PRIVATE_REFUSAL"), "MODEL_REFUSED", True),
    (lambda e: e["choices"][0]["message"].update(content='{"answer":'), "OUTPUT_INVALID", True),
    (lambda e: e["choices"][0]["message"].update(content='{"answer":0}'), "OUTPUT_INVALID", True),
    (lambda e: e["choices"][0]["message"].update(content='{"answer":1,"answer":2}'), "OUTPUT_INVALID", True),
    (lambda e: e["choices"][0]["message"].update(content='{"answer":NaN}'), "OUTPUT_INVALID", True),
    (lambda e: e["choices"][0]["message"].update(content='{"answer":1} {}'), "OUTPUT_INVALID", True),
    (lambda e: e["choices"][0]["message"].update(content='[]'), "OUTPUT_INVALID", True),
    (lambda e: e["choices"][0]["message"].update(tool_calls=[{}]), "RESPONSE_INVALID", True),
])
async def test_refused_partial_malformed_responses_never_repair(tmp_path, mutation, code, reconciled):
    response = envelope()
    mutation(response)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    provider = provider_at(tmp_path, handler=handler)
    with pytest.raises(ModelCompletionError) as error:
        await provider.complete("Unit", {}, SCHEMA)
    assert error.value.code == code and "PRIVATE_REFUSAL" not in str(error.value)
    assert len(calls) == 1 and provider.last_receipt.outcome == code
    assert (provider.last_receipt.cost_state == "RECONCILED") == reconciled
    assert provider.budget_snapshot()["requests"] == 1


@pytest.mark.parametrize("raw", [b"partial {", b'{"object":"chat.completion","object":"other"}', b"\xff"])
async def test_invalid_envelope_retains_full_reservation(tmp_path, raw):
    provider = provider_at(tmp_path, handler=lambda _: httpx.Response(
        200, content=raw, headers={"content-type": "application/json"},
    ))
    with pytest.raises(ModelCompletionError, match="Malformed"):
        await provider.complete("Unit", {}, SCHEMA)
    assert provider.last_receipt.cost_state == "RESERVED"


async def test_nonfinite_exponent_and_surrogate_text_fail_safely(tmp_path):
    contents = iter(['{"answer":1e999}', '{"answer":"\\ud800"}'])
    provider = provider_at(tmp_path, handler=lambda _: httpx.Response(
        200, json=envelope(next(contents)),
    ))
    with pytest.raises(ModelCompletionError, match="typed validation"):
        await provider.complete("Unit", {}, {"type": "object"})
    with pytest.raises(ModelCompletionError, match="typed validation"):
        await provider.complete("Unit", {}, SCHEMA)
    assert provider.budget_snapshot()["requests"] == 2


@pytest.mark.parametrize("headers,code", [
    ({"content-type": "text/event-stream"}, "RESPONSE_TYPE"),
    ({"content-type": "application/json", "content-encoding": "br"}, "RESPONSE_ENCODING"),
])
async def test_streaming_or_compressed_response_not_accepted(tmp_path, headers, code):
    provider = provider_at(tmp_path, handler=lambda _: httpx.Response(200, headers=headers))
    with pytest.raises(ModelCompletionError) as error:
        await provider.complete("Unit", {}, SCHEMA)
    assert error.value.code == code and provider.last_receipt.cost_state == "RESERVED"


async def test_corrupt_existing_ledger_fails_closed_without_network(tmp_path):
    config = config_at(tmp_path)
    config.ledger_path.write_bytes(b"UNIT-NOT-A-SQLITE-DATABASE")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=envelope())

    provider = provider_at(tmp_path, config=config, handler=handler)
    with pytest.raises(ModelCompletionError, match="ledger unavailable"):
        await provider.complete("Unit", {}, SCHEMA)
    assert not calls and provider.last_receipt is None


@pytest.mark.parametrize("level", ["response", "output"])
async def test_byte_limits_on_response_and_content(tmp_path, level):
    config = config_at(tmp_path, **(
        {"max_response_bytes": 100, "max_output_bytes": 100} if level == "response"
        else {"max_output_bytes": 10}
    ))
    provider = provider_at(tmp_path, config=config)
    with pytest.raises(ModelCompletionError, match="byte limit"):
        await provider.complete("Unit", {}, SCHEMA)
    assert provider.last_receipt.outcome == (
        "RESPONSE_TOO_LARGE" if level == "response" else "OUTPUT_TOO_LARGE"
    )


async def test_usage_over_reviewed_ceiling_blocks_future_attempts(tmp_path):
    response = envelope(usage={"prompt_tokens": 1001, "completion_tokens": 200, "total_tokens": 1201})
    provider = provider_at(tmp_path, handler=lambda _: httpx.Response(200, json=response))
    with pytest.raises(ModelCompletionError, match="ceiling"):
        await provider.complete("Unit", {}, SCHEMA)
    assert provider.last_receipt.charged_cost == Decimal("0.001401")
    restarted = provider_at(tmp_path, config=provider.config, approval=provider._approval)
    with pytest.raises(ModelCompletionError, match="invalidated"):
        await restarted.complete("Unit", {}, SCHEMA)
    assert restarted.budget_snapshot()["requests"] == 1


@pytest.mark.parametrize("kind", ["timeout", "transport", "cancel"])
@pytest.mark.parametrize("guarded", [False, True])
async def test_unknown_dispatch_outcomes_are_durable(tmp_path, kind, guarded):
    active = [True]

    async def handler(request):
        active[0] = False
        if kind == "timeout":
            raise httpx.ReadTimeout("PRIVATE_TRANSPORT", request=request)
        if kind == "transport":
            raise httpx.ConnectError("PRIVATE_TRANSPORT", request=request)
        raise asyncio.CancelledError

    provider = provider_at(tmp_path, handler=handler)
    with provider.request_guard(lambda: active[0]) if guarded else nullcontext():
        with pytest.raises(asyncio.CancelledError if kind == "cancel" else ModelCompletionError):
            await provider.complete("Unit", {}, SCHEMA)
    receipt = provider.last_receipt
    assert receipt.cost_state == "RESERVED" and receipt.usage is None
    assert "PRIVATE_TRANSPORT" not in receipt.model_dump_json()
    assert provider.request_receipt(receipt.request_id) == receipt
    restarted = provider_at(tmp_path, config=provider.config, approval=provider._approval)
    assert restarted.budget_snapshot()["committed_cost"] == "0.0012"


async def test_wall_clock_timeout_limits_slow_response(tmp_path):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=envelope())

    provider = provider_at(tmp_path, config=config_at(tmp_path, timeout_seconds=0.03), handler=handler)
    with pytest.raises(ModelCompletionError, match="timed out"):
        await provider.complete("Unit", {}, SCHEMA)
    assert provider.last_receipt.outcome == "TIMED_OUT_UNKNOWN"


async def test_two_instances_atomic_budget_reservation_before_dispatch(tmp_path):
    config = config_at(tmp_path, operational_cap="0.0012")
    approval = approval_for(config)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=envelope())

    first = provider_at(tmp_path, config=config, approval=approval, handler=handler)
    second = provider_at(tmp_path, config=config, approval=approval, handler=handler)
    task = asyncio.create_task(first.complete("Unit first arm", {}, SCHEMA))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        assert second.budget_snapshot()["committed_cost"] == "0.0012"
        with pytest.raises(ModelCompletionError, match="cap exhausted"):
            await second.complete("Unit second arm", {}, SCHEMA)
        assert len(calls) == 1
    finally:
        release.set()
        await task
    assert second.budget_snapshot()["requests"] == 1
    assert first.last_receipt is None  # A different task's receipt cannot leak here.


async def test_task_local_receipts_same_provider_are_not_racy(tmp_path):
    async def handler(request):
        data = json.loads(request.content)
        arm = json.loads(data["messages"][1]["content"])["arm"]
        await asyncio.sleep(0.01 if arm == 1 else 0)
        return httpx.Response(200, json=envelope(
            content=json.dumps({"answer": arm}),
            usage={"prompt_tokens": arm, "completion_tokens": arm, "total_tokens": 2 * arm},
        ))

    provider = provider_at(tmp_path, handler=handler)

    async def arm(number):
        await provider.complete("Unit", {"arm": number}, SCHEMA)
        return provider.last_receipt

    first, second = await asyncio.gather(arm(1), arm(2))
    assert first.request_id != second.request_id
    assert first.usage.prompt_tokens == 1 and second.usage.prompt_tokens == 2
    assert provider.last_receipt is None
    assert provider.request_receipt(first.request_id) == first
    assert provider.request_receipt(second.request_id) == second


async def test_restart_call_cap_cannot_reset_and_invalid_plans_are_paid(tmp_path):
    config = config_at(tmp_path, max_requests=1)
    approval = approval_for(config)
    first = provider_at(tmp_path, config=config, approval=approval, handler=lambda _: httpx.Response(
        200, json=envelope(content='{"answer":0}'),
    ))
    with pytest.raises(ModelCompletionError):
        await first.complete("Unit", {}, SCHEMA)
    restarted = provider_at(tmp_path, config=config, approval=approval)
    with pytest.raises(ModelCompletionError, match="cap exhausted"):
        await restarted.complete("Unit", {}, SCHEMA)
    assert restarted.budget_snapshot()["requests"] == 1
    assert Decimal(restarted.budget_snapshot()["committed_cost"]) > 0


async def test_changed_approval_receipt_cannot_reset_existing_allowance(tmp_path):
    provider = provider_at(tmp_path)
    await provider.complete("Unit", {}, SCHEMA)
    changed = approval_for(provider.config, expires_at=datetime.now(timezone.utc) + timedelta(hours=2))
    second = provider_at(tmp_path, config=provider.config, approval=changed)
    with pytest.raises(ModelCompletionError, match="changed terms"):
        await second.complete("Unit", {}, SCHEMA)


def _process_attempt(config_values, approval_values, barrier, queue):
    config = AzureOpenAIConfig.model_validate(config_values)
    approval = Approval.model_validate(approval_values)
    provider = provider_at(config.config_path.parent, config=config, approval=approval)
    barrier.wait(timeout=15)
    try:
        asyncio.run(provider.complete("UNIT subprocess", {}, SCHEMA))
    except ModelCompletionError as error:
        queue.put(error.code)
    else:
        queue.put("COMPLETE")


def test_atomic_call_cap_across_processes(tmp_path):
    config = config_at(tmp_path, max_requests=1)
    approval = approval_for(config)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(target=_process_attempt, args=(
            config.model_dump(mode="json"), approval.model_dump(mode="json"), barrier, queue,
        )) for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        results = [queue.get(timeout=25), queue.get(timeout=25)]
        assert sorted(results) == ["BUDGET_EXHAUSTED", "COMPLETE"]
    finally:
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join()
        queue.close()
    assert all(process.exitcode == 0 for process in processes)
    assert provider_at(tmp_path, config=config, approval=approval).budget_snapshot()["requests"] == 1


async def test_actual_plan_schema_dict_union_and_pydantic_constraints(tmp_path):
    plan = {
        "request_id": "UNIT-ONLY", "question": "Unit question",
        "principal_scope": {"tenant": "UNIT", "principal": "UNIT", "authorization_revision": "unit",
                            "models": ["subscriptions"], "mode": "OFFLINE_SYNTHETIC"},
        "as_of": "2026-10-01T00:00:00Z", "ontology_revision": "UNIT", "metric_selections": [],
        "nodes": [
            {"id": "source", "kind": "QuerySemanticModel", "model": "subscriptions", "table": "account",
             "columns": ["account_key"], "expected_grain": ["account_key"]},
            {"id": "rename", "kind": "ProjectColumns", "source": "source", "columns": {"id": "account_key"}},
        ],
        "result_node": "rename",
    }
    provider = provider_at(tmp_path, handler=lambda _: httpx.Response(200, json=envelope(json.dumps(plan))))
    result = await provider.complete("Unit", {}, Plan.model_json_schema())
    assert Plan.model_validate_json(result).nodes[1].columns == {"id": "account_key"}
    plan["nodes"][0]["metrics"] = ["invalid-mixed-mode"]
    with pytest.raises(ModelCompletionError, match="typed validation"):
        await provider.complete("Unit", {}, Plan.model_json_schema())
    assert provider.last_receipt.cost_state == "RECONCILED"


def test_private_config_loader_and_target_free_proposal(tmp_path):
    config = config_at(tmp_path)
    config.config_path.write_text(config.model_dump_json(exclude={"config_path"}), encoding="utf-8")
    loaded = AzureOpenAIConfig.load(config.config_path)
    assert loaded == config
    proposal = json.loads((REPO / "infra" / "azure-openai-proposal.json").read_text(encoding="utf-8"))
    assert proposal["status"] == "STAGED" and proposal["configurationStatus"] == "UNCONFIGURED"
    assert proposal["liveStatus"] == "NOT_RUN"
    assert proposal["deploymentAuthorized"] is False and proposal["inferenceAuthorized"] is False
    for key in ("resource", "model", "modelVersion", "region", "sku", "deployment"):
        assert proposal["selection"][key] == "NOT_SELECTED"
    assert proposal["budget"]["inputPricePerMillion"] is None
    assert proposal["identity"]["tokenScope"] == TOKEN_SCOPE
