from __future__ import annotations

import base64
import hashlib
import importlib.resources
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Iterable

from .models import HERO_MODELS, PROFILE, SEED, DeployError, canonical_id


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_iso, allow_nan=False).encode(
        "utf-8",
    )


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SourceAsset:
    table: str
    columns: dict[str, str]
    primary_key: tuple[str, ...]
    rows: int
    file_sha256: str
    content_sha256: str
    payload: str
    bytes: int

    def notebook_value(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "columns": self.columns,
            "primaryKey": list(self.primary_key),
            "rows": self.rows,
            "file_sha256": self.file_sha256,
            "content_sha256": self.content_sha256,
            "payload": self.payload,
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class AssetBundle:
    manifest: dict[str, Any]
    tables: tuple[SourceAsset, ...]
    model_parts: dict[str, dict[str, bytes]]
    source_digest: str


def _resource_path(relative: str) -> Path:
    resource = importlib.resources.files("telco_iq.deploy").joinpath(*PurePosixPath(relative).parts)
    if not resource.is_file():
        raise DeployError(f"Required packaged deployment asset is missing: {relative}")
    path = Path(str(resource))
    if not path.is_file():
        raise DeployError(f"Packaged deployment asset must be an installed file: {relative}")
    return path


def _resource_files(directory: Path) -> Iterable[Path]:
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            yield path


def _load_builder() -> ModuleType:
    path = _resource_path("assets/build_data.py")
    spec = importlib.util.spec_from_file_location("telco_iq._deploy_build_data", path)
    if spec is None or spec.loader is None:
        raise DeployError("Cannot load the packaged data generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_model_parts() -> dict[str, dict[str, bytes]]:
    models: dict[str, dict[str, bytes]] = {}
    for alias in HERO_MODELS:
        root = _resource_path(f"assets/semantic-models/{alias}.SemanticModel/definition.pbism").parent
        parts: dict[str, bytes] = {}
        files = [
            root / "definition.pbism",
            *(path for path in _resource_files(root / "definition") if path.suffix.lower() == ".tmdl"),
        ]
        for path in files:
            relative = path.relative_to(root).as_posix()
            parts[relative] = path.read_bytes()
        required = {
            "definition.pbism",
            "definition/database.tmdl",
            "definition/model.tmdl",
            "definition/expressions.tmdl",
        }
        if not required <= parts.keys():
            raise DeployError(f"Incomplete packaged model definition: {alias}")
        expressions = parts["definition/expressions.tmdl"]
        if expressions.count(b"<WORKSPACE_ID>") != 1 or expressions.count(b"<LAKEHOUSE_ID>") != 1:
            raise DeployError(f"Unexpected OneLake binding placeholders in {alias}")
        models[alias] = parts
    return models


def build_assets(working_directory: Path) -> AssetBundle:
    builder = _load_builder()
    models = load_model_parts()
    required_tables = {
        PurePosixPath(path).stem
        for parts in models.values()
        for path in parts
        if path.startswith("definition/tables/") and path.endswith(".tmdl")
    }
    output = working_directory / "generated"
    manifest = builder.build(PROFILE, SEED, output)
    if manifest.get("profile") != PROFILE or manifest.get("seed") != SEED:
        raise DeployError("Packaged source generator returned an unexpected profile or seed")
    if manifest.get("version") != "service-assurance-v1.0":
        raise DeployError("Service-assurance source revision is not v1.0")
    metadata = {item["id"]: item for item in builder.source_tables(PROFILE)}
    generated = {item["id"]: item for item in manifest["tables"]}
    if required_tables - generated.keys():
        raise DeployError("Generated service-assurance data is missing required model tables")
    assets = []
    for name in sorted(required_tables):
        spec = metadata[name]
        item = generated[name]
        raw = (output / item["file"]).read_bytes()
        if len(raw) != item["bytes"] or sha256(raw) != item["file_sha256"]:
            raise DeployError(f"Generated Parquet bytes/hash changed: {name}")
        assets.append(
            SourceAsset(
                table=name,
                columns=dict(spec["columns"]),
                primary_key=tuple(spec["primaryKey"]),
                rows=item["rows"],
                file_sha256=item["file_sha256"],
                content_sha256=item["content_sha256"],
                payload=base64.b64encode(raw).decode("ascii"),
                bytes=len(raw),
            ),
        )
    if len(assets) != len(required_tables):
        raise DeployError("Selected table census is ambiguous")
    selected_manifest = {
        "profile": PROFILE,
        "seed": SEED,
        "revision": manifest["version"],
        "asOf": manifest["as_of"],
        "tables": [
            {
                "name": asset.table,
                "rows": asset.rows,
                "bytes": asset.bytes,
                "fileSha256": asset.file_sha256,
                "contentSha256": asset.content_sha256,
                "schemaSha256": sha256(canonical(asset.columns)),
            }
            for asset in assets
        ],
    }
    digest = sha256(
        canonical(
            {
                "manifest": selected_manifest,
                "models": {
                    alias: {path: sha256(raw) for path, raw in sorted(parts.items())}
                    for alias, parts in models.items()
                },
            },
        ),
    )
    return AssetBundle(manifest=selected_manifest, tables=tuple(assets), model_parts=models, source_digest=digest)


def bind_definition_parts(
    parts: dict[str, bytes],
    workspace_id: str,
    lakehouse_id: str,
) -> dict[str, bytes]:
    canonical_id(workspace_id, "workspace ID")
    canonical_id(lakehouse_id, "lakehouse ID")
    bound: dict[str, bytes] = {}
    workspace_count = 0
    lakehouse_count = 0
    for path, raw in parts.items():
        if not isinstance(path, str) or not path or path.startswith(("/", "\\")) or "\\" in path:
            raise DeployError("Definition part path is not canonical")
        workspace_count += raw.count(b"<WORKSPACE_ID>")
        lakehouse_count += raw.count(b"<LAKEHOUSE_ID>")
        value = raw.replace(b"<WORKSPACE_ID>", workspace_id.encode("ascii"))
        value = value.replace(b"<LAKEHOUSE_ID>", lakehouse_id.encode("ascii"))
        if b"<WORKSPACE_ID>" in value or b"<LAKEHOUSE_ID>" in value:
            raise DeployError(f"Unresolved binding placeholder in {path}")
        bound[path] = value
    if workspace_count != 1 or lakehouse_count != 1:
        raise DeployError("Every model must contain exactly one of each deployment placeholder")
    if bound == parts:
        raise DeployError("Binding did not change the source definition")
    return bound


def definition_digest(parts: dict[str, bytes]) -> str:
    return sha256(canonical({path: sha256(raw) for path, raw in sorted(parts.items())}))


def encode_definition(parts: dict[str, bytes]) -> dict[str, Any]:
    return {
        "format": "TMDL",
        "parts": [
            {
                "path": path,
                "payload": base64.b64encode(parts[path]).decode("ascii"),
                "payloadType": "InlineBase64",
            }
            for path in sorted(parts)
        ],
    }


def decode_definition(value: dict[str, Any]) -> dict[str, bytes]:
    if value.get("format") != "TMDL" or not isinstance(value.get("parts"), list):
        raise DeployError("Semantic model readback is not a complete TMDL definition")
    result: dict[str, bytes] = {}
    for entry in value["parts"]:
        path = entry.get("path")
        if (
            not isinstance(path, str)
            or path in result
            or path.startswith(("/", "\\"))
            or "\\" in path
            or entry.get("payloadType") != "InlineBase64"
        ):
            raise DeployError("Semantic model readback contains an invalid definition part")
        try:
            result[path] = base64.b64decode(entry["payload"], validate=True)
        except (KeyError, ValueError, TypeError) as error:
            raise DeployError("Semantic model readback contains invalid base64") from error
    return result


def loader_source() -> str:
    return """import base64
import hashlib
import io
import json
import pyarrow.parquet as pq
import notebookutils
from pyspark.sql.types import StructType, StructField, LongType, StringType, BooleanType, DateType, TimestampType

context = notebookutils.runtime.context
WORKSPACE_NAME = <WORKSPACE_NAME>
LAKEHOUSE_NAME = <LAKEHOUSE_NAME>
if context["currentWorkspaceId"] != "<WORKSPACE_ID>":
    raise RuntimeError("Notebook is not in the exact deployment workspace")
if context["currentWorkspaceName"] != WORKSPACE_NAME:
    raise RuntimeError("Notebook workspace display name differs")
if context["defaultLakehouseName"] != LAKEHOUSE_NAME:
    raise RuntimeError("Exact deployment lakehouse must be attached")
spark.conf.set("spark.sql.session.timeZone", "UTC")
spark.conf.set("spark.databricks.delta.properties.defaults.enableDeletionVectors", "false")
spark.conf.set("spark.databricks.delta.properties.defaults.minReaderVersion", "1")
spark.conf.set("spark.databricks.delta.properties.defaults.minWriterVersion", "2")
quoted_schema = ".".join(
    "`" + part.replace("`", "``") + "`" for part in (WORKSPACE_NAME, LAKEHOUSE_NAME, "dbo")
)
existing_tables = {row["tableName"] for row in spark.sql("SHOW TABLES IN " + quoted_schema).collect()}
types = {
    "int64": (LongType, "bigint"),
    "string": (StringType, "string"),
    "bool": (BooleanType, "boolean"),
    "date": (DateType, "date"),
    "timestamp": (TimestampType, "timestamp"),
}
arrow_types = {
    "int64": "int64",
    "string": "string",
    "bool": "bool",
    "date": "date32[day]",
    "timestamp": "timestamp[us, tz=+00:00]",
}

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=lambda item: item.isoformat())

prepared = []
for asset in ASSETS:
    raw = base64.b64decode(asset["payload"], validate=True)
    if hashlib.sha256(raw).hexdigest() != asset["file_sha256"] or len(raw) != asset["bytes"]:
        raise RuntimeError("Embedded synthetic Parquet bytes/hash mismatch: " + asset["table"])
    arrow = pq.read_table(io.BytesIO(raw))
    observed_arrow_types = [str(field.type) for field in arrow.schema]
    expected_arrow_types = [arrow_types[kind] for kind in asset["columns"].values()]
    if (
        arrow.num_rows != asset["rows"]
        or arrow.column_names != list(asset["columns"])
        or observed_arrow_types != expected_arrow_types
    ):
        raise RuntimeError("Embedded synthetic schema/row mismatch: " + asset["table"])
    rows = arrow.to_pylist()
    logical = hashlib.sha256(canonical(asset["columns"]).encode())
    for row in rows:
        logical.update(b"\\n" + canonical(row).encode())
    if logical.hexdigest() != asset["content_sha256"]:
        raise RuntimeError("Embedded synthetic logical content hash mismatch: " + asset["table"])
    keys = [tuple(row[column] for column in asset["primaryKey"]) for row in rows]
    if any(None in key for key in keys) or len(keys) != len(set(keys)):
        raise RuntimeError("Embedded synthetic primary key check failed: " + asset["table"])
    schema = StructType([
        StructField(name, types[kind][0](), name not in asset["primaryKey"])
        for name, kind in asset["columns"].items()
    ])
    prepared.append((asset, spark.createDataFrame(rows, schema=schema)))

receipts = []
for asset, frame in prepared:
    name = quoted_schema + ".`" + asset["table"].replace("`", "``") + "`"
    if asset["table"] in existing_tables:
        state = "EXISTING_EXACT_MATCH"
    else:
        (
            frame.write.format("delta")
            .option("delta.enableDeletionVectors", "false")
            .option("delta.minReaderVersion", "1")
            .option("delta.minWriterVersion", "2")
            .mode("errorifexists")
            .saveAsTable(name)
        )
        state = "CREATED"
    stored = spark.table(name)
    observed_schema = [(field.name, field.dataType.simpleString()) for field in stored.schema.fields]
    expected_schema = [(name, types[kind][1]) for name, kind in asset["columns"].items()]
    if observed_schema != expected_schema:
        raise RuntimeError("Delta schema differs; no overwrite is authorized: " + name)
    selected = stored.select(frame.columns)
    if (
        selected.count() != asset["rows"]
        or frame.exceptAll(selected).limit(1).count()
        or selected.exceptAll(frame).limit(1).count()
    ):
        raise RuntimeError("Delta readback differs; no overwrite is authorized: " + name)
    receipts.append({
        "table": name,
        "rows": asset["rows"],
        "state": state,
        "schemaSha256": hashlib.sha256(canonical(asset["columns"]).encode()).hexdigest(),
        "contentSha256": asset["content_sha256"],
        "exactDataReadback": True,
    })
print(json.dumps({"mode": "SYNTHETIC_DELTA_LOAD", "tables": receipts}, sort_keys=True))
"""


def bind_notebook(
    bundle: AssetBundle,
    *,
    workspace_id: str,
    workspace_name: str,
    lakehouse_id: str,
    lakehouse_name: str,
) -> bytes:
    code = loader_source()
    replacements = {
        "<WORKSPACE_ID>": (workspace_id, 1),
        "<WORKSPACE_NAME>": (repr(workspace_name), 1),
        "<LAKEHOUSE_NAME>": (repr(lakehouse_name), 1),
    }
    for token, (replacement, expected_count) in replacements.items():
        if code.count(token) != expected_count:
            raise DeployError(f"Notebook loader template has unexpected {token} count")
        code = code.replace(token, replacement)
    if any(token in code for token in ("<WORKSPACE_ID>", "<WORKSPACE_NAME>", "<LAKEHOUSE_NAME>")):
        raise DeployError("Notebook binding left a placeholder")
    assets = json.dumps([asset.notebook_value() for asset in bundle.tables], separators=(",", ":"))
    cells = [
        {
            "cell_type": "code",
            "id": "synthetic-assets",
            "metadata": {},
            "execution_count": None,
            "outputs": [],
            "source": ("import json\nASSETS = json.loads(" + repr(assets) + ")\n").splitlines(keepends=True),
        },
        {
            "cell_type": "code",
            "id": "bounded-delta-loader",
            "metadata": {},
            "execution_count": None,
            "outputs": [],
            "source": code.splitlines(keepends=True),
        },
    ]
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "cells": cells,
        "metadata": {
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "language_info": {"name": "python"},
            "dependencies": {
                "lakehouse": {
                    "default_lakehouse": lakehouse_id,
                    "default_lakehouse_workspace_id": workspace_id,
                    "default_lakehouse_name": lakehouse_name,
                },
            },
        },
    }
    return (json.dumps(notebook, indent=2, allow_nan=False) + "\n").encode("utf-8")


def notebook_definition(value: bytes) -> dict[str, Any]:
    return {
        "format": "ipynb",
        "parts": [
            {
                "path": "notebook-content.ipynb",
                "payload": base64.b64encode(value).decode("ascii"),
                "payloadType": "InlineBase64",
            },
        ],
    }


def decode_notebook_definition(value: dict[str, Any]) -> bytes:
    if value.get("format") != "ipynb" or len(value.get("parts", ())) != 1:
        raise DeployError("Notebook readback is not a single ipynb definition")
    part = value["parts"][0]
    if part.get("path") != "notebook-content.ipynb" or part.get("payloadType") != "InlineBase64":
        raise DeployError("Notebook readback has an unexpected definition part")
    try:
        return base64.b64decode(part["payload"], validate=True)
    except (KeyError, TypeError, ValueError) as error:
        raise DeployError("Notebook readback contains invalid base64") from error
