from __future__ import annotations

import importlib.resources
import importlib.util
import json
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

from .models import BootstrapError

SOURCE_ASSETS = {
    "assets/build_ontology_prompts.py": Path("tools/build_ontology_prompts.py"),
    "assets/check_ontology_conformance.py": Path("tools/check_ontology_conformance.py"),
    "assets/service-assurance.json": Path("fabric/ontology/service-assurance.json"),
    "assets/service-assurance.prompts.md": Path("fabric/ontology/service-assurance.prompts.md"),
}


def resource_path(relative: str) -> Path:
    resource = importlib.resources.files("telco_iq.ontology").joinpath(
        *PurePosixPath(relative).parts,
    )
    if not resource.is_file():
        source_relative = SOURCE_ASSETS.get(relative)
        source_root = Path(__file__).resolve().parents[3]
        source = source_root / source_relative if source_relative is not None else None
        if source is not None and source.is_file():
            return source
        raise BootstrapError(f"Required packaged ontology asset is missing: {relative}")
    path = Path(str(resource))
    if not path.is_file():
        raise BootstrapError(f"Packaged ontology asset must be an installed file: {relative}")
    return path


def contract_path() -> Path:
    return resource_path("assets/service-assurance.json")


def load_contract() -> dict[str, Any]:
    value = json.loads(contract_path().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BootstrapError("Packaged ontology contract is not an object")
    return value


def load_asset_module(relative: str, module_name: str) -> ModuleType:
    path = resource_path(relative)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise BootstrapError(f"Cannot load packaged ontology asset: {relative}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
