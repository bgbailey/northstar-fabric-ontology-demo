"""Synthetic eight-source usage-scoped row-audit shape; no private capture, credentials or live requests."""

import base64
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import UUID

import pytest
from test_current_read_population import ROOT, byte_case

from telco_iq.catalog import Catalog
from telco_iq.contracts import Scope
from telco_iq.live_contracts import ReaderBinding, SourceIdentity
from telco_iq.source_proof import verify_current_p0_rows, verify_current_showcase_audit


def audit_case(monkeypatch):
    models = tuple(Catalog(ROOT / "data" / "contracts" / "catalog.json").models)
    catalog, manifest, files, reports, expected, binary = byte_case(models)
    manifest.update(profile="showcase-v2", version="showcase-v2.3")
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(seconds=40), now - timedelta(seconds=10)
    workspace, lakehouse = str(UUID(int=100)), str(UUID(int=110))
    sources = {alias: SourceIdentity(workspace_id=workspace, dataset_id=UUID(int=200 + index))
               for index, alias in enumerate(models)}
    scope = Scope(tenant="synthetic-only-tenant", principal="synthetic-only-reader",
                  authorization_revision="synthetic-epoch", models=list(models), mode="LIVE_DELEGATED")
    binding = ReaderBinding(scope=scope, ontology_workspace_id=workspace, ontology_item_id=UUID(int=199),
                            native_snapshot_hash="a" * 64, sources=sources)
    profile = SimpleNamespace(identity=sources["finance"], lakehouse_id=UUID(lakehouse))
    documents = {}
    def document(value, *, text=False):
        raw = value if text else json.dumps(value, sort_keys=True)
        key = hashlib.sha256(raw.encode()).hexdigest()
        documents[key] = raw
        return key
    monkeypatch.setattr("telco_iq.source_observation._bindings", lambda *args: None)
    listing, logs, operations, intent_hashes, response_hashes = [], {"body": {}, "request": {}, "response": {}}, [], [], []
    types = {"int64": "long", "string": "string", "bool": "boolean", "date": "date", "timestamp": "timestamp", "float64": "double"}
    for index, name in enumerate(sorted(expected)):
        prefix = lakehouse + "/Tables/dbo/" + name
        incarnation = str(UUID(int=1000 + index))
        table_files = []
        for file_index, item in enumerate(files):
            if item["table"] != name:
                continue
            filename = f"part-synthetic-{file_index:03d}.parquet"
            item.update(source_path="/" + workspace + "/" + prefix + "/" + filename,
                        observed_at=(start + timedelta(seconds=1)).isoformat())
            listing.append({"name": prefix + "/" + filename, "etag": item["headers"]["etag"],
                            "contentLength": str(item["bytes"]), "lastModified": "synthetic-fixed"})
            table_files.append({"add": {"path": filename, "size": item["bytes"]}})
        actions = [
            {"protocol": {"minReaderVersion": 1, "minWriterVersion": 2}},
            {"metaData": {"id": incarnation, "format": {"provider": "parquet"}, "schemaString": json.dumps({
                "type": "struct", "fields": [{"name": key, "type": types[value]} for key, value in catalog.tables[name]["columns"].items()],
            })}},
            *table_files,
        ]
        raw = "\n".join(json.dumps(value) for value in actions)
        log_hash = document(raw, text=True)
        log_path, log_etag = prefix + "/_delta_log/00000000000000000000.json", '"synthetic-log-' + name + '"'
        listing.append({"name": log_path, "etag": log_etag, "contentLength": str(len(raw)), "lastModified": "synthetic-fixed"})
        logs["body"][name] = log_hash
        logs["request"][name] = document({
            "method": "GET", "url": "https://centralus-onelake.dfs.fabric.microsoft.com/" + workspace + "/" + log_path,
            "started_at": (start - timedelta(seconds=2)).isoformat(),
        })
        logs["response"][name] = document({
            "http_status": 200, "body_sha256": log_hash, "headers": {"etag": log_etag},
            "observed_at": (start - timedelta(seconds=1)).isoformat(),
        })
        for item in files:
            if item["table"] != name:
                continue
            operations.append({
                "method": "GET", "audience": "https://storage.azure.com",
                "url": "https://centralus-onelake.dfs.fabric.microsoft.com" + item["source_path"],
                "source_path": item["source_path"], "table": name,
                "headers": {"If-Match": item["headers"]["etag"]}, "expectedBytes": item["bytes"],
                "Delta_version": 0, "incarnation_id": incarnation, "log_body_sha256": log_hash,
            })
    before_hash = document({"paths": listing})
    after_request = {"method": "GET", "url": "https://centralus-onelake.dfs.fabric.microsoft.com/" + workspace + "?" + urlencode({
        "resource": "filesystem", "directory": lakehouse + "/Tables", "recursive": "true", "upn": "false", "maxResults": "5000",
    })}
    manifest_hash = document(manifest)
    total_bytes = sum(item["bytes"] for item in files)
    plan = {
        "kind": "READ_ONLY_EXACT_SHOWCASE_CURRENT_ACTIVE_FILE_ROWS_AND_PK_AUDIT",
        "created_at": (start - timedelta(seconds=1)).isoformat(), "approval_mode": "USAGE_SCOPED", "work_cutoff": None,
        "sourceCatalogRevision": catalog.revision, "manifestFileSha256": manifest_hash,
        "sourceMetadataPathsSha256": before_hash, "source_model_aliases": list(models),
        "identity": {"tenant_id": scope.tenant, "expected_user": "synthetic@example.invalid"},
        "expected_reader_object_ids": [scope.principal], "workspace_id": workspace, "lakehouse_id": lakehouse,
        "onelake_dfs_endpoint": "https://centralus-onelake.dfs.fabric.microsoft.com",
        "afterMetadataRequest": after_request, "operations": operations,
        "maximum_data_GETs": len(files), "maximum_after_listing_GETs": 1,
        "limits": {"dataFileGets": len(files), "afterListingGets": 1, "sourceBytes": total_bytes, "maximumBytes": total_bytes,
                   "expectedLogicalRows": sum(item["rows"] for item in reports), "maximumRows": sum(item["rows"] for item in reports)},
    }
    plan_hash = document(plan)
    file_by_path = {item["source_path"]: item for item in files}
    ordered_files = [file_by_path[item["source_path"]] for item in operations]
    for operation, response in zip(operations, ordered_files, strict=True):
        intent_hashes.append(document({"proposal_sha256": plan_hash, "operation": operation, "started_at": start.isoformat()}))
        response_hashes.append(document(response))
    commitments = verify_current_p0_rows(catalog, manifest, files, reports, expected, binary,
                                         selected_models=models, maximum_files=512, commitment_algorithm="CURRENT_SOURCE_TYPED_PK_V1")
    for item in reports:
        item["typed_commitment"] = commitments[item["table"]]
    report_hash = document({
        "kind": "ACTUAL_SHOWCASE_CURRENT_SOURCE_TYPED_ROW_AUDIT", "proposal_sha256": plan_hash,
        "source_capture_started_at": start.isoformat(), "observed_at": end.isoformat(),
        "verified_offline_at": now.isoformat(), "source_files": ordered_files, "tables": reports,
    })
    evidence = SimpleNamespace(
        identity=profile.identity, population=SimpleNamespace(model="finance"), bound_definition_hash=document({}),
        attestation_hash=report_hash, read_plan_hash=plan_hash,
        read_approval_hash=document({"approved": True, "proposal_sha256": plan_hash}),
        target_map_hash=document({"tenant_id": scope.tenant, "workspace_id": workspace, "lakehouse_id": lakehouse,
                                  "sources": {alias: source.model_dump(mode="json") for alias, source in sources.items()}}),
        expected_identity_hash=document({"expected_user": "synthetic@example.invalid"}),
        source_manifest_file_hash=manifest_hash, listing_before_hash=before_hash,
        listing_after_hash=document({"status": 200, "headers": {}, "body": {"paths": deepcopy(listing)}, "observed_at": end.isoformat()}),
        after_listing_intent_hash=document(after_request), log_body_hashes=logs["body"],
        log_response_hashes=logs["response"], log_request_hashes=logs["request"],
        file_intent_hashes=intent_hashes, file_response_hashes=response_hashes, expected_files=expected,
    )
    return evidence, documents, binary, catalog, binding, profile, document, commitments


def test_exact_eight_source_usage_scoped_audit_verifies_original_conditional_bytes(monkeypatch):
    evidence, documents, binary, catalog, binding, profile, _, expected = audit_case(monkeypatch)
    assert verify_current_showcase_audit(evidence, documents, binary, catalog, binding, profile) == expected
    assert len(expected) == 36 and len(evidence.file_response_hashes) == 72


@pytest.mark.parametrize("change", [
    "actor", "models", "approval", "missing-file", "byte-tamper", "changed-listing", "new-commit", "stats-as-data",
])
def test_current_showcase_audit_never_accepts_wrong_scope_partial_bytes_or_changed_snapshot(monkeypatch, change):
    evidence, documents, binary, catalog, binding, profile, document, _ = audit_case(monkeypatch)
    if change == "actor":
        binding = binding.model_copy(update={"scope": binding.scope.model_copy(update={"principal": "wrong"})})
    elif change == "models":
        binding = binding.model_copy(update={"sources": {"finance": binding.sources["finance"]}})
    elif change == "approval":
        evidence.read_approval_hash = document({"approved": False, "proposal_sha256": evidence.read_plan_hash})
    elif change == "missing-file":
        evidence.file_response_hashes.pop()
    elif change == "byte-tamper":
        binary[next(iter(binary))] = base64.b64encode(b"not original source bytes").decode()
    elif change in {"changed-listing", "new-commit"}:
        after = json.loads(documents[evidence.listing_after_hash])
        if change == "changed-listing":
            after["body"]["paths"][0]["etag"] = "changed"
        else:
            log = next(item for item in after["body"]["paths"] if "/_delta_log/" in item["name"])
            after["body"]["paths"].append({**log, "name": log["name"].replace("00000000000000000000.json", "00000000000000000001.json")})
        evidence.listing_after_hash = document(after)
    else:
        plan = json.loads(documents[evidence.read_plan_hash])
        plan["operations"][0]["url"] += "/_delta_log/_stats/not-data.parquet"
        evidence.read_plan_hash = document(plan)
    with pytest.raises((ValueError, KeyError)):
        verify_current_showcase_audit(evidence, documents, binary, catalog, binding, profile)


def test_auxiliary_statistics_are_not_source_rows_or_a_reason_to_ignore_active_data_changes(monkeypatch):
    evidence, documents, binary, catalog, binding, profile, document, expected = audit_case(monkeypatch)
    after = json.loads(documents[evidence.listing_after_hash])
    path = after["body"]["paths"][0]["name"].rsplit("/", 1)[0] + "/_delta_log/_stats/new.parquet"
    after["body"]["paths"].append({"name": path, "etag": "aux-only", "contentLength": "5"})
    evidence.listing_after_hash = document(after)
    assert verify_current_showcase_audit(evidence, documents, binary, catalog, binding, profile) == expected
