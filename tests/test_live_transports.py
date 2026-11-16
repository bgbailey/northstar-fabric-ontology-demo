"""Public subprocess/httpx interfaces only; synthetic profiles/tokens, no live CLI."""

import base64
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import test_hero_deployment as hero

from infra import hero_deploy as deploy
from infra import live_transports as live
from infra.powerbi_refresh import FABRIC_RESOURCE, POWERBI_RESOURCE
from infra.renewal import read_json

packet_dir = hero.packet_dir
no_live_io = hero.no_live_io
HTTP_CLIENT = httpx.Client


def config_file(private, **changes):
    executable, profile = private / "selected-private-az.cmd", private / "selected-private-az-profile"
    executable.write_text("synthetic test placeholder; never executed", encoding="utf-8")
    profile.mkdir(exist_ok=True)
    return Path(hero.save(private / "live-cli.json", {
        "expectedUser": "synthetic@invalid.test", "expectedTenant": hero.TENANT,
        "expectedSub": hero.SUBSCRIPTION, "azureCliExecutable": str(executable),
        "azureConfigDir": str(profile), **changes,
    })["file"])


def token(resource, *, claims=None, metadata=None):
    now = int(time.time())
    payload = {
        "aud": resource, "tid": hero.TENANT, "upn": "synthetic@invalid.test",
        "exp": now + 3600, "nbf": now - 10, "scp": "synthetic-reviewed-scope",
        **(claims or {}),
    }

    def encoded(body):
        return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")

    return {
        "accessToken": encoded({"alg": "RS256"}) + "." + encoded(payload) + ".synthetic-signature",
        "tokenType": "Bearer", "tenant": hero.TENANT, "subscription": hero.SUBSCRIPTION,
        "expires_on": payload["exp"], **(metadata or {}),
    }


@pytest.fixture
def mocked_public_interfaces(monkeypatch, packet_dir):
    service = hero.SyntheticService(packet_dir)
    calls = []
    identities = {
        "tenantId": hero.TENANT, "id": hero.SUBSCRIPTION,
        "user": {"name": "synthetic@invalid.test", "type": "user"},
        "environmentName": "AzureCloud", "state": "Enabled",
    }
    state = SimpleNamespace(service=service, calls=calls, identity=identities, claims={}, metadata={}, fail=False)

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert arguments[1:3] in (["account", "show"], ["account", "get-access-token"])
        assert kwargs["capture_output"] and kwargs["text"] and not kwargs["shell"]
        assert kwargs["timeout"] == 60 and arguments[-3:] == ["--output", "json", "--only-show-errors"]
        assert "--subscription" in arguments
        if state.fail:
            return SimpleNamespace(returncode=1, stdout="sensitive stdout must not escape", stderr="secret")
        if arguments[2] == "show":
            return SimpleNamespace(returncode=0, stdout=json.dumps(state.identity))
        assert "--resource" in arguments
        assert "--subscription" in arguments and "--tenant" not in arguments
        resource = arguments[arguments.index("--resource") + 1]
        return SimpleNamespace(returncode=0, stdout=json.dumps(token(
            resource, claims=state.claims, metadata=state.metadata,
        )))

    monkeypatch.setattr(live.subprocess, "run", run)
    monkeypatch.setattr(live.httpx, "Client", lambda **kwargs: HTTP_CLIENT(
        transport=httpx.MockTransport(service.handle), **kwargs,
    ))
    return state


def configure(private, packet_dir, **kwargs):
    config = config_file(private)
    change = kwargs.pop("alter_scope", None)

    def alter_scope(scope):
        scope["liveConfig"] = deploy.artifact(config)
        if change:
            change(scope)

    lease, scope_file = hero.configure(private, packet_dir, alter_scope=alter_scope, **kwargs)
    return lease, scope_file, config


def test_valid_single_factory_real_author_dispatch_and_private_environment(
    tmp_path, packet_dir, mocked_public_interfaces, monkeypatch,
):
    monkeypatch.setenv("AZURE_CONFIG_DIR", "ambient-profile-must-not-be-used")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "synthetic-ambient-secret-must-not-be-forwarded")
    lease, scope_file, config = configure(tmp_path, packet_dir)
    state = mocked_public_interfaces
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        ready = deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
        assert isinstance(ready.fabric, deploy.Author) and not state.service.sent
        assert [entry[0][2] for entry in state.calls] == ["show", "get-access-token"]
        receipt = ready.run()
        assert receipt["status"] == "INITIAL_CREATE_READ_BACK"
        assert receipt["proofLevel"] == "AUTHOR_SERVICE"  # Simulated label, NOT live proof.
        assert any(request.method == "POST" for request in state.service.sent)
        for arguments, kwargs in state.calls:
            assert arguments[0] == str(tmp_path / "selected-private-az.cmd")
            assert kwargs["env"]["AZURE_CONFIG_DIR"] == str(tmp_path / "selected-private-az-profile")
            assert "AZURE_CLIENT_SECRET" not in kwargs["env"]
        assert all(r.headers["Authorization"] == "Bearer " + factory.tokens.cache[FABRIC_RESOURCE]["accessToken"]
                   for r in state.service.sent)
        with pytest.raises(RuntimeError, match="reuse"):
            factory(ready.scope, lease)
    assert all(client.is_closed for client in factory.clients)
    assert not factory.tokens.cache


@pytest.mark.parametrize("name", ["SYNTHETIC@INVALID.TEST", "Synthetic@Invalid.Test"])
def test_token_user_case_does_not_change_pinned_tenant_resource_or_expiry(tmp_path, name):
    config = live.PrivateCliConfig(
        expectedUser="synthetic@invalid.test", expectedTenant=hero.TENANT,
        expectedSubscription=hero.SUBSCRIPTION, azureCliExecutable=str(tmp_path / "synthetic-az.cmd"),
        azureConfigDirectory=str(tmp_path / "synthetic-profile"),
    )
    result = token(FABRIC_RESOURCE, claims={"upn": name})
    assert live.token_expiry(result, FABRIC_RESOURCE, config, current=True) == result["expires_on"]
    with pytest.raises(RuntimeError, match="identity/audience/expiry"):
        live.token_expiry(result, POWERBI_RESOURCE, config, current=True)
    result = token(FABRIC_RESOURCE, claims={"upn": "another@invalid.test"})
    with pytest.raises(RuntimeError, match="identity/audience/expiry"):
        live.token_expiry(result, FABRIC_RESOURCE, config, current=True)


@pytest.mark.parametrize("delta", [-1, 1, -60, 60])
@pytest.mark.parametrize("text_metadata", [False, True])
def test_token_expiry_uses_earliest_cli_or_jwt_clock(tmp_path, delta, text_metadata):
    config = live.PrivateCliConfig(
        expectedUser="synthetic@invalid.test", expectedTenant=hero.TENANT,
        expectedSubscription=hero.SUBSCRIPTION, azureCliExecutable=str(tmp_path / "synthetic-az.cmd"),
        azureConfigDirectory=str(tmp_path / "synthetic-profile"),
    )
    result = token(FABRIC_RESOURCE)
    jwt_expiry = result["expires_on"]
    result["expires_on"] = str(jwt_expiry + delta) if text_metadata else jwt_expiry + delta
    assert live.token_expiry(result, FABRIC_RESOURCE, config, current=True) == min(jwt_expiry, jwt_expiry + delta)


@pytest.mark.parametrize("metadata", [None, True, False, "not-a-time", "1.5", "", -1, 1])
def test_token_expiry_invalid_or_expired_cli_clock_does_not_extend_valid_jwt(tmp_path, metadata):
    config = live.PrivateCliConfig(
        expectedUser="synthetic@invalid.test", expectedTenant=hero.TENANT,
        expectedSubscription=hero.SUBSCRIPTION, azureCliExecutable=str(tmp_path / "synthetic-az.cmd"),
        azureConfigDirectory=str(tmp_path / "synthetic-profile"),
    )
    result = token(FABRIC_RESOURCE, metadata={"expires_on": metadata})
    with pytest.raises(RuntimeError, match="identity/audience/expiry"):
        live.token_expiry(result, FABRIC_RESOURCE, config, current=True)


def test_actual_cli_and_token_user_case_preserve_selected_author_boundary(
    tmp_path, packet_dir, mocked_public_interfaces,
):
    lease, scope_file, config = configure(tmp_path, packet_dir)
    state = mocked_public_interfaces
    state.identity["user"]["name"] = "SYNTHETIC@INVALID.TEST"
    state.claims = {"upn": "Synthetic@Invalid.Test"}
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
        assert len(state.calls) == 2 and not state.service.sent
        assert factory.tokens.cache[FABRIC_RESOURCE]["tenant"] == hero.TENANT


@pytest.mark.parametrize("field,value", [
    ("tenantId", hero.COMPETING), ("id", hero.COMPETING),
    ("user", {"name": "other@invalid.test", "type": "user"}),
    ("user", {"name": "synthetic@invalid.test", "type": "servicePrincipal"}),
    ("environmentName", "AzureUSGovernment"), ("state", "Disabled"),
])
def test_actual_account_mismatch_stops_before_token_or_http(
    tmp_path, packet_dir, mocked_public_interfaces, field, value,
):
    lease, scope_file, config = configure(tmp_path, packet_dir)
    state = mocked_public_interfaces
    state.identity[field] = value
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        with pytest.raises(RuntimeError, match="Actual selected CLI"):
            deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
    assert len(state.calls) == 1 and not state.service.sent


@pytest.mark.parametrize("change", ["profile", "principal", "tenant", "subscription", "body", "code", "target", "protected"])
def test_static_approval_config_code_body_target_denials_precede_auth(
    tmp_path, packet_dir, mocked_public_interfaces, monkeypatch, change,
):
    config = config_file(tmp_path)
    if change == "profile":
        value = read_json(config)
        value["azureConfigDir"] += "-wrong"
        hero.save(config, value)

    def scope_change(scope):
        scope["liveConfig"] = deploy.artifact(config)
        if change in {"principal", "tenant", "subscription"}:
            field = {"principal": "authorUser", "tenant": "tenantId", "subscription": "subscriptionId"}[change]
            scope["principal"][field] = "other@invalid.test" if field == "authorUser" else hero.COMPETING
        if change == "target":
            scope["capacityId"] = hero.COMPETING

    def approval_change(approval):
        if change == "body":
            next(op for op in approval["operations"] if op["method"] == "POST")["bodySha256"] = "0" * 64
        if change == "protected":
            approval["ownedItems"]["hero-lakehouse"] = hero.PROTECTED

    with pytest.raises(RuntimeError):
        lease, scope_file = hero.configure(
            tmp_path, packet_dir, alter_scope=scope_change, alter_approval=approval_change,
        )
        if change == "code":
            original = Path.read_bytes

            def changed(path):
                return original(path) + (b"\n" if path == deploy.ROOT / "infra" / "live_transports.py" else b"")

            monkeypatch.setattr(Path, "read_bytes", changed)
        with live.LiveTransports(scope_file, config, stage="initial") as factory:
            deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
    assert not mocked_public_interfaces.calls and not mocked_public_interfaces.service.sent


def test_expiry_dryrun_unapproved_and_cli_stage_intent_never_authenticate(
    tmp_path, packet_dir, mocked_public_interfaces, monkeypatch, capsys,
):
    lease, scope_file, config = configure(tmp_path, packet_dir)
    argv = ["--private-dir", str(tmp_path), "--approval", str(lease.path), "--scope", str(scope_file)]
    deploy.main(["dry-run", *argv])
    assert "VALIDATED_DRY_RUN_ZERO_TRANSPORT" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="stage"):
        deploy.main(["execute", *argv, "--stage", "load", "--cli-config", str(config)])
    with pytest.raises(SystemExit):
        deploy.main(["execute", *argv, "--cli-config", str(config)])
    from infra import renewal

    original = renewal.datetime

    class Expired(original):
        @classmethod
        def now(cls, tz=None):
            return lease.deadline

    monkeypatch.setattr(renewal, "datetime", Expired)
    with pytest.raises(RuntimeError, match="expired"):
        deploy.main(["execute", *argv, "--stage", "initial", "--cli-config", str(config)])
    monkeypatch.setattr(renewal, "datetime", original)
    approval = read_json(lease.path)
    approval["approved"] = False
    hero.save(lease.path, approval)
    with pytest.raises(RuntimeError, match="confirmation absent"):
        deploy.main(["execute", *argv, "--stage", "initial", "--cli-config", str(config)])
    assert not mocked_public_interfaces.calls and not mocked_public_interfaces.service.sent


@pytest.mark.parametrize("claims,metadata", [
    ({"aud": POWERBI_RESOURCE}, {}), ({"tid": hero.COMPETING}, {}),
    ({"upn": "other@invalid.test"}, {}), ({"exp": 1}, {}), ({"nbf": 9999999999}, {}),
    ({"scp": ""}, {}), ({"idtyp": "app"}, {}), ({}, {"subscription": hero.COMPETING}),
    ({}, {"tenant": hero.COMPETING}), ({}, {"expires_on": 1}), ({}, {"tokenType": "Refresh"}),
    ({}, {"accessToken": "not-a-jwt"}),
])
def test_bad_acquired_token_stops_without_dispatch_or_secret_output(
    tmp_path, packet_dir, mocked_public_interfaces, claims, metadata, capsys,
):
    lease, scope_file, config = configure(tmp_path, packet_dir)
    state = mocked_public_interfaces
    state.claims, state.metadata = claims, metadata
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        with pytest.raises(RuntimeError, match="token identity"):
            deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
    assert not state.service.sent and len(state.calls) == 2
    assert not capsys.readouterr().out


def test_separate_audiences_cached_corruption_and_bad_refresh_stop(
    tmp_path, packet_dir, mocked_public_interfaces,
):
    lease, scope_file, config = configure(tmp_path, packet_dir)
    state = mocked_public_interfaces
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        ready = deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
        factory.tokens.get(POWERBI_RESOURCE)
        resources = [args[args.index("--resource") + 1] for args, _ in state.calls if "--resource" in args]
        assert resources == [FABRIC_RESOURCE, POWERBI_RESOURCE]
        assert [entry[0][2] for entry in state.calls] == ["show", "get-access-token"] * 2
        cached = copy.deepcopy(factory.tokens.cache[FABRIC_RESOURCE])
        factory.tokens.cache[FABRIC_RESOURCE] = token(POWERBI_RESOURCE)
        with pytest.raises(RuntimeError, match="token identity"):
            ready.fabric.request("GET", hero.PREFIX)
        assert not state.service.sent and len(state.calls) == 4
        factory.tokens.cache[FABRIC_RESOURCE] = token(FABRIC_RESOURCE, claims={"exp": int(time.time()) + 10})
        state.claims = {"tid": hero.COMPETING}
        with pytest.raises(RuntimeError, match="token identity"):
            ready.fabric.request("GET", hero.PREFIX)
        assert not state.service.sent and len(state.calls) == 6
        assert factory.tokens.cache[FABRIC_RESOURCE] != cached


@pytest.mark.parametrize("resource,url", [
    (FABRIC_RESOURCE, "https://api.powerbi.com/v1/workspaces"),
    (POWERBI_RESOURCE, FABRIC_RESOURCE + "/v1/workspaces"),
    (FABRIC_RESOURCE, "https://user@api.fabric.microsoft.com/v1/workspaces"),
    (FABRIC_RESOURCE, "http://api.fabric.microsoft.com/v1/workspaces"),
    (FABRIC_RESOURCE, FABRIC_RESOURCE + ":443/v1/workspaces"),
    (FABRIC_RESOURCE, FABRIC_RESOURCE + "/v1/../workspaces"),
    (FABRIC_RESOURCE, FABRIC_RESOURCE + "/v1/%2e/workspaces"),
    (FABRIC_RESOURCE, FABRIC_RESOURCE + "/v1/workspaces#fragment"),
    (FABRIC_RESOURCE, FABRIC_RESOURCE + "/v1//workspaces"),
    (FABRIC_RESOURCE, FABRIC_RESOURCE + "/v1.0/myorg/groups/test"),
    (POWERBI_RESOURCE, "https://user@api.powerbi.com/v1.0/myorg/groups/test"),
    (POWERBI_RESOURCE, "https://api.powerbi.com/v1/workspaces"),
])
def test_exact_audience_host_and_path_denials_precede_token_or_http(resource, url):
    def forbidden(*_, **__):
        pytest.fail("Rejected route reached tokens or HTTP")

    client = live.AudienceClient(SimpleNamespace(request=forbidden), SimpleNamespace(get=forbidden), resource)
    with pytest.raises(RuntimeError):
        client.request("GET", url)


def test_redirects_headers_and_service_200_errors_are_not_success(
    tmp_path, packet_dir, mocked_public_interfaces,
):
    lease, scope_file, config = configure(tmp_path, packet_dir)
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        ready = deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
        for options in ({"follow_redirects": True}, {"headers": {"Authorization": "different"}},
                        {"cookies": {"session": "unreviewed"}}):
            with pytest.raises(RuntimeError, match="options"):
                ready.fabric.client.request("GET", FABRIC_RESOURCE + hero.PREFIX, **options)
        sent = []
        for status, body in ((302, {}), (200, {"error": "failure"}), (200, {"value": [], "partial": True})):
            def handler(request):
                sent.append(request)
                return httpx.Response(status, json=body, headers={"Location": "https://unreviewed.invalid/"})

            with HTTP_CLIENT(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
                transport = live.AudienceClient(client, factory.tokens, FABRIC_RESOURCE)
                with pytest.raises(RuntimeError, match="redirect|partial"):
                    transport.request("GET", FABRIC_RESOURCE + hero.PREFIX)
        assert len(sent) == 3 and all(request.url.host == "api.fabric.microsoft.com" for request in sent)
    assert not mocked_public_interfaces.service.sent


@pytest.mark.parametrize("lro_polling", [None, {
    "strategy": "PUBLIC_OPERATION_ID",
    "observedLocationOrigin": "https://wabi-us-central-b-primary-redirect.analysis.windows.net",
}])
def test_unknown_post_with_actual_factory_is_never_retried(
    tmp_path, packet_dir, mocked_public_interfaces, lro_polling,
):
    lease, scope_file, config = configure(tmp_path, packet_dir, lro_polling=lro_polling)
    state = mocked_public_interfaces
    state.service.fail_post = True
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        ready = deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
        with pytest.raises(httpx.ReadTimeout):
            ready.run()
        assert lease._records()[0]["state"] == "RESERVED_OUTCOME_UNKNOWN"
        with pytest.raises(RuntimeError, match="unknown"):
            ready.run()
    assert sum(request.method == "POST" for request in state.service.sent) == 1


def test_execute_cli_reaches_actual_runner_and_does_not_print_private_ids(
    tmp_path, packet_dir, mocked_public_interfaces, capsys,
):
    lease, scope_file, config = configure(tmp_path, packet_dir)
    deploy.main([
        "execute", "--private-dir", str(tmp_path), "--approval", str(lease.path), "--scope", str(scope_file),
        "--stage", "initial", "--cli-config", str(config),
    ])
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "INITIAL_CREATE_READ_BACK"
    assert hero.WORKSPACE not in output and hero.IDS["hero-lakehouse"] not in output
    assert any(request.method == "POST" for request in mocked_public_interfaces.service.sent)


@pytest.fixture
def live_models_scope(tmp_path, packet_dir, mocked_public_interfaces, monkeypatch, request):
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    capacity_policy = getattr(request, "param", "DEDICATED_CAPACITY")
    lease, scope_file, config = configure(tmp_path, packet_dir, capacity_policy=capacity_policy)
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        assert deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory).run()["status"] == (
            "INITIAL_CREATE_READ_BACK"
        )
    bound = tmp_path / "bound"
    deploy.bind_owned(tmp_path, scope_file, lease, bound)
    ownership = {
        "workspace": lease.approval.ownershipReceipts["workspace"],
        "hero-lakehouse": deploy.ownership_spec(tmp_path, lease, "hero-lakehouse"),
    }
    lease, scope_file = hero.configure(
        tmp_path, packet_dir, phase="load", ownership=ownership, bound=bound,
        alter_scope=lambda value: value.update(liveConfig=deploy.artifact(config)),
        capacity_policy=capacity_policy,
    )
    with live.LiveTransports(scope_file, config, stage="load") as factory:
        result = deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory).run()
        assert result["status"] == "SOURCE_ASSERTIONS_REQUIRED"
    ownership["hero-loader"] = deploy.ownership_spec(tmp_path, lease, "hero-loader")
    accepted = next(path for path in lease.ledger.glob("*.json") if read_json(path)["operation"] == "run-loader")
    completed = next(path for path in tmp_path.glob("http-*.json") if (
        read_json(path)["method"] == "GET" and read_json(path)["path"].endswith("/jobs/instances/" + hero.JOB)
        and read_json(path)["response"]["status"] == "Completed"
    ))
    proof = {"authorization": deploy.artifact(lease.path), "accepted": deploy.artifact(accepted),
             "completed": deploy.artifact(completed)}
    packet = read_json(packet_dir / "packet.json")
    assertions = hero.save(tmp_path / "actual-shaped-synthetic-source-assertions.json", {
        "schemaVersion": "hero-source-assertions-v1", "proofLevel": "AUTHOR_SERVICE",
        "packetSha256": deploy.sha256((packet_dir / "packet.json").read_bytes()),
        "populationSha256": deploy.body_digest(packet["population"]), "lakehouseId": hero.IDS["hero-lakehouse"],
        "loaderId": hero.IDS["hero-loader"], "jobId": hero.JOB, "jobReceiptSha256": proof["completed"]["sha256"],
        "notebookBodySha256": deploy.body_digest(read_json(bound / "notebook-template.json")),
        "observedAtUtc": hero.datetime.now(hero.timezone.utc).isoformat(),
        "tables": [{"name": table["name"], "rows": table["rows"], "expectedFileSha256": table["fileSha256"],
                    "exactDataReadback": True, "keyChecks": "PASSED", "missingExpectedRows": 0,
                    "unexpectedRows": 0} for table in packet["tables"]],
    })

    def models_scope():
        return hero.configure(
            tmp_path, packet_dir, phase="models", ownership=ownership, bound=bound, proof=proof,
            assertions=assertions, alter_scope=lambda value: value.update(liveConfig=deploy.artifact(config)),
            capacity_policy=capacity_policy,
        )

    return SimpleNamespace(
        private=tmp_path, config=config, create_scope=models_scope, proof=proof,
        assertions=assertions, state=mocked_public_interfaces, packet=packet,
        capacity_policy=capacity_policy,
    )


@pytest.mark.parametrize("live_models_scope", ["DEDICATED_CAPACITY", "SHARED_CAPACITY_AS_IS"], indirect=True)
def test_actual_factory_all_r4_stages_complete_full_parts_refresh_and_controls(live_models_scope):
    setup = live_models_scope
    lease, scope_file = setup.create_scope()
    with live.LiveTransports(scope_file, setup.config, stage="models") as factory:
        ready = deploy.HeroRunner(setup.private, scope_file, lease, transports=factory)
        report = ready.run()
        assert report["status"] == "THREE_SOURCE_QUERY_CONTROLS_PASSED"
        assert set(report["models"]) == {"enterprise", "finance", "network"}
        assert setup.packet["rows"] == 7181 and len(setup.packet["tables"]) == 18
        expected_parts = {"enterprise": 16, "finance": 13, "network": 14}
        for model, count in expected_parts.items():
            assert len(setup.state.service.bodies[hero.IDS[model]]["definition"]["parts"]) == count
        resources = [args[args.index("--resource") + 1] for args, _ in setup.state.calls if "--resource" in args]
        assert resources == [FABRIC_RESOURCE] * 3 + [POWERBI_RESOURCE]
        for request in setup.state.service.sent:
            resource = FABRIC_RESOURCE if request.url.host == "api.fabric.microsoft.com" else POWERBI_RESOURCE
            raw = request.headers["Authorization"].removeprefix("Bearer ").split(".")[1]
            assert json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))["aud"] == resource
        assert sum(r.url.path.endswith("/refreshes") and r.method == "POST" for r in setup.state.service.sent) == 3
        assert sum(r.url.path.endswith("/executeQueries") for r in setup.state.service.sent) == 3
        assert all(result["completeDefinitionReadback"] == "EXACT_APPROVED_PARTS"
                   for result in report["models"].values())
        assert report["ontologyClock"] is None and report["contextCacheClock"] is None
        assert report["lifecycle"]["capacityPolicy"] == setup.capacity_policy
        if setup.capacity_policy == "SHARED_CAPACITY_AS_IS":
            assert not list(setup.private.glob("*-shutdown.json"))
            assert report["lifecycle"]["artifactDisposition"] == "RETAIN"
            assert report["lifecycle"]["proofLevel"] == "APPROVED_MANUAL_OBLIGATIONS_NOT_AUTOMATION_PROOF"
            assert report["lifecycle"]["automaticJobCancellation"] == "NOT_IMPLEMENTED"
            with pytest.raises(RuntimeError, match="no capacity suspension"):
                lease.verify_shutdown_guard()
            for stage in setup.private.glob("hero-stage-*.json"):
                assert read_json(stage)["lifecycle"]["capacityPolicy"] == "SHARED_CAPACITY_AS_IS"
            assert not any(
                word in str(request.url).lower()
                for request in setup.state.service.sent
                for word in ("suspend", "resume", "assign", "cancel")
            )


@pytest.mark.parametrize("kind", ["source-assertion", "source-proof"])
def test_approved_but_incomplete_source_proof_blocks_factory_before_auth(live_models_scope, kind):
    setup = live_models_scope
    if kind == "source-assertion":
        path = Path(setup.assertions["file"])
        value = read_json(path)
        value["tables"][0]["exactDataReadback"] = False
        setup.assertions.update(hero.save(path, value))
    else:
        path = Path(setup.proof["completed"]["file"])
        value = read_json(path)
        value["response"]["status"] = "InProgress"
        setup.proof["completed"] = hero.save(path, value)
    lease, scope_file = setup.create_scope()
    calls, requests = len(setup.state.calls), len(setup.state.service.sent)
    with live.LiveTransports(scope_file, setup.config, stage="models") as factory:
        with pytest.raises(RuntimeError, match="Source assertion"):
            deploy.HeroRunner(setup.private, scope_file, lease, transports=factory)
        assert not factory.constructed
    assert len(setup.state.calls) == calls and len(setup.state.service.sent) == requests


def test_private_schema_expected_aliases_unknown_fields_and_unpinned_config_deny(
    tmp_path, packet_dir, mocked_public_interfaces,
):
    lease, scope_file, config = configure(tmp_path, packet_dir)
    scope = deploy.HeroScope.model_validate(read_json(scope_file))
    value = read_json(config)
    assert live.PrivateCliConfig.model_validate(value).expectedSubscription == hero.SUBSCRIPTION
    value["unexpectedSecret"] = "synthetic-do-not-print"
    hero.save(config, value)
    with pytest.raises(RuntimeError, match="schema invalid") as error:
        live.validate_config(config, scope, lease)
    assert "synthetic-do-not-print" not in str(error.value)
    assert not mocked_public_interfaces.calls and not mocked_public_interfaces.service.sent


def test_cli_failure_is_sanitized_without_auth_fallback(tmp_path, packet_dir, mocked_public_interfaces):
    lease, scope_file, config = configure(tmp_path, packet_dir)
    mocked_public_interfaces.fail = True
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        with pytest.raises(RuntimeError, match="Selected CLI command failed") as error:
            deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
        assert "sensitive stdout" not in str(error.value) and "secret" not in str(error.value)
    assert len(mocked_public_interfaces.calls) == 1 and not mocked_public_interfaces.service.sent


def test_unpinned_private_cli_argument_is_not_a_fallback(tmp_path, packet_dir, mocked_public_interfaces):
    lease, scope_file, config = configure(tmp_path, packet_dir)
    other = tmp_path / "unapproved-cli.json"
    other.write_bytes(config.read_bytes())
    with live.LiveTransports(scope_file, other, stage="initial") as factory:
        with pytest.raises(RuntimeError, match="configuration"):
            deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
    assert not mocked_public_interfaces.calls and not mocked_public_interfaces.service.sent


@pytest.mark.parametrize("kind", [
    "missing", "unapproved", "owner", "deadline", "capacity-id", "capacity-resource",
    "scope-mode", "disposition", "fake-automation", "suspension-conflict",
])
def test_invalid_shared_policy_blocks_before_authentication(
    tmp_path, packet_dir, mocked_public_interfaces, kind,
):
    def change(approval):
        policy = approval["sharedCapacityWorkload"]
        if kind == "missing":
            approval["sharedCapacityWorkload"] = None
        elif kind == "unapproved":
            policy["approved"] = False
        elif kind == "owner":
            policy["owner"] = "other owner"
        elif kind == "deadline":
            policy["notAfterUtc"] = "2000-01-01T00:00:00Z"
        elif kind == "capacity-id":
            policy["capacityId"] = hero.COMPETING
        elif kind == "capacity-resource":
            policy["capacityResourceId"] += "-different"
        elif kind == "disposition":
            policy["artifactDisposition"] = "DELETE"
        elif kind == "fake-automation":
            policy["enforcement"] = "HOSTED_STOP_CONTROLLER_ACTIVE"
        elif kind == "suspension-conflict":
            approval["shutdown"] = {
                "approvalReference": "SYNTHETIC", "capacityResourceId": policy["capacityResourceId"],
                "scheduleResourceId": "SYNTHETIC", "runbookName": "SyntheticPause",
                "runbookSha256": "a" * 64, "receiptFile": "must-not-be-needed.json",
            }

    with pytest.raises(RuntimeError):
        lease, scope_file, config = configure(
            tmp_path, packet_dir, capacity_policy="SHARED_CAPACITY_AS_IS", alter_approval=change,
            alter_scope=lambda value: value.update(capacityPolicy="DEDICATED_CAPACITY")
            if kind == "scope-mode" else None,
        )
        with live.LiveTransports(scope_file, config, stage="initial") as factory:
            deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
    assert not mocked_public_interfaces.calls and not mocked_public_interfaces.service.sent


def test_shared_policy_cannot_extend_expired_finite_stage_or_authorize_capacity_mutation(
    tmp_path, packet_dir, mocked_public_interfaces, monkeypatch,
):
    lease, scope_file, config = configure(tmp_path, packet_dir, capacity_policy="SHARED_CAPACITY_AS_IS")
    assert lease.window["shutdownGuard"] == "NOT_APPLICABLE_SHARED_MANUAL_OWNER"
    assert lease.approval.shutdown is None
    assert lease.verify_lifecycle_policy()["jobStopsByThisExecutor"] == "NONE_AUTHORIZED"
    from infra import renewal

    original = renewal.datetime

    class Expired(original):
        @classmethod
        def now(cls, tz=None):
            return lease.deadline

    monkeypatch.setattr(renewal, "datetime", Expired)
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        with pytest.raises(RuntimeError, match="expired"):
            deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
    assert not mocked_public_interfaces.calls and not mocked_public_interfaces.service.sent


@pytest.mark.parametrize("method,path", [
    ("POST", hero.PREFIX + "/assignToCapacity"),
    ("POST", hero.PREFIX + "/unassignFromCapacity"),
    ("DELETE", hero.PREFIX + "/items/{hero-lakehouse}"),
    ("POST", hero.PREFIX + "/items/{hero-lakehouse}/jobs/instances/" + hero.JOB + "/cancel"),
])
def test_shared_workload_retains_artifacts_and_does_not_authorize_job_stop_or_capacity_routes(
    tmp_path, packet_dir, mocked_public_interfaces, method, path,
):
    def change(approval):
        approval["operations"].append({
            "name": "forbidden-shared-operation", "method": method, "path": path,
            "bodySha256": deploy.body_digest(None),
        })

    with pytest.raises(RuntimeError, match="Shared workload"):
        configure(tmp_path, packet_dir, capacity_policy="SHARED_CAPACITY_AS_IS", alter_approval=change)
    assert not mocked_public_interfaces.calls and not mocked_public_interfaces.service.sent


@pytest.mark.parametrize("outcome", ["owned", "protected-id", "wrong-workspace"])
def test_public_id_factory_follows_only_public_urls_and_preserves_exact_created_ownership(
    tmp_path, packet_dir, mocked_public_interfaces, monkeypatch, outcome,
):
    origin = "https://wabi-us-central-b-primary-redirect.analysis.windows.net"
    policy = {"strategy": "PUBLIC_OPERATION_ID", "observedLocationOrigin": origin}
    lease, scope_file, config = configure(
        tmp_path, packet_dir, lro_polling=policy, capacity_policy="SHARED_CAPACITY_AS_IS",
    )
    state = mocked_public_interfaces
    state.service.async_alias = "hero-lakehouse"
    state.service.location_override = origin + "/v1/operations/" + hero.OPERATION
    if outcome == "protected-id":
        state.service.returned_id = hero.PROTECTED
    original = state.service.handle

    def handler(request):
        response = original(request)
        if request.url.path == "/v1/operations/" + hero.OPERATION:
            body = response.json() | {"error": None}
            headers = {"x-ms-operation-id": hero.OPERATION, "Retry-After": "0"}
            if body["status"] == "Succeeded":
                headers["Location"] = origin + "/v1/operations/" + hero.OPERATION + "/result"
            return httpx.Response(200, json=body, headers=headers)
        if request.url.path.endswith("/result") and outcome == "wrong-workspace":
            return httpx.Response(200, json=response.json() | {"workspaceId": hero.COMPETING})
        return response

    state.service.handle = handler
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        ready = deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
        if outcome == "owned":
            assert ready.run()["status"] == "INITIAL_CREATE_READ_BACK"
            spec = deploy.ownership_spec(tmp_path, lease, "hero-lakehouse")
            assert deploy.verified_ownership(tmp_path, spec)["id"] == hero.IDS["hero-lakehouse"]
            assert lease._records()[0]["location"] == state.service.location_override
        else:
            with pytest.raises(RuntimeError, match="existing/protected|escaped"):
                ready.run()
            assert not lease._derived()[0]
    assert sum(request.method == "POST" for request in state.service.sent) == 1
    assert all(request.url.host == "api.fabric.microsoft.com" for request in state.service.sent)


def test_public_id_strategy_must_match_explicit_approval_before_auth(
    tmp_path, packet_dir, mocked_public_interfaces,
):
    lease, scope_file, config = configure(
        tmp_path, packet_dir,
        alter_scope=lambda scope: scope.update(lroPolling={
            "strategy": "PUBLIC_OPERATION_ID",
            "observedLocationOrigin": "https://wabi-us-central-b-primary-redirect.analysis.windows.net",
        }),
    )
    with live.LiveTransports(scope_file, config, stage="initial") as factory:
        with pytest.raises(RuntimeError, match="LRO polling strategy"):
            deploy.HeroRunner(tmp_path, scope_file, lease, transports=factory)
    assert not mocked_public_interfaces.calls and not mocked_public_interfaces.service.sent
