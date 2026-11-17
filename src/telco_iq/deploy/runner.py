from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

import httpx

from .assets import (
    AssetBundle,
    bind_definition_parts,
    bind_notebook,
    build_assets,
    decode_definition,
    decode_notebook_definition,
    definition_digest,
    encode_definition,
    notebook_definition,
    sha256,
)
from .client import (
    SEMANTIC_SKILL,
    SPARK_SKILL,
    ApiReceipt,
    AzureCliFabricClient,
    FabricClient,
    SubmissionBlockedError,
    SubmissionRejectedError,
)
from .journal import DeploymentJournal
from .models import (
    HERO_MODELS,
    LAKEHOUSE_NAME,
    MODEL_DISPLAY_NAMES,
    NOTEBOOK_NAME,
    PROFILE,
    SEED,
    DeployError,
    DeployOptions,
    DeployResult,
    build_plan,
    canonical_id,
    deployment_id,
    deployment_marker,
    journal_path,
)


@dataclass
class MutationCounts:
    created: list[str]
    reused: list[str]
    refreshed: list[str]


def run_deploy(
    options: DeployOptions,
    *,
    client: FabricClient | None = None,
    asset_bundle: AssetBundle | None = None,
    asset_factory: Callable[[Path], AssetBundle] = build_assets,
    stdout: TextIO | None = None,
) -> DeployResult:
    plan = build_plan(options)
    if options.dry_run:
        print(json.dumps(plan, indent=2, sort_keys=True), file=stdout)
        return DeployResult(
            status="DRY_RUN_OFFLINE",
            workspace=None,
            journal_path=journal_path(options),
            plan=plan,
        )

    owns_client = client is None
    if client is None:
        client = AzureCliFabricClient(options)
    assert client is not None
    try:
        workspace = client.resolve_workspace(options.workspace or "")
        workspace_id = canonical_id(workspace.get("id", ""), "resolved workspace ID")
        workspace_name = workspace.get("displayName")
        if (
            not isinstance(workspace_name, str)
            or not workspace_name
            or workspace.get("capacityId") is None
        ):
            raise DeployError("Existing workspace readback must include name and capacity assignment")
        journal = DeploymentJournal.for_options(options)
        deploy_id = deployment_id(options)
        journal.initialize(
            {
                "deploymentId": deploy_id,
                "tag": options.deployment_tag,
                "profile": PROFILE,
                "seed": SEED,
                "tenant": {
                    "id": canonical_id(client.tenant_id, "actual tenant ID"),
                    "user": client.user_name,
                },
                "subscription": client.subscription_id,
                "workspace": {
                    "id": workspace_id,
                    "displayName": workspace_name,
                    "capacityId": workspace["capacityId"],
                },
            },
        )
        work = journal.path.parent / "working" / deploy_id.replace(":", "-")
        work.mkdir(parents=True, exist_ok=True)
        bundle = asset_bundle or asset_factory(work)
        _validate_bundle(bundle)
        counts = MutationCounts([], [], [])
        artifacts: dict[str, Any] = {
            "source": bundle.source_digest,
            "dataManifest": sha256(json.dumps(bundle.manifest, sort_keys=True).encode("utf-8")),
            "models": {},
            "tables": [
                {
                    "name": item.table,
                    "rows": item.rows,
                    "bytes": item.bytes,
                    "fileSha256": item.file_sha256,
                    "contentSha256": item.content_sha256,
                }
                for item in bundle.tables
            ],
        }

        lakehouse_marker = deployment_marker(deploy_id, "lakehouse")
        lakehouse_body = {
            "displayName": LAKEHOUSE_NAME,
            "type": "Lakehouse",
            "description": lakehouse_marker,
            "creationPayload": {"enableSchemas": True},
        }
        lakehouse = _ensure_item(
            journal,
            client,
            options,
            alias="lakehouse",
            item_type="Lakehouse",
            display_name=LAKEHOUSE_NAME,
            marker=lakehouse_marker,
            body=lakehouse_body,
            skill=SPARK_SKILL,
            counts=counts,
        )
        lakehouse_id = canonical_id(lakehouse["id"], "lakehouse ID")
        artifacts["lakehouse"] = _body_digest(lakehouse_body)

        notebook_raw = bind_notebook(
            bundle,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            lakehouse_id=lakehouse_id,
            lakehouse_name=LAKEHOUSE_NAME,
        )
        notebook_marker = deployment_marker(deploy_id, "notebook")
        notebook_body = {
            "displayName": NOTEBOOK_NAME,
            "type": "Notebook",
            "description": notebook_marker,
            "definition": notebook_definition(notebook_raw),
        }
        notebook = _ensure_item(
            journal,
            client,
            options,
            alias="notebook",
            item_type="Notebook",
            display_name=NOTEBOOK_NAME,
            marker=notebook_marker,
            body=notebook_body,
            skill=SPARK_SKILL,
            counts=counts,
        )
        _verify_notebook(client, journal, workspace_id, notebook, notebook_raw)
        artifacts["notebook"] = {
            "body": _body_digest(notebook_body),
            "content": sha256(notebook_raw),
        }
        _ensure_notebook_job(journal, client, options, workspace_id, notebook["id"])

        model_items: dict[str, dict[str, Any]] = {}
        for alias in HERO_MODELS:
            bound = bind_definition_parts(bundle.model_parts[alias], workspace_id, lakehouse_id)
            model_marker = deployment_marker(deploy_id, f"model-{alias}")
            model_body = {
                "displayName": MODEL_DISPLAY_NAMES[alias],
                "description": model_marker,
                "definition": encode_definition(bound),
            }
            model = _ensure_item(
                journal,
                client,
                options,
                alias=f"model-{alias}",
                item_type="SemanticModel",
                display_name=MODEL_DISPLAY_NAMES[alias],
                marker=model_marker,
                body=model_body,
                skill=SEMANTIC_SKILL,
                counts=counts,
            )
            _verify_model(client, journal, workspace_id, model, alias, bound, options)
            artifacts["models"][alias] = {
                "body": _body_digest(model_body),
                "definition": definition_digest(bound),
                "name": MODEL_DISPLAY_NAMES[alias],
            }
            model_items[alias] = model

        for alias in HERO_MODELS:
            _ensure_refresh(journal, client, options, workspace_id, model_items[alias], alias, counts)

        journal.complete(artifacts)
        return DeployResult(
            status="DEPLOYED_AND_VERIFIED",
            workspace={"id": workspace_id, "displayName": workspace_name},
            journal_path=journal.path,
            created=tuple(counts.created),
            reused=tuple(counts.reused),
            refreshed=tuple(counts.refreshed),
        )
    finally:
        if owns_client and hasattr(client, "close"):
            client.close()


def _validate_bundle(bundle: AssetBundle) -> None:
    if bundle.manifest.get("profile") != PROFILE or bundle.manifest.get("seed") != SEED:
        raise DeployError("Asset bundle profile/seed differs from M5-01")
    if not bundle.model_parts or set(bundle.model_parts) != set(HERO_MODELS):
        raise DeployError("Asset bundle must contain exactly the four hero models")
    if not bundle.tables:
        raise DeployError("Asset bundle has no synthetic Delta tables")
    names = [item.table for item in bundle.tables]
    if len(names) != len(set(names)):
        raise DeployError("Asset bundle has duplicate table names")
    expected = {
        Path(path).stem
        for parts in bundle.model_parts.values()
        for path in parts
        if path.startswith("definition/tables/") and path.endswith(".tmdl")
    }
    if expected != set(names):
        raise DeployError("Asset bundle table selection differs from the four TMDL models")
    for item in bundle.tables:
        raw = base64.b64decode(item.payload, validate=True)
        if len(raw) != item.bytes or sha256(raw) != item.file_sha256:
            raise DeployError(f"Asset bundle payload differs: {item.table}")


def _ensure_item(
    journal: DeploymentJournal,
    client: FabricClient,
    options: DeployOptions,
    *,
    alias: str,
    item_type: str,
    display_name: str,
    marker: str,
    body: dict[str, Any],
    skill: str,
    counts: MutationCounts,
) -> dict[str, Any]:
    workspace_id = journal.data["workspace"]["id"]
    existing = journal.data.get("items", {}).get(alias)
    inventory = client.list_items(workspace_id, item_type)
    same_name = [item for item in inventory if item.get("displayName") == display_name]
    if existing is not None:
        state = existing.get("state")
        if state == "submitting":
            recovered = [
                item
                for item in same_name
                if item.get("type") == item_type and item.get("description") == marker
            ]
            if len(recovered) != 1:
                raise DeployError(
                    f"Creation outcome for {alias} is unknown; exact marker reconciliation found "
                    f"{len(recovered)} items and a POST will not be retried",
                )
            existing = {"state": "created-unverified", "item": recovered[0]}
            journal.set_item(alias, existing)
        elif state == "accepted":
            receipt = ApiReceipt.from_journal(existing["receipt"])
            result = client.poll_lro(receipt, timeout_seconds=options.poll_timeout_seconds)
            item = _validated_create_result(result, item_type)
            journal.set_item(
                alias,
                {
                    "state": "created-unverified",
                    "item": item,
                    "receipt": receipt.journal_value(),
                    "artifactDigest": _body_digest(body),
                },
            )
            existing = journal.data["items"][alias]
        elif state == "rejected":
            _raise_saved_rejection("Item creation", alias, existing)
        elif state not in {"created-unverified", "verified"}:
            raise DeployError(f"Unknown persisted item state for {alias}")
        item = existing["item"]
        if existing.get("artifactDigest") not in (None, _body_digest(body)):
            raise DeployError(f"Journal body digest differs for {alias}")
        observed = client.verify_item(
            workspace_id,
            item,
            expected_name=display_name,
            expected_type=item_type,
            expected_marker=marker,
        )
        if same_name and any(candidate.get("id") != item.get("id") for candidate in same_name):
            raise DeployError(f"Same-name item collision after journal recovery: {display_name}")
        journal.set_item(
            alias,
            {
                "state": "verified",
                "item": observed,
                "receipt": existing.get("receipt"),
                "artifactDigest": _body_digest(body),
            },
        )
        counts.reused.append(alias)
        return observed

    if same_name:
        raise DeployError(
            f"Name collision: {item_type} {display_name!r} exists; no item is adopted by name",
        )
    journal.set_item(alias, {"state": "submitting", "artifactDigest": _body_digest(body)})
    try:
        receipt = client.submit_item(
            workspace_id,
            body,
            item_type=item_type,
            skill=skill,
        )
    except SubmissionRejectedError as error:
        journal.set_item(
            alias,
            {
                "state": "rejected",
                "status": error.status,
                "error": str(error),
                "artifactDigest": _body_digest(body),
            },
        )
        raise
    except (DeployError, TimeoutError, OSError, httpx.HTTPError):
        raise DeployError(
            f"Item creation outcome for {alias} is unknown or failed; the journal blocks a duplicate POST",
        ) from None
    if receipt.state == "accepted":
        journal.set_item(
            alias,
            {
                "state": "accepted",
                "receipt": receipt.journal_value(),
                "artifactDigest": _body_digest(body),
            },
        )
        result = client.poll_lro(receipt, timeout_seconds=options.poll_timeout_seconds)
        item = _validated_create_result(result, item_type)
    else:
        item = _validated_create_result(receipt.body, item_type)
    journal.set_item(
        alias,
        {
            "state": "created-unverified",
            "item": item,
            "receipt": receipt.journal_value(),
            "artifactDigest": _body_digest(body),
        },
    )
    observed = client.verify_item(
        workspace_id,
        item,
        expected_name=display_name,
        expected_type=item_type,
        expected_marker=marker,
    )
    journal.set_item(
        alias,
        {
            "state": "verified",
            "item": observed,
            "receipt": receipt.journal_value(),
            "artifactDigest": _body_digest(body),
        },
    )
    counts.created.append(alias)
    return observed


def _validated_create_result(value: Any, item_type: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.get("id"):
        raise DeployError("Fabric create/readback result has no exact item identity")
    if value.get("type") not in (None, item_type):
        raise DeployError("Fabric create/readback result type differs")
    return value


def _verify_notebook(
    client: FabricClient,
    journal: DeploymentJournal,
    workspace_id: str,
    item: dict[str, Any],
    expected: bytes,
) -> None:
    value = client.get_definition(
        workspace_id,
        item["id"],
        item_type="Notebook",
        definition_format="ipynb",
        timeout_seconds=1800,
    )
    observed = decode_notebook_definition(value["definition"])
    if observed != expected or sha256(observed) != sha256(expected):
        raise DeployError("Notebook definition readback digest differs")
    journal.set_operation("definition:notebook", {"state": "verified", "digest": sha256(expected)})


def _verify_model(
    client: FabricClient,
    journal: DeploymentJournal,
    workspace_id: str,
    item: dict[str, Any],
    alias: str,
    expected: dict[str, bytes],
    options: DeployOptions,
) -> None:
    value = client.get_definition(
        workspace_id,
        item["id"],
        item_type="SemanticModel",
        definition_format="TMDL",
        timeout_seconds=options.poll_timeout_seconds,
    )
    observed = decode_definition(value["definition"])
    if observed != expected or definition_digest(observed) != definition_digest(expected):
        raise DeployError(f"Semantic model {alias} readback digest differs")
    journal.set_operation(
        f"definition:model-{alias}",
        {"state": "verified", "digest": definition_digest(expected)},
    )


def _ensure_notebook_job(
    journal: DeploymentJournal,
    client: FabricClient,
    options: DeployOptions,
    workspace_id: str,
    notebook_id: str,
) -> None:
    key = "job:notebook"
    operation = journal.data.get("operations", {}).get(key)
    if operation and operation.get("state") == "completed":
        return
    if operation and operation.get("state") == "rejected":
        _raise_saved_rejection("Notebook job", "notebook", operation)
    if operation and operation.get("state") == "accepted":
        receipt = ApiReceipt.from_journal(operation["receipt"])
        state = client.poll_job(receipt, timeout_seconds=options.poll_timeout_seconds)
        journal.set_operation(key, {"state": "completed", "receipt": receipt.journal_value(), "result": state})
        return
    if operation and operation.get("state") == "submitting":
        raise DeployError("Notebook job submission outcome is unknown; no duplicate job will be submitted")
    active = [
        job
        for job in client.list_jobs(workspace_id, notebook_id)
        if job.get("status") in {"NotStarted", "InProgress"}
    ]
    if active:
        raise DeployError("An exact notebook has an active unjournaled job; no duplicate job will be submitted")
    journal.set_operation(key, {"state": "submitting"})
    try:
        receipt = client.submit_job(
            workspace_id,
            notebook_id,
            job_type="RunNotebook",
            skill=SPARK_SKILL,
        )
    except SubmissionRejectedError as error:
        journal.set_operation(
            key,
            {"state": "rejected", "status": error.status, "error": str(error)},
        )
        raise
    except (DeployError, TimeoutError, OSError, httpx.HTTPError):
        raise DeployError("Notebook job outcome is unknown; no duplicate submission is allowed") from None
    journal.set_operation(key, {"state": "accepted", "receipt": receipt.journal_value()})
    state = client.poll_job(receipt, timeout_seconds=options.poll_timeout_seconds)
    journal.set_operation(key, {"state": "completed", "receipt": receipt.journal_value(), "result": state})


def _ensure_refresh(
    journal: DeploymentJournal,
    client: FabricClient,
    options: DeployOptions,
    workspace_id: str,
    item: dict[str, Any],
    alias: str,
    counts: MutationCounts,
) -> None:
    key = f"refresh:{alias}"
    operation = journal.data.get("operations", {}).get(key)
    if operation and operation.get("state") == "completed":
        counts.refreshed.append(alias)
        return
    if operation and operation.get("state") == "rejected":
        _raise_saved_rejection("Refresh submission", alias, operation)
    if operation and operation.get("state") == "accepted":
        receipt = ApiReceipt.from_journal(operation["receipt"])
        state = client.poll_job(receipt, timeout_seconds=options.poll_timeout_seconds)
        journal.set_operation(key, {"state": "completed", "receipt": receipt.journal_value(), "result": state})
        counts.refreshed.append(alias)
        return
    if operation and operation.get("state") == "submitting":
        raise DeployError(f"Refresh submission for {alias} is unknown; no duplicate refresh is allowed")
    journal.set_operation(key, {"state": "submitting"})
    try:
        receipt = client.refresh_model(workspace_id, item["id"])
    except SubmissionBlockedError as error:
        journal.set_operation(key, {"state": "blocked", "error": str(error)})
        raise
    except SubmissionRejectedError as error:
        journal.set_operation(
            key,
            {"state": "rejected", "status": error.status, "error": str(error)},
        )
        raise
    except (DeployError, TimeoutError, OSError, httpx.HTTPError):
        raise DeployError(f"Refresh submission for {alias} is unknown; no duplicate is allowed") from None
    journal.set_operation(key, {"state": "accepted", "receipt": receipt.journal_value()})
    state = client.poll_job(receipt, timeout_seconds=options.poll_timeout_seconds)
    journal.set_operation(key, {"state": "completed", "receipt": receipt.journal_value(), "result": state})
    counts.refreshed.append(alias)


def _body_digest(body: dict[str, Any]) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _raise_saved_rejection(kind: str, alias: str, state: dict[str, Any]) -> None:
    status = state.get("status", "unknown")
    detail = state.get("error") or "server rejection"
    raise DeployError(f"{kind} for {alias} previously rejected with HTTP {status}: {detail}")
