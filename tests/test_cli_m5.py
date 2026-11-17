from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from telco_iq import cli


def test_help_lists_every_m5_command():
    help_text = cli.build_parser().format_help()

    assert "serve" in help_text
    assert "deploy" in help_text
    assert "ontology" in help_text
    assert "teardown" in help_text


@pytest.mark.parametrize(
    ("argv", "handler_path"),
    [
        (["deploy", "--dry-run"], "telco_iq.deploy.cli.deploy_from_args"),
        (
            ["ontology", "bootstrap", "--dry-run", "--journal", "private.json"],
            "telco_iq.ontology.cli.ontology_from_args",
        ),
        (["teardown", "--dry-run"], "telco_iq.teardown.teardown_from_args"),
    ],
)
def test_m5_commands_dispatch_the_registered_handler(monkeypatch, argv, handler_path):
    seen: list[Namespace] = []

    def handler(args: Namespace) -> int:
        seen.append(args)
        return 7

    monkeypatch.setattr(handler_path, handler)

    assert cli.main(argv) == 7
    assert len(seen) == 1
    assert seen[0].command == argv[0]


def test_no_command_remains_an_argparse_error():
    with pytest.raises(SystemExit) as error:
        cli.main([])

    assert error.value.code == 2


def test_serve_passes_live_arguments_to_the_existing_api(monkeypatch, tmp_path: Path):
    import uvicorn

    from telco_iq import api, auth, live_demo

    root = tmp_path / "root"
    data = tmp_path / "data.json"
    reader_config = tmp_path / "reader.json"
    live_config = tmp_path / "live.json"
    observation_config = tmp_path / "observation.json"
    reader_options = object()
    reader = object()
    live = object()
    app = object()
    calls: dict[str, Any] = {}

    def read_reader(path, base):
        calls["reader_config"] = (path, base)
        return reader_options

    def read_live(path, selected_auth, base):
        calls["live_config"] = (path, selected_auth, base)
        return live

    def create_app(*args, **kwargs):
        calls["create_app"] = (args, kwargs)
        return app

    def run(selected_app, **kwargs):
        calls["uvicorn"] = (selected_app, kwargs)

    monkeypatch.setattr(auth.ReaderConfig, "read", staticmethod(read_reader))
    monkeypatch.setattr(auth, "ReaderAuth", lambda options: reader if options is reader_options else None)
    monkeypatch.setattr(live_demo.ConfiguredLiveGateway, "read", staticmethod(read_live))
    monkeypatch.setattr(api, "create_app", create_app)
    monkeypatch.setattr(uvicorn, "run", run)

    assert cli.main([
        "serve",
        "--root", str(root),
        "--data", str(data),
        "--port", "9123",
        "--reader-config", str(reader_config),
        "--live-config", str(live_config),
        "--source-observation-config", str(observation_config),
    ]) == 0

    assert calls["reader_config"] == (reader_config, root)
    assert calls["live_config"] == (live_config, reader, root)
    assert calls["create_app"] == (
        (root, data, reader, None, 9123, live),
        {
            "deferred_live_config": None,
            "source_observation_config": observation_config,
            "recorded_comparisons": None,
            "recorded_library": None,
        },
    )
    assert calls["uvicorn"] == (
        app,
        {
            "host": "127.0.0.1",
            "port": 9123,
            "log_level": "info",
            "access_log": False,
        },
    )


def test_serve_preserves_recorded_mode_without_loading_auth(monkeypatch, tmp_path: Path):
    import uvicorn

    from telco_iq import api, auth, live_demo

    recording = tmp_path / "recorded.json"
    app = object()
    calls: dict[str, Any] = {}

    def forbidden(*args, **kwargs):
        pytest.fail("recorded serve attempted live or reader configuration")

    def create_app(*args, **kwargs):
        calls["create_app"] = (args, kwargs)
        return app

    def run(selected_app, **kwargs):
        calls["uvicorn"] = (selected_app, kwargs)

    monkeypatch.setattr(auth.ReaderConfig, "read", forbidden)
    monkeypatch.setattr(live_demo.ConfiguredLiveGateway, "read", forbidden)
    monkeypatch.setattr(api, "create_app", create_app)
    monkeypatch.setattr(uvicorn, "run", run)

    assert cli.main(["serve", "--recorded-library", str(recording), "--port", "9124"]) == 0

    args, kwargs = calls["create_app"]
    assert args == (Path.cwd(), None, None, None, 9124, None)
    assert kwargs == {
        "deferred_live_config": None,
        "source_observation_config": None,
        "recorded_comparisons": None,
        "recorded_library": recording,
    }
