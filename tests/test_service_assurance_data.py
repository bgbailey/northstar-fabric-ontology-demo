"""M2-04 offline tests for service-assurance and showcase-v2 hero scenario data."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from eval.runners.reconcile_sla_fixture import reconcile
from telco_iq.catalog import Catalog, compatible_populations, digest
from telco_iq.contracts import QueryNode, Scope, Status
from telco_iq.providers import OfflineProvider
from tools import build_data as data

UTC = timezone.utc
JULY = date(2026, 7, 1)
JULY_START = datetime(2026, 7, 1, tzinfo=UTC)
JULY_END = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def generated_profiles(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, dict]]:
    root = tmp_path_factory.mktemp("m2-04-profiles")
    result: dict[str, tuple[Path, dict]] = {}
    for profile in ("showcase-v2", "service-assurance"):
        folder = root / profile
        manifest = data.build(profile, data.SEED, folder)
        result[profile] = (folder, manifest)
    return result


def _load_tables(folder: Path, profile: str) -> dict[str, list[dict]]:
    return {
        spec["id"]: pq.read_table(folder / f"{spec['id']}.parquet").to_pylist()
        for spec in data.source_tables(profile)
    }


def _union_minutes(intervals: list[tuple[datetime, datetime]]) -> int:
    end: datetime | None = None
    seconds = 0.0
    for left, right in sorted(intervals):
        if right <= left:
            continue
        start = max(left, end) if end is not None else left
        seconds += max(0.0, (right - start).total_seconds())
        end = max(end, right) if end is not None else right
    return int(seconds // 60)


def test_historical_miniature_and_smoke_hashes_unchanged(tmp_path: Path) -> None:
    miniature = data.build("miniature", data.SEED, tmp_path / "miniature")
    smoke = data.build("smoke", data.SEED, tmp_path / "smoke")
    assert miniature["content_sha256"] == (
        "d90203f642d91af26ee6232af7260a6a83b78575fcc19c43bcdc62bbd7de21b8"
    )
    assert smoke["content_sha256"] == (
        "d725e8b83d10723ccc73f59a00e3a590fa93689b71468f9efcc29fe7db73e90a"
    )


def test_service_assurance_determinism_and_cli_overwrite(
    generated_profiles: dict[str, tuple[Path, dict]], tmp_path: Path,
) -> None:
    first_dir, first_manifest = generated_profiles["service-assurance"]
    second_dir = tmp_path / "second"
    second_manifest = data.build("service-assurance", data.SEED, second_dir)
    assert second_manifest == first_manifest
    assert first_manifest["version"] == "service-assurance-v1.0"
    assert first_manifest["generation_seconds"] is None
    for path in first_dir.iterdir():
        assert path.read_bytes() == (second_dir / path.name).read_bytes(), path.name

    cli_dir = tmp_path / "cli-service-assurance"
    cmd = [
        sys.executable,
        str(data.ROOT / "tools" / "build_data.py"),
        "--profile",
        "service-assurance",
        "--seed",
        str(data.SEED),
        "--output",
        str(cli_dir),
    ]
    run1 = subprocess.run(cmd, capture_output=True, text=True, check=True)
    snapshot1 = {path.name: path.read_bytes() for path in cli_dir.iterdir()}
    run2 = subprocess.run(cmd, capture_output=True, text=True, check=True)
    snapshot2 = {path.name: path.read_bytes() for path in cli_dir.iterdir()}
    assert json.loads(run1.stdout) == json.loads(run2.stdout)
    assert json.loads(run1.stdout)["content_sha256"] == first_manifest["content_sha256"]
    assert snapshot1 == snapshot2


def test_service_assurance_scale_keys_and_integrity(
    generated_profiles: dict[str, tuple[Path, dict]],
) -> None:
    folder, manifest = generated_profiles["service-assurance"]
    rows = _load_tables(folder, "service-assurance")
    assert len(rows["account"]) == 240
    assert sum(row["enterprise"] for row in rows["account"]) == 100
    assert len(rows["subscription"]) == 480
    assert len(rows["site"]) == 40
    assert len(rows["contract"]) == 120
    assert len(rows["service"]) == 180
    assert len(rows["case"]) == 1500
    expected_months = [
        *(date(2025, m, 1).isoformat() for m in range(9, 13)),
        *(date(2026, m, 1).isoformat() for m in range(1, 9)),
    ]
    assert manifest["service_months"] == expected_months
    assert len(manifest["service_months"]) == 12
    assert {row["month"].isoformat() for row in rows["service_month"]} == set(expected_months)
    assert len({row["month"] for row in rows["case"]}) == 12

    calendar_days = {row["date"] for row in rows["calendar_date"]}
    for spec in data.source_tables("service-assurance"):
        table_rows = rows[spec["id"]]
        assert table_rows, spec["id"]
        keys = [tuple(row[col] for col in spec["primaryKey"]) for row in table_rows]
        assert len(keys) == len(set(keys)), spec["id"]
        assert all(all(val is not None for val in key) for key in keys), spec["id"]
        for col, kind in spec["columns"].items():
            if kind == "date":
                assert all(row[col] in calendar_days for row in table_rows), (spec["id"], col)
        path = folder / f"{spec['id']}.parquet"
        entry = next(item for item in manifest["tables"] if item["id"] == spec["id"])
        with path.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == entry["file_sha256"]

    targets = {
        "account_key": ("account", "account_key"),
        "subscription_key": ("subscription", "subscription_key"),
        "site_key": ("site", "site_key"),
        "service_key": ("service", "service_key"),
        "contract_key": ("contract", "contract_key"),
        "market_id": ("market", "market_id"),
        "case_key": ("case", "case_key"),
    }
    for column, (target, pk) in targets.items():
        valid = {row[pk] for row in rows[target]}
        for table_name, records in rows.items():
            assert all(
                row.get(column) is None or row[column] in valid for row in records
            ), (table_name, column)

    for table_id, key_col in (
        ("billing_account_map", "billing_key"),
        ("service_site_map", "service_key"),
    ):
        grouped: dict[int, list[dict]] = defaultdict(list)
        for row in rows[table_id]:
            assert row["valid_from"] < row["valid_to"]
            grouped[row[key_col]].append(row)
        for spans in grouped.values():
            ordered = sorted(spans, key=lambda item: item["valid_from"])
            assert all(a["valid_to"] <= b["valid_from"] for a, b in zip(ordered, ordered[1:]))

    closing = Counter()
    for row in rows["subscription_month"]:
        closing[row["account_key"], row["month"]] += row["active_eom"]
    for row in rows["account_month"]:
        assert row["active_lines_eom"] == closing[row["account_key"], row["month"]]


async def test_service_assurance_catalog_provider_and_reconcile(
    generated_profiles: dict[str, tuple[Path, dict]],
) -> None:
    folder, manifest = generated_profiles["service-assurance"]
    catalog = Catalog(data.ROOT / "data" / "contracts" / "catalog.json")
    populations = [catalog.fixture_population(folder, model) for model in catalog.models]
    assert len(populations) == 8
    assert all(pop is not None for pop in populations)
    typed_pops = [pop for pop in populations if pop is not None]
    assert len(compatible_populations(typed_pops[:1], typed_pops[1:])) == 8
    assert {pop.profile for pop in typed_pops} == {"service-assurance"}
    assert {pop.generator_revision for pop in typed_pops} == {"service-assurance-v1.0"}
    assert {pop.manifest_hash for pop in typed_pops} == {digest(manifest)}
    assert {pop.identity_revision for pop in typed_pops} == {
        manifest["population"]["identityRevision"]
    }

    scope = Scope(
        tenant="synthetic",
        principal="m2-04",
        authorization_revision="1",
        mode="OFFLINE_SYNTHETIC",
        models=list(catalog.models),
    )
    provider = OfflineProvider(catalog, folder)
    for model in catalog.models:
        metric = next(
            m for m in catalog.metrics.values() if m["model"] == model and m["dateColumn"] == "month"
        )
        period = "2026-08-01" if model == "growth" else "2026-07-01"
        node = QueryNode(
            id=model,
            kind="QuerySemanticModel",
            model=model,
            table=metric["table"],
            metrics=[metric["id"]],
            group_by=["month"],
            expected_grain=["month"],
            date_role="month",
            filters=[{"column": "month", "op": "eq", "values": [period]}],
        )
        artifact, receipt = await provider.query(node, scope)
        assert artifact.status == receipt.status == Status.COMPLETE

    unknown_node = QueryNode(
        id="unknown-gap",
        kind="QuerySemanticModel",
        model="enterprise",
        table="sla_evaluation",
        metrics=["enterprise.credit_exposure"],
        group_by=["account_key", "month"],
        expected_grain=["account_key", "month"],
        date_role="month",
        filters=[
            {"column": "month", "op": "eq", "values": ["2026-07-01"]},
            {"column": "account_key", "op": "in", "values": [6, 9]},
        ],
        completeness="qualified_partial",
    )
    unknown_artifact, unknown_receipt = await provider.query(unknown_node, scope)
    assert unknown_artifact.status == unknown_receipt.status == Status.PARTIAL
    assert unknown_artifact.coverage["unknown_source_rows"] == 2

    july_report = await reconcile(folder)
    assert july_report["status"] == "PASS"
    assert len(july_report["independent_expected"]["accounts"]) >= 25
    august_report = await reconcile(folder, period="2026-08")
    assert set(august_report["verified_outcomes"].values()) == {
        "EMPTY_COHORT_BLOCKED_NO_FINANCIAL_QUERY",
    }


@pytest.mark.parametrize("profile", ["showcase-v2", "service-assurance"])
def test_all_six_designed_hero_cases_exist_in_both_profiles(
    generated_profiles: dict[str, tuple[Path, dict]], profile: str,
) -> None:
    folder, _ = generated_profiles[profile]
    rows = _load_tables(folder, profile)
    contracts = {row["contract_key"]: row for row in rows["contract"]}
    july_incidents = [
        inc for inc in rows["incident"] if inc["end"] > JULY_START and inc["start"] < JULY_END
    ]
    incidents_by_site: dict[int, list[dict]] = defaultdict(list)
    for inc in july_incidents:
        incidents_by_site[inc["site_key"]].append(inc)

    exposed_contracts: set[int] = set()
    naive_site_only_contracts: set[int] = set()
    mapping_only_contracts: set[int] = set()
    service_spans_by_contract: dict[tuple[int, int], list[tuple[datetime, datetime]]] = defaultdict(list)

    for mapping in rows["service_site_map"]:
        site_incidents = incidents_by_site.get(mapping["site_key"], [])
        if not site_incidents:
            continue
        naive_site_only_contracts.add(mapping["contract_key"])
        contract = contracts[mapping["contract_key"]]
        for inc in site_incidents:
            if max(JULY_START, inc["start"], mapping["valid_from"]) < min(
                JULY_END, inc["end"], mapping["valid_to"]
            ):
                mapping_only_contracts.add(mapping["contract_key"])
            start = max(JULY_START, inc["start"], mapping["valid_from"], contract["valid_from"])
            end = min(JULY_END, inc["end"], mapping["valid_to"], contract["valid_to"])
            if start < end:
                exposed_contracts.add(contract["contract_key"])
                service_spans_by_contract[(contract["contract_key"], mapping["service_key"])].append(
                    (start, end)
                )

    exposed_accounts = {contracts[ck]["account_key"] for ck in exposed_contracts}
    contracts_by_account: dict[int, set[int]] = defaultdict(set)
    for ck in exposed_contracts:
        contracts_by_account[contracts[ck]["account_key"]].add(ck)

    # Case 1: An account with two contracts on the path (fixed anchor account_key=1: contracts 1 & 2).
    assert contracts_by_account[1] == {1, 2}
    if profile == "service-assurance":
        assert len([ak for ak, cks in contracts_by_account.items() if len(cks) >= 2]) >= 4

    # Case 2: A service-site assignment that ends before the incident (fixed service_key=12, contract_key=9).
    service_12_maps = sorted(
        (m for m in rows["service_site_map"] if m["service_key"] == 12),
        key=lambda item: item["valid_from"],
    )
    assert len(service_12_maps) == 2
    assert service_12_maps[0]["site_key"] == 2
    assert service_12_maps[0]["valid_to"] == datetime(2026, 7, 10, tzinfo=UTC)
    assert all(
        service_12_maps[0]["valid_to"] <= inc["start"] for inc in incidents_by_site[2]
    )
    assert 9 in naive_site_only_contracts and 9 not in mapping_only_contracts
    assert 9 not in exposed_contracts

    # Case 3: A contract that starts after the incident (fixed contract_key=10, service_key=13).
    assert contracts[10]["valid_from"] == datetime(2026, 7, 20, tzinfo=UTC)
    assert all(contracts[10]["valid_from"] >= inc["end"] for inc in incidents_by_site[1])
    assert 10 in mapping_only_contracts and 10 not in exposed_contracts
    if profile == "service-assurance":
        assert {11, 12} <= naive_site_only_contracts - exposed_contracts
        assert {7, 8}.isdisjoint(exposed_accounts)

    # Case 4: A contract-month where the monetary cap applies (fixed contract_key=1 in 2026-07).
    sla_july = {row["contract_key"]: row for row in rows["sla_evaluation"] if row["month"] == JULY}
    assert sla_july[9]["coverage_complete"] is True and sla_july[9]["eligible_credit_cents"] == 0
    assert sla_july[10]["coverage_complete"] is True and sla_july[10]["eligible_credit_cents"] == 0
    uncapped_1 = sum(
        max(0, _union_minutes(spans) - contracts[1]["threshold_minutes"])
        * contracts[1]["credit_per_minute_cents"]
        for (ck, _sk), spans in service_spans_by_contract.items()
        if ck == 1
    )
    assert uncapped_1 > contracts[1]["credit_cap_cents"] == 5000
    assert sla_july[1]["eligible_credit_cents"] == 5000

    # Case 5: A missing applied-credit row (fixed contract_key=1 in 2026-07, plus mapping gap contract_key=8).
    applied_july = {row["contract_key"]: row for row in rows["applied_credit"] if row["month"] == JULY}
    assert 1 not in applied_july and sla_july[1]["eligible_credit_cents"] > 0
    assert applied_july[2]["amount_cents"] == 0
    assert sla_july[8]["coverage_complete"] is False and sla_july[8]["eligible_credit_cents"] is None

    # Case 6: An account on the path with an escalated case and one with no case in July 2026.
    july_cases_by_account: dict[int, list[dict]] = defaultdict(list)
    for case in rows["case"]:
        if case["month"] == JULY:
            july_cases_by_account[case["account_key"]].append(case)
    assert 2 in exposed_accounts and any(c["escalated"] == 1 for c in july_cases_by_account[2])
    assert 3 in exposed_accounts and not july_cases_by_account[3]
    if profile == "service-assurance":
        all_case_accounts = {c["account_key"] for c in rows["case"]}
        assert 15 in exposed_accounts and 15 not in all_case_accounts
