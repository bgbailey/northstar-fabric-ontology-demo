from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE = "service-assurance"
SEED = 20260929
SCHEMA_VERSION = "northstar-deploy-v1"

HERO_MODELS = ("network", "enterprise", "finance", "care")
MODEL_DISPLAY_NAMES = {
    "network": "Northstar Network Service Assurance",
    "enterprise": "Northstar Enterprise Services Service Assurance",
    "finance": "Northstar Billing and Finance Service Assurance",
    "care": "Northstar Customer Care Service Assurance",
}
HERO_TABLES = (
    "account",
    "account_month",
    "applied_credit",
    "billing_account_map",
    "calendar_date",
    "case",
    "case_activity",
    "case_daily_snapshot",
    "charge_line",
    "contract",
    "credit",
    "incident",
    "market",
    "service",
    "service_month",
    "service_site_map",
    "site",
    "site_day",
    "site_hour",
    "sla_evaluation",
    "subscription_site_exposure",
)
LAKEHOUSE_NAME = "northstar_service_assurance_20260929"
NOTEBOOK_NAME = "Northstar_Service_Assurance_Load_20260929"


class DeployError(RuntimeError):
    """A fail-closed deployment contract error."""


@dataclass(frozen=True)
class DeployOptions:
    workspace: str | None = None
    tenant_id: str | None = None
    expected_user: str | None = None
    subscription_id: str | None = None
    deployment_tag: str = "m5-01"
    dry_run: bool = False
    journal_path: Path | None = None
    azure_cli: Path | None = None
    poll_timeout_seconds: int = 1800

    def __post_init__(self) -> None:
        if not self.deployment_tag or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for character in self.deployment_tag
        ):
            raise DeployError("deployment_tag must contain only letters, numbers, '-', '_' or '.'")
        if self.tenant_id is not None:
            canonical_id(self.tenant_id, "tenant_id")
        if self.subscription_id is not None:
            canonical_id(self.subscription_id, "subscription_id")
        if not self.dry_run and not self.workspace:
            raise DeployError("A workspace name or identifier is required for live deployment")
        if not self.dry_run and (not self.tenant_id or not self.expected_user):
            raise DeployError("Live deployment requires an expected tenant ID and interactive user")
        if self.poll_timeout_seconds <= 0:
            raise DeployError("poll_timeout_seconds must be positive")


@dataclass(frozen=True)
class DeployResult:
    status: str
    workspace: dict[str, Any] | None
    journal_path: Path
    created: tuple[str, ...] = ()
    reused: tuple[str, ...] = ()
    refreshed: tuple[str, ...] = ()
    plan: dict[str, Any] | None = None


def canonical_id(value: str, label: str) -> str:
    parsed = str(uuid.UUID(value))
    if parsed != value.lower():
        raise DeployError(f"{label} must be a canonical UUID")
    return parsed


def looks_like_id(value: str) -> bool:
    try:
        canonical_id(value, "identifier")
        return True
    except (ValueError, DeployError):
        return False


def state_directory() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root) / "northstar" / "deploy"
    root = os.environ.get("XDG_STATE_HOME")
    return (Path(root) if root else Path.home() / ".local" / "state") / "northstar" / "deploy"


def journal_path(options: DeployOptions) -> Path:
    if options.journal_path is not None:
        return options.journal_path.expanduser().resolve()
    selector = (options.workspace or "<unselected-workspace>").casefold()
    key = hashlib.sha256(
        f"{SCHEMA_VERSION}:{options.deployment_tag}:{selector}".encode("utf-8"),
    ).hexdigest()[:20]
    return state_directory() / f"{key}.json"


def deployment_marker(deployment_id: str, alias: str) -> str:
    return f"Northstar {PROFILE}; deployment={deployment_id}; asset={alias}"


def deployment_id(options: DeployOptions) -> str:
    selector = (options.workspace or "<unselected-workspace>").casefold()
    digest = hashlib.sha256(
        f"{SCHEMA_VERSION}:{options.deployment_tag}:{selector}:{PROFILE}:{SEED}".encode("utf-8"),
    ).hexdigest()[:16]
    return f"northstar-m5-01:{digest}"


def build_plan(options: DeployOptions) -> dict[str, Any]:
    path = journal_path(options)
    deploy_id = deployment_id(options)
    workspace = options.workspace or "<WORKSPACE_ID_OR_NAME_FROM_CLI_OR_ENV>"
    resolution = (
        "direct GET /v1/workspaces/{id}"
        if options.workspace and looks_like_id(options.workspace)
        else "GET /v1/workspaces with pagination, exact displayName filter; fail on zero or multiple"
    )
    return {
        "status": "DRY_RUN_OFFLINE",
        "proofLevel": "LOCAL_STATIC_ONLY",
        "liveExecution": "NOT_RUN",
        "tenant": {
            "selected": options.tenant_id or "<interactive-Azure-CLI-tenant>",
            "expectedUser": options.expected_user or "<verify-selected-interactive-user>",
            "expectedSubscription": options.subscription_id or "<active-context-if-not-explicitly-pinned>",
            "authentication": (
                "not attempted in dry-run; live mode requires a matching, pre-existing, "
                "operator-opened interactive Azure CLI session and never launches sign-in"
            ),
        },
        "workspace": {
            "selector": workspace,
            "resolution": resolution,
            "requirement": "existing workspace only; no workspace create/update/delete",
            "capacity": "read-only verify that capacityId is present; never create, pause, resize, or reassign",
        },
        "deployment": {
            "id": deploy_id,
            "tag": options.deployment_tag,
            "profile": PROFILE,
            "seed": SEED,
            "sourceClock": "2026-09-29T00:00:00+00:00",
            "journal": str(path),
        },
        "data": {
            "generation": "packaged deterministic tools/build_data.py under importlib.resources",
            "profile": PROFILE,
            "seed": SEED,
            "selectedTableCount": len(HERO_TABLES),
            "tables": list(HERO_TABLES),
            "checks": [
                "validate every Parquet file SHA-256, schema, row count, primary key, and logical content SHA-256",
                "embed only the 21 tables referenced by the four selected models",
                "Delta retry writes use errorifexists, never overwrite or append",
                "existing Delta table must exactly match schema, keys, row count, and bidirectional row content",
            ],
        },
        "lakehouse": {
            "displayName": LAKEHOUSE_NAME,
            "type": "Lakehouse",
            "schemas": True,
            "marker": deployment_marker(deploy_id, "lakehouse"),
        },
        "notebook": {
            "displayName": NOTEBOOK_NAME,
            "type": "Notebook",
            "operation": "create once, run once, poll exact accepted job; no POST retry on unknown outcome",
            "load": "write missing Delta tables and read back every embedded synthetic table",
            "checks": "job completion plus notebook source hash assertions and exact Delta comparisons",
        },
        "semanticModels": [
            {
                "alias": alias,
                "displayName": MODEL_DISPLAY_NAMES[alias],
                "definition": "existing generated TMDL; no source DAX or other definition mutation",
                "marker": deployment_marker(deploy_id, f"model-{alias}"),
            }
            for alias in HERO_MODELS
        ],
        "binding": {
            "substitutions": {
                "<WORKSPACE_ID>": "<resolved-workspace-id>",
                "<LAKEHOUSE_ID>": "<created-or-exactly-owned-lakehouse-id>",
            },
            "checks": [
                "replace only the two exact placeholder tokens",
                "fail if either placeholder remains in any definition part",
                "compare the complete TMDL readback part set and digest to the submitted bound definition",
            ],
        },
        "orderedMutations": [
            "read-only workspace and collision inventory",
            "create lakehouse, persist accepted receipt, poll/read back exact marker and item ID",
            "create loader notebook, verify definition digest, submit one RunNotebook job, poll exact receipt",
            "create four TMDL semantic models with bound IDs, poll/read back full definitions",
            (
                "submit one Power BI refresh at "
                "https://api.powerbi.com/v1.0/myorg/groups/{workspace}/datasets/{model}/refreshes "
                "per exact model ID, then GET the accepted Location to completion"
            ),
            "atomically persist completion journal with names, IDs, digests, LRO/job paths, and receipts",
        ],
        "idempotency": [
            "a completed journal is read-only: zero duplicate create, update, notebook-run, or refresh submissions",
            "pending accepted creation is reconciled by its exact operation and item ID, never re-created",
            "unknown or unobserved POST outcomes are journaled and never retried",
            "same-name items are collisions unless recovered under this deployment's exact marker and state",
            "readback definition digest mismatch fails closed",
        ],
        "safety": [
            "existing workspace required",
            "never delete any workspace or item",
            "never change, pause, resume, create, resize, or reassign capacity",
            "no rollback delete path and no broad cleanup",
            "no tenant, workspace, token, or item ID is committed",
        ],
        "todo": "TODO(verify-live): deploy to an empty demo workspace with the exact tenant user and record evidence separately",
    }
