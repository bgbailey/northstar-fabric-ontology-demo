"""Offline contract tests for shared agent instructions, starter prompts, and VS Code prompt files."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "fabric" / "ontology" / "service-assurance.json"
AGENTS_DIR = ROOT / "agents"
SHARED_INSTRUCTIONS_PATH = AGENTS_DIR / "shared" / "instructions.md"
STARTER_PROMPTS_PATH = AGENTS_DIR / "shared" / "starter-prompts.md"
COPILOT_DOC_PATH = ROOT / "docs" / "agents" / "github-copilot.md"
PROMPTS_DIR = ROOT / ".github" / "prompts"
EXPECTED_PROMPT_FILES = (
    "explain-hero-path.prompt.md",
    "find-credit-exposure-measure.prompt.md",
    "propose-measure-and-tests.prompt.md",
)

MAX_SHARED_INSTRUCTION_LINES = 25
NGRAM_SIZE = 5

UUID_RE = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F])"
)
DAX_EXPRESSION_RE = re.compile(
    r"\b(?:SUM|CALCULATE|DIVIDE|EXTERNALMEASURE|COUNTROWS|DISTINCTCOUNT|KEEPFILTERS|HASONEVALUE)\s*\(",
    re.IGNORECASE,
)
WORD_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

CURATED_RULE_PHRASES: dict[str, tuple[str, ...]] = {
    "R1": (
        "half-open UTC intervals",
        "incident interval, the service-site assignment and the contract",
    ),
    "R2": (
        "capped once per contract-month",
        "eligible credit minus applied credit",
        "whole-month settlement",
        "never apply the cap twice",
    ),
    "R3": (
        "one row per account-month before looking up finance",
        "Credits posted in Finance are not netted",
    ),
    "R4": (
        "map it through the billing account map",
        "billing key is not an account key",
        "Display names are not identity",
    ),
    "R5": (
        "missing applied-credit row",
        "absent credit row",
        "sum ratios, distinct counts or daily snapshots",
    ),
}

GENERIC_META_NGRAMS: frozenset[str] = frozenset({
    "as defined by the source",
    "defined by the source measure",
})


def load_contract_rules(contract_path: Path = CONTRACT_PATH) -> list[dict[str, Any]]:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    rules = list(contract["rules"])
    assert [rule["id"] for rule in rules] == ["R1", "R2", "R3", "R4", "R5"]
    return rules


def extract_bullet_lines(markdown: str) -> list[str]:
    return [line.strip() for line in markdown.splitlines() if line.lstrip().startswith("- ")]


def _normalize_phrase(text: str) -> str:
    return " ".join(WORD_TOKEN_RE.findall(text.lower()))


def build_rule_ngrams(
    rules: Sequence[Mapping[str, Any]],
    *,
    n: int = NGRAM_SIZE,
) -> dict[str, tuple[str, ...]]:
    by_rule: dict[str, tuple[str, ...]] = {}
    for rule in rules:
        rule_id = str(rule["id"])
        statement = str(rule["statement"])
        collected: list[str] = []
        seen: set[str] = set()
        for clause in re.split(r"[.;]", statement):
            tokens = WORD_TOKEN_RE.findall(clause.lower())
            for idx in range(max(0, len(tokens) - n + 1)):
                ngram = " ".join(tokens[idx : idx + n])
                if ngram in GENERIC_META_NGRAMS or ngram in seen:
                    continue
                seen.add(ngram)
                collected.append(ngram)
        assert collected, f"Expected at least one {n}-gram for {rule_id}"
        by_rule[rule_id] = tuple(collected)
    return by_rule


def validate_instruction_text(
    text: str,
    *,
    shared_bullets: Sequence[str] | None = None,
    rules: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    active_rules = load_contract_rules() if rules is None else rules
    violations: list[str] = []

    if shared_bullets is not None:
        present_lines = {line.strip() for line in text.splitlines()}
        for bullet in shared_bullets:
            if bullet not in present_lines:
                violations.append(f"missing shared instruction bullet: {bullet!r}")

    dax_match = DAX_EXPRESSION_RE.search(text)
    if dax_match is not None:
        violations.append(f"contains forbidden DAX expression: {dax_match.group(0)!r}")

    uuid_matches = UUID_RE.findall(text)
    if uuid_matches:
        violations.append(f"contains forbidden UUID identifier: {uuid_matches[0]}")

    normalized_text = f" {_normalize_phrase(text)} "
    for rule_id, phrases in CURATED_RULE_PHRASES.items():
        for phrase in phrases:
            needle = f" {_normalize_phrase(phrase)} "
            if needle in normalized_text:
                violations.append(f"restates {rule_id} rule phrase: {phrase!r}")

    for rule_id, ngrams in build_rule_ngrams(active_rules).items():
        for ngram in ngrams:
            if f" {ngram} " in normalized_text:
                violations.append(f"restates {rule_id} contract statement {NGRAM_SIZE}-gram: {ngram!r}")
                break

    return violations


def _assert_utf8_lf_without_uuids(path: Path) -> str:
    assert path.is_file(), f"Missing expected file: {path}"
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{path} must not contain a UTF-8 BOM"
    assert b"\r\n" not in raw, f"{path} must use LF line endings"
    text = raw.decode("utf-8")
    assert UUID_RE.findall(text) == [], f"{path} must not contain UUID identifiers"
    return text


def test_curated_phrases_track_service_assurance_contract() -> None:
    rules_by_id = {rule["id"]: _normalize_phrase(rule["statement"]) for rule in load_contract_rules()}
    trap_variant_phrases = {"missing applied-credit row"}
    for rule_id, phrases in CURATED_RULE_PHRASES.items():
        statement = rules_by_id[rule_id]
        for phrase in phrases:
            if phrase in trap_variant_phrases:
                continue
            assert _normalize_phrase(phrase) in statement, (
                f"Expected phrase {phrase!r} to appear in contract rule {rule_id}"
            )


def test_shared_instructions_are_concise_and_free_of_rule_leakage() -> None:
    shared_text = _assert_utf8_lf_without_uuids(SHARED_INSTRUCTIONS_PATH)
    lines = shared_text.splitlines()
    assert 1 <= len(lines) <= MAX_SHARED_INSTRUCTION_LINES

    bullets = extract_bullet_lines(shared_text)
    assert len(bullets) >= 5
    assert validate_instruction_text(shared_text, shared_bullets=bullets) == []


def test_all_agent_instruction_files_derive_from_shared_and_omit_contract_rules() -> None:
    shared_text = _assert_utf8_lf_without_uuids(SHARED_INSTRUCTIONS_PATH)
    shared_bullets = extract_bullet_lines(shared_text)
    rules = load_contract_rules()

    instruction_files = sorted(AGENTS_DIR.rglob("instructions.md"))
    assert SHARED_INSTRUCTIONS_PATH in instruction_files

    for instruction_path in instruction_files:
        text = _assert_utf8_lf_without_uuids(instruction_path)
        violations = validate_instruction_text(text, shared_bullets=shared_bullets, rules=rules)
        assert violations == [], f"{instruction_path.relative_to(ROOT)}: {violations}"


@pytest.mark.parametrize(
    ("rule_id", "leaked_snippet"),
    [
        ("R1", "Validity periods are half-open UTC intervals for every join."),
        ("R1", "Check the incident interval, the service-site assignment and the contract together."),
        ("R2", "Remember that SLA credit is capped once per contract-month."),
        ("R2", "Compute credit exposure as eligible credit minus applied credit."),
        ("R3", "Collapse to one row per account-month before looking up finance totals."),
        ("R3", "Credits posted in Finance are not netted against SLA credits."),
        ("R4", "A billing key is not an account key; map it through the billing account map."),
        ("R5", "Never treat a missing applied-credit row as zero."),
        ("R5", "Do not sum ratios, distinct counts or daily snapshots across months."),
    ],
)
def test_validator_rejects_curated_rule_phrases_in_instructions(
    rule_id: str,
    leaked_snippet: str,
) -> None:
    shared_bullets = extract_bullet_lines(SHARED_INSTRUCTIONS_PATH.read_text(encoding="utf-8"))
    seeded = SHARED_INSTRUCTIONS_PATH.read_text(encoding="utf-8") + f"\n- {leaked_snippet}\n"
    violations = validate_instruction_text(seeded, shared_bullets=shared_bullets)
    assert any(rule_id in item for item in violations), violations


def test_validator_rejects_any_rule_statement_or_five_word_slice_from_contract() -> None:
    shared_text = SHARED_INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    shared_bullets = extract_bullet_lines(shared_text)
    rules = load_contract_rules()
    ngrams_by_rule = build_rule_ngrams(rules)

    for rule in rules:
        rule_id = rule["id"]
        full_statement = f"{shared_text}\n- {rule['statement']}\n"
        full_violations = validate_instruction_text(
            full_statement,
            shared_bullets=shared_bullets,
            rules=rules,
        )
        assert any(rule_id in item for item in full_violations), (rule_id, full_violations)

        sample_ngram = ngrams_by_rule[rule_id][0]
        sliced = f"{shared_text}\n- Always check {sample_ngram} first.\n"
        ngram_violations = validate_instruction_text(
            sliced,
            shared_bullets=shared_bullets,
            rules=rules,
        )
        assert any(rule_id in item for item in ngram_violations), (rule_id, sample_ngram, ngram_violations)


@pytest.mark.parametrize(
    "dax_snippet",
    [
        "SUM('SLA Evaluation'[eligible_credit_cents])",
        "CALCULATE(COUNTROWS('Incidents'))",
        "DIVIDE(numerator, denominator)",
        "EXTERNALMEASURE(\"Credit Exposure\", INTEGER, \"DirectQuery\")",
    ],
)
def test_validator_rejects_dax_expressions_and_missing_shared_bullets(dax_snippet: str) -> None:
    shared_text = SHARED_INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    shared_bullets = extract_bullet_lines(shared_text)

    with_dax = f"{shared_text}\n- Compute {dax_snippet}.\n"
    dax_violations = validate_instruction_text(with_dax, shared_bullets=shared_bullets)
    assert any("forbidden DAX expression" in item for item in dax_violations), dax_violations

    dropped_bullet = "\n".join(shared_text.splitlines()[:-1]) + "\n"
    missing_violations = validate_instruction_text(dropped_bullet, shared_bullets=shared_bullets)
    assert any("missing shared instruction bullet" in item for item in missing_violations)

    synthetic_uuid = "-".join(("12345678", "1234", "4321", "8765", "123456789abc"))
    with_uuid = f"{shared_text}\n- Workspace {synthetic_uuid}\n"
    uuid_violations = validate_instruction_text(with_uuid, shared_bullets=shared_bullets)
    assert any("forbidden UUID identifier" in item for item in uuid_violations)


def test_starter_prompts_cover_all_four_evaluation_levels() -> None:
    prompts_text = _assert_utf8_lf_without_uuids(STARTER_PROMPTS_PATH)
    for level in ("L1", "L2", "L3", "L4"):
        assert re.search(rf"\b{level}\b", prompts_text), f"Missing level {level} in starter-prompts.md"
    for expected_phrase in (
        "July 2026",
        "SLA credit exposure",
        "recognized service revenue",
        "contribution margin",
        "escalated care cases",
    ):
        assert expected_phrase in prompts_text
    assert validate_instruction_text(prompts_text, shared_bullets=None) == []


def test_github_copilot_doc_and_vscode_prompt_files_exist_and_pass_guards() -> None:
    doc_text = _assert_utf8_lf_without_uuids(COPILOT_DOC_PATH)
    for required_token in (
        "TODO(verify-live): G5",
        ".mcp.example.json",
        ".mcp.json",
        "mcpServers",
        "<WORKSPACE_ID>",
        "<ONTOLOGY_ITEM_ID>",
        "https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp",
        "https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server",
        "https://code.visualstudio.com/docs/agents/reference/mcp-configuration",
        "../develop/agent-tooling.md",
        "../architecture/platform-status.md",
        "../limitations.md",
        *EXPECTED_PROMPT_FILES,
    ):
        assert required_token in doc_text, f"Missing {required_token!r} in docs/agents/github-copilot.md"

    for prompt_filename in EXPECTED_PROMPT_FILES:
        prompt_path = PROMPTS_DIR / prompt_filename
        prompt_text = _assert_utf8_lf_without_uuids(prompt_path)
        front_matter_match = FRONT_MATTER_RE.match(prompt_text)
        assert front_matter_match is not None, f"{prompt_filename} is missing YAML front matter"
        front_matter = front_matter_match.group(1)
        assert re.search(r"^description:\s*\S+", front_matter, re.MULTILINE), prompt_filename
        assert re.search(r"^(?:agent|mode):\s*[\"']?agent[\"']?\s*$", front_matter, re.MULTILINE), (
            prompt_filename
        )
        violations = validate_instruction_text(prompt_text, shared_bullets=None)
        assert violations == [], f"{prompt_filename}: {violations}"
