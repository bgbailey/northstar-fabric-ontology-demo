from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telco_iq.deploy.models import DeployError

BOOTSTRAP_SCHEMA_VERSION = "northstar-ontology-bootstrap-v1"
ONTOLOGY_NAME = "Northstar_Service_Assurance"
ONTOLOGY_ITEM_TYPE = "Ontology"
ONTOLOGY_SKILL = "ontology-cli"


class BootstrapError(DeployError):
    """A fail-closed ontology bootstrap contract error."""


@dataclass(frozen=True)
class BootstrapOptions:
    workspace: str | None = None
    tenant_id: str | None = None
    expected_user: str | None = None
    subscription_id: str | None = None
    deployment_tag: str = "m5-02"
    deployment_journal: Path | None = None
    deploy_journal: Path | None = None
    journal_path: Path | None = None
    state_journal: Path | None = None
    export_path: Path | None = None
    ontology_name: str = ONTOLOGY_NAME
    dry_run: bool = False
    azure_cli: Path | None = None
    poll_timeout_seconds: int = 1800

    def __post_init__(self) -> None:
        supplied = [
            path
            for path in (self.deployment_journal, self.deploy_journal, self.journal_path)
            if path is not None
        ]
        if not supplied:
            raise BootstrapError("A completed private deployment journal is required")
        if any(path.expanduser().resolve() != supplied[0].expanduser().resolve() for path in supplied[1:]):
            raise BootstrapError("Conflicting deployment journal paths")
        if self.ontology_name != ONTOLOGY_NAME:
            raise BootstrapError(f"Ontology display name must be {ONTOLOGY_NAME!r}")
        if self.poll_timeout_seconds <= 0:
            raise BootstrapError("poll_timeout_seconds must be positive")
        if self.tenant_id is not None:
            canonical_id(self.tenant_id, "tenant_id")
        if self.subscription_id is not None:
            canonical_id(self.subscription_id, "subscription_id")
        if not self.deployment_tag or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for character in self.deployment_tag
        ):
            raise BootstrapError(
                "deployment_tag must contain only letters, numbers, '-', '_' or '.'",
            )
        if not self.dry_run:
            if not self.workspace:
                raise BootstrapError("A workspace name or identifier is required for live bootstrap")
            if not self.tenant_id or not self.expected_user:
                raise BootstrapError("Live bootstrap requires an expected tenant ID and user")

    @property
    def resolved_deployment_journal(self) -> Path:
        path = self.deployment_journal or self.deploy_journal or self.journal_path
        assert path is not None
        return path.expanduser().resolve()


@dataclass(frozen=True)
class BootstrapResult:
    status: str
    phase: str
    workspace: dict[str, Any] | None
    state_journal_path: Path
    ontology_item_id: str | None = None
    export_path: Path | None = None
    plan: dict[str, Any] | None = None


def canonical_id(value: str, label: str) -> str:
    parsed = str(uuid.UUID(value))
    if parsed != value.lower():
        raise BootstrapError(f"{label} must be a canonical UUID")
    return parsed


def state_path(options: BootstrapOptions) -> Path:
    if options.state_journal is not None:
        return options.state_journal.expanduser().resolve()
    deploy = options.resolved_deployment_journal
    return deploy.with_name(f"{deploy.stem}.ontology-bootstrap-v1.json")


def export_path(options: BootstrapOptions) -> Path:
    if options.export_path is not None:
        return options.export_path.expanduser().resolve()
    deploy = options.resolved_deployment_journal
    return deploy.with_name(f"{deploy.stem}.ontology-definition-v1.json")


def environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None
