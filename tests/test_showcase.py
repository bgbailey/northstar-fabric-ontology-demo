"""TQ-004 source-population/packet checks; evaluator imports stay in tests only."""

import ast
import base64
import hashlib
import json
import shutil
from collections import Counter
from datetime import date, timedelta

import pyarrow.parquet as pq
import pytest

from eval.runners.reconcile_sla_fixture import reconcile, source_expectations
from telco_iq.catalog import Catalog, compatible_populations, digest
from telco_iq.contracts import QueryNode, Scope, Status
from telco_iq.errors import LabError
from telco_iq.providers import OfflineProvider
from tools import build_data as data
from tools import build_hero_packet as hero
from tools import build_models as models


@pytest.fixture(scope="module")
def showcase(tmp_path_factory):
    root = tmp_path_factory.mktemp("showcase")
    folder = root / "data"
    manifest = data.build("showcase-v2", data.SEED, folder)
    models.build(root / "models", root / "contracts", folder / "manifest.json")
    return root, folder, manifest


def test_versioned_population_quality_and_determinism(showcase, tmp_path):
    _, folder, manifest = showcase
    rows = {spec["id"]: pq.read_table(folder / f"{spec['id']}.parquet").to_pylist() for spec in data.TABLES}
    assert manifest["version"] == "showcase-v2.3"
    assert 0 < manifest["rows"] < 20000
    assert len(set(manifest["watermarks"].values())) == 8
    assert manifest["source_policy"]["rounding"] == "EXACT_WHOLE_MINUTES_INTEGER_CENTS_NO_ROUNDING"
    again = tmp_path / "repeat"
    assert data.build("showcase-v2", data.SEED, again) == manifest
    assert all((folder / p.name).read_bytes() == p.read_bytes() for p in again.iterdir())
    with pytest.raises(FileExistsError):
        data.build("showcase-v2", data.SEED, folder)
    accounts = {row["account_key"] for row in rows["account"]}
    for spec in data.TABLES:
        values = rows[spec["id"]]
        keys = [tuple(row[key] for key in spec["primaryKey"]) for row in values]
        assert len(keys) == len(set(keys)), spec["id"]
        assert all(row.get("account_key") is None or row["account_key"] in accounts for row in values)
    credits = rows["credit"]
    assert len({row["service_month"] for row in credits}) >= 2
    assert len({row["credit_key"] for row in credits}) == len(credits)
    july = [row for row in rows["account_month"] if row["month"] == date(2026, 7, 1)]
    margins = [row["revenue_cents"] - row["credit_cents"] - row["service_cost_cents"] for row in july]
    assert min(margins) < 0 and max(margins) > 0 and 0 in margins
    assert any(row["revenue_cents"] == 0 for row in july)
    assert len({row["account_key"] for row in rows["contract"]}) < len(rows["contract"])
    assert len({row["service_key"] for row in rows["service_site_map"] if row["contract_key"] == 1}) >= 2
    assignments = [row for row in rows["offer_assignment"] if row["randomized"] and row["eligible"]]
    assert {row["treatment"] for row in assignments} == {0, 1}
    assert any(row["account_key"] is None for row in rows["session"])
    assert len(rows["order"]) < sum(row["successful_checkout"] for row in rows["session"])
    assert all(row["booked_revenue_cents"] == 45000 for row in rows["order_line"])
    assert len({row["snapshot_date"] for row in rows["store_device_day"]}) == 18
    services = {row["service_key"]: row for row in rows["service"]}
    contracts = {row["contract_key"]: row for row in rows["contract"]}
    for row in rows["service_month"]:
        assert row["account_key"] == services[row["service_key"]]["account_key"]
        assert row["account_key"] == contracts[row["contract_key"]]["account_key"]
    unknown = [row for row in rows["sla_evaluation"] if row["contract_key"] == 8
               and row["month"].month in {6, 7}]
    assert len(unknown) == 2
    assert all(row["eligible_credit_cents"] is None and row["applied_credit_cents"] is None for row in unknown)


@pytest.mark.parametrize("defect", ["duplicate", "fractional", "nonintegral_cents", "negative_posting"])
def test_rejects_duplicate_keys_and_implicit_rounding(tmp_path, monkeypatch, defect):
    original = data.ShowcaseSources.rows

    def bad(self, identifier):
        values = list(original(self, identifier))
        if identifier == "incident":
            if defect == "duplicate":
                values.append(dict(values[0]))
            elif defect == "fractional":
                values[0]["end"] += timedelta(seconds=1)
        if identifier == "account_month" and defect == "nonintegral_cents":
            values[0]["revenue_cents"] = 10.5
        if identifier == "applied_credit" and defect == "negative_posting":
            values[0]["amount_cents"] = -1
        return iter(values)

    monkeypatch.setattr(data.ShowcaseSources, "rows", bad)
    with pytest.raises(ValueError, match="Duplicate|whole.minute|integer|Invalid settlement"):
        data.build("showcase-v2", data.SEED, tmp_path / "bad")
    assert not (tmp_path / "bad" / "manifest.json").exists()


def test_all_eight_actual_offline_population_contracts(showcase):
    root, folder, manifest = showcase
    catalog = Catalog(root / "contracts" / "catalog.json")
    provider = OfflineProvider(catalog, folder)
    assert provider is not None
    populations = [catalog.fixture_population(folder, model) for model in catalog.models]
    assert len(compatible_populations(populations[:1], populations[1:])) == 8
    assert len(populations) == 8
    assert {p.profile for p in populations} == {"showcase-v2"}
    assert {p.generator_revision for p in populations} == {"showcase-v2.3"}
    assert {p.manifest_hash for p in populations} == {digest(manifest)}
    assert {p.identity_revision for p in populations} == {manifest["population"]["identityRevision"]}
    assert all(p.shared_map_hashes == manifest["population"]["sharedMapHashes"] for p in populations)
    assert len({p.binding_hash for p in populations}) == 8


def test_showcase_packet_full_parts_and_explicit_mode(showcase, tmp_path):
    root, folder, manifest = showcase
    with pytest.raises(ValueError, match="smoke"):
        hero.build(folder, tmp_path / "implicit")
    output = tmp_path / "packet"
    packet = hero.build(folder, output, showcase=True, model_root=root / "models",
                        catalog_path=root / "contracts" / "catalog.json")
    assert packet["profile"] == "showcase-v2"
    assert packet["models"] == sorted(hero.HERO_MODELS)
    assert packet["approved"] is False and packet["binding"]["status"] == "UNRESOLVED_PRIVATE_STEP"
    assert packet["sourceManifestSha256"] == hashlib.sha256((folder / "manifest.json").read_bytes()).hexdigest()
    assert packet["population"] == manifest["population"]
    assert packet["financialQuality"]["marginSigns"] == {"positive": 3, "zero": 1, "negative": 2}
    for model in packet["modelPackets"]:
        source = root / "models" / (model["alias"] + ".SemanticModel")
        body = json.loads((output / model["file"]).read_bytes())
        parts = body["definition"]["parts"]
        for part in parts:
            assert base64.b64decode(part["payload"]) == (source / part["path"]).read_bytes()
        assert b"1702" in base64.b64decode(next(p["payload"] for p in parts if p["path"].endswith("database.tmdl")))
        pbism = next(p for p in parts if p["path"] == "definition.pbism")
        assert json.loads(base64.b64decode(pbism["payload"]))["version"] == "4.2"
    second = hero.build(folder, tmp_path / "repeat", showcase=True, model_root=root / "models",
                        catalog_path=root / "contracts" / "catalog.json")
    assert packet == second
    assert all((output / p.name).read_bytes() == p.read_bytes() for p in (tmp_path / "repeat").iterdir())
    with pytest.raises(FileExistsError):
        hero.build(folder, output, showcase=True, model_root=root / "models")
    copied = tmp_path / "mixed"
    shutil.copytree(folder, copied)
    shutil.copyfile(data.ROOT / "data" / "fixtures" / "miniature" / "account.parquet", copied / "account.parquet")
    with pytest.raises(ValueError, match="manifest"):
        hero.build(copied, tmp_path / "no-mix", showcase=True, model_root=root / "models",
                   catalog_path=root / "contracts" / "catalog.json")


async def test_offline_hero_execution_rankings_settlement_and_abstention(showcase):
    _, folder, _ = showcase
    report = await reconcile(folder)
    assert report["status"] == "PASS"
    contracts = {row["contract_key"]: row for row in report["independent_expected"]["contracts"]}
    assert contracts[1]["enterprise.applied_credits"] is None
    assert contracts[2]["enterprise.applied_credits"] == 0
    assert 0 < contracts[3]["enterprise.applied_credits"] < contracts[3]["enterprise.eligible_credits"]
    assert contracts[4]["enterprise.applied_credits"] == contracts[4]["enterprise.eligible_credits"]
    assert contracts[5]["enterprise.eligible_credits"] == 0
    assert contracts[1]["enterprise.eligible_credits"] == 5000
    assert contracts[2]["enterprise.eligible_credits"] == 8580
    assert contracts[5]["enterprise.applied_credits"] is None
    accounts = report["independent_expected"]["accounts"]
    by_exposure = sorted(accounts, key=lambda row: (-row["enterprise.credit_exposure"], row["account_key"]))
    by_revenue = sorted(accounts, key=lambda row: (-row["finance.recognized_service_revenue"], row["account_key"]))
    assert by_exposure[0]["account_key"] != by_revenue[0]["account_key"]
    assert [row["account_key"] for row in by_exposure] == [1, 2, 5, 3, 4, 6]
    assert [row["account_key"] for row in by_revenue] == [3, 6, 2, 5, 1, 4]
    assert len({row["account_key"] for row in accounts}) == len(accounts)
    for view in report["executions"].values():
        assert view["status"] == Status.COMPLETE
        assert view["receipts"] and view["answer"]["source_populations"]
        assert all(receipt["lane"] == "OFFLINE_SYNTHETIC" for receipt in view["receipts"])
    alternate = await reconcile(folder, incident_keys=[5])
    assert alternate["independent_expected"]["contract_keys"] == [5]
    assert alternate["independent_expected"]["contracts"][0]["enterprise.credit_exposure"] == 0
    cross_month = await reconcile(folder, incident_keys=[6])
    assert cross_month["independent_expected"]["contract_keys"] == [6]
    assert cross_month["independent_expected"]["contracts"][0]["enterprise.eligible_credits"] == 180
    missing = source_expectations(folder, incident_keys=[7])
    assert missing["contract_keys"] == [] and missing["unmatched_incidents"] == [7]
    for kwargs in ({"period": "2026-08"}, {"incident_keys": [7]}, {"incident_keys": [999]}):
        empty = await reconcile(folder, **kwargs)
        assert set(empty["verified_outcomes"].values()) == {"EMPTY_COHORT_BLOCKED_NO_FINANCIAL_QUERY"}
    postings = pq.read_table(folder / "applied_credit.parquet").to_pylist()
    assert all(row["amount_cents"] >= 0 for row in postings)
    counts = Counter(row["contract_key"] for row in pq.read_table(folder / "service_site_map.parquet").to_pylist())
    assert counts[1] >= 3


def test_generator_never_imports_evaluator_or_runtime():
    tree = ast.parse(data.ROOT.joinpath("tools", "build_data.py").read_text())
    for node in ast.walk(tree):
        names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
            [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        assert not any(name.startswith(("eval", "telco_iq")) for name in names)


async def test_actual_eight_source_queries_and_partial_unknowns(showcase, tmp_path):
    root, folder, _ = showcase
    catalog = Catalog(root / "contracts" / "catalog.json")
    scope = Scope(tenant="synthetic", principal="tq004", authorization_revision="1",
                  mode="OFFLINE_SYNTHETIC", models=list(catalog.models))
    provider = OfflineProvider(catalog, folder)
    for model in catalog.models:
        metric = next(metric for metric in catalog.metrics.values()
                      if metric["model"] == model and metric["dateColumn"] == "month")
        node = QueryNode(
            id=model, kind="QuerySemanticModel", model=model, table=metric["table"],
            metrics=[metric["id"]], group_by=["month"], expected_grain=["month"], date_role="month",
            filters=[{"column": "month", "op": "eq", "values": [
                "2026-08-01" if model == "growth" else "2026-07-01",
            ]}],
        )
        artifact, receipt = await provider.query(node, scope)
        assert artifact.status == receipt.status == Status.COMPLETE
        assert artifact.source_models == [model]
        assert catalog.fixture_population(folder, model).profile == "showcase-v2"
        assert receipt.lane == "OFFLINE_SYNTHETIC"
    node = QueryNode(id="bounded", kind="QuerySemanticModel", model="finance", table="account_month",
                     columns=["account_key"], expected_grain=["account_key"], max_rows=1,
                     date_role="month", filters=[{"column": "month", "op": "eq", "values": ["2026-07-01"]}])
    artifact, _ = await provider.query(node, scope)
    assert artifact.status == Status.PARTIAL
    stale = tmp_path / "stale"
    shutil.copytree(folder, stale)
    manifest = json.loads((stale / "manifest.json").read_bytes())
    manifest["watermarks"]["finance"] = "2026-07-15T00:00:00+00:00"
    (stale / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(LabError, match="not complete") as error:
        await OfflineProvider(catalog, stale).query(node, scope)
    assert error.value.status == Status.PARTIAL
