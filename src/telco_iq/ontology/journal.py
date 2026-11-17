from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from telco_iq.deploy.journal import DeploymentJournal

from .models import BOOTSTRAP_SCHEMA_VERSION, BootstrapError


class BootstrapJournal:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        DeploymentJournal.validate_private_path(self.path)
        self.data = self._read() if self.path.exists() else {}

    def initialize(self, identity: dict[str, Any]) -> None:
        if not self.data:
            self.data = copy.deepcopy(identity)
            self.data["schemaVersion"] = BOOTSTRAP_SCHEMA_VERSION
            self.data["phase"] = "create"
            self.data["completed"] = False
            self.data["completedSteps"] = []
            self.data["operations"] = {}
            self.save()
            return
        for key, value in identity.items():
            if self.data.get(key) != value:
                raise BootstrapError(f"Bootstrap journal target mismatch: {key}")
        if self.data.get("schemaVersion") != BOOTSTRAP_SCHEMA_VERSION:
            raise BootstrapError("Unsupported ontology bootstrap journal schema")

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise BootstrapError(f"Cannot read ontology bootstrap journal {self.path}") from error
        if (
            not isinstance(value, dict)
            or value.get("schemaVersion") != BOOTSTRAP_SCHEMA_VERSION
        ):
            raise BootstrapError("Unsupported ontology bootstrap journal schema")
        return value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, indent=2, sort_keys=True, allow_nan=False) + "\n"
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def set_phase(self, phase: str) -> None:
        self.data["phase"] = phase
        self.save()

    def set_operation(self, name: str, value: dict[str, Any]) -> None:
        self.data.setdefault("operations", {})[name] = copy.deepcopy(value)
        self.save()
