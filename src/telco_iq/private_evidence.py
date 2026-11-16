"""Bounded, explicitly approved private MCP evidence, never a transport log.

Only tool arguments, returned JSON and reviewed source/query context enter this
sink. Authentication objects/headers are not accepted. Raw means the lossless
JSON result object, not HTTP bytes. Unsafe echoes are refused, never silently
redacted into purported raw evidence.

Retention is a hard count/byte cap with no eviction or automatic cleanup.
Exhaustion or approval expiry blocks new captures; removal needs a separately
approved owner action. Derivatives are deterministic, not replay substitutes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, model_validator

from telco_iq.approval import private_config_path
from telco_iq.catalog import digest
from telco_iq.contracts import Contract, Status
from telco_iq.errors import LabError
from telco_iq.evidence import JWT
from telco_iq.live_contracts import ReaderBinding

_OWNER = "telco-iq-private-source-evidence-v1"
_FILE = re.compile(r"tq005-([0-9a-f]{64})\.json")
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b")
_AUTH_KEYS = frozenset({
    "authorization", "proxyauthorization", "cookie", "setcookie", "headers",
    "token", "accesstoken", "refreshtoken", "idtoken", "clientsecret", "clientcredential",
    "clientcredentials", "credential", "credentials", "password", "privatekey", "apikey",
    "connectionstring", "sastoken", "secret", "auth", "authentication", "clientassertion",
    "accesskey", "sharedaccesssignature", "xapikey", "ocpapimsubscriptionkey",
})
_AUTH_TEXT = re.compile(
    r"(?i)\b(?:bearer|basic)\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:[?&;]|\b)(?:sig|access_token|refresh_token|client_secret|password|api_key|AccountKey)="
)


def _fail(message: str) -> None:
    raise LabError(message, Status.BLOCKED_CAPABILITY) from None


def _url_spans(text: str):
    """Scan literal URI delimiters once, without retrying a greedy scheme at every byte."""
    cursor = 0
    while True:
        delimiter = text.find("://", cursor)
        if delimiter < 0:
            return
        start = delimiter
        while start > cursor:
            char = text[start - 1]
            if not (char.isascii() and (char.isalnum() or char in "+.-")):
                break
            start -= 1
        while start < delimiter and not (text[start].isascii() and text[start].isalpha()):
            start += 1
        end = delimiter + 3
        while end < len(text) and not (text[end].isspace() or text[end] in '"<>'):
            end += 1
        if start < delimiter and end > delimiter + 3:
            yield start, end
            cursor = end
        else:
            cursor = delimiter + 3


def _json(value: object, *, forbidden: tuple[str, ...] = (), depth: int = 0) -> None:
    if depth > 80:
        _fail("JSON evidence nesting exceeds the safe capture bound")
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is not str:
                _fail("JSON evidence requires string keys")
            if key == "AccountKey" or re.sub(r"[-_\s]", "", key).casefold() in _AUTH_KEYS:
                _fail("Unsafe credential/transport field in evidence")
            _json(key, forbidden=forbidden, depth=depth + 1)
            _json(child, forbidden=forbidden, depth=depth + 1)
        if value.get("payloadType") == "InlineBase64":
            try:
                decoded = base64.b64decode(value["payload"], validate=True).decode("utf-8")
            except (KeyError, TypeError, ValueError, UnicodeError):
                _fail("Unsafe invalid encoded definition part in evidence")
            _json(decoded, forbidden=forbidden, depth=depth + 1)
            if decoded.lstrip().startswith(("{", "[")):
                try:
                    structured = json.loads(decoded)
                except ValueError:
                    _fail("Unsafe malformed structured definition part in evidence")
                _json(structured, forbidden=forbidden, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _json(child, forbidden=forbidden, depth=depth + 1)
    elif type(value) is str:
        if ("." in value and JWT.search(value)) or _AUTH_TEXT.search(value) or any(secret in value for secret in forbidden):
            _fail("Unsafe credential value in evidence")
        for start, end in _url_spans(value):
            url = value[start:end]
            # URL user-info is not business data, even without an auth key.
            try:
                username = urlsplit(url).username
            except ValueError:
                _fail("Unsafe malformed URL in evidence")
            if username is not None:
                _fail("Unsafe credential URL in evidence")
    elif value is None or type(value) in (bool, int):
        return
    elif type(value) is float and math.isfinite(value):
        return
    else:
        _fail("Evidence must be lossless finite JSON; objects are never stringified")


def _http_guard_view(envelope: dict, forbidden: tuple[str, ...]) -> dict:
    if envelope.get("response_kind") != "HTTP_METADATA_RESULT":
        return envelope
    response = envelope["response"]
    for name, value in response.get("original_header_pairs", []):
        _json({name: value}, forbidden=forbidden)
    try:
        raw = base64.b64decode(response["body_base64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != response["body_sha256"]:
            _fail("HTTP metadata original byte hash differs")
        text = raw.decode("utf-8", errors="replace")
    except (KeyError, ValueError, UnicodeError):
        _fail("HTTP metadata original bytes are invalid")
    _json(text, forbidden=forbidden)
    try:
        values = [json.loads(text)]
    except ValueError:
        values = []
        for line in text.splitlines():
            try:
                values.append(json.loads(line))
            except ValueError:
                continue
    for value in values:
        _json(value, forbidden=forbidden)
    # Base64 is opaque encoding, not text to scan again for URLs/JWTs.
    return {**envelope, "response": {**response, "body_base64": "[VALIDATED_HTTP_BYTES]"}}


class PrivateEvidenceConfig(Contract):
    """An explicit private-store approval, not cloud/query authorization."""

    directory: Path
    approval_reference: str = Field(min_length=1)
    expires_at: datetime
    max_capture_bytes: int = Field(gt=0, le=16_000_000)
    max_total_bytes: int = Field(gt=0, le=1_000_000_000)
    max_records: int = Field(gt=0, le=100_000)

    @model_validator(mode="after")
    def bounds(self):
        if self.expires_at.tzinfo is None or self.max_total_bytes < self.max_capture_bytes:
            raise ValueError("Explicit timezone and total retention >= capture size are required")
        return self


@dataclass(frozen=True)
class EvidenceReference:
    capture_id: str
    request_hash: str
    response_hash: str
    derivative_hash: str


def private_identifiers(binding: ReaderBinding) -> tuple[str, ...]:
    return tuple(sorted({
        binding.scope.tenant, binding.scope.principal, binding.scope.authorization_revision,
        str(binding.ontology_workspace_id), str(binding.ontology_item_id),
        *(str(source.workspace_id) for source in binding.sources.values()),
        *(str(source.dataset_id) for source in binding.sources.values()),
    }))


class PrivateEvidenceSink:
    def __init__(
        self, config: PrivateEvidenceConfig, repo: Path, *,
        forbidden_values: tuple[str, ...] = (), now: Callable[[], datetime] | None = None,
    ):
        self.config = PrivateEvidenceConfig.model_validate(config.model_dump())
        if not config.directory.is_absolute():
            _fail("Private evidence directory must be absolute")
        self.repo = repo.resolve()
        self.directory = private_config_path(config.directory, self.repo)
        self._forbidden = forbidden_values
        if any(type(value) is not str or not value for value in forbidden_values):
            _fail("Unsafe credential-echo protection configuration")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._location()

    def _location(self) -> None:
        private_config_path(self.directory, self.repo)
        for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            root = os.environ.get(name)
            if root and self.directory.is_relative_to(Path(root).resolve()):
                _fail("Private evidence must be outside OneDrive sync roots")
        paths = (self.config.directory, *self.config.directory.parents, self.directory, *self.directory.parents)
        for path in paths:
            if path.exists() and (
                path.is_symlink()
                or getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                _fail("Private evidence paths cannot traverse links or reparse points")

    def ready(self) -> None:
        self._location()
        now = self._now()
        if now.tzinfo is None or now >= self.config.expires_at:
            _fail("Private evidence approval expired or clock has no timezone")

    @staticmethod
    def _redact(value: object, identifiers: tuple[str, ...]) -> object:
        def text(original: str) -> str:
            result = original
            for identifier in sorted(set(identifiers), key=lambda item: (-len(item), item)):
                result = result.replace(identifier, f"[id:{digest(identifier)}]")
            result = _UUID.sub(lambda match: f"[id:{digest(match.group().casefold())}]", result)
            pieces, position = [], 0
            for start, end in _url_spans(result):
                pieces += [result[position:start], f"[url:{digest(result[start:end])}]"]
                position = end
            return "".join(pieces) + result[position:]

        if isinstance(value, dict):
            if any(text(key) != key for key in value):
                _fail("Unsafe identifier/URL key cannot be redacted while preserving evidence keys")
            return {key: PrivateEvidenceSink._redact(child, identifiers) for key, child in value.items()}
        if isinstance(value, list):
            return [PrivateEvidenceSink._redact(child, identifiers) for child in value]
        return text(value) if isinstance(value, str) else value

    @staticmethod
    def _bytes(value: dict) -> bytes:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()

    @classmethod
    def _public(cls, envelope: dict) -> dict:
        public_source = envelope
        if envelope.get("response_kind") == "HTTP_METADATA_RESULT":
            public_source = {
                **envelope, "response": {
                    **envelope["response"],
                    "body_base64": "[PRIVATE_HTTP_BODY:" + envelope["response"]["body_sha256"] + "]",
                },
            }
        return {
            "kind": "SANITIZED_SOURCE_EVIDENCE_NOT_RAW_REPLAY", "raw_capture_hash": digest(envelope),
            "request_hash": digest(envelope["request"]), "response_hash": digest(envelope["response"]),
            "evidence": cls._redact(public_source, tuple(envelope["private_identifiers"])),
        }

    def capture(
        self, *, tool: str, source_alias: str, request: dict, response: dict, context: dict,
        private_identifiers: tuple[str, ...], synthetic: bool,
        response_kind: Literal[
            "MCP_TOOL_RESULT", "MCP_PROTOCOL_ERROR", "SDK_RESULT_UNOBTAINABLE", "HTTP_METADATA_RESULT",
            "MCP_INVENTORY_PROJECTION", "HTTP_METADATA_TRANSPORT_FAILURE",
        ] = "MCP_TOOL_RESULT",
        metadata: bool = False,
        native_answer: bool = False,
    ) -> EvidenceReference:
        self.ready()
        expected_tool = (
            "ask_ontology" if native_answer else
            "SourceMetadata" if response_kind in {"HTTP_METADATA_RESULT", "HTTP_METADATA_TRANSPORT_FAILURE"} else "ExecuteQuery"
        )
        allowed = (
            {"MCPInventory"} if response_kind == "MCP_INVENTORY_PROJECTION" else
            {"GetSemanticModelSchema", "list_ontology_entities", "list_ontology_rules"}
        ) if metadata else {expected_tool}
        if tool not in allowed or metadata and (response_kind in {"HTTP_METADATA_RESULT", "HTTP_METADATA_TRANSPORT_FAILURE"} or native_answer) or not re.fullmatch(
            r"[a-z][a-z0-9_]*", source_alias,
        ) or response_kind == "MCP_INVENTORY_PROJECTION" and not metadata:
            _fail("Private source sink accepts only explicit source-query envelopes")
        if native_answer and (
            response_kind not in {"MCP_TOOL_RESULT", "MCP_PROTOCOL_ERROR", "SDK_RESULT_UNOBTAINABLE"}
            or set(request) != {"request"} or type(request["request"]) is not str
            or context.get("kind") != "READ_ONLY_NATIVE_ONTOLOGY_QUESTION"
        ):
            _fail("Native answer evidence requires the exact reviewed question envelope")
        if response_kind not in {
            "MCP_TOOL_RESULT", "MCP_PROTOCOL_ERROR", "SDK_RESULT_UNOBTAINABLE", "HTTP_METADATA_RESULT",
            "MCP_INVENTORY_PROJECTION", "HTTP_METADATA_TRANSPORT_FAILURE",
        }:
            _fail("Unknown source evidence kind")
        if not all(type(value) is dict for value in (request, response, context)):
            _fail("JSON source evidence requires request, response and context objects")
        if any(type(value) is not str or not value for value in private_identifiers):
            _fail("Unsafe private identifier mapping")
        envelope = {
            "version": 1, "owner": _OWNER, "captured_at": self._now().isoformat(),
            "approval_reference": self.config.approval_reference,
            "retention_policy_hash": digest(self.config.model_dump(mode="json")),
            "lane": "SYNTHETIC_TEST" if synthetic else "LIVE_DELEGATED",
            "tool": tool, "source_alias": source_alias, "request": request, "response": response,
            "context": context, "private_identifiers": sorted(set(private_identifiers)),
            "response_kind": response_kind,
        }
        if metadata:
            envelope["capture_purpose"] = "CURRENT_READER_NATIVE_SCHEMA_METADATA_ONLY"
        elif native_answer:
            envelope["capture_purpose"] = "READ_ONLY_NATIVE_ONTOLOGY_QUESTION_NOT_CONTROLLED_APP_COMPARISON"
        _json(_http_guard_view(envelope, self._forbidden), forbidden=self._forbidden)
        # Check the raw bound before constructing its derivative.
        if len(self._bytes(envelope)) > self.config.max_capture_bytes:
            _fail("Private evidence capture size limit exceeded")
        capture_id = digest(envelope)
        public = self._public(envelope)
        _json(public, forbidden=self._forbidden)
        record = {"envelope": envelope, "derivative": public}
        content = self._bytes(record)
        if len(content) > self.config.max_capture_bytes:
            _fail("Private evidence capture size including derivative exceeds limit")
        self._write(capture_id, content)
        return EvidenceReference(capture_id, digest(request), digest(response), digest(public))

    def capture_http_metadata(self, *, source_alias, request, status, header_fields, body: bytes,
                              observed_at, actor_scope, context, private_identifiers, synthetic, resource=None,
                              original_header_pairs=None):
        """Exact response bytes/headers; authentication is never an input."""
        return self.capture(
            tool="SourceMetadata", source_alias=source_alias, request=request,
            response={
                "status": status, "header_fields": header_fields,
                "body_base64": base64.b64encode(body).decode(), "body_sha256": hashlib.sha256(body).hexdigest(),
                "observed_at": observed_at.isoformat(), "actor_scope": actor_scope.model_dump(mode="json"),
                "resource": resource,
                "original_header_pairs": [[name, value] for name, value in
                                          (original_header_pairs or header_fields.items())],
            },
            context=context, private_identifiers=private_identifiers, synthetic=synthetic,
            response_kind="HTTP_METADATA_RESULT",
        )

    def _write(self, key: str, content: bytes) -> None:
        self._location()
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._location()
            lock = self.directory / ".tq005.lock"
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                _fail("Private evidence store busy; no unbounded wait or stale-lock deletion")
            os.close(descriptor)
            try:
                path = self.directory / f"tq005-{key}.json"
                if path.exists():
                    if path.is_symlink() or path.stat().st_nlink != 1 or path.read_bytes() != content:
                        _fail("Private evidence collision or integrity failure")
                    return
                count, total = 0, 0
                for existing in self.directory.iterdir():
                    if not _FILE.fullmatch(existing.name):
                        continue
                    if existing.is_symlink() or not existing.is_file() or existing.stat().st_nlink != 1:
                        _fail("Private evidence integrity failure")
                    count += 1
                    total += existing.stat().st_size
                    if count >= self.config.max_records or total + len(content) > self.config.max_total_bytes:
                        _fail("Private evidence retention capacity exhausted; no automatic eviction")
                if total + len(content) > self.config.max_total_bytes:
                    _fail("Private evidence retention byte capacity exhausted")
                pending = self.directory / f".tq005-{uuid4().hex}.pending"
                try:
                    with pending.open("xb") as handle:
                        os.chmod(pending, 0o600)
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    # Same-directory hard-link publication is atomic and never overwrites.
                    os.link(pending, path)
                finally:
                    if pending.exists():
                        pending.unlink()
            finally:
                lock.unlink()
        except OSError:
            raise LabError("Private evidence write failed; capture is not replay-ready", Status.BLOCKED_CAPABILITY) from None

    def _record_bytes(self, key: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            _fail("Invalid private evidence reference")
        path = self.directory / f"tq005-{key}.json"
        try:
            if path.is_symlink() or path.stat().st_nlink != 1 or path.stat().st_size > self.config.max_capture_bytes:
                _fail("Private evidence integrity failure")
            return path.read_bytes()
        except OSError:
            raise LabError("Private evidence integrity failure", Status.BLOCKED_CAPABILITY) from None

    def _verified_record(self, key: str) -> tuple[dict, str]:
        self._location()
        raw = self._record_bytes(key)
        try:
            record = json.loads(raw)
        except (OSError, ValueError):
            raise LabError("Private evidence integrity failure", Status.BLOCKED_CAPABILITY) from None
        if (
            not isinstance(record, dict) or set(record) != {"envelope", "derivative"}
            or not isinstance(record["envelope"], dict) or not isinstance(record["derivative"], dict)
            or digest(record["envelope"]) != key or record["envelope"].get("owner") != _OWNER
            or not {"request", "response", "private_identifiers"} <= record["envelope"].keys()
        ):
            _fail("Private evidence integrity failure")
        _json({"envelope": _http_guard_view(record["envelope"], self._forbidden),
               "derivative": record["derivative"]}, forbidden=self._forbidden)
        if record["derivative"] != self._public(record["envelope"]):
            _fail("Private evidence derivative integrity failure")
        return record, hashlib.sha256(raw).hexdigest()

    def _record(self, key: str) -> dict:
        return self._verified_record(key)[0]

    def read(self, key: str) -> dict:
        return self._record(key)["envelope"]

    def derivative(self, key: str) -> dict:
        return self._record(key)["derivative"]


class PinnedEvidenceReader:
    """Request-local validated reads; every later check rehashes the original file bytes."""

    def __init__(self, sink: PrivateEvidenceSink):
        self.sink = sink
        self._policy = digest(sink.config.model_dump(mode="json"))
        self._directory = sink.directory
        self._pins: dict[str, str] = {}
        self._sealed = False

    def ready(self) -> None:
        if self.sink.directory != self._directory or digest(self.sink.config.model_dump(mode="json")) != self._policy:
            _fail("Pinned evidence retention contract changed")
        self.sink.ready()

    def read(self, key: str) -> dict:
        if self._sealed:
            _fail("Sealed evidence review cannot acquire new records")
        self.ready()
        record, pin = self.sink._verified_record(key)
        if key in self._pins and self._pins[key] != pin:
            _fail("Original evidence changed during source proof validation")
        self._pins[key] = pin
        return record["envelope"]

    def seal(self) -> None:
        if self._sealed:
            _fail("Evidence review already sealed")
        self.verify()
        self._sealed = True

    def verify(self) -> None:
        self.ready()
        for key, pin in self._pins.items():
            if hashlib.sha256(self.sink._record_bytes(key)).hexdigest() != pin:
                _fail("Pinned original evidence bytes changed; source population cannot be reused")
