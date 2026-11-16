"""Opt-in read-only source metadata facts, not engine-reported consumed versions.

Capture bootstrap is deliberately independent of population/model-provider
activation. Only a separately reviewed reader calibration and original
definition-at-frame/full-model-refresh/load evidence may convert V0 facts into
an ObservedSourceState. Author shape captures never supply reader authority.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import Field, model_validator

from telco_iq.approval import Approval, private_config_path
from telco_iq.catalog import digest
from telco_iq.contracts import Budgets, Contract, Scope, Sha256, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import (
    ObservedSourceState,
    ReaderBinding,
    SourceCallBudget,
    SourceIdentity,
    accepted_refresh_location,
)
from telco_iq.mcp_client import mcp_capture_failure_status
from telco_iq.private_evidence import PrivateEvidenceConfig, PrivateEvidenceSink, private_identifiers
from telco_iq.retry import retry_after

FABRIC = "https://api.fabric.microsoft.com"
POWERBI = "https://api.powerbi.com"
STORAGE = "https://onelake.dfs.fabric.microsoft.com"
V0 = "00000000000000000000.json"
READ_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
READ_TRANSPORT_FAILURES = frozenset({
    "ConnectError", "ReadError", "WriteError", "CloseError", "RemoteProtocolError",
    "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
})


def _fail(message, status=Status.BLOCKED_CAPABILITY):
    raise LabError(message, status) from None


def code_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        _fail("Source metadata requires original timezone-aware observation times")
    return result


def _refresh_detail_time(value: str, history_value: str) -> datetime:
    """Offset-free execution details must exactly match the same job's explicit UTC history."""
    bare = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?"
    if isinstance(value, str) and re.fullmatch(bare, value):
        history = _time(history_value)
        match = re.fullmatch("(" + bare + r")(?:Z|[+-]00:00)", history_value)
        if match is None or history.utcoffset() != timedelta(0):
            _fail("Offset-free enhanced refresh details require explicit matching UTC history")
        wall, expected = value.partition("."), match[1].partition(".")
        if wall[0] != expected[0] or wall[2].ljust(7, "0") != expected[2].ljust(7, "0"):
            _fail("Offset-free enhanced refresh details differ from the exact UTC history")
        return history
    return _time(value)


def _has_failure(value: dict) -> bool:
    return (
        any(value.get(key) is not None for key in ("error", "errorCode"))
        or value.get("errors") not in (None, [])
        or any(value.get(key) is not None and value.get(key) is not False for key in ("partial", "isPartial"))
    )


def _parts(value: dict) -> dict[str, bytes]:
    if not isinstance(value, dict) or _has_failure(value):
        _fail("Source definition reports embedded errors or partial output")
    if value.get("definition", {}).get("format") != "TMDL":
        _fail("Source definition is not the explicitly reviewed complete TMDL format")
    entries = value["definition"]["parts"]
    result = {entry["path"]: base64.b64decode(entry["payload"], validate=True)
              for entry in entries if entry["payloadType"] == "InlineBase64"}
    if len(result) != len(entries) or not result:
        _fail("Complete unique TMDL parts are required")
    return result


def definition_hash(value: dict) -> str:
    return digest({name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(_parts(value).items())})


class TableTarget(Contract):
    catalog_table: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    part: str = Field(pattern=r"^definition/tables/[^/\\]+\.tmdl$")
    schema_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    entity_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    columns: dict[str, Literal["long", "string", "boolean", "date", "timestamp", "double"]] = Field(min_length=1)
    incarnation_id: UUID | None = None
    service_birth: str | None = None
    log_birth: str | None = None

    @model_validator(mode="after")
    def source_schema(self):
        if any(re.match(r"^(?:eval|holdout|oracle|latent)(?:_|$)", name, re.I)
               for name in (self.catalog_table, self.entity_name, *self.columns)):
            raise ValueError("Evaluation/oracle/latent fields are outside the source metadata whitelist")
        return self

    @property
    def table_path(self):
        return self.schema_name + "/" + self.entity_name


class FrameAnchor(Contract):
    """References to existing original documents, not new service booleans."""

    model_body_hash: Sha256
    load_job_hash: Sha256
    definition_at_frame_hash: Sha256
    full_refresh_body_hash: Sha256
    accepted_refresh_hash: Sha256


class CurrentReadFrameAnchor(Contract):
    """Explicit full-model framing after a separately verified current-row audit."""

    kind: Literal["current_p0_read_full_frame_v0", "current_showcase_read_full_frame_v1"]
    model_body_hash: Sha256
    attestation_hash: Sha256
    read_plan_hash: Sha256
    definition_at_frame_hash: Sha256
    full_refresh_body_hash: Sha256 | None = Field(default=None, exclude_if=lambda value: value is None)
    full_refresh_intent_hash: Sha256 | None = Field(default=None, exclude_if=lambda value: value is None)
    accepted_refresh_hash: Sha256
    definition_state_hashes: list[Sha256] = Field(default_factory=list, exclude_if=lambda value: not value)
    definition_result_hash: Sha256 | None = Field(default=None, exclude_if=lambda value: value is None)
    refresh_details_hash: Sha256 | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def original_request(self):
        if (self.full_refresh_body_hash is None) == (self.full_refresh_intent_hash is None):
            raise ValueError("Exactly one original refresh body or original request intent is required")
        return self


class MetadataStateProfile(Contract):
    method: Literal["source_metadata_v0"] = "source_metadata_v0"
    identity: SourceIdentity
    lakehouse_id: UUID
    tables: list[TableTarget] = Field(min_length=1, max_length=64)
    part_paths: list[str] = Field(min_length=1, max_length=256)
    frame_request_id: UUID
    frame_refresh_type: Literal["DirectLakeFraming", "ViaEnhancedApi"] = "DirectLakeFraming"
    regional_location_origin: str | None = None
    storage_api_version: Literal["2023-11-03"] = "2023-11-03"
    max_pages: int = Field(default=4, ge=1, le=10)
    max_lro_polls: int = Field(default=4, ge=1, le=10)
    max_read_attempts: int = Field(default=1, ge=1, le=3, exclude_if=lambda value: value == 1)
    code_hash: Sha256
    review_reference: str = Field(min_length=1)
    provenance: Literal["author_shape_capture", "reader_capture", "synthetic_test"]
    reviewed_at: datetime
    expires_at: datetime
    max_capture_age_seconds: int = Field(default=300, ge=1, le=300)
    calibration_capture_ids: list[Sha256] = Field(default_factory=list)
    frame_anchor: FrameAnchor | CurrentReadFrameAnchor | None = None
    declared_business_watermark: str | None = None

    @model_validator(mode="after")
    def reviewed(self):
        if (
            self.reviewed_at.tzinfo is None or self.expires_at.tzinfo is None
            or self.reviewed_at >= self.expires_at or not self.review_reference.strip()
            or len({table.table_path for table in self.tables}) != len(self.tables)
            or len({table.catalog_table for table in self.tables}) != len(self.tables)
            or len(set(self.part_paths)) != len(self.part_paths)
        ):
            raise ValueError("Exact unique reviewed source metadata bindings and expiry required")
        if self.regional_location_origin is not None and not re.fullmatch(
            r"https://wabi-[a-z0-9-]+\.analysis\.windows\.net", self.regional_location_origin,
        ):
            raise ValueError("Regional location evidence requires an exact reviewed HTTPS origin")
        return self

    @property
    def profile_hash(self):
        return digest(self.model_dump(mode="json"))

    @property
    def contract_hash(self):
        values = {
            "method": self.method, "identity": self.identity.model_dump(mode="json"),
            "lakehouse": str(self.lakehouse_id), "code": self.code_hash, "parts": self.part_paths,
            "tables": [(table.catalog_table, table.part, table.table_path, table.columns) for table in self.tables],
            "frame": str(self.frame_request_id), "regional_origin": self.regional_location_origin,
            "frame_refresh_type": self.frame_refresh_type,
            "storage_api_version": self.storage_api_version,
        }
        if self.max_read_attempts != 1:
            values["max_read_attempts"] = self.max_read_attempts
        return digest(values)

    def verify(self, binding: ReaderBinding, alias: str, pin: str, *, synthetic: bool):
        now = datetime.now(timezone.utc)
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]*", alias)
            or self.profile_hash != pin or self.code_hash != code_hash()
            or self.identity != binding.sources.get(alias)
            or not self.reviewed_at <= now < self.expires_at
            or (self.provenance == "synthetic_test" and not synthetic)
        ):
            _fail("Source metadata profile/code/target/review pin is absent, stale or changed")


@dataclass(frozen=True)
class MetadataRequest:
    role: str
    resource: Literal["source_metadata", "storage", "iq"]
    method: Literal["GET", "POST"]
    url: str
    body: bytes = b""
    header_fields: tuple[tuple[str, str], ...] = ()

    def evidence(self):
        return {
            "role": self.role, "resource": self.resource, "method": self.method, "url": self.url,
            "body_base64": base64.b64encode(self.body).decode(), "header_fields": dict(self.header_fields),
        }


@dataclass(frozen=True)
class MetadataResponse:
    status: int
    header_fields: dict[str, str]
    body: bytes
    observed_at: datetime
    actor_scope: Scope
    resource: str
    original_header_pairs: tuple[tuple[str, str], ...] | None = None


class MetadataRequester(Protocol):
    async def __call__(self, request: MetadataRequest) -> MetadataResponse: ...


class MetadataFacts(Contract):
    binding_hash: Sha256
    source: str
    contract_hash: Sha256
    origin: Literal["READER_DELEGATED", "SYNTHETIC_TEST", "AUTHOR_SHAPE"]
    observed_at: datetime
    complete_definition_hash: Sha256
    binding_map: dict[str, str]
    frame: dict
    tables: dict[str, dict]
    data_fingerprint: Sha256
    model_fingerprint: Sha256
    capture_ids: list[Sha256]
    definition: dict
    blockers: list[str]
    declared_business_watermark: str | None = None
    observation_id: UUID | None = None
    window_started_at: datetime | None = None
    window_ended_at: datetime | None = None
    role_order: list[str] = Field(default_factory=list)


class _CaptureGroup(list):
    def __init__(self):
        super().__init__()
        self.observation_id = str(uuid4())


def _observation_window(records: list[dict], profile: MetadataStateProfile, *, attempts: list[dict] | None = None):
    """One ordered original I/O group, fresh from its oldest required response."""
    if not records:
        _fail("Required metadata observation group is empty")
    original_responses = records if attempts is None else attempts
    groups = [record["context"].get("observation_id") for record in original_responses]
    if any(type(group) is not str for group in groups) or len(set(groups)) != 1:
        _fail("Metadata responses belong to missing or mixed observation groups")
    group = groups[0]
    try:
        valid_group = str(UUID(group)) == group
    except ValueError:
        valid_group = False
    if not valid_group:
        _fail("Metadata observation group identity is invalid")
    times = [_time(record["response"]["observed_at"]) for record in original_responses]
    successful_times = [_time(record["response"]["observed_at"]) for record in records]
    retained = [_time(record["captured_at"]) for record in original_responses]
    if times != sorted(times) or successful_times != sorted(successful_times) or any(
        observed > captured for observed, captured in zip(times, retained)
    ):
        _fail("Metadata original response/capture times are not chronologically coherent")
    if times[-1] - times[0] > timedelta(seconds=profile.max_capture_age_seconds):
        _fail("Metadata responses exceed one bounded observation window")
    roles = [record["request"]["role"] for record in records]
    index = 0
    def exact(role):
        nonlocal index
        if index >= len(roles) or roles[index] != role:
            _fail("Required metadata roles are missing, swapped or out of order")
        index += 1
    def definition(phase):
        nonlocal index
        start = index
        exact(phase)
        status = records[start]["response"]["status"]
        if status == 200:
            return
        if status != 202:
            _fail("Metadata definition has no successful original response")
        count = 0
        while index < len(roles) and roles[index].startswith(phase + "-state-"):
            exact(phase + "-state-" + str(count))
            count += 1
        if not 0 < count <= profile.max_lro_polls:
            _fail("Metadata definition operation state evidence is incomplete")
        exact(phase + "-result")
    def pages(phase):
        count = 0
        while index < len(roles) and roles[index].startswith(phase + "-"):
            exact(phase + "-" + str(count))
            count += 1
        if not 0 < count <= profile.max_pages:
            _fail("Metadata original listing pages are incomplete")
    definition("definition-before")
    exact("frame-before")
    pages("paths-before")
    logs = []
    while index < len(roles) and roles[index].startswith("log0:"):
        logs.append(roles[index])
        index += 1
    expected_logs = {"log0:" + target.table_path for target in profile.tables}
    if len(logs) != len(expected_logs) or set(logs) != expected_logs:
        _fail("Required metadata logs are missing, repeated or outside the bound table set")
    pages("paths-after")
    exact("frame-after")
    definition("definition-after")
    if index != len(roles):
        _fail("Unexpected trailing metadata roles")
    return group, times[0], times[-1], roles


def _metadata_record(record, binding, alias, profile, *, synthetic):
    request, response, context = record["request"], record["response"], record["context"]
    if not (
        record.get("response_kind") in {"HTTP_METADATA_RESULT", "HTTP_METADATA_TRANSPORT_FAILURE"}
        and record.get("tool") == "SourceMetadata"
        and record.get("source_alias") == alias
        and record.get("lane") == ("SYNTHETIC_TEST" if synthetic else "LIVE_DELEGATED")
        and context.get("binding_hash") == binding.binding_hash and context.get("contract_hash") == profile.contract_hash
        and context.get("code_hash") == code_hash() and response.get("actor_scope") == binding.scope.model_dump(mode="json")
        and response.get("resource") == request["resource"]
        and context.get("kind") == "SOURCE_METADATA_OBSERVATION"
        and context.get("source") == profile.identity.model_dump(mode="json")
        and context.get("origin") == ("SYNTHETIC_TEST" if synthetic else "READER_DELEGATED")
    ):
        _fail("Retained metadata is not an original current-reader/profile capture", Status.BLOCKED_AUTH)
    if record.get("response_kind") == "HTTP_METADATA_TRANSPORT_FAILURE":
        if request["method"] != "GET" or response.get("response_obtained") is not False or (
            response.get("transport_error_type") not in READ_TRANSPORT_FAILURES
            or set(response) != {"response_obtained", "transport_error_type", "observed_at", "actor_scope", "resource"}
        ):
            _fail("Retained read transport failure is not an approved response-absent GET")
        return request, response, context, None
    raw = base64.b64decode(response["body_base64"], validate=True)
    if hashlib.sha256(raw).hexdigest() != response["body_sha256"]:
        _fail("Retained HTTP metadata byte hash differs")
    return request, response, context, raw


def _retained_metadata_window(binding, alias, profile, sink, capture_ids, *, synthetic):
    if len(set(capture_ids)) != len(capture_ids):
        _fail("Repeated source metadata capture reference")
    records = [sink.read(key) for key in capture_ids]
    attempts, seen_failures = [], set()
    for record in records:
        request, response, context, _ = _metadata_record(record, binding, alias, profile, synthetic=synthetic)
        if record["response_kind"] != "HTTP_METADATA_RESULT" or response["status"] not in (200, 202):
            _fail("A failed or response-absent read cannot be a successful metadata observation")
        failed = context.get("failed_read_attempt_ids", [])
        enabled = request["method"] == "GET" and profile.max_read_attempts > 1
        if not enabled:
            if "failed_read_attempt_ids" in context or "read_attempt" in context or "read_attempt_started_at" in context:
                _fail("Unapproved metadata read-retry evidence")
            attempts.append(record)
            continue
        attempt = context.get("read_attempt")
        if type(attempt) is not int or not 0 <= attempt < profile.max_read_attempts or (
            not isinstance(failed, list) or len(failed) != attempt
            or any(type(key) is not str or not re.fullmatch(r"[0-9a-f]{64}", key) for key in failed)
            or len(set(failed)) != len(failed) or set(failed) & (set(capture_ids) | seen_failures)
        ):
            _fail("Metadata read-retry chain is missing, repeated or outside its reviewed bound")
        chain = [sink.read(key) for key in failed] + [record]
        previous = None
        for index, original in enumerate(chain):
            tried_request, tried_response, tried_context, _ = _metadata_record(
                original, binding, alias, profile, synthetic=synthetic,
            )
            started = _time(tried_context["read_attempt_started_at"])
            if tried_request != request or tried_context.get("read_attempt") != index or (
                tried_context.get("failed_read_attempt_ids", []) != failed[:index]
                or tried_context.get("observation_id") != context.get("observation_id")
                or not started <= _time(tried_response["observed_at"]) <= _time(original["captured_at"])
                or index < attempt and original["response_kind"] == "HTTP_METADATA_RESULT"
                and tried_response["status"] not in READ_RETRY_STATUSES
            ):
                _fail("Retried metadata read differs from its original request, actor or failed HTTP response")
            if previous is not None:
                headers = {key.lower(): value for key, value in previous.get("header_fields", {}).items()}
                delay = retry_after(headers.get("retry-after"), index - 1, now=_time(previous["observed_at"]))
                if started < _time(previous["observed_at"]) + timedelta(seconds=delay):
                    _fail("Metadata read retried before the retained Retry-After/backoff elapsed")
            previous = tried_response
            attempts.append(original)
        seen_failures.update(failed)
    attempts.sort(key=lambda record: _time(record["response"]["observed_at"]))
    return records, _observation_window(records, profile, attempts=attempts)


def _set_window(facts, window):
    group, start, end, roles = window
    facts.observation_id = UUID(group)
    facts.window_started_at, facts.window_ended_at, facts.role_order = start, end, roles
    facts.observed_at = start
    return facts


def _bindings(definition: dict, profile: MetadataStateProfile) -> dict[str, str]:
    parts = _parts(definition)
    if set(parts) != set(profile.part_paths):
        _fail("Source export part inventory differs from its complete reviewed schema")
    text = parts["definition/expressions.tmdl"].decode()
    for name, expected in (("WorkspaceId", str(profile.identity.workspace_id)), ("LakehouseId", str(profile.lakehouse_id))):
        matches = re.findall(r"^expression " + name + r' = "([^"]+)"', text, re.M)
        if matches != [expected]:
            _fail("Actual TMDL source workspace/lakehouse binding differs")
    if len(re.findall(r"^expression 'OneLake Source' =", text, re.M)) != 1 or (
        'AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/" & WorkspaceId & "/" & LakehouseId' not in text
    ):
        _fail("Unreviewed OneLake expression source")
    result = {}
    partition_parts = {path for path, raw in parts.items() if re.search(r"^\s*partition ", raw.decode(), re.M)}
    if partition_parts != {table.part for table in profile.tables}:
        _fail("All actual bound source partitions must be explicitly reviewed")
    for table in profile.tables:
        source = parts[table.part].decode()
        columns = re.findall(r"^\s*column ([A-Za-z_][A-Za-z0-9_]*)\s*$", source, re.M)
        if len(columns) != len(table.columns) or set(columns) != set(table.columns):
            _fail("Actual TMDL columns differ from the approved source-only schema whitelist")
        if len(re.findall(r"^\s*partition ", source, re.M)) != 1 or not all(
            len(re.findall(pattern, source, re.M)) == 1 for pattern in (
                r"^\s*mode: directLake\s*$", r"^\s*entityName: " + re.escape(table.entity_name) + r"\s*$",
                r"^\s*schemaName: " + re.escape(table.schema_name) + r"\s*$",
                r"^\s*expressionSource: 'OneLake Source'\s*$",
            )
        ):
            _fail("Actual Direct Lake partition/home binding differs from review")
        result[table.catalog_table] = table.table_path
    return result


def normalize_metadata(
    binding: ReaderBinding, alias: str, profile: MetadataStateProfile, *, definition: dict,
    framing: dict, paths_before: list[dict], paths_after: list[dict],
    zero_logs: dict[str, tuple[bytes, dict[str, str]]], observed_at: datetime, capture_ids: list[str],
    origin: str,
) -> MetadataFacts:
    """Pure facts. Original observation times survive replay; no eligibility grant."""
    mapping = _bindings(definition, profile)
    if observed_at.tzinfo is None:
        _fail("Original metadata observation time is required")
    blockers = []
    before = {entry["name"]: entry for entry in paths_before}
    after = {entry["name"]: entry for entry in paths_after}
    if len(before) != len(paths_before) or len(after) != len(paths_after):
        _fail("Duplicate OneLake path identity")
    if before != after:
        blockers.append("OneLake service path/incarnation/log metadata changed during observation")
    frames = framing.get("value")
    if not isinstance(frames, list) or any(framing.get(key) for key in (
        "@odata.nextLink", "continuationToken", "ContinuationToken", "error", "errors", "partial", "isPartial",
    )):
        _fail("Refresh history is not a complete reviewed array")
    candidates = [row for row in frames if row.get("requestId") == str(profile.frame_request_id)]
    if len(candidates) != 1:
        _fail("Explicit source frame identity is missing or ambiguous")
    frame = candidates[0]
    if frame.get("status") != "Completed" or frame.get("refreshType") != profile.frame_refresh_type or (
        frame.get("serviceExceptionJson") is not None
    ):
        blockers.append("Selected source frame/refresh kind is not explicitly completed without errors")
    frame_start, frame_end = _time(frame["startTime"]), _time(frame["endTime"])
    if frame_end < frame_start or frame_end > observed_at:
        blockers.append("Frame service times are inconsistent with the original observation")
    if any(_time(row["startTime"]) > frame_start for row in frames if row.get("refreshType") in {
        "DirectLakeFraming", "ViaEnhancedApi",
    }):
        blockers.append("A later frame exists; the reviewed frame identity is stale")
    tables = {}
    if not {target.table_path for target in profile.tables} <= zero_logs.keys():
        _fail("Original bound initial Delta log captures are incomplete")
    for target in profile.tables:
        prefix = str(profile.lakehouse_id) + "/Tables/" + target.table_path
        directory, log_directory = before.get(prefix), before.get(prefix + "/_delta_log")
        if not directory or not log_directory or any(
            item.get("isDirectory") != "true" for item in (directory, log_directory)
        ):
            _fail("Bound source table/log directory is missing from complete OneLake listing")
        files = {name.removeprefix(prefix + "/_delta_log/"): value for name, value in before.items()
                 if name.startswith(prefix + "/_delta_log/")}
        commits = {int(name[:20]): entry for name, entry in files.items() if re.fullmatch(r"\d{20}\.json", name)}
        if 0 not in commits:
            _fail("Initial Delta log is missing; no highest-filename incarnation inference")
        unexplained = [name for name, value in files.items() if
                       not re.fullmatch(r"\d{20}\.json", name)
                       and not (name == "_commits" and value.get("isDirectory") == "true")
                       and not name.startswith("_stats/") and not (
                           name == "_stats" and value.get("isDirectory") == "true"
                       )]
        if unexplained:
            blockers.append("Unexplained Delta checkpoint/commit/log state: " + target.catalog_table)
        if set(commits) != {0}:
            blockers.append("V1+ Delta facts are not eligible for the sole-initial-snapshot claim")
        raw, headers = zero_logs[target.table_path]
        headers = {name.lower(): value for name, value in headers.items()}
        entry = commits[0]
        if headers.get("etag", "").strip('"') != entry["etag"].strip('"') or (
            headers.get("last-modified") != entry["lastModified"]
        ):
            _fail("ETag-bound initial Delta log/service metadata differs")
        actions = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        metadata = [action["metaData"] for action in actions if "metaData" in action]
        if len(metadata) != 1 or metadata[0].get("format", {}).get("provider") != "parquet":
            _fail("Stable Delta metaData.id/schema incarnation is absent")
        incarnation = str(UUID(metadata[0]["id"]))
        protocols = [action["protocol"] for action in actions if "protocol" in action]
        if protocols != [{"minReaderVersion": 1, "minWriterVersion": 2}] or any(
            action.get("add", {}).get("deletionVector") is not None for action in actions
        ):
            blockers.append("Initial Delta protocol/features need a separately reviewed eligibility contract")
        if any("remove" in action or "cdc" in action for action in actions) or not any(
            action.get("commitInfo", {}).get("operation") == "CREATE TABLE AS SELECT" for action in actions
        ):
            blockers.append("Initial Delta actions are not the reviewed sole-create snapshot")
        schema = json.loads(metadata[0]["schemaString"])
        if schema.get("type") != "struct" or not isinstance(schema.get("fields"), list):
            _fail("Delta schemaString is not the observed struct contract")
        if {field["name"]: field["type"] for field in schema["fields"]} != target.columns:
            _fail("Actual Delta schema differs from the approved source-only schema whitelist")
        birth, log_birth = directory["creationTime"], entry["creationTime"]
        for value in (birth, log_birth, log_directory["creationTime"]):
            if not isinstance(value, str) or not value.isdigit():
                _fail("Trusted service creationTime is required; client commit timestamps are insufficient")
        born = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(log_birth) // 10)
        modified = parsedate_to_datetime(entry["lastModified"])
        if born > frame_start or modified > frame_start:
            blockers.append("Delta service birth/modification is later than the selected frame")
        if target.incarnation_id is not None and str(target.incarnation_id) != incarnation or (
            target.service_birth is not None and target.service_birth != birth
        ) or (target.log_birth is not None and target.log_birth != log_birth):
            blockers.append("Reviewed table incarnation/service birth changed or path was recreated")
        if len({field["name"] for field in schema["fields"]}) != len(schema["fields"]):
            _fail("Delta schemaString has duplicate field identities")
        active_files = {}
        for action in actions:
            if "add" not in action:
                continue
            name = action["add"]["path"]
            if not re.fullmatch(r"part-[A-Za-z0-9_.-]+\.parquet", name) or name in active_files:
                _fail("Unreviewed initial Delta file identity/path")
            physical = before.get(prefix + "/" + name)
            if not physical or physical.get("isDirectory") == "true" or not physical.get("etag"):
                _fail("Initial Delta active file metadata is missing")
            if not isinstance(physical.get("creationTime"), str) or not physical["creationTime"].isdigit():
                _fail("Initial Delta file requires trusted service birth metadata")
            if parsedate_to_datetime(physical["lastModified"]) > frame_start:
                blockers.append("Initial Delta active file was modified after framing")
            active_files[name] = {key: physical[key] for key in ("etag", "creationTime", "lastModified")}
        if not active_files:
            blockers.append("Initial Delta active file set is unproven")
        tables[target.catalog_table] = {
            "path": target.table_path, "incarnation_id": incarnation, "service_birth": birth, "log_birth": log_birth,
            "schema_hash": digest(schema), "zero_log_hash": hashlib.sha256(raw).hexdigest(),
            "schema_fields": {field["name"]: field["type"] for field in schema["fields"]},
            "active_files": active_files,
            "commits": {str(version): {"etag": item["etag"], "creationTime": item["creationTime"],
                                     "lastModified": item["lastModified"]} for version, item in sorted(commits.items())},
        }
    return MetadataFacts(
        binding_hash=binding.binding_hash, source=alias, contract_hash=profile.contract_hash, origin=origin,
        observed_at=observed_at, complete_definition_hash=definition_hash(definition), binding_map=mapping,
        frame=frame, tables=tables, data_fingerprint=digest(tables),
        model_fingerprint=digest({"complete_tmdl": definition_hash(definition), "frame": {
            key: frame[key] for key in ("requestId", "id", "refreshType", "startTime", "endTime", "status")
        }}),
        capture_ids=capture_ids, definition=definition, blockers=blockers,
        declared_business_watermark=profile.declared_business_watermark,
    )


def eligible_state(
    facts: MetadataFacts, profile: MetadataStateProfile, documents: dict[str, str], *,
    model_body_hash: str, load_job_hash: str | None = None,
    current_attestation_hash: str | None = None, synthetic=False,
    anchor_reviewed_at: datetime | None = None,
) -> ObservedSourceState:
    """V0 state, or an immutable timely-reviewed anchor; original observation clocks are never freshened."""
    now = datetime.now(timezone.utc)
    freshness_at = now if anchor_reviewed_at is None else anchor_reviewed_at
    if freshness_at.tzinfo is None or freshness_at > now:
        _fail("Source anchor review must have an original nonfuture timezone-aware clock")
    if facts.origin != ("SYNTHETIC_TEST" if synthetic else "READER_DELEGATED") or profile.provenance not in (
        {"synthetic_test", "reader_capture"} if synthetic else {"reader_capture"}
    ):
        _fail("Author/shape metadata is not current-reader source-state proof")
    if facts.blockers:
        _fail("; ".join(facts.blockers))
    if facts.contract_hash != profile.contract_hash:
        _fail("Source frame facts and reviewed metadata contract differ")
    if (
        facts.observation_id is None or facts.window_started_at is None or facts.window_ended_at is None
        or facts.observed_at != facts.window_started_at or not facts.role_order
        or not facts.window_started_at <= facts.window_ended_at <= freshness_at
        or facts.window_ended_at - facts.window_started_at > timedelta(seconds=profile.max_capture_age_seconds)
    ):
        _fail("Source state requires one complete original coherent observation window")
    if freshness_at - facts.observed_at > timedelta(seconds=profile.max_capture_age_seconds):
        _fail("Metadata observation is stale; replay is not a fresh source clock")
    if facts.observed_at > freshness_at:
        _fail("Metadata observation time is future")
    if not profile.calibration_capture_ids or any(
        target.incarnation_id is None or target.service_birth is None or target.log_birth is None
        for target in profile.tables
    ):
        _fail("Original reviewed reader calibration/incarnation pins are missing")
    anchor = profile.frame_anchor
    current_read = isinstance(anchor, CurrentReadFrameAnchor)
    if (
        anchor is None or anchor.model_body_hash != model_body_hash
        or current_read and (anchor.attestation_hash != current_attestation_hash or load_job_hash is not None)
        or not current_read and (anchor.load_job_hash != load_job_hash or current_attestation_hash is not None)
    ):
        _fail("Original full-model definition-at-frame/refresh/load coverage evidence is required")
    def original(key):
        text = documents[key]
        if hashlib.sha256(text.encode()).hexdigest() != key:
            _fail("Source frame/load original evidence hash changed")
        return json.loads(text)
    try:
        body, definition, accepted = [
            original(getattr(anchor, name)) for name in (
                "model_body_hash", "definition_at_frame_hash",
                "accepted_refresh_hash",
            )
        ]
        intent = original(anchor.full_refresh_intent_hash) if current_read and anchor.full_refresh_intent_hash else None
        refresh_body = intent["body"] if intent is not None else original(anchor.full_refresh_body_hash)
        if definition.get("method") != "POST" or definition.get("bodySha256") != digest({}):
            _fail("Original complete definition-at-frame readback is absent")
        base = f"/v1/workspaces/{profile.identity.workspace_id}/semanticModels/{profile.identity.dataset_id}/getDefinition"
        if definition["path"] not in {base, base + "?format=TMDL"}:
            _fail("Definition-at-frame source path differs")
        definition_body = definition["response"]
        definition_completed_at = _time(definition["startedAt"])
        if definition["status"] == 202 and current_read:
            operation = definition["operationId"]
            if str(UUID(operation)) != operation or not definition.get("location"):
                _fail("Current definition LRO has no original accepted operation identity")
            state_path = "/v1/operations/" + operation
            _operation_location(definition["location"], operation, profile)
            if not 0 < len(anchor.definition_state_hashes) <= profile.max_lro_polls or (
                anchor.definition_result_hash is None
                or len(set(anchor.definition_state_hashes)) != len(anchor.definition_state_hashes)
            ):
                _fail("Current definition LRO needs original state and result receipts")
            last = None
            for key in anchor.definition_state_hashes:
                state = original(key)
                body_state = state["response"]
                if (
                    not isinstance(body_state, dict) or state["method"] != "GET" or state["path"] != state_path
                    or state["status"] != 200 or _has_failure(body_state)
                    or state.get("operationId", operation) not in (None, operation)
                    or body_state.get("status") not in {"Running", "NotStarted", "Succeeded"}
                    or _time(state["startedAt"]) < definition_completed_at
                    or last is not None and last["response"].get("status") == "Succeeded"
                ):
                    _fail("Current definition LRO state chain is failed, partial or unlinked")
                definition_completed_at = _time(state["startedAt"])
                last = state
            if last["response"]["status"] != "Succeeded" or not last.get("location"):
                _fail("Current definition LRO has no successful original result Location")
            _operation_location(last["location"], operation, profile, result=True)
            result = original(anchor.definition_result_hash)
            if (
                result["method"] != "GET" or result["status"] != 200
                or result["path"] != state_path + "/result"
                or result.get("operationId", operation) not in (None, operation)
                or _time(result["startedAt"]) < definition_completed_at
            ):
                _fail("Current definition LRO result is not linked to its successful public operation")
            definition_body = result["response"]
            definition_completed_at = _time(result["startedAt"])
        elif definition["status"] != 200 or current_read and (
            anchor.definition_state_hashes or anchor.definition_result_hash is not None
        ):
            _fail("Definition-at-frame needs a complete immediate or reviewed LRO result")
        if definition_hash(definition_body) != facts.complete_definition_hash or any(
            _parts(definition_body).get(name) != raw for name, raw in _parts(body).items()
        ):
            _fail("Current/frame/original bound complete definitions differ")
        if current_read and _parts(body) != _parts(definition_body):
            _fail("Current-read complete definition and framing bindings differ")
        refresh_path = f"/v1.0/myorg/groups/{profile.identity.workspace_id}/datasets/{profile.identity.dataset_id}/refreshes"
        if intent is not None and not (
            intent["method"] == "POST" and intent["url"] == POWERBI + refresh_path
            and intent["bodySha256"] == digest(refresh_body)
            and intent["approvalSha256"] == accepted["approvalSha256"]
            and _time(intent["startedAt"]) <= _time(accepted["startedAt"])
        ):
            _fail("Original full-refresh request intent/target/body/approval linkage differs")
        allowed_body = refresh_body == {"notifyOption": "NoNotification"} or (
            set(refresh_body) <= {"type", "commitMode", "retryCount", "timeout"}
            and refresh_body.get("type") == "full" and refresh_body.get("commitMode") == "transactional"
            and refresh_body.get("retryCount") == 0
        )
        accepted_id, accepted_path = accepted_refresh_location(
            accepted, str(profile.identity.workspace_id), str(profile.identity.dataset_id),
            reviewed_regional_origin=profile.regional_location_origin,
        )
        # Original intent/acceptance share the client clock; the service job clock can be slightly earlier.
        request_started_at = _time(intent["startedAt"]) if intent is not None else _time(accepted["startedAt"])
        accepted_at = _time(accepted["startedAt"])
        if not allowed_body or not (
            accepted["method"] == "POST" and accepted["status"] == 202 and accepted["path"] == refresh_path
            and accepted["bodySha256"] == digest(refresh_body)
            and accepted_id == str(profile.frame_request_id)
            and _time(definition["startedAt"]) <= request_started_at <= accepted_at
            and definition_completed_at <= request_started_at
            and request_started_at - _time(definition["startedAt"]) <= timedelta(minutes=5)
            and (intent is not None or (
                accepted_at <= _time(facts.frame["startTime"])
                and _time(facts.frame["startTime"]) - _time(definition["startedAt"]) <= timedelta(minutes=5)
            ))
        ):
            _fail("Exact full-model framing request/identity/bindings are unproven; selective/calculate refresh is not coverage")
        if profile.frame_refresh_type == "ViaEnhancedApi":
            if not current_read or anchor.refresh_details_hash is None:
                _fail("Enhanced full refresh requires original completed table/partition details")
            if not (
                refresh_body.get("type") == "full" and refresh_body.get("commitMode") == "transactional"
                and refresh_body.get("retryCount") == 0
            ):
                _fail("Enhanced full-frame proof requires an explicit complete transactional refresh request")
            details = original(anchor.refresh_details_hash)
            values = details["response"]
            if (
                details["method"] != "GET" or details["status"] != 200 or details["path"] != accepted_path
                or not isinstance(values, dict) or _has_failure(values)
                or values.get("type") != "Full" or values.get("commitMode") != "Transactional"
                or values.get("status") != "Completed" or values.get("extendedStatus") != "Completed"
                or values.get("serviceExceptionJson") is not None
                or _refresh_detail_time(values["startTime"], facts.frame["startTime"])
                != _time(facts.frame["startTime"])
                or _refresh_detail_time(values["endTime"], facts.frame["endTime"])
                != _time(facts.frame["endTime"])
                or _refresh_detail_time(values["endTime"], facts.frame["endTime"])
                > _time(details["observedAtUtc"])
            ):
                _fail("Enhanced refresh details are partial, unlinked or not the exact completed full transaction")
            from telco_iq.native_context import NAME, _name
            expected_objects = set()
            for target in profile.tables:
                text = _parts(definition_body)[target.part].decode()
                names = re.findall(r"^table (" + NAME + r")\s*$", text, re.M)
                partitions = re.findall(r"^\s*partition (" + NAME + r") = entity\s*$", text, re.M)
                if len(names) != 1 or len(partitions) != 1:
                    _fail("Every enhanced-refresh source table and partition must be explicitly bound")
                expected_objects.add((_name(names[0]), _name(partitions[0])))
            if len(expected_objects) != len(profile.tables):
                _fail("Enhanced refresh source table/partition identities are ambiguous")
            objects = values.get("objects")
            if (
                not isinstance(objects, list) or len(objects) != len(expected_objects)
                or any(not isinstance(item, dict) or set(item) != {"table", "partition", "status"}
                       or item["status"] != "Completed" for item in objects)
                or {(item["table"], item["partition"]) for item in objects} != expected_objects
            ):
                _fail("Enhanced full refresh did not complete the exact entire bound table/partition set")
        elif current_read and anchor.refresh_details_hash is not None:
            _fail("Enhanced-refresh details cannot be relabelled as a native DirectLakeFraming entry")
        if current_read:
            report, plan = original(anchor.attestation_hash), original(anchor.read_plan_hash)
            expected_report_kind = (
                "ACTUAL_SHOWCASE_CURRENT_SOURCE_TYPED_ROW_AUDIT"
                if anchor.kind == "current_showcase_read_full_frame_v1"
                else "ACTUAL_SELECTED_USER_CURRENT_SOURCE_DATA_READ_ATTESTATION"
            )
            if (
                report["kind"] != expected_report_kind
                or report["proposal_sha256"] != anchor.read_plan_hash
                or not _time(report["source_capture_started_at"]) <= _time(report["observed_at"])
                <= _time(definition["startedAt"])
            ):
                _fail("Current row audit must precede the exact new full-model framing request")
            for name, table in facts.tables.items():
                operations = [item for item in plan["operations"] if item["table"] == name]
                if not operations or any(
                    operation["Delta_version" if anchor.kind == "current_showcase_read_full_frame_v1" else "deltaVersion"] != 0
                    or operation["incarnation_id" if anchor.kind == "current_showcase_read_full_frame_v1" else "deltaTableId"] != table["incarnation_id"]
                    or operation["log_body_sha256" if anchor.kind == "current_showcase_read_full_frame_v1" else "sourceLogResponseSha256"] != table["zero_log_hash"]
                    for operation in operations
                ):
                    _fail("Current frame incarnation/log differs from the verified row audit")
                expected = {
                    f"/{profile.identity.workspace_id}/{profile.lakehouse_id}/Tables/{table['path']}/{file}": metadata
                    for file, metadata in table["active_files"].items()
                }
                observed = [item for item in report["source_files"] if item["table"] == name]
                if len(observed) != len(expected) or {item["source_path"] for item in observed} != set(expected) or any(
                    item["headers"]["etag"].strip('"') != expected[item["source_path"]]["etag"].strip('"')
                    for item in observed
                ):
                    _fail("Current framing active-file set differs from the verified row audit")
        else:
            job = original(anchor.load_job_hash)
            job = job.get("response", job)
            if job.get("status") != "Completed":
                _fail("Original loaded population job is not completed")
            start, end = _time(job["startTimeUtc"]), _time(job["endTimeUtc"])
            for table in facts.tables.values():
                for name in ("service_birth", "log_birth"):
                    birth = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(table[name]) // 10)
                    if not start <= birth <= end <= _time(facts.frame["startTime"]):
                        _fail("Delta incarnation service birth is outside the verified original load/frame window")
                for item in table["active_files"].values():
                    birth = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(item["creationTime"]) // 10)
                    if not start <= birth <= end or parsedate_to_datetime(item["lastModified"]) > end:
                        _fail("Initial Delta active file metadata is outside the verified sole-load window")
    except (KeyError, TypeError, ValueError):
        _fail("Required original definition-at-frame/full-model refresh/load evidence shape is unavailable")
    return ObservedSourceState(
        data_revision="DERIVED_DELTA_VECTOR:" + facts.data_fingerprint,
        model_revision="DERIVED_TMDL_FRAME:" + facts.model_fingerprint,
        evidence_hash=digest({"capture_ids": facts.capture_ids, "facts": facts.model_dump(mode="json")}),
        observed_at=facts.observed_at,
    )


def _json_body(response: MetadataResponse):
    try:
        value = json.loads(response.body)
    except (ValueError, UnicodeError):
        _fail("Retained source metadata response is not the reviewed JSON shape")
    if not isinstance(value, dict) or _has_failure(value):
        _fail("Retained metadata reports embedded errors or partial output")
    return value


def _operation_location(value, operation, profile, *, result=False):
    expected = "/v1/operations/" + operation + ("/result" if result else "")
    if value is None:
        return FABRIC + expected
    parsed = urlsplit(value)
    origin = parsed.scheme + "://" + parsed.netloc
    if value not in {expected, FABRIC + expected, (profile.regional_location_origin or "") + expected}:
        _fail("Returned metadata operation origin/UUID/path is not explicitly reviewed")
    if parsed.netloc and origin not in {FABRIC, profile.regional_location_origin}:
        _fail("Regional metadata Location is evidence only; no regional bearer dispatch")
    return FABRIC + expected


class SourceMetadataObserver:
    def __init__(self, binding, profiles, pins, requester: MetadataRequester, sink, budget,
                 session_active: Callable[[Scope], bool], *, synthetic=False):
        self.binding, self.profiles, self.pins = binding, profiles, pins
        self.requester, self.sink, self.budget = requester, sink, budget
        self.session_active, self.synthetic = session_active, synthetic
        self._log_slots = asyncio.Semaphore(4)

    def _current(self, alias):
        if not self.session_active(self.binding.scope):
            _fail("Current reader session ended during metadata capture", Status.BLOCKED_AUTH)
        self.profiles[alias].verify(self.binding, alias, self.pins[alias], synthetic=self.synthetic)

    async def _call(self, alias, request, captures):
        failed = []
        enabled = request.method == "GET" and self.profiles[alias].max_read_attempts > 1
        attempts = self.profiles[alias].max_read_attempts if enabled else 1
        for attempt in range(attempts):
            response, reference, transport_status = await self._attempt(
                alias, request, captures, attempt if enabled else None, failed,
            )
            if response is None:
                if not enabled or attempt == attempts - 1:
                    _fail("Metadata GET transport failed after its approved attempt bound; no response or fallback",
                          transport_status)
                failed.append(reference.capture_id)
                delay = retry_after(None, attempt)
                if delay >= self.budget.remaining_seconds():
                    _fail("Read transport backoff exceeds the remaining approved observation budget", Status.TIMED_OUT)
                await asyncio.sleep(delay)
                continue
            if response.status not in READ_RETRY_STATUSES or attempt == attempts - 1:
                captures.append(reference.capture_id)
                if response.status not in (200, 202):
                    _fail("Metadata request failed after its approved read-attempt bound; no redirect or fallback")
                return response
            failed.append(reference.capture_id)
            headers = {key.lower(): value for key, value in response.header_fields.items()}
            delay = retry_after(headers.get("retry-after"), attempt, now=response.observed_at)
            if delay >= self.budget.remaining_seconds():
                _fail("Metadata Retry-After exceeds the remaining approved observation budget", Status.TIMED_OUT)
            await asyncio.sleep(delay)
        raise AssertionError("Bounded metadata read attempts exhausted without a response")

    async def _attempt(self, alias, request, captures, attempt, failed):
        self._current(alias)
        self.sink.ready()
        self.budget.reserve()
        started = datetime.now(timezone.utc)
        context = {
            "kind": "SOURCE_METADATA_OBSERVATION", "binding_hash": self.binding.binding_hash,
            "source": self.binding.sources[alias].model_dump(mode="json"),
            "contract_hash": self.profiles[alias].contract_hash, "code_hash": code_hash(),
            "origin": "SYNTHETIC_TEST" if self.synthetic else "READER_DELEGATED",
            "observation_id": captures.observation_id,
            **({"read_attempt": attempt, "read_attempt_started_at": started.isoformat(),
                "failed_read_attempt_ids": list(failed)} if attempt is not None else {}),
        }
        try:
            async with asyncio.timeout(self.budget.remaining_seconds()):
                response = await self.requester(request)
        except httpx.HTTPError as error:
            if request.method == "GET" and isinstance(error, httpx.TransportError) and type(error).__name__ in READ_TRANSPORT_FAILURES:
                reference = self.sink.capture(
                    tool="SourceMetadata", source_alias=alias, request=request.evidence(),
                    response={
                        "response_obtained": False, "transport_error_type": type(error).__name__,
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "actor_scope": self.binding.scope.model_dump(mode="json"), "resource": request.resource,
                    },
                    context=context, private_identifiers=private_identifiers(self.binding),
                    synthetic=self.synthetic, response_kind="HTTP_METADATA_TRANSPORT_FAILURE",
                )
                self._current(alias)
                return None, reference, Status.TIMED_OUT if isinstance(error, httpx.TimeoutException) else Status.BLOCKED_CAPABILITY
            _fail("Source metadata transport failed; no response, retry or fallback",
                  Status.TIMED_OUT if isinstance(error, httpx.TimeoutException) else Status.BLOCKED_CAPABILITY)
        # Retain originals before status/body/actor/decoder validation.
        reference = self.sink.capture_http_metadata(
            source_alias=alias, request=request.evidence(), status=response.status,
            header_fields=response.header_fields, body=response.body, observed_at=response.observed_at,
            actor_scope=response.actor_scope,
            resource=response.resource,
            original_header_pairs=response.original_header_pairs,
            context=context,
            private_identifiers=private_identifiers(self.binding), synthetic=self.synthetic,
        )
        self.budget.response_bytes(len(response.body) + sum(len(k) + len(v) for k, v in response.header_fields.items()))
        self._current(alias)
        if response.actor_scope != self.binding.scope or response.resource != request.resource:
            _fail("Metadata response belongs to another reader/resource", Status.BLOCKED_AUTH)
        if response.status in (401, 403):
            _fail("Current reader metadata permission is unavailable", Status.BLOCKED_AUTH)
        return response, reference, None

    async def _definition(self, alias, phase, captures):
        profile = self.profiles[alias]
        base = f"{FABRIC}/v1/workspaces/{profile.identity.workspace_id}/semanticModels/{profile.identity.dataset_id}"
        response = await self._call(alias, MetadataRequest(phase, "source_metadata", "POST",
                                                         base + "/getDefinition?format=TMDL", b"{}",
                                                         (("Content-Type", "application/json"),
                                                          ("x-ms-fabric-skill", "semantic-model-authoring"))), captures)
        if response.status == 200:
            value = _json_body(response)
            self.budget.returned(len(_parts(value)))
            return value
        headers = {name.lower(): value for name, value in response.header_fields.items()}
        operation = headers.get("x-ms-operation-id")
        if operation is None or str(UUID(operation)) != operation:
            _fail("Accepted metadata operation ID is absent; never repeat POST")
        state_url = _operation_location(headers.get("location"), operation, profile)
        for index in range(profile.max_lro_polls):
            delay = int(headers.get("retry-after", "0"))
            if delay < 0:
                _fail("Metadata Retry-After is invalid")
            if delay >= self.budget.remaining_seconds():
                _fail("Metadata Retry-After exceeds the approved observation window", Status.TIMED_OUT)
            if delay:
                await asyncio.sleep(delay)
            state = await self._call(alias, MetadataRequest(
                phase + "-state-" + str(index), "source_metadata", "GET", state_url,
                header_fields=(("x-ms-fabric-skill", "semantic-model-authoring"),),
            ), captures)
            headers = {name.lower(): value for name, value in state.header_fields.items()}
            value = _json_body(state)
            self.budget.returned(1)
            if headers.get("x-ms-operation-id", operation) != operation:
                _fail("Metadata operation identity changed")
            if state.status == 200 and value.get("status") == "Succeeded":
                if _has_failure(value):
                    _fail("Metadata operation reports embedded error/partial state")
                result_url = _operation_location(headers.get("location"), operation, profile, result=True)
                result = await self._call(alias, MetadataRequest(
                    phase + "-result", "source_metadata", "GET", result_url,
                    header_fields=(("x-ms-fabric-skill", "semantic-model-authoring"),),
                ), captures)
                if result.status != 200:
                    _fail("Metadata operation result is not an actual 200")
                value = _json_body(result)
                self.budget.returned(len(_parts(value)))
                return value
            if value.get("status") not in {"NotStarted", "Running"}:
                _fail("Metadata operation did not succeed")
        _fail("Metadata operation polling limit exhausted", Status.PARTIAL)

    async def _paths(self, alias, phase, captures):
        profile, result, cursor, seen = self.profiles[alias], [], None, set()
        for index in range(profile.max_pages):
            query = {
                "resource": "filesystem", "directory": str(profile.lakehouse_id) + "/Tables",
                "recursive": "true", "maxResults": "5000",
            }
            if cursor:
                query["continuation"] = cursor
            response = await self._call(alias, MetadataRequest(
                phase + "-" + str(index), "storage", "GET",
                STORAGE + "/" + str(profile.identity.workspace_id) + "?" + urlencode(query),
                header_fields=(("x-ms-version", profile.storage_api_version),),
            ), captures)
            body = _json_body(response)
            values = body.get("paths")
            if response.status != 200 or not isinstance(values, list):
                _fail("OneLake listing does not prove complete bound metadata")
            self.budget.returned(len(values))
            result += values
            headers = {name.lower(): value for name, value in response.header_fields.items()}
            cursor = headers.get("x-ms-continuation") or body.get("ContinuationToken")
            if not cursor:
                return result
            if not isinstance(cursor, str) or cursor in seen:
                _fail("OneLake continuation is invalid or repeated")
            seen.add(cursor)
        _fail("OneLake metadata pagination limit reached", Status.PARTIAL)

    async def capture(self, alias: str) -> MetadataFacts:
        self._current(alias)
        profile, captures = self.profiles[alias], _CaptureGroup()
        definition = await self._definition(alias, "definition-before", captures)
        _bindings(definition, profile)  # Wrong source bindings stop before destination storage requests.
        history_url = (
            f"{POWERBI}/v1.0/myorg/groups/{profile.identity.workspace_id}/datasets/"
            f"{profile.identity.dataset_id}/refreshes?$top=100"
        )
        before = await self._call(alias, MetadataRequest("frame-before", "iq", "GET", history_url), captures)
        history = _json_body(before)
        if before.status != 200 or not isinstance(history.get("value"), list) or len(history["value"]) >= 100 or any(
            history.get(key) for key in ("@odata.nextLink", "ContinuationToken", "continuationToken")
        ):
            _fail("Exact bounded source refresh history is incomplete")
        self.budget.returned(len(history["value"]))
        paths = await self._paths(alias, "paths-before", captures)
        failed = False

        async def initial_log(target):
            nonlocal failed
            name = str(profile.lakehouse_id) + "/Tables/" + target.table_path + "/_delta_log/" + V0
            entries = [entry for entry in paths if entry["name"] == name]
            if len(entries) != 1:
                _fail("Bound initial Delta log is absent or ambiguous")
            async with self._log_slots:
                if failed:
                    _fail("Initial metadata reads stopped after a required Delta log failed")
                completed = False
                try:
                    response = await self._call(alias, MetadataRequest(
                        "log0:" + target.table_path, "storage", "GET",
                        STORAGE + "/" + str(profile.identity.workspace_id) + "/" + name,
                        header_fields=(("If-Match", entries[0]["etag"]), ("x-ms-version", profile.storage_api_version)),
                    ), captures)
                    if response.status != 200:
                        _fail("ETag-bound initial Delta log was not returned")
                    self.budget.returned(len(response.body.splitlines()))
                    completed = True
                    return response.body, response.header_fields
                finally:
                    if not completed:
                        failed = True

        try:
            async with asyncio.TaskGroup() as group:
                tasks = {target.table_path: group.create_task(initial_log(target)) for target in profile.tables}
        except ExceptionGroup as error:
            if len(error.exceptions) == 1:
                raise error.exceptions[0] from None
            status = mcp_capture_failure_status(error)
            details = "; ".join(dict.fromkeys(str(cause) for cause in error.exceptions if isinstance(cause, LabError)))
            raise LabError("Concurrent initial Delta metadata reads failed: " + details, status) from None
        zero_logs = {name: task.result() for name, task in tasks.items()}
        after_paths = await self._paths(alias, "paths-after", captures)
        after = await self._call(alias, MetadataRequest("frame-after", "iq", "GET", history_url), captures)
        self.budget.returned(len(_json_body(after).get("value", [])))
        after_definition = await self._definition(alias, "definition-after", captures)
        _, window = _retained_metadata_window(
            self.binding, alias, profile, self.sink, captures, synthetic=self.synthetic,
        )
        facts = normalize_metadata(
            self.binding, alias, profile, definition=definition, framing=history, paths_before=paths,
            paths_after=after_paths, zero_logs=zero_logs, observed_at=window[1],
            capture_ids=captures, origin="SYNTHETIC_TEST" if self.synthetic else "READER_DELEGATED",
        )
        if history != _json_body(after):
            facts.blockers.append("Source framing history changed during observation")
        if definition_hash(after_definition) != facts.complete_definition_hash:
            facts.blockers.append("Complete model definition changed during observation")
        self._current(alias)
        return _set_window(facts, window)

    async def observe(
        self, binding, alias, documents, model_body_hash, load_job_hash=None, *, current_attestation_hash=None,
    ):
        if binding != self.binding:
            _fail("Source metadata observer reader binding differs", Status.BLOCKED_AUTH)
        facts = await self.capture(alias)
        self.validate_calibration(alias)
        return eligible_state(facts, self.profiles[alias], documents, model_body_hash=model_body_hash,
                              load_job_hash=load_job_hash, current_attestation_hash=current_attestation_hash,
                              synthetic=self.synthetic)

    def validate_calibration(self, alias):
        profile = self.profiles[alias]
        if not profile.calibration_capture_ids:
            _fail("A separately reviewed original reader calibration capture is required")
        facts = facts_from_captures(self.binding, alias, profile, self.sink, profile.calibration_capture_ids,
                                    synthetic=self.synthetic)
        if facts.blockers:
            _fail("Reader calibration is partial, mismatched or changed")
        if facts.observed_at > profile.reviewed_at:
            _fail("Reader calibration must be reviewed after its original capture")
        return facts


def facts_from_captures(binding, alias, profile, sink, capture_ids, *, synthetic=False) -> MetadataFacts:
    """Re-open retained original HTTP facts without re-stamping time or performing I/O."""
    records, window = _retained_metadata_window(binding, alias, profile, sink, capture_ids, synthetic=synthetic)
    roles = {}
    operations, succeeded = {}, {}
    for record in records:
        request, response, context, raw = _metadata_record(record, binding, alias, profile, synthetic=synthetic)
        role = request["role"]
        headers = {name.lower(): value for name, value in response["header_fields"].items()}
        base = f"{FABRIC}/v1/workspaces/{profile.identity.workspace_id}/semanticModels/{profile.identity.dataset_id}"
        if role in {"definition-before", "definition-after"}:
            if request["url"] != base + "/getDefinition?format=TMDL" or request["resource"] != "source_metadata" or (
                request["method"] != "POST" or base64.b64decode(request["body_base64"]) != b"{}"
            ):
                _fail("Retained definition request target/body differs")
            if response["status"] == 202:
                operation = headers["x-ms-operation-id"]
                if str(UUID(operation)) != operation:
                    _fail("Retained operation ID is invalid")
                _operation_location(headers.get("location"), operation, profile)
                operations[role] = operation
        elif role.startswith("definition-"):
            phase = role.split("-state-", 1)[0] if "-state-" in role else role.removesuffix("-result")
            operation = operations.get(phase)
            if operation is None or request["resource"] != "source_metadata" or request["method"] != "GET":
                _fail("Retained LRO read is not linked to an actual accepted public operation")
            if "-state-" in role:
                if request["url"] != FABRIC + "/v1/operations/" + operation:
                    _fail("Retained operation state target differs")
                value = json.loads(raw)
                if response["status"] == 200 and value.get("status") == "Succeeded":
                    if _has_failure(value) or headers.get("x-ms-operation-id", operation) != operation:
                        _fail("Retained operation state is partial or errored")
                    succeeded[phase] = _operation_location(headers.get("location"), operation, profile, result=True)
            elif role.endswith("-result"):
                if request["url"] != succeeded.get(phase) or response["status"] != 200:
                    _fail("Retained result has no actual successful operation state")
            else:
                _fail("Unknown retained operation read role")
        elif role in {"frame-before", "frame-after"}:
            expected = f"{POWERBI}/v1.0/myorg/groups/{profile.identity.workspace_id}/datasets/{profile.identity.dataset_id}/refreshes?$top=100"
            if request["url"] != expected or request["resource"] != "iq" or request["method"] != "GET":
                _fail("Retained source framing history target/resource differs")
        elif role.startswith(("paths-before-", "paths-after-")):
            parsed = urlsplit(request["url"])
            if parsed.scheme + "://" + parsed.netloc != STORAGE or parsed.path != "/" + str(profile.identity.workspace_id):
                _fail("Retained OneLake listing target differs")
            from urllib.parse import parse_qs
            query = parse_qs(parsed.query)
            if (
                request["resource"] != "storage" or request["method"] != "GET"
                or set(query) - {"directory", "resource", "recursive", "maxResults", "continuation"}
                or query.get("directory") != [str(profile.lakehouse_id) + "/Tables"]
                or query.get("resource") != ["filesystem"] or query.get("recursive") != ["true"]
                or query.get("maxResults") != ["5000"]
            ):
                _fail("Retained OneLake listing is outside the bound lakehouse")
        elif role.startswith("log0:"):
            table = next((target for target in profile.tables if target.table_path == role.removeprefix("log0:")), None)
            expected = f"{STORAGE}/{profile.identity.workspace_id}/{profile.lakehouse_id}/Tables/{table.table_path if table else ''}/_delta_log/{V0}"
            if table is None or request["url"] != expected or request["resource"] != "storage" or request["method"] != "GET":
                _fail("Retained Delta log target/resource differs")
        else:
            _fail("Unknown retained metadata operation")
        roles.setdefault(role, []).append((response, raw))
    def successful(prefix, *, definition=False):
        found = [(response, raw) for role, values in roles.items() if role == prefix or (
            definition and role == prefix + "-result"
        ) for response, raw in values if response["status"] == 200]
        if len(found) != 1:
            _fail("Original successful metadata result is missing or ambiguous")
        return json.loads(found[0][1])
    def paths(phase):
        result, continuation = [], None
        pages = sorted((role, values) for role, values in roles.items() if role.startswith(phase + "-"))
        seen = set()
        for index, (role, values) in enumerate(pages):
            if role != phase + "-" + str(index) or len(values) != 1 or values[0][0]["status"] != 200:
                _fail("Retained OneLake pagination is incomplete")
            response, raw = values[0]
            record = next(item for item in records if item["request"]["role"] == role)
            from urllib.parse import parse_qs
            requested = parse_qs(urlsplit(record["request"]["url"]).query).get("continuation")
            if requested != ([continuation] if continuation else None):
                _fail("Retained OneLake page is not linked to the actual prior continuation")
            value = json.loads(raw)
            result += value["paths"]
            headers = {key.lower(): val for key, val in response["header_fields"].items()}
            continuation = headers.get("x-ms-continuation") or value.get("ContinuationToken")
            if index < len(pages) - 1 and not continuation:
                _fail("Retained extra page has no service continuation")
            if continuation:
                if not isinstance(continuation, str) or continuation in seen:
                    _fail("Retained continuation is invalid or repeated")
                seen.add(continuation)
        if not pages or continuation or len(pages) > profile.max_pages:
            _fail("Retained final OneLake page is missing")
        return result
    definition = successful("definition-before", definition=True)
    listed = {item["name"]: item for item in paths("paths-before")}
    for target in profile.tables:
        role = "log0:" + target.table_path
        candidates = [item for item in records if item["request"]["role"] == role]
        expected_path = str(profile.lakehouse_id) + "/Tables/" + target.table_path + "/_delta_log/" + V0
        if len(candidates) != 1 or candidates[0]["response"]["status"] != 200 or (
            {key.lower(): value for key, value in candidates[0]["request"]["header_fields"].items()}.get("if-match")
            != listed[expected_path]["etag"]
        ):
            _fail("Retained initial log is not ETag-bound to the complete service listing")
    facts = normalize_metadata(
        binding, alias, profile, definition=definition, framing=successful("frame-before"),
        paths_before=paths("paths-before"), paths_after=paths("paths-after"),
        zero_logs={
            target.table_path: (roles["log0:" + target.table_path][0][1],
                                roles["log0:" + target.table_path][0][0]["header_fields"])
            for target in profile.tables
        },
        observed_at=window[1],
        capture_ids=capture_ids, origin="SYNTHETIC_TEST" if synthetic else "READER_DELEGATED",
    )
    if successful("frame-before") != successful("frame-after") or (
        definition_hash(successful("definition-after", definition=True)) != facts.complete_definition_hash
    ):
        facts.blockers.append("Retained before/after source model/frame evidence differs")
    return _set_window(facts, window)


class SourceMetadataCaptureConfig(Contract):
    scope: Scope
    ontology_workspace_id: UUID
    ontology_item_id: UUID
    native_snapshot_hash: Sha256
    profiles: dict[str, MetadataStateProfile]
    profile_pins: dict[str, Sha256]
    source_execution_mode: Literal["sequential", "parallel"] = Field(
        default="sequential", exclude_if=lambda value: value == "sequential",
    )
    budgets: Budgets
    private_evidence: PrivateEvidenceConfig
    approval: Approval
    effects: Literal["SOURCE_DEFINITION_FRAMING_ONELAKE_METADATA_ONLY"]

    @property
    def target_digest(self):
        return digest({"scope": self.scope.model_dump(mode="json"),
                       "sources": {alias: profile.identity.model_dump(mode="json")
                                   for alias, profile in self.profiles.items()}})

    @property
    def artifact_digest(self):
        return digest(self.model_dump(mode="json", exclude={"approval"}))


class SourceMetadataCaptureService:
    """Server-fixed capture-only seam; constructor reads config but never authenticates or probes."""

    def __init__(self, reader, config_file: Path, root: Path, *, requester_factory=None):
        self.reader, self.root, self.requester_factory = reader, root, requester_factory
        try:
            path = private_config_path(config_file, root)
            if not path.is_absolute() or path.stat().st_size > 2_000_000:
                _fail("Source metadata capture configuration exceeds private limits")
            raw = path.read_bytes()
            from telco_iq.reader_mcp_capture import EFFECTS, ReaderMCPMetadataCaptureConfig
            from telco_iq.reader_native_question import EFFECTS as NATIVE_QUESTION_EFFECTS
            from telco_iq.reader_native_question import ReaderNativeQuestionConfig
            from telco_iq.reader_query_capture import EFFECTS as QUERY_EFFECTS
            from telco_iq.reader_query_capture import ReaderQueryCalibrationConfig
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Capture configuration must be an object")
            if value.get("effects") == EFFECTS:
                self.config = ReaderMCPMetadataCaptureConfig.model_validate_json(raw)
            elif value.get("effects") == QUERY_EFFECTS:
                self.config = ReaderQueryCalibrationConfig.model_validate_json(raw)
            elif value.get("effects") == NATIVE_QUESTION_EFFECTS:
                self.config = ReaderNativeQuestionConfig.model_validate_json(raw)
            else:
                self.config = SourceMetadataCaptureConfig.model_validate_json(raw)
        except LabError:
            raise
        except (OSError, ValueError):
            _fail("Fixed private source metadata capture configuration is invalid")

    async def capture(self, session) -> dict:
        config = self.config
        from telco_iq.reader_mcp_capture import ReaderMCPMetadataCaptureConfig, capture_reader_mcp_metadata
        from telco_iq.reader_native_question import ReaderNativeQuestionConfig, capture_reader_native_question
        from telco_iq.reader_query_capture import (
            ReaderQueryCalibrationConfig,
            capture_reader_query_calibration,
        )
        if isinstance(config, ReaderNativeQuestionConfig):
            if self.requester_factory is not None:
                _fail("HTTP metadata requester cannot select a native question transport")
            return await capture_reader_native_question(self.reader, config, self.root, session)
        if isinstance(config, ReaderQueryCalibrationConfig):
            if self.requester_factory is not None:
                _fail("HTTP metadata requester cannot select a source-query calibration transport")
            return await capture_reader_query_calibration(self.reader, config, self.root, session)
        if isinstance(config, ReaderMCPMetadataCaptureConfig):
            if self.requester_factory is not None:
                _fail("HTTP metadata requester cannot select an MCP bootstrap transport")
            return await capture_reader_mcp_metadata(self.reader, config, self.root, session)
        scope = session.scope().model_copy(update={"models": sorted(config.profiles)})
        if scope != config.scope or not self.reader.active(session):
            _fail("Source metadata capture belongs to another current reader session", Status.BLOCKED_AUTH)
        config.approval.authorize(config.artifact_digest, config.target_digest, "source-metadata-capture")
        if set(config.profiles) != set(config.profile_pins) or set(scope.models) != set(config.profiles):
            _fail("Capture profile pins must cover exactly the approved reader sources")
        binding = ReaderBinding(
            scope=scope, ontology_workspace_id=config.ontology_workspace_id, ontology_item_id=config.ontology_item_id,
            native_snapshot_hash=config.native_snapshot_hash,
            sources={alias: profile.identity for alias, profile in config.profiles.items()},
        )
        for alias, profile in config.profiles.items():
            profile.verify(binding, alias, config.profile_pins[alias], synthetic=False)
        tokens = {}
        if self.requester_factory is None:
            for resource in ("source_metadata", "storage", "iq"):
                if not self.reader.active(session):
                    _fail("Reader ended before metadata token acquisition", Status.BLOCKED_AUTH)
                tokens[resource] = await asyncio.to_thread(self.reader.token, session, resource=resource)
        sink = PrivateEvidenceSink(config.private_evidence, self.root, forbidden_values=tuple(tokens.values()))
        budget = SourceCallBudget(config.budgets)
        async with httpx.AsyncClient(follow_redirects=False, timeout=config.budgets.timeout_seconds) as client:
            async def request(value):
                if not self.reader.active(session):
                    _fail("Reader ended before metadata request", Status.BLOCKED_AUTH)
                response = await client.request(
                    value.method, value.url, content=value.body,
                    headers={**dict(value.header_fields), "Authorization": "Bearer " + tokens[value.resource]},
                )
                return MetadataResponse(
                    response.status_code, dict(response.headers), response.content, datetime.now(timezone.utc),
                    scope, value.resource, tuple((key.decode("latin-1"), val.decode("latin-1"))
                                                 for key, val in response.headers.raw),
                )
            requester = self.requester_factory(binding, sink) if self.requester_factory else request
            def active_scope(current):
                config.approval.authorize(config.artifact_digest, config.target_digest, "source-metadata-capture")
                return current == scope and self.reader.active(session)
            observer = SourceMetadataObserver(
                binding, config.profiles, config.profile_pins, requester, sink, budget,
                session_active=active_scope,
            )
            facts = []
            async def capture_source(alias):
                try:
                    return await observer.capture(alias)
                except (ValueError, KeyError, TypeError, UnicodeError):
                    _fail("Retained metadata requires an additional reviewed shape; no source state was granted")
            try:
                async with asyncio.timeout(config.budgets.timeout_seconds):
                    if config.source_execution_mode == "parallel":
                        try:
                            async with asyncio.TaskGroup() as group:
                                tasks = [group.create_task(capture_source(alias)) for alias in sorted(config.profiles)]
                        except ExceptionGroup as error:
                            known, unknown = error.split(LabError)
                            if unknown is not None:
                                raise
                            from telco_iq.mcp_client import mcp_capture_failure_status
                            _fail("Parallel source metadata capture was blocked; no source state was granted",
                                  mcp_capture_failure_status(known))
                        facts = [task.result() for task in tasks]
                    else:
                        for alias in sorted(config.profiles):
                            facts.append(await capture_source(alias))
            except (ValueError, KeyError, TypeError, UnicodeError):
                _fail("Retained metadata requires an additional reviewed shape; no source state was granted")
            except TimeoutError:
                _fail("Source metadata observation exceeded its approved time budget", Status.TIMED_OUT)
            if not self.reader.active(session):
                _fail("Reader ended during source metadata capture", Status.BLOCKED_AUTH)
        return {
            "kind": "SOURCE_METADATA_CAPTURE_ONLY", "ready": False,
            "claim": "Metadata facts only; no source state, engine-consumed versions, RLS or complete business answer.",
            "capture_hashes": [key for fact in facts for key in fact.capture_ids],
            "facts": [{
                "source_alias": fact.source, "observed_at": fact.observed_at.isoformat(),
                "data_fingerprint": fact.data_fingerprint, "model_fingerprint": fact.model_fingerprint,
                "versions": {name: list(table["commits"]) for name, table in fact.tables.items()},
                "blockers": fact.blockers + ["Capture-only: reader calibration and original full-frame coverage require review"],
            } for fact in facts],
        }


def delegated_metadata_requester(client, scope, tokens, active):
    """Parent/Gateway HTTP seam: resource-specific current-reader tokens only."""
    async def request(value):
        if not active(scope):
            _fail("Reader ended before metadata request", Status.BLOCKED_AUTH)
        response = await client.request(
            value.method, value.url, content=value.body,
            headers={**dict(value.header_fields), "Authorization": "Bearer " + tokens[value.resource]},
        )
        return MetadataResponse(
            response.status_code, dict(response.headers), response.content, datetime.now(timezone.utc),
            scope, value.resource, tuple((key.decode("latin-1"), val.decode("latin-1"))
                                         for key, val in response.headers.raw),
        )
    return request
