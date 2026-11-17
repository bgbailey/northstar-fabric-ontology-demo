"""Offline evaluation arm protocol and deterministic scripted providers (M2-07).

Provides one typed interface (``ArmProvider`` / ``ArmRunRecord``) shared by
scripted CI arms and future live MCP arms (M4-02). Scripted offline arms run
against local synthetic Parquet fixtures so CI exercises the harness and
scorecard end-to-end without a tenant or LLM deployment.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from eval.oracle.service_assurance import (
    VALID_TRAPS,
    evaluate_question,
    evaluate_trap,
    validate_question_record,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "fabric" / "ontology" / "service-assurance.json"

ArmName = Literal["baseline", "baseline_plus_rules", "ontology"]
RunStatus = Literal["COMPLETE", "PARTIAL", "FAILED"]


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


class ScriptedBaselinePlusRulesArm(ScriptedOntologyArm):
    """Offline scripted semantic-models-plus-pasted-rules arm (ready for M4-02)."""

    arm: ArmName = "baseline_plus_rules"
    mode: str = "OFFLINE_SCRIPTED"

    def run_question(
        self,
        question: dict[str, Any],
        data_dir: Path,
        *,
        repeat: int = 1,
    ) -> ArmRunRecord:
        base_record = super().run_question(question, data_dir, repeat=repeat)
        models = list(question["models"])
        calls: list[dict[str, Any]] = []
        step = 1
        per_step = round(base_record.effort["elapsed_ms"] / max(1, len(models) * 2), 3)
        for model_id in models:
            calls.append(
                asdict(
                    ToolCallRecord(
                        step=step,
                        server="fabric_iq",
                        tool="GetSemanticModelSchema",
                        target_model=model_id,
                        summary=f"Inspect {model_id} schema with rules in prompt",
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
            input_tokens=650 + 420 * len(models),
            output_tokens=150 * len(models),
            elapsed_ms=base_record.effort["elapsed_ms"],
        )
        path_dict = dict(base_record.path)
        path_dict["entities_traversed"] = []
        path_dict["bridges_used"] = []
        return ArmRunRecord(
            question_id=base_record.question_id,
            level=base_record.level,
            persona=base_record.persona,
            arm=self.arm,
            repeat=repeat,
            status=base_record.status,
            rows=base_record.rows,
            exclusions=base_record.exclusions,
            path=path_dict,
            tool_calls=calls,
            effort=asdict(effort),
            model_deployment=base_record.model_deployment,
            toolset_variant=base_record.toolset_variant,
            started_at=base_record.started_at,
            completed_at=base_record.completed_at,
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
