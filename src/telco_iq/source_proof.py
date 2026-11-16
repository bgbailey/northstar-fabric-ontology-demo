"""Private reviewed import, not a source-population discovery service or grant.

The independent digest approves the normalized assertions AND retained original
deployment/load/inventory documents. No fixture manifest is promoted to live
proof. Actual source-state captures are re-opened, not accepted as hashes alone.
"""

import ast
import base64
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator
from referencing import Registry

from telco_iq.approval import private_config_path
from telco_iq.catalog import Catalog, compatible_populations, digest
from telco_iq.contracts import Contract, Scope, Sha256, SourcePopulation, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import ReaderBinding, SourceIdentity
from telco_iq.mcp_client import pointer


def _original(text: str) -> dict:
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate original-evidence property")
            result[name] = value
        return result
    value = json.loads(text, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Original source evidence must be a complete JSON object")
    return value


class PopulationEvidence(Contract):
    identity: SourceIdentity
    population: SourcePopulation
    source_schema_hash: Sha256
    data_revision: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    source_capture_id: Sha256
    source_response_hash: Sha256
    metadata_capture_ids: list[Sha256] | None = Field(default=None, exclude_if=lambda value: value is None)

    def state_anchor_refs(self) -> dict[str, str]:
        raise NotImplementedError

    def evidence_ids(self) -> list[str]:
        raise NotImplementedError


class PopulationCapture(PopulationEvidence):
    receipt_kind: Literal["HERO_V1", "LEGACY_P0"] = Field(
        default="HERO_V1", exclude_if=lambda value: value == "HERO_V1",
    )
    deployment_packet_hash: Sha256
    deployment_approval_hash: Sha256
    source_manifest_file_hash: Sha256
    load_assertion_hash: Sha256
    model_inventory_hash: Sha256
    deployment_receipts: dict[Literal[
        "load_accepted", "load_completed", "notebook_body", "source_binding",
        "model_authorization", "model_body", "model_create", "model_item", "model_result", "definition_accepted",
        "workspace_item", "lakehouse_item", "notebook_item", "notebook_template", "notebook_create",
        "notebook_http_create", "notebook_result", "load_http_submitted", "load_http_completed",
        "model_http_create", "notebook_repair_approval", "notebook_repair_applied",
        "notebook_definition", "notebook_update", "notebook_update_completed",
    ], Sha256]
    def state_anchor_refs(self) -> dict[str, str]:
        return {
            "model_body_hash": self.deployment_receipts["model_body"],
            "load_job_hash": self.load_assertion_hash if self.receipt_kind == "LEGACY_P0"
            else self.deployment_receipts["load_completed"],
        }

    def evidence_ids(self):
        return [self.source_capture_id, self.source_response_hash, self.deployment_packet_hash,
                self.source_manifest_file_hash, self.load_assertion_hash, self.model_inventory_hash]


class CurrentP0ReadCapture(PopulationEvidence):
    """Current source bytes are population evidence, not historical deployment proof."""

    receipt_kind: Literal["CURRENT_P0_READ_ATTESTATION"]
    source_manifest_file_hash: Sha256
    attestation_hash: Sha256
    read_plan_hash: Sha256
    read_approval_hash: Sha256
    target_map_hash: Sha256
    expected_identity_hash: Sha256
    listing_before_hash: Sha256
    listing_after_hash: Sha256
    after_listing_intent_hash: Sha256
    log_body_hashes: dict[str, Sha256]
    log_response_hashes: dict[str, Sha256]
    file_intent_hashes: list[Sha256]
    file_response_hashes: list[Sha256]
    expected_files: dict[str, Sha256]
    bound_definition_hash: Sha256

    def state_anchor_refs(self) -> dict[str, str]:
        return {
            "model_body_hash": self.bound_definition_hash,
            "current_attestation_hash": self.attestation_hash,
        }

    def evidence_ids(self) -> list[str]:
        return [
            self.source_capture_id, self.source_response_hash, self.attestation_hash, self.read_plan_hash,
            self.read_approval_hash, self.source_manifest_file_hash, self.bound_definition_hash,
            self.listing_before_hash, self.listing_after_hash,
        ]


class CurrentShowcaseReadCapture(CurrentP0ReadCapture):
    receipt_kind: Literal["CURRENT_SHOWCASE_READ_ATTESTATION_V1"]
    log_request_hashes: dict[str, Sha256]


def _parquet_rows(raw: bytes, table: dict) -> tuple[list[dict], list[str]]:
    """Conformed typed rows, never metric/oracle arithmetic or file-order equality."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if len(raw) > 2_000_000:
        raise ValueError("Current P0 Parquet exceeds bounded import size")
    parquet = pq.ParquetFile(io.BytesIO(raw))
    if parquet.metadata.num_rows > 10_000 or parquet.schema_arrow.names != list(table["columns"]):
        raise ValueError("Current P0 Parquet source columns/count differ")
    predicates = {
        "int64": pa.types.is_int64, "string": pa.types.is_string, "bool": pa.types.is_boolean,
        "date": pa.types.is_date32, "timestamp": pa.types.is_timestamp, "float64": pa.types.is_float64,
    }
    for field in parquet.schema_arrow:
        if not predicates[table["columns"][field.name]](field.type):
            raise ValueError("Current P0 Parquet source types differ")
        if pa.types.is_timestamp(field.type) and (field.type.unit != "us" or field.type.tz not in {"UTC", "+00:00", "Etc/UTC"}):
            raise ValueError("Current P0 timestamps require exact UTC microseconds")
    rows, seen = [], set()
    for row in parquet.read().to_pylist():
        key = tuple(row[name] for name in table["primaryKey"])
        if None in key or key in seen:
            raise ValueError("Current P0 primary key is null or duplicated")
        seen.add(key)
        rows.append({name: value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime)
                     else value.isoformat() if hasattr(value, "isoformat") else value for name, value in row.items()})
    return rows, table["primaryKey"]


def _typed_commitment(rows: list[dict], keys: list[str], *, algorithm: str = "CURRENT_P0_TYPED_PK_V1") -> str:
    return digest({"algorithm": algorithm,
                   "rows": sorted(rows, key=lambda row: tuple(row[key] for key in keys))})


def verify_current_p0_rows(
    catalog: Catalog, manifest: dict, file_records: list[dict], table_records: list[dict],
    expected_files: dict[str, str], binary_objects: dict[str, str],
    *, selected_models: tuple[str, ...] = ("subscriptions", "finance"),
    maximum_files: int = 70, commitment_algorithm: str = "CURRENT_P0_TYPED_PK_V1",
) -> dict[str, str]:
    """Independent byte-level population check; no report boolean is authority."""
    if not selected_models or len(set(selected_models)) != len(selected_models) or not set(selected_models) <= catalog.models.keys():
        raise ValueError("Exact unique source models are required for current-row verification")
    selected = {name for alias in selected_models for name in catalog.models[alias]["tables"]}
    declared = {table["id"]: table for table in manifest["tables"]}
    reports = {table["table"]: table for table in table_records}
    if (
        set(expected_files) != selected or set(reports) != selected or len(reports) != len(table_records)
        or not 0 < len(file_records) <= maximum_files
        or len({item["source_path"] for item in file_records}) != len(file_records)
        or {item["table"] for item in file_records} != selected
    ):
        raise ValueError("Complete original P0 selected table/file sets are required")

    def raw(key):
        value = base64.b64decode(binary_objects[key], validate=True)
        if len(value) > 2_000_000 or hashlib.sha256(value).hexdigest() != key:
            raise ValueError("Pinned current/expected Parquet bytes changed")
        return value

    commitments = {}
    for name in sorted(selected):
        metadata, report = declared[name], reports[name]
        expected_hash = expected_files[name]
        if expected_hash != metadata["file_sha256"] or report["expected_file_sha256"] != expected_hash or (
            report["declared_logical_sha256"] != metadata["content_sha256"]
        ):
            raise ValueError("Current read does not reference original expected source file/manifest")
        expected, keys = _parquet_rows(raw(expected_hash), catalog.tables[name])
        stream = hashlib.sha256(json.dumps(catalog.tables[name]["columns"], sort_keys=True, separators=(",", ":")).encode())
        for row in expected:
            stream.update(b"\n" + json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
        if stream.hexdigest() != metadata["content_sha256"] or len(expected) != metadata["rows"]:
            raise ValueError("Expected original source bytes do not match manifest logical stream")
        files = [item for item in file_records if item["table"] == name]
        current = []
        for item in files:
            payload = raw(item["sha256"])
            if (
                item["status"] != 200 or type(item["bytes"]) is not int or item["bytes"] != len(payload)
                or int(item["headers"]["content-length"]) != len(payload) or not item["headers"]["etag"]
            ):
                raise ValueError("Actual current file GET/status/size/etag evidence is incomplete")
            rows, _ = _parquet_rows(payload, catalog.tables[name])
            current += rows
        if type(report["rows"]) is not int or len(current) != len(expected) or report["rows"] != len(current) or (
            type(report["files"]) is not int or report["files"] != len(files)
            or len({tuple(row[key] for key in keys) for row in current}) != len(current)
        ):
            raise ValueError("Current conformed rows/count/files/PK uniqueness differ")
        expected_commitment = _typed_commitment(expected, keys, algorithm=commitment_algorithm)
        if _typed_commitment(current, keys, algorithm=commitment_algorithm) != expected_commitment:
            raise ValueError("Current typed source population differs from original expected bytes")
        commitments[name] = expected_commitment
    return commitments


def verify_current_p0_audit(
    evidence: CurrentP0ReadCapture, documents: dict[str, str], binary_objects: dict[str, str],
    catalog: Catalog, binding: ReaderBinding, profile,
) -> dict[str, str]:
    """Reopen the original audit envelopes and complete Delta/file/row authority chain."""
    from telco_iq.source_observation import _bindings

    def original(key):
        text = documents[key]
        if hashlib.sha256(text.encode()).hexdigest() != key:
            raise ValueError("Current P0 original document hash differs")
        return _original(text)

    def timestamp(value):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Current P0 original timestamp has no timezone")
        return result

    def listing(body):
        entries = body["paths"]
        paths = {item["name"]: item for item in entries}
        if len(paths) != len(entries) or any(body.get(key) for key in (
            "ContinuationToken", "continuationToken", "error", "errors", "partial", "isPartial",
        )):
            raise ValueError("Current P0 original listing is incomplete")
        return paths

    def etag(value):
        if type(value) is not str or not value.strip('"'):
            raise ValueError("Current P0 service ETag is absent")
        return value.strip('"')

    report, plan, approval, target, identity, manifest = [
        original(getattr(evidence, name)) for name in (
            "attestation_hash", "read_plan_hash", "read_approval_hash", "target_map_hash",
            "expected_identity_hash", "source_manifest_file_hash",
        )
    ]
    selected = {name for alias in ("subscriptions", "finance") for name in catalog.models[alias]["tables"]}
    if set(binding.sources) != {"subscriptions", "finance"} or evidence.identity != binding.sources.get(
        evidence.population.model,
    ) or profile.identity != evidence.identity:
        raise ValueError("Current P0 audit is limited to the exact original two-model binding")
    if not (
        report["kind"] == "ACTUAL_SELECTED_USER_CURRENT_SOURCE_DATA_READ_ATTESTATION"
        and approval["approved"] is True
        and approval["proposal_sha256"] == report["proposal_sha256"] == evidence.read_plan_hash
        and plan["targetMapSha256"] == evidence.target_map_hash
        and plan["expectedIdentitySha256"] == evidence.expected_identity_hash
        and plan["manifestFileSha256"] == evidence.source_manifest_file_hash
        and target["tenant_id"] == binding.scope.tenant
        and identity["expected_user"] and target["expected_identity_file"] == plan["expectedIdentityFile"]
        and target["protected_p0_workspace"]["id"] == str(profile.identity.workspace_id)
        and manifest["profile"] == "miniature"
        and type(manifest["seed"]) is int
    ):
        raise ValueError("Current P0 original approval/plan/target/identity/manifest linkage differs")
    items = {item["id"]: item for item in target["p0_items"]}
    if len(items) != len(target["p0_items"]) or any(
        items.get(str(source.dataset_id), {}).get("type") != "SemanticModel"
        or items[str(source.dataset_id)]["workspaceId"] != str(source.workspace_id)
        for source in binding.sources.values()
    ) or items.get(str(profile.lakehouse_id), {}).get("type") != "Lakehouse":
        raise ValueError("Current P0 audit source/model/lakehouse targets differ")
    definition = original(evidence.bound_definition_hash)
    _bindings(definition, profile)
    before_text = documents[evidence.listing_before_hash]
    if hashlib.sha256(before_text.encode()).hexdigest() != plan["sourceMetadataPathsSha256"]:
        raise ValueError("Current P0 original before-listing hash differs")
    before = listing(original(evidence.listing_before_hash))
    after = original(evidence.listing_after_hash)
    after_intent = original(evidence.after_listing_intent_hash)
    if (
        after["status"] != 200 or after["headers"].get("x-ms-continuation")
        or after_intent != {"method": "GET", "url": plan["afterMetadataRequest"]["url"]}
        or before != listing(after["body"])
    ):
        raise ValueError("Current P0 original after-listing is partial, changed or unlinked")
    listing_uri = urlsplit(plan["afterMetadataRequest"]["url"])
    from urllib.parse import parse_qs
    query = parse_qs(listing_uri.query)
    if (
        listing_uri.scheme != "https" or listing_uri.netloc != "centralus-onelake.dfs.fabric.microsoft.com"
        or listing_uri.path != "/" + str(profile.identity.workspace_id)
        or listing_uri.fragment or plan["afterMetadataRequest"]["method"] != "GET"
        or query.get("directory") != [str(profile.lakehouse_id) + "/Tables"]
        or query.get("resource") != ["filesystem"] or query.get("recursive") != ["true"]
        or query.get("upn") not in (None, ["false"])
        or set(query) - {"directory", "resource", "recursive", "maxResults", "upn"}
    ):
        raise ValueError("Current P0 after-listing target is outside the bound lakehouse")
    start, end = timestamp(report["source_capture_started_at"]), timestamp(report["observed_at"])
    if not (
        timestamp(plan["createdAt"]) <= start <= end == timestamp(after["observed_at"])
        <= timestamp(approval["expires_at"]) <= timestamp(plan["expiresAt"])
        and end <= timestamp(report["verified_offline_at"])
    ):
        raise ValueError("Current P0 original read/approval/verification times differ")
    tables = {table["id"]: table for table in manifest["tables"]}
    planned_tables = {table["table"]: table for table in plan["tables"]}
    if len(tables) != len(manifest["tables"]) or set(planned_tables) != selected or (
        len(planned_tables) != len(plan["tables"])
        or set(evidence.log_body_hashes) != selected or set(evidence.log_response_hashes) != selected
    ):
        raise ValueError("Current P0 original table/log inventory is incomplete")
    wid, lid = str(profile.identity.workspace_id), str(profile.lakehouse_id)
    active, incarnations = {}, {}
    for name in sorted(selected):
        expected = planned_tables[name]
        if (
            not isinstance(expected["columns"], list)
            or len(expected["columns"]) != len(catalog.tables[name]["columns"])
            or set(expected["columns"]) != set(catalog.tables[name]["columns"])
            or expected["expectedRows"] != tables[name]["rows"]
            or expected["expectedLogicalContentSha256"] != tables[name]["content_sha256"]
        ):
            raise ValueError("Current P0 original source whitelist/manifest differs")
        log_text = documents[evidence.log_body_hashes[name]]
        response = original(evidence.log_response_hashes[name])
        if (
            hashlib.sha256(log_text.encode()).hexdigest() != evidence.log_body_hashes[name]
            or response["status_code"] != 200 or response["body_sha256"] != evidence.log_body_hashes[name]
            or response["alias"] != "delta-v0-metadata-dbo-" + name
        ):
            raise ValueError("Current P0 original Delta response/body linkage differs")
        actions = [json.loads(line) for line in log_text.splitlines() if line.strip()]
        metadata = [action["metaData"] for action in actions if "metaData" in action]
        if (
            len(metadata) != 1 or metadata[0].get("format", {}).get("provider") != "parquet"
            or [action["protocol"] for action in actions if "protocol" in action]
            != [{"minReaderVersion": 1, "minWriterVersion": 2}]
            or any("remove" in action or "cdc" in action or action.get("add", {}).get("deletionVector") is not None
                   for action in actions)
        ):
            raise ValueError("Current P0 Delta incarnation/protocol is unsupported")
        incarnations[name] = str(UUID(metadata[0]["id"]))
        fields = _original(metadata[0]["schemaString"])["fields"]
        physical_types = {
            "int64": "long", "string": "string", "bool": "boolean",
            "date": "date", "timestamp": "timestamp", "float64": "double",
        }
        if len(fields) != len(catalog.tables[name]["columns"]) or {
            field["name"]: field["type"] for field in fields
        } != {column: physical_types[kind] for column, kind in catalog.tables[name]["columns"].items()}:
            raise ValueError("Current P0 original Delta source-only schema differs")
        prefix = lid + "/Tables/dbo/" + name
        log_path = prefix + "/_delta_log/00000000000000000000.json"
        log_entry = before[log_path]
        if (
            etag(log_entry["etag"]) != etag(response["headers"]["etag"])
            or log_entry["lastModified"] != response["headers"]["last-modified"]
            or any(path.startswith(prefix + "/_delta_log/") and path.endswith(".json")
                   and path != log_path for path in before)
        ):
            raise ValueError("Current P0 Delta log is not the listed sole V0 snapshot")
        for action in actions:
            if "add" not in action:
                continue
            path = action["add"]["path"]
            if "/" in path or "\\" in path or not path.startswith("part-") or not path.endswith(".parquet"):
                raise ValueError("Current P0 Delta file path escapes its source table")
            source_path = "/" + wid + "/" + prefix + "/" + path
            if source_path in active:
                raise ValueError("Current P0 duplicate active Delta file")
            active[source_path] = (name, before[prefix + "/" + path], action["add"]["size"])
    operations = plan["operations"]
    files = report["source_files"]
    if not (
        0 < len(operations) == len(files) == len(active) <= 70
        and len(evidence.file_intent_hashes) == len(evidence.file_response_hashes) == len(files)
        and len({item["source_path"] for item in files}) == len(files)
    ):
        raise ValueError("Current P0 complete active-file request/response set differs")
    responses = {item["source_path"]: item for item in files}
    planned_paths = set()
    for operation, intent_hash, response_hash in zip(
        operations, evidence.file_intent_hashes, evidence.file_response_hashes,
    ):
        uri = urlsplit(operation["url"])
        source_path = unquote(uri.path)
        if (
            uri.scheme != "https" or uri.netloc != "centralus-onelake.dfs.fabric.microsoft.com"
            or uri.query or uri.fragment or source_path != uri.path or source_path in planned_paths
            or source_path not in active or operation["method"] != "GET"
            or operation["audience"] != "https://storage.azure.com"
        ):
            raise ValueError("Current P0 approved file endpoint/path differs")
        planned_paths.add(source_path)
        name, listed, size = active[source_path]
        response = original(response_hash)
        if (
            original(intent_hash) != {"proposal_sha256": evidence.read_plan_hash, "operation": operation}
            or response != responses[source_path] or response["table"] != name
            or operation["table"] != name or operation["deltaVersion"] != 0
            or operation["deltaTableId"] != incarnations[name]
            or operation["sourceLogResponseSha256"] != evidence.log_body_hashes[name]
            or operation["expectedBytes"] != response["bytes"] or response["bytes"] != size
            or int(listed["contentLength"]) != size
            or etag(operation["headers"]["If-Match"]) != etag(listed["etag"])
            or etag(response["headers"]["etag"]) != etag(listed["etag"])
            or not start <= timestamp(response["observed_at"]) <= end
        ):
            raise ValueError("Current P0 original file/log/incarnation/ETag authority differs")
    if planned_paths != set(active) or set(responses) != set(active):
        raise ValueError("Current P0 file set is incomplete")
    if set(binary_objects) != set(evidence.expected_files.values()) | {item["sha256"] for item in files}:
        raise ValueError("Current P0 byte import contains missing or unrelated objects")
    limits = plan["limits"]
    if (
        len(files) > approval["maximum_data_gets"] or len(files) > limits["dataFileGets"]
        or approval["maximum_after_listing_gets"] != 1 or limits["afterListingGets"] != 1
        or sum(item["bytes"] for item in files) != limits["sourceBytes"]
        or limits["sourceBytes"] > min(approval["maximum_bytes"], limits["maximumBytes"])
        or sum(item["rows"] for item in report["tables"]) != limits["expectedLogicalRows"]
        or limits["expectedLogicalRows"] > min(approval["maximum_rows"], limits["maximumRows"])
    ):
        raise ValueError("Current P0 original finite read budgets differ")
    return verify_current_p0_rows(
        catalog, manifest, files, report["tables"], evidence.expected_files, binary_objects,
    )


def verify_current_showcase_audit(
    evidence: CurrentShowcaseReadCapture, documents: dict[str, str], binary_objects: dict[str, str],
    catalog: Catalog, binding: ReaderBinding, profile,
) -> dict[str, str]:
    """Verify the observed usage-scoped showcase audit without recasting it as a P0 deployment."""
    from urllib.parse import parse_qs

    from telco_iq.source_observation import _bindings

    def original(key):
        text = documents[key]
        if hashlib.sha256(text.encode()).hexdigest() != key:
            raise ValueError("Current showcase original document hash differs")
        return _original(text)

    def timestamp(value):
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Current showcase audit timestamp is not timezone-aware")
        return stamp

    def paths(body):
        entries = body["paths"]
        mapped = {item["name"]: item for item in entries}
        if len(mapped) != len(entries) or any(body.get(key) for key in (
            "ContinuationToken", "continuationToken", "error", "errors", "partial", "isPartial",
        )):
            raise ValueError("Current showcase source listing is incomplete")
        return mapped

    def etag(value):
        if type(value) is not str or not value.strip('"'):
            raise ValueError("Current showcase service ETag is absent")
        return value.strip('"')

    report, plan, approval, target, identity, manifest = [
        original(getattr(evidence, name)) for name in (
            "attestation_hash", "read_plan_hash", "read_approval_hash", "target_map_hash",
            "expected_identity_hash", "source_manifest_file_hash",
        )
    ]
    models = tuple(sorted(binding.sources))
    selected = {name for alias in models for name in catalog.models[alias]["tables"]}
    if set(models) != set(catalog.models) or len(models) != 8 or len(selected) != 36 or (
        evidence.identity != binding.sources.get(evidence.population.model) or profile.identity != evidence.identity
    ):
        raise ValueError("Current showcase proof requires its complete exact eight-source binding")
    if not (
        report["kind"] == "ACTUAL_SHOWCASE_CURRENT_SOURCE_TYPED_ROW_AUDIT"
        and report["proposal_sha256"] == evidence.read_plan_hash
        and plan["kind"] == "READ_ONLY_EXACT_SHOWCASE_CURRENT_ACTIVE_FILE_ROWS_AND_PK_AUDIT"
        and approval["approved"] is True and approval["proposal_sha256"] == evidence.read_plan_hash
        and plan["work_cutoff"] is None and plan["approval_mode"] == "USAGE_SCOPED"
        and plan["sourceCatalogRevision"] == catalog.revision
        and plan["manifestFileSha256"] == evidence.source_manifest_file_hash
        and plan["sourceMetadataPathsSha256"] == evidence.listing_before_hash
        and set(plan["source_model_aliases"]) == set(models)
        and plan["identity"]["tenant_id"] == binding.scope.tenant
        and plan["expected_reader_object_ids"] == [binding.scope.principal]
        and plan["identity"]["expected_user"] == identity["expected_user"]
        and manifest["profile"] == "showcase-v2" and manifest["version"] == "showcase-v2.3"
        and type(manifest["seed"]) is int
        and target["tenant_id"] == binding.scope.tenant
        and target["workspace_id"] == str(profile.identity.workspace_id) == plan["workspace_id"]
        and target["lakehouse_id"] == str(profile.lakehouse_id) == plan["lakehouse_id"]
        and target["sources"] == {alias: source.model_dump(mode="json") for alias, source in binding.sources.items()}
    ):
        raise ValueError("Current showcase approval/identity/catalog/source linkage differs")
    _bindings(original(evidence.bound_definition_hash), profile)
    before = paths(original(evidence.listing_before_hash))
    after, after_intent = original(evidence.listing_after_hash), original(evidence.after_listing_intent_hash)
    if after["status"] != 200 or after["headers"].get("x-ms-continuation") or (
        after_intent != plan["afterMetadataRequest"]
    ):
        raise ValueError("Current showcase final listing is incomplete or unlinked")
    current = paths(after["body"])
    uri = urlsplit(after_intent["url"])
    query = parse_qs(uri.query)
    wid, lid = str(profile.identity.workspace_id), str(profile.lakehouse_id)
    if (
        after_intent["method"] != "GET" or uri.scheme != "https"
        or uri.netloc != "centralus-onelake.dfs.fabric.microsoft.com"
        or plan["onelake_dfs_endpoint"] != "https://" + uri.netloc or uri.path != "/" + wid or uri.fragment
        or query.get("directory") != [lid + "/Tables"] or query.get("resource") != ["filesystem"]
        or query.get("recursive") != ["true"] or query.get("upn") not in (None, ["false"])
        or set(query) - {"directory", "resource", "recursive", "maxResults", "upn"}
    ):
        raise ValueError("Current showcase inventory escaped the bound source lakehouse")
    start, end = timestamp(report["source_capture_started_at"]), timestamp(report["observed_at"])
    if not timestamp(plan["created_at"]) <= start <= end == timestamp(after["observed_at"]) <= timestamp(report["verified_offline_at"]):
        raise ValueError("Current showcase original audit clocks differ")
    if any(set(values) != selected for values in (
        evidence.log_body_hashes, evidence.log_response_hashes, evidence.log_request_hashes, evidence.expected_files,
    )):
        raise ValueError("Current showcase complete source/log file selection differs")
    active = {}
    for name in sorted(selected):
        text = documents[evidence.log_body_hashes[name]]
        if hashlib.sha256(text.encode()).hexdigest() != evidence.log_body_hashes[name]:
            raise ValueError("Current showcase source Delta body changed")
        request, response = original(evidence.log_request_hashes[name]), original(evidence.log_response_hashes[name])
        prefix = lid + "/Tables/dbo/" + name
        log_path = prefix + "/_delta_log/00000000000000000000.json"
        if (
            request["method"] != "GET" or request["url"] != plan["onelake_dfs_endpoint"] + "/" + wid + "/" + log_path
            or response["http_status"] != 200 or response["body_sha256"] != evidence.log_body_hashes[name]
            or timestamp(response["observed_at"]) > start or timestamp(request["started_at"]) > timestamp(response["observed_at"])
            or current.get(log_path) != before[log_path]
            or etag(before[log_path]["etag"]) != etag(response["headers"]["etag"])
        ):
            raise ValueError("Current showcase original Delta read/ETag/target is unlinked")
        for listing in (before, current):
            if any(path.startswith(prefix + "/_delta_log/") and "/" not in path[len(prefix + "/_delta_log/"):]
                   and path.endswith(".json") and path != log_path for path in listing):
                raise ValueError("Current showcase audit is not a soleV0Delta snapshot")
        actions = [json.loads(line) for line in text.splitlines() if line.strip()]
        metadata = [item["metaData"] for item in actions if "metaData" in item]
        if (
            len(metadata) != 1 or metadata[0].get("format", {}).get("provider") != "parquet"
            or [item["protocol"] for item in actions if "protocol" in item] != [{"minReaderVersion": 1, "minWriterVersion": 2}]
            or any("remove" in item or "cdc" in item or item.get("add", {}).get("deletionVector") is not None for item in actions)
        ):
            raise ValueError("Current showcase Delta actions/incarnation are unsupported")
        incarnation = str(UUID(metadata[0]["id"]))
        types = {"int64": "long", "string": "string", "bool": "boolean",
                 "date": "date", "timestamp": "timestamp", "float64": "double"}
        fields = _original(metadata[0]["schemaString"])["fields"]
        if len(fields) != len(catalog.tables[name]["columns"]) or {
            field["name"]: field["type"] for field in fields
        } != {column: types[kind] for column, kind in catalog.tables[name]["columns"].items()}:
            raise ValueError("Current showcase exact physical source schema differs")
        for item in actions:
            if "add" not in item:
                continue
            filename = item["add"]["path"]
            if "/" in filename or "\\" in filename or not filename.startswith("part-") or not filename.endswith(".parquet"):
                raise ValueError("Current showcase Delta file escaped its source table")
            name_on_lake = prefix + "/" + filename
            source_path = "/" + wid + "/" + name_on_lake
            if source_path in active or current.get(name_on_lake) != before[name_on_lake]:
                raise ValueError("Current showcase active source file changed or was repeated")
            active[source_path] = (name, incarnation, before[name_on_lake], item["add"]["size"], evidence.log_body_hashes[name])
    operations, files, limits = plan["operations"], report["source_files"], plan["limits"]
    if (
        not 0 < len(operations) == len(files) == len(active) <= 512
        or len(evidence.file_intent_hashes) != len(files) or len(evidence.file_response_hashes) != len(files)
        or len({item["source_path"] for item in files}) != len(files)
    ):
        raise ValueError("Current showcase complete active-file read set differs")
    reports = {item["source_path"]: item for item in files}
    planned = set()
    for operation, intent_hash, response_hash in zip(operations, evidence.file_intent_hashes, evidence.file_response_hashes, strict=True):
        request_uri = urlsplit(operation["url"])
        path = unquote(request_uri.path)
        if (
            operation["method"] != "GET" or operation["audience"] != "https://storage.azure.com"
            or request_uri.scheme != "https" or request_uri.netloc != uri.netloc
            or request_uri.query or request_uri.fragment or path != request_uri.path
            or path not in active or path in planned or operation["source_path"] != path
        ):
            raise ValueError("Current showcase file operation escaped its exact approved Delta set")
        planned.add(path)
        name, incarnation, listed, size, log_hash = active[path]
        intent, response = original(intent_hash), original(response_hash)
        if (
            intent["proposal_sha256"] != evidence.read_plan_hash or intent["operation"] != operation
            or response != reports[path] or response["table"] != operation["table"] or response["table"] != name
            or response["status"] != 200 or response["bytes"] != operation["expectedBytes"] or response["bytes"] != size
            or int(listed["contentLength"]) != size or int(response["headers"]["content-length"]) != size
            or operation["Delta_version"] != 0 or operation["incarnation_id"] != incarnation or operation["log_body_sha256"] != log_hash
            or etag(operation["headers"]["If-Match"]) != etag(listed["etag"])
            or etag(response["headers"]["etag"]) != etag(listed["etag"])
            or not start <= timestamp(intent["started_at"]) <= timestamp(response["observed_at"]) <= end
        ):
            raise ValueError("Current showcase original conditional file bytes/metadata/identity differ")
    if planned != set(active) or set(reports) != set(active) or set(binary_objects) != (
        set(evidence.expected_files.values()) | {item["sha256"] for item in files}
    ):
        raise ValueError("Current showcase data-file/byte set is incomplete or contains unrelated objects")
    if (
        len(files) != plan["maximum_data_GETs"] or limits["dataFileGets"] != len(files)
        or limits["afterListingGets"] != plan["maximum_after_listing_GETs"] or limits["afterListingGets"] != 1
        or sum(item["bytes"] for item in files) != limits["sourceBytes"] or limits["sourceBytes"] > limits["maximumBytes"]
        or sum(item["rows"] for item in report["tables"]) != limits["expectedLogicalRows"]
        or limits["expectedLogicalRows"] > limits["maximumRows"]
    ):
        raise ValueError("Current showcase exact finite read budget differs")
    declared = {item["id"]: item for item in manifest["tables"]}
    table_reports = [{**item, "declared_logical_sha256": declared[item["table"]]["content_sha256"]}
                     for item in report["tables"]]
    commitments = verify_current_p0_rows(
        catalog, manifest, files, table_reports, evidence.expected_files, binary_objects,
        selected_models=models, maximum_files=512, commitment_algorithm="CURRENT_SOURCE_TYPED_PK_V1",
    )
    if any(item["typed_commitment"] != commitments[item["table"]] for item in report["tables"]):
        raise ValueError("Current showcase reviewed typed-row commitment differs from actual bytes")
    return commitments


class ReviewedSourceProof(Contract):
    version: Literal[1] = 1
    proof_kind: Literal["ACTUAL_APPROVED_SOURCE_CAPTURE", "SYNTHETIC_TEST"]
    scope: Scope
    captured_at: datetime
    reviewed_at: datetime
    expires_at: datetime
    review_reference: str = Field(min_length=1)
    deployment_actor: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None)
    catalog_hash: Sha256
    native_snapshot_hash: Sha256
    enrollment_digest: Sha256
    source_profiles_hash: Sha256
    context_contract_hash: Sha256
    # Complete original structured documents, privately reviewed; not wire bytes.
    documents: dict[Sha256, str] = Field(min_length=1)
    sources: dict[str, PopulationCapture | CurrentP0ReadCapture | CurrentShowcaseReadCapture] = Field(min_length=1)
    binary_objects: dict[Sha256, str] | None = Field(default=None, exclude_if=lambda value: value is None)
    load_assertion_status: Literal["COMPLETE"] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def timestamps(self):
        if (
            any(t.tzinfo is None for t in (self.captured_at, self.reviewed_at, self.expires_at))
            or not self.captured_at <= self.reviewed_at < self.expires_at
            or not self.review_reference.strip()
        ):
            raise ValueError("Source proof requires reviewed, expiring timestamped captures")
        if all(isinstance(source, CurrentP0ReadCapture) for source in self.sources.values()):
            if self.deployment_actor is not None or self.load_assertion_status is not None:
                raise ValueError("Current-read evidence cannot declare a historical deployment actor or load assertion")
        elif self.deployment_actor is None or self.load_assertion_status != "COMPLETE":
            raise ValueError("Deployment population proof requires its original actor and complete load assertion")
        return self

    @classmethod
    def read(cls, path: Path, repo: Path):
        path = private_config_path(path, repo)
        if path.stat().st_size > 16_000_000:
            raise LabError("Private source proof exceeds import limit", Status.BLOCKED_CAPABILITY)
        return cls.model_validate(_original(path.read_text(encoding="utf-8")))

    @property
    def proof_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    def _deployment_links(self, evidence: PopulationCapture, packet: dict, assertion: dict, approval: dict,
                          catalog: Catalog, *, synthetic: bool) -> bool:
        """Verify existing hero/renewal receipt links, without importing deployment code."""
        required = {
            "load_accepted", "load_completed", "notebook_body", "source_binding",
            "model_authorization", "model_body", "model_create", "model_item",
        }
        if not required <= set(evidence.deployment_receipts):
            return False

        def original(key):
            return _original(self.documents[evidence.deployment_receipts[key]])

        def parts(body):
            entries = body["definition"]["parts"]
            result = {entry["path"]: base64.b64decode(entry["payload"], validate=True) for entry in entries
                      if entry["payloadType"] == "InlineBase64"}
            if len(result) != len(entries):
                raise ValueError("Incomplete or duplicate source definition parts")
            return result

        def location(value):
            parsed = urlsplit(value)
            if parsed.query or parsed.fragment or ((parsed.scheme or parsed.netloc) and (
                parsed.scheme != "https" or parsed.netloc != "api.fabric.microsoft.com"
            )):
                raise ValueError("Original receipt has a noncanonical Fabric location")
            return parsed.path

        accepted, completed = original("load_accepted"), original("load_completed")
        notebook, binding = original("notebook_body"), original("source_binding")
        wid, dataset = str(evidence.identity.workspace_id), str(evidence.identity.dataset_id)
        loader, lakehouse, job = assertion["loaderId"], assertion["lakehouseId"], assertion["jobId"]
        if any(str(UUID(value)) != value for value in (loader, lakehouse, job)):
            return False
        job_path = f"/v1/workspaces/{wid}/items/{loader}/jobs/instances/{job}"
        load_op = next(op for op in approval["operations"] if op["name"] == accepted["operation"])
        level = "SYNTHETIC_LOCAL" if synthetic else "AUTHOR_SERVICE"
        if not (
            accepted["state"] == "ACCEPTED" and accepted["receiptValidated"] is True
            and accepted["approvalSha256"] == digest(approval)
            and accepted["method"] == load_op["method"] == "POST" and accepted["status"] == 202
            and accepted["path"] == load_op["path"].replace("{hero-loader}", loader)
            == f"/v1/workspaces/{wid}/items/{loader}/jobs/RunNotebook/instances"
            and accepted["bodySha256"] == load_op["bodySha256"] == digest(None)
            and location(accepted["location"]) == job_path and completed["proofLevel"] == level
            and completed["method"] == "GET" and completed["path"] == job_path and completed["status"] == 200
            and completed["response"]["id"] == job and completed["response"]["itemId"] == loader
            and completed["response"]["jobType"] == "RunNotebook"
            and completed["response"]["status"] == "Completed" and not completed["response"].get("failureReason")
            and datetime.fromisoformat(completed["startedAt"]) <= datetime.fromisoformat(assertion["observedAtUtc"])
            and assertion["jobReceiptSha256"] == evidence.deployment_receipts["load_completed"]
            and assertion["notebookBodySha256"] == digest(notebook)
            and binding["packetSha256"] == evidence.deployment_packet_hash
            and binding["workspace"]["id"] == wid and binding["lakehouse"]["id"] == lakehouse
            and binding["lakehouse"]["displayName"] == packet["lakehouseName"]
            and approval["ownedItems"]["hero-lakehouse"] == lakehouse
            and approval["ownedItems"]["hero-loader"] == loader
        ):
            return False
        replacements = {
            "<WORKSPACE_ID>": wid, "<LAKEHOUSE_ID>": lakehouse,
            packet["workspaceName"]: binding["workspace"]["displayName"],
        }
        notebook_template = next(item for item in packet["packetFiles"] if item["file"] == "notebook-template.json")
        notebook_source = _original(self.documents[notebook_template["sha256"]])
        notebook_text = parts(notebook_source)["notebook-content.ipynb"].decode()
        for token, value in replacements.items():
            notebook_text = notebook_text.replace(token, value)
        notebook_bytes = (json.dumps(_original(notebook_text), indent=2, allow_nan=False) + "\n").encode()
        if parts(notebook) != {"notebook-content.ipynb": notebook_bytes} or (
            {key: value for key, value in notebook_source.items() if key != "definition"}
            != {key: value for key, value in notebook.items() if key != "definition"}
        ):
            return False
        template = next(item for item in packet["modelPackets"] if item["alias"] == evidence.population.model)
        source_body = _original(self.documents[template["sha256"]])
        model_body, model_create = original("model_body"), original("model_create")
        expected = parts(source_body)
        required_parts = {
            "definition.pbism", "definition/database.tmdl", "definition/model.tmdl", "definition/expressions.tmdl",
            *(f"definition/tables/{catalog.tables[name]['displayName']}.tmdl"
              for name in catalog.models[evidence.population.model]["tables"]),
        }
        if (
            not required_parts <= expected.keys()
            or {item["path"]: item["sha256"] for item in packet["sourceModelParts"]
                if item["model"] == evidence.population.model} != {
                    path: hashlib.sha256(raw).hexdigest() for path, raw in expected.items()
                }
            or json.loads(expected["definition.pbism"])["version"] != "4.2"
            or "compatibilityLevel: 1702" not in expected["definition/database.tmdl"].decode()
            or not source_body["definition"]["format"] == model_body["definition"]["format"] == "TMDL"
        ):
            return False
        for token, value in replacements.items():
            expected["definition/expressions.tmdl"] = expected["definition/expressions.tmdl"].replace(
                token.encode(), value.encode(),
            )
        observed = parts(model_body)
        inventory = _original(self.documents[evidence.model_inventory_hash])
        model_item, model_auth = original("model_item"), original("model_authorization")
        item = model_item["response"]
        model_op = next(op for op in model_auth["operations"] if op["name"] == model_create["operation"])
        definition_path = f"/v1/workspaces/{wid}/semanticModels/{dataset}/getDefinition?format=TMDL"
        if not any(
            op["method"] == "POST" and op["path"].replace("{" + evidence.population.model + "}", dataset) == definition_path
            and op["bodySha256"] == digest({}) for op in model_auth["operations"]
        ):
            return False
        if inventory["method"] == "POST":
            definition_matches = (
                inventory["path"] in {definition_path, urlsplit(definition_path).path} and inventory["status"] == 200
                and inventory["bodySha256"] == digest({})
                and (inventory["path"] == definition_path or inventory.get("approvalSha256") == digest(model_auth))
            )
        else:
            definition = original("definition_accepted")
            operation = definition["operationId"]
            if str(UUID(operation)) != operation:
                return False
            operation_path = f"/v1/operations/{operation}"
            definition_matches = (
                definition["state"] == "ACCEPTED" and definition["receiptValidated"] is True
                and definition["method"] == "POST" and definition["path"] == definition_path
                and definition["status"] == 202 and definition["bodySha256"] == digest({})
                and definition["approvalSha256"] == digest(model_auth)
                and location(definition["location"]) == definition["operationPath"] == operation_path
                and definition["resultPath"] == inventory["path"] == operation_path + "/result"
                and inventory["method"] == "GET" and inventory["status"] == 200
            )
        if model_create["status"] == 201:
            if model_create["response"] != item:
                return False
        elif model_create["status"] == 202:
            result = original("model_result")
            operation = model_create["operationId"]
            if str(UUID(operation)) != operation:
                return False
            result_path = f"/v1/operations/{operation}/result"
            if not (
                model_create["resultPath"] == result_path and result["path"] == result_path
                and location(model_create["location"]) == model_create["operationPath"] == result_path.removesuffix("/result")
                and result["method"] == "GET" and result["status"] == 200 and result["response"] == item
                and result["proofLevel"] == level
            ):
                return False
        def nondefinitions(body):
            return {key: value for key, value in body.items() if key != "definition"}
        return (
            observed == expected == parts(inventory["response"])
            and nondefinitions(source_body) == nondefinitions(model_body)
            and model_auth["approved"] is True and model_auth["tenantId"] == self.scope.tenant
            and model_auth["workspaceId"] == wid and model_auth["authorUser"] == self.deployment_actor
            and model_create["approvalSha256"] == digest(model_auth)
            and model_create["state"] == "ACCEPTED" and model_create["receiptValidated"] is True
            and model_create["readbackVerified"] is True and model_create["createdId"] == dataset
            and model_create["createdItem"] == item and model_create["status"] in (201, 202)
            and model_create["method"] == model_op["method"] == "POST"
            and model_create["path"] == model_op["path"] == f"/v1/workspaces/{wid}/semanticModels"
            and model_create["bodySha256"] == model_op["bodySha256"] == digest(model_body)
            and model_item["proofLevel"] == inventory["proofLevel"] == level
            and item["id"] == dataset and item["type"] == "SemanticModel"
            and item["displayName"] == model_body["displayName"]
            and model_item["method"] == "GET" and model_item["status"] == 200
            and model_item["path"] == f"/v1/workspaces/{wid}/items/{dataset}"
            and definition_matches
        )

    def _legacy_p0(self, evidence, packet, manifest, completed, approval, catalog) -> bool:
        """Original P0 producers, not hero-shaped replacements or new authority."""
        import pyarrow.parquet as pq

        references = evidence.deployment_receipts
        required = {
            "workspace_item", "lakehouse_item", "notebook_item", "notebook_template", "notebook_body",
            "notebook_create", "notebook_http_create", "load_accepted", "load_completed",
            "load_http_submitted", "load_http_completed", "model_body", "model_create",
            "model_http_create", "model_item",
        }
        if not required <= references.keys() or set(self.sources) != {"subscriptions", "finance"}:
            return False

        def original(key):
            return _original(self.documents[references[key]])

        def parts(body):
            entries = body["definition"]["parts"]
            result = {entry["path"]: base64.b64decode(entry["payload"], validate=True) for entry in entries
                      if entry["payloadType"] == "InlineBase64"}
            if len(result) != len(entries):
                raise ValueError("Incomplete P0 definition")
            return result

        def path(value):
            parsed = urlsplit(value)
            if parsed.query or parsed.fragment or ((parsed.scheme or parsed.netloc) and (
                parsed.scheme != "https" or parsed.netloc != "api.fabric.microsoft.com"
            )):
                raise ValueError("Noncanonical P0 receipt location")
            return parsed.path

        workspace, lakehouse, notebook_item = (original(key) for key in (
            "workspace_item", "lakehouse_item", "notebook_item",
        ))
        wid, lid, nid = workspace["id"], lakehouse["id"], notebook_item["id"]
        if any(str(UUID(value)) != value for value in (wid, lid, nid)):
            return False
        template, bound = original("notebook_template"), original("notebook_body")
        active_hash = references["notebook_template"]
        if (
            approval["approved"] is not True or approval["tenantId"] != self.scope.tenant
            or approval["authorUser"] != self.deployment_actor or wid != str(evidence.identity.workspace_id)
            or workspace["displayName"] != packet["workspaceName"] or workspace["displayName"] != approval["workspaceName"]
            or lakehouse["displayName"] != packet["lakehouseName"] or lakehouse["displayName"] != approval["lakehouseName"]
            or notebook_item["displayName"] != packet["notebookName"]
            or notebook_item["displayName"] != approval["notebookName"]
            or lakehouse["type"] != "Lakehouse" or notebook_item["type"] != "Notebook"
        ):
            return False
        expected_bound = json.loads(json.dumps(template))
        expected_bound["metadata"]["dependencies"] = {"lakehouse": {
            "default_lakehouse": lid, "default_lakehouse_workspace_id": wid,
            "default_lakehouse_name": lakehouse["displayName"],
        }}
        if bound != expected_bound:
            return False
        notebook_body = {
            "displayName": approval["notebookName"], "type": "Notebook",
            "definition": {"format": "ipynb", "parts": [{
                "path": "notebook-content.ipynb", "payloadType": "InlineBase64",
                "payload": base64.b64encode(json.dumps(bound).encode()).decode(),
            }]},
        }
        notebook_http, notebook_create = original("notebook_http_create"), original("notebook_create")
        if (
            notebook_http["method"] != "POST" or notebook_http["path"] != f"/v1/workspaces/{wid}/items"
            or notebook_http["status"] != notebook_create["status"]
            or notebook_http["status"] not in (201, 202)
        ):
            return False
        if notebook_http["status"] == 201:
            if notebook_http["response"] != notebook_item:
                return False
        else:
            result = original("notebook_result")
            operation = notebook_http["operationId"]
            if not (
                str(UUID(operation)) == operation and notebook_create["operationId"] == operation
                and path(notebook_create["location"]) == path(notebook_http["location"]) == f"/v1/operations/{operation}"
                and result["method"] == "GET" and result["status"] == 200
                and result["path"] == path(notebook_http["location"]) + "/result"
                and result["response"] == notebook_item
            ):
                return False
        if active_hash == approval["notebookSha256"]:
            if (
                packet["notebookSha256"] != active_hash
                or notebook_http["bodySha256"] != digest(notebook_body)
                or notebook_create["requestSha256"] != hashlib.sha256(json.dumps(notebook_body, sort_keys=True).encode()).hexdigest()
            ):
                return False
        else:
            repair, applied = original("notebook_repair_approval"), original("notebook_repair_applied")
            if not (
                repair["approved"] is True and repair["originalNotebookSha256"] == approval["notebookSha256"]
                and repair["revisedNotebookSha256"] == active_hash and repair["notebookId"] == nid
                and repair["maximumTotalRuns"] == 2 and applied["approvedTemplateSha256"] == active_hash
                and packet["notebookSha256"] in {active_hash, approval["notebookSha256"]}
            ):
                return False
            service, update = original("notebook_definition"), original("notebook_update")
            service_parts = json.loads(json.dumps(service["response"]["definition"]["parts"]))
            ipynb = next(part for part in service_parts if part["path"].endswith(".ipynb"))
            ipynb["payload"] = notebook_body["definition"]["parts"][0]["payload"]
            if not (
                service["method"] == "POST" and service["status"] == 200
                and service["path"] == f"/v1/workspaces/{wid}/notebooks/{nid}/getDefinition"
                and service["bodySha256"] == digest({})
                and update["method"] == "POST"
                and update["path"] == f"/v1/workspaces/{wid}/notebooks/{nid}/updateDefinition"
                and update["bodySha256"] == digest({"definition": {"format": "ipynb", "parts": service_parts}})
                and update["status"] in (200, 202)
            ):
                return False
            if update["status"] == 202:
                terminal = original("notebook_update_completed")
                if not (
                    terminal["method"] == "GET" and terminal["status"] == 200
                    and terminal["path"] == path(update["location"]) == f"/v1/operations/{update['operationId']}"
                    and terminal["response"]["status"] == "Succeeded"
                ):
                    return False
        accepted, submitted, observed = (original(key) for key in (
            "load_accepted", "load_http_submitted", "load_http_completed",
        ))
        job_path = path(accepted["location"])
        prefix = f"/v1/workspaces/{wid}/items/{nid}/jobs/instances/"
        job = job_path.removeprefix(prefix)
        if not job_path.startswith(prefix) or str(UUID(job)) != job:
            return False
        if not (
            evidence.load_assertion_hash == references["load_completed"] and completed == original("load_completed")
            and accepted["itemId"] == nid and completed["id"] == job and completed["itemId"] == nid
            and completed["jobType"] == "RunNotebook" and completed["status"] == "Completed"
            and not completed.get("failureReason") and observed["response"] == completed
            and submitted["method"] == "POST" and submitted["status"] == 202
            and submitted["path"] == f"/v1/workspaces/{wid}/items/{nid}/jobs/RunNotebook/instances"
            and submitted["bodySha256"] == digest(None) and path(submitted["location"]) == job_path
            and observed["method"] == "GET" and observed["status"] == 200 and observed["path"] == job_path
            and datetime.fromisoformat(notebook_http["startedAt"]) <= datetime.fromisoformat(submitted["startedAt"])
            and self.captured_at <= datetime.fromisoformat(submitted["startedAt"])
            <= datetime.fromisoformat(observed["startedAt"]) <= self.reviewed_at
            and datetime.fromisoformat(observed["startedAt"]) <= datetime.fromisoformat(approval["deadlineUtc"])
        ):
            return False
        if active_hash != approval["notebookSha256"] and (
            datetime.fromisoformat(original("notebook_update")["startedAt"])
            > datetime.fromisoformat(submitted["startedAt"])
        ):
            return False
        # Parse the producer's JSON constant; never execute notebook code.
        cell = next(cell for cell in template["cells"] if cell["id"] == "synthetic-assets")
        tree = ast.parse("".join(cell["source"]))
        assignment = tree.body[-1]
        if not (
            len(tree.body) == 2 and isinstance(tree.body[0], ast.Import)
            and [(alias.name, alias.asname) for alias in tree.body[0].names] == [("json", None)]
            and isinstance(assignment, ast.Assign) and len(assignment.targets) == 1
            and isinstance(assignment.targets[0], ast.Name) and assignment.targets[0].id == "ASSETS"
            and isinstance(assignment.value, ast.Call) and not assignment.value.keywords
            and isinstance(assignment.value.func, ast.Attribute) and assignment.value.func.attr == "loads"
            and isinstance(assignment.value.func.value, ast.Name) and assignment.value.func.value.id == "json"
            and len(assignment.value.args) == 1 and isinstance(assignment.value.args[0], ast.Constant)
            and type(assignment.value.args[0].value) is str
        ):
            return False
        assets = json.loads(assignment.value.args[0].value)
        tables = {table["id"]: table for table in manifest["tables"]}
        selected = {name for alias in self.sources for name in catalog.models[alias]["tables"]}
        declared = {table["name"]: table for table in packet["tables"]}
        actual = {asset["table"]: asset for asset in assets}
        if not (
            packet["models"] == ["finance", "subscriptions"] and packet["profile"] == manifest["profile"] == "miniature"
            and len(actual) == len(assets) == len(declared) == len(packet["tables"]) == len(selected)
            and actual.keys() == declared.keys() == selected
            and packet["sourceContentSha256"] == manifest["content_sha256"] == digest({
                name: table["content_sha256"] for name, table in tables.items()
            })
        ):
            return False
        for name, asset in actual.items():
            raw = base64.b64decode(asset["payload"], validate=True)
            table = tables[name]
            parquet = pq.ParquetFile(io.BytesIO(raw))
            if not (
                0 <= parquet.metadata.num_rows <= 100_000 and type(asset["rows"]) is int
                and parquet.metadata.num_rows == asset["rows"] == table["rows"] == declared[name]["rows"]
                and hashlib.sha256(raw).hexdigest() == asset["file_sha256"] == table["file_sha256"]
                == declared[name]["sourceFileSha256"] and asset["columns"] == catalog.tables[name]["columns"]
            ):
                return False
            logical = hashlib.sha256(json.dumps(asset["columns"], sort_keys=True, separators=(",", ":")).encode())
            for row in parquet.read().to_pylist():
                values = {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}
                logical.update(b"\n" + json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
            if logical.hexdigest() != table["content_sha256"]:
                return False
        shared = {name for name in selected if
                  (catalog.tables[name]["model"] == "core" and name != "quarantine")
                  or name.endswith("_map") or name in {"subscription", "contract"}}
        population = evidence.population
        if not (
            population.profile == "miniature" and population.seed == manifest["seed"]
            and population.generator_revision == "LEGACY_P0_NOTEBOOK_SHA256:" + active_hash
            and population.manifest_hash == digest(manifest)
            and population.identity_revision == digest({
                name: {"columns": actual[name]["columns"], "primaryKey": catalog.tables[name]["primaryKey"]}
                for name in sorted(shared)
            })
            and population.shared_map_hashes == {name: tables[name]["content_sha256"] for name in sorted(shared)}
            and population.table_hashes == {
                name: tables[name]["content_sha256"] for name in catalog.models[evidence.population.model]["tables"]
            }
        ):
            return False
        spec = next(item for item in packet["modelPackets"] if item["model"] == population.model)
        if spec["templateSha256"] != approval["modelTemplateHashes"][population.model]:
            return False
        source = _original(self.documents[spec["templateSha256"]])
        body, created, http, item = (original(key) for key in ("model_body", "model_create", "model_http_create", "model_item"))
        expected = parts(source)
        for name, raw in expected.items():
            expected[name] = raw.replace(b"<WORKSPACE_ID>", wid.encode()).replace(b"<LAKEHOUSE_ID>", lid.encode())
        if not (
            parts(body) == expected and source["definition"]["format"] == body["definition"]["format"] == "TMDL"
            and body["displayName"] == source["displayName"] == spec["displayName"] == item["displayName"]
            and item["id"] == str(evidence.identity.dataset_id) and item["type"] == "SemanticModel"
            and _original(self.documents[evidence.model_inventory_hash]) == item
            and created["requestSha256"] == hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            and created["status"] == http["status"] and http["status"] in (201, 202)
            and http["method"] == "POST" and http["path"] == f"/v1/workspaces/{wid}/semanticModels"
            and http["bodySha256"] == digest(body)
            and datetime.fromisoformat(http["startedAt"]) >= datetime.fromisoformat(observed["startedAt"])
        ):
            return False
        if http["status"] == 201:
            return http["response"] == item
        result = original("model_result")
        operation_path = path(http["location"])
        operation = http["operationId"]
        return (
            str(UUID(operation)) == operation and created["operationId"] == operation
            and path(created["location"]) == operation_path == f"/v1/operations/{operation}"
            and result["method"] == "GET" and result["status"] == 200
            and result["path"] == operation_path + "/result" and result["response"] == item
        )

    def _metadata_state(self, evidence, alias, binding, profiles, catalog, sink, *, synthetic):
        from telco_iq.source_observation import SourceMetadataObserver, eligible_state, facts_from_captures

        metadata_profiles = getattr(profiles, "metadata_states", {})
        metadata_pins = getattr(profiles, "metadata_state_pins", {})
        profile = metadata_profiles.get(alias)
        if profile is None or alias in profiles.states or not evidence.metadata_capture_ids:
            raise ValueError("Reviewed metadata profile/capture set is missing or ambiguous")
        profile.verify(binding, alias, metadata_pins.get(alias), synthetic=synthetic)
        checker = SourceMetadataObserver(
            binding, metadata_profiles, metadata_pins, None, sink, None,
            lambda scope: scope == binding.scope, synthetic=synthetic,
        )
        checker.validate_calibration(alias)
        facts = facts_from_captures(binding, alias, profile, sink, evidence.metadata_capture_ids, synthetic=synthetic)
        expected_types = {
            "int64": "long", "string": "string", "bool": "boolean",
            "date": "date", "timestamp": "timestamp", "float64": "double",
        }
        if set(facts.tables) != set(catalog.models[alias]["tables"]) or any(
            table["schema_fields"] != {
                name: expected_types[kind] for name, kind in catalog.tables[table_id]["columns"].items()
            } for table_id, table in facts.tables.items()
        ):
            raise ValueError("Actual Delta physical schema/tables differ from reviewed source population")
        state = eligible_state(
            facts, profile, self.documents, **evidence.state_anchor_refs(), synthetic=synthetic,
            anchor_reviewed_at=self.reviewed_at if isinstance(evidence, CurrentP0ReadCapture) else None,
        )
        if (
            evidence.source_capture_id not in evidence.metadata_capture_ids
            or digest(sink.read(evidence.source_capture_id)["response"]) != evidence.source_response_hash
            or (state.data_revision, state.model_revision) != (evidence.data_revision, evidence.model_revision)
            or not self.captured_at <= state.observed_at <= self.reviewed_at
        ):
            raise ValueError("Metadata original evidence/revisions/scope window conflict with population proof")
        return profile

    def validate_for(
        self, *, pinned_digest: str, scope: Scope, catalog: Catalog, snapshot,
        enrollment, profiles, context_contract_hash: str, sink, synthetic: bool = False,
    ) -> list[SourcePopulation]:
        def fail(message):
            raise LabError("Reviewed source population proof: " + message, Status.BLOCKED_CAPABILITY)

        now = datetime.now(timezone.utc)
        if self.proof_digest != pinned_digest:
            fail("independent review digest changed")
        if self.proof_kind != ("SYNTHETIC_TEST" if synthetic else "ACTUAL_APPROVED_SOURCE_CAPTURE"):
            fail("fixture/test origin cannot authorize actual source execution")
        if scope != self.scope or scope.mode != "LIVE_DELEGATED":
            fail("task, tenant, reader or authorization epoch differs")
        if not self.reviewed_at <= now < self.expires_at:
            fail("review is future or expired")
        expected_sources = {
            source.alias: SourceIdentity(workspace_id=source.workspace_id, dataset_id=source.dataset_id)
            for source in snapshot.sources
        }
        if (
            self.catalog_hash != catalog.revision
            or self.native_snapshot_hash != snapshot.content_hash
            or self.enrollment_digest != enrollment.enrollment_digest
            or self.source_profiles_hash != digest(profiles.model_dump(mode="json"))
            or self.context_contract_hash != context_contract_hash
            or expected_sources != enrollment.sources
            or set(self.sources) != set(expected_sources) or set(scope.models) != set(expected_sources)
        ):
            fail("catalog, native binding, schema/profile, enrollment or context approval differs")
        legacy = {entry.receipt_kind for entry in self.sources.values()} == {"LEGACY_P0"}
        current_read = all(isinstance(entry, CurrentP0ReadCapture) for entry in self.sources.values())
        showcase_read = all(isinstance(entry, CurrentShowcaseReadCapture) for entry in self.sources.values())
        if any(entry.receipt_kind == "LEGACY_P0" for entry in self.sources.values()) and (
            not legacy or set(self.sources) != {"subscriptions", "finance"}
        ):
            fail("legacy P0 proof is limited to the original two-model population")
        if any(isinstance(entry, CurrentP0ReadCapture) for entry in self.sources.values()) and (
            not current_read or (not showcase_read and set(self.sources) != {"subscriptions", "finance"})
            or self.deployment_actor is not None or self.load_assertion_status is not None
        ):
            fail("current P0 read proof is limited to the complete original two-model population")
        if any(isinstance(entry, CurrentShowcaseReadCapture) for entry in self.sources.values()) and (
            not showcase_read or set(self.sources) != set(catalog.models) or len(self.sources) != 8
        ):
            fail("current showcase read proof requires the complete exact eight-source population")
        if any(hashlib.sha256(document.encode("utf-8")).hexdigest() != key
               for key, document in self.documents.items()):
            fail("original packet/load/inventory document was tampered with")
        records = []
        binding = ReaderBinding(
            scope=scope, ontology_workspace_id=enrollment.ontology_workspace_id,
            ontology_item_id=enrollment.ontology_item_id,
            native_snapshot_hash=snapshot.content_hash, sources=expected_sources,
        )
        shared = {
            name for name, table in catalog.tables.items()
            if (table["model"] == "core" and name != "quarantine")
            or name.endswith("_map") or name in {"subscription", "contract"}
        }
        if legacy or current_read:
            shared &= {name for alias in self.sources for name in catalog.models[alias]["tables"]}
        if sink is None:
            fail("original private source capture store is absent")
        sink.ready()
        for alias, evidence in self.sources.items():
            population = evidence.population
            identity = expected_sources[alias]
            if (
                evidence.identity != identity or population.model != alias
                or population.evidence_kind != "REVIEWED_LIVE"
                or population.binding_hash != digest(identity.model_dump(mode="json"))
                or evidence.source_schema_hash != enrollment.source_schema_hashes.get(alias)
                or not set(catalog.models[alias]["tables"]) <= population.table_hashes.keys()
                or set(population.shared_map_hashes) != shared
            ):
                fail("source identity, source schema or complete structural population differs")
            if isinstance(evidence, CurrentP0ReadCapture):
                try:
                    profile = self._metadata_state(
                        evidence, alias, binding, profiles, catalog, sink, synthetic=synthetic,
                    )
                    commitments = (verify_current_showcase_audit if showcase_read else verify_current_p0_audit)(
                        evidence, self.documents, self.binary_objects or {}, catalog, binding, profile,
                    )
                    manifest = _original(self.documents[evidence.source_manifest_file_hash])
                    report = _original(self.documents[evidence.attestation_hash])
                    valid = (
                        population.manifest_hash == digest(manifest)
                        and population.profile == manifest["profile"] == ("showcase-v2" if showcase_read else "miniature")
                        and population.seed == manifest["seed"]
                        and population.generator_revision == (
                            "CURRENT_SHOWCASE_MANIFEST_FORMAT:" if showcase_read else "CURRENT_P0_MANIFEST_FORMAT:"
                        ) + manifest["version"]
                        and population.identity_revision == digest({
                            name: {key: catalog.tables[name][key] for key in ("columns", "primaryKey")}
                            for name in sorted(shared)
                        })
                        and population.shared_map_hashes == {name: commitments[name] for name in sorted(shared)}
                        and population.table_hashes == {
                            name: commitments[name] for name in catalog.models[alias]["tables"]
                        }
                        and self.captured_at <= datetime.fromisoformat(report["source_capture_started_at"])
                        <= datetime.fromisoformat(report["observed_at"]) <= self.reviewed_at
                        and getattr(profile.frame_anchor, "read_plan_hash", None) == evidence.read_plan_hash
                    )
                except (KeyError, TypeError, ValueError):
                    valid = False
                if not valid:
                    fail("current-read audit/bytes/active files/framing proof is incomplete or changed")
                records.append(population)
                continue
            for key in (
                evidence.deployment_packet_hash, evidence.load_assertion_hash, evidence.model_inventory_hash,
                evidence.source_manifest_file_hash,
                evidence.deployment_approval_hash,
                *evidence.deployment_receipts.values(),
            ):
                if key not in self.documents:
                    fail("complete original deployment/load/model-inventory evidence is missing")
            try:
                manifest = _original(self.documents[evidence.source_manifest_file_hash])
                packet = _original(self.documents[evidence.deployment_packet_hash])
                assertion = _original(self.documents[evidence.load_assertion_hash])
                approval = _original(self.documents[evidence.deployment_approval_hash])
                if legacy:
                    valid = self._legacy_p0(evidence, packet, manifest, assertion, approval, catalog)
                else:
                    declared = manifest["population"]
                    tables = {table["id"]: table for table in manifest["tables"]}
                    loaded = {table["name"]: table for table in assertion["tables"]}
                    packet_tables = {table["name"]: table for table in packet["tables"]}
                    valid = (
                        packet["sourceManifestSha256"] == evidence.source_manifest_file_hash
                        and packet["population"] == declared
                        and population.manifest_hash == digest(manifest)
                        and population.profile == declared["profile"] == manifest["profile"]
                        and population.seed == declared["seed"] == manifest["seed"]
                        and population.generator_revision == manifest["generator_sha256"] == packet["generatorSha256"]
                        and population.identity_revision == declared["identityRevision"]
                        and population.shared_map_hashes == declared["sharedMapHashes"]
                        and population.table_hashes == {
                            name: tables[name]["content_sha256"] for name in catalog.models[alias]["tables"]
                        }
                        and assertion["schemaVersion"] == "hero-source-assertions-v1"
                        and assertion["proofLevel"] == ("SYNTHETIC_LOCAL" if synthetic else "AUTHOR_SERVICE")
                        and assertion["packetSha256"] == evidence.deployment_packet_hash
                        and assertion["populationSha256"] == digest(declared)
                        and approval["schemaVersion"] == "fabric-renewal-v1" and approval["approved"] is True
                        and approval["tenantId"] == scope.tenant and approval["authorUser"] == self.deployment_actor
                        and approval["workspaceId"] == str(identity.workspace_id)
                        and any(artifact["sha256"] == evidence.deployment_packet_hash for artifact in approval["artifacts"])
                        and len(loaded) == len(assertion["tables"]) == len(packet_tables)
                        and loaded.keys() == packet_tables.keys()
                    )
                    for name, table in packet_tables.items():
                        observed = loaded[name]
                        valid = valid and (
                            type(observed["rows"]) is int and observed["rows"] == table["rows"] == tables[name]["rows"]
                            and observed["expectedFileSha256"] == table["fileSha256"] == tables[name]["file_sha256"]
                            and observed["exactDataReadback"] is True and observed["keyChecks"] == "PASSED"
                            and type(observed["missingExpectedRows"]) is int and observed["missingExpectedRows"] == 0
                            and type(observed["unexpectedRows"]) is int and observed["unexpectedRows"] == 0
                        )
                    valid = valid and self._deployment_links(evidence, packet, assertion, approval, catalog, synthetic=synthetic)
                    observed = datetime.fromisoformat(assertion["observedAtUtc"])
                    valid = valid and self.captured_at <= observed <= self.reviewed_at and (
                        observed <= datetime.fromisoformat(approval["window"]["notAfterUtc"])
                    )
            except (KeyError, TypeError, ValueError, StopIteration, SyntaxError):
                valid = False
            if not valid:
                fail("packet/load assertion/manifest is partial, mixed, or not an actual reviewed deployment")
            if evidence.metadata_capture_ids is not None:
                try:
                    self._metadata_state(evidence, alias, binding, profiles, catalog, sink, synthetic=synthetic)
                except (KeyError, TypeError, ValueError):
                    fail("reviewed metadata original evidence/schema/revisions differ")
                records.append(population)
                continue
            raw = sink.read(evidence.source_capture_id)
            profile = profiles.states.get(alias)
            if (
                profile is None or profile.identity != identity or profile.review.provenance not in (
                    {"reader_capture", "synthetic_test"} if synthetic else {"reader_capture"}
                )
                or raw.get("lane") != ("SYNTHETIC_TEST" if synthetic else "LIVE_DELEGATED")
                or raw.get("response_kind") != "MCP_TOOL_RESULT"
                or raw.get("source_alias") != alias or raw.get("tool") != "ExecuteQuery"
                or raw.get("request") != {
                    "artifactId": str(identity.dataset_id), "daxQueries": [profile.dax], "maxRows": 1,
                }
                or raw.get("context", {}).get("source") != identity.model_dump(mode="json")
                or raw.get("context", {}).get("schema_hash") != profile.schema_hash
                or raw.get("context", {}).get("binding_hash") != binding.binding_hash
                or digest(raw.get("response")) != evidence.source_response_hash
            ):
                fail("retained original source capture or reviewed identity/profile differs")
            captured = datetime.fromisoformat(raw["captured_at"])
            if not self.captured_at <= captured <= self.reviewed_at:
                fail("source capture is outside the reviewed observation window")
            response = raw["response"]
            data = response.get("structuredContent")
            try:
                valid = Draft202012Validator(profile.output_schema, registry=Registry()).is_valid(data)
            except Exception:
                valid = False
            if (
                not valid or response.get("isError") is True
                or pointer(data, profile.errors_pointer) != []
                or pointer(data, profile.complete_pointer) is not True
                or pointer(data, profile.data_revision_pointer) != evidence.data_revision
                or pointer(data, profile.model_revision_pointer) != evidence.model_revision
            ):
                fail("source capture is partial, errored, or conflicts with the reviewed revisions")
            records.append(population)
        return compatible_populations(records, records)
