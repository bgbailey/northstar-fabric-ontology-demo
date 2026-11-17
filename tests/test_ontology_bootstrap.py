from __future__ import annotations

import argparse
import base64
import copy
import io
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from telco_iq.deploy.client import ApiReceipt, SubmissionRejectedError
from telco_iq.deploy.models import (
    HERO_MODELS,
    MODEL_DISPLAY_NAMES,
    SCHEMA_VERSION,
    deployment_marker,
)
from telco_iq.ontology import (
    BootstrapError,
    BootstrapOptions,
    build_ontology_parser,
    check_exported_definition,
    run_bootstrap,
)
from telco_iq.ontology.assets import load_contract
from telco_iq.ontology.client import AzureCliOntologyClient

TENANT = "00000000-0000-0000-0000-000000000101"
WORKSPACE = "00000000-0000-0000-0000-000000000102"
DEPLOYMENT_ID = "northstar-m5-01:0123456789abcdef"
USER = "synthetic@invalid.test"


def synthetic_item_id(value: int) -> str:
    return f"00000000-0000-0000-0000-{value:012}"


def write_deployment_journal(path: Path, *, wrong_model: str | None = None) -> dict[str, Any]:
    items: dict[str, Any] = {}
    models: dict[str, Any] = {}
    for index, alias in enumerate(HERO_MODELS, start=10):
        item = {
            "id": f"00000000-0000-0000-0000-{index:012}",
            "displayName": MODEL_DISPLAY_NAMES[alias],
            "type": "SemanticModel",
            "description": deployment_marker(DEPLOYMENT_ID, f"model-{alias}"),
            "workspaceId": WORKSPACE,
        }
        items[f"model-{alias}"] = {
            "state": "verified",
            "item": item,
            "artifactDigest": f"digest-{alias}",
        }
        models[alias] = {
            "definition": f"definition-{alias}",
            "body": f"body-{alias}",
            "name": item["displayName"],
        }
    if wrong_model:
        items[f"model-{wrong_model}"]["item"]["id"] = synthetic_item_id(999999999999)
        items[f"model-{wrong_model}"]["item"]["displayName"] = "Wrong target model"
    value = {
        "schemaVersion": SCHEMA_VERSION,
        "deploymentId": DEPLOYMENT_ID,
        "tag": "m5-01-test",
        "profile": "service-assurance",
        "seed": 20260929,
        "completed": True,
        "tenant": {"id": TENANT, "user": USER},
        "workspace": {
            "id": WORKSPACE,
            "displayName": "Northstar Synthetic Workspace",
            "capacityId": "00000000-0000-0000-0000-000000000103",
        },
        "items": items,
        "operations": {},
        "artifactDigests": {"models": models},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return value


def options(tmp_path: Path, **changes: Any) -> BootstrapOptions:
    deployment_journal = tmp_path / "private" / "deployment.json"
    write_deployment_journal(deployment_journal)
    values = {
        "workspace": "Northstar Synthetic Workspace",
        "tenant_id": TENANT,
        "expected_user": USER,
        "deployment_journal": deployment_journal,
        "state_journal": tmp_path / "private" / "bootstrap.json",
        "export_path": tmp_path / "private" / "ontology-export.json",
    }
    values.update(changes)
    return BootstrapOptions(**values)


class FakeOntologyClient:
    tenant_id = TENANT
    user_name = USER

    def __init__(self) -> None:
        self.workspace = {
            "id": WORKSPACE,
            "displayName": "Northstar Synthetic Workspace",
        }
        self.items: dict[str, dict[str, Any]] = {}
        self.create_attempts = 0
        self.poll_attempts = 0
        self.export_attempts = 0
        self.fail_create: bool = False
        self.fail_first_poll: bool = False
        self.accept_creation: bool = False
        self.created_without_response = False
        self.reject_create = False
        self.submit_error: str | None = None
        self.export_source: Path | None = None

    def resolve_workspace(self, selector: str) -> dict[str, Any]:
        return copy.deepcopy(self.workspace)

    def list_items(self, workspace_id: str, item_type: str) -> list[dict[str, Any]]:
        assert workspace_id == WORKSPACE
        return [
            copy.deepcopy(item)
            for item in self.items.values()
            if item["type"] == item_type
        ]

    def get_item(
        self,
        workspace_id: str,
        item_id: str,
        *,
        skill: str = "ontology-cli",
    ) -> dict[str, Any]:
        assert skill == "ontology-cli"
        return copy.deepcopy(self.items[item_id])

    def submit_item(
        self,
        workspace_id: str,
        body: dict[str, Any],
        *,
        item_type: str,
        skill: str,
    ) -> ApiReceipt:
        assert skill == "ontology-cli"
        self.create_attempts += 1
        if self.reject_create:
            raise SubmissionRejectedError(
                "Fabric rejected ontology payload with HTTP 403",
                403,
            )
        if self.submit_error == "500":
            raise BootstrapError("Ontology item creation failed with HTTP 500")
        if self.submit_error == "identityless-202":
            raise BootstrapError("Ontology create acceptance has no operation identity")
        if self.fail_create:
            if self.created_without_response:
                self.add_created_item(body)
            raise TimeoutError("unknown create outcome")
        item = self.add_created_item(body)
        if self.accept_creation:
            return ApiReceipt(
                "accepted",
                202,
                location=(
                    f"https://api.fabric.microsoft.com/v1/operations/"
                    f"ontology-{self.create_attempts}"
                ),
                operation_id=f"ontology-{self.create_attempts}",
                retry_after="0",
                skill=skill,
            )
        return ApiReceipt("completed", 201, body=copy.deepcopy(item), skill=skill)

    def add_created_item(self, body: dict[str, Any]) -> dict[str, Any]:
        item_id = f"00000000-0000-0000-0000-{500 + self.create_attempts:012}"
        item = {
            "id": item_id,
            "displayName": body["displayName"],
            "type": "Ontology",
            "description": body["description"],
            "workspaceId": WORKSPACE,
        }
        self.items[item_id] = item
        return item

    def poll_lro(self, receipt: ApiReceipt, *, timeout_seconds: int) -> dict[str, Any]:
        self.poll_attempts += 1
        if self.fail_first_poll:
            self.fail_first_poll = False
            raise BootstrapError("synthetic accepted operation interrupted")
        assert receipt.skill == "ontology-cli"
        return copy.deepcopy(next(reversed(self.items.values())))

    def get_definition(
        self,
        workspace_id: str,
        item_id: str,
        *,
        item_type: str,
        definition_format: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        assert workspace_id == WORKSPACE
        assert item_id in self.items
        assert item_type == "Ontology"
        assert definition_format == "TMDL"
        self.export_attempts += 1
        if self.export_source is not None:
            parts = []
            for source in sorted(self.export_source.rglob("*")):
                if not source.is_file():
                    continue
                parts.append(
                    {
                        "path": source.relative_to(self.export_source).as_posix(),
                        "payload": base64.b64encode(source.read_bytes()).decode("ascii"),
                        "payloadType": "InlineBase64",
                    },
                )
            return {"definition": {"format": "TMDL", "parts": parts}}
        return {
            "definition": {
                "format": "TMDL",
                "parts": [
                    {
                        "path": "database.tmdl",
                        "payload": "ZGF0YWJhc2UKCWNvbXBhdGliaWxpdHlMZXZlbDogMTAwMDAwMAo=",
                        "payloadType": "InlineBase64",
                    },
                ],
            },
        }

    def verify_item(
        self,
        workspace_id: str,
        item: dict[str, Any],
        *,
        expected_name: str,
        expected_type: str,
        expected_marker: str,
    ) -> dict[str, Any]:
        observed = self.get_item(workspace_id, item["id"])
        assert observed["displayName"] == expected_name
        assert observed["type"] == expected_type
        assert observed["description"] == expected_marker
        return observed


def confirmations() -> Iterator[str]:
    yield "Northstar_Service_Assurance"
    yield "apply step 1"
    yield "Northstar_Service_Assurance"
    yield "apply step 2"
    yield "Northstar_Service_Assurance"
    yield "apply step 3"
    yield "Northstar_Service_Assurance"
    yield "apply step 4"


def conformant(
    contract: dict[str, Any],
    path: Path,
    *,
    expected_models: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    assert path.is_file()
    if expected_models is not None:
        assert set(expected_models) == set(HERO_MODELS)
        assert all(item["displayName"] and item["id"] for item in expected_models.values())
    return {
        "conformant": True,
        "summary": {"missing": 0, "extra": 0, "different": 0},
    }


def test_dry_run_uses_complete_journal_without_auth_network_or_mutation(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run attempted live behavior")

    monkeypatch.setattr("telco_iq.ontology.runner.AzureCliOntologyClient", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    output = io.StringIO()
    result = run_bootstrap(
        options(tmp_path, dry_run=True),
        client=forbidden,
        input_func=forbidden,
        stdout=output,
        conformance=forbidden,
    )
    plan = json.loads(output.getvalue())
    assert result.status == "DRY_RUN_OFFLINE"
    assert [model["alias"] for model in plan["models"]] == list(HERO_MODELS)
    assert all("id" in model and "displayName" in model for model in plan["models"])
    assert plan["ontology"]["definition"] == [".platform", "database.tmdl"]
    rendered = json.dumps(plan["guidedSteps"])
    assert "Generate ontology" not in rendered
    assert "create `Northstar_Service_Assurance`" not in rendered
    assert "do not create another item" in rendered
    assert "updateDefinition" in "\n".join(plan["safety"])
    assert not result.state_journal_path.exists()


def test_creation_is_once_and_completed_state_needs_no_replay(tmp_path):
    fake = FakeOntologyClient()
    opts = options(tmp_path)
    answers = confirmations()
    first = run_bootstrap(
        opts,
        client=fake,
        input_func=lambda _: next(answers),
        conformance=conformant,
    )
    assert first.status == "BOOTSTRAPPED_AND_CONFORMANT"
    assert fake.create_attempts == 1
    assert fake.export_attempts == 1
    assert first.export_path and first.export_path.is_file()

    second = run_bootstrap(
        opts,
        client=fake,
        input_func=lambda _: pytest.fail("completed state must not prompt"),
        conformance=lambda *_: pytest.fail("completed state must not reconform"),
    )
    assert second.status == "BOOTSTRAPPED_AND_CONFORMANT"
    assert fake.create_attempts == 1
    assert fake.export_attempts == 1
    state = json.loads(opts.state_journal.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert state["schemaVersion"] == "northstar-ontology-bootstrap-v1"
    assert state["operations"]["ontology:create"]["state"] == "verified"
    assert state["operations"]["ontology:create"]["item"]["id"]
    assert state["operations"]["export"]["state"] == "completed"


def test_accepted_creation_resumes_exact_operation_without_second_post(tmp_path):
    fake = FakeOntologyClient()
    fake.accept_creation = True
    fake.fail_first_poll = True
    opts = options(tmp_path)
    with pytest.raises(BootstrapError, match="accepted operation interrupted"):
        run_bootstrap(
            opts,
            client=fake,
            input_func=lambda _: pytest.fail("poll failure must happen before prompts"),
            conformance=conformant,
        )
    assert fake.create_attempts == 1
    state = json.loads(opts.state_journal.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert state["operations"]["ontology:create"]["state"] == "accepted"
    assert state["operations"]["ontology:create"]["receipt"]["operationId"] == "ontology-1"

    answers = confirmations()
    run_bootstrap(
        opts,
        client=fake,
        input_func=lambda _: next(answers),
        conformance=conformant,
    )
    assert fake.create_attempts == 1
    assert fake.poll_attempts == 2


def test_unknown_create_outcome_recovers_exact_marker_and_blocks_duplicate_post(tmp_path):
    fake = FakeOntologyClient()
    fake.fail_create = True
    fake.created_without_response = False
    opts = options(tmp_path)
    with pytest.raises(BootstrapError, match="outcome is unknown"):
        run_bootstrap(opts, client=fake, input_func=lambda _: "", conformance=conformant)
    assert fake.create_attempts == 1

    with pytest.raises(BootstrapError, match="unknown.*another POST is blocked"):
        run_bootstrap(opts, client=fake, input_func=lambda _: "", conformance=conformant)
    assert fake.create_attempts == 1

    marker = deployment_marker(DEPLOYMENT_ID, "ontology")
    fake.fail_create = False
    recovered_id = synthetic_item_id(550)
    fake.items[recovered_id] = {
        "id": recovered_id,
        "displayName": "Northstar_Service_Assurance",
        "type": "Ontology",
        "description": marker,
        "workspaceId": WORKSPACE,
    }
    answers = confirmations()
    run_bootstrap(
        opts,
        client=fake,
        input_func=lambda _: next(answers),
        conformance=conformant,
    )
    assert fake.create_attempts == 1


def test_403_rejection_is_persisted_and_never_retried(tmp_path):
    fake = FakeOntologyClient()
    fake.reject_create = True
    opts = options(tmp_path)
    with pytest.raises(SubmissionRejectedError, match="HTTP 403"):
        run_bootstrap(opts, client=fake, input_func=lambda _: "", conformance=conformant)
    assert fake.create_attempts == 1
    state = json.loads(opts.state_journal.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert state["operations"]["ontology:create"]["state"] == "rejected"
    assert state["operations"]["ontology:create"]["status"] == 403
    with pytest.raises(BootstrapError, match="rejected on an earlier run"):
        run_bootstrap(opts, client=fake, input_func=lambda _: "", conformance=conformant)
    assert fake.create_attempts == 1


@pytest.mark.parametrize("submit_error", ["500", "identityless-202"])
def test_unexpected_submission_failure_stays_submitting_and_recovers_by_marker(
    tmp_path,
    submit_error,
):
    fake = FakeOntologyClient()
    fake.submit_error = submit_error
    opts = options(tmp_path)
    with pytest.raises(BootstrapError, match="outcome is unknown"):
        run_bootstrap(opts, client=fake, input_func=lambda _: "", conformance=conformant)
    assert fake.create_attempts == 1
    state = json.loads(opts.state_journal.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert state["operations"]["ontology:create"]["state"] == "submitting"

    fake.submit_error = None
    marker = deployment_marker(DEPLOYMENT_ID, "ontology")
    recovered_id = synthetic_item_id(552)
    fake.items[recovered_id] = {
        "id": recovered_id,
        "displayName": "Northstar_Service_Assurance",
        "type": "Ontology",
        "description": marker,
        "workspaceId": WORKSPACE,
    }
    answers = confirmations()
    result = run_bootstrap(
        opts,
        client=fake,
        input_func=lambda _: next(answers),
        conformance=conformant,
    )
    assert result.status == "BOOTSTRAPPED_AND_CONFORMANT"
    assert fake.create_attempts == 1


def test_azure_submit_only_marks_4xx_as_rejected(monkeypatch):
    client = object.__new__(AzureCliOntologyClient)
    body = {"displayName": "Northstar_Service_Assurance", "type": "Ontology"}

    for status in (500, 202, 307):
        headers = {} if status == 202 else None
        response = __import__("httpx").Response(status, json={}, headers=headers)
        monkeypatch.setattr(client, "_request", lambda *args, **kwargs: response)
        with pytest.raises(BootstrapError) as error:
            client.submit_item(
                WORKSPACE,
                body,
                item_type="Ontology",
                skill="ontology-cli",
            )
        assert not isinstance(error.value, SubmissionRejectedError)

    forbidden = __import__("httpx").Response(403, json={})
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: forbidden)
    with pytest.raises(SubmissionRejectedError) as error:
        client.submit_item(
            WORKSPACE,
            body,
            item_type="Ontology",
            skill="ontology-cli",
        )
    assert error.value.status == 403


def test_azure_cli_client_adapts_bootstrap_options_and_uses_labelled_auth_error(
    monkeypatch,
    tmp_path,
):
    completed = subprocess.CompletedProcess(["az"], 1, "", "not signed in")
    monkeypatch.setattr("telco_iq.deploy.client.subprocess.run", lambda *args, **kwargs: completed)
    opts = options(
        tmp_path,
        subscription_id="00000000-0000-0000-0000-000000000104",
        azure_cli=tmp_path / "az",
    )
    with pytest.raises(BootstrapError) as error:
        AzureCliOntologyClient(opts)
    message = str(error.value)
    assert "Scout | M5-02 ontology bootstrap / exact run" in message
    assert "Microsoft Fabric" in message
    assert TENANT in message
    assert USER in message
    assert "no sign-in was attempted" in message
    assert "AttributeError" not in message


def test_wrong_deployment_journal_target_fails_before_client(tmp_path):
    journal = tmp_path / "private" / "deployment.json"
    write_deployment_journal(journal, wrong_model="network")
    opts = BootstrapOptions(
        workspace="Northstar Synthetic Workspace",
        tenant_id=TENANT,
        expected_user=USER,
        deployment_journal=journal,
        state_journal=tmp_path / "private" / "bootstrap.json",
    )
    with pytest.raises(BootstrapError, match="model target differs"):
        run_bootstrap(opts, client=FakeOntologyClient(), conformance=conformant)


def test_wrong_requested_target_fails_before_client(tmp_path):
    opts = options(tmp_path, workspace="Different Workspace")
    with pytest.raises(BootstrapError, match="workspace does not match"):
        run_bootstrap(opts, client=FakeOntologyClient(), conformance=conformant)


def test_name_collision_is_never_adopted(tmp_path):
    fake = FakeOntologyClient()
    collision_id = synthetic_item_id(551)
    fake.items[collision_id] = {
        "id": collision_id,
        "displayName": "Northstar_Service_Assurance",
        "type": "Ontology",
        "description": "unrelated",
        "workspaceId": WORKSPACE,
    }
    with pytest.raises(BootstrapError, match="name collision"):
        run_bootstrap(options(tmp_path), client=fake, conformance=conformant)
    assert fake.create_attempts == 0


def test_pause_on_eof_and_resume_from_same_review_step(tmp_path):
    fake = FakeOntologyClient()
    opts = options(tmp_path)
    paused = run_bootstrap(
        opts,
        client=fake,
        input_func=lambda _: (_ for _ in ()).throw(EOFError),
        conformance=conformant,
    )
    assert paused.status == "PENDING_INPUT"
    assert paused.phase == "awaiting-model-step-1-review"
    state = json.loads(opts.state_journal.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert state["completedSteps"] == []
    assert fake.export_attempts == 0

    answers = confirmations()
    resumed = run_bootstrap(
        opts,
        client=fake,
        input_func=lambda _: next(answers),
        conformance=conformant,
    )
    assert resumed.status == "BOOTSTRAPPED_AND_CONFORMANT"
    assert fake.create_attempts == 1


def test_incorrect_apply_confirmation_pauses_without_success(tmp_path):
    values = iter(
        [
            "Northstar_Service_Assurance",
            "not the exact apply phrase",
        ],
    )
    result = run_bootstrap(
        options(tmp_path),
        client=FakeOntologyClient(),
        input_func=lambda _: next(values),
        conformance=conformant,
    )
    assert result.status == "PENDING_INPUT"
    assert result.phase == "awaiting-apply-step-1"


def test_export_and_conformance_failure_are_persisted_and_never_update(tmp_path):
    fake = FakeOntologyClient()
    opts = options(tmp_path)
    answers = confirmations()
    with pytest.raises(BootstrapError, match="non-conformant"):
        run_bootstrap(
            opts,
            client=fake,
            input_func=lambda _: next(answers),
            conformance=lambda *_: {
                "conformant": False,
                "summary": {"missing": 1, "extra": 0, "different": 0},
            },
        )
    assert fake.export_attempts == 1
    assert not hasattr(fake, "updateDefinition")
    state = json.loads(opts.state_journal.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert state["phase"] == "conformance-failed"
    assert state["completed"] is False
    assert state["operations"]["conformance"]["state"] == "failed"
    assert opts.export_path and opts.export_path.is_file()


def test_runner_fails_conformance_when_exported_metric_binds_to_wrong_model(
    tmp_path,
):
    definition = tmp_path / "private" / "definition"
    source = Path("tests/fixtures/ontology_conformance")
    definition.mkdir(parents=True)
    shutil.copytree(source, definition, dirs_exist_ok=True)
    metric_file = definition / "tables" / "network_incident.tmdl"
    metric_file.write_text(
        metric_file.read_text(encoding="utf-8").replace(
            "SYNTHETIC_NETWORK",
            "SYNTHETIC_FINANCE",
        ),
        encoding="utf-8",
    )
    fake = FakeOntologyClient()
    fake.export_source = definition
    opts = options(tmp_path)
    answers = confirmations()
    with pytest.raises(BootstrapError, match="non-conformant"):
        run_bootstrap(
            opts,
            client=fake,
            input_func=lambda _: next(answers),
        )
    state = json.loads(opts.state_journal.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert state["operations"]["conformance"]["state"] == "failed"
    assert any(
        "does not match expected source model 'network'" in reason
        for difference in state["operations"]["conformance"]["report"]["metrics"]["different"]
        for reason in difference["reasons"]
    )


def test_parser_exports_nested_integrator_surface():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    ontology_parser = build_ontology_parser(commands)
    args = parser.parse_args(
        [
            "ontology",
            "bootstrap",
            "--dry-run",
            "--deployment-journal",
            "private/deployment.json",
            "--subscription",
            "00000000-0000-0000-0000-000000000104",
        ],
    )
    assert args.command == "ontology"
    assert args.ontology_command == "bootstrap"
    assert args.ontology_handler.__name__ == "ontology_from_args"
    assert args.subscription_id == "00000000-0000-0000-0000-000000000104"
    assert ontology_parser.prog.endswith(" ontology")


def test_packaged_conformance_wrapper_checks_exported_definition():
    fixture = Path("tests/fixtures/ontology_conformance")
    report = check_exported_definition(load_contract(), fixture)
    assert report["conformant"] is True
    assert report["summary"] == {"missing": 0, "extra": 0, "different": 0}
    expected_models = {
        alias: {
            "displayName": f"Synthetic {alias}",
            "id": synthetic_item_id(index),
        }
        for index, alias in enumerate(HERO_MODELS, start=10)
    }
    with_models = check_exported_definition(
        load_contract(),
        fixture,
        expected_models=expected_models,
    )
    assert with_models["conformant"] is True


def test_runtime_boundary_has_no_tools_or_infra_imports():
    root = Path("src/telco_iq/ontology")
    imported = "\n".join(
        path.read_text(encoding="utf-8") for path in root.glob("*.py") if path.is_file()
    )
    assert "from tools" not in imported
    assert "import tools" not in imported
    assert "from infra" not in imported
    assert "import infra" not in imported
