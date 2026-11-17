"""Offline tests for the Microsoft Foundry and Microsoft Copilot Studio agent assets."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agents.foundry import create_agent

REPO_ROOT = Path(__file__).resolve().parents[1]
CREATE_AGENT_SCRIPT = REPO_ROOT / "agents" / "foundry" / "create_agent.py"
FOUNDRY_DIR = REPO_ROOT / "agents" / "foundry"
COPILOT_STUDIO_DIR = REPO_ROOT / "agents" / "copilot-studio"

EXPECTED_SHARED_BULLETS: tuple[str, ...] = (
    (
        "- Read entity types, relationships, metric bindings, and rules from the connected "
        "Fabric IQ ontology before planning a query."
    ),
    (
        "- Follow every rule returned by the ontology instead of assuming join keys, "
        "time windows, or grains from table names."
    ),
    (
        "- Obtain every numeric figure from its governing semantic-model measure; "
        "never invent or rewrite formulas."
    ),
    "- Cite the source semantic model, home table, measure name, and relationship path used in your answer.",
    (
        "- Report missing, unmapped, or unauthorized data explicitly as unknown and "
        "list exclusions; never substitute zero."
    ),
    (
        "- Recommend actions and priorities for a person to review; never execute "
        "customer outreach, billing adjustments, or configuration changes."
    ),
)

FORBIDDEN_RULE_PHRASES: tuple[str, ...] = (
    "half-open utc",
    "service-site assignment",
    "capped once",
    "contract-month",
    "account-month",
    "billing account map",
    "billing key",
    "applied-credit",
    "daily snapshots",
)

AGENT_ENV_KEYS: tuple[str, ...] = (
    "FOUNDRY_AGENT_NAME",
    "FOUNDRY_PROJECT_ENDPOINT",
    "FOUNDRY_MODEL_NAME",
    "FOUNDRY_TOOLBOX_NAME",
    "FABRIC_IQ_PROJECT_CONNECTION_ID",
    "FABRIC_IQ_SERVER_LABEL",
    "FABRIC_IQ_SERVER_URL",
    "FABRIC_WORKSPACE_ID",
    "FABRIC_ONTOLOGY_ITEM_ID",
)


def _sanitized_subprocess_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in AGENT_ENV_KEYS}
    if overrides:
        env.update(overrides)
    return env


def _run_script_with_blocked_network(
    cli_args: list[str],
    *,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    runner_code = (
        "import runpy, socket, sys\n"
        "def _deny(*_a, **_k):\n"
        "    raise RuntimeError('Network access is forbidden during offline agent tests')\n"
        "socket.socket = _deny\n"
        "socket.create_connection = _deny\n"
        "script = sys.argv[1]\n"
        "sys.argv = [script, *sys.argv[2:]]\n"
        "runpy.run_path(script, run_name='__main__')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", runner_code, str(CREATE_AGENT_SCRIPT), *cli_args],
        cwd=REPO_ROOT,
        env=_sanitized_subprocess_env(overrides),
        capture_output=True,
        text=True,
        check=False,
    )


def test_foundry_dry_run_subprocess_clean_env() -> None:
    direct = subprocess.run(
        [sys.executable, str(CREATE_AGENT_SCRIPT), "--dry-run"],
        cwd=REPO_ROOT,
        env=_sanitized_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert direct.returncode == 0, direct.stderr

    blocked = _run_script_with_blocked_network(["--dry-run"])
    assert blocked.returncode == 0, blocked.stderr
    assert blocked.stdout == direct.stdout

    payload: dict[str, Any] = json.loads(blocked.stdout)
    assert payload["agent_name"] == "northstar-service-assurance"
    assert payload["project_endpoint"] == "<PROJECT_ENDPOINT>"
    assert payload["identity_mode"] == "on_behalf_of_signed_in_user"

    definition = payload["definition"]
    assert definition["kind"] == "prompt"
    assert definition["model"] == "<FOUNDRY_MODEL_NAME>"
    assert "fabric_iq_preview" in definition["instructions"]

    tools = definition["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    tool = tools[0]
    assert set(tool.keys()) == {
        "type",
        "project_connection_id",
        "server_label",
        "server_url",
        "require_approval",
    }
    assert tool["type"] == "fabric_iq_preview"
    assert tool["project_connection_id"] == "<CONNECTION_ID>"
    assert tool["server_label"] == "fabriciq-ontology"
    assert tool["require_approval"] == "never"
    assert tool["server_url"] == (
        "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/"
        "<WORKSPACE_ID>/items/<ONTOLOGY_ITEM_ID>/ontologyEndpoint"
    )
    assert tool["server_url"].endswith("/ontologyEndpoint")

    knowledge_files = payload["knowledge_files"]
    assert knowledge_files == [
        "agents/foundry/knowledge/sla-terms.md",
        "agents/foundry/knowledge/incident-runbook.md",
    ]
    for rel_path in knowledge_files:
        full_path = REPO_ROOT / rel_path
        assert full_path.is_file(), f"Missing knowledge file: {rel_path}"
        assert full_path.read_text(encoding="utf-8").strip()

    toolbox = payload["optional_toolbox"]
    assert toolbox["endpoint_path"].endswith("/versions?api-version=v1")
    assert toolbox["tools"][0]["type"] == "fabric_iq_preview"


def test_foundry_dry_run_subprocess_custom_synthetic_env() -> None:
    custom_env = {
        "FOUNDRY_PROJECT_ENDPOINT": (
            "https://northstar-synthetic.services.ai.azure.com/api/projects/service-assurance"
        ),
        "FOUNDRY_MODEL_NAME": "gpt-5-mini",
        "FABRIC_IQ_PROJECT_CONNECTION_ID": "conn-fabriciq-obo-demo",
        "FABRIC_IQ_SERVER_LABEL": "northstar-ontology",
        "FABRIC_WORKSPACE_ID": "ws-synthetic-demo",
        "FABRIC_ONTOLOGY_ITEM_ID": "ont-synthetic-service-assurance",
    }
    proc = _run_script_with_blocked_network(["--dry-run"], overrides=custom_env)
    assert proc.returncode == 0, proc.stderr

    payload: dict[str, Any] = json.loads(proc.stdout)
    tool = payload["definition"]["tools"][0]
    assert payload["project_endpoint"] == custom_env["FOUNDRY_PROJECT_ENDPOINT"]
    assert payload["definition"]["model"] == "gpt-5-mini"
    assert tool["project_connection_id"] == "conn-fabriciq-obo-demo"
    assert tool["server_label"] == "northstar-ontology"
    assert tool["server_url"] == (
        "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/"
        "ws-synthetic-demo/items/ont-synthetic-service-assurance/ontologyEndpoint"
    )


def test_foundry_create_fails_closed_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _deny_network(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Network must not be called when live guards reject execution")

    monkeypatch.setattr(socket, "socket", _deny_network)
    monkeypatch.setattr(socket, "create_connection", _deny_network)

    valid_synthetic_env = {
        "FOUNDRY_PROJECT_ENDPOINT": (
            "https://northstar-synthetic.services.ai.azure.com/api/projects/service-assurance"
        ),
        "FOUNDRY_MODEL_NAME": "gpt-5-mini",
        "FABRIC_IQ_PROJECT_CONNECTION_ID": "conn-fabriciq-obo-demo",
        "FABRIC_IQ_SERVER_LABEL": "fabriciq-ontology",
        "FABRIC_WORKSPACE_ID": "ws-synthetic-demo",
        "FABRIC_ONTOLOGY_ITEM_ID": "ont-synthetic-service-assurance",
    }

    # Missing --confirm-live fails closed even when synthetic env vars are complete.
    assert create_agent.main(["--create"], env=valid_synthetic_env) == 2

    # Subprocess --create without --confirm-live also fails closed before network.
    no_confirm = _run_script_with_blocked_network(["--create"], overrides=valid_synthetic_env)
    assert no_confirm.returncode == 2
    assert "--confirm-live" in no_confirm.stderr

    # Missing/placeholder environment variables fail closed even with --confirm-live.
    missing_env = _run_script_with_blocked_network(["--create", "--confirm-live"])
    assert missing_env.returncode == 2
    assert "FOUNDRY_PROJECT_ENDPOINT" in missing_env.stderr

    # Malformed server URL fails closed before network.
    bad_url_env = {**valid_synthetic_env, "FABRIC_IQ_SERVER_URL": "https://example.com/not-ontology"}
    assert create_agent.main(["--create", "--confirm-live"], env=bad_url_env) == 2

    # Uppercase or invalid server label fails closed before network.
    bad_label_env = {**valid_synthetic_env, "FABRIC_IQ_SERVER_LABEL": "Invalid Label"}
    assert create_agent.main(["--create", "--confirm-live"], env=bad_label_env) == 2


def test_foundry_and_copilot_studio_instructions_preserve_shared_contract() -> None:
    instruction_files = (
        FOUNDRY_DIR / "instructions.md",
        COPILOT_STUDIO_DIR / "instructions.md",
    )
    for path in instruction_files:
        assert path.is_file(), f"Missing instructions file: {path}"
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert len(lines) <= 25, f"{path} should stay concise (<= 25 lines)"
        for bullet in EXPECTED_SHARED_BULLETS:
            assert bullet in lines, f"Missing shared bullet in {path}: {bullet}"
        lowered = text.lower()
        for phrase in FORBIDDEN_RULE_PHRASES:
            assert phrase not in lowered, f"Rule phrase {phrase!r} leaked into {path}"


def test_copilot_studio_docs_and_starter_prompts_cover_r14_contract() -> None:
    readme_path = COPILOT_STUDIO_DIR / "README.md"
    instructions_path = COPILOT_STUDIO_DIR / "instructions.md"
    prompts_path = COPILOT_STUDIO_DIR / "starter-prompts.md"

    assert readme_path.is_file()
    assert instructions_path.is_file()
    assert prompts_path.is_file()

    readme_text = readme_path.read_text(encoding="utf-8")
    required_r14_markers = (
        "mcp_FabricIQOntology",
        "list_ontology_entity_types",
        "search_ontology",
        "McpServers.FabricIQOntology.All",
        "Microsoft 365 Copilot",
        "F2",
        "P1",
        "https://agent365.svc.cloud.microsoft/",
        "<WORKSPACE_ID>",
        "<ONTOLOGY_ITEM_ID>",
        "16 November 2026",
        "TODO(verify-live): G5",
    )
    for marker in required_r14_markers:
        assert marker in readme_text, f"Missing R-14 requirement {marker!r} in {readme_path}"

    prompts_text = prompts_path.read_text(encoding="utf-8")
    for level in ("L1", "L2", "L3", "L4"):
        assert level in prompts_text, f"Missing starter prompt level {level} in {prompts_path}"
    lowered_prompts = prompts_text.lower()
    for phrase in FORBIDDEN_RULE_PHRASES:
        assert phrase not in lowered_prompts, f"Rule phrase {phrase!r} leaked into {prompts_path}"
