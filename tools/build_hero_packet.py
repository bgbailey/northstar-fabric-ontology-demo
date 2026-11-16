"""Build an isolated synthetic smoke hero packet or bind a copy locally; never deploy.

Build: python -m tools.build_hero_packet --source data\\generated\\smoke
Showcase: add --showcase --model-root <generated-models> --catalog <generated-catalog>
          --source <showcase-v2-data> --output <new-directory>
Eight domains: add --all-models; the original three-model default is unchanged.
Bind:  python -m tools.build_hero_packet --bind-packet <packet> --binding <private-json>
       --output <new-private-directory>
Binding JSON: packetSha256 plus workspace/lakehouse objects with id and displayName
from actual approved create/readback receipts. Binding itself grants no approval.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

if __package__:
    from .build_p0_packet import LOADER
else:
    from build_p0_packet import LOADER

ROOT = Path(__file__).resolve().parents[1]
HERO_MODELS = ("enterprise", "finance", "network")
ALL_MODELS = ("subscriptions", "finance", "digital", "care", "network", "commerce", "growth", "enterprise")
WORKSPACE = "<WORKSPACE_NAME>"
GUIDANCE_COMMIT = "6c11ad58c25992e5d1435ce7cd80d217d5598a31"


def encoded(value) -> bytes:
    return (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=lambda item: item.isoformat(), allow_nan=False).encode()


def part(path: str, raw: bytes) -> dict:
    return {"path": path, "payloadType": "InlineBase64", "payload": base64.b64encode(raw).decode()}


def read_packet(packet_dir: Path) -> dict:
    packet = json.loads((packet_dir / "packet.json").read_text(encoding="utf-8"))
    if packet["schemaVersion"] != "hero-source-packet-v1":
        raise ValueError("Unsupported hero packet")
    for asset in packet["packetFiles"]:
        path = (packet_dir / asset["file"]).resolve()
        if not path.is_relative_to(packet_dir.resolve()):
            raise ValueError("Packet file escapes its directory")
        raw = path.read_bytes()
        if len(raw) != asset["bytes"] or digest(raw) != asset["sha256"]:
            raise ValueError("Staged hero artifact changed: " + asset["file"])
    return packet


def _financial_quality(tables: dict, *, variation: bool = False) -> dict:
    """A local source-quality warning, not governed measure execution or an answer."""
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    contracts = {row["contract_key"]: row for row in tables["contract"].to_pylist()}
    sites = {row["site_key"] for row in tables["site"].to_pylist()}
    mappings = {}
    for row in tables["service_site_map"].to_pylist():
        mappings.setdefault(row["site_key"], []).append(row)
    accounts = set()
    for incident in tables["incident"].to_pylist():
        if incident["site_key"] not in sites or incident["start"] >= end or incident["end"] <= start:
            continue
        for mapping in mappings.get(incident["site_key"], []):
            contract = contracts.get(mapping["contract_key"])
            if contract is None:
                continue
            if max(start, incident["start"], mapping["valid_from"], contract["valid_from"]) < min(
                end, incident["end"], mapping["valid_to"], contract["valid_to"],
            ):
                accounts.add(contract["account_key"])
    finance = [row for row in tables["account_month"].to_pylist()
               if row["account_key"] in accounts and row["month"] == date(2026, 7, 1)]
    missing = accounts - {row["account_key"] for row in finance}
    zero = sum(row["revenue_cents"] == 0 for row in finance)
    nonpositive = sum(
        row["revenue_cents"] <= row["credit_cents"] + row["service_cost_cents"] for row in finance
    )
    warnings = []
    if not accounts:
        warnings.append("July has no affected account cohort; not a useful first hero without scenario review.")
    if missing:
        warnings.append("Selected accounts lack July Finance rows; absence is not zero.")
    if zero or nonpositive:
        warnings.append("Selected financials include zero revenue/nonpositive margins; preserve source values.")
    quality = {
        "status": "LOCAL_SOURCE_QUALITY_ONLY", "period": "2026-07",
        "cohort": "All July incidents, registered sites, half-open mapping and contract validity intersection",
        "selectedAccounts": len(accounts), "missingFinanceAccounts": len(missing),
        "zeroRevenueAccounts": zero, "nonpositiveMarginAccounts": nonpositive,
        "warnings": warnings, "liveSourceMeasureValidation": "NOT_RUN",
    }
    if variation:
        margins = [row["revenue_cents"] - row["credit_cents"] - row["service_cost_cents"] for row in finance]
        quality["marginSigns"] = {
            "positive": sum(value > 0 for value in margins), "zero": margins.count(0),
            "negative": sum(value < 0 for value in margins),
        }
    return quality


def build(
    source: Path, output: Path, *, include_subscriptions: bool = False,
    all_models: bool = False,
    showcase: bool = False, model_root: Path | None = None, catalog_path: Path | None = None,
) -> dict:
    if output.exists():
        raise FileExistsError("Hero output already exists; choose a new directory, never replace prior proof")
    if all_models and include_subscriptions:
        raise ValueError("Choose all eight models or the hero-plus-subscriptions scope, not both")
    source = source.resolve()
    manifest_raw = (source / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    expected_profile = "showcase-v2" if showcase else "smoke"
    if manifest["profile"] != expected_profile:
        raise ValueError(f"The isolated hero packet requires one {expected_profile} revision; miniature mixing is forbidden")
    if not showcase and (model_root is not None or catalog_path is not None):
        raise ValueError("Custom source model/catalog paths require explicit showcase mode")
    if showcase and (model_root is None or catalog_path is None):
        raise ValueError("Showcase requires explicit --model-root and --catalog from the same versioned build")
    if showcase and manifest.get("version") != "showcase-v2.3":
        raise ValueError("Showcase requires the diagnostic showcase-v2.3 source revision")
    catalog_path = catalog_path or ROOT / "data" / "contracts" / "catalog.json"
    model_root = model_root or ROOT / "fabric" / "semantic-models"
    catalog_raw = catalog_path.read_bytes()
    catalog = json.loads(catalog_raw)
    if showcase:
        census = json.loads((model_root / "census.json").read_bytes())
        if (census.get("source_manifest_sha256") != digest(manifest_raw) or
                census.get("population") != manifest.get("population") or
                census.get("catalog_sha256") != digest(catalog_raw)):
            raise ValueError("Source model population/manifest differs from selected showcase revision")
    selected = sorted(ALL_MODELS if all_models else [
        *HERO_MODELS, *(["subscriptions"] if include_subscriptions else []),
    ])
    models = [model for model in catalog["models"] if model["id"] in selected]
    if sorted(model["id"] for model in models) != selected:
        raise ValueError("Required hero models are absent from the source catalog")
    names = sorted({name for model in models for name in model["tables"]})
    definitions = {table["id"]: table for table in catalog["tables"]}
    if "quarantine" in names or any(
        column.casefold().startswith(("latent_", "gold_", "oracle_", "evaluator_"))
        for name in names for column in definitions[name]["columns"]
    ):
        raise ValueError("Source deployment cannot package evaluator-only tables or columns")
    metadata = {table["id"]: table for table in manifest["tables"]}
    if len(metadata) != len(manifest["tables"]) or digest(canonical({
        table["id"]: table["content_sha256"] for table in manifest["tables"]
    })) != manifest["content_sha256"]:
        raise ValueError("Source manifest revision is inconsistent")
    assets, source_tables = [], {}
    for name in names:
        item = metadata[name]
        path = (source / item["file"]).resolve()
        if not path.is_relative_to(source) or path.name != name + ".parquet":
            raise ValueError("Source table must be in the single selected revision directory")
        raw = path.read_bytes()
        if len(raw) != item["bytes"] or digest(raw) != item["file_sha256"]:
            raise ValueError("Source table bytes/hash differ from the smoke manifest: " + name)
        table = pq.read_table(io.BytesIO(raw))
        columns = definitions[name]["columns"]
        if table.num_rows != item["rows"] or table.column_names != list(columns):
            raise ValueError("Source table rows/schema differ from the full model contract: " + name)
        logical = hashlib.sha256(canonical(columns))
        for row in table.to_pylist():
            logical.update(b"\n" + canonical(row))
        if logical.hexdigest() != item["content_sha256"]:
            raise ValueError("Source table content is from another revision: " + name)
        source_tables[name] = table
        assets.append({
            "table": name, "rows": item["rows"], "bytes": len(raw), "columns": columns,
            "file_sha256": item["file_sha256"], "content_sha256": item["content_sha256"],
            "payload": base64.b64encode(raw).decode(),
        })
    model_parts, template_inventory = {}, []
    for model in models:
        folder = model_root / f"{model['id']}.SemanticModel"
        files = [folder / "definition.pbism", *sorted((folder / "definition").rglob("*"))]
        files = [path for path in files if path.is_file()]
        for optional in (".platform", "diagramLayout.json"):
            if (folder / optional).exists():
                files.append(folder / optional)
        if {path.stem for path in files if path.parent.name == "tables"} != set(model["tables"]):
            raise ValueError("Full generated TMDL table membership differs from catalog: " + model["id"])
        parts = [part(path.relative_to(folder).as_posix(), path.read_bytes()) for path in files]
        required = {"definition.pbism", "definition/database.tmdl", "definition/model.tmdl",
                    "definition/expressions.tmdl"}
        if not required <= {entry["path"] for entry in parts}:
            raise ValueError("Incomplete source model envelope")
        model_parts[model["id"]] = parts
        template_inventory.extend({
            "model": model["id"], "path": entry["path"],
            "sha256": digest(base64.b64decode(entry["payload"])),
        } for entry in parts)
        if showcase:
            expected = next(item["definition_parts"] for item in census["models"] if item["id"] == model["id"])
            actual = [{"path": entry["path"], "sha256": digest(base64.b64decode(entry["payload"]))}
                      for entry in parts]
            if actual != expected:
                raise ValueError("Complete source model parts differ from selected population census")
    revision = digest(canonical({
        "sourceManifestSha256": digest(manifest_raw), "catalogSha256": digest(catalog_raw),
        "models": template_inventory,
    }))
    suffix = ("ShowcaseV2_" if showcase else "Smoke_") + revision[:10]
    lakehouse = "lh_telcoiq_hero_" + suffix.lower()
    notebook_name = "Load_TelcoIQ_Hero_" + suffix
    model_names = {model: f"TelcoIQ_{model.title()}_Hero_{suffix}" for model in selected}
    loader = LOADER.replace("ws-telco-iq-p0-20260929", WORKSPACE).replace("lh_telcoiq_p0", lakehouse)
    loader = loader.replace("approved new P0 workspace", "approved hero workspace").replace(
        "Approved P0 lakehouse", "Approved isolated hero lakehouse",
    )
    cells = [{
        "cell_type": "code", "id": "synthetic-assets", "metadata": {},
        "execution_count": None, "outputs": [],
        "source": ("import json\nASSETS = json.loads(" + repr(json.dumps(assets)) + ")\n").splitlines(True),
    }, {
        "cell_type": "code", "id": "bounded-delta-loader", "metadata": {},
        "execution_count": None, "outputs": [], "source": loader.splitlines(True),
    }]
    notebook = {
        "nbformat": 4, "nbformat_minor": 5, "cells": cells,
        "metadata": {
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "language_info": {"name": "python"},
            "dependencies": {"lakehouse": {
                "default_lakehouse": "<LAKEHOUSE_ID>",
                "default_lakehouse_workspace_id": "<WORKSPACE_ID>", "default_lakehouse_name": lakehouse,
            }},
        },
    }
    files = {
        "source-manifest.json": manifest_raw,
        "source-catalog.json": encoded({
            "version": catalog["version"], "models": models,
            "tables": [definitions[name] for name in names],
            "metrics": [metric for metric in catalog["metrics"] if metric["model"] in selected],
        }),
        "loader.ipynb": encoded(notebook),
        "lakehouse-template.json": encoded({
            "displayName": lakehouse, "type": "Lakehouse", "creationPayload": {"enableSchemas": True},
        }),
        "notebook-template.json": encoded({
            "displayName": notebook_name, "type": "Notebook",
            "definition": {"format": "ipynb", "parts": [part("notebook-content.ipynb", encoded(notebook))]},
        }),
    }
    model_packets = []
    for model in selected:
        file = f"{model}-template.json"
        files[file] = encoded({
            "displayName": model_names[model], "definition": {"format": "TMDL", "parts": model_parts[model]},
        })
        model_packets.append({
            "alias": model, "displayName": model_names[model], "file": file,
            "parts": len(model_parts[model]), "sha256": digest(files[file]),
        })
    packet = {
        "schemaVersion": "hero-source-packet-v1", "status": "STAGED_UNAPPROVED", "approved": False,
        "profile": manifest["profile"], "sourceContentSha256": manifest["content_sha256"],
        "sourceManifestSha256": digest(manifest_raw), "catalogSha256": digest(catalog_raw),
        "packetRevision": revision, "dataClocks": manifest["clocks"]["data"],
        "isolation": "NEW_OWNED_LAKEHOUSE_NOTEBOOK_MODELS; do not update or adopt historical P0 items",
        "aliases": {"workspace": "hero-workspace", "lakehouse": "hero-lakehouse", "notebook": "hero-loader"},
        "workspaceName": WORKSPACE, "lakehouseName": lakehouse, "notebookName": notebook_name,
        "models": selected, "modelPackets": model_packets,
        "domainScope": "ALL_EIGHT_CUSTOMER_DOMAINS" if all_models else "SLA_HERO",
        "modelDependencies": {model["id"]: model["tables"] for model in models},
        "sharedDependencies": sorted(name for name in names
                                     if sum(name in model["tables"] for model in models) > 1),
        "tables": [{
            "name": asset["table"], "rows": asset["rows"], "bytes": asset["bytes"],
            "fileSha256": asset["file_sha256"], "contentSha256": asset["content_sha256"],
        } for asset in assets],
        "rows": sum(asset["rows"] for asset in assets), "sourceBytes": sum(asset["bytes"] for asset in assets),
        "executionLimits": {"maximumNotebookRuns": 1, "maximumNotebookSeconds": None,
                            "note": "Select/approve runtime and cancellation scope before execution"},
        "sourceModelParts": template_inventory, "sourceMeasures": "UNCHANGED_FROM_GENERATED_TMDL",
        "financialQuality": _financial_quality(source_tables, variation=showcase),
        "binding": {
            "status": "UNRESOLVED_PRIVATE_STEP", "placeholders": ["<WORKSPACE_ID>", "<LAKEHOUSE_ID>", WORKSPACE],
            "interface": "bind(packet_dir, binding, output); exact packetSha256 and actual workspace/lakehouse receipts",
            "review": "Bound JSON body digests require separate operation approval before any request",
        },
        "proposedReads": [
            "Exact approved workspace/item readback; name-collision listing in that workspace only",
            "Only accepted-operation LRO/job status and results; full definition readback",
            "Exact Delta counts and bidirectional exceptAll against every embedded synthetic table",
            "Separately scoped initial source refresh/readback and selected source-measure reconciliation",
        ],
        "proposedWrites": [
            "Create one new schema-enabled lakehouse and one new notebook under the resolved workspace",
            "One bounded notebook run, errorifexists tables only; existing tables must be exact matches",
            "Create complete new semantic models listed in modelPackets; no historical item updates",
        ],
        "ttlTeardown": {
            "ttlSeconds": None, "absoluteDeadlineUtc": None, "owner": None,
            "approvalMode": "USAGE_SCOPED",
            "mechanism": "Finite approved operations and event-based cancellation/teardown of receipt-owned new items only",
            "responsibilities": [
                "Author records all accepted operations and created IDs; never re-submit ambiguous mutations",
                "Stop on consumed operation/request/spend caps, revocation, changed target/artifact or invalid actual credentials",
                "Demo owner cancels receipt-owned notebook jobs at the approved runtime bound; retain accepted demo items until separately scoped teardown",
                "Retain prior two-model/ontology proof; no automatic shared-capacity pause, resize, reassignment or billing stop",
            ],
        },
        "modelProviderProposal": "infra/azure-openai-proposal.example.json",
        "guidance": {
            "commit": GUIDANCE_COMMIT,
            "paths": [
                "skills/semantic-model-authoring/SKILL.md",
                "skills/semantic-model-authoring/references/direct-lake-guidelines.md",
                "common/ITEM-DEFINITIONS-CORE.md",
            ],
            "freshness": "Cached baseline only; no reference refresh/network authorized in this local lane",
            "projectContracts": [
                "tools/build_p0_packet.py", "tools/build_models.py",
                "docs/executive-native-setup.md",
            ],
        },
        "evidenceGaps": [
            "Actual resource/price/cost cap/event-based teardown and exact DEV targets are unselected blockers, not zero cost",
            "No cloud create/load/refresh/source DAX, ontology generation, rule application or model call ran",
            "AI preparation/native source generation belong to parent/native lanes; no fabricated API",
        ],
        "packetFiles": [{"file": name, "bytes": len(raw), "sha256": digest(raw)}
                        for name, raw in sorted(files.items())],
        "liveExecution": "NOT_RUN",
    }
    if showcase:
        packet["population"] = manifest["population"]
        packet["structuralManifestHash"] = digest(canonical(manifest))
        packet["generatorSha256"] = manifest["generator_sha256"]
        packet["guidance"]["commit"] = "bc42aaf734771bf752c1d7082dd91e5a6ca72b9f"
        packet["guidance"]["freshness"] = (
            "Cached upstream reference only; prescribed refresh blocked by unrelated local checkout changes, not claimed fresh. "
            "Current Learn and actual observations take precedence; no ontology recipe or API mutation used."
        )
        packet["financialQuality"]["warnings"] += [
            "Synthetic motifs are not causal evidence or individual predictions.",
            "An incident with no effective service mapping blocks membership; it is not observed zero impact.",
            "Credits are whole selected-contract service-month settlement, never incident-attributed loss.",
            "Network telemetry is days 14-18 only; source incident intervals are separate coverage.",
        ]
    output.mkdir(parents=True, exist_ok=False)
    for name, raw in files.items():
        (output / name).write_bytes(raw)
    (output / "packet.json").write_bytes(encoded(packet))
    read_packet(output)
    return packet


def bind(packet_dir: Path, binding: dict, output: Path) -> dict:
    """Resolve placeholders in a NEW private copy only; does not authorize deployment."""
    output = output.resolve()
    if output.is_relative_to(ROOT) or "onedrive" in str(output).lower():
        raise ValueError("Resolved target identifiers must remain outside source/OneDrive")
    if output.exists():
        raise FileExistsError("Bound output exists; never overwrite a reviewed artifact")
    packet = read_packet(packet_dir)
    packet_hash = digest((packet_dir / "packet.json").read_bytes())
    if set(binding) != {"packetSha256", "workspace", "lakehouse"} or binding["packetSha256"] != packet_hash:
        raise ValueError("Binding differs from the reviewed packet")
    workspace, lakehouse = binding["workspace"], binding["lakehouse"]
    for item in (workspace, lakehouse):
        if set(item) != {"id", "displayName"} or str(uuid.UUID(item["id"])) != item["id"]:
            raise ValueError("Actual canonical item IDs and names are required")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", item["displayName"]):
            raise ValueError("Approved notebook namespace requires simple exact workspace/lakehouse names")
    if lakehouse["displayName"] != packet["lakehouseName"] or lakehouse["id"] == workspace["id"]:
        raise ValueError("Binding would adopt a different lakehouse instead of the isolated hero item")
    replacements = {"<WORKSPACE_ID>": workspace["id"], "<LAKEHOUSE_ID>": lakehouse["id"],
                    WORKSPACE: workspace["displayName"]}

    def replace(raw: bytes) -> bytes:
        text = raw.decode()
        for token, value in replacements.items():
            text = text.replace(token, value)
        if re.search(r"<[A-Z_]+>", text):
            raise ValueError("Unresolved binding placeholder")
        return text.encode()

    notebook = json.loads(replace((packet_dir / "loader.ipynb").read_bytes()))
    files = {"loader.ipynb": encoded(notebook)}
    for asset in packet["packetFiles"]:
        if not asset["file"].endswith("-template.json"):
            continue
        body = json.loads((packet_dir / asset["file"]).read_bytes())
        if asset["file"] == "notebook-template.json":
            body["definition"]["parts"] = [part("notebook-content.ipynb", encoded(notebook))]
        elif "definition" in body:
            for entry in body["definition"]["parts"]:
                raw = base64.b64decode(entry["payload"], validate=True)
                if entry["path"] == "definition/expressions.tmdl":
                    raw = replace(raw)
                elif any(token.encode() in raw for token in replacements):
                    raise ValueError("Unexpected binding token outside the generated connection expressions")
                entry["payload"] = base64.b64encode(raw).decode()
        files[asset["file"]] = encoded(body)
    report = {
        "status": "BOUND_LOCALLY_NOT_APPROVED", "approved": False, "sourcePacketSha256": packet_hash,
        "workspace": workspace, "lakehouse": lakehouse,
        "files": [{"file": name, "sha256": digest(raw), "bytes": len(raw)} for name, raw in sorted(files.items())],
        "liveExecution": "NOT_RUN",
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, raw in files.items():
        (output / name).write_bytes(raw)
    (output / "bound-packet.json").write_bytes(encoded(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "generated" / "smoke",
                        help="One smoke revision, or showcase-v2 with --showcase; never mixes miniature")
    parser.add_argument("--output", type=Path, help="NEW output directory; existing paths are rejected")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--include-subscriptions", action="store_true",
                       help="Also include the complete Subscriptions model from the same revision")
    scope.add_argument("--all-models", action="store_true",
                       help="Include all eight complete domain models from one coherent source revision")
    parser.add_argument("--showcase", action="store_true",
                        help="Opt in to showcase-v2; requires matching generated --model-root and --catalog")
    parser.add_argument("--model-root", type=Path, help="Complete generated model directory, including census.json")
    parser.add_argument("--catalog", type=Path, help="Catalog generated alongside the selected source models")
    parser.add_argument("--bind-packet", type=Path, help="Existing local packet to copy and bind, without deployment")
    parser.add_argument("--binding", type=Path, help="Private reviewed binding JSON; never credentials")
    args = parser.parse_args()
    if args.bind_packet:
        if (not args.binding or not args.output or args.include_subscriptions or args.all_models or
                args.showcase or args.model_root or args.catalog):
            parser.error("Binding requires --binding and --output; no model selection changes")
        if args.binding.resolve().is_relative_to(ROOT) or "onedrive" in str(args.binding.resolve()).lower():
            parser.error("Binding inputs must be private and outside source/OneDrive")
        report = bind(args.bind_packet, json.loads(args.binding.read_text(encoding="utf-8-sig")), args.output)
    else:
        if args.binding:
            parser.error("--binding requires --bind-packet")
        output = args.output or ROOT / ".local" / (
            "hero-packet-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        )
        report = build(args.source, output, include_subscriptions=args.include_subscriptions,
                       all_models=args.all_models,
                       showcase=args.showcase, model_root=args.model_root, catalog_path=args.catalog)
        report = {"packet": str(output / "packet.json"), "revision": report["packetRevision"],
                  "models": report["models"], "tables": len(report["tables"]), "rows": report["rows"],
                  "financialQuality": report["financialQuality"], "status": report["status"]}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
