"""Opt-in real TOM parser tests; no source-service connection or DAX execution."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import build_models

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "contracts" / "catalog.json"
MODELS = ROOT / "fabric" / "semantic-models"
DLL = os.environ.get("TELCO_TMDL_CHECK_DLL")
pytestmark = pytest.mark.skipif(not DLL, reason="Explicit TELCO_TMDL_CHECK_DLL opts into the local .NET checker")


def run(models: Path, catalog: Path) -> subprocess.CompletedProcess:
    assert DLL and Path(DLL).is_file(), "Build the explicitly configured TOM checker before opting in"
    executable = shutil.which("dotnet")
    assert executable, "The explicitly enabled TOM check requires an installed .NET runtime"
    return subprocess.run(
        [executable, DLL, str(models), str(catalog)], capture_output=True, text=True, timeout=45,
        env={**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"},
    )


@pytest.fixture
def isolated(tmp_path):
    catalog = json.loads(CATALOG.read_bytes())
    catalog["models"] = [model for model in catalog["models"] if model["id"] == "subscriptions"]
    catalog["metrics"] = [metric for metric in catalog["metrics"] if metric["model"] == "subscriptions"]
    model_root = tmp_path / "models"
    folder = model_root / "subscriptions.SemanticModel"
    shutil.copytree(MODELS / "subscriptions.SemanticModel", folder)
    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_text(json.dumps(catalog), encoding="utf-8")
    return model_root, folder, catalog_file


def test_real_tom_deserializes_all_eight_models_and_matches_all_source_measure_definitions():
    result = run(MODELS, CATALOG)
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["status"] == "LOCAL_TOM_METADATA_VALIDATED_NOT_LIVE_DAX"
    assert len(report["models"]) == 8 and report["measures"] == 64
    assert all(model["status"] == "LOCAL_TOM_METADATA_VALIDATED" and model["measures"] == 8
               and not model["errors"] for model in report["models"])
    assert report["source_files_unchanged"] is True
    assert report["source_catalog_unchanged"] is True
    assert report["source_catalog_sha256"] == hashlib.sha256(CATALOG.read_bytes()).hexdigest()
    assert report["deployed"] is False and report["server_connections"] == 0 and report["live_dax"] == "NOT_RUN"
    assert report["source_files_sha256"] and report["limitations"]


def test_real_tom_accepts_a_fresh_eight_model_generator_output(tmp_path):
    model_root, contracts = tmp_path / "models", tmp_path / "contracts"
    build_models.build(model_root, contracts)
    result = run(model_root, contracts / "catalog.json")
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["status"] == "LOCAL_TOM_METADATA_VALIDATED_NOT_LIVE_DAX"
    assert len(report["models"]) == 8 and report["measures"] == 64
    assert report["source_files_unchanged"] is True and report["source_catalog_unchanged"] is True


@pytest.mark.parametrize("change", ["syntax", "expression", "source_column", "settings", "compatibility"])
def test_real_tom_or_source_contract_rejects_damaged_local_metadata(isolated, change):
    model_root, folder, catalog_file = isolated
    if change == "syntax":
        path = folder / "definition" / "database.tmdl"
        path.write_text("not_a_tmdl_object\n", encoding="utf-8")
    elif change == "expression":
        path = folder / "definition" / "tables" / "subscription_month.tmdl"
        metric = json.loads(catalog_file.read_bytes())["metrics"][0]
        text = path.read_text(encoding="utf-8")
        assert metric["dax"] in text
        path.write_text(text.replace(metric["dax"], metric["dax"] + " + 1", 1), encoding="utf-8")
    elif change == "source_column":
        path = folder / "definition" / "tables" / "subscription_month.tmdl"
        text = path.read_text(encoding="utf-8")
        assert "sourceColumn: active_eom" in text
        path.write_text(text.replace("sourceColumn: active_eom", "sourceColumn: invented_column", 1), encoding="utf-8")
    elif change == "settings":
        path = folder / "definition.pbism"
        settings = json.loads(path.read_bytes())
        settings["settings"]["qnaEnabled"] = False
        path.write_text(json.dumps(settings), encoding="utf-8")
    else:
        path = folder / "definition" / "database.tmdl"
        text = path.read_text(encoding="utf-8")
        assert "compatibilityLevel: 1702" in text
        path.write_text(text.replace("compatibilityLevel: 1702", "compatibilityLevel: 1703"), encoding="utf-8")
    before = path.read_bytes()
    result = run(model_root, catalog_file)
    assert result.returncode == 1, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["status"] == "FAILED"
    assert report["models"][0]["status"].startswith("FAILED")
    assert report["source_files_unchanged"] is True
    assert report["server_connections"] == 0 and report["deployed"] is False
    assert path.read_bytes() == before
    if change == "syntax":
        assert report["models"][0]["document"] and report["models"][0]["line"] >= 1
    else:
        assert report["models"][0]["errors"]


def test_model_alias_cannot_escape_the_local_model_root(isolated):
    model_root, _, catalog_file = isolated
    catalog = json.loads(catalog_file.read_bytes())
    catalog["models"][0]["id"] = "..\\outside"
    catalog_file.write_text(json.dumps(catalog), encoding="utf-8")
    result = run(model_root, catalog_file)
    assert result.returncode == 2
    assert "not paths" in result.stderr
    assert not result.stdout


def test_missing_arguments_are_an_explicit_cli_error():
    assert DLL
    executable = shutil.which("dotnet")
    assert executable
    result = subprocess.run(
        [executable, DLL], capture_output=True, text=True, timeout=45,
        env={**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"},
    )
    assert result.returncode == 2 and "Usage:" in result.stderr
    assert not result.stdout
