"""Narrow renewal-aware Power BI refresh/query adapter. No authentication factory."""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

import httpx

from infra.renewal import (
    Record,
    Renewal,
    body_digest,
    powerbi_path,
    private_path,
    read_json,
    sha256,
    utc,
)
from telco_iq.live_contracts import accepted_refresh_location

FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
POWERBI_RESOURCE = "https://analysis.windows.net/powerbi/api"


class SelectedPrincipal(Record):
    tenantId: str
    subscriptionId: str
    authorUser: str
    azureCliExecutable: str
    azureConfigDirectory: str
    fabricResource: Literal["https://api.fabric.microsoft.com"]
    powerbiResource: Literal["https://analysis.windows.net/powerbi/api"]

    def validate_private(self) -> None:
        for value in (self.tenantId, self.subscriptionId):
            if str(uuid.UUID(value)) != value:
                raise RuntimeError("Explicit canonical principal targets required")
        for value in (self.azureCliExecutable, self.azureConfigDirectory):
            if not Path(value).is_absolute():
                raise RuntimeError("Explicit absolute private CLI/profile selection required")
            private_path(Path(value))
        if not self.authorUser or not self.authorUser.strip():
            raise RuntimeError("Explicit selected principal required")


class HttpClient(Protocol):
    def request(self, method: str, url: str, **kwargs) -> httpx.Response: ...


def validate_principal(lease: Renewal, principal: SelectedPrincipal) -> None:
    principal.validate_private()
    if (principal.tenantId, principal.subscriptionId, principal.authorUser) != (
        lease.approval.tenantId, lease.approval.subscriptionId, lease.approval.authorUser,
    ):
        raise RuntimeError("Selected actual transport principal differs from approval")


def refresh_body(body: dict) -> dict:
    if body == {"notifyOption": "NoNotification"}:
        return body
    # Deliberately bounded enhanced subset: no partial commits or implicit retries.
    if (
        not isinstance(body, dict) or "notifyOption" in body
        or set(body) - {"type", "commitMode", "retryCount", "timeout"}
        or body.get("type") not in {"full", "automatic", "calculate"}
        or body.get("commitMode") != "transactional" or body.get("retryCount") != 0
        or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]", body.get("timeout", ""))
    ):
        raise RuntimeError("Unreviewed ordinary/enhanced refresh body")
    return body


def canonical_refresh_location(
    receipt: dict, workspace: str, dataset: str, *, reviewed_regional_origin: str | None = None,
) -> tuple[str, str]:
    try:
        return accepted_refresh_location(
            receipt, workspace, dataset, reviewed_regional_origin=reviewed_regional_origin,
        )
    except ValueError as error:
        raise RuntimeError(str(error)) from None


def query_rows(response: httpx.Response) -> list[dict]:
    if response.status_code != 200 or len(response.content) > 15_000_000:
        raise RuntimeError("Query response status/byte limit is not complete")
    body = response.json()
    try:
        if set(body) - {"results", "informationProtectionLabel"} or len(body["results"]) != 1:
            raise ValueError
        result = body["results"][0]
        if set(result) != {"tables"} or len(result["tables"]) != 1:
            raise ValueError
        table = result["tables"][0]
        if set(table) != {"rows"} or not isinstance(table["rows"], list):
            raise ValueError
        rows = table["rows"]
        if len(rows) >= 100_000 or sum(len(row) for row in rows) >= 1_000_000:
            raise ValueError
        if any(
            not isinstance(row, dict) or any(
                isinstance(value, (list, dict)) for value in row.values()
            ) for row in rows
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Query errors/partial/unknown structure cannot establish complete readiness") from error
    return rows


class PowerBIRefresh:
    def __init__(
        self, lease: Renewal, client: HttpClient, *, principal: SelectedPrincipal,
        token_resource: str, proof_level: Literal["SYNTHETIC_LOCAL", "AUTHOR_SERVICE"],
        sleep=time.sleep, reviewed_regional_origin: str | None = None,
    ):
        lease.check_current()
        validate_principal(lease, principal)
        if token_resource != POWERBI_RESOURCE:
            raise RuntimeError("Power BI transport token audience mismatch")
        if proof_level not in {"SYNTHETIC_LOCAL", "AUTHOR_SERVICE"}:
            raise RuntimeError("Explicit synthetic/author-service proof level required")
        if reviewed_regional_origin is not None and not re.fullmatch(
            r"https://wabi-[a-z0-9-]+\.analysis\.windows\.net", reviewed_regional_origin,
        ):
            raise RuntimeError("Refresh regional Location requires an exact reviewed HTTPS origin")
        self.lease, self.client, self.principal = lease, client, principal
        self.proof_level, self.sleep = proof_level, sleep
        self.reviewed_regional_origin = reviewed_regional_origin

    def request(self, method: str, path: str, *, body=None) -> tuple[httpx.Response, dict]:
        self.lease.check_current()
        validate_principal(self.lease, self.principal)
        path = powerbi_path(path)
        kwargs = {} if body is None else {"json": body}
        ticket = self.lease.begin(method, path, kwargs, audience="powerbi")
        self.lease.check_current()
        started = datetime.now(timezone.utc).isoformat()
        response = self.client.request(
            method, "https://api.powerbi.com" + path, timeout=45, follow_redirects=False, **kwargs,
        )
        try:
            response_body = response.json() if response.content else None
        except ValueError:
            response_body = {"format": "UNEXPECTED_NON_JSON", "text": response.text[:2000]}
        receipt = {
            "proofLevel": self.proof_level, "approvalSha256": self.lease.digest,
            "method": method, "path": path, "bodySha256": body_digest(body),
            "status": response.status_code, "location": response.headers.get("Location"),
            "requestId": response.headers.get("x-ms-request-id"),
            "retryAfter": response.headers.get("Retry-After"),
            "response": response_body,
            "startedAt": started,
            "observedAtUtc": datetime.now(timezone.utc).isoformat(),
        }
        Renewal._write_record(self.lease.private / ("hero-powerbi-http-" + uuid.uuid4().hex + ".json"), receipt)
        if ticket is not None:
            self.lease.observe(ticket, method, path, response, receipt["response"])
        if response.status_code >= 300:
            raise RuntimeError("Power BI request failed; inspect private receipt, never repeat a submission")
        return response, receipt

    @staticmethod
    def base(workspace: str, dataset: str) -> str:
        for value in (workspace, dataset):
            if str(uuid.UUID(value)) != value:
                raise RuntimeError("Canonical receipt-owned Power BI group/dataset IDs required")
        return f"/v1.0/myorg/groups/{workspace}/datasets/{dataset}/refreshes"

    def submit(self, alias: str, workspace: str, dataset: str, body: dict) -> dict:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", alias):
            raise RuntimeError("Invalid model alias")
        body = refresh_body(body)
        path = self.base(workspace, dataset)
        state_path = self.lease.private / f"hero-{alias}-refresh-accepted.json"
        if state_path.exists():
            raise RuntimeError("Refresh already accepted; monitor exact receipt, never submit again")
        _, prior = self.request("GET", path + "?$top=10")
        history = prior["response"]["value"]
        if any(row.get("status") in {"Unknown", "InProgress"} for row in history):
            raise RuntimeError("Competing active refresh; do not adopt or submit")
        response, receipt = self.request("POST", path, body=body)
        # Preserve even malformed acceptance before validating its identity.
        with state_path.open("x", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2)
        receipt["acceptedRefreshId"] = self.accepted_identity(receipt, workspace, dataset)
        _, receipt["canonicalLocation"] = canonical_refresh_location(
            receipt, workspace, dataset, reviewed_regional_origin=self.reviewed_regional_origin,
        )
        Renewal._write_record(state_path, receipt)
        return receipt

    def accepted_identity(self, receipt: dict, workspace: str, dataset: str) -> str:
        return canonical_refresh_location(
            receipt, workspace, dataset, reviewed_regional_origin=self.reviewed_regional_origin,
        )[0]

    def monitor(
        self, alias: str, workspace: str, dataset: str, *, maximum_polls: int, maximum_seconds: int,
    ) -> dict:
        state_path = self.lease.private / f"hero-{alias}-refresh-accepted.json"
        receipt = read_json(state_path)
        if receipt.get("approvalSha256") != self.lease.digest and not any(
            item.file == state_path.name and item.sha256 == sha256(state_path.read_bytes())
            for item in self.lease.approval.recoveryReceipts
        ):
            raise RuntimeError("Refresh recovery needs its own immutable receipt approval")
        request_id = self.accepted_identity(receipt, workspace, dataset)
        stop = time.monotonic() + maximum_seconds
        delay = receipt.get("retryAfter") or "10"
        for _ in range(maximum_polls):
            self.lease.check_current()
            seconds = int(delay)
            if seconds < 0 or time.monotonic() + seconds >= stop or (
                self.lease.deadline - datetime.now(timezone.utc)
            ).total_seconds() <= seconds:
                raise TimeoutError("Refresh polling exceeds approved budget/window")
            self.sleep(seconds)
            response, current = self.request("GET", self.base(workspace, dataset) + "?$top=10")
            delay = response.headers.get("Retry-After", "10")
            candidates = [row for row in current["response"]["value"] if row.get("requestId") == request_id]
            if len(candidates) > 1:
                raise RuntimeError("Duplicate refresh history identity; no complete proof")
            if not candidates:
                continue
            row = candidates[0]
            if row.get("status") == "Completed" and row.get("endTime") and not row.get("serviceExceptionJson"):
                utc(row["endTime"])
                return current | {"refresh": row}
            if row.get("status") not in {"Unknown", "InProgress"}:
                raise RuntimeError("Exact refresh failed/unknown terminal result; no automatic retry")
        raise TimeoutError("Refresh pending; retain exact receipt and monitor only")
