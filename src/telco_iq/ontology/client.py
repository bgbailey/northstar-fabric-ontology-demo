from __future__ import annotations

import time
from typing import Any, Protocol

import httpx

from telco_iq.deploy.client import (
    ApiReceipt,
    AzureCliFabricClient,
    FabricClient,
    SubmissionRejectedError,
)
from telco_iq.deploy.models import DeployOptions, looks_like_id

from .models import ONTOLOGY_SKILL, BootstrapError, BootstrapOptions


class OntologyClient(FabricClient, Protocol):
    tenant_id: str
    user_name: str


ONTOLOGY_TRANSPORT_ERRORS = (httpx.TransportError, TimeoutError, OSError)


class AzureCliOntologyClient(AzureCliFabricClient):
    def __init__(self, options: BootstrapOptions):
        adapted = DeployOptions(
            workspace=options.workspace,
            tenant_id=options.tenant_id,
            expected_user=options.expected_user,
            subscription_id=options.subscription_id,
            deployment_tag=options.deployment_tag,
            dry_run=False,
            journal_path=options.resolved_deployment_journal,
            azure_cli=options.azure_cli,
            poll_timeout_seconds=options.poll_timeout_seconds,
        )
        super().__init__(adapted)

    def _fail_sign_in_context(self, reason: str) -> None:
        tenant = self.expected_tenant or "<tenant>"
        user = self.expected_user or "<expected UPN>"
        subscription = (
            f" and subscription {self.expected_subscription}"
            if self.expected_subscription
            else ""
        )
        label = (
            f"Scout | M5-02 ontology bootstrap / exact run {self.run_id} | "
            f"Microsoft Fabric | {tenant} | {user}"
        )
        raise BootstrapError(
            f"{reason}; no sign-in was attempted. In a persistently labelled compliant "
            f"browser context titled `{label}`, sign in as {user} in tenant {tenant}"
            f"{subscription}, verify the actual user, tenant, and subscription with "
            "`az account show`, then rerun. Unmanaged or silent sign-in is not allowed.",
        )

    def resolve_workspace(self, selector: str) -> dict[str, Any]:
        if looks_like_id(selector):
            workspace = self._read(f"/v1/workspaces/{selector}", skill=ONTOLOGY_SKILL)
            if workspace.get("id") != selector:
                raise BootstrapError("Workspace direct readback ID differs")
            return workspace
        matches = [
            item
            for item in self._listing("/v1/workspaces", skill=ONTOLOGY_SKILL)
            if item.get("displayName") == selector
        ]
        if len(matches) != 1:
            raise BootstrapError(f"Workspace selector must resolve exactly once, found {len(matches)}")
        return matches[0]

    def list_items(self, workspace_id: str, item_type: str) -> list[dict[str, Any]]:
        return self._listing(
            f"/v1/workspaces/{workspace_id}/items?type={item_type}",
            skill=ONTOLOGY_SKILL,
        )

    def get_item(
        self,
        workspace_id: str,
        item_id: str,
        *,
        skill: str = ONTOLOGY_SKILL,
    ) -> dict[str, Any]:
        return super().get_item(workspace_id, item_id, skill=skill)

    def submit_item(
        self,
        workspace_id: str,
        body: dict[str, Any],
        *,
        item_type: str,
        skill: str,
    ) -> ApiReceipt:
        if item_type != "Ontology" or skill != ONTOLOGY_SKILL:
            raise BootstrapError("Ontology client only creates Ontology items")
        response = self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/items",
            skill=ONTOLOGY_SKILL,
            body=body,
        )
        if response.status_code in (200, 201):
            return ApiReceipt(
                "completed",
                response.status_code,
                self._json(response),
                skill=ONTOLOGY_SKILL,
            )
        if response.status_code == 202:
            operation_id = response.headers.get("x-ms-operation-id")
            location = response.headers.get("Location")
            if not operation_id and not location:
                raise BootstrapError("Ontology create acceptance has no operation identity")
            return ApiReceipt(
                "accepted",
                202,
                location=location,
                operation_id=operation_id,
                retry_after=response.headers.get("Retry-After"),
                skill=ONTOLOGY_SKILL,
            )
        if 400 <= response.status_code < 500:
            raise SubmissionRejectedError(
                f"Ontology item creation rejected with HTTP {response.status_code}",
                response.status_code,
            )
        raise BootstrapError(f"Ontology item creation failed with HTTP {response.status_code}")

    def poll_lro(self, receipt: ApiReceipt, *, timeout_seconds: int) -> dict[str, Any]:
        if not receipt.operation_id and not receipt.location:
            raise BootstrapError("Cannot poll an ontology operation without exact identity")
        deadline = time.monotonic() + timeout_seconds
        operation_path = (
            f"/v1/operations/{receipt.operation_id}"
            if receipt.operation_id
            else str(receipt.location)
        )
        while time.monotonic() < deadline:
            response = self._request("GET", operation_path, skill=ONTOLOGY_SKILL)
            state = self._json(response)
            observed = response.headers.get("x-ms-operation-id")
            if receipt.operation_id and observed and observed != receipt.operation_id:
                raise BootstrapError("Ontology operation identity changed while polling")
            status = state.get("status")
            if response.status_code != 200 or status not in {
                "NotStarted",
                "Running",
                "Succeeded",
                "Failed",
                "Cancelled",
            }:
                raise BootstrapError("Ontology operation returned an unknown state")
            if status in {"Failed", "Cancelled"}:
                raise BootstrapError("Ontology operation failed or was cancelled")
            if status == "Succeeded":
                result_path = (
                    f"/v1/operations/{receipt.operation_id}/result"
                    if receipt.operation_id
                    else operation_path.rstrip("/") + "/result"
                )
                result = self._request("GET", result_path, skill=ONTOLOGY_SKILL)
                if result.status_code != 200:
                    raise BootstrapError("Ontology operation succeeded without a complete result")
                return self._json(result)
            delay = response.headers.get("Retry-After") or receipt.retry_after or "5"
            try:
                seconds = int(delay)
            except ValueError as error:
                raise BootstrapError("Ontology operation returned invalid Retry-After") from error
            if seconds < 0 or time.monotonic() + seconds >= deadline:
                raise BootstrapError("Ontology operation Retry-After exceeds the bootstrap timeout")
            time.sleep(seconds)
        raise BootstrapError("Ontology operation exceeded the bootstrap timeout")

    def get_definition(
        self,
        workspace_id: str,
        item_id: str,
        *,
        item_type: str,
        definition_format: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if item_type != "Ontology":
            raise BootstrapError("Ontology client only reads Ontology definitions")
        path = f"/v1/workspaces/{workspace_id}/items/{item_id}/getDefinition?format={definition_format}"
        response = self._request("POST", path, skill=ONTOLOGY_SKILL, body={})
        if response.status_code == 200:
            return self._json(response)
        if response.status_code == 202:
            location = response.headers.get("Location")
            operation_id = response.headers.get("x-ms-operation-id")
            if not location and not operation_id:
                raise BootstrapError("Ontology getDefinition acceptance has no operation identity")
            receipt = ApiReceipt(
                "accepted",
                202,
                location=location,
                operation_id=operation_id,
                retry_after=response.headers.get("Retry-After"),
                skill=ONTOLOGY_SKILL,
            )
            return self.poll_lro(receipt, timeout_seconds=timeout_seconds)
        raise BootstrapError("Ontology getDefinition failed before readback completed")

    def verify_item(
        self,
        workspace_id: str,
        item: dict[str, Any],
        *,
        expected_name: str,
        expected_type: str,
        expected_marker: str,
    ) -> dict[str, Any]:
        if item.get("id") != str(item.get("id")) or not item.get("id"):
            raise BootstrapError("Created ontology response has no exact ID")
        observed = self.get_item(workspace_id, item["id"], skill=ONTOLOGY_SKILL)
        if (
            observed.get("id") != item["id"]
            or observed.get("workspaceId") not in (None, workspace_id)
            or observed.get("displayName") != expected_name
            or observed.get("type") != expected_type
            or observed.get("description") != expected_marker
        ):
            raise BootstrapError("Ontology item readback differs from the exact bootstrap expectation")
        return observed
