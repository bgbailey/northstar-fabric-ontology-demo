"""Private, operation-specific Fabric authorization; no login or network in this module.

Parent interface: stage_execution_window(approval_path, private) records a one-shot
UTC execution window. Dedicated capacity requires actual shutdown schedule proof.
Shared capacity instead requires an explicit retain/manual-owner workload policy;
it grants no capacity changes, automated cancellation or job-stop authority.
Historical P0/lifecycle approvals are never edited. See Approval.model_json_schema()
for the executable configuration (the unapproved proposal is deliberately invalid).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parents[1]
HOST = "api.fabric.microsoft.com"
HEX = r"^[0-9a-f]{64}$"
ALIAS = r"^[a-z][a-z0-9-]{0,63}$"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Artifact(Record):
    file: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX)


class Operation(Record):
    name: str = Field(pattern=ALIAS)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1)
    bodySha256: str = Field(pattern=HEX)
    allowPagination: bool = False
    allowLro: bool = False
    allowJobPolling: bool = False
    createsAlias: str | None = Field(default=None, pattern=ALIAS)
    createdDisplayName: str | None = None
    createdType: str | None = None
    audience: Literal["fabric", "powerbi"] = "fabric"


class Ownership(Record):
    authorization: Artifact
    accepted: Artifact
    readback: Artifact
    result: Artifact | None = None


class Window(Record):
    ttlSeconds: int = Field(gt=0, strict=True)
    minimumRemainingSeconds: int = Field(gt=0, strict=True)
    shutdownRetrySeconds: int = Field(gt=0, strict=True)
    latestStartUtc: str
    notAfterUtc: str


class Cost(Record):
    resource: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    unitPrice: float = Field(ge=0, allow_inf_nan=False)
    estimated: float = Field(ge=0, allow_inf_nan=False)
    cap: float = Field(gt=0, allow_inf_nan=False)
    basis: str = Field(min_length=1)


class Shutdown(Record):
    approvalReference: str = Field(min_length=1)
    capacityResourceId: str = Field(min_length=1)
    scheduleResourceId: str = Field(min_length=1)
    runbookName: str = Field(min_length=1)
    runbookSha256: str = Field(pattern=HEX)
    receiptFile: str = Field(min_length=1)


class SharedCapacityWorkload(Record):
    """Manual owner must reconcile accepted jobs/spend by the finite deadline.

    Retain artifacts and leave shared capacity unchanged. No hosted controller or
    cancellation is implemented here; any stop needs separate exact receipt-owned
    job approval, not this workload policy or a local timeout.
    """

    approved: Literal[True]
    approvalReference: str = Field(min_length=1)
    capacityId: str
    capacityResourceId: str = Field(min_length=1)
    notAfterUtc: str
    owner: str = Field(min_length=1)
    artifactDisposition: Literal["RETAIN"]
    enforcement: Literal["MANUAL_OWNER_FINITE_STAGES"]
    jobStopAuthority: Literal["SEPARATE_APPROVAL_RECEIPT_OWNED_JOBS_ONLY"]


class Teardown(Record):
    owner: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    approvalReference: str = Field(min_length=1)
    ownedOnly: Literal[True]


class LroPolling(Record):
    strategy: Literal["RETURNED_LOCATION", "PUBLIC_OPERATION_ID"] = "RETURNED_LOCATION"
    observedLocationOrigin: str | None = Field(default=None, pattern=r"^https://[a-z0-9-]+(?:\.[a-z0-9-]+)*$")


class Approval(Record):
    schemaVersion: Literal["fabric-renewal-v1"]
    approved: Literal[True]
    confirmationReference: str = Field(min_length=1)
    tenantId: str
    subscriptionId: str
    authorUser: str = Field(min_length=1)
    workspaceId: str
    ownedItems: dict[str, str]
    protectedItemIds: list[str]
    artifacts: list[Artifact] = Field(min_length=1)
    artifactSetSha256: str = Field(pattern=HEX)
    recoveryReceipts: list[Artifact] = Field(default_factory=list)
    operations: list[Operation] = Field(min_length=1)
    lroPolling: LroPolling = Field(default_factory=LroPolling)
    cost: Cost
    window: Window
    capacityPolicy: Literal["DEDICATED_CAPACITY", "SHARED_CAPACITY_AS_IS"] = "DEDICATED_CAPACITY"
    shutdown: Shutdown | None = None
    sharedCapacityWorkload: SharedCapacityWorkload | None = None
    teardown: Teardown
    ownershipReceipts: dict[str, Ownership] = Field(default_factory=dict)
    maximumRequests: int = Field(default=300, gt=0, le=10000)


class RecoveryObservation(Record):
    schemaVersion: Literal["fabric-recovery-observation-v1"]
    approvalSha256: str = Field(pattern=HEX)
    acceptedReceipt: Artifact
    acceptedApprovalSha256: str = Field(pattern=HEX)
    method: Literal["GET"]
    path: str
    responseStatus: Literal[200]
    responseBody: dict
    responseBodySha256: str = Field(pattern=HEX)
    location: str | None = None
    operationIdHeader: str | None = None
    resultPath: str
    observedAtUtc: str


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def body_digest(body) -> str:
    """Digest of canonical JSON, not the transport's whitespace/encoding."""
    return sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Invalid or unreadable renewal JSON: {path.name}") from error


def utc(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
            raise ValueError("UTC required")
        return stamp
    except (TypeError, ValueError) as error:
        raise RuntimeError("Renewal timestamps must be absolute UTC") from error


def private_path(path: Path) -> Path:
    path = path.resolve()
    if path.is_relative_to(ROOT) or "onedrive" in str(path).lower():
        raise RuntimeError("Renewal target/state must remain outside source/OneDrive")
    return path


def local_file(private: Path, name: str) -> Path:
    target = (private / name).resolve()
    if not target.is_relative_to(private) or target == private:
        raise RuntimeError("Renewal receipt must be inside its private directory")
    return target


def fabric_path(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != HOST:
            raise RuntimeError("Only the exact Fabric HTTPS host is authorized")
    if (
        not parsed.path.startswith("/v1/") or parsed.fragment
        or "\\" in value or "%" in parsed.path or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in (".", "..", "") for part in parsed.path.split("/")[1:])
    ):
        raise RuntimeError("Noncanonical Fabric request path")
    return parsed.path + ("?" + parsed.query if parsed.query else "")


def powerbi_path(value: str) -> str:
    """Power BI paths are NOT accepted by Author or fabric_path."""
    parsed = urlsplit(value)
    if (parsed.scheme or parsed.netloc) and (
        parsed.scheme != "https" or parsed.netloc != "api.powerbi.com"
    ):
        raise RuntimeError("Only the exact Power BI HTTPS host is authorized")
    if (
        not parsed.path.startswith("/v1.0/myorg/groups/") or parsed.fragment
        or "\\" in value or "%" in parsed.path or value.strip() != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in (".", "..", "") for part in parsed.path.split("/")[1:])
    ):
        raise RuntimeError("Noncanonical Power BI request path")
    return parsed.path + ("?" + parsed.query if parsed.query else "")


def operation_location(record: dict, location: str | None, policy: LroPolling, *, result=False) -> str:
    """Validate evidence origins, never authorize bearer dispatch to those origins."""
    operation = record.get("operationId")
    if not operation:
        raise RuntimeError("LRO operation ID absent; do not resubmit mutation")
    try:
        canonical = str(uuid.UUID(operation)) == operation
    except (ValueError, TypeError, AttributeError):
        canonical = False
    if not canonical:
        raise RuntimeError("Noncanonical accepted operation ID")
    expected = f"/v1/operations/{operation}" + ("/result" if result else "")
    if policy.strategy == "PUBLIC_OPERATION_ID":
        if location is None:
            return expected
        parsed = urlsplit(location)
        if parsed.scheme or parsed.netloc:
            origin = parsed.scheme + "://" + parsed.netloc
            if origin not in {"https://" + HOST, policy.observedLocationOrigin} or location != origin + expected:
                raise RuntimeError("Unapproved LRO evidence origin/path; no regional bearer dispatch")
        elif location != expected:
            raise RuntimeError("LRO Location escaped its exact returned operation ID")
        return expected
    if not location or fabric_path(location) != expected:
        raise RuntimeError("Accepted LRO Location absent or outside its exact operation")
    return expected


def receipt_location(record: dict, policy: LroPolling | None = None) -> str:
    return operation_location(record, record.get("location"), policy or LroPolling())


def operation_result(record: dict, response, body, policy: LroPolling) -> str:
    if (
        response.status_code != 200 or not isinstance(body, dict) or body.get("status") != "Succeeded"
        or any(body.get(key) is not None for key in ("error", "errors", "errorCode"))
        or any(body.get(key) is not None and body.get(key) is not False for key in ("partial", "isPartial"))
        or response.headers.get("x-ms-operation-id", record["operationId"]) != record["operationId"]
    ):
        raise RuntimeError("LRO result requires exact successful operation state/header")
    return operation_location(record, response.headers.get("Location"), policy, result=True)


def verified_ownership(private: Path, spec: Ownership) -> dict:
    """An approved accepted-create chain, never a name/UUID-only adoption."""
    values = {}
    for label in ("authorization", "accepted", "readback", "result"):
        artifact = getattr(spec, label)
        if artifact is None:
            continue
        path = private_path(Path(artifact.file)) if Path(artifact.file).is_absolute() else local_file(
            private, artifact.file,
        )
        if sha256(path.read_bytes()) != artifact.sha256:
            raise RuntimeError("Ownership receipt changed")
        values[label] = read_json(path)
    authorization, accepted, readback = (values[key] for key in ("authorization", "accepted", "readback"))
    try:
        op = next(op for op in authorization["operations"] if op["name"] == accepted["operation"])
        result = values.get("result", accepted)
        item = result["response"]
        item_id = str(uuid.UUID(item["id"]))
        origin = accepted["path"]
        workspace_create = origin == "/v1/workspaces"
        read_path = f"/v1/workspaces/{item_id}" if workspace_create else (
            origin.rsplit("/", 1)[0] + f"/items/{item_id}"
        )
        valid = (
            authorization["approved"] is True
            and body_digest(authorization) == accepted["approvalSha256"]
            and accepted["state"] == "ACCEPTED" and accepted["receiptValidated"] is True
            and accepted["status"] in (201, 202)
            and accepted["method"] == op["method"] == "POST"
            and origin == op["path"]
            and accepted["bodySha256"] == op["bodySha256"]
            and accepted["createdId"] == item_id
            and item["displayName"] == op["createdDisplayName"]
            and (workspace_create or item["type"] == op["createdType"])
            and readback["method"] == "GET" and readback["path"] == read_path
            and readback["status"] == 200 and readback["response"] == item
        )
        if accepted["status"] == 202:
            policy = LroPolling.model_validate(authorization.get("lroPolling", {}))
            valid = valid and result is not accepted and (
                result["method"] == "GET" and result["status"] == 200
                and result["path"] == accepted["resultPath"]
                and accepted["resultPath"] == receipt_location(accepted, policy) + "/result"
            )
            if policy.strategy == "PUBLIC_OPERATION_ID":
                original = checked_receipt(private, Artifact.model_validate(accepted["acceptedReceipt"]))
                observation = RecoveryObservation.model_validate(checked_receipt(
                    private, Artifact.model_validate(accepted["resultObservation"]),
                ))
                valid = valid and (
                    all(accepted.get(key) == value for key, value in original.items())
                    and observation.acceptedReceipt.model_dump() == accepted["acceptedReceipt"]
                    and observation.approvalSha256 == observation.acceptedApprovalSha256 == accepted["approvalSha256"]
                    and observation.path == receipt_location(accepted, policy)
                    and observation.resultPath == accepted["resultPath"]
                    and body_digest(observation.responseBody) == observation.responseBodySha256
                    and operation_result(accepted, httpx_observation(observation), observation.responseBody, policy)
                    == accepted["resultPath"]
                )
        elif result is not accepted:
            valid = False
    except (KeyError, TypeError, ValueError, StopIteration) as error:
        raise RuntimeError("Incomplete accepted-create ownership chain") from error
    if not valid:
        raise RuntimeError("Ownership is not an exact approved accepted-create/readback chain")
    return item


def checked_receipt(private: Path, artifact: Artifact) -> dict:
    path = local_file(private, artifact.file)
    if sha256(path.read_bytes()) != artifact.sha256:
        raise RuntimeError("Immutable accepted operation/observation changed")
    return read_json(path)


def httpx_observation(observation: RecoveryObservation):
    # Only the response fields consumed by operation_result; no HTTP client.
    from types import SimpleNamespace
    return SimpleNamespace(status_code=observation.responseStatus, headers={
        **({"Location": observation.location} if observation.location is not None else {}),
        **({"x-ms-operation-id": observation.operationIdHeader} if observation.operationIdHeader is not None else {}),
    })


def _read_only(method: str, path: str) -> bool:
    return method == "GET" or (
        method == "POST" and re.fullmatch(
            r"/v1.0/myorg/groups/[^/]+/datasets/[^/]+/executeQueries", urlsplit(path).path,
        ) is not None
    ) or (
        method == "POST" and re.fullmatch(
            r"/v1/workspaces/[^/]+/(items|ontologies|semanticModels|notebooks|lakehouses)/[^/]+/getDefinition",
            urlsplit(path).path,
        ) is not None
    )


def _load_approval(path: Path) -> tuple[Approval, str]:
    raw = read_json(path)
    if not isinstance(raw, dict) or raw.get("approved") is not True:
        raise RuntimeError("Separate renewal confirmation absent")
    try:
        approval = Approval.model_validate(raw)
        if approval.lroPolling.strategy == "RETURNED_LOCATION" and approval.lroPolling.observedLocationOrigin is not None:
            raise ValueError("Regional evidence origin requires explicit public operation-ID strategy")
        for value in [
            approval.tenantId, approval.subscriptionId, approval.workspaceId,
            *approval.ownedItems.values(), *approval.protectedItemIds,
        ]:
            if str(uuid.UUID(value)) != value:
                raise ValueError("Canonical UUID required")
        if any(not re.fullmatch(ALIAS, alias) for alias in approval.ownedItems):
            raise ValueError("Invalid owned-item alias")
        if "workspace" in approval.ownedItems:
            raise ValueError("Workspace is a separate exact target, not an owned-item alias")
        if approval.cost.estimated > approval.cost.cap:
            raise ValueError("Cost estimate exceeds cap")
        if approval.window.minimumRemainingSeconds > approval.window.ttlSeconds:
            raise ValueError("Execution TTL is too short")
        if utc(approval.window.latestStartUtc) >= utc(approval.window.notAfterUtc):
            raise ValueError("Latest start must precede the absolute approval bound")
        if len({a.file for a in approval.artifacts}) != len(approval.artifacts):
            raise ValueError("Duplicate artifact")
        if body_digest([a.model_dump() for a in approval.artifacts]) != approval.artifactSetSha256:
            raise ValueError("Approved artifact set digest mismatch")
        shared = approval.sharedCapacityWorkload
        if approval.capacityPolicy == "SHARED_CAPACITY_AS_IS":
            if shared is None or approval.shutdown is not None:
                raise ValueError("Shared capacity requires its explicit workload policy, never a suspension guard")
            if (
                str(uuid.UUID(shared.capacityId)) != shared.capacityId
                or shared.owner != approval.teardown.owner
                or utc(shared.notAfterUtc) != utc(approval.window.notAfterUtc)
                or not approval.ownershipReceipts
            ):
                raise ValueError("Shared workload owner/deadline/receipt-owned target mismatch")
            capacity_resource = shared.capacityResourceId
        else:
            if approval.shutdown is None or shared is not None:
                raise ValueError("Dedicated capacity requires its shutdown guard and no shared-policy bypass")
            capacity_resource = approval.shutdown.capacityResourceId
        if not re.fullmatch(
            rf"/subscriptions/{approval.subscriptionId}/resourceGroups/[^/]+"
            r"/providers/Microsoft\.Fabric/capacities/[^/]+", capacity_resource,
        ):
            raise ValueError("Lifecycle capacity subscription mismatch")
        if approval.cost.resource != capacity_resource:
            raise ValueError("Fabric cost must identify the actual approved capacity")
        if approval.shutdown is not None and not re.fullmatch(
            rf"/subscriptions/{approval.subscriptionId}/resourceGroups/[^/]+"
            r"/providers/Microsoft\.Automation/automationAccounts/[^/]+/schedules/[^/]+",
            approval.shutdown.scheduleResourceId,
        ):
            raise ValueError("Shutdown schedule subscription mismatch")
        creates = [op.createsAlias for op in approval.operations if op.createsAlias]
        if len(creates) != len(set(creates)) or set(creates) & approval.ownedItems.keys():
            raise ValueError("Created aliases must be new and unique")
        if len({op.name for op in approval.operations}) != len(approval.operations):
            raise ValueError("Operation names must be unique")
        for op in approval.operations:
            path = fabric_path(op.path) if op.audience == "fabric" else powerbi_path(op.path)
            prefix = f"/v1/workspaces/{approval.workspaceId}/" if op.audience == "fabric" else (
                f"/v1.0/myorg/groups/{approval.workspaceId}/datasets/"
            )
            if path != op.path or not (
                path.startswith(prefix)
                or (op.audience == "fabric" and op.method == "GET" and path == prefix.rstrip("/"))
                or (op.audience == "fabric" and op.method == "GET" and path == "/v1/capacities"
                    and op.bodySha256 == body_digest(None) and not (
                        op.allowLro or op.allowJobPolling or op.createsAlias
                    ))
            ):
                raise ValueError("Operation outside the exact approved workspace")
            if approval.capacityPolicy == "SHARED_CAPACITY_AS_IS":
                if op.method not in {"GET", "POST"}:
                    raise ValueError("Shared workload retains artifacts; updates/deletes need separate authority")
                if op.audience == "fabric" and op.method == "POST" and not re.fullmatch(
                    re.escape(prefix) + (
                        r"(?:items|semanticModels|"
                        r"(?:notebooks|semanticModels)/\{[a-z][a-z0-9-]*\}/getDefinition\?format=(?:ipynb|TMDL)|"
                        r"items/\{[a-z][a-z0-9-]*\}/jobs/RunNotebook/instances)"
                    ), path,
                ):
                    raise ValueError("Shared workload cannot change capacity/assignment or stop unapproved jobs")
            tokens = re.findall(r"\{([a-z][a-z0-9-]*)\}", path)
            if any(token not in creates for token in tokens):
                raise ValueError("Only receipt-derived create aliases may appear in paths")
            substituted = re.sub(r"\{[a-z][a-z0-9-]*\}", "created-id", path)
            if "{" in substituted or "}" in substituted or any(
                "{" + token + "}" not in path.split("/") for token in tokens
            ):
                raise ValueError("Created aliases must occupy a whole path segment")
            if op.createsAlias and (
                op.audience != "fabric" or op.method != "POST" or not op.createdDisplayName or tokens
                or not re.fullmatch(
                    rf"/v1/workspaces/{approval.workspaceId}/"
                    r"(items|lakehouses|notebooks|semanticModels|ontologies)", path,
                )
            ):
                raise ValueError("Create alias requires an exact item-collection POST and name")
            if op.allowPagination and op.method != "GET":
                raise ValueError("Only GET listing may paginate")
            if op.allowJobPolling and not re.fullmatch(
                rf"/v1/workspaces/{approval.workspaceId}/items/[^/]+/jobs/RunNotebook/instances", path,
            ):
                raise ValueError("Job polling requires an exact notebook submission")
            if op.allowJobPolling and op.method != "POST":
                raise ValueError("Only an accepted POST may grant job polling")
            if op.audience == "powerbi" and (
                op.allowLro or op.allowJobPolling or op.allowPagination or op.createsAlias
                or not re.fullmatch(
                    re.escape(prefix) + r"[^/]+/(refreshes(?:\?\$top=10)?|executeQueries)", path,
                )
                or op.method not in {"GET", "POST"}
                or (op.method == "GET" and not path.endswith("/refreshes?$top=10"))
            ):
                raise ValueError("Power BI scope is limited to exact model refresh/history/query routes")
    except (ValidationError, ValueError, TypeError) as error:
        raise RuntimeError(f"Invalid renewal authorization: {error}") from error
    return approval, body_digest(raw)


def _verify_artifacts(approval: Approval, private: Path) -> None:
    for artifact in approval.artifacts:
        path = Path(artifact.file)
        path = path if path.is_absolute() else local_file(private, artifact.file)
        try:
            actual = sha256(path.read_bytes())
        except OSError as error:
            raise RuntimeError(f"Approved artifact unavailable: {path.name}") from error
        if actual != artifact.sha256:
            raise RuntimeError(f"Approved artifact changed: {path.name}")


def _window_path(private: Path, digest: str) -> Path:
    return private / f"renewal-window-{digest}.json"


def stage_execution_window(approval_path: Path, private: Path, *, now: datetime | None = None) -> dict:
    """LOCAL ONLY. Freeze time once, after confirmation and just before execution.

    This does not create/verify a shutdown schedule, resume a capacity or renew a
    certificate. It cannot slide a deadline on retry; existing windows are rejected.
    """
    private, approval_path = private_path(private), private_path(approval_path)
    approval, digest = _load_approval(approval_path)
    _verify_artifacts(approval, private)
    now = now or datetime.now(timezone.utc)
    utc(now.isoformat())
    deadline = min(now + timedelta(seconds=approval.window.ttlSeconds), utc(approval.window.notAfterUtc))
    if now > utc(approval.window.latestStartUtc) or (
        deadline - now
    ).total_seconds() < approval.window.minimumRemainingSeconds:
        raise RuntimeError("Insufficient approved execution time; fresh approval required")
    value = {
        "approvalSha256": digest, "startedAtUtc": now.isoformat(), "deadlineUtc": deadline.isoformat(),
        "shutdownGuard": (
            "NOT_APPLICABLE_SHARED_MANUAL_OWNER"
            if approval.capacityPolicy == "SHARED_CAPACITY_AS_IS" else "NOT_VERIFIED"
        ), "resume": "NOT_RUN",
    }
    with _window_path(private, digest).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
    return value


class Renewal:
    """Fail-closed request guard. Call begin BEFORE transport; observe AFTER receipt."""

    def __init__(self, private: Path, approval_path: Path):
        self.private = private_path(private)
        self.path = private_path(approval_path)
        self.approval, self.digest = _load_approval(self.path)
        self.ledger = local_file(self.private, "renewal-requests")
        self.window = read_json(_window_path(self.private, self.digest))
        if not isinstance(self.window, dict) or self.window.get("approvalSha256") != self.digest:
            raise RuntimeError("Execution window is bound to another renewal")
        start = utc(self.window["startedAtUtc"])
        deadline = utc(self.window["deadlineUtc"])
        if (
            deadline != min(start + timedelta(seconds=self.approval.window.ttlSeconds),
                            utc(self.approval.window.notAfterUtc))
            or start > utc(self.approval.window.latestStartUtc)
        ):
            raise RuntimeError("Execution window was altered or started too late")
        self.deadline = deadline
        self.protected = set(self.approval.protectedItemIds)
        for alias, expected in {"workspace": self.approval.workspaceId, **self.approval.ownedItems}.items():
            if self.approval.ownershipReceipts:
                spec = self.approval.ownershipReceipts.get(alias)
                if spec is None or verified_ownership(self.private, spec)["id"] != expected:
                    raise RuntimeError(f"Receipt-owned target mismatch: {alias}")
                continue
            receipt = self.private / f"p0-{alias}.json"
            if not receipt.exists() or read_json(receipt).get("id") != expected:
                raise RuntimeError(f"Owned target mismatch or missing receipt: {alias}")
        original = self.private / "p0-ontology.json"
        if original.exists():
            original_id = read_json(original).get("id")
            if original_id not in self.protected:
                raise RuntimeError("The historical ontology must remain explicitly protected")
        self.check_current()
        self.verify_lifecycle_policy()
        self._derived()

    def check_current(self) -> None:
        _, digest = _load_approval(self.path)
        if digest != self.digest:
            raise RuntimeError("Renewal changed; construct a new reviewed context")
        now = datetime.now(timezone.utc)
        if now < utc(self.window["startedAtUtc"]) or now >= self.deadline:
            raise RuntimeError("Renewed execution window expired or not started")
        _verify_artifacts(self.approval, self.private)
        for spec in self.approval.ownershipReceipts.values():
            verified_ownership(self.private, spec)

    def _budget(self) -> None:
        directory = local_file(self.private, "renewal-budget-" + self.digest)
        directory.mkdir(exist_ok=True)
        for number in range(self.approval.maximumRequests):
            try:
                with (directory / f"{number:05}.json").open("x", encoding="utf-8") as handle:
                    json.dump({"reservedAtUtc": datetime.now(timezone.utc).isoformat()}, handle)
                return
            except FileExistsError:
                continue
        raise RuntimeError("Approved request budget exhausted; no dispatch")

    def verify_lifecycle_policy(self) -> dict:
        """Validate authority, not a claim that a manual owner or controller acted."""
        self.check_current()
        if self.approval.capacityPolicy == "DEDICATED_CAPACITY":
            self.verify_shutdown_guard()
            return {"capacityPolicy": "DEDICATED_CAPACITY", "proofLevel": "ACTUAL_SHUTDOWN_READBACK"}
        policy = self.approval.sharedCapacityWorkload
        if policy is None or (
            self.deadline - datetime.now(timezone.utc)
        ).total_seconds() < self.approval.window.minimumRemainingSeconds:
            raise RuntimeError("Shared workload policy/window insufficient; new explicit approval required")
        return {
            "capacityPolicy": "SHARED_CAPACITY_AS_IS",
            "proofLevel": "APPROVED_MANUAL_OBLIGATIONS_NOT_AUTOMATION_PROOF",
            "artifactDisposition": policy.artifactDisposition, "owner": policy.owner,
            "deadlineUtc": self.deadline.isoformat(), "enforcement": policy.enforcement,
            "jobStopAuthority": policy.jobStopAuthority,
            "automaticJobCancellation": "NOT_IMPLEMENTED",
            "capacityChanges": "FORBIDDEN", "jobStopsByThisExecutor": "NONE_AUTHORIZED",
        }

    def verify_shutdown_guard(self) -> None:
        """Validate a fresh actual ARM schedule/binding readback before any use.

        The separate ARM executor must call this again immediately BEFORE resume.
        A local deadline edit or a boolean 'verified' flag is not accepted evidence.
        """
        self.check_current()
        if self.approval.capacityPolicy != "DEDICATED_CAPACITY" or self.approval.shutdown is None:
            raise RuntimeError("Shared workload policy supplies no capacity suspension/resume authority")
        spec = self.approval.shutdown
        evidence = read_json(local_file(self.private, spec.receiptFile))
        try:
            observed = utc(evidence["observedAtUtc"])
            now = datetime.now(timezone.utc)
            schedule, binding = evidence["schedule"], evidence["binding"]
            properties = schedule["properties"]
            parameters = {key.casefold(): value for key, value in binding["properties"]["parameters"].items()}
            expected = {
                "targetresourceid": spec.capacityResourceId,
                "targetsha256": sha256(spec.capacityResourceId.lower().encode()),
                "tenantid": self.approval.tenantId,
                "deadlineutc": self.window["deadlineUtc"], "mode": "SuspendWhenDue",
            }
            valid = (
                utc(self.window["startedAtUtc"]) <= observed <= now
                and (now - observed).total_seconds() <= 300
                and (self.deadline - now).total_seconds() >= self.approval.window.minimumRemainingSeconds
                and evidence["tenantId"] == self.approval.tenantId
                and evidence["subscriptionId"] == self.approval.subscriptionId
                and evidence["runbookSha256"] == spec.runbookSha256
                and evidence["inspect"] == {"status": "Completed", "mutations": 0}
                and schedule["id"] == spec.scheduleResourceId
                and properties["isEnabled"] is True
                and utc(properties["startTime"]) == self.deadline
                and utc(properties["expiryTime"]) == self.deadline + timedelta(
                    seconds=self.approval.window.shutdownRetrySeconds)
                and properties["frequency"] == "Hour" and properties["interval"] == 1
                and parameters == expected
                and len(parameters) == len(binding["properties"]["parameters"])
                and binding["id"].startswith(spec.scheduleResourceId.rsplit("/schedules/", 1)[0]
                                              + "/jobSchedules/")
                and binding["properties"]["schedule"]["name"] == spec.scheduleResourceId.rsplit("/", 1)[1]
                and binding["properties"]["runbook"]["name"] == spec.runbookName
            )
        except (KeyError, TypeError, AttributeError) as error:
            raise RuntimeError("Incomplete shutdown guard readback") from error
        if not valid:
            raise RuntimeError("Shutdown guard readback does not match this execution window")

    def _records(self) -> list[dict]:
        return [
            record for path in sorted(self.ledger.glob("*.json"))
            if (record := read_json(path)).get("approvalSha256") == self.digest
        ]

    def _recovery_receipts(self) -> dict[tuple[str, str], tuple[dict, str]]:
        receipts = {}
        for recovery in self.approval.recoveryReceipts:
            path = local_file(self.private, recovery.file)
            record = read_json(path)
            if sha256(path.read_bytes()) != recovery.sha256:
                raise RuntimeError("Approved recovery receipt changed")
            if record.get("path", "").startswith("/v1.0/myorg/"):
                origin = powerbi_path(record["path"])
                if (
                    record.get("status") != 202 or record.get("method") != "POST"
                    or not origin.startswith(f"/v1.0/myorg/groups/{self.approval.workspaceId}/datasets/")
                    or not origin.endswith("/refreshes") or not record.get("location")
                ):
                    raise RuntimeError("Invalid exact-workspace Power BI refresh recovery receipt")
                # A separate explicit history allowlist is still required. Never
                # turn a Power BI receipt into Fabric token forwarding authority.
                continue
            try:
                origin = fabric_path(record["path"])
                valid = (
                    record["status"] == 202 and record["method"] in {"POST", "PUT", "PATCH", "DELETE"}
                    and origin.startswith(f"/v1/workspaces/{self.approval.workspaceId}/")
                    and re.fullmatch(HEX, record["bodySha256"])
                    and re.fullmatch(HEX, record["approvalSha256"])
                    and record.get("state", "ACCEPTED") == "ACCEPTED"
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError("Incomplete accepted-operation recovery receipt") from error
            if not valid:
                raise RuntimeError("Recovery receipt is not an accepted operation in the exact workspace")
            status_path = receipt_location(record, self.approval.lroPolling)
            if record.get("resultPath"):
                if record["resultPath"] != status_path + "/result":
                    raise RuntimeError("Recovery result escaped the actual accepted operation")
            receipts[(recovery.file, recovery.sha256)] = (record, status_path)
        if self.approval.lroPolling.strategy == "PUBLIC_OPERATION_ID":
            for record in self._records():
                if record.get("state") != "ACCEPTED" or record.get("receiptValidated") is not True:
                    continue
                if not record.get("acceptedReceipt"):
                    continue
                artifact = Artifact.model_validate(record["acceptedReceipt"])
                original = checked_receipt(self.private, artifact)
                operation = next((op for op in self.approval.operations if op.name == record["operation"]), None)
                if (
                    operation is None or not operation.allowLro or original.get("status") != 202
                    or original.get("method") != operation.method
                    or original.get("bodySha256") != operation.bodySha256
                    or not all(record.get(key) == value for key, value in original.items())
                ):
                    raise RuntimeError("Accepted-operation snapshot differs from its original receipt")
                receipts[(artifact.file, artifact.sha256)] = (
                    original, receipt_location(original, self.approval.lroPolling),
                )
        return receipts

    def _recovery_directory(self) -> Path:
        return local_file(self.private, "renewal-recoveries-" + self.digest)

    def _recovery_reads(self, receipts: dict) -> set[str]:
        reads = {status for _, status in receipts.values()}
        for path in self._recovery_directory().glob("*.json"):
            raw = path.read_bytes()
            if path.stem != sha256(raw):
                raise RuntimeError("Corrupt immutable recovery observation")
            try:
                observation = RecoveryObservation.model_validate(read_json(path))
                original, status_path = receipts[
                    (observation.acceptedReceipt.file, observation.acceptedReceipt.sha256)
                ]
                observed = utc(observation.observedAtUtc)
                valid = (
                    observation.approvalSha256 == self.digest
                    and observation.acceptedApprovalSha256 == original["approvalSha256"]
                    and observation.path == status_path
                    and body_digest(observation.responseBody) == observation.responseBodySha256
                    and operation_result(
                        original, httpx_observation(observation), observation.responseBody, self.approval.lroPolling,
                    ) == observation.resultPath == status_path + "/result"
                    and utc(self.window["startedAtUtc"]) <= observed <= datetime.now(timezone.utc) < self.deadline
                )
            except (ValidationError, KeyError, TypeError, ValueError, RuntimeError) as error:
                raise RuntimeError("Invalid approval-bound recovery observation") from error
            if not valid:
                raise RuntimeError("Recovery observation does not match accepted receipt/current scope/outcome")
            reads.add(observation.resultPath)
        return reads

    def _observe_recovery(self, method: str, path: str, response, body) -> None:
        for (file, digest), (original, status_path) in self._recovery_receipts().items():
            if method != "GET" or path != status_path or response.status_code != 200:
                continue
            if not isinstance(body, dict) or body.get("status") != "Succeeded" or body.get("error"):
                continue
            location = response.headers.get("Location")
            if location is None and self.approval.lroPolling.strategy == "RETURNED_LOCATION":
                continue
            result = operation_result(original, response, body, self.approval.lroPolling)
            self.check_current()
            observation = RecoveryObservation(
                schemaVersion="fabric-recovery-observation-v1", approvalSha256=self.digest,
                acceptedReceipt=Artifact(file=file, sha256=digest),
                acceptedApprovalSha256=original["approvalSha256"], method="GET", path=path,
                responseStatus=200, responseBody=body, responseBodySha256=body_digest(body),
                location=location, operationIdHeader=response.headers.get("x-ms-operation-id"),
                resultPath=result, observedAtUtc=datetime.now(timezone.utc).isoformat(),
            )
            self._immutable(self._recovery_directory(), observation.model_dump())

    def _immutable(self, directory: Path, value: dict) -> Artifact:
        raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        directory.mkdir(exist_ok=True)
        target = directory / (sha256(raw) + ".json")
        try:
            with target.open("xb") as handle:
                handle.write(raw)
        except FileExistsError:
            if target.read_bytes() != raw:
                raise RuntimeError("Immutable operation observation changed; never overwrite it")
        return Artifact(file=str(target.relative_to(self.private)), sha256=sha256(raw))

    def _derived(self) -> tuple[dict[str, str], set[str]]:
        aliases = {}
        reads = self._recovery_reads(self._recovery_receipts())
        specs = {op.name: op for op in self.approval.operations}
        for record in self._records():
            op = specs.get(record["operation"])
            if not op or record.get("state") != "ACCEPTED" or record.get("receiptValidated") is not True:
                continue
            if op.allowLro and record.get("operationPath"):
                status_path = receipt_location(record, self.approval.lroPolling)
                reads.add(status_path)
                if self.approval.lroPolling.strategy == "RETURNED_LOCATION" and (
                    record.get("resultPath") == status_path + "/result"
                ):
                    reads.add(record["resultPath"])
            if op.allowJobPolling and record.get("jobPath"):
                reads.add(record["jobPath"])
            if op.createsAlias and record.get("createdId"):
                aliases[op.createsAlias] = record["createdId"]
        return aliases, reads

    def _check_prior_intent(self, method: str, path: str, op: Operation, body, audience: str) -> None:
        for record_path in self.ledger.glob("*.json"):
            record = read_json(record_path)
            if not isinstance(record, dict):
                raise RuntimeError("Invalid historical reservation; reconcile it before mutation")
            if (record.get("method"), record.get("path"), record.get("audience", "fabric")) != (
                method, path, audience,
            ):
                continue
            if op.createsAlias:
                name = body["displayName"]
                item = record.get("createdItem")
                if (
                    record.get("state") == "ACCEPTED" and record.get("receiptValidated") is True
                    and record.get("readbackVerified") is True and isinstance(item, dict)
                    and item.get("id") == record.get("createdId") and item.get("displayName") != name
                ):
                    continue
                recorded_name = record.get("createName")
                if record.get("reservationVersion") == 2 and recorded_name and record_path.stem == body_digest({
                    "method": method, "path": path, "createName": recorded_name,
                }) and recorded_name != name:
                    continue
            # Baseline keys included bodySha256 and omitted createName. Scan
            # their immutable records across ALL approvals, including changed
            # bodies: an unknown collection create cannot prove a different name.
            raise RuntimeError("Mutation already attempted/reserved in historical ledger; reconcile, never resubmit")

    def begin(self, method: str, path: str, kwargs: dict, *, audience: str = "fabric") -> dict | None:
        self.check_current()
        if set(kwargs) - {"json", "timeout"}:
            raise RuntimeError("Renewal permits only the approved JSON body and a transport timeout")
        if audience not in {"fabric", "powerbi"}:
            raise RuntimeError("Unreviewed token audience")
        path = fabric_path(path) if audience == "fabric" else powerbi_path(path)
        if "{" in path or "}" in path:
            raise RuntimeError("Unresolved target aliases may not be sent to Fabric")
        digest = body_digest(kwargs.get("json"))
        aliases, reads = self._derived()
        if audience == "fabric" and method == "GET" and path in reads and digest == body_digest(None):
            self._budget()
            return None
        matches = []
        for op in self.approval.operations:
            if op.audience != audience:
                continue
            expected = op.path
            for alias, item_id in aliases.items():
                expected = expected.replace("{" + alias + "}", item_id)
            actual = path
            if op.allowPagination:
                parsed = urlsplit(path)
                pairs = parse_qsl(parsed.query, keep_blank_values=True)
                cursors = [value for key, value in pairs if key == "continuationToken"]
                if len(cursors) > 1 or (cursors and not cursors[0]):
                    raise RuntimeError("Invalid continuation token")
                query = urlencode([(key, value) for key, value in pairs if key != "continuationToken"])
                actual = parsed.path + ("?" + query if query else "")
            if (op.method, expected, op.bodySha256) == (method, actual, digest):
                matches.append(op)
        if len(matches) != 1:
            raise RuntimeError("Request is outside the renewed method/path/body allowlist")
        op = matches[0]
        mutation = not _read_only(method, path)
        if mutation and self.protected.intersection(part.lower() for part in urlsplit(path).path.split("/")):
            raise RuntimeError("Renewal cannot mutate the protected historical ontology/item")
        if op.createsAlias:
            body = kwargs.get("json")
            if not isinstance(body, dict) or body.get("displayName") != op.createdDisplayName:
                raise RuntimeError("Created target name differs from approval")
        if mutation:
            for alias in re.findall(r"\{([a-z][a-z0-9-]*)\}", op.path):
                candidates = [record for record in self._records() if (
                    record.get("createdId") == aliases.get(alias)
                    and record.get("readbackVerified") is True
                )]
                if len(candidates) != 1:
                    raise RuntimeError("Created mutation target needs its exact accepted item readback first")
        if not mutation and not op.allowLro:
            self._budget()
            return None
        if mutation:
            self._check_prior_intent(method, path, op, kwargs.get("json"), audience)
        self.ledger.mkdir(exist_ok=True)
        # The same create name/job/refresh stays reserved across changed approvals
        # and body digests. A new window is not permission to repeat an unknown POST.
        key = body_digest({
            "method": method, "path": path,
            "createName": kwargs.get("json", {}).get("displayName") if op.createsAlias else None,
        })
        if not mutation:
            key = uuid.uuid4().hex
        ticket = {
            "approvalSha256": self.digest, "operation": op.name, "method": method, "path": path,
            "bodySha256": digest, "state": "RESERVED_OUTCOME_UNKNOWN",
            "audience": audience,
            "reservationVersion": 2,
            "createName": kwargs.get("json", {}).get("displayName") if op.createsAlias else None,
            "startedAtUtc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with (self.ledger / f"{key}.json").open("x", encoding="utf-8") as handle:
                json.dump(ticket, handle, indent=2)
                handle.write("\n")
        except FileExistsError as error:
            raise RuntimeError("Mutation already attempted; inspect its receipt/LRO, never resubmit") from error
        self._budget()
        return ticket | {"key": key}

    def observe(self, ticket: dict | None, method: str, path: str, response, body) -> None:
        if ticket is None:
            # Only the result of a previously accepted, approved create may bind a new ID.
            result_path = fabric_path(path)
            self._observe_recovery(method, result_path, response, body)
            for record_path in self.ledger.glob("*.json"):
                record = read_json(record_path)
                if (
                    record.get("approvalSha256") == self.digest and record.get("state") == "ACCEPTED"
                    and method == "GET" and response.status_code == 200
                ):
                    created_id = record.get("createdId")
                    item_path = f"/v1/workspaces/{self.approval.workspaceId}/items/{created_id}"
                    if result_path == item_path and created_id:
                        if body != record.get("createdItem"):
                            raise RuntimeError("Created item readback differs from its exact accepted result")
                        record["readbackVerified"] = True
                        self._write_record(record_path, record)
                    elif (result_path == record.get("operationPath") and isinstance(body, dict)
                            and body.get("status") == "Succeeded"
                            and self.approval.lroPolling.strategy == "RETURNED_LOCATION"):
                        location = response.headers.get("Location")
                        if location:
                            result = operation_result(record, response, body, self.approval.lroPolling)
                            record["resultPath"] = result
                            self._write_record(record_path, record)
                    elif result_path == record.get("resultPath") or (
                        self.approval.lroPolling.strategy == "PUBLIC_OPERATION_ID"
                        and record.get("acceptedReceipt")
                        and result_path == record.get("operationPath", "") + "/result"
                        and result_path in self._recovery_reads(self._recovery_receipts())
                    ):
                        if response.headers.get("x-ms-operation-id", record["operationId"]) != record["operationId"]:
                            raise RuntimeError("Result response operation header mismatch")
                        if self.approval.lroPolling.strategy == "PUBLIC_OPERATION_ID":
                            for observed in self._recovery_directory().glob("*.json"):
                                value = read_json(observed)
                                if value["acceptedReceipt"] == record["acceptedReceipt"]:
                                    record["resultObservation"] = {
                                        "file": str(observed.relative_to(self.private)), "sha256": sha256(observed.read_bytes()),
                                    }
                                    break
                            record["resultPath"] = result_path
                        self._bind_created(record, body)
                        self._write_record(record_path, record)
            return
        record = {key: value for key, value in ticket.items() if key != "key"}
        record["status"] = response.status_code
        record["state"] = "ACCEPTED" if 200 <= response.status_code < 300 else "REJECTED"
        record["location"] = response.headers.get("Location")
        record["retryAfter"] = response.headers.get("Retry-After")
        record["requestId"] = response.headers.get("x-ms-request-id")
        record["operationId"] = response.headers.get("x-ms-operation-id")
        record["response"] = body
        # Persist acceptance BEFORE parsing returned locations/IDs or polling.
        self._write_record(self.ledger / f"{ticket['key']}.json", record)
        operation_id = response.headers.get("x-ms-operation-id")
        if operation_id:
            try:
                if str(uuid.UUID(operation_id)) != operation_id:
                    raise ValueError("Noncanonical operation ID")
            except ValueError as error:
                raise RuntimeError("Invalid accepted operation ID; do not resubmit") from error
        location = response.headers.get("Location")
        op = next(op for op in self.approval.operations if op.name == record["operation"])
        if op.allowLro and response.status_code == 202:
            record["operationPath"] = receipt_location(record, self.approval.lroPolling)
        if op.allowJobPolling and location and record["state"] == "ACCEPTED":
            job_path = fabric_path(location)
            prefix = urlsplit(record["path"]).path.rsplit("/jobs/", 1)[0] + "/jobs/instances/"
            if not job_path.startswith(prefix) or "/" in job_path[len(prefix):]:
                raise RuntimeError("Job location escaped the approved notebook")
            uuid.UUID(job_path[len(prefix):])
            record["jobPath"] = job_path
        if response.status_code in (200, 201):
            self._bind_created(record, body)
        record["receiptValidated"] = True
        if op.allowLro and response.status_code == 202 and self.approval.lroPolling.strategy == "PUBLIC_OPERATION_ID":
            record["acceptedReceipt"] = self._immutable(
                local_file(self.private, "renewal-lro-accepted"), record.copy(),
            ).model_dump()
        self._write_record(self.ledger / f"{ticket['key']}.json", record)

    def _bind_created(self, record: dict, body) -> None:
        op = next(op for op in self.approval.operations if op.name == record["operation"])
        if not op.createsAlias:
            return
        if not isinstance(body, dict) or body.get("displayName") != op.createdDisplayName:
            raise RuntimeError("Create readback has no matching owned identity; do not resubmit")
        try:
            item_id = str(uuid.UUID(body["id"]))
        except (KeyError, ValueError, TypeError) as error:
            raise RuntimeError("Create readback ID is invalid; do not resubmit") from error
        if (
            item_id in self.protected or item_id in self.approval.ownedItems.values()
            or item_id == self.approval.workspaceId
            or any(other.get("createdId") == item_id and other["operation"] != record["operation"]
                   for other in self._records())
        ):
            raise RuntimeError("Create returned an existing/protected item; no ownership granted")
        if record.get("createdId", item_id) != item_id:
            raise RuntimeError("Accepted create identity changed")
        record["createdId"] = item_id
        if op.createdType and body.get("type") != op.createdType:
            raise RuntimeError("Create returned a different item type; no ownership granted")
        if body.get("workspaceId", self.approval.workspaceId) != self.approval.workspaceId:
            raise RuntimeError("Create escaped the approved workspace")
        record["createdItem"] = body

    @staticmethod
    def _write_record(path: Path, record: dict) -> None:
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, allow_nan=False)
            handle.write("\n")
        temporary.replace(path)
