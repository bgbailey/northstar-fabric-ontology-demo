"""Create or preview the Northstar service assurance agent in Microsoft Foundry.

Follows Microsoft Learn R-12 (https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/fabric-iq,
preview checked 16 November 2026) using the `fabric_iq_preview` tool pointed at the
Fabric IQ ontology MCP endpoint with on-behalf-of signed-in user identity.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# TODO(verify-live): G5 — Create the Foundry connection and run the agent in the demo tenant.

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTRUCTIONS_PATH = Path(__file__).resolve().with_name("instructions.md")
KNOWLEDGE_FILES: tuple[str, ...] = (
    "agents/foundry/knowledge/sla-terms.md",
    "agents/foundry/knowledge/incident-runbook.md",
)

DEFAULT_AGENT_NAME = "northstar-service-assurance"
DEFAULT_SERVER_LABEL = "fabriciq-ontology"
DEFAULT_TOOLBOX_NAME = "northstar-fabriciq-toolbox"

PLACEHOLDER_PROJECT_ENDPOINT = "<PROJECT_ENDPOINT>"
PLACEHOLDER_MODEL_NAME = "<FOUNDRY_MODEL_NAME>"
PLACEHOLDER_CONNECTION_ID = "<CONNECTION_ID>"
PLACEHOLDER_WORKSPACE_ID = "<WORKSPACE_ID>"
PLACEHOLDER_ONTOLOGY_ITEM_ID = "<ONTOLOGY_ITEM_ID>"

ONTOLOGY_ENDPOINT_PREFIX = "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/"
ONTOLOGY_ENDPOINT_SUFFIX = "/ontologyEndpoint"
SERVER_LABEL_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ZERO_GUID = "00000000-0000-0000-0000-000000000000"


class FoundryConfigError(ValueError):
    """Raised when live Foundry or Fabric IQ configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class FoundryAgentConfig:
    agent_name: str
    project_endpoint: str
    model_name: str
    project_connection_id: str
    server_label: str
    server_url: str
    toolbox_name: str


def build_ontology_endpoint_url(workspace_id: str, ontology_item_id: str) -> str:
    """Construct the item-scoped Fabric IQ ontology MCP endpoint URL (R-08, R-12)."""
    return (
        f"{ONTOLOGY_ENDPOINT_PREFIX}{workspace_id}"
        f"/items/{ontology_item_id}{ONTOLOGY_ENDPOINT_SUFFIX}"
    )


def _env_or_default(env: Mapping[str, str], key: str, default: str) -> str:
    raw = env.get(key, "").strip()
    return raw if raw else default


def load_config_from_env(env: Mapping[str, str] | None = None) -> FoundryAgentConfig:
    """Load Foundry agent settings from environment variables with dry-run placeholders."""
    source = os.environ if env is None else env
    workspace_id = _env_or_default(source, "FABRIC_WORKSPACE_ID", PLACEHOLDER_WORKSPACE_ID)
    ontology_item_id = _env_or_default(
        source,
        "FABRIC_ONTOLOGY_ITEM_ID",
        PLACEHOLDER_ONTOLOGY_ITEM_ID,
    )
    default_server_url = build_ontology_endpoint_url(workspace_id, ontology_item_id)
    return FoundryAgentConfig(
        agent_name=_env_or_default(source, "FOUNDRY_AGENT_NAME", DEFAULT_AGENT_NAME),
        project_endpoint=_env_or_default(
            source,
            "FOUNDRY_PROJECT_ENDPOINT",
            PLACEHOLDER_PROJECT_ENDPOINT,
        ),
        model_name=_env_or_default(source, "FOUNDRY_MODEL_NAME", PLACEHOLDER_MODEL_NAME),
        project_connection_id=_env_or_default(
            source,
            "FABRIC_IQ_PROJECT_CONNECTION_ID",
            PLACEHOLDER_CONNECTION_ID,
        ),
        server_label=_env_or_default(source, "FABRIC_IQ_SERVER_LABEL", DEFAULT_SERVER_LABEL),
        server_url=_env_or_default(source, "FABRIC_IQ_SERVER_URL", default_server_url),
        toolbox_name=_env_or_default(source, "FOUNDRY_TOOLBOX_NAME", DEFAULT_TOOLBOX_NAME),
    )


def load_instructions(path: Path = INSTRUCTIONS_PATH) -> str:
    """Read the versioned Microsoft Foundry instruction file."""
    if not path.is_file():
        raise FoundryConfigError(f"Instructions file does not exist: {path}")
    return path.read_text(encoding="utf-8").strip()


def verify_local_assets(repo_root: Path = REPO_ROOT) -> list[str]:
    """Ensure the versioned knowledge files exist on disk and return relative paths."""
    missing: list[str] = []
    for rel_path in KNOWLEDGE_FILES:
        candidate = repo_root / rel_path
        if not candidate.is_file():
            missing.append(rel_path)
    if missing:
        joined = ", ".join(missing)
        raise FoundryConfigError(f"Missing Foundry knowledge files: {joined}")
    return list(KNOWLEDGE_FILES)


def build_agent_spec(
    config: FoundryAgentConfig,
    *,
    instructions_path: Path = INSTRUCTIONS_PATH,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """Build the deterministic R-12 PromptAgentDefinition and optional toolbox spec."""
    instructions = load_instructions(instructions_path)
    knowledge_files = verify_local_assets(repo_root)
    fabric_iq_tool: dict[str, str] = {
        "type": "fabric_iq_preview",
        "project_connection_id": config.project_connection_id,
        "server_label": config.server_label,
        "server_url": config.server_url,
        "require_approval": "never",
    }
    toolbox_tool: dict[str, str] = {
        "type": "fabric_iq_preview",
        "project_connection_id": config.project_connection_id,
        "server_label": config.server_label,
        "server_url": config.server_url,
    }
    return {
        "agent_name": config.agent_name,
        "project_endpoint": config.project_endpoint,
        "identity_mode": "on_behalf_of_signed_in_user",
        "definition": {
            "kind": "prompt",
            "model": config.model_name,
            "instructions": instructions,
            "tools": [fabric_iq_tool],
        },
        "knowledge_files": knowledge_files,
        "optional_toolbox": {
            "toolbox_name": config.toolbox_name,
            "endpoint_path": f"/toolboxes/{config.toolbox_name}/versions?api-version=v1",
            "tools": [toolbox_tool],
        },
    }


def _looks_like_placeholder(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if "<" in stripped or ">" in stripped or ZERO_GUID in lowered:
        return True
    return lowered.startswith(("placeholder", "example-", "example_", "redacted"))


def validate_live_config(config: FoundryAgentConfig) -> None:
    """Fail closed before any credential lookup or network call when config is incomplete."""
    checks: tuple[tuple[str, str], ...] = (
        ("FOUNDRY_PROJECT_ENDPOINT", config.project_endpoint),
        ("FOUNDRY_MODEL_NAME", config.model_name),
        ("FABRIC_IQ_PROJECT_CONNECTION_ID", config.project_connection_id),
        ("FABRIC_IQ_SERVER_LABEL", config.server_label),
        ("FABRIC_IQ_SERVER_URL", config.server_url),
    )
    invalid_fields = [name for name, val in checks if _looks_like_placeholder(val)]
    if invalid_fields:
        joined = ", ".join(invalid_fields)
        raise FoundryConfigError(
            f"Live creation requires non-placeholder values for: {joined}."
        )

    if not config.project_endpoint.startswith("https://") or "/api/projects/" not in config.project_endpoint:
        raise FoundryConfigError(
            "FOUNDRY_PROJECT_ENDPOINT must be an HTTPS Foundry project URL containing '/api/projects/'."
        )

    if not SERVER_LABEL_RE.fullmatch(config.server_label):
        raise FoundryConfigError(
            "FABRIC_IQ_SERVER_LABEL must be lowercase alphanumeric words separated by hyphens."
        )

    if not (
        config.server_url.startswith(ONTOLOGY_ENDPOINT_PREFIX)
        and config.server_url.endswith(ONTOLOGY_ENDPOINT_SUFFIX)
        and "/items/" in config.server_url
    ):
        raise FoundryConfigError(
            "FABRIC_IQ_SERVER_URL must match "
            "'https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/<WORKSPACE_ID>"
            "/items/<ONTOLOGY_ITEM_ID>/ontologyEndpoint'."
        )


def _acquire_foundry_bearer_token() -> str:
    """Acquire a delegated Entra token for https://ai.azure.com/.default."""
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore[import-not-found]

        credential = DefaultAzureCredential()
        return str(credential.get_token("https://ai.azure.com/.default").token)
    except ImportError:
        result = subprocess.run(
            [
                "az",
                "account",
                "get-access-token",
                "--scope",
                "https://ai.azure.com/.default",
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        token = result.stdout.strip()
        if not token:
            raise FoundryConfigError("Azure CLI returned an empty access token.")
        return token


def create_live_agent(
    config: FoundryAgentConfig,
    spec: dict[str, Any],
    *,
    register_toolbox: bool = False,
) -> dict[str, Any]:
    """Create the prompt agent version in Microsoft Foundry via SDK or R-12 v1 REST API."""
    validate_live_config(config)

    if not register_toolbox:
        try:
            from azure.ai.projects import AIProjectClient  # type: ignore[import-not-found]
            from azure.ai.projects.models import (  # type: ignore[import-not-found]
                FabricIQPreviewTool,
                PromptAgentDefinition,
            )
            from azure.identity import DefaultAzureCredential  # type: ignore[import-not-found]

            tool_spec = spec["definition"]["tools"][0]
            client = AIProjectClient(
                endpoint=config.project_endpoint,
                credential=DefaultAzureCredential(),
            )
            definition = PromptAgentDefinition(
                model=config.model_name,
                instructions=spec["definition"]["instructions"],
                tools=[
                    FabricIQPreviewTool(
                        project_connection_id=tool_spec["project_connection_id"],
                        server_label=tool_spec["server_label"],
                        server_url=tool_spec["server_url"],
                        require_approval=tool_spec["require_approval"],
                    )
                ],
            )
            created = client.agents.create_version(
                agent_name=config.agent_name,
                definition=definition,
            )
            return {
                "transport": "azure-ai-projects",
                "agent_name": getattr(created, "name", config.agent_name),
                "version": getattr(created, "version", None),
            }
        except ImportError:
            pass

    import httpx

    token = _acquire_foundry_bearer_token()
    base_url = config.project_endpoint.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    output: dict[str, Any] = {"transport": "rest-v1"}
    with httpx.Client(timeout=30.0) as http_client:
        if register_toolbox:
            toolbox_url = (
                f"{base_url}/toolboxes/{config.toolbox_name}/versions?api-version=v1"
            )
            tb_resp = http_client.post(
                toolbox_url,
                headers=headers,
                json={"tools": spec["optional_toolbox"]["tools"]},
            )
            tb_resp.raise_for_status()
            output["toolbox"] = tb_resp.json()

        agent_url = f"{base_url}/agents/{config.agent_name}/versions?api-version=v1"
        agent_resp = http_client.post(
            agent_url,
            headers=headers,
            json={"definition": spec["definition"]},
        )
        agent_resp.raise_for_status()
        output["agent"] = agent_resp.json()
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview (--dry-run) or create (--create --confirm-live) the Northstar "
            "service assurance prompt agent in Microsoft Foundry."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the deterministic PromptAgentDefinition JSON without network calls.",
    )
    mode.add_argument(
        "--create",
        action="store_true",
        help="Create a new prompt agent version in the configured Microsoft Foundry project.",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Confirm explicit human approval for a live Microsoft Foundry write operation.",
    )
    parser.add_argument(
        "--register-toolbox",
        action="store_true",
        help="Also register the optional Foundry toolbox version (live mode only).",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        config = load_config_from_env(env)
        spec = build_agent_spec(config)
        if args.dry_run:
            if args.register_toolbox:
                raise FoundryConfigError("--register-toolbox can only be used with --create.")
            print(json.dumps(spec, indent=2))
            return 0

        if not args.confirm_live:
            raise FoundryConfigError(
                "Refusing live Microsoft Foundry agent creation without --confirm-live."
            )
        validate_live_config(config)
        result = create_live_agent(
            config,
            spec,
            register_toolbox=args.register_toolbox,
        )
        print(json.dumps(result, indent=2))
        return 0
    except FoundryConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
