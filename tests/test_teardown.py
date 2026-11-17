from __future__ import annotations

import argparse
import ast
import copy
import io
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from telco_iq.deploy.models import (
    HERO_MODELS,
    LAKEHOUSE_NAME,
    MODEL_DISPLAY_NAMES,
    NOTEBOOK_NAME,
    deployment_marker,
)
from telco_iq.teardown import (
    STATUS,
    ReadOnlyFabricClient,
    TeardownError,
    TeardownOptions,
    build_teardown_parser,
    run_teardown,
)

TENANT = "00000000-0000-0000-0000-000000000101"
WORKSPACE = "00000000-0000-0000-0000-000000000102"
CAPACITY = "00000000-0000-0000-0000-000000000103"
USER = "synthetic@invalid.test"
WORKSPACE_NAME = "Northstar Synthetic Workspace"
DEPLOYMENT_ID = "northstar-m5-01:0123456789abcdef"


class FakeReadOnlyClient:
    tenant_id = TENANT
    user_name = USER

    def __init__(self, items: list[dict] | None = None):
        self.workspace = {
            "id": WORKSPACE,
            "displayName": WORKSPACE_NAME,
            "capacityId": CAPACITY,
        }
        self.items = items or deployment_items()
        self.calls: list[tuple] = []
        self.request_calls: list[tuple] = []

    def resolve_workspace(self, selector: str) -> dict:
        self.calls.append(("resolve_workspace", selector))
        return copy.deepcopy(self.workspace)

    def list_items(self, workspace_id: str, item_type: str) -> list[dict]:
        self.calls.append(("list_items", workspace_id, item_type))
        return [
            copy.deepcopy(item)
            for item in self.items
            if item["type"] == item_type
        ]

    def get_item(
        self,
        workspace_id: str,
        item_id: str,
        *,
        skill: str = "spark-cli",
    ) -> dict:
        self.calls.append(("get_item", workspace_id, item_id, skill))
        return copy.deepcopy(next(item for item in self.items if item["id"] == item_id))

    def request(self, method: str, path: str, **kwargs):
        self.request_calls.append((method, path, kwargs))
        if method.upper() == "DELETE":
            raise AssertionError("the teardown command attempted DELETE")
        raise AssertionError("the teardown command called an unexpected HTTP method")


def deployment_items() -> list[dict]:
    specs = [
        ("lakehouse", "Lakehouse", LAKEHOUSE_NAME, 200),
        ("notebook", "Notebook", NOTEBOOK_NAME, 201),
        *[
            (f"model-{alias}", "SemanticModel", MODEL_DISPLAY_NAMES[alias], 202 + index)
            for index, alias in enumerate(HERO_MODELS)
        ],
    ]
    return [
        {
            "id": f"00000000-0000-0000-0000-{suffix:012}",
            "workspaceId": WORKSPACE,
            "displayName": name,
            "type": item_type,
            "description": deployment_marker(DEPLOYMENT_ID, alias),
        }
        for alias, item_type, name, suffix in specs
    ]


def deployment_journal() -> dict:
    aliases = {
        "lakehouse": (LAKEHOUSE_NAME, "Lakehouse"),
        "notebook": (NOTEBOOK_NAME, "Notebook"),
        **{
            f"model-{alias}": (MODEL_DISPLAY_NAMES[alias], "SemanticModel")
            for alias in HERO_MODELS
        },
    }
    items = {item["displayName"]: item for item in deployment_items()}
    digest = "a" * 64
    artifact_digests = {
        "source": digest,
        "dataManifest": digest,
        "models": {
            alias: {"body": digest, "definition": digest, "name": MODEL_DISPLAY_NAMES[alias]}
            for alias in HERO_MODELS
        },
        "tables": [{"name": "synthetic", "contentSha256": digest}],
        "lakehouse": digest,
        "notebook": {"body": digest, "content": digest},
    }
    return {
        "schemaVersion": "northstar-deploy-v1",
        "deploymentId": DEPLOYMENT_ID,
        "tag": "m5-01",
        "profile": "service-assurance",
        "seed": 20260929,
        "tenant": {"id": TENANT, "user": USER},
        "workspace": {
            "id": WORKSPACE,
            "displayName": WORKSPACE_NAME,
            "capacityId": CAPACITY,
        },
        "items": {
            alias: {
                "state": "verified",
                "item": copy.deepcopy(items[name]),
                "receipt": {"state": "completed", "status": 201},
                "artifactDigest": digest,
            }
            for alias, (name, _item_type) in aliases.items()
        },
        "operations": {},
        "artifactDigests": artifact_digests,
        "completed": True,
    }


def write_journal(tmp_path: Path, journal: dict | None = None) -> Path:
    path = tmp_path / "private" / "deployment.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(journal or deployment_journal(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def options(path: Path, **changes) -> TeardownOptions:
    values = {
        "workspace": WORKSPACE_NAME,
        "tenant_id": TENANT,
        "expected_user": USER,
        "deployment_tag": "m5-01",
        "confirm": WORKSPACE_NAME,
        "journal_path": path,
    }
    values.update(changes)
    return TeardownOptions(**values)


@pytest.mark.parametrize("tag_state", ["missing", "wrong-journal", "wrong-request"])
def test_missing_or_wrong_creation_tag_is_refused(tmp_path, tag_state):
    journal = deployment_journal()
    request_tag = "m5-01"
    if tag_state == "missing":
        journal.pop("tag")
    elif tag_state == "wrong-journal":
        journal["tag"] = "other"
    else:
        request_tag = "other"
    path = write_journal(tmp_path, journal)
    fake = FakeReadOnlyClient()

    with pytest.raises(TeardownError, match="tag|schema"):
        run_teardown(options(path, deployment_tag=request_tag), client=fake)

    assert fake.calls == []


def test_incomplete_journal_is_refused_before_client_use(tmp_path):
    journal = deployment_journal()
    journal["completed"] = False
    fake = FakeReadOnlyClient()

    with pytest.raises(TeardownError, match="incomplete"):
        run_teardown(options(write_journal(tmp_path, journal)), client=fake)

    assert fake.calls == []


@pytest.mark.parametrize("field", ["id", "displayName", "capacityId"])
def test_actual_workspace_mismatch_fails_closed(tmp_path, field):
    fake = FakeReadOnlyClient()
    if field == "id":
        fake.workspace[field] = str(uuid5(NAMESPACE_URL, "northstar:m5-teardown:workspace-id"))
    elif field == "displayName":
        fake.workspace[field] = "A Different Workspace"
    else:
        fake.workspace[field] = str(uuid5(NAMESPACE_URL, "northstar:m5-teardown:capacity-id"))

    with pytest.raises(TeardownError, match="workspace identity"):
        run_teardown(options(write_journal(tmp_path)), client=fake)

    assert fake.calls == [("resolve_workspace", WORKSPACE_NAME)]


def test_exact_confirmation_is_required_before_inspection(tmp_path):
    path = write_journal(tmp_path)
    fake = FakeReadOnlyClient()

    with pytest.raises(TeardownError, match="Confirmation|confirm"):
        run_teardown(options(path, confirm="A different display name"), client=fake)
    assert fake.calls == []

    with pytest.raises(TeardownError, match="--confirm"):
        TeardownOptions(
            workspace=WORKSPACE_NAME,
            tenant_id=TENANT,
            expected_user=USER,
            journal_path=path,
        )


def test_name_only_untagged_item_never_qualifies(tmp_path):
    fake = FakeReadOnlyClient()
    fake.items[0]["description"] = "created by somebody else"

    with pytest.raises(TeardownError, match="marker drifted"):
        run_teardown(options(write_journal(tmp_path)), client=fake)

    assert all(call[0] != "request" for call in fake.calls)
    assert fake.request_calls == []


@pytest.mark.parametrize("field", ["id", "displayName", "type", "description"])
def test_live_item_id_name_type_and_marker_drift_fails_closed(tmp_path, field):
    fake = FakeReadOnlyClient()
    changed = fake.items[0]
    if field == "id":
        changed[field] = str(uuid5(NAMESPACE_URL, "northstar:m5-teardown:item-id"))
    elif field == "displayName":
        changed[field] = "A same-name substitute does not exist"
    elif field == "type":
        changed[field] = "Notebook"
    else:
        changed[field] = "untagged"

    with pytest.raises(TeardownError, match="Inventory mismatch"):
        run_teardown(options(write_journal(tmp_path)), client=fake)

    assert fake.request_calls == []


def test_journal_digest_or_marker_drift_is_refused_before_client_use(tmp_path):
    journal = deployment_journal()
    journal["items"]["lakehouse"]["artifactDigest"] = "b" * 64
    fake = FakeReadOnlyClient()
    with pytest.raises(TeardownError, match="digest reference"):
        run_teardown(options(write_journal(tmp_path, journal)), client=fake)
    assert fake.calls == []

    journal = deployment_journal()
    journal["items"]["lakehouse"]["item"]["description"] = "untagged"
    with pytest.raises(TeardownError, match="marker differs"):
        run_teardown(options(write_journal(tmp_path / "other", journal)), client=fake)
    assert fake.calls == []


def test_exact_inventory_is_read_only_and_reports_deletion_refusal(tmp_path):
    fake = FakeReadOnlyClient()
    output = io.StringIO()
    result = run_teardown(options(write_journal(tmp_path)), client=fake, stdout=output)
    payload = json.loads(output.getvalue())

    assert result.status == payload["status"] == STATUS
    assert result.workspace == {"id": WORKSPACE, "displayName": WORKSPACE_NAME}
    assert [item["alias"] for item in result.items] == [
        "lakehouse",
        "notebook",
        "model-network",
        "model-enterprise",
        "model-finance",
        "model-care",
    ]
    assert all(item["marker"].startswith("Northstar service-assurance;") for item in result.items)
    assert payload["mutation"] == "none"
    assert "disabled by safety contract" in payload["deletion"]
    assert "TODO(reconcile): M5-03" in payload["todo"]
    assert fake.request_calls == []


def test_dry_run_never_constructs_client_or_uses_network(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run constructed or called a live client")

    monkeypatch.setattr("telco_iq.teardown._ReadOnlyAzureCliFabricClient", forbidden)
    output = io.StringIO()

    result = run_teardown(
        TeardownOptions(
            workspace=WORKSPACE_NAME,
            deployment_tag="m5-01",
            dry_run=True,
            journal_path=tmp_path / "missing-private-journal.json",
        ),
        stdout=output,
    )

    plan = json.loads(output.getvalue())
    assert result.status == plan["status"] == STATUS
    assert plan["dryRun"] is True
    assert plan["mutation"] == "none"
    assert len(plan["expectedItems"]) == 6


def test_parser_exports_integrator_surface_without_mutating_flags():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    teardown_parser = build_teardown_parser(subparsers)
    args = parser.parse_args(
        [
            "teardown",
            "--workspace",
            WORKSPACE_NAME,
            "--tenant",
            TENANT,
            "--expected-user",
            USER,
            "--confirm",
            WORKSPACE_NAME,
        ],
    )
    assert args.command == "teardown"
    assert args.teardown_handler is not None
    assert teardown_parser.prog.endswith(" teardown")

    for flag in ("--force", "--delete", "--delete-items"):
        with pytest.raises(SystemExit):
            parser.parse_args(["teardown", flag])


def test_protocol_and_module_have_no_mutation_surface():
    annotations = getattr(ReadOnlyFabricClient, "__annotations__", {})
    assert set(annotations) == {"tenant_id", "user_name"}
    protocol_methods = {
        name
        for name in ReadOnlyFabricClient.__dict__
        if not name.startswith("_") and callable(getattr(ReadOnlyFabricClient, name))
    }
    assert protocol_methods == {"resolve_workspace", "list_items", "get_item"}

    source = Path("src/telco_iq/teardown.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {
        "request",
        "submit",
        "submit_item",
        "delete_item",
        "remove_item",
        "pause",
        "resume",
        "resize",
        "reassign",
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(forbidden)
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/v1/")
        for node in ast.walk(tree)
    )
