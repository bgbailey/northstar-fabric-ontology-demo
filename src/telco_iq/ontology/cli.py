from __future__ import annotations

import argparse
import os
from pathlib import Path

from .models import BootstrapOptions, BootstrapResult, environment_path
from .runner import run_bootstrap


def build_ontology_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    ontology = subparsers.add_parser(
        "ontology",
        help="Manage the guided Northstar ontology bootstrap",
    )
    commands = ontology.add_subparsers(dest="ontology_command", required=True)
    bootstrap = commands.add_parser(
        "bootstrap",
        help="Create the empty ontology, guide agent authoring, export, and check conformance",
    )
    bootstrap.add_argument(
        "--workspace",
        default=os.environ.get("NORTHSTAR_FABRIC_WORKSPACE"),
        help="Existing workspace UUID or exact display name (env: NORTHSTAR_FABRIC_WORKSPACE)",
    )
    bootstrap.add_argument(
        "--tenant",
        dest="tenant_id",
        default=os.environ.get("NORTHSTAR_FABRIC_TENANT_ID"),
        help="Expected Entra tenant UUID (env: NORTHSTAR_FABRIC_TENANT_ID)",
    )
    bootstrap.add_argument(
        "--expected-user",
        default=os.environ.get("NORTHSTAR_FABRIC_USER"),
        help="Expected interactive user UPN (env: NORTHSTAR_FABRIC_USER)",
    )
    bootstrap.add_argument(
        "--subscription",
        dest="subscription_id",
        default=os.environ.get("NORTHSTAR_FABRIC_SUBSCRIPTION_ID"),
        help="Expected Azure subscription UUID (env: NORTHSTAR_FABRIC_SUBSCRIPTION_ID)",
    )
    bootstrap.add_argument(
        "--deployment-tag",
        default=os.environ.get("NORTHSTAR_ONTOLOGY_BOOTSTRAP_TAG", "m5-02"),
        help="Private bootstrap tag used only to identify the exact run",
    )
    bootstrap.add_argument(
        "--deploy-journal",
        "--deployment-journal",
        "--journal",
        dest="deployment_journal",
        type=Path,
        default=environment_path("NORTHSTAR_DEPLOY_JOURNAL"),
        help="Completed private northstar-deploy-v1 journal outside the repository and OneDrive",
    )
    bootstrap.add_argument(
        "--state-journal",
        type=Path,
        default=environment_path("NORTHSTAR_ONTOLOGY_BOOTSTRAP_JOURNAL"),
        help="Private v1 bootstrap state journal; defaults beside the deployment journal",
    )
    bootstrap.add_argument(
        "--export",
        "--export-path",
        dest="export_path",
        type=Path,
        default=environment_path("NORTHSTAR_ONTOLOGY_EXPORT"),
        help="Private output path for the final getDefinition envelope",
    )
    bootstrap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the full offline plan without authentication, subprocess, network, or client construction",
    )
    bootstrap.add_argument("--azure-cli", type=Path, help="Explicit az executable path (live only)")
    bootstrap.add_argument(
        "--poll-timeout-seconds",
        type=int,
        default=int(os.environ.get("NORTHSTAR_DEPLOY_POLL_TIMEOUT", "1800")),
    )
    bootstrap.set_defaults(ontology_handler=ontology_from_args)
    return ontology


def ontology_from_args(args: argparse.Namespace) -> BootstrapResult:
    return run_bootstrap(options_from_args(args))


def options_from_args(args: argparse.Namespace) -> BootstrapOptions:
    return BootstrapOptions(
        workspace=getattr(args, "workspace", None),
        tenant_id=getattr(args, "tenant_id", None),
        expected_user=getattr(args, "expected_user", None),
        subscription_id=getattr(args, "subscription_id", None),
        deployment_tag=getattr(args, "deployment_tag", "m5-02"),
        deployment_journal=getattr(args, "deployment_journal", None),
        state_journal=getattr(args, "state_journal", None),
        export_path=getattr(args, "export_path", None),
        dry_run=getattr(args, "dry_run", False),
        azure_cli=getattr(args, "azure_cli", None),
        poll_timeout_seconds=getattr(args, "poll_timeout_seconds", 1800),
    )
