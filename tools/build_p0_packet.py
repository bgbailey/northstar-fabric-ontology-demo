"""Build a bounded synthetic notebook packet; never authenticate or deploy."""

import argparse
import base64
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOADER = '''import base64
import hashlib
import io
import json
import pyarrow.parquet as pq
import notebookutils
from pyspark.sql.types import StructType, StructField, LongType, StringType, BooleanType, DateType, TimestampType

context = notebookutils.runtime.context
if context["currentWorkspaceName"] != "ws-telco-iq-p0-20260929":
    raise RuntimeError("Notebook is not in the approved new P0 workspace")
if context["defaultLakehouseName"] != "lh_telcoiq_p0":
    raise RuntimeError("Approved P0 lakehouse must be attached")
spark.conf.set("spark.sql.session.timeZone", "UTC")
spark.conf.set("spark.databricks.delta.properties.defaults.enableDeletionVectors", "false")
spark.conf.set("spark.databricks.delta.properties.defaults.minReaderVersion", "1")
spark.conf.set("spark.databricks.delta.properties.defaults.minWriterVersion", "2")
# Test actual namespace resolution, not SHOW SCHEMAS display labels.
schema_name = "`ws-telco-iq-p0-20260929`.`lh_telcoiq_p0`.`dbo`"
existing_tables = {row["tableName"] for row in spark.sql("SHOW TABLES IN " + schema_name).collect()}
types = {"int64": LongType, "string": StringType, "bool": BooleanType,
         "date": DateType, "timestamp": TimestampType}
prepared = []
for asset in ASSETS:
    raw = base64.b64decode(asset["payload"], validate=True)
    if hashlib.sha256(raw).hexdigest() != asset["file_sha256"]:
        raise RuntimeError("Embedded synthetic file hash mismatch")
    arrow = pq.read_table(io.BytesIO(raw))
    if arrow.num_rows != asset["rows"]:
        raise RuntimeError("Embedded row count mismatch")
    schema = StructType([StructField(name, types[kind](), True) for name, kind in asset["columns"].items()])
    frame = spark.createDataFrame(arrow.to_pylist(), schema=schema)
    prepared.append((asset, frame))

receipts = []
for asset, frame in prepared:
    name = schema_name + ".`" + asset["table"] + "`"
    if asset["table"] in existing_tables:
        existing = spark.table(name).select(frame.columns)
        if frame.exceptAll(existing).limit(1).count() or existing.exceptAll(frame).limit(1).count():
            raise RuntimeError("Existing table differs; overwrite is not authorized: " + name)
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
    stored = spark.table(name).select(frame.columns)
    if stored.count() != asset["rows"] or frame.exceptAll(stored).limit(1).count() or stored.exceptAll(frame).limit(1).count():
        raise RuntimeError("Delta readback does not equal approved synthetic source: " + name)
    receipts.append({"table": name, "rows": asset["rows"], "state": state, "exactDataReadback": True})
print(json.dumps({"mode": "SYNTHETIC_LIVE_DELTA_LOAD", "tables": receipts}, sort_keys=True))
'''


def build(source: Path, output: Path) -> dict:
    catalog = json.loads((ROOT / "data" / "contracts" / "catalog.json").read_text())
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest["profile"] != "miniature":
        raise ValueError("P0 packet accepts miniature only; no full smoke/standard upload")
    selected = {"subscriptions", "finance"}
    names = sorted({t for m in catalog["models"] if m["id"] in selected for t in m["tables"]})
    tables = {t["id"]: t for t in catalog["tables"]}
    source_tables = {t["id"]: t for t in manifest["tables"]}
    assets = []
    for name in names:
        metadata = source_tables[name]
        raw = (source / metadata["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != metadata["file_sha256"]:
            raise ValueError("Source differs from miniature manifest")
        assets.append({
            "table": name, "rows": metadata["rows"], "columns": tables[name]["columns"],
            "file_sha256": metadata["file_sha256"], "payload": base64.b64encode(raw).decode(),
        })
    code = "ASSETS = json.loads(" + repr(json.dumps(assets, separators=(",", ":"))) + ")\n"
    cells = [
        {"cell_type": "code", "id": "synthetic-assets", "metadata": {}, "execution_count": None, "outputs": [],
         "source": ("import json\n" + code).splitlines(keepends=True)},
        {"cell_type": "code", "id": "bounded-delta-loader", "metadata": {}, "execution_count": None, "outputs": [],
         "source": LOADER.splitlines(keepends=True)},
    ]
    notebook = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "language_info": {"name": "python"},
        },
        "cells": cells,
    }
    output.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(notebook, indent=2) + "\n").encode()
    (output / "Load_TelcoIQ_Miniature.ipynb").write_bytes(content)
    model_packets = []
    for model in sorted(selected):
        model_root = ROOT / "fabric" / "semantic-models" / f"{model}.SemanticModel"
        files = [model_root / "definition.pbism", *sorted((model_root / "definition").rglob("*.tmdl"))]
        parts = [{
            "path": path.relative_to(model_root).as_posix(),
            "payload": base64.b64encode(path.read_bytes()).decode(),
            "payloadType": "InlineBase64",
        } for path in files]
        body = {
            "displayName": f"TelcoIQ_{model.title()}_P0",
            "definition": {"format": "TMDL", "parts": parts},
        }
        model_content = (json.dumps(body, indent=2) + "\n").encode()
        (output / f"{model}-template.json").write_bytes(model_content)
        model_packets.append({
            "model": model, "displayName": body["displayName"], "parts": len(parts),
            "templateSha256": hashlib.sha256(model_content).hexdigest(),
        })
    packet = {
        "status": "STAGED_NOT_DEPLOYED", "profile": "miniature",
        "workspaceName": "ws-telco-iq-p0-20260929", "lakehouseName": "lh_telcoiq_p0",
        "notebookName": "Load_TelcoIQ_Miniature",
        "notebookSha256": hashlib.sha256(content).hexdigest(),
        "notebookBytes": len(content), "sourceContentSha256": manifest["content_sha256"],
        "models": sorted(selected),
        "modelPackets": model_packets,
        "tables": [{"name": a["table"], "rows": a["rows"], "sourceFileSha256": a["file_sha256"]} for a in assets],
        "binding": "Inject only actually created/list-resolved lakehouse/workspace IDs into private notebook metadata",
        "liveExecution": "NOT_RUN",
    }
    (output / "packet.json").write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    return packet


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "fixtures" / "miniature")
    parser.add_argument("--output", type=Path, default=ROOT / ".local" / "p0-packet")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), indent=2))
