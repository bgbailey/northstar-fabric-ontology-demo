from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from telco_iq.catalog import digest
from telco_iq.errors import LabError

SECRET_KEYS = re.compile(r"authorization|cookie|token|secret|password|private.?key", re.I)
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def sanitized(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if SECRET_KEYS.search(str(k)) else sanitized(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitized(v) for v in value]
    if isinstance(value, str):
        value = re.sub(
            r"(?i)(?:\?|(?<![\w]))(?=[^\s\"<>]*\bsig=)[a-z][a-z0-9_]*=[^\s\"<>]+",
            "[REDACTED_SAS]", value,
        )
        return JWT.sub("[REDACTED_JWT]", re.sub(r"(?i)Bearer\s+\S+", "[REDACTED_BEARER]", value))
    return value


def write_receipt(directory: Path, receipt: dict) -> Path:
    clean = sanitized(receipt)
    key = digest(clean)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.json"
    content = json.dumps(clean, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise LabError("Content-addressed receipt collision")
        return path
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)
    return path


def live_claim_allowed(register: dict, receipts: list[dict], claim: str) -> bool:
    gates = register["releaseClaimGates"].get(claim)
    if not gates:
        return False
    for experiment in gates:
        matches = [r for r in receipts if r.get("experimentId") == experiment and r.get("lane") == "LIVE_DEV"]
        if not matches:
            return False
        latest = max(matches, key=lambda r: r.get("completedAt", ""))
        if (
            latest.get("status") != "PASS" or latest.get("featureOutcome") != "SUPPORTED"
            or not latest.get("evidencePaths")
        ):
            return False
    return True


def offline_observation(name: str, observed: dict) -> dict:
    return {
        "lane": "OFFLINE_SYNTHETIC", "experiment": name,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "observed": observed,
        "limitation": "Does not change any E01-E24 live capability outcome",
    }
