from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
from pathlib import Path
from typing import Any, Callable, TextIO

from telco_iq.deploy.assets import sha256
from telco_iq.deploy.client import ApiReceipt, SubmissionRejectedError
from telco_iq.deploy.journal import DeploymentJournal
from telco_iq.deploy.models import (
    HERO_MODELS,
    MODEL_DISPLAY_NAMES,
    SCHEMA_VERSION,
    canonical_id,
    deployment_marker,
)

from .assets import load_contract
from .client import ONTOLOGY_TRANSPORT_ERRORS, AzureCliOntologyClient, OntologyClient
from .conformance import check_exported_definition
from .journal import BootstrapJournal
from .models import (
    ONTOLOGY_ITEM_TYPE,
    ONTOLOGY_NAME,
    ONTOLOGY_SKILL,
    BootstrapError,
    BootstrapOptions,
    BootstrapResult,
    export_path,
    state_path,
)
from .prompts import render_ordered_steps

ConformanceCheck = Callable[..., dict[str, Any]]
InputFunction = Callable[[str], str]


def run_bootstrap(
    options: BootstrapOptions,
    *,
    client: OntologyClient | None = None,
    input_func: InputFunction = input,
    stdout: TextIO | None = None,
    conformance: ConformanceCheck = check_exported_definition,
) -> BootstrapResult:
    contract = load_contract()
    target = _load_deployment_target(options)
    model_map = {
        alias: {"displayName": item["displayName"], "id": item["id"]}
        for alias, item in target["models"].items()
    }
    steps = render_ordered_steps(contract, model_map)
    state_file = state_path(options)
    definition_file = export_path(options)
    DeploymentJournal.validate_private_path(state_file)
    DeploymentJournal.validate_private_path(definition_file)
    plan = _build_plan(target, steps, state_file, definition_file)
    if options.dry_run:
        print(json.dumps(plan, indent=2, sort_keys=True), file=stdout)
        return BootstrapResult(
            status="DRY_RUN_OFFLINE",
            phase="create",
            workspace=target["workspace"],
            state_journal_path=state_file,
            export_path=definition_file,
            plan=plan,
        )

    journal = BootstrapJournal(state_file)
    identity = {
        "deploymentId": target["deploymentId"],
        "deploymentJournal": str(options.resolved_deployment_journal),
        "deploymentJournalSha256": target["sha256"],
        "contractSha256": sha256(_canonical(contract)),
        "tenant": target["tenant"],
        "workspace": target["workspace"],
        "models": model_map,
        "ontology": {"displayName": ONTOLOGY_NAME},
    }
    journal.initialize(identity)
    if journal.data.get("completed") is True:
        item_id = _verified_item_id(journal.data)
        return BootstrapResult(
            status="BOOTSTRAPPED_AND_CONFORMANT",
            phase="complete",
            workspace=target["workspace"],
            state_journal_path=journal.path,
            ontology_item_id=item_id,
            export_path=definition_file,
        )

    owns_client = client is None
    if client is None:
        client = AzureCliOntologyClient(options)
    assert client is not None
    try:
        _verify_client_target(client, target, options)
        item = _ensure_ontology_item(journal, client, options, target)
        item_id = canonical_id(item.get("id", ""), "ontology item ID")
        current_phase = _advance_guided_steps(journal, steps, item, input_func, stdout)
        if current_phase is not None:
            return BootstrapResult(
                status="PENDING_INPUT",
                phase=current_phase,
                workspace=target["workspace"],
                state_journal_path=journal.path,
                ontology_item_id=item_id,
                export_path=definition_file,
            )

        _export_definition(journal, client, options, item_id, definition_file)
        report = _run_conformance(conformance, contract, definition_file, model_map)
        if not isinstance(report, dict) or not isinstance(report.get("conformant"), bool):
            raise BootstrapError("Injected conformance check returned an invalid report")
        journal.set_operation(
            "conformance",
            {"state": "passed" if report["conformant"] else "failed", "report": report},
        )
        if not report["conformant"]:
            journal.set_phase("conformance-failed")
            raise BootstrapError("Exported ontology definition is non-conformant")
        journal.data["completed"] = True
        journal.set_phase("complete")
        return BootstrapResult(
            status="BOOTSTRAPPED_AND_CONFORMANT",
            phase="complete",
            workspace=target["workspace"],
            state_journal_path=journal.path,
            ontology_item_id=item_id,
            export_path=definition_file,
        )
    finally:
        if owns_client and hasattr(client, "close"):
            client.close()


def _load_deployment_target(options: BootstrapOptions) -> dict[str, Any]:
    path = options.resolved_deployment_journal
    DeploymentJournal.validate_private_path(path)
    journal = DeploymentJournal(path)
    data = journal.snapshot()
    if data.get("schemaVersion") != SCHEMA_VERSION or data.get("completed") is not True:
        raise BootstrapError("Deployment journal must be an exact completed northstar-deploy-v1 journal")
    workspace = data.get("workspace")
    tenant = data.get("tenant")
    if not isinstance(workspace, dict) or not isinstance(tenant, dict):
        raise BootstrapError("Completed deployment journal has no exact workspace/tenant target")
    workspace_id = canonical_id(workspace.get("id", ""), "deployed workspace ID")
    tenant_id = canonical_id(tenant.get("id", ""), "deployed tenant ID")
    workspace_name = workspace.get("displayName")
    user_name = tenant.get("user")
    if not isinstance(workspace_name, str) or not workspace_name:
        raise BootstrapError("Deployment journal workspace display name is missing")
    if not isinstance(user_name, str) or not user_name:
        raise BootstrapError("Deployment journal interactive user is missing")
    if options.workspace and options.workspace not in {workspace_id, workspace_name}:
        raise BootstrapError("Requested workspace does not match the completed deployment journal")
    if options.tenant_id and options.tenant_id != tenant_id:
        raise BootstrapError("Requested tenant does not match the completed deployment journal")
    if options.expected_user and options.expected_user.casefold() != user_name.casefold():
        raise BootstrapError("Requested user does not match the completed deployment journal")

    deploy_id = data.get("deploymentId")
    if not isinstance(deploy_id, str) or not deploy_id.startswith("northstar-m5-01:"):
        raise BootstrapError("Deployment journal has an invalid deployment ID")
    models: dict[str, dict[str, Any]] = {}
    for alias in HERO_MODELS:
        record = data.get("items", {}).get(f"model-{alias}")
        if not isinstance(record, dict) or record.get("state") != "verified":
            raise BootstrapError(f"Deployment journal has no verified model record: {alias}")
        item = record.get("item")
        if not isinstance(item, dict):
            raise BootstrapError(f"Deployment journal model record is incomplete: {alias}")
        item_id = canonical_id(item.get("id", ""), f"{alias} semantic model ID")
        expected_marker = deployment_marker(deploy_id, f"model-{alias}")
        if (
            item.get("displayName") != MODEL_DISPLAY_NAMES[alias]
            or item.get("type") != "SemanticModel"
            or item.get("description") != expected_marker
            or item.get("workspaceId") not in (None, workspace_id)
        ):
            raise BootstrapError(f"Deployment journal model target differs: {alias}")
        artifact = data.get("artifactDigests", {}).get("models", {}).get(alias)
        if not isinstance(artifact, dict) or artifact.get("name") != item.get("displayName"):
            raise BootstrapError(f"Deployment journal model artifact differs: {alias}")
        models[alias] = {"id": item_id, **{key: item[key] for key in ("displayName", "type")}}
    if len({model["id"] for model in models.values()}) != len(HERO_MODELS):
        raise BootstrapError("Deployment journal model IDs are not unique")
    return {
        "deploymentId": deploy_id,
        "tag": data.get("tag"),
        "tenant": {"id": tenant_id, "user": user_name},
        "workspace": {
            "id": workspace_id,
            "displayName": workspace_name,
            "capacityId": workspace.get("capacityId"),
        },
        "models": models,
        "sha256": sha256(_canonical(data)),
    }


def _verify_client_target(
    client: OntologyClient,
    target: dict[str, Any],
    options: BootstrapOptions,
) -> None:
    if canonical_id(client.tenant_id, "actual ontology client tenant") != target["tenant"]["id"]:
        raise BootstrapError("Ontology client tenant differs from the completed deployment")
    if client.user_name.casefold() != target["tenant"]["user"].casefold():
        raise BootstrapError("Ontology client user differs from the completed deployment")
    if options.tenant_id and canonical_id(options.tenant_id, "expected tenant") != target["tenant"]["id"]:
        raise BootstrapError("Expected tenant differs from the completed deployment")
    if options.expected_user and options.expected_user.casefold() != target["tenant"]["user"].casefold():
        raise BootstrapError("Expected user differs from the completed deployment")
    resolved = client.resolve_workspace(options.workspace or target["workspace"]["id"])
    if (
        resolved.get("id") != target["workspace"]["id"]
        or resolved.get("displayName") != target["workspace"]["displayName"]
        or (
            resolved.get("capacityId") is not None
            and resolved.get("capacityId") != target["workspace"]["capacityId"]
        )
    ):
        raise BootstrapError("Resolved workspace differs from the completed deployment")


def _build_plan(
    target: dict[str, Any],
    steps: list[dict[str, Any]],
    state_file: Path,
    definition_file: Path,
) -> dict[str, Any]:
    marker = deployment_marker(target["deploymentId"], "ontology")
    return {
        "status": "DRY_RUN_OFFLINE",
        "proofLevel": "LOCAL_STATIC_ONLY",
        "liveExecution": "NOT_RUN",
        "sourceDeployment": {
            "deploymentId": target["deploymentId"],
            "schema": SCHEMA_VERSION,
            "completed": True,
            "tenant": target["tenant"],
            "workspace": target["workspace"],
        },
        "ontology": {
            "displayName": ONTOLOGY_NAME,
            "type": ONTOLOGY_ITEM_TYPE,
            "marker": marker,
            "definition": [".platform", "database.tmdl"],
            "create": "one POST into the exact existing workspace; never adopt by name",
        },
        "models": [
            {"alias": alias, **target["models"][alias]}
            for alias in HERO_MODELS
        ],
        "guidedSteps": steps,
        "stateJournal": str(state_file),
        "exportPath": str(definition_file),
        "orderedPhases": [
            "create",
            "awaiting-model-step-1-review",
            "awaiting-apply-step-1",
            "export",
            "conformance",
            "complete",
        ],
        "safety": [
            "no workspace, item, or capacity deletion",
            "no capacity mutation",
            "no updateDefinition call",
            "semantic-model proxy metrics are authored and applied only by a person through the ontology agent",
            "unknown POST outcome blocks another create submission",
            "export is written only to a private path outside the repository and OneDrive",
        ],
        "todo": "TODO(verify-live): G1 run this command in an approved demo tenant and retain evidence privately",
    }


def _ensure_ontology_item(
    journal: BootstrapJournal,
    client: OntologyClient,
    options: BootstrapOptions,
    target: dict[str, Any],
) -> dict[str, Any]:
    workspace_id = target["workspace"]["id"]
    marker = deployment_marker(target["deploymentId"], "ontology")
    body = _minimal_body(marker)
    operation = journal.data.get("operations", {}).get("ontology:create")
    inventory = client.list_items(workspace_id, ONTOLOGY_ITEM_TYPE)
    same_name = [item for item in inventory if item.get("displayName") == ONTOLOGY_NAME]

    if operation is not None:
        state = operation.get("state")
        if state == "rejected":
            raise BootstrapError(
                "Ontology create was rejected on an earlier run; the POST will not be retried: "
                f"{operation.get('error', 'no prior rejection detail was recorded')}",
            )
        if state == "verified":
            item = operation.get("item")
            if not isinstance(item, dict):
                raise BootstrapError("Bootstrap journal verified ontology item is incomplete")
            observed = client.verify_item(
                workspace_id,
                item,
                expected_name=ONTOLOGY_NAME,
                expected_type=ONTOLOGY_ITEM_TYPE,
                expected_marker=marker,
            )
            if any(candidate.get("id") != observed.get("id") for candidate in same_name):
                raise BootstrapError("Same-name ontology collision appeared after journal recovery")
            return observed
        if state == "created-unverified":
            item = operation.get("item")
            if not isinstance(item, dict):
                raise BootstrapError("Bootstrap journal created ontology item is incomplete")
        elif state == "accepted":
            receipt = ApiReceipt.from_journal(operation["receipt"])
            created = client.poll_lro(receipt, timeout_seconds=options.poll_timeout_seconds)
            item = _validated_create_result(created)
        elif state == "submitting":
            recovered = [
                candidate
                for candidate in same_name
                if candidate.get("description") == marker and candidate.get("type") == ONTOLOGY_ITEM_TYPE
            ]
            if len(recovered) != 1:
                raise BootstrapError(
                    "Ontology creation outcome is unknown; exact marker reconciliation did not find "
                    "one item and another POST is blocked",
                )
            item = recovered[0]
        else:
            raise BootstrapError("Unknown persisted ontology creation state")
        observed = client.verify_item(
            workspace_id,
            item,
            expected_name=ONTOLOGY_NAME,
            expected_type=ONTOLOGY_ITEM_TYPE,
            expected_marker=marker,
        )
        journal.set_operation(
            "ontology:create",
            {
                "state": "verified",
                "item": observed,
                "receipt": operation.get("receipt"),
                "bodySha256": _body_sha256(body),
            },
        )
        return observed

    if same_name:
        raise BootstrapError(
            f"Ontology name collision: {ONTOLOGY_NAME!r} exists and cannot be adopted by name",
        )
    journal.set_operation(
        "ontology:create",
        {"state": "submitting", "bodySha256": _body_sha256(body)},
    )
    try:
        receipt = client.submit_item(
            workspace_id,
            body,
            item_type=ONTOLOGY_ITEM_TYPE,
            skill=ONTOLOGY_SKILL,
        )
    except SubmissionRejectedError as error:
        journal.set_operation(
            "ontology:create",
            {
                "state": "rejected",
                "bodySha256": _body_sha256(body),
                "status": error.status,
                "error": str(error),
            },
        )
        raise
    except (BootstrapError, *ONTOLOGY_TRANSPORT_ERRORS):
        raise BootstrapError(
            "Ontology create outcome is unknown; the bootstrap journal blocks a duplicate POST",
        ) from None
    if receipt.state == "accepted":
        journal.set_operation(
            "ontology:create",
            {
                "state": "accepted",
                "receipt": receipt.journal_value(),
                "bodySha256": _body_sha256(body),
            },
        )
        created = client.poll_lro(receipt, timeout_seconds=options.poll_timeout_seconds)
        item = _validated_create_result(created)
    else:
        item = _validated_create_result(receipt.body)
    journal.set_operation(
        "ontology:create",
        {
            "state": "created-unverified",
            "item": item,
            "receipt": receipt.journal_value(),
            "bodySha256": _body_sha256(body),
        },
    )
    observed = client.verify_item(
        workspace_id,
        item,
        expected_name=ONTOLOGY_NAME,
        expected_type=ONTOLOGY_ITEM_TYPE,
        expected_marker=marker,
    )
    journal.set_operation(
        "ontology:create",
        {
            "state": "verified",
            "item": observed,
            "receipt": receipt.journal_value(),
            "bodySha256": _body_sha256(body),
        },
    )
    return observed


def _minimal_body(marker: str) -> dict[str, Any]:
    platform = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
        "platformProperties/2.0.0/schema.json",
        "metadata": {"type": ONTOLOGY_ITEM_TYPE, "displayName": ONTOLOGY_NAME},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    }
    return {
        "displayName": ONTOLOGY_NAME,
        "type": ONTOLOGY_ITEM_TYPE,
        "description": marker,
        "definition": {
            "format": "TMDL",
            "parts": [
                _part(".platform", json.dumps(platform, indent=2).encode("utf-8") + b"\n"),
                _part("database.tmdl", b"database\n\tcompatibilityLevel: 1000000\n"),
            ],
        },
    }


def _part(path: str, raw: bytes) -> dict[str, str]:
    return {
        "path": path,
        "payload": base64.b64encode(raw).decode("ascii"),
        "payloadType": "InlineBase64",
    }


def _validated_create_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.get("id"):
        raise BootstrapError("Fabric create/readback result has no exact ontology identity")
    if value.get("type") not in (None, ONTOLOGY_ITEM_TYPE):
        raise BootstrapError("Fabric create/readback result type differs from Ontology")
    return value


def _advance_guided_steps(
    journal: BootstrapJournal,
    steps: list[dict[str, Any]],
    item: dict[str, Any],
    input_func: InputFunction,
    stdout: TextIO | None,
) -> str | None:
    completed = set(journal.data.get("completedSteps", []))
    first = next((number for number in range(1, len(steps) + 1) if number not in completed), None)
    if first is None:
        if journal.data.get("phase") not in {"export", "conformance-failed"}:
            journal.set_phase("export")
        return None
    phase = journal.data.get("phase")
    for step in steps:
        number = step["number"]
        if number in completed:
            continue
        review_phase = f"awaiting-model-step-{number}-review"
        apply_phase = f"awaiting-apply-step-{number}"
        if phase in (None, "create", "export"):
            journal.set_phase(review_phase)
            phase = review_phase
        if phase == review_phase:
            print(step["markdown"], file=stdout)
            print(
                f"Review the Plan-mode preview. Type the exact ontology item name "
                f"{item['displayName']!r} to continue.",
                file=stdout,
            )
            if not _confirm(input_func, item["displayName"]):
                journal.set_phase(review_phase)
                return review_phase
            journal.set_phase(apply_phase)
            phase = apply_phase
        if phase == apply_phase:
            print(
                f"Switch the ontology agent to Act mode for step {number}, apply only the reviewed "
                f"{step['model']['displayName']} change, and verify the canvas. "
                f"Type 'apply step {number}' when complete.",
                file=stdout,
            )
            if not _confirm(input_func, f"apply step {number}"):
                journal.set_phase(apply_phase)
                return apply_phase
            completed.add(number)
            journal.data["completedSteps"] = sorted(completed)
            journal.set_phase(f"awaiting-model-step-{number + 1}-review")
            phase = journal.data["phase"]
            continue
        raise BootstrapError(f"Bootstrap state is on unexpected phase: {phase}")
    if len(completed) == len(steps):
        journal.set_phase("export")
        return None
    return journal.data["phase"]


def _confirm(input_func: InputFunction, expected: str) -> bool:
    try:
        answer = input_func("> ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == expected


def _export_definition(
    journal: BootstrapJournal,
    client: OntologyClient,
    options: BootstrapOptions,
    item_id: str,
    output: Path,
) -> None:
    journal.set_phase("export")
    envelope = client.get_definition(
        journal.data["workspace"]["id"],
        item_id,
        item_type=ONTOLOGY_ITEM_TYPE,
        definition_format="TMDL",
        timeout_seconds=options.poll_timeout_seconds,
    )
    definition = envelope.get("definition")
    if not isinstance(definition, dict) or not isinstance(definition.get("parts"), list):
        raise BootstrapError("Ontology getDefinition did not return a complete TMDL envelope")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope, indent=2, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(output)
    journal.set_operation(
        "export",
        {
            "state": "completed",
            "path": str(output),
            "sha256": sha256(output.read_bytes()),
            "itemId": item_id,
        },
    )
    journal.set_phase("conformance")


def _verified_item_id(data: dict[str, Any]) -> str:
    operation = data.get("operations", {}).get("ontology:create", {})
    if operation.get("state") != "verified":
        raise BootstrapError("Completed bootstrap journal has no verified ontology item")
    return canonical_id(operation.get("item", {}).get("id", ""), "verified ontology item ID")


def _body_sha256(body: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(body)).hexdigest()


def _run_conformance(
    check: ConformanceCheck,
    contract: dict[str, Any],
    definition_file: Path,
    expected_models: dict[str, dict[str, str]],
) -> dict[str, Any]:
    parameters = inspect.signature(check).parameters.values()
    supports_mapping = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or parameter.name == "expected_models"
        for parameter in parameters
    )
    if supports_mapping:
        return check(contract, definition_file, expected_models=expected_models)
    return check(contract, definition_file)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
