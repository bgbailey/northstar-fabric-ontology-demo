"""Current-read byte verification uses synthetic P0 files, never private captures."""

import base64
import hashlib
import io
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from telco_iq.catalog import Catalog, digest
from telco_iq.contracts import Budgets, Scope, SourcePopulation
from telco_iq.errors import LabError
from telco_iq.live_contracts import ReaderBinding, SourceCallBudget, SourceIdentity
from telco_iq.native_context import NAME, _name
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink
from telco_iq.source_observation import (
    CurrentReadFrameAnchor,
    MetadataResponse,
    MetadataStateProfile,
    SourceMetadataObserver,
    TableTarget,
    _refresh_detail_time,
    _time,
    code_hash,
    eligible_state,
    facts_from_captures,
)
from telco_iq.source_proof import (
    CurrentP0ReadCapture,
    ReviewedSourceProof,
    verify_current_p0_audit,
    verify_current_p0_rows,
)

ROOT = Path(__file__).resolve().parents[1]


def byte_case(selected_models=("subscriptions", "finance")):
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    folder = ROOT / "data" / "fixtures" / "miniature"
    manifest = json.loads((folder / "manifest.json").read_bytes())
    selected = {name for alias in selected_models for name in catalog.models[alias]["tables"]}
    metadata = {table["id"]: table for table in manifest["tables"]}
    binaries, expected, files, tables = {}, {}, [], []
    for name in sorted(selected):
        raw = (folder / (name + ".parquet")).read_bytes()
        key = hashlib.sha256(raw).hexdigest()
        binaries[key] = base64.b64encode(raw).decode()
        expected[name] = key
        original = pq.read_table(io.BytesIO(raw))
        # Reorder and repartition like real Delta files; physical/stream hashes differ.
        rows = list(reversed(original.to_pylist()))
        chunks = [rows[index::2] for index in range(2)]
        for index, chunk in enumerate(chunks):
            output = io.BytesIO()
            pq.write_table(pa.Table.from_pylist(chunk, schema=original.schema), output)
            current = output.getvalue()
            sha = hashlib.sha256(current).hexdigest()
            binaries[sha] = base64.b64encode(current).decode()
            files.append({
                "table": name, "status": 200, "bytes": len(current), "sha256": sha,
                "source_path": "00000000-0000-0000-0000-000000000110/Tables/dbo/" + name
                               + f"/part-{index:05d}.parquet",
                "headers": {"etag": f'"synthetic-{name}-{index}"', "content-length": str(len(current))},
            })
        tables.append({
            "table": name, "rows": len(rows), "files": 2,
            "expected_file_sha256": key, "declared_logical_sha256": metadata[name]["content_sha256"],
            "canonical_row_sha256": "not-used-as-manifest-stream-or-current-proof",
        })
    return catalog, manifest, files, tables, expected, binaries


def test_typed_current_bytes_equal_original_population_not_physical_order_or_report_booleans():
    args = byte_case()
    commitments = verify_current_p0_rows(*args)
    assert len(commitments) == 10
    manifest = {table["id"]: table for table in args[1]["tables"]}
    assert all(commitments[name] != manifest[name]["content_sha256"] for name in commitments)


@pytest.mark.parametrize("failure", ["bytes", "expected", "duplicate", "missing", "extra", "partial", "rows", "etag", "length"])
def test_current_read_byte_population_fails_closed(failure):
    args = byte_case()
    catalog, manifest, files, tables, expected, binaries = args
    if failure == "bytes":
        binaries[files[0]["sha256"]] = base64.b64encode(b"not-the-observed-bytes").decode()
    elif failure == "expected":
        expected[next(iter(expected))] = "0" * 64
    elif failure == "duplicate":
        files[1] = {**files[0], "source_path": files[1]["source_path"]}
    elif failure == "missing":
        files.pop()
    elif failure == "extra":
        files.append({**files[0], "table": "holdout_answers", "source_path": "not-approved"})
    elif failure == "partial":
        files[0]["status"] = 206
    elif failure == "rows":
        tables[0]["rows"] += 1
    elif failure == "etag":
        files[0]["headers"]["etag"] = ""
    else:
        files[0]["headers"]["content-length"] = "1"
    with pytest.raises((ValueError, KeyError)):
        verify_current_p0_rows(catalog, manifest, files, tables, expected, binaries)


async def current_proof_case(tmp_path, *, enhanced=False):
    """Original-shaped synthetic audit plus actual local metadata-capture implementation."""
    catalog, manifest, files, reports, expected, binaries = byte_case()
    fixture = json.loads((ROOT / "tests" / "fixtures" / "source_metadata_actual_2026_10_15.json").read_bytes())
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(seconds=90), now - timedelta(seconds=80)
    frame_start, frame_end = now - timedelta(seconds=30), now - timedelta(seconds=20)
    workspace, lakehouse = fixture["workspace_id"], fixture["lakehouse_id"]
    identities = {alias: SourceIdentity(**source["identity"]) for alias, source in fixture["sources"].items()}
    scope = Scope(
        tenant="synthetic-tenant", principal="synthetic-reader", authorization_revision="synthetic-epoch-A",
        mode="LIVE_DELEGATED", models=sorted(identities),
    )
    binding = ReaderBinding(
        scope=scope, ontology_workspace_id=workspace, ontology_item_id="00000000-0000-0000-0000-000000000199",
        native_snapshot_hash="a" * 64, sources=identities,
    )
    documents = {}

    def document(value):
        text = json.dumps(value, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        documents[key] = text
        return key

    old_paths = fixture["paths"]["paths"]
    paths = [item for item in old_paths if not item["name"].endswith(".parquet")]
    for index, item in enumerate(files):
        name = item["table"]
        part = f"part-synthetic-{index:03d}.parquet"
        item.update({
            "source_path": f"/{workspace}/{lakehouse}/Tables/dbo/{name}/{part}",
            "observed_at": start.isoformat(), "file": f"source-{index + 1:02d}.parquet",
        })
        template = deepcopy(next(path for path in old_paths if
                                 path["name"].startswith(f"{lakehouse}/Tables/dbo/{name}/")
                                 and path["name"].endswith(".parquet")))
        template.update(name=item["source_path"].split("/", 2)[2], etag=item["headers"]["etag"],
                        contentLength=str(item["bytes"]))
        paths.append(template)
    log_bodies, log_responses, incarnations = {}, {}, {}
    for name in expected:
        original = fixture["delta_zero_logs"]["dbo/" + name]
        actions = [action for action in original["actions"] if "add" not in action]
        metadata = next(action["metaData"] for action in actions if "metaData" in action)
        incarnations[name] = metadata["id"]
        actions += [{"add": {"path": item["source_path"].rsplit("/", 1)[1], "size": item["bytes"]}}
                    for item in files if item["table"] == name]
        text = "\n".join(json.dumps(action) for action in actions)
        key = hashlib.sha256(text.encode()).hexdigest()
        documents[key] = text
        log_bodies[name] = key
        log_responses[name] = document({
            "observed_at": (start - timedelta(seconds=1)).isoformat(),
            "alias": "delta-v0-metadata-dbo-" + name, "status_code": 200,
            "headers": original["response_headers"], "body_file": name + ".body", "body_sha256": key,
        })
        original["actions"] = actions
    before = {"paths": paths, "ContinuationToken": None}
    before_hash = document(before)
    manifest_hash = document(manifest)
    identity_hash = document({"expected_user": "synthetic@example.invalid", "ambient_fallback_allowed": False})
    target_hash = document({
        "tenant_id": scope.tenant, "expected_identity_file": "synthetic-private-identity.json",
        "protected_p0_workspace": {"id": workspace},
        "p0_items": [
            *[{"id": str(identity.dataset_id), "workspaceId": workspace, "type": "SemanticModel"}
              for identity in identities.values()],
            {"id": lakehouse, "workspaceId": workspace, "type": "Lakehouse"},
        ],
    })
    operations = [{
        "table": item["table"], "method": "GET",
        "url": "https://centralus-onelake.dfs.fabric.microsoft.com" + item["source_path"],
        "audience": "https://storage.azure.com",
        "headers": {"If-Match": item["headers"]["etag"]},
        "expectedBytes": item["bytes"], "deltaTableId": incarnations[item["table"]], "deltaVersion": 0,
        "sourceLogResponseSha256": log_bodies[item["table"]],
    } for item in files]
    listing_url = "https://centralus-onelake.dfs.fabric.microsoft.com/" + workspace + "?" + urlencode({
        "resource": "filesystem", "directory": lakehouse + "/Tables", "recursive": "true",
        "maxResults": "5000", "upn": "false",
    })
    plan = {
        "createdAt": (start - timedelta(seconds=1)).isoformat(),
        "expiresAt": (now + timedelta(hours=1)).isoformat(),
        "targetMapSha256": target_hash, "expectedIdentitySha256": identity_hash,
        "manifestFileSha256": manifest_hash, "sourceMetadataPathsSha256": before_hash,
        "expectedIdentityFile": "synthetic-private-identity.json",
        "tables": [{
            "table": name, "expectedRows": next(table["rows"] for table in manifest["tables"] if table["id"] == name),
            "expectedLogicalContentSha256": next(table["content_sha256"] for table in manifest["tables"]
                                                if table["id"] == name),
            "columns": list(catalog.tables[name]["columns"]),
        } for name in sorted(expected)],
        "operations": operations, "afterMetadataRequest": {"method": "GET", "url": listing_url},
        "limits": {"dataFileGets": len(files), "afterListingGets": 1,
                   "sourceBytes": sum(item["bytes"] for item in files), "maximumBytes": 524288,
                   "expectedLogicalRows": sum(table["rows"] for table in reports), "maximumRows": 600},
    }
    plan_hash = document(plan)
    approval_hash = document({
        "approved": True, "proposal_sha256": plan_hash, "expires_at": plan["expiresAt"],
        "maximum_data_gets": len(files), "maximum_after_listing_gets": 1,
        "maximum_bytes": 524288, "maximum_rows": 600,
    })
    file_intents = [document({"proposal_sha256": plan_hash, "operation": op}) for op in operations]
    file_responses = [document(item) for item in files]
    after_hash = document({
        "observed_at": end.isoformat(), "status": 200, "headers": {},
        "body": before, "body_sha256": digest({"synthetic-wire-bytes": "not-reconstructed-from-json"}),
    })
    report_hash = document({
        "kind": "ACTUAL_SELECTED_USER_CURRENT_SOURCE_DATA_READ_ATTESTATION",
        "proposal_sha256": plan_hash, "source_capture_started_at": start.isoformat(),
        "observed_at": end.isoformat(), "verified_offline_at": now.isoformat(),
        "source_files": files, "tables": reports,
    })
    sink = PrivateEvidenceSink(PrivateEvidenceConfig(
        directory=tmp_path / "private", approval_reference="synthetic only", expires_at=now + timedelta(hours=1),
        max_capture_bytes=500000, max_total_bytes=16000000, max_records=100,
    ), tmp_path / "synthetic-repository")
    profiles, facts = {}, {}
    for alias, source in fixture["sources"].items():
        source["framing_history"]["value"][0].update(startTime=frame_start.isoformat(), endTime=frame_end.isoformat())
        if enhanced:
            source["framing_history"]["value"][0]["refreshType"] = "ViaEnhancedApi"
        parts = source["definition"]["definition"]["parts"]
        tables = []
        for part in parts:
            text = base64.b64decode(part["payload"]).decode()
            if "\t\t\tentityName: " not in text:
                continue
            name = text.split("entityName: ", 1)[1].splitlines()[0]
            delta = fixture["delta_zero_logs"]["dbo/" + name]
            metadata = next(action["metaData"] for action in delta["actions"] if "metaData" in action)
            prefix = f"{lakehouse}/Tables/dbo/{name}"
            tables.append(TableTarget(
                catalog_table=name, part=part["path"], schema_name="dbo", entity_name=name,
                columns={field["name"]: field["type"] for field in json.loads(metadata["schemaString"])["fields"]},
                incarnation_id=metadata["id"],
                service_birth=next(item["creationTime"] for item in paths if item["name"] == prefix),
                log_birth=next(item["creationTime"] for item in paths if
                               item["name"] == prefix + "/_delta_log/00000000000000000000.json"),
            ))
        model_hash = document(source["definition"])
        refresh_body = (
            {"type": "full", "commitMode": "transactional", "retryCount": 0, "timeout": "00:02:00"}
            if enhanced else {"notifyOption": "NoNotification"}
        )
        objects = []
        for target in tables:
            part = next(part for part in parts if part["path"] == target.part)
            text = base64.b64decode(part["payload"]).decode()
            objects.append({
                "table": _name(re.findall(r"^table (" + NAME + r")\s*$", text, re.M)[0]),
                "partition": _name(re.findall(r"^\s*partition (" + NAME + r") = entity\s*$", text, re.M)[0]),
                "status": "Completed",
            })
        details_hash = document({
            "method": "GET", "status": 200,
            "path": f"/v1.0/myorg/groups/{workspace}/datasets/{identities[alias].dataset_id}/refreshes/"
                    + source["framing_history"]["value"][0]["requestId"],
            "observedAtUtc": (frame_end + timedelta(seconds=1)).isoformat(),
            "response": {
                "startTime": frame_start.isoformat(), "endTime": frame_end.isoformat(),
                "type": "Full", "commitMode": "Transactional", "status": "Completed", "extendedStatus": "Completed",
                "objects": objects, "numberOfAttempts": 1,
            },
        }) if enhanced else None
        regional = "https://wabi-us-central-b-primary-redirect.analysis.windows.net" if enhanced else None
        profile = MetadataStateProfile(
            identity=identities[alias], lakehouse_id=lakehouse, tables=tables, part_paths=[p["path"] for p in parts],
            frame_request_id=source["framing_history"]["value"][0]["requestId"], code_hash=code_hash(),
            frame_refresh_type="ViaEnhancedApi" if enhanced else "DirectLakeFraming",
            regional_location_origin=regional,
            provenance="synthetic_test", reviewed_at=now - timedelta(seconds=1), expires_at=now + timedelta(hours=1),
            review_reference="Synthetic current-read/full-frame positive, not an actual author or reader grant",
            frame_anchor=CurrentReadFrameAnchor(
                kind="current_p0_read_full_frame_v0", model_body_hash=model_hash, attestation_hash=report_hash,
                read_plan_hash=plan_hash,
                refresh_details_hash=details_hash,
                definition_at_frame_hash=document({
                    "method": "POST", "status": 200, "bodySha256": digest({}),
                    "path": f"/v1/workspaces/{workspace}/semanticModels/{identities[alias].dataset_id}/getDefinition",
                    "startedAt": (frame_start - timedelta(seconds=2)).isoformat(), "response": source["definition"],
                }),
                full_refresh_body_hash=document(refresh_body),
                accepted_refresh_hash=document({
                    "method": "POST", "status": 202, "bodySha256": digest(refresh_body),
                    "path": f"/v1.0/myorg/groups/{workspace}/datasets/{identities[alias].dataset_id}/refreshes",
                    "startedAt": (frame_start - timedelta(seconds=1)).isoformat(),
                    "location": (regional or "") + f"/v1.0/myorg/groups/{workspace}/datasets/{identities[alias].dataset_id}/refreshes/"
                                + source["framing_history"]["value"][0]["requestId"],
                }),
            ),
        )
        async def request(value, source=source):
            if value.role.startswith("definition-"):
                body, headers = json.dumps(source["definition"]).encode(), {}
            elif value.role.startswith("frame-"):
                body, headers = json.dumps(source["framing_history"]).encode(), {}
            elif value.role.startswith("paths-"):
                body, headers = json.dumps(before).encode(), {}
            else:
                delta = fixture["delta_zero_logs"][value.role.removeprefix("log0:")]
                body = "\n".join(json.dumps(action) for action in delta["actions"]).encode()
                headers = delta["response_headers"]
            return MetadataResponse(200, headers, body, datetime.now(timezone.utc), scope, value.resource)
        observer = SourceMetadataObserver(
            binding, {alias: profile}, {alias: profile.profile_hash}, request, sink,
            SourceCallBudget(Budgets(max_queries=50, timeout_seconds=60)), lambda actual: actual == scope,
            synthetic=True,
        )
        facts[alias] = await observer.capture(alias)
        profile.calibration_capture_ids = facts[alias].capture_ids
        profile.reviewed_at = datetime.now(timezone.utc)
        profiles[alias] = profile
    commitments = verify_current_p0_rows(catalog, manifest, files, reports, expected, binaries)
    shared = {name for name in expected if catalog.tables[name]["model"] == "core"
              or name.endswith("_map") or name in {"subscription", "contract"}}
    captures = {
        alias: CurrentP0ReadCapture(
            receipt_kind="CURRENT_P0_READ_ATTESTATION", identity=identity,
            population=SourcePopulation(
                model=alias, evidence_kind="REVIEWED_LIVE", binding_hash=digest(identity.model_dump(mode="json")),
                profile="miniature", seed=manifest["seed"],
                generator_revision="CURRENT_P0_MANIFEST_FORMAT:" + manifest["version"],
                identity_revision=digest({name: {key: catalog.tables[name][key] for key in ("columns", "primaryKey")}
                                          for name in sorted(shared)}),
                manifest_hash=digest(manifest),
                table_hashes={name: commitments[name] for name in catalog.models[alias]["tables"]},
                shared_map_hashes={name: commitments[name] for name in sorted(shared)},
            ),
            source_schema_hash=digest({"synthetic-source-schema": alias}),
            data_revision="DERIVED_DELTA_VECTOR:" + facts[alias].data_fingerprint,
            model_revision="DERIVED_TMDL_FRAME:" + facts[alias].model_fingerprint,
            source_capture_id=facts[alias].capture_ids[0],
            source_response_hash=digest(sink.read(facts[alias].capture_ids[0])["response"]),
            metadata_capture_ids=facts[alias].capture_ids,
            source_manifest_file_hash=manifest_hash, attestation_hash=report_hash,
            read_plan_hash=plan_hash, read_approval_hash=approval_hash, target_map_hash=target_hash,
            expected_identity_hash=identity_hash, listing_before_hash=before_hash, listing_after_hash=after_hash,
            after_listing_intent_hash=document({"method": "GET", "url": listing_url}),
            log_body_hashes=log_bodies, log_response_hashes=log_responses,
            file_intent_hashes=file_intents, file_response_hashes=file_responses,
            expected_files=expected, bound_definition_hash=profiles[alias].frame_anchor.model_body_hash,
        ) for alias, identity in identities.items()
    }
    profile_config = SimpleNamespace(
        states={}, metadata_states=profiles,
        metadata_state_pins={alias: profile.profile_hash for alias, profile in profiles.items()},
        model_dump=lambda **_: {alias: profile.model_dump(mode="json") for alias, profile in profiles.items()},
    )
    proof = ReviewedSourceProof(
        proof_kind="SYNTHETIC_TEST", scope=scope, captured_at=start - timedelta(seconds=1),
        reviewed_at=datetime.now(timezone.utc), expires_at=now + timedelta(minutes=5),
        review_reference="Synthetic independently pinned audit and same-epoch metadata proof",
        catalog_hash=catalog.revision, native_snapshot_hash=binding.native_snapshot_hash,
        enrollment_digest="b" * 64, source_profiles_hash=digest(profile_config.model_dump()),
        context_contract_hash="c" * 64, documents=documents, sources=captures, binary_objects=binaries,
    )
    arguments = {
        "scope": scope, "catalog": catalog,
        "snapshot": SimpleNamespace(
            content_hash=binding.native_snapshot_hash,
            sources=[SimpleNamespace(alias=alias, workspace_id=str(identity.workspace_id),
                                     dataset_id=str(identity.dataset_id)) for alias, identity in identities.items()],
        ),
        "enrollment": SimpleNamespace(sources=identities, ontology_workspace_id=binding.ontology_workspace_id,
                                      ontology_item_id=binding.ontology_item_id,
                                      enrollment_digest="b" * 64,
                                      source_schema_hashes={alias: capture.source_schema_hash
                                                            for alias, capture in captures.items()}),
        "profiles": profile_config, "context_contract_hash": "c" * 64, "sink": sink, "synthetic": True,
    }
    return proof, arguments, binding


async def test_current_audit_import_requires_exact_bytes_and_new_full_frame_not_historical_wrappers(tmp_path):
    proof, arguments, _ = await current_proof_case(tmp_path)
    values = proof.validate_for(pinned_digest=proof.proof_digest, **arguments)
    assert {value.model for value in values} == {"subscriptions", "finance"}
    assert all("CURRENT_P0_MANIFEST_FORMAT:" in value.generator_revision for value in values)
    serialized = proof.model_dump(mode="json")
    assert "deployment_receipts" not in json.dumps(serialized)
    assert "deployment_actor" not in serialized and "load_assertion_status" not in serialized
    assert isinstance(ReviewedSourceProof.model_validate(serialized).sources["finance"], CurrentP0ReadCapture)
    with pytest.raises(LabError, match="fixture/test origin"):
        proof.validate_for(pinned_digest=proof.proof_digest, **{**arguments, "synthetic": False})


async def test_timely_reviewed_population_anchor_is_not_relabelled_as_a_current_source_clock(tmp_path, monkeypatch):
    import telco_iq.source_observation as observations
    import telco_iq.source_proof as proofs

    proof, arguments, binding = await current_proof_case(tmp_path, enhanced=True)
    original_review = proof.reviewed_at
    proof.expires_at += timedelta(minutes=30)
    later = datetime.now(timezone.utc) + timedelta(minutes=6)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later if tz is not None else later.replace(tzinfo=None)
    monkeypatch.setattr(observations, "datetime", Later)
    monkeypatch.setattr(proofs, "datetime", Later)
    assert {item.model for item in proof.validate_for(pinned_digest=proof.proof_digest, **arguments)} == {
        "finance", "subscriptions",
    }
    assert proof.reviewed_at == original_review
    alias = "finance"
    profile = arguments["profiles"].metadata_states[alias]
    facts = facts_from_captures(
        binding, alias, profile, arguments["sink"], proof.sources[alias].metadata_capture_ids, synthetic=True,
    )
    with pytest.raises(LabError, match="stale"):
        eligible_state(facts, profile, proof.documents, **proof.sources[alias].state_anchor_refs(), synthetic=True)
    assert facts.observed_at < later - timedelta(seconds=300)


async def test_population_anchor_cannot_be_reviewed_only_after_its_original_freshness_window(tmp_path, monkeypatch):
    import telco_iq.source_observation as observations
    import telco_iq.source_proof as proofs

    proof, arguments, _ = await current_proof_case(tmp_path, enhanced=True)
    later = datetime.now(timezone.utc) + timedelta(minutes=6)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later if tz is not None else later.replace(tzinfo=None)
    proof.reviewed_at = later
    proof.expires_at = later + timedelta(minutes=30)
    monkeypatch.setattr(observations, "datetime", Later)
    monkeypatch.setattr(proofs, "datetime", Later)
    with pytest.raises(LabError, match="stale"):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


async def test_actual_shaped_enhanced_full_refresh_requires_all_exact_completed_source_partitions(tmp_path):
    proof, arguments, _ = await current_proof_case(tmp_path, enhanced=True)
    values = proof.validate_for(pinned_digest=proof.proof_digest, **arguments)
    assert {value.model for value in values} == {"finance", "subscriptions"}
    assert all(profile.frame_refresh_type == "ViaEnhancedApi"
               for profile in arguments["profiles"].metadata_states.values())


@pytest.mark.parametrize("offset", ["", "Z", "+00:00"])
async def test_enhanced_refresh_detail_times_match_utc_history_without_rewriting_originals(tmp_path, offset):
    proof, arguments, _ = await current_proof_case(tmp_path, enhanced=True)
    for alias, profile in arguments["profiles"].metadata_states.items():
        anchor = profile.frame_anchor
        details = json.loads(proof.documents[anchor.refresh_details_hash])
        for field in ("startTime", "endTime"):
            details["response"][field] = details["response"][field].removesuffix("+00:00") + offset
        text = json.dumps(details, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        proof.documents[key] = text
        anchor.refresh_details_hash = key
        arguments["profiles"].metadata_state_pins[alias] = profile.profile_hash
    proof.source_profiles_hash = digest(arguments["profiles"].model_dump())
    originals = dict(proof.documents)
    assert {row.model for row in proof.validate_for(pinned_digest=proof.proof_digest, **arguments)} == {
        "finance", "subscriptions",
    }
    assert proof.documents == originals


@pytest.mark.parametrize("detail,history", [
    ("2026-10-16T16:30:01.913", "2026-10-16T16:30:01.913Z"),
    ("2026-10-16T16:58:28.68", "2026-10-16T16:58:28.6800000+00:00"),
    ("2026-10-16T16:30:01", "2026-10-16T16:30:01Z"),
    ("2026-10-16T16:30:01.9130000", "2026-10-16T16:30:01.913Z"),
])
def test_offset_free_enhanced_details_require_exact_explicit_utc_history(detail, history):
    assert _refresh_detail_time(detail, history) == _time(history)


@pytest.mark.parametrize("detail,history", [
    ("2026-10-16T16:30:01.913", "2026-10-16T16:30:01.913"),
    ("2026-10-16T16:30:01.913", "2026-10-16T16:30:01.914Z"),
    ("2026-10-16T16:30:01.913", "2026-10-16T16:30:01.9130001Z"),
    ("2026-10-16T16:30:01.9130001", "2026-10-16T16:30:01.913Z"),
    ("2026-10-16T16:30:01.913", "2026-10-16T16:30:01.913+01:00"),
    ("2026-10-16 16:30:01.913", "2026-10-16T16:30:01.913Z"),
    ("20261016T163001.913", "2026-10-16T16:30:01.913Z"),
    ("2026-10-16", "2026-10-16T00:00:00Z"),
    ("2026-10-16T16:30:01.91300000", "2026-10-16T16:30:01.913Z"),
])
def test_enhanced_details_cannot_invent_a_timezone_or_hide_a_changed_history(detail, history):
    with pytest.raises(LabError):
        _refresh_detail_time(detail, history)


def test_offset_free_endpoint_exception_does_not_relax_other_source_observation_times():
    with pytest.raises(LabError, match="timezone-aware"):
        _time("2026-10-16T16:30:01.913")


async def test_enhanced_details_exception_does_not_accept_offset_free_receipt_clock(tmp_path):
    proof, arguments, _ = await current_proof_case(tmp_path, enhanced=True)
    profile = arguments["profiles"].metadata_states["finance"]
    anchor = profile.frame_anchor
    details = json.loads(proof.documents[anchor.refresh_details_hash])
    details["observedAtUtc"] = details["observedAtUtc"].removesuffix("+00:00")
    text = json.dumps(details, sort_keys=True)
    key = hashlib.sha256(text.encode()).hexdigest()
    proof.documents[key] = text
    anchor.refresh_details_hash = key
    arguments["profiles"].metadata_state_pins["finance"] = profile.profile_hash
    proof.source_profiles_hash = digest(arguments["profiles"].model_dump())
    with pytest.raises(LabError, match="timezone-aware"):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


def use_original_refresh_intent(proof, arguments, *, change=None):
    for alias, profile in arguments["profiles"].metadata_states.items():
        anchor = profile.frame_anchor
        body = json.loads(proof.documents[anchor.full_refresh_body_hash])
        accepted = json.loads(proof.documents[anchor.accepted_refresh_hash])
        accepted["approvalSha256"] = "f" * 64
        text = json.dumps(accepted, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        proof.documents[key] = text
        anchor.accepted_refresh_hash = key
        intent = {
            "method": "POST", "url": "https://api.powerbi.com" + accepted["path"],
            "body": body, "bodySha256": digest(body), "approvalSha256": accepted["approvalSha256"],
            "startedAt": accepted["startedAt"],
        }
        if alias == "finance":
            if change == "target":
                intent["url"] += "-different"
            elif change == "body":
                intent["bodySha256"] = "0" * 64
            elif change == "approval":
                intent["approvalSha256"] = "0" * 64
            elif change == "time":
                intent["startedAt"] = "2099-01-01T00:00:00Z"
        text = json.dumps(intent, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        proof.documents[key] = text
        anchor.full_refresh_body_hash = None
        anchor.full_refresh_intent_hash = key
        arguments["profiles"].metadata_state_pins[alias] = profile.profile_hash
    proof.source_profiles_hash = digest(arguments["profiles"].model_dump())


async def test_actual_request_intent_shape_needs_no_fabricated_standalone_body_receipt(tmp_path):
    proof, arguments, _ = await current_proof_case(tmp_path, enhanced=True)
    use_original_refresh_intent(proof, arguments)
    assert {row.model for row in proof.validate_for(pinned_digest=proof.proof_digest, **arguments)} == {
        "finance", "subscriptions",
    }


@pytest.mark.parametrize("client_offset_ms", [-750, 315])
async def test_original_dispatch_uses_client_causality_not_strict_service_job_clock(tmp_path, client_offset_ms):
    proof, arguments, _ = await current_proof_case(tmp_path, enhanced=True)
    use_original_refresh_intent(proof, arguments)
    retained = {key: arguments["sink"].read(key) for value in proof.sources.values()
                for key in value.metadata_capture_ids}
    for alias, profile in arguments["profiles"].metadata_states.items():
        anchor = profile.frame_anchor
        intent = json.loads(proof.documents[anchor.full_refresh_intent_hash])
        accepted = json.loads(proof.documents[anchor.accepted_refresh_hash])
        details = json.loads(proof.documents[anchor.refresh_details_hash])
        server_start = _time(details["response"]["startTime"])
        intent["startedAt"] = (server_start + timedelta(milliseconds=client_offset_ms)).isoformat()
        accepted["startedAt"] = (server_start + timedelta(milliseconds=800)).isoformat()
        for field, value in (("full_refresh_intent_hash", intent), ("accepted_refresh_hash", accepted)):
            text = json.dumps(value, sort_keys=True)
            key = hashlib.sha256(text.encode()).hexdigest()
            proof.documents[key] = text
            setattr(anchor, field, key)
        arguments["profiles"].metadata_state_pins[alias] = profile.profile_hash
    proof.source_profiles_hash = digest(arguments["profiles"].model_dump())
    assert {row.model for row in proof.validate_for(pinned_digest=proof.proof_digest, **arguments)} == {
        "finance", "subscriptions",
    }
    assert all(arguments["sink"].read(key) == value for key, value in retained.items())


@pytest.mark.parametrize("change", ["intent-after-accept", "definition-after-intent", "stale-client-definition"])
async def test_client_refresh_causality_and_definition_freshness_remain_required(tmp_path, change):
    proof, arguments, _ = await current_proof_case(tmp_path, enhanced=True)
    use_original_refresh_intent(proof, arguments)
    profile = arguments["profiles"].metadata_states["finance"]
    anchor = profile.frame_anchor
    intent = json.loads(proof.documents[anchor.full_refresh_intent_hash])
    accepted = json.loads(proof.documents[anchor.accepted_refresh_hash])
    definition = json.loads(proof.documents[anchor.definition_at_frame_hash])
    if change == "intent-after-accept":
        intent["startedAt"] = (_time(accepted["startedAt"]) + timedelta(milliseconds=1)).isoformat()
    elif change == "definition-after-intent":
        definition["startedAt"] = (_time(intent["startedAt"]) + timedelta(milliseconds=1)).isoformat()
    else:
        definition["startedAt"] = (_time(intent["startedAt"]) - timedelta(minutes=6)).isoformat()
    for field, value in (("full_refresh_intent_hash", intent), ("definition_at_frame_hash", definition)):
        text = json.dumps(value, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        proof.documents[key] = text
        setattr(anchor, field, key)
    arguments["profiles"].metadata_state_pins["finance"] = profile.profile_hash
    proof.source_profiles_hash = digest(arguments["profiles"].model_dump())
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


@pytest.mark.parametrize("change", ["target", "body", "approval", "time"])
async def test_original_refresh_intent_cannot_be_relinked_to_another_request(tmp_path, change):
    proof, arguments, _ = await current_proof_case(tmp_path, enhanced=True)
    use_original_refresh_intent(proof, arguments, change=change)
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


@pytest.mark.parametrize("change", [
    "missing", "partial", "not-full", "not-transactional", "missing-object", "duplicate-object",
    "wrong-partition", "object-failed", "wrong-path", "wrong-time", "native-relabel", "extra-object-field",
    "implicit-body",
])
async def test_enhanced_refresh_metadata_cannot_fabricate_complete_or_native_framing(tmp_path, change):
    proof, arguments, _ = await current_proof_case(tmp_path, enhanced=True)
    profile = arguments["profiles"].metadata_states["finance"]
    anchor = profile.frame_anchor
    details = json.loads(proof.documents[anchor.refresh_details_hash])
    values = details["response"]
    if change == "missing":
        anchor.refresh_details_hash = None
    elif change == "partial":
        values["isPartial"] = True
    elif change == "not-full":
        values["type"] = "Calculate"
    elif change == "not-transactional":
        values["commitMode"] = "PartialBatch"
    elif change == "missing-object":
        values["objects"].pop()
    elif change == "duplicate-object":
        values["objects"][1] = dict(values["objects"][0])
    elif change == "wrong-partition":
        values["objects"][0]["partition"] = "Unreviewed partial partition"
    elif change == "object-failed":
        values["objects"][0]["status"] = "Failed"
    elif change == "wrong-path":
        details["path"] += "-different"
    elif change == "wrong-time":
        values["startTime"] = "2000-01-01T00:00:00Z"
    elif change == "native-relabel":
        profile.frame_refresh_type = "DirectLakeFraming"
    elif change == "implicit-body":
        body = {"notifyOption": "NoNotification"}
        text = json.dumps(body, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        proof.documents[key] = text
        anchor.full_refresh_body_hash = key
        accepted = json.loads(proof.documents[anchor.accepted_refresh_hash])
        accepted["bodySha256"] = digest(body)
        text = json.dumps(accepted, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        proof.documents[key] = text
        anchor.accepted_refresh_hash = key
    else:
        values["objects"][0]["isPartial"] = False
    if change != "missing":
        text = json.dumps(details, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        proof.documents[key] = text
        anchor.refresh_details_hash = key
    arguments["profiles"].metadata_state_pins["finance"] = profile.profile_hash
    proof.source_profiles_hash = digest(arguments["profiles"].model_dump())
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


@pytest.mark.parametrize("change", [
    "approval", "request", "response", "active-file", "extra-file", "wrong-incarnation", "log-hash",
    "byte-hash", "typed-vs-stream", "target", "manifest", "time", "missing-frame", "selective-frame",
    "wrong-frame", "author-shape", "epoch", "missing-original", "scope-pin",
])
async def test_current_audit_or_reader_full_frame_gaps_never_become_complete_population(tmp_path, change):
    proof, arguments, binding = await current_proof_case(tmp_path)
    evidence = proof.sources["finance"]

    def replace_document(field, edit):
        value = json.loads(proof.documents[getattr(evidence, field)])
        edit(value)
        text = json.dumps(value, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        proof.documents[key] = text
        setattr(evidence, field, key)

    if change == "approval":
        replace_document("read_approval_hash", lambda value: value.update(approved=False))
    elif change in {"request", "response"}:
        field = "file_intent_hashes" if change == "request" else "file_response_hashes"
        value = json.loads(proof.documents[getattr(evidence, field)[0]])
        value["unexpected"] = "Hash-consistent but not the original linked request/response"
        text = json.dumps(value, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        proof.documents[key] = text
        getattr(evidence, field)[0] = key
    elif change == "active-file":
        replace_document("listing_after_hash", lambda value: value["body"]["paths"].pop())
    elif change == "extra-file":
        proof.binary_objects["d" * 64] = base64.b64encode(b"unrelated source").decode()
    elif change == "wrong-incarnation":
        replace_document("read_plan_hash", lambda value: value["operations"][0].update(
            deltaTableId="00000000-0000-0000-0000-000000000777",
        ))
    elif change == "log-hash":
        evidence.log_body_hashes["account"] = "f" * 64
    elif change == "byte-hash":
        proof.binary_objects[next(iter(proof.binary_objects))] = base64.b64encode(b"changed").decode()
    elif change == "typed-vs-stream":
        manifest = json.loads(proof.documents[evidence.source_manifest_file_hash])
        evidence.population.table_hashes = {
            name: next(table["content_sha256"] for table in manifest["tables"] if table["id"] == name)
            for name in evidence.population.table_hashes
        }
    elif change == "target":
        replace_document("target_map_hash", lambda value: value.update(tenant_id="different-tenant"))
    elif change == "manifest":
        replace_document("source_manifest_file_hash", lambda value: value.update(seed=12345))
    elif change == "time":
        replace_document("attestation_hash", lambda value: value.update(observed_at="2099-01-01T00:00:00Z"))
    elif change in {"missing-frame", "selective-frame", "wrong-frame", "author-shape"}:
        profile = arguments["profiles"].metadata_states["finance"]
        if change == "missing-frame":
            profile.frame_anchor = None
        elif change == "author-shape":
            profile.provenance = "author_shape_capture"
        else:
            field = "full_refresh_body_hash" if change == "selective-frame" else "accepted_refresh_hash"
            value = json.loads(proof.documents[getattr(profile.frame_anchor, field)])
            value.update(objects=[{"table": "Account Month"}]) if change == "selective-frame" else value.update(
                location="/v1.0/myorg/groups/unrelated/datasets/wrong/refreshes/unrelated",
            )
            text = json.dumps(value, sort_keys=True)
            key = hashlib.sha256(text.encode()).hexdigest()
            proof.documents[key] = text
            setattr(profile.frame_anchor, field, key)
        arguments["profiles"].metadata_state_pins["finance"] = profile.profile_hash
        proof.source_profiles_hash = digest(arguments["profiles"].model_dump())
    elif change == "epoch":
        arguments["scope"] = binding.scope.model_copy(update={"authorization_revision": "synthetic-epoch-B"})
    elif change == "missing-original":
        del proof.documents[evidence.file_response_hashes[0]]
    else:
        with pytest.raises(LabError, match="review digest"):
            proof.validate_for(pinned_digest="0" * 64, **arguments)
        return
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


async def test_current_audit_standalone_rechecks_original_shapes_without_giving_reader_authority(tmp_path):
    proof, arguments, binding = await current_proof_case(tmp_path)
    capture = proof.sources["finance"]
    values = verify_current_p0_audit(
        capture, proof.documents, proof.binary_objects, arguments["catalog"], binding,
        arguments["profiles"].metadata_states["finance"],
    )
    assert len(values) == 10
    assert set(capture.evidence_ids()) <= set(proof.documents) | {
        capture.source_capture_id, capture.source_response_hash,
    }
    assert "current_attestation_hash" in capture.state_anchor_refs()
    assert "load_job_hash" not in capture.state_anchor_refs()


@pytest.mark.parametrize("field,value", [("deployment_actor", "not-a-deployment"), ("load_assertion_status", "COMPLETE")])
async def test_current_read_variant_cannot_be_relabelled_as_historical_load_proof(tmp_path, field, value):
    proof, arguments, _ = await current_proof_case(tmp_path)
    data = proof.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValueError, match="cannot declare a historical"):
        ReviewedSourceProof.model_validate(data)
    setattr(proof, field, value)
    with pytest.raises(LabError, match="complete original two-model population"):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)


def current_lro(proof, arguments):
    profile = arguments["profiles"].metadata_states["finance"]
    anchor = profile.frame_anchor
    initial = json.loads(proof.documents[anchor.definition_at_frame_hash])
    body = initial["response"]
    operation = "00000000-0000-0000-0000-000000000901"
    operation_path = "/v1/operations/" + operation
    started = datetime.fromisoformat(initial["startedAt"])

    def original(value):
        text = json.dumps(value, sort_keys=True)
        key = hashlib.sha256(text.encode()).hexdigest()
        proof.documents[key] = text
        return key

    initial.update(status=202, operationId=operation,
                   location="https://api.fabric.microsoft.com" + operation_path, response=None)
    state = {
        "method": "GET", "path": operation_path, "status": 200,
        "response": {"status": "Succeeded"}, "startedAt": (started + timedelta(milliseconds=100)).isoformat(),
        "location": "https://api.fabric.microsoft.com" + operation_path + "/result",
    }
    result = {
        "method": "GET", "path": operation_path + "/result", "status": 200, "response": body,
        "startedAt": (started + timedelta(milliseconds=200)).isoformat(),
    }
    anchor.definition_at_frame_hash = original(initial)
    anchor.definition_state_hashes = [original(state)]
    anchor.definition_result_hash = original(result)
    arguments["profiles"].metadata_state_pins["finance"] = profile.profile_hash
    proof.source_profiles_hash = digest(arguments["profiles"].model_dump())
    return profile, original


async def test_current_frame_accepts_actual_public_lro_receipt_shapes_without_rewriting_accepted_post(tmp_path):
    proof, arguments, _ = await current_proof_case(tmp_path)
    profile, _ = current_lro(proof, arguments)
    accepted_hash = profile.frame_anchor.definition_at_frame_hash
    assert {value.model for value in proof.validate_for(pinned_digest=proof.proof_digest, **arguments)} == {
        "finance", "subscriptions",
    }
    assert profile.frame_anchor.definition_at_frame_hash == accepted_hash
    assert json.loads(proof.documents[accepted_hash])["status"] == 202


@pytest.mark.parametrize("change", [
    "wrong-state-id", "failed", "partial", "missing-result-location", "wrong-result-id",
    "result-before-state", "unbounded-polls", "missing-state", "missing-result",
])
async def test_current_frame_lro_unknown_failed_or_unlinked_receipts_stay_blocked(tmp_path, change):
    proof, arguments, _ = await current_proof_case(tmp_path)
    profile, original = current_lro(proof, arguments)
    anchor = profile.frame_anchor
    state = json.loads(proof.documents[anchor.definition_state_hashes[0]])
    result = json.loads(proof.documents[anchor.definition_result_hash])
    if change == "wrong-state-id":
        state["path"] += "-different"
    elif change == "failed":
        state["response"]["status"] = "Failed"
    elif change == "partial":
        state["response"]["partial"] = True
    elif change == "missing-result-location":
        state["location"] = None
    elif change == "wrong-result-id":
        result["path"] += "-different"
    elif change == "result-before-state":
        result["startedAt"] = (datetime.fromisoformat(state["startedAt"]) - timedelta(seconds=1)).isoformat()
    if change == "unbounded-polls":
        anchor.definition_state_hashes *= profile.max_lro_polls + 1
    elif change == "missing-state":
        anchor.definition_state_hashes = []
    else:
        anchor.definition_state_hashes = [original(state)]
    anchor.definition_result_hash = None if change == "missing-result" else original(result)
    arguments["profiles"].metadata_state_pins["finance"] = profile.profile_hash
    proof.source_profiles_hash = digest(arguments["profiles"].model_dump())
    with pytest.raises(LabError):
        proof.validate_for(pinned_digest=proof.proof_digest, **arguments)
