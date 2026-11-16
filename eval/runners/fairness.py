"""Evaluation-only block validation; never imported by the application."""

import hashlib
import json
import random
from dataclasses import dataclass


def hash_facts(facts: list[dict]) -> str:
    normalized = sorted(json.dumps(f, sort_keys=True, separators=(",", ":")) for f in facts)
    return hashlib.sha256("\n".join(normalized).encode()).hexdigest()


@dataclass(frozen=True)
class EvaluationArm:
    name: str
    model_version: str
    provider: str
    prompt_hash: str
    source_revision: str
    scope_hash: str
    budgets_hash: str
    cache_state: str
    facts_hash: str
    context_mode: str
    context_receipt: str | None


def validate_pair(flat: EvaluationArm, native: EvaluationArm) -> None:
    if flat.name != "B" or native.name != "C":
        raise ValueError("Equal-information comparison pairs B and C only")
    for field in (
        "model_version", "provider", "prompt_hash", "source_revision", "scope_hash",
        "budgets_hash", "cache_state", "facts_hash",
    ):
        if getattr(flat, field) != getattr(native, field):
            raise ValueError(f"Unfair comparison: {field} differs")
    if native.context_mode != "NATIVE_LIVE" or not native.context_receipt:
        raise ValueError("Native arm requires actual live context receipt")


def randomized_order(seed: int, arms: list[str]) -> list[str]:
    order = list(arms)
    random.Random(seed).shuffle(order)
    return order
