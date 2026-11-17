from __future__ import annotations

import argparse
import os
from pathlib import Path

from .models import DeployOptions, DeployResult
from .runner import run_deploy


def build_deploy_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "deploy",
        help="Deploy the four-model Northstar service-assurance accelerator into an existing Fabric workspace",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("NORTHSTAR_FABRIC_WORKSPACE"),
        help="Existing workspace UUID or exact display name (env: NORTHSTAR_FABRIC_WORKSPACE)",
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
        help="Private deployment tag used in item descriptions and the journal",
    )
    parser.add_argument(
        "--journal",
        type=Path,
        default=_environment_path("NORTHSTAR_DEPLOY_JOURNAL"),
        help="Private journal path outside the repository and OneDrive (env: NORTHSTAR_DEPLOY_JOURNAL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the complete offline plan without authentication, subprocess, network, or generation",
    )
    parser.add_argument(
        "--azure-cli",
        type=Path,
        help="Explicit az executable path (live mode only)",
    )
    parser.add_argument(
        "--poll-timeout-seconds",
        type=int,
        default=int(os.environ.get("NORTHSTAR_DEPLOY_POLL_TIMEOUT", "1800")),
    )
    parser.set_defaults(deploy_handler=deploy_from_args)
    return parser


def deploy_from_args(args: argparse.Namespace) -> DeployResult:
    options = options_from_args(args)
    return run_deploy(options)


def options_from_args(args: argparse.Namespace) -> DeployOptions:
    return DeployOptions(
        workspace=args.workspace,
        tenant_id=args.tenant_id,
        expected_user=args.expected_user,
        subscription_id=os.environ.get("NORTHSTAR_FABRIC_SUBSCRIPTION_ID"),
        deployment_tag=args.deployment_tag,
        dry_run=args.dry_run,
        journal_path=args.journal,
        azure_cli=args.azure_cli,
        poll_timeout_seconds=args.poll_timeout_seconds,
    )


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None
