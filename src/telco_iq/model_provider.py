"""Opt-in Azure OpenAI v1 typed planning; no credential or deployment discovery."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from referencing import Registry
from referencing.exceptions import Unresolvable

from telco_iq.approval import Approval, private_config_path
from telco_iq.catalog import digest
from telco_iq.contracts import ColumnReferenceMode, Plan, Status
from telco_iq.errors import LabError
from telco_iq.plan_wire import (
    decode_strict_plan,
    server_plan_fields,
    strict_plan_schema,
    strict_wire_guidance,
)

REPO = Path(__file__).resolve().parents[2]
TOKEN_SCOPE = "https://ai.azure.com/.default"
OPERATION = "paid-plan-proposal"
PROVIDER = "azure-openai-v1-chat"
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Money = Annotated[Decimal, Field(ge=0, max_digits=24, decimal_places=12, allow_inf_nan=False)]
PositiveInt = Annotated[int, Field(gt=0, le=64_000_000, strict=True)]
_ORIGIN = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"\.(?:openai\.azure\.com|services\.ai\.azure\.com)"
)


class AzureOpenAIConfig(BaseModel):
    """Private reviewed configuration, not deployment approval.

    The full documented context window is reserved for input on EVERY request.
    Byte limits control memory/transport, not token counts. No token estimator
    is used. Capability evidence must establish a service-enforced context
    ceiling and max_completion_tokens support for the pinned model version.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    state: Literal["STAGED", "CONFIGURED"] = "STAGED"
    config_path: Path
    ledger_path: Path
    artifact_context_digest: Hash
    endpoint: str | None = None
    deployment: str | None = None
    model_version: str | None = None
    output_mode: Literal["json_object", "json_schema"]
    column_reference_mode: ColumnReferenceMode = "exact"
    input_token_bound: Literal["MODEL_CONTEXT_WINDOW"] = "MODEL_CONTEXT_WINDOW"
    model_context_window_tokens: PositiveInt
    max_input_tokens: PositiveInt
    max_output_tokens: PositiveInt
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    min_request_interval_seconds: float = Field(
        default=0, ge=0, le=120, allow_inf_nan=False, exclude_if=lambda value: value == 0,
    )
    max_request_bytes: PositiveInt
    max_response_bytes: PositiveInt
    max_output_bytes: PositiveInt
    timeout_seconds: float = Field(gt=0, le=300, allow_inf_nan=False)
    admission_timeout_seconds: float | None = Field(
        default=None, gt=0, le=300, strict=True, allow_inf_nan=False,
        exclude_if=lambda value: value is None,
    )
    max_requests: PositiveInt
    input_price_per_million: Money
    output_price_per_million: Money
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    operational_cap: Money
    capability_evidence_url: str
    capability_evidence_digest: Hash
    pricing_evidence_url: str
    pricing_evidence_digest: Hash

    @model_validator(mode="after")
    def reviewed_configuration(self) -> AzureOpenAIConfig:
        for name in ("config_path", "ledger_path"):
            path = getattr(self, name)
            if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
                raise ValueError("Private configuration/ledger require absolute local paths")
            object.__setattr__(self, name, private_config_path(path, REPO))
        if self.config_path == self.ledger_path or self.ledger_path.suffix != ".sqlite3":
            raise ValueError("The private ledger must be a separate .sqlite3 file")
        if self.config_path.parent != self.ledger_path.parent:
            raise ValueError("Use one private configuration directory and approval-wide ledger")
        if self.endpoint is not None and _ORIGIN.fullmatch(self.endpoint) is None:
            raise ValueError("Endpoint must be an exact Azure HTTPS resource origin without path/query/port")
        for value in (self.deployment, self.model_version):
            if value is not None and (
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None
                or value in ("NOT_SELECTED", "UNCONFIGURED")
            ):
                raise ValueError("Deployment/model version must be an explicit pinned identity")
        if self.state == "CONFIGURED" and not all((self.endpoint, self.deployment, self.model_version)):
            raise ValueError("CONFIGURED requires endpoint, deployment and expected response model version")
        if self.column_reference_mode != "exact" and self.output_mode != "json_schema":
            raise ValueError("Source-qualified column proposals require reviewed strict output")
        if self.max_input_tokens != self.model_context_window_tokens:
            raise ValueError("Reserve the complete documented model context window; no token heuristic")
        if self.max_output_tokens > self.model_context_window_tokens:
            raise ValueError("Output token limit exceeds the reviewed model context ceiling")
        if self.max_output_bytes > self.max_response_bytes:
            raise ValueError("Output byte limit exceeds response byte limit")
        pacing_timeout = self.admission_timeout_seconds or self.timeout_seconds
        if self.min_request_interval_seconds >= pacing_timeout:
            raise ValueError("Explicit model pacing must fit within each invocation's existing timeout")
        if self.input_price_per_million <= 0 or self.output_price_per_million <= 0:
            raise ValueError("Positive reviewed upper-bound pricing is required; no assumed free inference")
        for value in (self.capability_evidence_url, self.pricing_evidence_url):
            if re.fullmatch(r"https://(?:learn\.microsoft\.com|azure\.microsoft\.com)/[^\s?#]+", value) is None:
                raise ValueError("Pricing/capability evidence must cite a public Microsoft HTTPS page")
        return self

    @classmethod
    def load(cls, path: Path) -> AzureOpenAIConfig:
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise LabError("Private configuration requires an absolute local path")
        path = private_config_path(path, REPO)
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict) or "config_path" in body:
            raise LabError("Private config must be an object; its file location supplies config_path")
        return cls.model_validate({**body, "config_path": path})

    @property
    def target_digest(self) -> str:
        return digest({
            "provider": PROVIDER, "endpoint": self.endpoint,
            "deployment": self.deployment, "model_version": self.model_version,
        })

    @property
    def artifact_digest(self) -> str:
        values = self.model_dump(mode="json")
        if self.column_reference_mode == "exact":
            values.pop("column_reference_mode")
        return digest({"contract": "azure-openai-json-plan-v1", **values})

    def charge(self, input_tokens: int, output_tokens: int) -> Decimal:
        with localcontext() as context:
            context.prec = 80
            return (
                Decimal(input_tokens) * self.input_price_per_million
                + Decimal(output_tokens) * self.output_price_per_million
            ) / Decimal(1_000_000)

    @property
    def maximum_request_charge(self) -> Decimal:
        return self.charge(self.max_input_tokens, self.max_output_tokens)

    @property
    def maximum_invocation_seconds(self) -> float:
        return self.timeout_seconds + (self.admission_timeout_seconds or 0)


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prompt_tokens: int = Field(ge=0, strict=True)
    completion_tokens: int = Field(ge=0, strict=True)
    total_tokens: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def total(self) -> ModelUsage:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("Usage counts disagree")
        return self


class ModelRequestReceipt(BaseModel):
    """Sanitized local evidence; never stores messages, tokens or reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    approval_id: Hash
    target_digest: Hash
    artifact_digest: Hash
    request_hash: Hash
    response_hash: Hash | None = None
    provider: Literal["azure-openai-v1-chat"] = PROVIDER
    model_version: str
    started_at: datetime
    completed_at: datetime | None = None
    outcome: str = "DISPATCH_RESERVED"
    http_status: int | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86400, exclude_if=lambda value: value is None)
    admission_wait_ms: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, exclude_if=lambda value: value is None,
    )
    http_elapsed_ms: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, exclude_if=lambda value: value is None,
    )
    finish_reason: Literal["stop", "length", "content_filter", "tool_calls", "function_call"] | None = None
    usage: ModelUsage | None = None
    reserved_cost: Decimal
    charged_cost: Decimal
    cost_state: Literal["RESERVED", "RECONCILED"] = "RESERVED"
    currency: str


class ModelCompletionError(LabError):
    def __init__(
        self, code: str, message: str, status: Status = Status.FAILED, *, validation_details: list[dict] | None = None,
    ):
        super().__init__(message, status)
        self.code = code
        self.validation_details = validation_details


class _UsageLedger:
    def __init__(self, config: AzureOpenAIConfig, approval: Approval):
        self.config, self.approval = config, approval
        self.approval_id = digest({"confirmation_receipt": approval.confirmation_receipt})
        self.fingerprint = digest(approval.model_dump(mode="json"))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.config.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.config.ledger_path, timeout=5, isolation_level=None)) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS approvals (approval_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS attempts (request_id TEXT PRIMARY KEY, "
                    "approval_id TEXT NOT NULL, charged_cost TEXT NOT NULL, receipt TEXT NOT NULL)"
                )
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                except BaseException:
                    db.rollback()
                    raise
                else:
                    db.commit()
        except sqlite3.Error:
            raise ModelCompletionError(
                "LEDGER_UNAVAILABLE", "Private usage ledger unavailable; no unaccounted inference permitted"
            ) from None

    @contextmanager
    def reserve(self, request_hash: str) -> Iterator[ModelRequestReceipt]:
        c = self.config
        with self.transaction() as db:
            self.approval.authorize(c.artifact_digest, c.target_digest, OPERATION)
            row = db.execute(
                "SELECT fingerprint FROM approvals WHERE approval_id=?", (self.approval_id,)
            ).fetchone()
            if row is not None and row[0] != self.fingerprint:
                raise ModelCompletionError("APPROVAL_CHANGED", "Approval receipt was reused with changed terms")
            db.execute(
                "INSERT OR IGNORE INTO approvals VALUES (?, ?)", (self.approval_id, self.fingerprint)
            )
            costs = db.execute(
                "SELECT charged_cost, receipt FROM attempts WHERE approval_id=?", (self.approval_id,)
            ).fetchall()
            if any(
                json.loads(row[1])["outcome"] in ("USAGE_EXCEEDS_BOUND", "MODEL_MISMATCH")
                for row in costs
            ):
                raise ModelCompletionError("BOUND_VIOLATED", "Prior response invalidated reviewed model/cost bounds")
            with localcontext() as context:
                context.prec = 80
                spent = sum((Decimal(row[0]) for row in costs), Decimal(0))
                capped = spent + c.maximum_request_charge > c.operational_cap
            if len(costs) >= c.max_requests or capped:
                raise ModelCompletionError("BUDGET_EXHAUSTED", "Approval-wide request or operational cost cap exhausted")
            receipt = ModelRequestReceipt(
                request_id=str(uuid4()), approval_id=self.approval_id,
                target_digest=c.target_digest, artifact_digest=c.artifact_digest,
                request_hash=request_hash, model_version=c.model_version,
                started_at=datetime.now(timezone.utc), reserved_cost=c.maximum_request_charge,
                charged_cost=c.maximum_request_charge, currency=c.currency,
            )
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?)", (
                receipt.request_id, self.approval_id, str(receipt.charged_cost), receipt.model_dump_json(),
            ))
            yield receipt

    def finish(self, receipt: ModelRequestReceipt) -> None:
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE attempts SET charged_cost=?, receipt=? WHERE request_id=? AND approval_id=?",
                (str(receipt.charged_cost), receipt.model_dump_json(), receipt.request_id, self.approval_id),
            ).rowcount
            if changed != 1:
                raise ModelCompletionError("LEDGER_INCONSISTENT", "Reserved attempt is missing; stop all inference")

    def receipt(self, request_id: str) -> ModelRequestReceipt | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT receipt FROM attempts WHERE request_id=? AND approval_id=?",
                (request_id, self.approval_id),
            ).fetchone()
            return ModelRequestReceipt.model_validate_json(row[0]) if row else None

    def snapshot(self) -> dict:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT receipt FROM attempts WHERE approval_id=?", (self.approval_id,)
            ).fetchall()
        receipts = [ModelRequestReceipt.model_validate_json(row[0]) for row in rows]
        with localcontext() as context:
            context.prec = 80
            committed = sum((r.charged_cost for r in receipts), Decimal(0))
        return {
            "approval_id": self.approval_id, "requests": len(receipts),
            "reserved_requests": sum(r.cost_state == "RESERVED" for r in receipts),
            "committed_cost": str(committed), "operational_cap": str(self.config.operational_cap),
            "max_requests": self.config.max_requests, "currency": self.config.currency,
        }


def _strict_json(text: str | bytes) -> object:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Nonfinite JSON number")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Nonfinite JSON number")
        return result

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)


def _schema_validator(schema: dict) -> Draft202012Validator:
    def local_references(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("$id", "$dynamicRef", "$recursiveRef"):
                    raise ModelCompletionError("SCHEMA_UNSUPPORTED", "Schema must use local $defs/$ref only")
                if key == "$ref" and (not isinstance(item, str) or not item.startswith("#/")):
                    raise ModelCompletionError("SCHEMA_UNSUPPORTED", "External schema references are forbidden")
                local_references(item)
        elif isinstance(value, list):
            for item in value:
                local_references(item)

    local_references(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ModelCompletionError("SCHEMA_INVALID", "Invalid supplied JSON schema") from None
    # An empty registry cannot retrieve remote references, even from an invalid nested schema.
    return Draft202012Validator(schema, format_checker=FormatChecker(), registry=Registry())


class AzureOpenAICompletion:
    provider = PROVIDER

    def __init__(
        self, config: AzureOpenAIConfig, approval: Approval, *,
        token_provider: Callable[[], Awaitable[str]], transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = AzureOpenAIConfig.model_validate(config.model_dump())
        self._approval = Approval.model_validate(approval.model_dump())
        self._token_provider, self._transport = token_provider, transport
        self._ledger = _UsageLedger(self.config, self._approval)
        self._dispatch_gate = asyncio.Lock()
        self._next_dispatch = 0.0
        self._receipt: ContextVar[ModelRequestReceipt | None] = ContextVar(
            f"azure_openai_receipt_{id(self)}", default=None,
        )
        self._request_guards: ContextVar[tuple[Callable[[], bool], ...]] = ContextVar(
            f"azure_openai_request_guards_{id(self)}", default=(),
        )

    @property
    def model_version(self) -> str:
        return self.config.model_version or "UNCONFIGURED"

    @property
    def last_receipt(self) -> ModelRequestReceipt | None:
        """Only the current asyncio task's most recent attempt, never another arm's."""
        return self._receipt.get()

    def request_receipt(self, request_id: str) -> ModelRequestReceipt | None:
        return self._ledger.receipt(request_id)

    def budget_snapshot(self) -> dict:
        return self._ledger.snapshot()

    @contextmanager
    def request_guard(self, active: Callable[[], bool]) -> Iterator[None]:
        """Bind a synchronous reader/request check to this task's invocation.

        Live callers MUST wrap each Planner.propose in this context. Only True
        permits inference; callbacks must not perform I/O or return awaitables.
        Nested guards all apply and are restored on exit. Unguarded non-reader
        callers retain their existing explicit approval requirements.
        """
        if (
            not callable(active) or inspect.iscoroutinefunction(active)
            or inspect.iscoroutinefunction(getattr(active, "__call__", None))
        ):
            raise ModelCompletionError(
                "REQUEST_GUARD_INVALID", "Request guard must be a synchronous callable", Status.BLOCKED_AUTH,
            )
        token = self._request_guards.set((*self._request_guards.get(), active))
        try:
            yield
        finally:
            self._request_guards.reset(token)

    def _check_request_guard(self) -> None:
        for active in self._request_guards.get():
            try:
                permitted = active()
            except Exception:
                raise ModelCompletionError(
                    "REQUEST_GUARD_FAILED", "Reader/request authorization check failed", Status.BLOCKED_AUTH,
                ) from None
            if inspect.isawaitable(permitted):
                if inspect.iscoroutine(permitted):
                    permitted.close()
                raise ModelCompletionError(
                    "REQUEST_GUARD_INVALID", "Request guard must not return an awaitable", Status.BLOCKED_AUTH,
                )
            if permitted is not True:
                raise ModelCompletionError(
                    "REQUEST_REVOKED", "Reader/request authorization is no longer active", Status.BLOCKED_AUTH,
                )

    def _authorize(self) -> None:
        c, a = self.config, self._approval
        if c.state != "CONFIGURED":
            raise ModelCompletionError(
                "UNCONFIGURED", "Azure OpenAI is STAGED/UNCONFIGURED; no inference allowed", Status.BLOCKED_CAPABILITY,
            )
        try:
            a.authorize(c.artifact_digest, c.target_digest, OPERATION)
        except LabError as error:
            raise ModelCompletionError("APPROVAL_INVALID", str(error), Status.BLOCKED_AUTH) from None
        if (
            not Decimal(str(a.cap)).is_finite() or not Decimal(str(a.estimated_cost)).is_finite()
            or a.currency != c.currency or c.operational_cap > Decimal(str(a.cap))
            or c.maximum_request_charge > c.operational_cap
        ):
            raise ModelCompletionError("APPROVAL_COST", "Reviewed pricing/currency/cap does not fit approval")

    def _request(self, system: str, payload: dict, schema: dict) -> tuple[bytes, Draft202012Validator]:
        if not isinstance(system, str) or not isinstance(payload, dict) or not isinstance(schema, dict):
            raise ModelCompletionError("INPUT_INVALID", "System text, object payload and object schema are required")
        validator = _schema_validator(schema)
        response_format: dict[str, object] = {"type": self.config.output_mode}
        response_schema = schema
        guidance = "JSON mode is not schema enforcement; all constraints are validated locally."
        if self.config.output_mode == "json_schema":
            if schema != Plan.model_json_schema():
                raise ModelCompletionError("SCHEMA_UNSUPPORTED", "Strict structured planning requires the Plan contract")
            try:
                server_plan_fields(payload)
            except (KeyError, TypeError, ValueError):
                raise ModelCompletionError(
                    "INPUT_INVALID", "Strict planning requires complete server-owned request metadata",
                ) from None
            try:
                response_schema = strict_plan_schema()
            except ValueError:
                raise ModelCompletionError("SCHEMA_UNSUPPORTED", "Plan wire exceeds reviewed structured-output support") from None
            response_format["json_schema"] = {
                "name": "telco_iq_plan_v1", "strict": True, "schema": response_schema,
            }
            guidance = strict_wire_guidance() + " Original Plan constraints are still validated locally after decoding."
        try:
            schema_text = json.dumps(response_schema, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            payload_text = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            body = json.dumps({
                "model": self.config.deployment, "n": 1, "stream": False, "store": False,
                "max_completion_tokens": self.config.max_output_tokens,
                **({"reasoning_effort": self.config.reasoning_effort} if self.config.reasoning_effort is not None else {}),
                "response_format": response_format,
                "messages": [
                    {"role": "system", "content": (
                        system + "\nReturn exactly one JSON object matching this complete JSON schema. "
                        + guidance + "\n" + schema_text
                    )},
                    {"role": "user", "content": payload_text},
                ],
            }, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise ModelCompletionError("INPUT_INVALID", "Request must contain finite JSON data") from None
        if len(body) > self.config.max_request_bytes:
            raise ModelCompletionError("INPUT_TOO_LARGE", "Serialized request exceeds approved byte limit")
        return body, validator

    async def complete(self, system: str, payload: dict, schema: dict) -> str:
        invocation_started = perf_counter()
        http_started = None
        self._receipt.set(None)
        self._check_request_guard()
        self._authorize()
        body, validator = self._request(system, payload, schema)
        owned_fields = server_plan_fields(payload) if self.config.output_mode == "json_schema" else None
        try:
            async with asyncio.timeout(self.config.admission_timeout_seconds) as deadline:
                try:
                    token = await asyncio.wait_for(self._token_provider(), timeout=self.config.timeout_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._check_request_guard()
                    raise ModelCompletionError(
                        "TOKEN_FAILED", "Explicit token provider failed; no inference dispatched",
                    ) from None
                self._check_request_guard()
                if not isinstance(token, str) or not token or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is None:
                    raise ModelCompletionError("TOKEN_INVALID", "Explicit token provider returned an invalid bearer token")
                self._authorize()

                def admitted():
                    nonlocal http_started
                    loop = asyncio.get_running_loop()
                    expires = deadline.when()
                    if expires is None or loop.time() >= expires:
                        raise TimeoutError
                    http_started = perf_counter()
                    self._receipt.set(self.last_receipt.model_copy(update={
                        "admission_wait_ms": (http_started - invocation_started) * 1000,
                    }))
                    deadline.reschedule(loop.time() + self.config.timeout_seconds)

                split = self.config.admission_timeout_seconds is not None
                async with asyncio.timeout(None if split else self.config.timeout_seconds):
                    raw, http_status = await self._paced_dispatch(
                        body, token, on_dispatch=admitted if split else None,
                    )
            receipt = self.last_receipt
            if receipt is None:
                raise ModelCompletionError("LEDGER_INCONSISTENT", "No dispatched attempt is available")
            receipt = receipt.model_copy(update={
                "http_status": http_status, "response_hash": hashlib.sha256(raw).hexdigest(),
                **({"http_elapsed_ms": (perf_counter() - http_started) * 1000} if http_started is not None else {}),
            })
            self._receipt.set(receipt)
            result, receipt = self._parse(raw, receipt, validator, schema, owned_fields=owned_fields)
            receipt = receipt.model_copy(update={"outcome": "COMPLETE"})
        except asyncio.CancelledError:
            if self.last_receipt is not None:
                self._finish("CANCELLED_UNKNOWN", http_started=http_started)
            raise
        except (httpx.TimeoutException, TimeoutError):
            if self.last_receipt is not None:
                self._finish("TIMED_OUT_UNKNOWN", http_started=http_started)
            if self.config.admission_timeout_seconds is not None and http_started is None:
                raise ModelCompletionError(
                    "ADMISSION_TIMED_OUT", "Model admission timed out before HTTP; any reservation retained",
                    Status.TIMED_OUT,
                ) from None
            raise ModelCompletionError(
                "TIMED_OUT_UNKNOWN", "Inference timed out; any dispatched reservation retained", Status.TIMED_OUT,
            ) from None
        except httpx.HTTPError:
            if self.last_receipt is not None:
                self._finish("TRANSPORT_UNKNOWN", http_started=http_started)
            raise ModelCompletionError(
                "TRANSPORT_UNKNOWN", "Inference transport failed; any dispatched reservation retained",
            ) from None
        except ModelCompletionError as error:
            if self.last_receipt is not None:
                self._finish(error.code, http_started=http_started)
            raise
        else:
            self._receipt.set(receipt)
            self._finish("COMPLETE", http_started=http_started)
            return result

    def _finish(self, outcome: str, *, http_started: float | None = None) -> None:
        receipt = self.last_receipt
        if receipt is None:
            raise ModelCompletionError("LEDGER_INCONSISTENT", "No reserved attempt is available")
        receipt = receipt.model_copy(update={
            "outcome": outcome, "completed_at": datetime.now(timezone.utc),
            **({"http_elapsed_ms": (perf_counter() - http_started) * 1000}
               if http_started is not None and receipt.http_elapsed_ms is None else {}),
        })
        self._ledger.finish(receipt)
        self._receipt.set(receipt)

    async def _dispatch(
        self, body: bytes, token: str, *, on_dispatch: Callable[[], None] | None = None,
    ) -> tuple[bytes, int]:
        async with httpx.AsyncClient(
            transport=self._transport, trust_env=False, follow_redirects=False,
            timeout=self.config.timeout_seconds,
        ) as client:
            self._authorize()
            self._check_request_guard()
            with self._ledger.reserve(hashlib.sha256(body).hexdigest()) as receipt:
                # Roll back a revoked reservation; commit before send with no intervening await.
                self._check_request_guard()
            self._receipt.set(receipt)
            if on_dispatch is not None:
                on_dispatch()
            async with client.stream(
                "POST", self.config.endpoint + "/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                    "Accept": "application/json", "Accept-Encoding": "identity",
                }, content=body,
            ) as response:
                receipt = self.last_receipt.model_copy(update={"http_status": response.status_code})
                retry_after = response.headers.get("retry-after")
                if response.status_code == 429 and retry_after is not None and re.fullmatch(r"[0-9]{1,5}", retry_after):
                    seconds = int(retry_after)
                    if seconds <= 86400:
                        receipt = receipt.model_copy(update={"retry_after_seconds": seconds})
                        if self.config.min_request_interval_seconds:
                            self._next_dispatch = max(self._next_dispatch, perf_counter() + seconds)
                self._receipt.set(receipt)
                if response.status_code != 200:
                    raise ModelCompletionError("HTTP_ERROR", f"Inference returned HTTP {response.status_code}; no retry")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ModelCompletionError("RESPONSE_ENCODING", "Compressed model responses are not accepted")
                if response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                    raise ModelCompletionError("RESPONSE_TYPE", "Expected one JSON chat completion")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.config.max_response_bytes:
                        raise ModelCompletionError("RESPONSE_TOO_LARGE", "Response exceeds approved byte limit")
                    chunks.append(chunk)
                return b"".join(chunks), response.status_code

    async def _paced_dispatch(
        self, body: bytes, token: str, *, on_dispatch: Callable[[], None] | None = None,
    ) -> tuple[bytes, int]:
        if not self.config.min_request_interval_seconds:
            return await self._dispatch(body, token, on_dispatch=on_dispatch)
        await self._dispatch_gate.acquire()
        released = False

        def admitted():
            nonlocal released
            if on_dispatch is not None:
                on_dispatch()
            self._next_dispatch = perf_counter() + self.config.min_request_interval_seconds
            released = True
            self._dispatch_gate.release()

        try:
            while (delay := self._next_dispatch - perf_counter()) > 0:
                await asyncio.sleep(delay)
            self._authorize()
            self._check_request_guard()
            return await self._dispatch(body, token, on_dispatch=admitted)
        finally:
            if not released:
                self._dispatch_gate.release()

    def _parse(
        self, raw: bytes, receipt: ModelRequestReceipt, validator: Draft202012Validator, schema: dict,
        *, owned_fields: dict | None = None,
    ) -> tuple[str, ModelRequestReceipt]:
        try:
            envelope = _strict_json(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ModelCompletionError("RESPONSE_INVALID", "Malformed JSON completion envelope") from None
        if not isinstance(envelope, dict) or envelope.get("object") != "chat.completion":
            raise ModelCompletionError("RESPONSE_INVALID", "Expected a nonstreamed chat completion object")
        if envelope.get("model") != self.model_version:
            raise ModelCompletionError("MODEL_MISMATCH", "Response model does not match approved pinned version")
        choices = envelope.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ModelCompletionError("RESPONSE_INVALID", "Expected exactly one completion choice")
        choice = choices[0]
        if type(choice.get("index")) is not int or choice["index"] != 0:
            raise ModelCompletionError("RESPONSE_INVALID", "Invalid completion choice index")
        usage_raw = envelope.get("usage")
        try:
            if not isinstance(usage_raw, dict):
                raise ValueError("Usage missing")
            usage = ModelUsage.model_validate({
                key: usage_raw[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            })
        except (KeyError, ValueError, ValidationError):
            raise ModelCompletionError("USAGE_INVALID", "Missing or inconsistent usage; reservation retained") from None
        if usage.prompt_tokens > self.config.max_input_tokens or usage.completion_tokens > self.config.max_output_tokens:
            self._receipt.set(receipt.model_copy(update={
                "usage": usage,
                "charged_cost": max(
                    receipt.reserved_cost, self.config.charge(usage.prompt_tokens, usage.completion_tokens),
                ),
            }))
            raise ModelCompletionError("USAGE_EXCEEDS_BOUND", "Service usage exceeds reviewed token ceiling; stop inference")
        receipt = receipt.model_copy(update={
            "usage": usage, "charged_cost": self.config.charge(usage.prompt_tokens, usage.completion_tokens),
            "cost_state": "RECONCILED",
        })
        finish = choice.get("finish_reason")
        if not isinstance(finish, str):
            self._receipt.set(receipt)
            raise ModelCompletionError("FINISH_INVALID", "Missing or malformed finish reason")
        if finish in ("stop", "length", "content_filter", "tool_calls", "function_call"):
            receipt = receipt.model_copy(update={"finish_reason": finish})
        self._receipt.set(receipt)
        if finish != "stop":
            code = {"length": "OUTPUT_INCOMPLETE", "content_filter": "CONTENT_FILTERED"}.get(finish, "FINISH_INVALID")
            raise ModelCompletionError(code, "Completion did not stop normally; no partial JSON or paid repair")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ModelCompletionError("RESPONSE_INVALID", "Expected one assistant message")
        if message.get("refusal") is not None:
            raise ModelCompletionError("MODEL_REFUSED", "Model refused the request; no automatic retry")
        if message.get("tool_calls") or message.get("function_call") or message.get("audio"):
            raise ModelCompletionError("RESPONSE_INVALID", "Only text JSON output is allowed")
        text = message.get("content")
        if not isinstance(text, str):
            raise ModelCompletionError("OUTPUT_INVALID", "Model did not return JSON text")
        try:
            if len(text.encode("utf-8")) > self.config.max_output_bytes:
                raise ModelCompletionError("OUTPUT_TOO_LARGE", "Model output exceeds approved byte limit")
            parsed = _strict_json(text)
            if not isinstance(parsed, dict):
                raise ValueError("Expected JSON object")
            if self.config.output_mode == "json_schema":
                if owned_fields is None:
                    raise ValueError("Strict plan decoding requires original server metadata")
                _schema_validator(strict_plan_schema()).validate(parsed)
                parsed = decode_strict_plan(parsed, owned_fields)
            if schema == Plan.model_json_schema():
                Plan.model_validate(parsed)
            validator.validate(parsed)
        except ValidationError as error:
            details = [
                {"kind": "typed_plan", "path": _safe_validation_path(item["loc"]), "code": item["type"]}
                for item in error.errors(include_input=False, include_context=False, include_url=False)[:5]
            ]
            raise ModelCompletionError(
                "OUTPUT_INVALID", "Model JSON failed supplied schema/typed validation; no paid repair",
                validation_details=details,
            ) from None
        except SchemaValidationError as error:
            raise ModelCompletionError(
                "OUTPUT_INVALID", "Model JSON failed supplied schema/typed validation; no paid repair",
                validation_details=[{"kind": "json_schema", "path": _safe_validation_path(error.absolute_path),
                                     "code": error.validator}],
            ) from None
        except (ValueError, UnicodeError, RecursionError, Unresolvable):
            raise ModelCompletionError(
                "OUTPUT_INVALID", "Model JSON failed supplied schema/typed validation; no paid repair",
                validation_details=[{"kind": "json_or_wire", "path": [], "code": "invalid_encoding"}],
            ) from None
        return (
            json.dumps(parsed, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            if self.config.output_mode == "json_schema" else text
        ), receipt


def _safe_validation_path(path) -> list[str | int]:
    schema = Plan.model_json_schema()
    allowed = set(schema["properties"]) | {
        key for definition in schema["$defs"].values() for key in definition.get("properties", {})
    } | {
        "QuerySemanticModel", "ProjectColumns", "JoinResults", "ResolveIdentity", "SemiJoinCohort",
        "AggregateResult", "ApplyDeterministicRule", "ComparePeriods", "RankCandidates",
        "ResolveConcept", "SelectMetric", "ReadModelSchema", "SynthesizeAnswer",
    }
    return [
        item if type(item) is int or isinstance(item, str) and item in allowed else "<key>"
        for item in list(path)[:8]
    ]
