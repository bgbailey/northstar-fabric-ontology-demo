"""Offline contract tests for M1-06 Skills for Fabric and MCP configuration."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MCP_EXAMPLE_PATH = ROOT / ".mcp.example.json"
GITIGNORE_PATH = ROOT / ".gitignore"
REGISTER_PATH = ROOT / "eval" / "validation" / "register.json"
AGENT_TOOLING_DOC_PATH = ROOT / "docs" / "develop" / "agent-tooling.md"

UUID_RE = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F])"
)
DEPRECATED_MCP_KEYS = frozenset({"servers", "inputs", "envFile", "dev", "sandboxEnabled"})
CAMEL_CASE_ID_RE = re.compile(r"^[a-z][a-zA-Z0-9]*$")


def _collect_keys(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(str(key))
            found.update(_collect_keys(value))
    elif isinstance(node, list):
        for item in node:
            found.update(_collect_keys(item))
    return found


def test_mcp_example_matches_portable_vscode_schema_without_real_ids() -> None:
    raw_bytes = MCP_EXAMPLE_PATH.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw_bytes

    raw_text = raw_bytes.decode("utf-8")
    assert UUID_RE.findall(raw_text) == []

    config = json.loads(raw_text)
    assert set(config.keys()) == {"mcpServers"}
    assert _collect_keys(config).isdisjoint(DEPRECATED_MCP_KEYS)

    servers = config["mcpServers"]
    assert set(servers.keys()) == {"fabricIq", "northstarOntology"}
    for server_id, entry in servers.items():
        assert CAMEL_CASE_ID_RE.fullmatch(server_id)
        assert set(entry.keys()) == {"type", "url", "tools"}
        assert entry["type"] == "http"
        assert entry["url"].startswith("https://")
        assert entry["tools"] == ["*"]

    assert servers["fabricIq"]["url"] == "https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq"
    expected_ontology_url = (
        "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/"
        "<WORKSPACE_ID>/items/<ONTOLOGY_ITEM_ID>/ontologyEndpoint"
    )
    assert servers["northstarOntology"]["url"] == expected_ontology_url


def test_gitignore_ignores_local_mcp_config_and_keeps_example() -> None:
    lines = {
        line.strip()
        for line in GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".mcp.json" in lines
    assert "!.mcp.example.json" in lines
    assert ".vscode/mcp.json" in lines

    if (ROOT / ".git").exists():
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", ".mcp.json", ".vscode/mcp.json"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert ignored.returncode == 0
        not_ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", ".mcp.example.json"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert not_ignored.returncode == 1


def test_validation_register_pins_skills_release_tag_and_restructured_doc_paths() -> None:
    register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    upstream = register["upstream"]
    assert upstream["repository"] == "https://github.com/microsoft/skills-for-fabric"
    assert upstream["release"] == "v0.3.21"
    assert "commit" not in upstream

    assert register["companionPlan"] == "docs/run/streams.md"
    assert register["customerDemo"]["handoff"] == "docs/run/streams.md"
    assert register["customerDemo"]["readinessGuide"] == "docs/run/streams.md"
    assert register["sourceIdResolution"] == "docs/reference/sources.md"


def test_agent_tooling_doc_covers_skills_release_and_mcp_setup() -> None:
    raw_bytes = AGENT_TOOLING_DOC_PATH.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw_bytes

    markdown = raw_bytes.decode("utf-8")
    assert UUID_RE.findall(markdown) == []
    for expected_token in (
        "v0.3.21",
        ".mcp.example.json",
        ".mcp.json",
        "mcpServers",
        "<WORKSPACE_ID>",
        "<ONTOLOGY_ITEM_ID>",
        "semantic-model-authoring",
        "spark-cli",
        "onelake-catalog-cli",
        "eventhouse-cli",
        "eventstream-cli",
        "git-integration-operations-cli",
        "deployment-pipelines-authoring-cli",
        "e2e-fabric-cost-estimation",
        "../architecture/platform-status.md",
        "../security.md",
        "../limitations.md",
    ):
        assert expected_token in markdown
