"""Offline packet completeness, source coherence and private binding; never Fabric."""

import ast
import base64
import hashlib
import io
import json
import shutil
import socket
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from infra.renewal import Approval, body_digest
from infra.stage_renewal import stage
from tools import build_hero_packet as hero

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Hero packet tests must never use network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    source = ROOT / "data" / "generated" / "smoke"
    if not (source / "manifest.json").exists():
        from tools.build_data import build
        source = tmp_path_factory.mktemp("synthetic-smoke")
        build("smoke", 20260929, source)
    return source


@pytest.fixture(scope="module")
def packet_dir(tmp_path_factory, source):
    output = tmp_path_factory.mktemp("hero-test") / "packet"
    hero.build(source, output)
    return output


def read(path):
    return json.loads(path.read_bytes())


def notebook_assets(notebook):
    statement = ast.parse("".join(notebook["cells"][0]["source"])).body[1]
    return json.loads(ast.literal_eval(statement.value.args[0]))


def test_full_model_membership_minimal_union_one_revision(packet_dir, source):
    packet = read(packet_dir / "packet.json")
    catalog = read(ROOT / "data" / "contracts" / "catalog.json")
    manifest = read(source / "manifest.json")
    models = [m for m in catalog["models"] if m["id"] in hero.HERO_MODELS]
    expected = {name for model in models for name in model["tables"]}
    assert packet["models"] == sorted(hero.HERO_MODELS)
    assert {table["name"] for table in packet["tables"]} == expected
    assert len(expected) == 18
    assert packet["sourceContentSha256"] == manifest["content_sha256"]
    assert packet["sourceManifestSha256"] == hero.digest((source / "manifest.json").read_bytes())
    assert (packet_dir / "source-manifest.json").read_bytes() == (source / "manifest.json").read_bytes()
    assert packet["modelDependencies"] == {m["id"]: m["tables"] for m in models}
    assert set(packet["sharedDependencies"]) == {"account", "calendar_date", "market", "site"}
    assert packet["approved"] is False and packet["liveExecution"] == "NOT_RUN"
    assert packet["ttlTeardown"]["ttlSeconds"] is None


def test_embedded_tables_exact_hashes_rows_columns_and_readback_pattern(packet_dir):
    packet = read(packet_dir / "packet.json")
    notebook = read(packet_dir / "loader.ipynb")
    metadata = {table["name"]: table for table in packet["tables"]}
    assets = notebook_assets(notebook)
    assert {asset["table"] for asset in assets} == metadata.keys()
    for asset in assets:
        raw = base64.b64decode(asset["payload"], validate=True)
        table = pq.read_table(io.BytesIO(raw))
        assert len(raw) == asset["bytes"] == metadata[asset["table"]]["bytes"]
        assert hashlib.sha256(raw).hexdigest() == metadata[asset["table"]]["fileSha256"]
        assert table.num_rows == asset["rows"] == metadata[asset["table"]]["rows"]
        assert table.column_names == list(asset["columns"])
    assert sum(asset["rows"] for asset in assets) == packet["rows"]
    assert sum(asset["bytes"] for asset in assets) == packet["sourceBytes"]
    loader = "".join(notebook["cells"][1]["source"])
    assert f'`<WORKSPACE_NAME>`.`{packet["lakehouseName"]}`.`dbo`' in loader
    assert 'spark.sql("SHOW TABLES IN " + schema_name)' in loader
    assert 'mode("errorifexists")' in loader and 'mode("overwrite")' not in loader
    assert "frame.exceptAll(stored)" in loader and "stored.exceptAll(frame)" in loader
    assert 'stored.count() != asset["rows"]' in loader
    assert 'exactDataReadback": True' in loader
    assert "ws-telco-iq-p0-20260929" not in loader and "lh_telcoiq_p0" not in loader
    assert notebook["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"] == "<LAKEHOUSE_ID>"


def test_complete_definition_parts_source_measure_expressions_unchanged(packet_dir):
    packet = read(packet_dir / "packet.json")
    for model in packet["modelPackets"]:
        body = read(packet_dir / model["file"])
        root = ROOT / "fabric" / "semantic-models" / (model["alias"] + ".SemanticModel")
        expected = [root / "definition.pbism", *[
            file for file in (root / "definition").rglob("*") if file.is_file()
        ], *[root / name for name in (".platform", "diagramLayout.json") if (root / name).exists()]]
        parts = body["definition"]["parts"]
        assert body["definition"]["format"] == "TMDL"
        assert len(parts) == len(expected) == model["parts"]
        assert {p["path"] for p in parts} == {p.relative_to(root).as_posix() for p in expected}
        assert body["displayName"] == model["displayName"] and "_Hero_Smoke_" in body["displayName"]
        for p in parts:
            assert "\\" not in p["path"]
            assert p["payloadType"] == "InlineBase64"
            assert base64.b64decode(p["payload"], validate=True) == (root / p["path"]).read_bytes()
        expressions = next(p for p in parts if p["path"] == "definition/expressions.tmdl")
        assert b"<WORKSPACE_ID>" in base64.b64decode(expressions["payload"])
        assert b"<LAKEHOUSE_ID>" in base64.b64decode(expressions["payload"])
    template = read(packet_dir / "notebook-template.json")
    assert template["type"] == "Notebook" and template["definition"]["format"] == "ipynb"
    assert json.loads(base64.b64decode(template["definition"]["parts"][0]["payload"])) == read(
        packet_dir / "loader.ipynb",
    )
    assert read(packet_dir / "lakehouse-template.json")["creationPayload"] == {"enableSchemas": True}


def test_no_runtime_or_eval_imports_in_builder_or_loader(packet_dir):
    notebook = read(packet_dir / "loader.ipynb")
    trees = [ast.parse("".join(cell["source"])) for cell in notebook["cells"]]
    trees.append(ast.parse(Path(hero.__file__).read_text()))
    for tree in trees:
        for node in ast.walk(tree):
            imports = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            )
            assert not any(name.startswith(("eval", "telco_iq")) for name in imports)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec"}
    for cell in notebook["cells"]:
        assert cell["execution_count"] is None and cell["outputs"] == []


def test_smoke_financial_quality_is_honest_and_not_fabricated(packet_dir):
    packet = read(packet_dir / "packet.json")
    quality = packet["financialQuality"]
    assert quality["status"] == "LOCAL_SOURCE_QUALITY_ONLY"
    assert quality["liveSourceMeasureValidation"] == "NOT_RUN"
    assert quality["period"] == "2026-07" and quality["selectedAccounts"] > 0
    if quality["zeroRevenueAccounts"] or quality["nonpositiveMarginAccounts"]:
        assert any("preserve source values" in warning for warning in quality["warnings"])
    assert not any(word in quality for word in ("answer", "fabricDax", "revenue_cents"))


def test_optional_subscriptions_still_one_complete_revision(tmp_path, source):
    packet = hero.build(source, tmp_path / "all", include_subscriptions=True)
    assert packet["models"] == ["enterprise", "finance", "network", "subscriptions"]
    assert len(packet["tables"]) == 21
    assert {"subscription", "subscription_month", "lifecycle_event"} <= {
        table["name"] for table in packet["tables"]
    }
    assert len({p["displayName"] for p in packet["modelPackets"]}) == 4


def test_all_eight_models_use_exact_coherent_source_membership_without_evaluator_data(tmp_path):
    from tools import build_data, build_models

    source, model_root, contracts = tmp_path / "data", tmp_path / "models", tmp_path / "contracts"
    build_data.build("showcase-v2", build_data.SEED, source)
    build_models.build(model_root, contracts, source / "manifest.json")
    output = tmp_path / "eight-models"
    options = {"showcase": True, "model_root": model_root, "catalog_path": contracts / "catalog.json"}
    packet = hero.build(source, output, all_models=True, **options)
    catalog = read(contracts / "catalog.json")
    expected = {name for model in catalog["models"] for name in model["tables"]}
    assert packet["models"] == sorted(hero.ALL_MODELS)
    assert packet["domainScope"] == "ALL_EIGHT_CUSTOMER_DOMAINS"
    assert len(packet["modelPackets"]) == len({item["displayName"] for item in packet["modelPackets"]}) == 8
    assert {table["name"] for table in packet["tables"]} == expected
    assert "quarantine" not in expected
    assert not any(
        column.startswith(("latent_", "gold_", "oracle_", "evaluator_"))
        for table in packet["tables"] for column in next(
            spec["columns"] for spec in catalog["tables"] if spec["id"] == table["name"]
        )
    )
    for model in packet["modelPackets"]:
        body = read(output / model["file"])
        folder = model_root / f"{model['alias']}.SemanticModel"
        for item in body["definition"]["parts"]:
            assert base64.b64decode(item["payload"]) == (folder / item["path"]).read_bytes()
    assert packet["ttlTeardown"]["approvalMode"] == "USAGE_SCOPED"
    assert packet["ttlTeardown"]["absoluteDeadlineUtc"] is None
    assert not any("deadline-bound" in value for value in packet["ttlTeardown"]["responsibilities"])
    with pytest.raises(ValueError, match="not both"):
        hero.build(source, tmp_path / "ambiguous", all_models=True, include_subscriptions=True, **options)
    default = hero.build(source, tmp_path / "hero", **options)
    assert default["models"] == sorted(hero.HERO_MODELS)
    extended = hero.build(source, tmp_path / "hero-with-subscriptions", include_subscriptions=True, **options)
    assert extended["models"] == ["enterprise", "finance", "network", "subscriptions"]


def test_packet_deterministic_and_never_overwrites(tmp_path, source, packet_dir):
    another = tmp_path / "another"
    built = hero.build(source, another)
    assert built == read(packet_dir / "packet.json")
    for entry in built["packetFiles"]:
        assert (another / entry["file"]).read_bytes() == (packet_dir / entry["file"]).read_bytes()
    with pytest.raises(FileExistsError, match="never replace"):
        hero.build(source, another)


def test_miniature_and_cross_revision_table_rejected(tmp_path, source):
    with pytest.raises(ValueError, match="one smoke revision"):
        hero.build(ROOT / "data" / "fixtures" / "miniature", tmp_path / "no-miniature")
    copied = tmp_path / "mixed"
    shutil.copytree(source, copied)
    shutil.copyfile(ROOT / "data" / "fixtures" / "miniature" / "account.parquet", copied / "account.parquet")
    with pytest.raises(ValueError, match="smoke manifest"):
        hero.build(copied, tmp_path / "no-mixed")
    assert not (tmp_path / "no-mixed").exists()


def test_source_manifest_tampering_rejected(tmp_path, source):
    copied = tmp_path / "source"
    shutil.copytree(source, copied)
    manifest = read(copied / "manifest.json")
    manifest["tables"][0]["content_sha256"] = "0" * 64
    (copied / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="revision is inconsistent"):
        hero.build(copied, tmp_path / "output")


def binding_for(packet_dir):
    packet = read(packet_dir / "packet.json")
    return {
        "packetSha256": hero.digest((packet_dir / "packet.json").read_bytes()),
        "workspace": {"id": "00000000-0000-0000-0000-000000000010", "displayName": "Synthetic-Hero-Workspace"},
        "lakehouse": {"id": "00000000-0000-0000-0000-000000000011", "displayName": packet["lakehouseName"]},
    }


def test_private_binding_resolves_all_parts_without_changing_source(packet_dir, tmp_path):
    binding = binding_for(packet_dir)
    output = tmp_path / "bound"
    before = {file.name: file.read_bytes() for file in packet_dir.iterdir()}
    bound = hero.bind(packet_dir, binding, output)
    assert bound["approved"] is False and bound["liveExecution"] == "NOT_RUN"
    notebook = read(output / "loader.ipynb")
    assert notebook["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"] == binding["lakehouse"]["id"]
    assert notebook["metadata"]["dependencies"]["lakehouse"]["default_lakehouse_workspace_id"] == (
        binding["workspace"]["id"]
    )
    assert "<WORKSPACE_NAME>" not in json.dumps(notebook)
    ast.parse("".join(notebook["cells"][1]["source"]))
    for template in output.glob("*-template.json"):
        body = read(template)
        for part in body.get("definition", {}).get("parts", []):
            content = base64.b64decode(part["payload"])
            assert b"<WORKSPACE_ID>" not in content and b"<LAKEHOUSE_ID>" not in content
            assert b"<WORKSPACE_NAME>" not in content
            if part["path"].startswith("definition/tables/"):
                original = read(packet_dir / template.name)
                source_part = next(p for p in original["definition"]["parts"] if p["path"] == part["path"])
                assert content == base64.b64decode(source_part["payload"])
    assert before == {file.name: file.read_bytes() for file in packet_dir.iterdir()}
    with pytest.raises(FileExistsError):
        hero.bind(packet_dir, binding, output)


@pytest.mark.parametrize("change", [
    lambda b: b.update(packetSha256="0" * 64),
    lambda b: b["lakehouse"].update(displayName="lh_telcoiq_p0"),
    lambda b: b["workspace"].update(displayName='ws"; raise RuntimeError()'),
    lambda b: b["lakehouse"].update(id="<LAKEHOUSE_ID>"),
])
def test_binding_rejects_wrong_hash_targets_or_injection(packet_dir, tmp_path, change):
    binding = binding_for(packet_dir)
    change(binding)
    with pytest.raises(ValueError):
        hero.bind(packet_dir, binding, tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()


def test_binding_rejects_repository_destination(packet_dir):
    with pytest.raises(ValueError, match="outside source"):
        hero.bind(packet_dir, binding_for(packet_dir), ROOT / ".local" / "must-not-write-bound-targets")


def test_proposal_is_unapproved_complete_hashed_and_references_m_lane(packet_dir, tmp_path):
    m = tmp_path / "m-proposal.json"
    m.write_text('{"approved":false,"selection":"NEW DEV"}')
    output = tmp_path / "proposal"
    proposal = stage(packet_dir, output, model_proposal=m)
    assert proposal["approved"] is False and proposal["authorizationTemplate"]["approved"] is False
    assert proposal["cloudMutations"] == proposal["paidCalls"] == 0
    assert proposal["modelProviderProposal"]["sha256"] == hero.digest(m.read_bytes())
    assert proposal["bffCertificateRenewal"]["approved"] is False
    assert proposal["shutdownRenewal"]["approved"] is False
    assert proposal["shutdownRenewal"]["resumeImplementedHere"] is False
    authorization = proposal["authorizationTemplate"]
    for asset in authorization["artifacts"]:
        assert hero.digest(Path(asset["file"]).read_bytes()) == asset["sha256"]
    assert body_digest(authorization["artifacts"]) == authorization["artifactSetSha256"]
    assert authorization["cost"]["cap"] is None and authorization["window"]["ttlSeconds"] is None
    assert authorization["tenantId"] is None and authorization["workspaceId"] is None
    assert authorization["operations"] == []
    assert read(output / "renewal-schema.json") == Approval.model_json_schema()
    assert any("unit price" in blocker for blocker in proposal["blockers"])
    assert proposal["sourcePacket"]["sha256"] == hero.digest((packet_dir / "packet.json").read_bytes())
    with pytest.raises(FileExistsError):
        stage(packet_dir, output, model_proposal=m)


def test_packet_artifact_change_blocks_binding_and_proposal(packet_dir, tmp_path):
    copied = tmp_path / "changed"
    shutil.copytree(packet_dir, copied)
    (copied / "finance-template.json").write_text("{}")
    with pytest.raises(ValueError, match="artifact changed"):
        hero.bind(copied, binding_for(copied), tmp_path / "no-bind")
    with pytest.raises(ValueError, match="artifact changed"):
        stage(copied, tmp_path / "no-stage")
