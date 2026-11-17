from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

from .models import DeployError, DeployOptions, canonical_id, deployment_id, looks_like_id

FABRIC_ORIGIN = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = "https://api.fabric.microsoft.com"
POWERBI_ORIGIN = "https://api.powerbi.com"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api"
SEMANTIC_SKILL = "semantic-model-authoring"
SPARK_SKILL = "spark-cli"


@dataclass(frozen=True)
class ApiReceipt:
    state: Literal["accepted", "completed"]
    status: int
    body: dict[str, Any] | None = None
    location: str | None = None
    operation_id: str | None = None
    retry_after: str | None = None
    skill: str = SPARK_SKILL

    def journal_value(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "status": self.status,
            "body": self.body,
            "location": self.location,
            "operationId": self.operation_id,
            "retryAfter": self.retry_after,
            "skill": self.skill,
        }

    @classmethod
    def from_journal(cls, value: dict[str, Any]) -> ApiReceipt:
        state = value["state"]
        if state not in {"accepted", "completed"}:
            raise DeployError("Cannot resume an unaccepted operation")
        return cls(
            state=state,
            status=int(value["status"]),
            body=value.get("body"),
            location=value.get("location"),
            operation_id=value.get("operationId"),
            retry_after=value.get("retryAfter"),
            skill=value.get("skill", SPARK_SKILL),
        )


class FabricClient(Protocol):
    tenant_id: str
    user_name: str
    subscription_id: str | None

    def resolve_workspace(self, selector: str) -> dict[str, Any]: ...

    def list_items(self, workspace_id: str, item_type: str) -> list[dict[str, Any]]: ...

    def get_item(
        self,
        workspace_id: str,
        item_id: str,
        *,
        skill: str = SPARK_SKILL,
    ) -> dict[str, Any]: ...

    def submit_item(
        self,
        workspace_id: str,
        body: dict[str, Any],
        *,
        item_type: str,
        skill: str,
    ) -> ApiReceipt: ...

    def poll_lro(self, receipt: ApiReceipt, *, timeout_seconds: int) -> dict[str, Any]: ...

    def get_definition(
        self,
        workspace_id: str,
        item_id: str,
        *,
        item_type: str,
        definition_format: str,
        timeout_seconds: int,
    ) -> dict[str, Any]: ...

    def list_jobs(
        self,
        workspace_id: str,
        item_id: str,
        *,
        skill: str = SPARK_SKILL,
    ) -> list[dict[str, Any]]: ...

    def submit_job(
        self,
        workspace_id: str,
        item_id: str,
        *,
        job_type: str,
        skill: str,
    ) -> ApiReceipt: ...

    def refresh_model(self, workspace_id: str, item_id: str) -> ApiReceipt: ...

    def poll_job(self, receipt: ApiReceipt, *, timeout_seconds: int) -> dict[str, Any]: ...

    def verify_item(
        self,
        workspace_id: str,
        item: dict[str, Any],
        *,
        expected_name: str,
        expected_type: str,
        expected_marker: str,
    ) -> dict[str, Any]: ...


class SubmissionRejectedError(DeployError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class SubmissionBlockedError(DeployError):
    """A read-only preflight found an active operation; no submission occurred."""


class AzureCliFabricClient:
    def __init__(self, options: DeployOptions):
        executable = options.azure_cli or _which_az()
        self.az = str(executable)
        self.timeout = options.poll_timeout_seconds
        self.expected_tenant = options.tenant_id
        self.expected_user = options.expected_user
        self.expected_subscription = getattr(options, "subscription_id", None)
        self.run_id = deployment_id(options)
        account = self._require_existing_account()
        actual_user = account.get("user")
        if not isinstance(actual_user, dict) or actual_user.get("type") != "user":
            self._fail_sign_in_context("the selected identity is not an interactive user")
        if not isinstance(account.get("tenantId"), str):
            self._fail_sign_in_context("the active tenant cannot be verified")
        self.tenant_id = canonical_id(account["tenantId"], "actual Azure CLI tenant")
        if not isinstance(actual_user.get("name"), str) or not actual_user["name"]:
            self._fail_sign_in_context("the active user cannot be verified")
        self.user_name = actual_user["name"]
        if self.expected_tenant and self.tenant_id != self.expected_tenant:
            self._fail_sign_in_context("the active tenant differs from the expected tenant")
        if self.expected_user and self.user_name.casefold() != self.expected_user.casefold():
            self._fail_sign_in_context("the active user differs from the expected user")
        self.subscription_id = account.get("id")
        if self.subscription_id is not None:
            if not isinstance(self.subscription_id, str):
                self._fail_sign_in_context("the active subscription cannot be verified")
            self.subscription_id = canonical_id(
                self.subscription_id,
                "actual Azure CLI subscription",
            )
        if self.expected_subscription and self.subscription_id != self.expected_subscription:
            self._fail_sign_in_context("the active subscription differs from the expected subscription")
        self._token: str | None = None
        self._expires: datetime | None = None
        self._powerbi_token: str | None = None
        self._powerbi_expires: datetime | None = None
        self._client = httpx.Client(
            base_url=FABRIC_ORIGIN,
            timeout=90,
            follow_redirects=False,
            trust_env=True,
        )
        self._powerbi_client = httpx.Client(
            base_url=POWERBI_ORIGIN,
            timeout=90,
            follow_redirects=False,
            trust_env=True,
        )

    def close(self) -> None:
        self._client.close()
        self._powerbi_client.close()

    def _require_existing_account(self) -> dict[str, Any]:
        account = subprocess.run(
            [self.az, "account", "show", "--output", "json", "--only-show-errors"],
            capture_output=True,
            text=True,
            check=False,
        )
        if account.returncode:
            self._fail_sign_in_context("no active Azure CLI account is available")
        try:
            value = json.loads(account.stdout)
        except json.JSONDecodeError:
            self._fail_sign_in_context("the active Azure CLI account could not be verified")
        if not isinstance(value, dict):
            self._fail_sign_in_context("the active Azure CLI account response is incomplete")
        return value

    def _fail_sign_in_context(self, reason: str) -> None:
        tenant = self.expected_tenant or "<tenant>"
        user = self.expected_user or "<expected UPN>"
        subscription = (
            f" and subscription {self.expected_subscription}"
            if self.expected_subscription
            else ""
        )
        label = (
            f"Scout | M5-01 deployment / run {self.run_id} | Microsoft Fabric | "
            f"{tenant} | {user}"
        )
        raise DeployError(
            f"{reason}; no sign-in was attempted. In a persistently labelled compliant "
            f"browser context titled `{label}`, sign in as {user} in tenant {tenant}"
            f"{subscription}, verify the actual user, tenant, and subscription with "
            "`az account show`, then rerun. Unmanaged or silent device-code sign-in is not allowed."
        )

    def _az_json(self, *arguments: str) -> dict[str, Any]:
        result = subprocess.run(
            [self.az, *arguments, "--output", "json", "--only-show-errors"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise DeployError("Azure CLI read failed; no authentication fallback was attempted")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DeployError("Azure CLI returned non-JSON output") from error
        if not isinstance(value, dict):
            raise DeployError("Azure CLI returned an unexpected response")
        return value

    def _ensure_token(self, resource: str) -> tuple[str, datetime]:
        now = datetime.now(timezone.utc)
        if resource == FABRIC_SCOPE:
            cached_token, cached_expiry = self._token, self._expires
        elif resource == POWERBI_SCOPE:
            cached_token, cached_expiry = self._powerbi_token, self._powerbi_expires
        else:
            raise DeployError("Unreviewed token audience")
        if cached_token and cached_expiry and cached_expiry.timestamp() - now.timestamp() > 120:
            return cached_token, cached_expiry
        token = self._az_json(
            "account",
            "get-access-token",
            "--resource",
            resource,
            "--tenant",
            self.tenant_id,
        )
        raw = token.get("accessToken")
        expires = token.get("expiresOn") or token.get("expires_on")
        if not isinstance(raw, str) or not raw:
            raise DeployError(f"Azure CLI did not return an access token for {resource}")
        try:
            parsed = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError as error:
            raise DeployError("Azure CLI returned an invalid token expiration") from error
        if resource == FABRIC_SCOPE:
            self._token, self._expires = raw, parsed
        else:
            self._powerbi_token, self._powerbi_expires = raw, parsed
        return raw, parsed

    def _absolute_url(self, path: str) -> str:
        if path.startswith(FABRIC_ORIGIN + "/"):
            value = path
        elif path.startswith("/"):
            value = FABRIC_ORIGIN + path
        else:
            raise DeployError("Fabric API path must be absolute or origin-relative")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc != "api.fabric.microsoft.com" or parsed.fragment:
            raise DeployError("Refusing an unreviewed Fabric operation URL")
        return value

    def _request(
        self,
        method: str,
        path: str,
        *,
        skill: str,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        token, _ = self._ensure_token(FABRIC_SCOPE)
        headers = {
            "Authorization": "Bearer " + token,
            "x-ms-fabric-skill": skill,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self._client.request(
            method,
            self._absolute_url(path),
            headers=headers,
            json=body,
        )

    def _powerbi_request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        token, _ = self._ensure_token(POWERBI_SCOPE)
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self._powerbi_client.request(
            method,
            self._absolute_powerbi_url(path),
            headers=headers,
            json=body,
        )

    @staticmethod
    def _absolute_powerbi_url(path: str) -> str:
        if path.startswith(POWERBI_ORIGIN + "/"):
            value = path
        elif path.startswith("/"):
            value = POWERBI_ORIGIN + path
        else:
            raise DeployError("Power BI API path must be absolute or origin-relative")
        parsed = urlsplit(value)
        allowed_regional = bool(
            re.fullmatch(r"wabi-[a-z0-9-]+\.analysis\.windows\.net", parsed.netloc),
        )
        if (
            parsed.scheme != "https"
            or (parsed.netloc != "api.powerbi.com" and not allowed_regional)
            or parsed.fragment
        ):
            raise DeployError("Refusing an unreviewed Power BI operation URL")
        return value

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json() if response.content else {}
        except ValueError as error:
            raise DeployError("Fabric API returned non-JSON") from error
        if not isinstance(value, dict):
            raise DeployError("Fabric API returned an unexpected non-object body")
        return value

    def _read(self, path: str, *, skill: str) -> dict[str, Any]:
        response = self._request("GET", path, skill=skill)
        if response.status_code != 200:
            raise DeployError(f"Fabric GET failed with HTTP {response.status_code}")
        return self._json(response)

    def resolve_workspace(self, selector: str) -> dict[str, Any]:
        if looks_like_id(selector):
            workspace = self._read(f"/v1/workspaces/{selector}", skill=SPARK_SKILL)
            if workspace.get("id") != selector:
                raise DeployError("Workspace direct readback ID differs")
            return workspace
        matches = [
            item
            for item in self._listing("/v1/workspaces", skill=SPARK_SKILL)
            if item.get("displayName") == selector
        ]
        if len(matches) != 1:
            raise DeployError(f"Workspace selector must resolve exactly once, found {len(matches)}")
        return matches[0]

    def _listing(self, path: str, *, skill: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(100):
            target = path
            if cursor:
                parsed = urlsplit(path)
                query = parse_qs(parsed.query)
                query["continuationToken"] = [cursor]
                target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), ""))
            page = self._read(target, skill=skill)
            values = page.get("value")
            if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
                raise DeployError("Fabric list response is incomplete")
            result.extend(values)
            cursor = page.get("continuationToken")
            if not cursor:
                return result
            if not isinstance(cursor, str) or cursor in seen:
                raise DeployError("Fabric continuation token is invalid or repeated")
            seen.add(cursor)
        raise DeployError("Fabric pagination budget exceeded")

    def list_items(self, workspace_id: str, item_type: str) -> list[dict[str, Any]]:
        path = f"/v1/workspaces/{workspace_id}/items?type={item_type}"
        return self._listing(path, skill=_skill_for_item(item_type))

    def get_item(
        self,
        workspace_id: str,
        item_id: str,
        *,
        skill: str = SPARK_SKILL,
    ) -> dict[str, Any]:
        return self._read(f"/v1/workspaces/{workspace_id}/items/{item_id}", skill=skill)

    def submit_item(
        self,
        workspace_id: str,
        body: dict[str, Any],
        *,
        item_type: str,
        skill: str,
    ) -> ApiReceipt:
        endpoint = "semanticModels" if item_type == "SemanticModel" else "items"
        response = self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/{endpoint}",
            skill=skill,
            body=body,
        )
        if response.status_code in (200, 201):
            return ApiReceipt("completed", response.status_code, self._json(response), skill=skill)
        if response.status_code == 202:
            location = response.headers.get("Location")
            if not location:
                raise DeployError("Fabric accepted item creation without an operation location")
            return ApiReceipt(
                "accepted",
                202,
                location=location,
                operation_id=response.headers.get("x-ms-operation-id"),
                retry_after=response.headers.get("Retry-After"),
                skill=skill,
            )
        if 400 <= response.status_code < 500:
            raise SubmissionRejectedError(
                f"Fabric item creation rejected with HTTP {response.status_code}",
                response.status_code,
            )
        raise DeployError(f"Fabric item creation returned unknown HTTP {response.status_code}")

    def poll_lro(self, receipt: ApiReceipt, *, timeout_seconds: int) -> dict[str, Any]:
        if not receipt.location:
            raise DeployError("Cannot poll a Fabric operation without its accepted Location")
        deadline = time.monotonic() + timeout_seconds
        path = receipt.location
        while time.monotonic() < deadline:
            response = self._request("GET", path, skill=receipt.skill)
            state = self._json(response)
            observed_operation = response.headers.get("x-ms-operation-id")
            if receipt.operation_id and observed_operation and observed_operation != receipt.operation_id:
                raise DeployError("Fabric LRO operation identity changed during polling")
            if response.status_code != 200 or state.get("status") not in {
                "NotStarted",
                "Running",
                "Succeeded",
                "Failed",
                "Cancelled",
            }:
                raise DeployError("Fabric LRO returned an unknown state")
            if state["status"] in {"Failed", "Cancelled"}:
                raise DeployError("Fabric LRO failed or was cancelled")
            if state["status"] == "Succeeded":
                result_path = response.headers.get("Location") or _operation_result(path)
                result = self._request("GET", result_path, skill=receipt.skill)
                if result.status_code != 200:
                    raise DeployError("Fabric LRO succeeded without a complete result")
                return self._json(result)
            _sleep_retry(response.headers.get("Retry-After") or receipt.retry_after or "5", deadline)
        raise DeployError("Fabric LRO exceeded the deployment timeout")

    def get_definition(
        self,
        workspace_id: str,
        item_id: str,
        *,
        item_type: str,
        definition_format: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        path = (
            f"/v1/workspaces/{workspace_id}/{_definition_collection(item_type)}/{item_id}"
            f"/getDefinition?format={definition_format}"
        )
        response = self._request("POST", path, skill=_skill_for_item(item_type), body={})
        if response.status_code == 200:
            return self._json(response)
        if response.status_code == 202:
            receipt = ApiReceipt(
                "accepted",
                202,
                location=response.headers.get("Location"),
                operation_id=response.headers.get("x-ms-operation-id"),
                retry_after=response.headers.get("Retry-After"),
                skill=_skill_for_item(item_type),
            )
            return self.poll_lro(receipt, timeout_seconds=timeout_seconds)
        raise DeployError("Fabric getDefinition failed before readback completed")

    def list_jobs(
        self,
        workspace_id: str,
        item_id: str,
        *,
        skill: str = SPARK_SKILL,
    ) -> list[dict[str, Any]]:
        return self._listing(
            f"/v1/workspaces/{workspace_id}/items/{item_id}/jobs/instances",
            skill=skill,
        )

    def submit_job(
        self,
        workspace_id: str,
        item_id: str,
        *,
        job_type: str,
        skill: str,
    ) -> ApiReceipt:
        path = (
            f"/v1/workspaces/{workspace_id}/items/{item_id}/jobs/instances"
            f"?{urlencode({'jobType': job_type})}"
        )
        response = self._request("POST", path, skill=skill)
        if 400 <= response.status_code < 500:
            raise SubmissionRejectedError(
                f"Fabric {job_type} submission rejected with HTTP {response.status_code}",
                response.status_code,
            )
        if response.status_code != 202 or not response.headers.get("Location"):
            raise DeployError(f"Fabric {job_type} returned an unknown submission outcome")
        return ApiReceipt(
            "accepted",
            202,
            location=response.headers["Location"],
            operation_id=response.headers.get("x-ms-operation-id"),
            retry_after=response.headers.get("Retry-After"),
            skill=skill,
        )

    def refresh_model(self, workspace_id: str, item_id: str) -> ApiReceipt:
        path = (
            f"/v1.0/myorg/groups/{workspace_id}/datasets/{item_id}/refreshes"
            "?$top=10"
        )
        history = self._powerbi_request("GET", path)
        if history.status_code != 200:
            if 400 <= history.status_code < 500 and history.status_code not in {408, 429}:
                raise SubmissionRejectedError(
                    f"Power BI refresh history rejected with HTTP {history.status_code}",
                    history.status_code,
                )
            raise SubmissionBlockedError(
                f"Power BI refresh history returned HTTP {history.status_code}; no POST occurred",
            )
        history_body = self._json(history)
        entries = history_body.get("value")
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise DeployError("Power BI refresh history is incomplete")
        active = [entry for entry in entries if entry.get("status") in {
            "Unknown",
            "InProgress",
            "NotStarted",
        }]
        if active:
            raise SubmissionBlockedError(
                "Power BI reports an active or unknown refresh; no duplicate POST is allowed",
            )
        post_path = f"/v1.0/myorg/groups/{workspace_id}/datasets/{item_id}/refreshes"
        response = self._powerbi_request(
            "POST",
            post_path,
            body={"notifyOption": "NoNotification"},
        )
        if 400 <= response.status_code < 500:
            raise SubmissionRejectedError(
                f"Power BI refresh rejected with HTTP {response.status_code}",
                response.status_code,
            )
        location = response.headers.get("Location")
        request_id = response.headers.get("x-ms-request-id")
        if response.status_code != 202 or not location or not request_id:
            raise DeployError("Power BI refresh returned an ambiguous submission outcome")
        accepted_path = urlsplit(self._absolute_powerbi_url(location)).path.rstrip("/")
        if accepted_path.split("/")[-1] != request_id:
            raise DeployError("Power BI refresh Location and request ID differ")
        return ApiReceipt(
            "accepted",
            202,
            location=location,
            operation_id=request_id,
            retry_after=response.headers.get("Retry-After"),
            skill=SEMANTIC_SKILL,
        )

    def poll_job(self, receipt: ApiReceipt, *, timeout_seconds: int) -> dict[str, Any]:
        if not receipt.location:
            raise DeployError("Cannot poll a job without its accepted Location")
        if _is_powerbi_location(receipt.location):
            return self._poll_powerbi_refresh(receipt, timeout_seconds=timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = self._request("GET", receipt.location, skill=receipt.skill)
            state = self._json(response)
            observed_operation = response.headers.get("x-ms-operation-id")
            if receipt.operation_id and observed_operation and observed_operation != receipt.operation_id:
                raise DeployError("Fabric job operation identity changed during polling")
            status = state.get("status")
            if response.status_code != 200 or status not in {
                "NotStarted",
                "InProgress",
                "Completed",
                "Failed",
                "Cancelled",
                "Deduped",
            }:
                raise DeployError("Fabric job returned an unknown state")
            if status in {"Failed", "Cancelled", "Deduped"}:
                raise DeployError(f"Fabric job ended without success: {status}")
            if status == "Completed":
                if state.get("failureReason") not in (None, {}):
                    raise DeployError("Fabric job reported an incomplete failure detail")
                return state
            _sleep_retry(response.headers.get("Retry-After") or receipt.retry_after or "10", deadline)
        raise DeployError("Fabric job exceeded the deployment timeout")

    def _poll_powerbi_refresh(
        self,
        receipt: ApiReceipt,
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if not receipt.operation_id:
            raise DeployError("Power BI refresh receipt has no request ID")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = self._powerbi_request("GET", receipt.location)
            if response.status_code != 200:
                raise DeployError(
                    f"Power BI refresh readback failed with HTTP {response.status_code}",
                )
            state = self._json(response)
            observed_id = state.get("requestId") or state.get("id")
            if observed_id and observed_id != receipt.operation_id:
                raise DeployError("Power BI refresh request ID changed during polling")
            status = state.get("status")
            if status == "Completed":
                if state.get("serviceExceptionJson"):
                    raise DeployError("Power BI refresh completed with a service exception")
                return state
            if status in {"Failed", "Disabled", "Unknown"}:
                raise DeployError(f"Power BI refresh ended without success: {status}")
            if status not in {"NotStarted", "InProgress"}:
                raise DeployError("Power BI refresh returned an unknown state")
            _sleep_retry(response.headers.get("Retry-After") or receipt.retry_after or "10", deadline)
        raise DeployError("Power BI refresh exceeded the deployment timeout")

    def verify_item(
        self,
        workspace_id: str,
        item: dict[str, Any],
        *,
        expected_name: str,
        expected_type: str,
        expected_marker: str,
    ) -> dict[str, Any]:
        if not item.get("id"):
            raise DeployError("Created item response has no exact ID")
        observed = self.get_item(
            workspace_id,
            item["id"],
            skill=_skill_for_item(expected_type),
        )
        if (
            observed.get("id") != item["id"]
            or observed.get("workspaceId") not in (None, workspace_id)
            or observed.get("displayName") != expected_name
            or observed.get("type") != expected_type
            or observed.get("description") != expected_marker
        ):
            raise DeployError("Created item readback differs from the exact deployment expectation")
        return observed


def _which_az() -> Path:
    value = shutil.which("az")
    if not value:
        raise DeployError("Azure CLI ('az') is required for a live interactive deployment")
    return Path(value)


def _skill_for_item(item_type: str) -> str:
    return SEMANTIC_SKILL if item_type == "SemanticModel" else SPARK_SKILL


def _definition_collection(item_type: str) -> str:
    return "semanticModels" if item_type == "SemanticModel" else "items"


def _operation_result(location: str) -> str:
    parsed = urlsplit(location)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/result", parsed.query, ""))


def _is_powerbi_location(location: str) -> bool:
    host = urlsplit(location).netloc
    return host == "api.powerbi.com" or bool(
        re.fullmatch(r"wabi-[a-z0-9-]+\.analysis\.windows\.net", host),
    )


def _sleep_retry(value: str, deadline: float) -> None:
    try:
        seconds = int(value)
    except ValueError as error:
        raise DeployError("Fabric returned an invalid Retry-After value") from error
    if seconds < 0 or time.monotonic() + seconds >= deadline:
        raise DeployError("Fabric Retry-After exceeds the deployment timeout")
    time.sleep(seconds)
