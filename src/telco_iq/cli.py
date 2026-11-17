from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import uvicorn

from telco_iq.api import create_app
from telco_iq.auth import ReaderAuth, ReaderConfig
from telco_iq.live_demo import ConfiguredLiveGateway

_DEFAULT_CREATE_APP = create_app
_DEFAULT_READER_AUTH = ReaderAuth


def build_parser() -> argparse.ArgumentParser:
    from telco_iq.deploy import build_deploy_parser
    from telco_iq.ontology import build_ontology_parser
    from telco_iq.teardown import build_teardown_parser

    parser = argparse.ArgumentParser(description="Local-only synthetic Telco IQ analyst lab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _build_serve_parser(subparsers)
    build_deploy_parser(subparsers)
    build_ontology_parser(subparsers)
    build_teardown_parser(subparsers)
    return parser


def _build_serve_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("serve", help="Serve the local analyst lab")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--data", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--reader-config", type=Path)
    modes.add_argument(
        "--recorded-native",
        type=Path,
        help="Explicit display-only replay of an actual native capture",
    )
    modes.add_argument(
        "--recorded-comparisons",
        type=Path,
        help="Explicit read-only playback of verified actual pairs",
    )
    modes.add_argument(
        "--recorded-library",
        type=Path,
        help="Explicit executive/full native demo playback; no live calls",
    )
    parser.add_argument(
        "--live-config",
        type=Path,
        help="Private reviewed live comparison wiring; requires --reader-config",
    )
    parser.add_argument(
        "--defer-live-config",
        action="store_true",
        help=(
            "Load the fixed --live-config only after an authenticated explicit setup POST; "
            "preserve the reader session"
        ),
    )
    parser.add_argument(
        "--source-observation-config",
        type=Path,
        help="Fixed private metadata-capture configuration; requires --reader-config and an explicit capture POST",
    )
    parser.set_defaults(serve_handler=serve_from_args, command_parser=parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        handler = getattr(args, f"{args.command}_handler", None)
    if handler is None:
        parser.error(f"{args.command} has no registered handler")
    result = handler(args)
    return result if isinstance(result, int) else 0


def serve_from_args(args: argparse.Namespace) -> Any:
    from telco_iq import api, auth

    parser: argparse.ArgumentParser = args.command_parser
    if args.live_config and not args.reader_config:
        parser.error("--live-config requires --reader-config and cannot select offline or recorded execution")
    if args.defer_live_config and not (args.reader_config and args.live_config):
        parser.error("--defer-live-config requires both --reader-config and a fixed private --live-config")
    if args.source_observation_config and not args.reader_config:
        parser.error("--source-observation-config requires --reader-config")
    reader_auth_factory = ReaderAuth
    if ReaderAuth is _DEFAULT_READER_AUTH and auth.ReaderAuth is not _DEFAULT_READER_AUTH:
        reader_auth_factory = auth.ReaderAuth
    reader_auth = (
        reader_auth_factory(ReaderConfig.read(args.reader_config, args.root))
        if args.reader_config
        else None
    )
    live = (
        ConfiguredLiveGateway.read(args.live_config, reader_auth, args.root)
        if args.live_config and not args.defer_live_config
        else None
    )
    app_factory = create_app
    if create_app is _DEFAULT_CREATE_APP and api.create_app is not _DEFAULT_CREATE_APP:
        app_factory = api.create_app
    # OAuth callback query strings must never enter access logs.
    return uvicorn.run(
        app_factory(
            args.root,
            args.data,
            reader_auth,
            args.recorded_native,
            args.port,
            live,
            deferred_live_config=args.live_config if args.defer_live_config else None,
            source_observation_config=args.source_observation_config,
            recorded_comparisons=args.recorded_comparisons,
            recorded_library=args.recorded_library,
        ),
        host="127.0.0.1",
        port=args.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
