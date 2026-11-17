from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, TextIO

from .deploy.client import SEMANTIC_SKILL, SPARK_SKILL, AzureCliFabricClient
from .deploy.journal import DeploymentJournal
from .deploy.models import (
    HERO_MODELS,
    LAKEHOUSE_NAME,
    MODEL_DISPLAY_NAMES,
    NOTEBOOK_NAME,
    PROFILE,
    SCHEMA_VERSION,
    SEED,
    DeployOptions,
    canonical_id,
    deployment_marker,
    journal_path,
    looks_like_id,
)

STATUS = "INVENTORY_ONLY_DELETION_REFUSED"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DEPLOYMENT_ID = re.compile(r"^northstar-m5-01:[0-9a-f]{16}$")
_ITEM_SPECS = {
    "lakehouse": ("Lakehouse", LAKEHOUSE_NAME),
    "notebook": ("Notebook", NOTEBOOK_NAME),
    **{
        f"model-{alias}": ("SemanticModel", MODEL_DISPLAY_NAMES[alias])
        for alias in HERO_MODELS
    },
}


class TeardownError(RuntimeError):
    """A fail-closed inventory contract error."""


class ReadOnlyFabricClient(Protocol):
    tenant_id: str
    user_name: str

    def resolve_workspace(self, selector: str) -> dict[str, Any]: ...

    def list_items(self, workspace_id: str, item_type: str) -> list[dict[str, Any]]: ...

    def get_item(
        self,
        workspace_id: str,
        item_id: str,
        *,
        skill: str = SPARK_SKILL,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TeardownOptions:
    workspace: str | None = None
    tenant_id: str | None = None
    expected_user: str | None = None
    deployment_tag: str = "m5-01"
    confirm: str | None = None
    dry_run: bool = False
    journal_path: Path | None = None
    azure_cli: Path | None = None
    poll_timeout_seconds: int = 1800

    def __post_init__(self) -> None:
        if not self.deployment_tag or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for character in self.deployment_tag
        ):
            raise TeardownError(
                "deployment_tag must contain only letters, numbers, '-', '_' or '.'",
            )
        if self.tenant_id is not None:
            canonical_id(self.tenant_id, "tenant_id")
        if self.poll_timeout_seconds <= 0:
            raise TeardownError("poll_timeout_seconds must be positive")
        if not self.dry_run and not self.workspace:
            raise TeardownError("A workspace name or identifier is required for live inventory")
        if not self.dry_run and (not self.tenant_id or not self.expected_user):
            raise TeardownError("Live inventory requires an expected tenant ID and interactive user")
        if not self.dry_run and not self.confirm:
            raise TeardownError("Exact --confirm workspace display name is required for live inventory")


@dataclass(frozen=True)
class TeardownResult:
    status: str
    workspace: dict[str, Any] | None
    journal_path: Path
    items: tuple[dict[str, Any], ...] = ()
    plan: dict[str, Any] | None = None


class _ReadOnlyAzureCliFabricClient:
    def __init__(self, options: TeardownOptions):
        self._client = AzureCliFabricClient(
            DeployOptions(
                workspace=options.workspace,
                tenant_id=options.tenant_id,
                expected_user=options.expected_user,
                deployment_tag=options.deployment_tag,
                dry_run=False,
                journal_path=options.journal_path,
                azure_cli=options.azure_cli,
                poll_timeout_seconds=options.poll_timeout_seconds,
            ),
        )
        self.tenant_id = self._client.tenant_id
        self.user_name = self._client.user_name

    def resolve_workspace(self, selector: str) -> dict[str, Any]:
        return self._client.resolve_workspace(selector)

    def list_items(self, workspace_id: str, item_type: str) -> list[dict[str, Any]]:
        return self._client.list_items(workspace_id, item_type)

    def get_item(
        self,
        workspace_id: str,
        item_id: str,
        *,
        skill: str = SPARK_SKILL,
    ) -> dict[str, Any]:
        return self._client.get_item(workspace_id, item_id, skill=skill)

    def close(self) -> None:
        self._client.close()


def run_teardown(
    options: TeardownOptions,
    *,
    client: ReadOnlyFabricClient | None = None,
    stdout: TextIO | None = None,
) -> TeardownResult:
    path = teardown_journal_path(options)
    if options.dry_run:
        plan = _dry_run_plan(options, path)
        print(json.dumps(plan, indent=2, sort_keys=True), file=stdout)
        return TeardownResult(
            status=STATUS,
            workspace=None,
            journal_path=path,
            plan=plan,
        )

    journal = _read_journal(options, path)
    expected_workspace = journal["workspace"]
    if options.confirm != expected_workspace["displayName"]:
        raise TeardownError("Confirmation must exactly match the private journal workspace display name")
    _validate_selector(options.workspace, expected_workspace)

    owns_client = client is None
    if client is None:
        client = _ReadOnlyAzureCliFabricClient(options)
    assert client is not None
    try:
        _validate_client_identity(client, journal, options)
        observed_workspace = client.resolve_workspace(options.workspace or "")
        _validate_observed_workspace(observed_workspace, expected_workspace)
        items = inventory_tagged_items(
            journal,
            client,
            workspace_id=expected_workspace["id"],
        )
        workspace = {
            "id": expected_workspace["id"],
            "displayName": expected_workspace["displayName"],
        }
        result = TeardownResult(
            status=STATUS,
            workspace=workspace,
            journal_path=path,
            items=items,
        )
        print(
            json.dumps(_result_payload(result, options), indent=2, sort_keys=True),
            file=stdout,
        )
        return result
    finally:
        if owns_client and hasattr(client, "close"):
            client.close()


def inventory_tagged_items(
    journal: Mapping[str, Any],
    client: ReadOnlyFabricClient,
    *,
    workspace_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    expected_workspace = journal["workspace"]
    target_id = workspace_id or expected_workspace["id"]
    if target_id != expected_workspace["id"]:
        raise TeardownError("Inventory workspace ID differs from the completed journal")

    results: list[dict[str, Any]] = []
    for alias, (item_type, display_name) in _ITEM_SPECS.items():
        entry = journal["items"][alias]
        expected_item = entry["item"]
        inventory = client.list_items(target_id, item_type)
        id_matches = [item for item in inventory if item.get("id") == expected_item["id"]]
        if len(id_matches) != 1:
            raise TeardownError(
                f"Inventory mismatch for {alias}: exact journal item ID found {len(id_matches)} times",
            )
        same_name = [item for item in inventory if item.get("displayName") == display_name]
        if len(same_name) != 1 or same_name[0].get("id") != expected_item["id"]:
            raise TeardownError(
                f"Inventory mismatch for {alias}: exact name identifies zero or multiple qualifying items",
            )
        listed = id_matches[0]
        _validate_observed_item(alias, listed, expected_item)
        skill = SEMANTIC_SKILL if item_type == "SemanticModel" else SPARK_SKILL
        observed = client.get_item(target_id, expected_item["id"], skill=skill)
        _validate_observed_item(alias, observed, expected_item)
        results.append(
            {
                "alias": alias,
                "id": expected_item["id"],
                "type": item_type,
                "displayName": display_name,
                "marker": expected_item["description"],
                "artifactDigest": entry["artifactDigest"],
            },
        )
    return tuple(results)


def build_teardown_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "teardown",
        allow_abbrev=False,
        help="Read a completed deployment journal and inspect only its exact tagged items",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("NORTHSTAR_FABRIC_WORKSPACE"),
        help="Journal workspace UUID or exact display name (env: NORTHSTAR_FABRIC_WORKSPACE)",
    )
    parser.add_argument(
        "--tenant",
        dest="tenant_id",
        default=os.environ.get("NORTHSTAR_FABRIC_TENANT_ID"),
        help="Expected Entra tenant UUID (env: NORTHSTAR_FABRIC_TENANT_ID)",
    )
    parser.add_argument(
        "--expected-user",
        default=os.environ.get("NORTHSTAR_FABRIC_USER"),
        help="Expected interactive user UPN (env: NORTHSTAR_FABRIC_USER)",
    )
    parser.add_argument(
        "--deployment-tag",
        default=os.environ.get("NORTHSTAR_DEPLOYMENT_TAG", "m5-01"),
        help="Exact private creation tag recorded by the deployer",
    )
    parser.add_argument(
        "--confirm",
        help="Exact workspace display name; this authorizes read-only inspection only",
    )
    parser.add_argument(
        "--journal",
        type=Path,
        default=_environment_path("NORTHSTAR_DEPLOY_JOURNAL"),
        help="Private completed deployment journal (env: NORTHSTAR_DEPLOY_JOURNAL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the offline inventory plan without authentication or network access",
    )
    parser.add_argument("--azure-cli", type=Path, help="Explicit az executable path for live inventory")
    parser.add_argument(
        "--poll-timeout-seconds",
        type=int,
        default=int(os.environ.get("NORTHSTAR_DEPLOY_POLL_TIMEOUT", "1800")),
    )
    parser.set_defaults(teardown_handler=teardown_from_args)
    return parser


def teardown_from_args(args: argparse.Namespace) -> TeardownResult:
    return run_teardown(teardown_options_from_args(args))


def teardown_options_from_args(args: argparse.Namespace) -> TeardownOptions:
    return TeardownOptions(
        workspace=args.workspace,
        tenant_id=args.tenant_id,
        expected_user=args.expected_user,
        deployment_tag=args.deployment_tag,
        confirm=args.confirm,
        dry_run=args.dry_run,
        journal_path=args.journal,
        azure_cli=args.azure_cli,
        poll_timeout_seconds=args.poll_timeout_seconds,
    )


def teardown_journal_path(options: TeardownOptions) -> Path:
    return journal_path(
        DeployOptions(
            workspace=options.workspace,
            tenant_id=options.tenant_id,
            expected_user=options.expected_user,
            deployment_tag=options.deployment_tag,
            dry_run=True,
            journal_path=options.journal_path,
            azure_cli=options.azure_cli,
            poll_timeout_seconds=options.poll_timeout_seconds,
        ),
    )


def _read_journal(options: TeardownOptions, path: Path) -> dict[str, Any]:
    DeploymentJournal.validate_private_path(path)
    if not path.exists():
        raise TeardownError("Completed private deployment journal is missing; inventory refused")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TeardownError("Cannot read completed private deployment journal") from error
    if not isinstance(value, dict):
        raise TeardownError("Deployment journal root must be an object")
    _validate_journal(value, options.deployment_tag)
    return value


def _validate_journal(journal: dict[str, Any], expected_tag: str) -> None:
    required = {
        "schemaVersion",
        "deploymentId",
        "tag",
        "profile",
        "seed",
        "tenant",
        "workspace",
        "items",
        "operations",
        "artifactDigests",
        "completed",
    }
    if set(journal) != required:
        raise TeardownError("Deployment journal schema does not exactly match northstar-deploy-v1")
    if journal.get("schemaVersion") != SCHEMA_VERSION:
        raise TeardownError("Deployment journal schema is not supported")
    if journal.get("completed") is not True:
        raise TeardownError("Deployment journal is incomplete; inventory refused")
    if journal.get("tag") != expected_tag:
        raise TeardownError("Deployment creation tag is missing or differs from the requested tag")
    if journal.get("profile") != PROFILE or journal.get("seed") != SEED:
        raise TeardownError("Deployment profile or seed differs from M5-01")
    deploy_id = journal.get("deploymentId")
    if not isinstance(deploy_id, str) or not _DEPLOYMENT_ID.fullmatch(deploy_id):
        raise TeardownError("Deployment identifier is not a valid M5-01 identifier")
    if not isinstance(journal["operations"], dict):
        raise TeardownError("Deployment journal operations must be an object")

    tenant = journal["tenant"]
    workspace = journal["workspace"]
    if not isinstance(tenant, dict) or set(tenant) != {"id", "user"}:
        raise TeardownError("Deployment journal tenant identity is incomplete")
    canonical_id(str(tenant.get("id", "")), "journal tenant ID")
    if not isinstance(tenant.get("user"), str) or not tenant["user"]:
        raise TeardownError("Deployment journal interactive user is missing")
    if not isinstance(workspace, dict) or set(workspace) != {"id", "displayName", "capacityId"}:
        raise TeardownError("Deployment journal workspace identity is incomplete")
    canonical_id(str(workspace.get("id", "")), "journal workspace ID")
    if not isinstance(workspace.get("displayName"), str) or not workspace["displayName"]:
        raise TeardownError("Deployment journal workspace display name is missing")
    if workspace.get("capacityId") is None:
        raise TeardownError("Deployment journal workspace capacity identity is missing")

    items = journal["items"]
    if not isinstance(items, dict) or set(items) != set(_ITEM_SPECS):
        raise TeardownError("Deployment journal item set does not exactly match M5-01")
    digests = journal["artifactDigests"]
    if not isinstance(digests, dict) or set(digests) != {
        "source",
        "dataManifest",
        "models",
        "tables",
        "lakehouse",
        "notebook",
    }:
        raise TeardownError("Deployment journal artifact digest schema is incomplete")
    if (
        not isinstance(digests["models"], dict)
        or set(digests["models"]) != set(HERO_MODELS)
        or not isinstance(digests["tables"], list)
        or not digests["tables"]
    ):
        raise TeardownError("Deployment journal model or table digest inventory is incomplete")

    identifiers: set[str] = set()
    for alias, (item_type, display_name) in _ITEM_SPECS.items():
        entry = items[alias]
        if not isinstance(entry, dict) or set(entry) != {
            "state",
            "item",
            "receipt",
            "artifactDigest",
        }:
            raise TeardownError(f"Deployment journal entry for {alias} is incomplete")
        if entry["state"] != "verified":
            raise TeardownError(f"Deployment journal entry for {alias} was not exactly verified")
        item = entry["item"]
        expected_marker = deployment_marker(deploy_id, alias)
        if not isinstance(item, dict):
            raise TeardownError(f"Deployment journal item for {alias} is invalid")
        item_id = item.get("id")
        canonical_id(str(item_id or ""), f"journal {alias} ID")
        if item_id in identifiers:
            raise TeardownError("Deployment journal contains duplicate item identities")
        identifiers.add(item_id)
        if (
            item.get("type") != item_type
            or item.get("displayName") != display_name
            or item.get("description") != expected_marker
            or item.get("workspaceId") not in (None, workspace["id"])
        ):
            raise TeardownError(f"Deployment journal item identity or marker differs for {alias}")
        digest = entry.get("artifactDigest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise TeardownError(f"Deployment journal artifact digest is invalid for {alias}")
        if alias == "lakehouse":
            artifact_digest = digests["lakehouse"]
        elif alias == "notebook":
            notebook = digests["notebook"]
            artifact_digest = notebook.get("body") if isinstance(notebook, dict) else None
        else:
            model_alias = alias.removeprefix("model-")
            model = digests["models"][model_alias]
            artifact_digest = model.get("body") if isinstance(model, dict) else None
        if digest != artifact_digest:
            raise TeardownError(f"Deployment journal digest reference differs for {alias}")


def _validate_selector(selector: str | None, expected: Mapping[str, Any]) -> None:
    if not selector:
        raise TeardownError("A workspace selector is required for live inventory")
    if looks_like_id(selector):
        if selector != expected["id"]:
            raise TeardownError("Workspace identifier differs from the completed deployment journal")
    elif selector != expected["displayName"]:
        raise TeardownError("Workspace display name differs from the completed deployment journal")


def _validate_client_identity(
    client: ReadOnlyFabricClient,
    journal: Mapping[str, Any],
    options: TeardownOptions,
) -> None:
    tenant = journal["tenant"]
    if canonical_id(client.tenant_id, "actual tenant ID") != tenant["id"]:
        raise TeardownError("Actual tenant identity differs from the completed deployment journal")
    if client.user_name.casefold() != tenant["user"].casefold():
        raise TeardownError("Actual interactive user differs from the completed deployment journal")
    if options.tenant_id and options.tenant_id != tenant["id"]:
        raise TeardownError("--tenant differs from the completed deployment journal")
    if options.expected_user and options.expected_user.casefold() != tenant["user"].casefold():
        raise TeardownError("--expected-user differs from the completed deployment journal")


def _validate_observed_workspace(
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    if (
        observed.get("id") != expected["id"]
        or observed.get("displayName") != expected["displayName"]
        or observed.get("capacityId") != expected["capacityId"]
    ):
        raise TeardownError("Actual workspace identity differs from the completed deployment journal")


def _validate_observed_item(
    alias: str,
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    if (
        observed.get("id") != expected["id"]
        or observed.get("type") != expected["type"]
        or observed.get("displayName") != expected["displayName"]
        or observed.get("description") != expected["description"]
    ):
        raise TeardownError(
            f"Inventory mismatch for {alias}: ID, type, name, or creation marker drifted",
        )
    if "workspaceId" in observed and observed.get("workspaceId") != expected.get(
        "workspaceId",
        None,
    ):
        raise TeardownError(f"Inventory mismatch for {alias}: workspace identity drifted")


def _dry_run_plan(options: TeardownOptions, path: Path) -> dict[str, Any]:
    return {
        "status": STATUS,
        "proofLevel": "LOCAL_STATIC_ONLY",
        "dryRun": True,
        "journal": str(path),
        "deploymentTag": options.deployment_tag,
        "workspace": {
            "selector": options.workspace or "<EXACT_JOURNAL_WORKSPACE>",
            "confirmation": options.confirm or "<EXACT_WORKSPACE_DISPLAY_NAME_REQUIRED_LIVE>",
        },
        "expectedItems": [
            {"alias": alias, "type": item_type, "displayName": display_name}
            for alias, (item_type, display_name) in _ITEM_SPECS.items()
        ],
        "readOnlyChecks": [
            "read and exactly validate the completed private M5-01 journal",
            "match the requested creation tag, tenant, user, workspace, item IDs, types, names, and markers",
            "list and read each exact journal item; name-only and untagged matches do not qualify",
        ],
        "mutation": "none",
        "deletion": "disabled by safety contract, including workspace and item contents",
        "manualCostControls": (
            "Pause only through the existing approved lease-aware owner and a fresh explicit approval"
        ),
        "todo": "TODO(reconcile): M5-03 item deletion is prohibited by the standing safety contract",
    }


def _result_payload(result: TeardownResult, options: TeardownOptions) -> dict[str, Any]:
    return {
        "status": result.status,
        "proofLevel": "READ_ONLY_LIVE_INVENTORY",
        "dryRun": False,
        "journal": str(result.journal_path),
        "deploymentTag": options.deployment_tag,
        "workspace": result.workspace,
        "items": list(result.items),
        "mutation": "none",
        "deletion": "disabled by safety contract, including workspace and item contents",
        "manualCostControls": (
            "Pause capacity only through the existing approved lease-aware owner and fresh explicit approval"
        ),
        "todo": "TODO(reconcile): M5-03 item deletion is prohibited by the standing safety contract",
    }


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None
