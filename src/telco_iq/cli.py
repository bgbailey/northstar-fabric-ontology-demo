import argparse
from pathlib import Path

import uvicorn

from telco_iq.api import create_app
from telco_iq.auth import ReaderAuth, ReaderConfig
from telco_iq.live_demo import ConfiguredLiveGateway


def main() -> None:
    parser = argparse.ArgumentParser(description="Local-only synthetic Telco IQ analyst lab")
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--data", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--reader-config", type=Path)
    modes.add_argument("--recorded-native", type=Path, help="Explicit display-only replay of an actual native capture")
    modes.add_argument("--recorded-comparisons", type=Path, help="Explicit read-only playback of verified actual pairs")
    modes.add_argument("--recorded-library", type=Path, help="Explicit executive/full native demo playback; no live calls")
    parser.add_argument("--live-config", type=Path, help="Private reviewed live comparison wiring; requires --reader-config")
    parser.add_argument(
        "--defer-live-config", action="store_true",
        help="Load the fixed --live-config only after an authenticated explicit setup POST; preserve the reader session",
    )
    parser.add_argument(
        "--source-observation-config", type=Path,
        help="Fixed private metadata-capture configuration; requires --reader-config and an explicit capture POST",
    )
    args = parser.parse_args()
    if args.live_config and not args.reader_config:
        parser.error("--live-config requires --reader-config and cannot select offline or recorded execution")
    if args.defer_live_config and not (args.reader_config and args.live_config):
        parser.error("--defer-live-config requires both --reader-config and a fixed private --live-config")
    if args.source_observation_config and not args.reader_config:
        parser.error("--source-observation-config requires --reader-config")
    auth = ReaderAuth(ReaderConfig.read(args.reader_config, args.root)) if args.reader_config else None
    live = ConfiguredLiveGateway.read(args.live_config, auth, args.root) if (
        args.live_config and not args.defer_live_config
    ) else None
    # OAuth callback query strings must never enter access logs.
    uvicorn.run(create_app(
        args.root, args.data, auth, args.recorded_native, args.port, live,
        deferred_live_config=args.live_config if args.defer_live_config else None,
        source_observation_config=args.source_observation_config,
        recorded_comparisons=args.recorded_comparisons,
        recorded_library=args.recorded_library,
    ), host="127.0.0.1", port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
