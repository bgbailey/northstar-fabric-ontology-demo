from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .models import SCHEMA_VERSION, DeployError, DeployOptions, journal_path


class DeploymentJournal:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.data = self._read() if self.path.exists() else {"schemaVersion": SCHEMA_VERSION}

    @classmethod
    def for_options(cls, options: DeployOptions) -> DeploymentJournal:
        path = journal_path(options)
        cls.validate_private_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(path)

    @staticmethod
    def validate_private_path(path: Path) -> None:
        value = path.expanduser().resolve()
        if "onedrive" in str(value).casefold():
            raise DeployError("Deployment journal must be outside OneDrive and other synchronized folders")
        for parent in (value, *value.parents):
            if (parent / ".git").exists():
                raise DeployError("Deployment journal must be outside every repository")

    @staticmethod
    def _copy(value: Any) -> Any:
        return json.loads(json.dumps(value, sort_keys=True))

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DeployError(f"Cannot read deployment journal {self.path}") from error
        if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA_VERSION:
            raise DeployError("Unsupported deployment journal schema")
        return value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["schemaVersion"] = SCHEMA_VERSION
        payload = json.dumps(self.data, indent=2, sort_keys=True, allow_nan=False) + "\n"
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def initialize(self, identity: dict[str, Any]) -> None:
        if not self.data or self.data == {"schemaVersion": SCHEMA_VERSION}:
            self.data = self._copy(identity)
            self.data["schemaVersion"] = SCHEMA_VERSION
            self.data["items"] = {}
            self.data["operations"] = {}
            self.data["completed"] = False
            self.save()
            return
        expected = {
            "deploymentId": identity["deploymentId"],
            "tag": identity["tag"],
            "tenant": identity["tenant"],
            "subscription": identity["subscription"],
            "workspace": identity["workspace"],
        }
        for key, value in expected.items():
            if self.data.get(key) != value:
                raise DeployError(f"Journal target mismatch: {key}")

    def set_item(self, alias: str, value: dict[str, Any]) -> None:
        self.data.setdefault("items", {})[alias] = self._copy(value)
        self.save()

    def set_operation(self, name: str, value: dict[str, Any]) -> None:
        self.data.setdefault("operations", {})[name] = self._copy(value)
        self.save()

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def complete(self, artifact_digests: dict[str, Any]) -> None:
        self.data["artifactDigests"] = self._copy(artifact_digests)
        self.data["completed"] = True
        self.save()
