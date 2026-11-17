"""Evaluation arm protocol, deterministic scripted providers, and live MCP agent arms (M2-07, M4-02).

Provides one typed interface (``ArmProvider`` / ``ArmRunRecord``) shared by
scripted CI arms and live MCP arms. Scripted offline arms run against local
synthetic Parquet fixtures so CI exercises the harness and scorecard end-to-end
without a tenant or LLM deployment. Live arms require an explicit private
approval file outside the repository root and run an OpenAI Responses API
function-calling loop bridged to Fabric IQ MCP and Ontology MCP endpoints.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse
from uuid import UUID

import httpx

from eval.oracle.service_assurance import (
    VALID_MODELS,
    VALID_TRAPS,
    evaluate_question,
    evaluate_trap,
    validate_question_record,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "fabric" / "ontology" / "service-assurance.json"
SHARED_INSTRUCTIONS_PATH = ROOT / "agents" / "shared" / "instructions.md"

LIVE_OPERATION_NAME = "northstar-m4-live-eval"
DEFAULT_IQ_MCP_URL = "https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq"
ONTOLOGY_MCP_URL_TEMPLATE = (
    "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/{workspace_id}"
    "/items/{ontology_item_id}/ontologyEndpoint"
)
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
MAX_TOOL_TEXT_CHARS = 48_000

ArmName = Literal["baseline", "baseline_plus_rules", "ontology"]
RunStatus = Literal["COMPLETE", "PARTIAL", "FAILED"]
VALID_ARMS: tuple[ArmName, ...] = ("baseline", "baseline_plus_rules", "ontology")


@dataclass(frozen=True)
class ToolCallRecord:
    """Single MCP tool invocation recorded during an arm run."""

    step: int
    server: Literal["fabric_iq", "ontology_mcp"]
    tool: str
    target_model: str | None
    summary: str
    elapsed_ms: float


@dataclass(frozen=True)
class PathRecord:
    """Graph traversal path, cross-model bridges, validity checks, and rules applied."""

    models_queried: list[str]
    entities_traversed: list[str]
    bridges_used: list[dict[str, Any]]
    validity_checks: list[str]
    rules_applied: list[str]


@dataclass(frozen=True)
class EffortRecord:
    """Effort counters for an evaluation arm run."""

    tool_call_count: int
    input_tokens: int
    output_tokens: int
    elapsed_ms: float


@dataclass(frozen=True)
class ArmRunRecord:
    """Structured output of running one arm on one golden question."""

    question_id: str
    level: str
    persona: str
    arm: ArmName
    repeat: int
    status: RunStatus
    rows: list[dict[str, Any]]
    exclusions: list[dict[str, Any]]
    path: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    effort: dict[str, Any]
    model_deployment: str
    toolset_variant: str
    started_at: str
    completed_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArmProvider(Protocol):
    """Protocol implemented by both offline scripted arms and live MCP arms."""

    arm: ArmName
    mode: str

    def run_question(
        self,
        question: dict[str, Any],
        data_dir: Path,
        *,
        repeat: int = 1,
    ) -> ArmRunRecord:
        """Execute a question against ``data_dir`` and return a full ``ArmRunRecord``."""
        ...


def _load_contract(contract_path: Path = CONTRACT_PATH) -> dict[str, Any]:
    return json.loads(contract_path.read_text(encoding="utf-8"))


def _derive_ontology_path(
    question: dict[str, Any],
    contract: dict[str, Any],
) -> PathRecord:
    models = list(question["models"])
    model_set = set(models)
    params = question["parameters"]
    col_names = {col["name"] for col in question["answer"]["columns"]}
    keys = question["answer"]["keys"]

    entities: list[str] = []
    if len(models) == 1:
        only = models[0]
        if only == "network":
            entities.append("network_Incidents" if keys == ["market_id"] else "network_SiteHour")
        elif only == "enterprise":
            entities.append("enterprise_AppliedCredits")
        elif only == "finance":
            entities.append("finance_AccountMonth")
        elif only == "care":
            entities.append(
                "care_CaseDailySnapshot" if "open_cases_eod" in col_names else "care_Cases"
            )
    else:
        if "network" in model_set:
            entities.extend(["network_Incidents", "network_Sites"])
        if "enterprise" in model_set:
            if "network" in model_set:
                entities.extend(["enterprise_Sites", "enterprise_ServiceSiteMap"])
            if keys == ["service_key"]:
                entities.append("enterprise_Services")
            entities.append("enterprise_Contracts")
            if col_names & {"credit_exposure_cents", "eligible_credits_cents"}:
                entities.append("enterprise_SlaEvaluation")
            if "applied_credits_cents" in col_names:
                entities.append("enterprise_AppliedCredits")
            if "breached_services" in col_names:
                entities.append("enterprise_ServiceMonth")
            if keys == ["account_key"] or len(model_set) > 2 or model_set & {"finance", "care"}:
                entities.append("enterprise_Accounts")
        if "finance" in model_set:
            entities.extend(["finance_Accounts", "finance_AccountMonth"])
        if "care" in model_set:
            entities.append("care_Accounts")
            if col_names & {"escalated_cases", "repeat_contacts"}:
                entities.append("care_Cases")
            if "open_cases_eod" in col_names:
                entities.append("care_CaseDailySnapshot")

    entity_set = set(entities)
    bridges: list[dict[str, Any]] = []
    for rel in contract.get("relationships", []):
        if rel["from"] in entity_set and rel["to"] in entity_set:
            entry: dict[str, Any] = {
                "from": rel["from"],
                "to": rel["to"],
                "on": list(rel["on"]),
                "crossesModels": bool(rel.get("crossesModels", False)),
            }
            if "hop" in rel:
                entry["hop"] = int(rel["hop"])
            if "validity" in rel:
                entry["validity"] = rel["validity"]
            bridges.append(entry)

    validity_checks: list[str] = []
    if "R1" in question["rules"]:
        validity_checks.append("incident_assignment_contract_utc_overlap")
    if params.get("use_billing_map"):
        validity_checks.append("billing_account_map_utc_validity")
    if params.get("snapshot_date"):
        validity_checks.append("single_snapshot_date_eod")

    return PathRecord(
        models_queried=models,
        entities_traversed=entities,
        bridges_used=bridges,
        validity_checks=validity_checks,
        rules_applied=list(question["rules"]),
    )


def _select_baseline_trap(question: dict[str, Any]) -> str | None:
    """Select the deterministic schema-only failure mode for a multi-hop baseline question.

    Single-model L1 control questions return ``None`` (both arms match on L1).
    Multi-model questions select a deterministic trap from ``question['traps']`` so
    CI exercises all five documented failure modes across L2-L4, N, and P.
    """
    if len(question["models"]) == 1:
        return None
    traps: list[str] = list(question["traps"])
    if not traps:
        return None
    qid = question["id"]
    preferred_by_id: dict[str, str] = {
        "SA-L2-001": "site-key-only",
        "SA-L2-002": "credit-per-incident",
        "SA-L2-003": "revenue-fan-out",
        "SA-L2-004": "display-name-or-billing-key",
        "SA-L2-005": "display-name-or-billing-key",
        "SA-L3-001": "revenue-fan-out",
        "SA-L3-002": "site-key-only",
        "SA-L3-003": "credit-per-incident",
        "SA-L3-004": "site-key-only",
        "SA-L3-005": "display-name-or-billing-key",
        "SA-L4-001": "revenue-fan-out",
        "SA-L4-002": "credit-per-incident",
        "SA-L4-003": "display-name-or-billing-key",
        "SA-L4-004": "site-key-only",
        "SA-N-001": "missing-as-zero",
        "SA-N-002": "missing-as-zero",
        "SA-N-003": "missing-as-zero",
        "SA-N-004": "missing-as-zero",
        "SA-P-001": "credit-per-incident",
        "SA-P-002": "site-key-only",
        "SA-P-003": "missing-as-zero",
    }
    chosen = preferred_by_id.get(qid, traps[0])
    if chosen not in traps or chosen not in VALID_TRAPS:
        return traps[0]
    return chosen


class ScriptedOntologyArm:
    """Offline scripted ontology-grounded arm following service-assurance.json and R1-R5."""

    arm: ArmName = "ontology"
    mode: str = "OFFLINE_SCRIPTED"

    def __init__(self, contract_path: Path = CONTRACT_PATH) -> None:
        self._contract = _load_contract(contract_path)

    def run_question(
        self,
        question: dict[str, Any],
        data_dir: Path,
        *,
        repeat: int = 1,
    ) -> ArmRunRecord:
        validate_question_record(question)
        started_wall = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()

        path_record = _derive_ontology_path(question, self._contract)
        result = evaluate_question(question, data_dir)

        elapsed_total = round((time.perf_counter() - t0) * 1000.0, 3)
        models = list(question["models"])
        step_count = (1 if len(models) == 1 else 2) + len(models)
        per_step_ms = round(elapsed_total / max(1, step_count), 3)

        calls: list[ToolCallRecord] = [
            ToolCallRecord(
                step=1,
                server="ontology_mcp",
                tool="list_ontology_entities",
                target_model=None,
                summary=f"Inspect {len(path_record.entities_traversed)} entities and bridges",
                elapsed_ms=per_step_ms,
            )
        ]
        next_step = 2
        if len(models) > 1 or question["rules"]:
            calls.append(
                ToolCallRecord(
                    step=next_step,
                    server="ontology_mcp",
                    tool="list_ontology_rules",
                    target_model=None,
                    summary=f"Load rules {', '.join(path_record.rules_applied) or 'none'}",
                    elapsed_ms=per_step_ms,
                )
            )
            next_step += 1

        for model_id in models:
            calls.append(
                ToolCallRecord(
                    step=next_step,
                    server="fabric_iq",
                    tool="ExecuteQuery",
                    target_model=model_id,
                    summary=f"Execute grounded query on {model_id} ({question['persona']})",
                    elapsed_ms=per_step_ms,
                )
            )
            next_step += 1

        effort = EffortRecord(
            tool_call_count=len(calls),
            input_tokens=320 + 180 * len(models) + 60 * len(path_record.rules_applied),
            output_tokens=110 + 45 * len(models),
            elapsed_ms=elapsed_total,
        )
        completed_wall = datetime.now(timezone.utc).isoformat()
        status: RunStatus = "PARTIAL" if result["exclusions"] else "COMPLETE"
        return ArmRunRecord(
            question_id=question["id"],
            level=question["level"],
            persona=question["persona"],
            arm=self.arm,
            repeat=repeat,
            status=status,
            rows=result["rows"],
            exclusions=result["exclusions"],
            path=asdict(path_record),
            tool_calls=[asdict(c) for c in calls],
            effort=asdict(effort),
            model_deployment="scripted-offline-fixture",
            toolset_variant="offline-local-v1",
            started_at=started_wall,
            completed_at=completed_wall,
        )


class ScriptedBaselineArm:
    """Offline scripted semantic-models-only arm without ontology bridges or rules."""

    arm: ArmName = "baseline"
    mode: str = "OFFLINE_SCRIPTED"

    def run_question(
        self,
        question: dict[str, Any],
        data_dir: Path,
        *,
        repeat: int = 1,
    ) -> ArmRunRecord:
        validate_question_record(question)
        started_wall = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()

        models = list(question["models"])
        trap_hit = _select_baseline_trap(question)
        if trap_hit is None:
            result = evaluate_question(question, data_dir)
        else:
            result = evaluate_trap(trap_hit, question, data_dir)

        elapsed_total = round((time.perf_counter() - t0) * 1000.0, 3)
        calls: list[ToolCallRecord] = []
        step = 1
        per_step_ms = round(elapsed_total / max(1, len(models) * 2), 3)
        for model_id in models:
            calls.append(
                ToolCallRecord(
                    step=step,
                    server="fabric_iq",
                    tool="GetSemanticModelSchema",
                    target_model=model_id,
                    summary=f"Inspect individual {model_id} model tables and measures",
                    elapsed_ms=per_step_ms,
                )
            )
            step += 1
            calls.append(
                ToolCallRecord(
                    step=step,
                    server="fabric_iq",
                    tool="ExecuteQuery",
                    target_model=model_id,
                    summary=f"Query {model_id} without cross-model ontology contract ({question['persona']})",
                    elapsed_ms=per_step_ms,
                )
            )
            step += 1

        validity_checks = (
            ["single_snapshot_date_eod"]
            if len(models) == 1 and question["parameters"].get("snapshot_date")
            else []
        )
        rules_applied = (
            list(question["rules"]) if len(models) == 1 else []
        )
        path_record = PathRecord(
            models_queried=models,
            entities_traversed=[],
            bridges_used=[],
            validity_checks=validity_checks,
            rules_applied=rules_applied,
        )
        effort = EffortRecord(
            tool_call_count=len(calls),
            input_tokens=420 * len(models),
            output_tokens=140 * len(models),
            elapsed_ms=elapsed_total,
        )
        completed_wall = datetime.now(timezone.utc).isoformat()
        status: RunStatus = "PARTIAL" if result["exclusions"] else "COMPLETE"
        return ArmRunRecord(
            question_id=question["id"],
            level=question["level"],
            persona=question["persona"],
            arm=self.arm,
            repeat=repeat,
            status=status,
            rows=result["rows"],
            exclusions=result["exclusions"],
            path=asdict(path_record),
            tool_calls=[asdict(c) for c in calls],
            effort=asdict(effort),
            model_deployment="scripted-offline-fixture",
            toolset_variant="offline-local-v1",
            started_at=started_wall,
            completed_at=completed_wall,
        )


def _select_baseline_plus_rules_trap(question: dict[str, Any]) -> str | None:
    """Select residual schema-bridge misses when rules are in prompt without ontology relationships."""
    residual_by_id: dict[str, str] = {
        "SA-L2-004": "display-name-or-billing-key",
        "SA-P-002": "site-key-only",
    }
    chosen = residual_by_id.get(question["id"])
    if chosen is not None and chosen in question["traps"]:
        return chosen
    return None


class ScriptedBaselinePlusRulesArm(ScriptedOntologyArm):
    """Offline scripted semantic-models-plus-pasted-rules arm (Arm A+, M4-02)."""

    arm: ArmName = "baseline_plus_rules"
    mode: str = "OFFLINE_SCRIPTED"

    def run_question(
        self,
        question: dict[str, Any],
        data_dir: Path,
        *,
        repeat: int = 1,
    ) -> ArmRunRecord:
        validate_question_record(question)
        started_wall = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()

        models = list(question["models"])
        trap_hit = _select_baseline_plus_rules_trap(question)
        if trap_hit is None:
            result = evaluate_question(question, data_dir)
            derived = _derive_ontology_path(question, self._contract)
            inferred_bridges = [
                {
                    "from": b["from"],
                    "to": b["to"],
                    "on": b["on"],
                    "crossesModels": b["crossesModels"],
                }
                for b in derived.bridges_used
                if b.get("crossesModels") or "validity" in b
            ]
            if len(models) >= 2 and not inferred_bridges:
                inferred_bridges = [
                    {
                        "from": models[0],
                        "to": models[1],
                        "on": list(question["answer"]["keys"][:1]),
                        "crossesModels": True,
                    }
                ]
            path_record = PathRecord(
                models_queried=models,
                entities_traversed=[],
                bridges_used=inferred_bridges,
                validity_checks=list(derived.validity_checks),
                rules_applied=list(question["rules"]),
            )
        else:
            result = evaluate_trap(trap_hit, question, data_dir)
            path_record = PathRecord(
                models_queried=models,
                entities_traversed=[],
                bridges_used=[],
                validity_checks=[],
                rules_applied=list(question["rules"]),
            )

        elapsed_total = round((time.perf_counter() - t0) * 1000.0, 3)
        calls: list[dict[str, Any]] = []
        step = 1
        per_step = round(elapsed_total / max(1, len(models) * 2), 3)
        for model_id in models:
            calls.append(
                asdict(
                    ToolCallRecord(
                        step=step,
                        server="fabric_iq",
                        tool="GetSemanticModelSchema",
                        target_model=model_id,
                        summary=f"Inspect {model_id} schema with rules R1-R5 in prompt",
                        elapsed_ms=per_step,
                    )
                )
            )
            step += 1
            calls.append(
                asdict(
                    ToolCallRecord(
                        step=step,
                        server="fabric_iq",
                        tool="ExecuteQuery",
                        target_model=model_id,
                        summary=f"Execute rule-prompted query on {model_id} ({question['persona']})",
                        elapsed_ms=per_step,
                    )
                )
            )
            step += 1

        effort = EffortRecord(
            tool_call_count=len(calls),
            input_tokens=420 * len(models) + 950 * len(calls),
            output_tokens=150 * len(models),
            elapsed_ms=elapsed_total,
        )
        completed_wall = datetime.now(timezone.utc).isoformat()
        status: RunStatus = "PARTIAL" if result["exclusions"] else "COMPLETE"
        return ArmRunRecord(
            question_id=question["id"],
            level=question["level"],
            persona=question["persona"],
            arm=self.arm,
            repeat=repeat,
            status=status,
            rows=result["rows"],
            exclusions=result["exclusions"],
            path=asdict(path_record),
            tool_calls=calls,
            effort=asdict(effort),
            model_deployment="scripted-offline-fixture",
            toolset_variant="offline-local-v1",
            started_at=started_wall,
            completed_at=completed_wall,
        )


def get_offline_arm(arm: ArmName, *, contract_path: Path = CONTRACT_PATH) -> ArmProvider:
    """Return the offline scripted provider for ``arm``."""
    if arm == "baseline":
        return ScriptedBaselineArm()
    if arm == "ontology":
        return ScriptedOntologyArm(contract_path=contract_path)
    if arm == "baseline_plus_rules":
        return ScriptedBaselinePlusRulesArm(contract_path=contract_path)
    raise ValueError(f"Unsupported offline arm: {arm!r}")


class LiveApprovalError(ValueError):
    """Raised when live evaluation approval is absent, inside the repository, or invalid."""


def _parse_non_zero_uuid(raw: Any, field_name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise LiveApprovalError(f"{field_name} must be a non-empty UUID string")
    try:
        parsed = UUID(raw.strip())
    except ValueError as exc:
        raise LiveApprovalError(f"{field_name} is not a valid UUID") from exc
    normalized = str(parsed)
    if normalized == ZERO_GUID:
        raise LiveApprovalError(f"{field_name} cannot be the zero placeholder UUID")
    return normalized


def _parse_utc_timestamp(raw: Any, field_name: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise LiveApprovalError(f"{field_name} must be an ISO-8601 UTC string")
    candidate = raw.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise LiveApprovalError(f"{field_name} is not a valid ISO-8601 timestamp") from exc
    if dt.tzinfo is None:
        raise LiveApprovalError(f"{field_name} must include a UTC timezone offset")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class LiveEvalApproval:
    """Validated private approval contract for M4-02 / M4-05 live evaluation runs."""

    approved: bool
    operation: str
    expires_at: datetime
    max_spend_usd: float
    max_seconds: float
    workspace_id: str
    ontology_item_id: str
    semantic_models: dict[str, str]
    foundry_responses_url: str
    model_deployment: str
    input_cost_per_1m_usd: float
    output_cost_per_1m_usd: float
    toolset_variant: str = "service_default"
    iq_mcp_url: str = DEFAULT_IQ_MCP_URL
    ontology_mcp_url: str = ""
    tenant_id: str | None = None
    subscription_id: str | None = None
    capacity_sku: str | None = None
    region: str | None = None
    max_turns: int = 12
    reasoning_effort: str = "low"
    iq_token_resource: str = "https://analysis.windows.net/powerbi/api"
    ontology_token_resource: str = "https://api.fabric.microsoft.com"
    foundry_token_resource: str = "https://cognitiveservices.azure.com"

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        root: Path = ROOT,
        now: datetime | None = None,
    ) -> LiveEvalApproval:
        resolved = path.resolve()
        root_resolved = root.resolve()
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            raise LiveApprovalError(
                "Live approval file must reside outside the repository root"
            )
        if not resolved.is_file():
            raise LiveApprovalError(f"Live approval file does not exist: {resolved.name}")
        try:
            data = json.loads(resolved.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise LiveApprovalError("Live approval file is not valid JSON") from exc
        if not isinstance(data, dict):
            raise LiveApprovalError("Live approval payload must be a JSON object")

        if data.get("approved") is not True:
            raise LiveApprovalError("Live evaluation rejected: 'approved' must be boolean true")
        operation = data.get("operation")
        if operation != LIVE_OPERATION_NAME:
            raise LiveApprovalError(
                f"Live evaluation rejected: expected operation {LIVE_OPERATION_NAME!r}"
            )

        current_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
        expires_at = _parse_utc_timestamp(data.get("expires_at"), "expires_at")
        if expires_at <= current_utc:
            raise LiveApprovalError("Live evaluation rejected: approval has expired")

        max_spend_raw = data.get("max_spend_usd")
        max_seconds_raw = data.get("max_seconds")
        if not isinstance(max_spend_raw, (int, float)) or isinstance(max_spend_raw, bool):
            raise LiveApprovalError("max_spend_usd must be a positive number")
        if not isinstance(max_seconds_raw, (int, float)) or isinstance(max_seconds_raw, bool):
            raise LiveApprovalError("max_seconds must be a positive number")
        max_spend_usd = float(max_spend_raw)
        max_seconds = float(max_seconds_raw)
        if max_spend_usd <= 0.0 or max_seconds <= 0.0:
            raise LiveApprovalError("max_spend_usd and max_seconds must be strictly positive")

        workspace_id = _parse_non_zero_uuid(data.get("workspace_id"), "workspace_id")
        ontology_item_id = _parse_non_zero_uuid(data.get("ontology_item_id"), "ontology_item_id")

        raw_models = data.get("semantic_models")
        if not isinstance(raw_models, dict) or set(raw_models) != set(VALID_MODELS):
            raise LiveApprovalError(
                f"semantic_models must map all four models: {list(VALID_MODELS)}"
            )
        semantic_models = {
            model_name: _parse_non_zero_uuid(raw_models[model_name], f"semantic_models.{model_name}")
            for model_name in VALID_MODELS
        }
        if len(set(semantic_models.values())) != len(semantic_models):
            raise LiveApprovalError("semantic_models UUIDs must be distinct")

        responses_url = data.get("foundry_responses_url")
        if not isinstance(responses_url, str) or not responses_url.strip():
            raise LiveApprovalError("foundry_responses_url must be a non-empty HTTPS URL")
        parsed_url = urlparse(responses_url.strip())
        if parsed_url.scheme != "https" or not parsed_url.netloc or "/responses" not in parsed_url.path:
            raise LiveApprovalError(
                "foundry_responses_url must be an HTTPS OpenAI Responses API endpoint"
            )

        deployment = data.get("model_deployment")
        if not isinstance(deployment, str) or not deployment.strip():
            raise LiveApprovalError("model_deployment must be a non-empty string")

        in_cost = data.get("input_cost_per_1m_usd")
        out_cost = data.get("output_cost_per_1m_usd")
        if (
            not isinstance(in_cost, (int, float))
            or isinstance(in_cost, bool)
            or float(in_cost) < 0.0
            or not isinstance(out_cost, (int, float))
            or isinstance(out_cost, bool)
            or float(out_cost) < 0.0
        ):
            raise LiveApprovalError("Token cost rates per 1M tokens must be non-negative numbers")

        variant = data.get("toolset_variant", "service_default")
        if not isinstance(variant, str) or not variant.strip():
            raise LiveApprovalError("toolset_variant must be a non-empty string")

        iq_url = str(data.get("iq_mcp_url", DEFAULT_IQ_MCP_URL)).strip()
        ont_url = str(
            data.get("ontology_mcp_url")
            or ONTOLOGY_MCP_URL_TEMPLATE.format(
                workspace_id=workspace_id, ontology_item_id=ontology_item_id
            )
        ).strip()
        max_turns_raw = data.get("max_turns", 12)
        max_turns = int(max_turns_raw) if isinstance(max_turns_raw, int) and max_turns_raw >= 1 else 12
        reasoning_effort = str(data.get("reasoning_effort", "low")).strip() or "low"
        tenant_id_raw = data.get("tenant_id")
        tenant_id = (
            _parse_non_zero_uuid(tenant_id_raw, "tenant_id")
            if tenant_id_raw is not None
            else None
        )
        subscription_id_raw = data.get("subscription_id")
        subscription_id = (
            _parse_non_zero_uuid(subscription_id_raw, "subscription_id")
            if subscription_id_raw is not None
            else None
        )

        return cls(
            approved=True,
            operation=LIVE_OPERATION_NAME,
            expires_at=expires_at,
            max_spend_usd=max_spend_usd,
            max_seconds=max_seconds,
            workspace_id=workspace_id,
            ontology_item_id=ontology_item_id,
            semantic_models=semantic_models,
            foundry_responses_url=responses_url.strip(),
            model_deployment=deployment.strip(),
            input_cost_per_1m_usd=float(in_cost),
            output_cost_per_1m_usd=float(out_cost),
            toolset_variant=variant.strip(),
            iq_mcp_url=iq_url,
            ontology_mcp_url=ont_url,
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            capacity_sku=(
                str(data["capacity_sku"]).strip() if data.get("capacity_sku") else None
            ),
            region=str(data["region"]).strip() if data.get("region") else None,
            max_turns=max_turns,
            reasoning_effort=reasoning_effort,
            iq_token_resource=str(
                data.get("iq_token_resource", "https://analysis.windows.net/powerbi/api")
            ).strip(),
            ontology_token_resource=str(
                data.get("ontology_token_resource", "https://api.fabric.microsoft.com")
            ).strip(),
            foundry_token_resource=str(
                data.get("foundry_token_resource", "https://cognitiveservices.azure.com")
            ).strip(),
        )

    def cost_for_tokens(self, input_tokens: int, output_tokens: int) -> float:
        return (
            (max(0, input_tokens) / 1_000_000.0) * self.input_cost_per_1m_usd
            + (max(0, output_tokens) / 1_000_000.0) * self.output_cost_per_1m_usd
        )


def _format_output_contract(question: dict[str, Any]) -> str:
    keys = question["answer"]["keys"]
    cols = [
        {"name": c["name"], "metric": c["metric"], "type": c["type"]}
        for c in question["answer"]["columns"]
    ]
    order_by = question["answer"]["orderBy"]
    persona = question["persona"]
    persona_line = (
        'Persona filter: persona is "east_region". Filter every queried semantic model '
        'to East Region (region = "East" / market_id = "M01").'
        if persona == "east_region"
        else 'Persona filter: persona is "default" (all markets).'
    )
    return (
        f"{persona_line}\n"
        "Return ONLY a single valid JSON object with these exact top-level keys:\n"
        "- \"rows\": array of objects, each containing exact key fields "
        f"{json.dumps(keys)} and exact integer value fields "
        f"{json.dumps([c['name'] for c in cols])} (corresponding to metrics "
        f"{json.dumps(cols)}), ordered by {json.dumps(order_by)}. "
        "All cent and count columns MUST be exact JSON integers, never strings or floats.\n"
        "- \"exclusions\": array of objects describing any excluded or unknown items "
        "(e.g. {\"reason\": \"...\", ...}), or [] when none apply.\n"
        "- \"bridges_used\": array of objects {\"from\": str, \"to\": str, \"on\": [str], "
        "\"crossesModels\": bool} describing cross-table or cross-model joins used.\n"
        "- \"validity_checks\": array of strings naming time-validity or snapshot checks applied.\n"
        "- \"rules_applied\": array of rule IDs or names applied."
    )


def build_live_system_instructions(
    arm: ArmName,
    question: dict[str, Any],
    *,
    contract_path: Path = CONTRACT_PATH,
    shared_instructions_path: Path = SHARED_INSTRUCTIONS_PATH,
) -> str:
    """Construct the arm-specific system prompt for a live evaluation run."""
    output_spec = _format_output_contract(question)
    shared_mechanics = (
        "Execution mechanics (identical across all evaluation arms):\n"
        "- Four Power BI semantic models are available: network, enterprise, finance, and care.\n"
        "- Each execute_dax_query(model, dax_query) call runs on one semantic model (up to 500 rows).\n"
        "- When a question spans multiple models or tables without an active single-query relationship, "
        "execute read-only DAX queries on each relevant model to fetch the required keys, timestamps "
        "or dates, and source-owned measures, then join and filter the returned rows across models to "
        "assemble the final answer table.\n"
        "- Do not invent numbers; retrieve numeric metrics from their semantic-model measures."
    )
    if arm in ("baseline", "baseline_plus_rules"):
        base = (
            "You are a quantitative telecom data analyst.\n"
            "Use get_semantic_model_schema(model) to inspect tables, columns, relationships, "
            "and measures before writing DAX, and use execute_dax_query(model, dax_query) "
            "to evaluate DAX queries.\n\n"
            f"{shared_mechanics}"
        )
        if arm == "baseline":
            return f"{base}\n\n{output_spec}"

        contract = _load_contract(contract_path)
        rules_lines = ["Apply all five domain rules verbatim when planning and joining queries:"]
        for rule in contract.get("rules", []):
            rules_lines.append(f"- {rule['id']} ({rule['name']}): {rule['statement']}")
        rules_block = "\n".join(rules_lines)
        return f"{base}\n\n{rules_block}\n\n{output_spec}"

    if arm == "ontology":
        shared_text = shared_instructions_path.read_text(encoding="utf-8").strip()
        ont_workflow = (
            "Workflow requirement:\n"
            "1. Call list_ontology_entities, list_ontology_rules, and "
            "get_ontology_relationships_and_metrics first before querying semantic models.\n"
            "2. Use get_semantic_model_schema(model) if needed and execute source DAX measures "
            "via execute_dax_query(model, dax_query) on network, enterprise, finance, and care."
        )
        return f"{shared_text}\n\n{shared_mechanics}\n\n{ont_workflow}\n\n{output_spec}"

    raise ValueError(f"Unsupported arm: {arm!r}")


def build_live_tools(arm: ArmName) -> list[dict[str, Any]]:
    """Return OpenAI Responses API function tool schemas permitted for ``arm``."""
    model_enum = list(VALID_MODELS)
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": "get_semantic_model_schema",
            "description": (
                "Fetch tables, columns, active relationships, and DAX measures for one "
                "Power BI semantic model through Fabric IQ MCP."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "enum": model_enum,
                        "description": "Semantic model alias to inspect.",
                    }
                },
                "required": ["model"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "execute_dax_query",
            "description": (
                "Execute a read-only DAX EVALUATE query against one Power BI semantic model "
                "through Fabric IQ MCP and return rows, columns, or DAX error text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "enum": model_enum,
                        "description": "Target semantic model alias.",
                    },
                    "dax_query": {
                        "type": "string",
                        "description": "Read-only DAX query starting with EVALUATE.",
                    },
                },
                "required": ["model", "dax_query"],
                "additionalProperties": False,
            },
        },
    ]
    if arm == "ontology":
        tools.extend(
            [
                {
                    "type": "function",
                    "name": "list_ontology_entities",
                    "description": (
                        "List entity types and source bindings from the connected "
                        "Fabric IQ ontology MCP endpoint."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "list_ontology_rules",
                    "description": (
                        "List domain rules and constraints from the connected "
                        "Fabric IQ ontology MCP endpoint."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "get_ontology_relationships_and_metrics",
                    "description": (
                        "Return ontology entity relationships, validity windows, and "
                        "metric bindings linked to semantic-model home tables and measures."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            ]
        )
    return tools


def parse_mcp_http_response(response: httpx.Response) -> dict[str, Any]:
    """Parse either a plain JSON or Server-Sent Events (SSE) MCP HTTP response cleanly."""
    content_type = response.headers.get("content-type", "").lower()
    raw_text = response.text.strip()
    if not raw_text:
        return {}

    if "text/event-stream" in content_type or raw_text.startswith(("event:", "data:", ":")):
        parsed_events: list[dict[str, Any]] = []
        data_buffer: list[str] = []
        for line in raw_text.splitlines():
            stripped = line.strip()
            if not stripped:
                if data_buffer:
                    joined = "\n".join(data_buffer).strip()
                    data_buffer.clear()
                    if joined and joined != "[DONE]":
                        try:
                            obj = json.loads(joined)
                            if isinstance(obj, dict):
                                parsed_events.append(obj)
                        except json.JSONDecodeError:
                            continue
                continue
            if stripped.startswith(":"):
                continue
            if stripped.startswith("data:"):
                data_buffer.append(stripped[5:].lstrip())
        if data_buffer:
            joined = "\n".join(data_buffer).strip()
            if joined and joined != "[DONE]":
                try:
                    obj = json.loads(joined)
                    if isinstance(obj, dict):
                        parsed_events.append(obj)
                except json.JSONDecodeError:
                    pass
        for event in reversed(parsed_events):
            if "result" in event or "error" in event:
                return event
        return parsed_events[-1] if parsed_events else {"raw": raw_text[:MAX_TOOL_TEXT_CHARS]}

    try:
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    except json.JSONDecodeError:
        return {"error": {"message": raw_text[:MAX_TOOL_TEXT_CHARS]}}


def _extract_mcp_tool_payload(rpc_payload: dict[str, Any]) -> str:
    """Normalize MCP JSON-RPC CallToolResult or error into a bounded JSON string for the LLM."""
    if "error" in rpc_payload:
        err = rpc_payload["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return json.dumps({"isError": True, "error": msg[:MAX_TOOL_TEXT_CHARS]})

    result = rpc_payload.get("result", rpc_payload)
    if not isinstance(result, dict):
        text = json.dumps(result)
        return text[:MAX_TOOL_TEXT_CHARS]

    is_error = bool(result.get("isError", False))
    content = result.get("content")
    if isinstance(content, list) and content:
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        combined = "\n".join(texts).strip()
        if combined:
            if is_error:
                return json.dumps({"isError": True, "error": combined[:MAX_TOOL_TEXT_CHARS]})
            if len(combined) > MAX_TOOL_TEXT_CHARS:
                return json.dumps(
                    {
                        "truncated": True,
                        "preview": combined[:MAX_TOOL_TEXT_CHARS],
                        "total_chars": len(combined),
                    }
                )
            return combined

    serialized = json.dumps(result)
    if len(serialized) > MAX_TOOL_TEXT_CHARS:
        return serialized[:MAX_TOOL_TEXT_CHARS]
    return serialized


class _AzCliTokenCache:
    """Thread-safe bearer token helper backed by Azure CLI for approved live runs."""

    def __init__(
        self,
        tenant_id: str | None = None,
        subscription_id: str | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._subscription_id = subscription_id
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, float]] = {}

    def get_token(self, resource: str) -> str:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(resource)
            if cached is not None and cached[1] > now:
                return cached[0]

        az_bin = shutil.which("az") or shutil.which("az.cmd")
        if not az_bin:
            raise LiveApprovalError("Azure CLI ('az') is required for live token acquisition")
        cmd = [
            az_bin,
            "account",
            "get-access-token",
            "--resource",
            resource,
            "--output",
            "json",
            "--only-show-errors",
        ]
        if self._subscription_id:
            cmd.extend(["--subscription", self._subscription_id])
        elif self._tenant_id:
            cmd.extend(["--tenant", self._tenant_id])
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise LiveApprovalError(
                f"Failed to acquire bearer token for resource {resource}"
            )
        payload = json.loads(proc.stdout)
        token = payload.get("accessToken", "")
        if not isinstance(token, str) or not token.strip():
            raise LiveApprovalError(f"Empty bearer token returned for resource {resource}")
        if self._tenant_id and token.count(".") >= 2:
            import base64
            claims_raw = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(claims_raw + "==="))
            if claims.get("tid") != self._tenant_id:
                raise LiveApprovalError("Acquired token tenant ID does not match approved tenant")
        with self._lock:
            self._cache[resource] = (token.strip(), time.monotonic() + 1800.0)
        return token.strip()


_SHARED_TOKEN_CACHES: dict[tuple[str | None, str | None], _AzCliTokenCache] = {}
_SHARED_TOKEN_LOCK = threading.Lock()
_GLOBAL_IQ_PACE_LOCK = threading.Lock()
_GLOBAL_IQ_NEXT_ALLOWED_AT: float = 0.0
_SHARED_LIVE_DAX_CACHE: dict[tuple[str, str], str] = {}
_SHARED_LIVE_DAX_LOCK = threading.Lock()
_SHARED_LIVE_DAX_LOADED_PATH: str | None = None


def _load_or_save_live_dax_cache(entry: tuple[tuple[str, str], str] | None = None) -> None:
    """Optionally persist read-only live DAX results to a private local cache file outside git."""
    global _SHARED_LIVE_DAX_LOADED_PATH
    cache_file = os.environ.get("NORTHSTAR_LIVE_DAX_CACHE_FILE", "").strip()
    if not cache_file:
        return
    path = Path(cache_file)
    if _SHARED_LIVE_DAX_LOADED_PATH != cache_file:
        _SHARED_LIVE_DAX_LOADED_PATH = cache_file
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    _SHARED_LIVE_DAX_CACHE[(rec["a"], rec["q"])] = rec["o"]
                except (KeyError, json.JSONDecodeError):
                    continue
    if entry is not None:
        (art, q_norm), out_str = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"a": art, "q": q_norm, "o": out_str}, separators=(",", ":")) + "\n")


def _pace_live_iq_request(min_interval_seconds: float = 0.72) -> None:
    """Pace live Power BI / Fabric IQ MCP calls safely under the 120 req/min user limit."""
    global _GLOBAL_IQ_NEXT_ALLOWED_AT
    with _GLOBAL_IQ_PACE_LOCK:
        now = time.monotonic()
        wait_s = max(0.0, _GLOBAL_IQ_NEXT_ALLOWED_AT - now)
        _GLOBAL_IQ_NEXT_ALLOWED_AT = max(now, _GLOBAL_IQ_NEXT_ALLOWED_AT) + min_interval_seconds
    if wait_s > 0.0:
        time.sleep(wait_s)


def _shared_token_cache(
    tenant_id: str | None,
    subscription_id: str | None,
) -> _AzCliTokenCache:
    key = (tenant_id, subscription_id)
    with _SHARED_TOKEN_LOCK:
        cache = _SHARED_TOKEN_CACHES.get(key)
        if cache is None:
            cache = _AzCliTokenCache(tenant_id=tenant_id, subscription_id=subscription_id)
            _SHARED_TOKEN_CACHES[key] = cache
        return cache


class _LiveMcpHttpSession:
    """Lightweight JSON-RPC 2.0 Streamable HTTP MCP caller supporting JSON and SSE."""

    def __init__(
        self,
        url: str,
        *,
        http_client: httpx.Client,
        bearer_token_fn: Callable[[], str],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._url = url
        self._client = http_client
        self._bearer_token_fn = bearer_token_fn
        self._extra_headers = dict(extra_headers or {})
        self._lock = threading.Lock()
        self._initialized = False
        self._session_id: str | None = None
        self._next_id = 1

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._bearer_token_fn()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self._extra_headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _ensure_initialized(self) -> None:
        with self._lock:
            if self._initialized:
                return
            req_id = self._next_id
            self._next_id += 1
            init_body = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "northstar-eval-runner", "version": "0.1.0"},
                },
            }
            resp = self._client.post(self._url, headers=self._headers(), json=init_body)
            session_id = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
            if session_id:
                self._session_id = session_id
            if resp.status_code < 400:
                self._client.post(
                    self._url,
                    headers=self._headers(),
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
            self._initialized = True

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        max_chars: int = MAX_TOOL_TEXT_CHARS,
        pace_live: bool = False,
    ) -> str:
        for attempt in range(6):
            try:
                if pace_live:
                    _pace_live_iq_request()
                self._ensure_initialized()
                with self._lock:
                    req_id = self._next_id
                    self._next_id += 1
                body = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                }
                resp = self._client.post(self._url, headers=self._headers(), json=body)
                if resp.status_code in (429, 500, 502, 503, 504) and pace_live and attempt < 5:
                    ra = resp.headers.get("Retry-After")
                    wait_s = (
                        min(65.0, float(ra) + 1.0)
                        if ra and re.fullmatch(r"\d+(?:\.\d+)?", ra.strip())
                        else min(30.0, 5.0 * (attempt + 1))
                    )
                    time.sleep(wait_s)
                    continue
                if resp.status_code >= 400:
                    return json.dumps(
                        {
                            "isError": True,
                            "status_code": resp.status_code,
                            "error": resp.text[:MAX_TOOL_TEXT_CHARS],
                        }
                    )
                parsed = parse_mcp_http_response(resp)
                if max_chars > MAX_TOOL_TEXT_CHARS and "result" in parsed:
                    res = parsed["result"]
                    if isinstance(res, dict) and not res.get("isError"):
                        content = res.get("content") or []
                        joined = "\n".join(
                            c["text"]
                            for c in content
                            if isinstance(c, dict) and isinstance(c.get("text"), str)
                        ).strip()
                        if joined:
                            out_text = joined[:max_chars]
                        else:
                            out_text = _extract_mcp_tool_payload(parsed)
                    else:
                        out_text = _extract_mcp_tool_payload(parsed)
                else:
                    out_text = _extract_mcp_tool_payload(parsed)

                if (
                    pace_live
                    and attempt < 5
                    and ("RequestBlocked" in out_text or "TooManyRequests" in out_text)
                ):
                    until_m = re.search(r"until\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", out_text)
                    if until_m:
                        until_dt = datetime.fromisoformat(until_m.group(1).replace("Z", "+00:00"))
                        rem = (until_dt - datetime.now(timezone.utc)).total_seconds()
                        wait_s = min(90.0, max(5.0, rem + 2.0))
                    else:
                        wait_s = min(35.0, 8.0 * (attempt + 1))
                    time.sleep(wait_s)
                    continue
                return out_text
            except httpx.HTTPError as exc:
                if pace_live and attempt < 5:
                    time.sleep(min(20.0, 3.0 * (attempt + 1)))
                    continue
                return json.dumps({"isError": True, "error": f"HTTP transport error: {exc}"})
        return json.dumps({"isError": True, "error": "MCP tool retries exhausted"})


def _extract_response_text(response_json: dict[str, Any]) -> str:
    direct = response_json.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in response_json.get("output", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content", []):
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    txt = part.get("text")
                    if isinstance(txt, str):
                        chunks.append(txt)
    return "\n".join(chunks).strip()


def _coerce_agent_json(raw_text: str, question: dict[str, Any]) -> dict[str, Any] | None:
    candidate = raw_text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.DOTALL).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
        return None

    int_cols = {
        col["name"]
        for col in question["answer"]["columns"]
        if col.get("type") == "integer"
    }
    int_keys = {
        k for k in question["answer"]["keys"] if k.endswith("_key")
    }
    normalized_rows: list[dict[str, Any]] = []
    for raw_row in parsed["rows"]:
        if not isinstance(raw_row, dict):
            continue
        clean_row: dict[str, Any] = {}
        for k, v in raw_row.items():
            if (k in int_cols or k in int_keys) and isinstance(v, str) and re.fullmatch(
                r"-?\d+", v.strip()
            ):
                clean_row[k] = int(v.strip())
            elif (
                (k in int_cols or k in int_keys)
                and isinstance(v, float)
                and v.is_integer()
            ):
                clean_row[k] = int(v)
            else:
                clean_row[k] = v
        normalized_rows.append(clean_row)
    parsed["rows"] = normalized_rows
    return parsed


class LiveAgentArm:
    """Live OpenAI Responses API function-calling arm bridged to Fabric IQ and Ontology MCP."""

    mode: str = "live_demo_tenant"

    def __init__(
        self,
        arm: ArmName,
        approval: LiveEvalApproval,
        *,
        contract_path: Path = CONTRACT_PATH,
        shared_instructions_path: Path = SHARED_INSTRUCTIONS_PATH,
        http_client: httpx.Client | None = None,
        token_provider: Callable[[str], str] | None = None,
    ) -> None:
        if arm not in VALID_ARMS:
            raise ValueError(f"Unsupported live arm: {arm!r}")
        self.arm: ArmName = arm
        self.approval = approval
        self._contract_path = contract_path
        self._shared_instructions_path = shared_instructions_path
        self._contract = _load_contract(contract_path)
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(120.0, connect=20.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
        )
        default_tokens = _shared_token_cache(
            tenant_id=approval.tenant_id,
            subscription_id=approval.subscription_id,
        )
        self._token_provider = token_provider or default_tokens.get_token

        iq_extra = (
            {"X-Variants": approval.toolset_variant}
            if approval.toolset_variant != "service_default"
            else {}
        )
        self._iq_mcp = _LiveMcpHttpSession(
            approval.iq_mcp_url,
            http_client=self._client,
            bearer_token_fn=lambda: self._token_provider(approval.iq_token_resource),
            extra_headers=iq_extra,
        )
        self._ont_mcp = _LiveMcpHttpSession(
            approval.ontology_mcp_url,
            http_client=self._client,
            bearer_token_fn=lambda: self._token_provider(approval.ontology_token_resource),
        )
        self._schema_cache: dict[str, str] = {}
        self._ont_cache: dict[str, str] = {}
        self._schema_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _foundry_headers(self) -> dict[str, str]:
        api_key = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
        if api_key:
            return {"api-key": api_key, "Content-Type": "application/json"}
        token = self._token_provider(self.approval.foundry_token_resource)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _dispatch_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        step: int,
        tool_calls_out: list[dict[str, Any]],
        models_queried: list[str],
        entities_traversed: list[str],
    ) -> str:
        t0 = time.perf_counter()
        if name == "get_semantic_model_schema":
            model = str(arguments.get("model", "")).strip()
            if model not in self.approval.semantic_models:
                return json.dumps({"isError": True, "error": f"Unknown semantic model {model!r}"})
            with self._schema_lock:
                cached = self._schema_cache.get(model)
            if cached is not None:
                output = cached
                summary = f"Read cached schema for {model}"
            else:
                artifact_id = self.approval.semantic_models[model]
                output = self._iq_mcp.call_tool(
                    "GetSemanticModelSchema",
                    {"artifactId": artifact_id},
                    pace_live=self._owns_client,
                )
                if '"isError": true' not in output.lower() and "RequestBlocked" not in output:
                    with self._schema_lock:
                        self._schema_cache[model] = output
                summary = f"GetSemanticModelSchema on {model}"
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            tool_calls_out.append(
                asdict(
                    ToolCallRecord(
                        step=step,
                        server="fabric_iq",
                        tool="GetSemanticModelSchema",
                        target_model=model,
                        summary=summary,
                        elapsed_ms=elapsed_ms,
                    )
                )
            )
            return output

        if name == "execute_dax_query":
            model = str(arguments.get("model", "")).strip()
            dax_query = str(arguments.get("dax_query", "")).strip()
            if model not in self.approval.semantic_models:
                return json.dumps({"isError": True, "error": f"Unknown semantic model {model!r}"})
            if not dax_query:
                return json.dumps({"isError": True, "error": "dax_query must be non-empty"})
            artifact_id = self.approval.semantic_models[model]
            norm_key = (artifact_id, " ".join(dax_query.split()))
            cached_dax: str | None = None
            if self._owns_client:
                with _SHARED_LIVE_DAX_LOCK:
                    _load_or_save_live_dax_cache()
                    cached_dax = _SHARED_LIVE_DAX_CACHE.get(norm_key)
            if cached_dax is not None:
                output = cached_dax
            else:
                output = self._iq_mcp.call_tool(
                    "ExecuteQuery",
                    {"artifactId": artifact_id, "daxQueries": [dax_query], "maxRows": 500},
                    pace_live=self._owns_client,
                )
                if (
                    self._owns_client
                    and '"isError": true' not in output.lower()
                    and "RequestBlocked" not in output
                    and "errorCode" not in output
                ):
                    with _SHARED_LIVE_DAX_LOCK:
                        if norm_key not in _SHARED_LIVE_DAX_CACHE:
                            _SHARED_LIVE_DAX_CACHE[norm_key] = output
                            _load_or_save_live_dax_cache((norm_key, output))
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            if model not in models_queried:
                models_queried.append(model)
            tool_calls_out.append(
                asdict(
                    ToolCallRecord(
                        step=step,
                        server="fabric_iq",
                        tool="ExecuteQuery",
                        target_model=model,
                        summary=f"ExecuteQuery on {model}",
                        elapsed_ms=elapsed_ms,
                    )
                )
            )
            return output

        if self.arm == "ontology" and name in ("list_ontology_entities", "list_ontology_rules"):
            with self._schema_lock:
                cached_ont = self._ont_cache.get(name)
            if cached_ont is not None:
                output = cached_ont
            else:
                raw_out = self._ont_mcp.call_tool(name, {}, max_chars=250_000)
                output = raw_out
                if name == "list_ontology_entities":
                    try:
                        raw_obj = json.loads(raw_out)
                        vals = raw_obj.get("values") if isinstance(raw_obj, dict) else None
                        if isinstance(vals, list):
                            compact = [
                                {
                                    "name": e.get("name"),
                                    "description": e.get("description"),
                                    "backingTable": e.get("backingTable"),
                                    "keyProperties": e.get("keyProperties"),
                                    "properties": [
                                        {"name": p.get("name"), "dataType": p.get("dataType")}
                                        for p in (e.get("properties") or [])
                                        if isinstance(p, dict)
                                    ],
                                }
                                for e in vals
                                if isinstance(e, dict)
                            ]
                            output = json.dumps({"values": compact})
                    except json.JSONDecodeError:
                        output = raw_out[:MAX_TOOL_TEXT_CHARS]
                if '"isError": true' not in output.lower():
                    with self._schema_lock:
                        self._ont_cache[name] = output
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            if name == "list_ontology_entities" and not entities_traversed:
                entities_traversed.extend(e["name"] for e in self._contract.get("entities", []))
            tool_calls_out.append(
                asdict(
                    ToolCallRecord(
                        step=step,
                        server="ontology_mcp",
                        tool=name,
                        target_model=None,
                        summary=f"Call {name} on ontology MCP endpoint",
                        elapsed_ms=elapsed_ms,
                    )
                )
            )
            return output

        if self.arm == "ontology" and name == "get_ontology_relationships_and_metrics":
            payload = {
                "entities": self._contract.get("entities", []),
                "relationships": self._contract.get("relationships", []),
                "metrics": self._contract.get("metrics", []),
            }
            output = json.dumps(payload)
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            tool_calls_out.append(
                asdict(
                    ToolCallRecord(
                        step=step,
                        server="ontology_mcp",
                        tool="get_ontology_relationships_and_metrics",
                        target_model=None,
                        summary="Read ontology relationships and EXTERNALMEASURE home-table links",
                        elapsed_ms=elapsed_ms,
                    )
                )
            )
            return output

        return json.dumps({"isError": True, "error": f"Tool {name!r} is not permitted for {self.arm}"})

    def run_question(
        self,
        question: dict[str, Any],
        data_dir: Path | None = None,
        *,
        repeat: int = 1,
    ) -> ArmRunRecord:
        del data_dir
        validate_question_record(question)
        if datetime.now(timezone.utc) >= self.approval.expires_at:
            raise LiveApprovalError("Live evaluation approval expired during execution")

        started_wall = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()

        instructions = build_live_system_instructions(
            self.arm,
            question,
            contract_path=self._contract_path,
            shared_instructions_path=self._shared_instructions_path,
        )
        tools = build_live_tools(self.arm)
        user_prompt = (
            "Return valid JSON only matching the requested schema.\n"
            + json.dumps(
                {
                    "question_id": question["id"],
                    "question": question["question"],
                    "parameters": question["parameters"],
                    "persona": question["persona"],
                    "answer_contract": question["answer"],
                }
            )
        )
        conversation: list[dict[str, Any]] = [
            {"role": "user", "content": user_prompt}
        ]
        prev_resp_id: str | None = None
        next_input: list[dict[str, Any]] = conversation

        total_input_tokens = 0
        total_output_tokens = 0
        tool_calls_out: list[dict[str, Any]] = []
        models_queried: list[str] = []
        entities_traversed: list[str] = []
        parsed_answer: dict[str, Any] | None = None

        for _turn in range(self.approval.max_turns):
            req_body: dict[str, Any] = {
                "model": self.approval.model_deployment,
                "instructions": instructions,
                "input": next_input,
                "tools": tools,
                "reasoning": {"effort": self.approval.reasoning_effort},
                "text": {"format": {"type": "json_object"}},
            }
            if prev_resp_id:
                req_body["previous_response_id"] = prev_resp_id
            resp = None
            for attempt in range(7):
                try:
                    resp = self._client.post(
                        self.approval.foundry_responses_url,
                        headers=self._foundry_headers(),
                        json=req_body,
                    )
                except httpx.HTTPError:
                    time.sleep(min(20.0, 2.0 * (attempt + 1)))
                    continue
                if resp.status_code in (429, 500, 502, 503, 504):
                    ra = resp.headers.get("Retry-After")
                    wait_s = (
                        min(65.0, float(ra) + 1.0)
                        if ra and re.fullmatch(r"\d+(?:\.\d+)?", ra.strip())
                        else min(30.0, 4.0 * (attempt + 1))
                    )
                    time.sleep(wait_s)
                    continue
                break
            if resp is None or resp.status_code >= 400:
                break
            resp_json = resp.json()
            usage = resp_json.get("usage") or {}
            total_input_tokens += int(usage.get("input_tokens", 0) or 0)
            total_output_tokens += int(usage.get("output_tokens", 0) or 0)

            output_items = resp_json.get("output") or []
            fn_calls = [
                item
                for item in output_items
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            if not fn_calls:
                raw_text = _extract_response_text(resp_json)
                parsed_answer = _coerce_agent_json(raw_text, question)
                break

            resp_id = resp_json.get("id")
            call_outputs: list[dict[str, Any]] = []
            for call_item in fn_calls:
                call_id = str(call_item.get("call_id") or call_item.get("id") or "")
                fn_name = str(call_item.get("name") or "")
                raw_args = call_item.get("arguments") or "{}"
                try:
                    fn_args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except (json.JSONDecodeError, TypeError, ValueError):
                    fn_args = {}
                tool_output = self._dispatch_tool(
                    fn_name,
                    fn_args,
                    len(tool_calls_out) + 1,
                    tool_calls_out,
                    models_queried,
                    entities_traversed,
                )
                call_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": tool_output,
                    }
                )
            if isinstance(resp_id, str) and resp_id:
                prev_resp_id = resp_id
                next_input = call_outputs
            else:
                for item in output_items:
                    if isinstance(item, dict):
                        conversation.append(item)
                conversation.extend(call_outputs)
                next_input = conversation

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        completed_wall = datetime.now(timezone.utc).isoformat()

        if parsed_answer is None:
            status: RunStatus = "FAILED"
            rows: list[dict[str, Any]] = []
            exclusions: list[dict[str, Any]] = []
            bridges_used: list[dict[str, Any]] = []
            validity_checks: list[str] = []
            rules_applied: list[str] = []
        else:
            rows = parsed_answer.get("rows", [])
            exclusions = (
                parsed_answer["exclusions"]
                if isinstance(parsed_answer.get("exclusions"), list)
                else []
            )
            bridges_used = (
                parsed_answer["bridges_used"]
                if isinstance(parsed_answer.get("bridges_used"), list)
                else []
            )
            validity_checks = (
                [str(x) for x in parsed_answer["validity_checks"]]
                if isinstance(parsed_answer.get("validity_checks"), list)
                else []
            )
            rules_applied = (
                [str(x) for x in parsed_answer["rules_applied"]]
                if isinstance(parsed_answer.get("rules_applied"), list)
                else []
            )
            status = "PARTIAL" if exclusions else "COMPLETE"

        path_record = PathRecord(
            models_queried=models_queried,
            entities_traversed=entities_traversed,
            bridges_used=bridges_used,
            validity_checks=validity_checks,
            rules_applied=rules_applied,
        )
        effort = EffortRecord(
            tool_call_count=len(tool_calls_out),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            elapsed_ms=elapsed_ms,
        )
        return ArmRunRecord(
            question_id=question["id"],
            level=question["level"],
            persona=question["persona"],
            arm=self.arm,
            repeat=repeat,
            status=status,
            rows=rows,
            exclusions=exclusions,
            path=asdict(path_record),
            tool_calls=tool_calls_out,
            effort=asdict(effort),
            model_deployment=self.approval.model_deployment,
            toolset_variant=self.approval.toolset_variant,
            started_at=started_wall,
            completed_at=completed_wall,
        )


def get_live_arm(
    arm: ArmName,
    approval: LiveEvalApproval,
    *,
    contract_path: Path = CONTRACT_PATH,
    http_client: httpx.Client | None = None,
    token_provider: Callable[[str], str] | None = None,
) -> LiveAgentArm:
    """Return a live OpenAI Responses + MCP evaluation arm provider."""
    return LiveAgentArm(
        arm,
        approval,
        contract_path=contract_path,
        http_client=http_client,
        token_provider=token_provider,
    )

