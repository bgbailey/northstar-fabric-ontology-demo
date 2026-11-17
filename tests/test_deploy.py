from __future__ import annotations

import argparse
import base64
import copy
import io
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest

from telco_iq.deploy import (
    DeployOptions,
    FabricClient,
    bind_definition_parts,
    build_deploy_parser,
    run_deploy,
)
from telco_iq.deploy.assets import AssetBundle, SourceAsset, sha256
from telco_iq.deploy.client import (
    FABRIC_SCOPE,
    POWERBI_SCOPE,
    ApiReceipt,
    AzureCliFabricClient,
    SubmissionRejectedError,
)
from telco_iq.deploy.journal import DeploymentJournal
from telco_iq.deploy.models import HERO_MODELS, HERO_TABLES, PROFILE, SEED, DeployError

TENANT = "00000000-0000-0000-0000-000000000101"
WORKSPACE = "00000000-0000-0000-0000-000000000102"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000103"
USER = "synthetic@invalid.test"


class FakeFabricClient:
    tenant_id = TENANT
    user_name = USER
    subscription_id = SUBSCRIPTION

    def __init__(self):
        self.workspace = {
            "id": WORKSPACE,
            "displayName": "Northstar Synthetic Workspace",
            "capacityId": "00000000-0000-0000-0000-000000000103",
        }
        self.items: dict[str, dict] = {}
        self.definitions: dict[str, dict] = {}
        self.create_attempts = 0
        self.job_attempts = 0
        self.refresh_attempts = 0
        self.lro_attempts = 0
        self._next_id = 200
        self.fail_next_verify = False
        self.fail_create = False
        self.reject_create = False
        self.programming_error_create = False
        self.reject_job = False
        self.reject_refresh = False
        self.corrupt_readback_alias: str | None = None

    def resolve_workspace(self, selector: str) -> dict:
        assert selector == self.workspace["displayName"]
        return copy.deepcopy(self.workspace)

    def list_items(self, workspace_id: str, item_type: str) -> list[dict]:
        assert workspace_id == WORKSPACE
        return [
            copy.deepcopy(item)
            for item in self.items.values()
            if item["type"] == item_type
        ]

    def get_item(self, workspace_id: str, item_id: str, *, skill: str = "spark-cli") -> dict:
        return copy.deepcopy(self.items[item_id])

    def submit_item(
        self,
        workspace_id: str,
        body: dict,
        *,
        item_type: str,
        skill: str,
    ) -> ApiReceipt:
        self.create_attempts += 1
        if self.fail_create:
            raise TimeoutError("synthetic outcome is unknown")
        if self.reject_create:
            raise SubmissionRejectedError("Fabric item creation rejected with HTTP 400", 400)
        if self.programming_error_create:
            raise ValueError("synthetic programming bug")
        item_id = f"00000000-0000-0000-0000-{self._next_id:012}"
        self._next_id += 1
        item = {
            "id": item_id,
            "displayName": body["displayName"],
            "type": item_type,
            "description": body["description"],
            "workspaceId": workspace_id,
        }
        self.items[item_id] = item
        if "definition" in body:
            self.definitions[item_id] = copy.deepcopy(body["definition"])
        return ApiReceipt(
            "completed",
            201,
            body=copy.deepcopy(item),
            skill=skill,
        )

    def poll_lro(self, receipt: ApiReceipt, *, timeout_seconds: int) -> dict:
        self.lro_attempts += 1
        return {}

    def get_definition(
        self,
        workspace_id: str,
        item_id: str,
        *,
        item_type: str,
        definition_format: str,
        timeout_seconds: int,
    ) -> dict:
        definition = copy.deepcopy(self.definitions[item_id])
        if self.corrupt_readback_alias and any(
            item["id"] == item_id
            and self.corrupt_readback_alias in item["displayName"].lower()
            for item in self.items.values()
        ):
            definition["parts"][0]["payload"] = base64.b64encode(b"changed").decode("ascii")
        return {"definition": definition}

    def list_jobs(
        self,
        workspace_id: str,
        item_id: str,
        *,
        skill: str = "spark-cli",
    ) -> list[dict]:
        return []

    def submit_job(
        self,
        workspace_id: str,
        item_id: str,
        *,
        job_type: str,
        skill: str,
    ) -> ApiReceipt:
        self.job_attempts += 1
        if self.reject_job:
            raise SubmissionRejectedError("Fabric RunNotebook rejected with HTTP 403", 403)
        return ApiReceipt(
            "accepted",
            202,
            location=f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{item_id}/jobs/{job_type}",
            operation_id=f"job-{self.job_attempts}",
            retry_after="0",
            skill=skill,
        )

    def refresh_model(self, workspace_id: str, item_id: str) -> ApiReceipt:
        self.refresh_attempts += 1
        if self.reject_refresh:
            raise SubmissionRejectedError("Power BI refresh rejected with HTTP 400", 400)
        return ApiReceipt(
            "accepted",
            202,
            location=f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{item_id}/refresh",
            operation_id=f"refresh-{self.refresh_attempts}",
            retry_after="0",
            skill="semantic-model-authoring",
        )

    def poll_job(self, receipt: ApiReceipt, *, timeout_seconds: int) -> dict:
        return {"status": "Completed", "operationId": receipt.operation_id, "failureReason": None}

    def verify_item(
        self,
        workspace_id: str,
        item: dict,
        *,
        expected_name: str,
        expected_type: str,
        expected_marker: str,
    ) -> dict:
        if self.fail_next_verify:
            self.fail_next_verify = False
            raise DeployError("synthetic readback interruption")
        observed = self.get_item(workspace_id, item["id"])
        assert observed["displayName"] == expected_name
        assert observed["type"] == expected_type
        assert observed["description"] == expected_marker
        return observed


def asset_bundle() -> AssetBundle:
    models = {}
    tables = []
    for alias in HERO_MODELS:
        raw = (
            f"expression WorkspaceId = \"<WORKSPACE_ID>\"\n"
            f"expression LakehouseId = \"<LAKEHOUSE_ID>\"\n"
            f"table {alias}\n"
        ).encode()
        models[alias] = {
            "definition.pbism": b'{"version":"4.2"}',
            "definition/database.tmdl": b"database synthetic\n\tcompatibilityLevel: 1702",
            "definition/model.tmdl": f"model {alias}".encode(),
            "definition/expressions.tmdl": raw,
            f"definition/tables/{alias}.tmdl": f"table {alias}\n".encode(),
        }
        payload = (f"synthetic-{alias}").encode()
        tables.append(
            SourceAsset(
                table=alias,
                columns={"id": "int64"},
                primary_key=("id",),
                rows=1,
                file_sha256=sha256(payload),
                content_sha256=sha256(b"synthetic-content"),
                payload=base64.b64encode(payload).decode("ascii"),
                bytes=len(payload),
            ),
        )
    selected = [
        {
            "name": table.table,
            "rows": table.rows,
            "bytes": table.bytes,
            "fileSha256": table.file_sha256,
            "contentSha256": table.content_sha256,
        }
        for table in tables
    ]
    return AssetBundle(
        manifest={
            "profile": PROFILE,
            "seed": SEED,
            "revision": "service-assurance-v1.0",
            "tables": selected,
        },
        tables=tuple(tables),
        model_parts=models,
        source_digest=sha256(json.dumps(selected, sort_keys=True).encode()),
    )


def options(tmp_path: Path) -> DeployOptions:
    return DeployOptions(
        workspace="Northstar Synthetic Workspace",
        tenant_id=TENANT,
        expected_user=USER,
        journal_path=tmp_path / "private" / "deployment.json",
    )


def test_placeholder_binding_replaces_only_exact_tokens_and_preserves_source():
    source = {
        "definition/expressions.tmdl": (
            b'WorkspaceId = "<WORKSPACE_ID>"\n'
            b'LakehouseId = "<LAKEHOUSE_ID>"\n'
            b'Untouched = "<WORKSPACE_ID_EXTRA>"\n'
        ),
    }
    bound = bind_definition_parts(source, WORKSPACE, TENANT)
    assert source["definition/expressions.tmdl"].endswith(b'Untouched = "<WORKSPACE_ID_EXTRA>"\n')
    assert WORKSPACE.encode() in bound["definition/expressions.tmdl"]
    assert TENANT.encode() in bound["definition/expressions.tmdl"]
    assert b"<WORKSPACE_ID>" not in bound["definition/expressions.tmdl"]
    assert b"<LAKEHOUSE_ID>" not in bound["definition/expressions.tmdl"]
    assert b"<WORKSPACE_ID_EXTRA>" in bound["definition/expressions.tmdl"]


def test_dry_run_is_complete_offline_and_never_constructs_live_clients(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run attempted authentication, subprocess, network, or generation")

    monkeypatch.setattr("telco_iq.deploy.runner.AzureCliFabricClient", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    output = io.StringIO()
    result = run_deploy(
        DeployOptions(dry_run=True),
        asset_factory=forbidden,
        stdout=output,
    )
    plan = json.loads(output.getvalue())
    assert result.status == "DRY_RUN_OFFLINE"
    assert plan["workspace"]["selector"].startswith("<WORKSPACE")
    assert plan["data"]["profile"] == "service-assurance"
    assert plan["data"]["seed"] == 20260929
    assert plan["data"]["tables"] == list(HERO_TABLES)
    assert [model["alias"] for model in plan["semanticModels"]] == list(HERO_MODELS)
    assert plan["binding"]["substitutions"] == {
        "<WORKSPACE_ID>": "<resolved-workspace-id>",
        "<LAKEHOUSE_ID>": "<created-or-exactly-owned-lakehouse-id>",
    }
    assert any("never" in statement and "capacity" in statement for statement in plan["safety"])
    assert "TODO(verify-live)" in plan["todo"]


@pytest.mark.parametrize(
    "account_result",
    [
        subprocess.CompletedProcess([], 1, "", "not signed in"),
        subprocess.CompletedProcess(
            [],
            0,
            json.dumps({
                "id": SUBSCRIPTION,
                "tenantId": "00000000-0000-0000-0000-000000000199",
                "user": {"name": USER, "type": "user"},
            }),
            "",
        ),
        subprocess.CompletedProcess(
            [],
            0,
            json.dumps({
                "id": "00000000-0000-0000-0000-000000000199",
                "tenantId": TENANT,
                "user": {"name": USER, "type": "user"},
            }),
            "",
        ),
        subprocess.CompletedProcess(
            [],
            0,
            json.dumps({
                "id": SUBSCRIPTION,
                "tenantId": TENANT,
                "user": {"name": "other@invalid.test", "type": "user"},
            }),
            "",
        ),
    ],
    ids=["account-absent", "tenant-mismatch", "subscription-mismatch", "user-mismatch"],
)
def test_live_auth_fails_closed_without_launching_sign_in(monkeypatch, tmp_path, account_result):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return account_result

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DeployError) as error:
        AzureCliFabricClient(
            DeployOptions(
                workspace="Existing",
                tenant_id=TENANT,
                expected_user=USER,
                subscription_id=SUBSCRIPTION,
                azure_cli=Path("synthetic-az"),
                journal_path=tmp_path / "journal.json",
            ),
        )

    assert "no sign-in was attempted" in str(error.value)
    assert "Scout | M5-01 deployment / run northstar-m5-01:" in str(error.value)
    assert calls == [
        ["synthetic-az", "account", "show", "--output", "json", "--only-show-errors"],
    ]
    assert all("login" not in argument for command in calls for argument in command)


def test_journal_rejects_target_inside_repository_even_from_other_cwd(monkeypatch, tmp_path):
    repository = Path.cwd().resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    with pytest.raises(DeployError, match="outside every repository"):
        DeploymentJournal.validate_private_path(repository / "private" / "deployment.json")


def test_journal_rejects_target_inside_another_repository(tmp_path):
    other_repository = tmp_path / "other-repository"
    (other_repository / ".git").mkdir(parents=True)

    with pytest.raises(DeployError, match="outside every repository"):
        DeploymentJournal.validate_private_path(other_repository / "private" / "deployment.json")


@pytest.mark.parametrize("status", [400, 403])
def test_live_client_surfaces_definitive_http_rejection(status):
    client = object.__new__(AzureCliFabricClient)
    client._token = "fabric-token"
    client._expires = datetime.now(timezone.utc) + timedelta(minutes=10)

    def handler(request):
        assert request.url.host == "api.fabric.microsoft.com"
        assert request.headers["x-ms-fabric-skill"] == "spark-cli"
        return httpx.Response(status, json={"error": "definitive"})

    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    client._powerbi_client = httpx.Client()
    try:
        with pytest.raises(SubmissionRejectedError, match=f"HTTP {status}") as error:
            client.submit_item(
                WORKSPACE,
                {"displayName": "Rejected", "type": "Lakehouse"},
                item_type="Lakehouse",
                skill="spark-cli",
            )
        assert error.value.status == status
    finally:
        client.close()


def test_refresh_uses_powerbi_origin_endpoints_and_separate_token():
    client = object.__new__(AzureCliFabricClient)
    client._token = "fabric-token"
    client._expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    client._powerbi_token = "powerbi-token"
    client._powerbi_expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    calls = []
    model_id = str(uuid5(NAMESPACE_URL, "northstar:model"))
    request_id = "refresh-request-1"
    location = (
        f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE}/datasets/"
        f"{model_id}/refreshes/{request_id}"
    )

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.powerbi.com"
        assert "x-ms-fabric-skill" not in request.headers
        assert request.headers["Authorization"] == "Bearer powerbi-token"
        base = f"/v1.0/myorg/groups/{WORKSPACE}/datasets/{model_id}/refreshes"
        if request.method == "GET" and request.url.path == base and "$top" in request.url.params:
            assert request.url.params["$top"] == "10"
            return httpx.Response(200, json={"value": []})
        if request.method == "POST" and request.url.path == base:
            assert json.loads(request.content) == {"notifyOption": "NoNotification"}
            return httpx.Response(
                202,
                headers={
                    "Location": location,
                    "x-ms-request-id": request_id,
                    "Retry-After": "0",
                },
            )
        if request.method == "GET" and str(request.url) == location:
            return httpx.Response(
                200,
                json={"id": request_id, "requestId": request_id, "status": "Completed"},
            )
        raise AssertionError(f"unexpected Power BI request: {request.method} {request.url}")

    client._powerbi_client = httpx.Client(transport=httpx.MockTransport(handler))
    client._client = httpx.Client()
    try:
        receipt = client.refresh_model(WORKSPACE, model_id)
        result = client.poll_job(receipt, timeout_seconds=2)
        assert result["status"] == "Completed"
        assert [(call.method, call.url.host, call.url.path) for call in calls] == [
            ("GET", "api.powerbi.com", f"/v1.0/myorg/groups/{WORKSPACE}/datasets/{model_id}/refreshes"),
            ("POST", "api.powerbi.com", f"/v1.0/myorg/groups/{WORKSPACE}/datasets/{model_id}/refreshes"),
            ("GET", "api.powerbi.com", f"/v1.0/myorg/groups/{WORKSPACE}/datasets/{model_id}/refreshes/{request_id}"),
        ]
    finally:
        client.close()


def test_refresh_blocks_active_powerbi_history_without_post():
    client = object.__new__(AzureCliFabricClient)
    client._powerbi_token = "powerbi-token"
    client._powerbi_expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"value": [{"status": "Unknown", "requestId": "active"}]})

    client._powerbi_client = httpx.Client(transport=httpx.MockTransport(handler))
    client._client = httpx.Client()
    try:
        with pytest.raises(DeployError, match="active or unknown refresh"):
            client.refresh_model(WORKSPACE, "model")
        assert methods == ["GET"]
    finally:
        client.close()


def test_fabric_and_powerbi_tokens_are_cached_by_distinct_audience():
    client = object.__new__(AzureCliFabricClient)
    client.tenant_id = TENANT
    client._token = None
    client._expires = None
    client._powerbi_token = None
    client._powerbi_expires = None
    resources = []

    def token_request(*arguments):
        resource = arguments[arguments.index("--resource") + 1]
        resources.append(resource)
        return {"accessToken": resource + "-token", "expiresOn": "2099-01-01T00:00:00Z"}

    client._az_json = token_request
    assert client._ensure_token(FABRIC_SCOPE)[0] == FABRIC_SCOPE + "-token"
    assert client._ensure_token(FABRIC_SCOPE)[0] == FABRIC_SCOPE + "-token"
    assert client._ensure_token(POWERBI_SCOPE)[0] == POWERBI_SCOPE + "-token"
    assert client._ensure_token(POWERBI_SCOPE)[0] == POWERBI_SCOPE + "-token"
    assert resources == [FABRIC_SCOPE, POWERBI_SCOPE]


def test_auth_client_accepts_identity_options_without_subscription_field(monkeypatch):
    def account(self):
        return {
            "id": SUBSCRIPTION,
            "tenantId": TENANT,
            "user": {"name": USER, "type": "user"},
        }

    monkeypatch.setattr(AzureCliFabricClient, "_require_existing_account", account)
    legacy_options = SimpleNamespace(
        azure_cli=Path("synthetic-az"),
        poll_timeout_seconds=30,
        tenant_id=TENANT,
        expected_user=USER,
        workspace="Existing",
        deployment_tag="m5-01",
    )
    client = AzureCliFabricClient(legacy_options)
    try:
        assert client.tenant_id == TENANT
        assert client.user_name == USER
        assert client.expected_subscription is None
    finally:
        client.close()


def test_deploy_is_idempotent_with_persisted_journal(tmp_path):
    fake = FakeFabricClient()
    opts = options(tmp_path)
    bundle = asset_bundle()
    first = run_deploy(opts, client=fake, asset_bundle=bundle)
    assert first.status == "DEPLOYED_AND_VERIFIED"
    assert fake.create_attempts == 6
    assert fake.job_attempts == 1
    assert fake.refresh_attempts == 4
    attempts = (fake.create_attempts, fake.job_attempts, fake.refresh_attempts)

    second = run_deploy(opts, client=fake, asset_bundle=bundle)
    assert second.status == "DEPLOYED_AND_VERIFIED"
    assert (fake.create_attempts, fake.job_attempts, fake.refresh_attempts) == attempts
    journal = json.loads((tmp_path / "private" / "deployment.json").read_text(encoding="utf-8"))
    assert journal["completed"] is True
    assert journal["schemaVersion"] == "northstar-deploy-v1"
    assert journal["deploymentId"]
    assert journal["workspace"]["id"] == WORKSPACE
    assert set(journal["artifactDigests"]["models"]) == set(HERO_MODELS)
    assert journal["operations"]["job:notebook"]["receipt"]["location"]


def test_interrupted_create_readback_never_posts_again(tmp_path):
    fake = FakeFabricClient()
    fake.fail_next_verify = True
    opts = options(tmp_path)
    with pytest.raises(DeployError, match="readback interruption"):
        run_deploy(opts, client=fake, asset_bundle=asset_bundle())
    assert fake.create_attempts == 1

    run_deploy(opts, client=fake, asset_bundle=asset_bundle())
    assert fake.create_attempts == 6


def test_unknown_create_outcome_cannot_retry_post(tmp_path):
    fake = FakeFabricClient()
    fake.fail_create = True
    opts = options(tmp_path)
    with pytest.raises(DeployError, match="blocks a duplicate POST"):
        run_deploy(opts, client=fake, asset_bundle=asset_bundle())
    attempts = fake.create_attempts
    fake.fail_create = False
    with pytest.raises(DeployError, match="outcome.*unknown|found 0"):
        run_deploy(opts, client=fake, asset_bundle=asset_bundle())
    assert fake.create_attempts == attempts == 1


@pytest.mark.parametrize(
    ("mode", "attempt_field", "journal_key", "expected_attempts", "match"),
    [
        ("reject_create", "create_attempts", "lakehouse", 1, "HTTP 400"),
        ("reject_job", "job_attempts", "job:notebook", 1, "HTTP 403"),
        ("reject_refresh", "refresh_attempts", "refresh:network", 1, "HTTP 400"),
    ],
)
def test_definitive_rejection_is_journaled_and_never_resubmitted(
    tmp_path,
    mode,
    attempt_field,
    journal_key,
    expected_attempts,
    match,
):
    fake = FakeFabricClient()
    setattr(fake, mode, True)
    opts = options(tmp_path)
    with pytest.raises(SubmissionRejectedError, match=match):
        run_deploy(opts, client=fake, asset_bundle=asset_bundle())

    journal = json.loads(opts.journal_path.read_text(encoding="utf-8"))
    state = (
        journal["items"][journal_key]
        if journal_key == "lakehouse"
        else journal["operations"][journal_key]
    )
    assert state["state"] == "rejected"
    attempts = getattr(fake, attempt_field)
    assert attempts == expected_attempts

    with pytest.raises(DeployError, match="previously rejected"):
        run_deploy(opts, client=fake, asset_bundle=asset_bundle())
    assert getattr(fake, attempt_field) == attempts


def test_programming_error_during_submission_is_not_swallowed(tmp_path):
    fake = FakeFabricClient()
    fake.programming_error_create = True
    with pytest.raises(ValueError, match="programming bug"):
        run_deploy(options(tmp_path), client=fake, asset_bundle=asset_bundle())


def test_name_collision_fails_closed_without_adoption(tmp_path):
    fake = FakeFabricClient()
    collision_id = str(uuid5(NAMESPACE_URL, "northstar:m5-deploy:name-collision"))
    fake.items[collision_id] = {
        "id": collision_id,
        "displayName": "northstar_service_assurance_20260929",
        "type": "Lakehouse",
        "description": "unrelated item",
        "workspaceId": WORKSPACE,
    }
    with pytest.raises(DeployError, match="Name collision"):
        run_deploy(options(tmp_path), client=fake, asset_bundle=asset_bundle())
    assert fake.create_attempts == 0


def test_definition_readback_digest_mismatch_fails(tmp_path):
    fake = FakeFabricClient()
    fake.corrupt_readback_alias = "network"
    with pytest.raises(DeployError, match="readback digest differs"):
        run_deploy(options(tmp_path), client=fake, asset_bundle=asset_bundle())
    assert fake.create_attempts == 3
    assert fake.refresh_attempts == 0


def test_parser_exports_integrator_surface():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    deploy_parser = build_deploy_parser(subparsers)
    args = parser.parse_args(["deploy", "--dry-run", "--workspace", "Existing"])
    assert args.command == "deploy"
    assert args.deploy_handler is not None
    assert deploy_parser.prog.endswith(" deploy")


def test_protocol_boundary_has_no_infra_or_tools_runtime_imports():
    source = Path("src/telco_iq/deploy")
    imported = "\n".join(
        path.read_text(encoding="utf-8")
        for path in source.glob("*.py")
        if path.is_file()
    )
    assert "from infra" not in imported
    assert "import infra" not in imported
    assert "from tools" not in imported
    assert "import tools" not in imported


def test_fake_implements_public_protocol():
    client: FabricClient = FakeFabricClient()
    assert client.tenant_id == TENANT
