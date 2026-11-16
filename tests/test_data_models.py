"""Offline source/model checks only; not the parent's independent question oracle."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from uuid import UUID

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tools import build_data as data
from tools import build_models as models


def test_source_measures_do_not_shadow_home_columns_case_insensitively():
    catalog = models.catalog(showcase=True)
    tables = {table["id"]: table for table in catalog["tables"]}
    for metric in catalog["metrics"]:
        assert metric["name"].casefold() not in {
            column.casefold() for column in tables[metric["table"]]["columns"]
        }, metric["id"]
    units = next(metric for metric in catalog["metrics"] if metric["id"] == "commerce.units")
    assert units["name"] == "Booked Units"
    assert units["dax"] == f"SUM({models.ref('order_line', 'units')})"
    assert units["sql"] == "SUM(units)" and units["table"] == "order_line"


@pytest.mark.parametrize("name", ["units", "Units", "UNITS"])
def test_tmdl_generation_refuses_case_insensitive_measure_column_collisions(name):
    metric = next(metric for metric in models.metrics() if metric["id"] == "commerce.units")
    metric = {**metric, "name": name}
    with pytest.raises(ValueError, match="Measure name collides with a home-table column"):
        models.table_tmdl("commerce", data.TABLE_BY_ID["order_line"], [metric])


def test_enterprise_validity_start_dates_are_not_calendar_relationships(tmp_path):
    relationships = models.relationships("enterprise")
    assert not [
        entry for entry in relationships
        if entry["fromTable"] in {"contract", "service_site_map"} and entry["toTable"] == "calendar_date"
    ]
    assert not [entry for entry in relationships if entry["toTable"] == "contract"]
    output, contracts = tmp_path / "models", tmp_path / "contracts"
    models.build(output, contracts)
    definition = output / "enterprise.SemanticModel" / "definition"
    checked_in = data.ROOT / "fabric" / "semantic-models" / "enterprise.SemanticModel" / "definition"
    assert (definition / "relationships.tmdl").read_text() == (checked_in / "relationships.tmdl").read_text()
    assert "compatibilityLevel: 1702" in (definition / "database.tmdl").read_text()
    assert json.loads((definition.parent / "definition.pbism").read_text())["version"] == "4.2"


@pytest.fixture(scope="module")
def miniature(tmp_path_factory):
    folder = tmp_path_factory.mktemp("data-miniature")
    manifest = data.build("miniature", data.SEED, folder, batch_size=17)
    rows = {spec["id"]: pq.read_table(folder / f'{spec["id"]}.parquet').to_pylist() for spec in data.TABLES}
    return folder, manifest, rows


@pytest.fixture(scope="module")
def sql(miniature):
    connection = duckdb.connect()
    for spec in data.TABLES:
        connection.register(spec["id"], pq.read_table(miniature[0] / f'{spec["id"]}.parquet'))
    yield connection
    connection.close()


def test_exact_fixture_and_arrow_types(miniature):
    folder, manifest, rows = miniature
    assert {name: len(rows[name]) for name in ("account", "subscription", "account_month", "case")} == {
        "account": 10, "subscription": 20, "account_month": 60, "case": 8,
    }
    assert len(rows["subscription_month"]) == 120
    assert {row["month"] for row in rows["account_month"]} == set(data.MONTHS)
    for spec in data.TABLES:
        actual = pq.read_schema(folder / f'{spec["id"]}.parquet')
        assert actual == data.arrow_schema(spec)
        metadata = pq.read_metadata(folder / f'{spec["id"]}.parquet')
        assert all(metadata.row_group(index).num_rows <= 17 for index in range(metadata.num_row_groups))
        for column, kind in spec["columns"].items():
            if kind == "timestamp":
                assert actual.field(column).type.tz == "+00:00"
            elif kind == "date":
                assert pa.types.is_date32(actual.field(column).type)
    assert len(manifest["watermarks"]) == 8
    assert len(set(manifest["watermarks"].values())) == 8
    assert set(manifest["clocks"]) == {"data", "source_model", "ontology", "context_cache"}
    assert set(manifest["validation"].values()) == {"NOT_RUN"}
    assert manifest["generated_bytes"] == sum((folder / item["file"]).stat().st_size for item in manifest["tables"])
    assert manifest["generation_seconds"] > 0


def test_deterministic_logical_and_file_hashes(miniature, tmp_path):
    _, original, _ = miniature
    repeat = data.build("miniature", data.SEED, tmp_path / "repeat", batch_size=17)
    rebatched = data.build("miniature", data.SEED, tmp_path / "rebatched", batch_size=7)
    other_seed = data.build("miniature", data.SEED + 1, tmp_path / "seed", batch_size=17)
    assert original["content_sha256"] == repeat["content_sha256"] == rebatched["content_sha256"]
    assert original["content_sha256"] != other_seed["content_sha256"]
    for first, second in zip(original["tables"], repeat["tables"], strict=True):
        assert first["file_sha256"] == second["file_sha256"]
        assert first["content_sha256"] == second["content_sha256"]
        with (tmp_path / "repeat" / second["file"]).open("rb") as stream:
            assert second["file_sha256"] == hashlib.file_digest(stream, "sha256").hexdigest()


def test_primary_keys_and_foreign_keys(miniature):
    rows = miniature[2]
    for spec in data.TABLES:
        keys = [tuple(row[column] for column in spec["primaryKey"]) for row in rows[spec["id"]]]
        assert len(keys) == len(set(keys)), spec["id"]
        assert all(all(value is not None for value in key) for key in keys)
    targets = {
        "account_key": ("account", "account_key"), "subscription_key": ("subscription", "subscription_key"),
        "site_key": ("site", "site_key"), "service_key": ("service", "service_key"),
        "contract_key": ("contract", "contract_key"), "market_id": ("market", "market_id"),
        "case_key": ("case", "case_key"), "session_key": ("session", "session_key"),
        "order_key": ("order", "order_key"), "device_key": ("device", "device_key"),
        "store_key": ("store", "store_key"), "assignment_key": ("offer_assignment", "assignment_key"),
        "campaign_key": ("campaign", "campaign_key"),
    }
    for column, (target, key) in targets.items():
        valid = {row[key] for row in rows[target]}
        for source, records in rows.items():
            assert all(row.get(column) is None or row[column] in valid for row in records), (source, column)


def test_lifecycle_reconciliation(miniature):
    rows = miniature[2]
    previous = {}
    closing = Counter()
    for row in rows["subscription_month"]:
        key = row["subscription_key"]
        assert row["active_eom"] == row["opening_active"] + row["gross_add"] - row["disconnect"]
        assert row["disconnect"] <= row["opening_active"]
        assert row["gross_add"] <= 1 - row["opening_active"]
        if key in previous:
            assert row["opening_active"] == previous[key]
        previous[key] = row["active_eom"]
        closing[row["account_key"], row["month"]] += row["active_eom"]
    for row in rows["account_month"]:
        assert row["active_lines_eom"] == closing[row["account_key"], row["month"]]
    incident_accounts = {row["account_key"] for row in rows["case"] if row["reason"] == "network"}
    disconnect_accounts = {row["account_key"] for row in rows["subscription_month"] if row["disconnect"]}
    assert {1, 2} <= incident_accounts & disconnect_accounts


def test_half_open_identity_and_late_postings(miniature):
    rows = miniature[2]
    for table_id, identity in (("billing_account_map", "billing_key"), ("service_site_map", "service_key")):
        groups = defaultdict(list)
        for row in rows[table_id]:
            assert row["valid_from"] < row["valid_to"]
            groups[row[identity]].append(row)
        for group in groups.values():
            ordered = sorted(group, key=lambda item: item["valid_from"])
            assert all(a["valid_to"] <= b["valid_from"] for a, b in zip(ordered, ordered[1:]))
    reused = sorted([row for row in rows["billing_account_map"] if row["billing_key"] == 2],
                    key=lambda item: item["valid_from"])
    assert [row["account_key"] for row in reused] == [1, 2]
    assert reused[0]["valid_to"] == reused[1]["valid_from"]
    assert reused[0]["billing_key"] != reused[0]["account_key"]
    for row in rows["account_month"]:
        at = datetime.combine(row["month"], datetime.min.time(), tzinfo=timezone.utc)
        matches = [mapping for mapping in rows["billing_account_map"]
                   if mapping["billing_key"] == row["billing_key"] and mapping["valid_from"] <= at < mapping["valid_to"]]
        assert len(matches) == 1 and matches[0]["account_key"] == row["account_key"]
    assert rows["credit"] and all(row["month"] > row["service_month"] for row in rows["credit"])
    assert any(row["amount_cents"] < 0 for row in rows["charge_line"])


def test_unknown_ambiguity_quarantine_and_raw_motifs(miniature):
    rows = miniature[2]
    names = Counter(row["display_name"] for row in rows["account"])
    assert sorted(count for count in names.values() if count > 1) == [2, 2]
    assert any(row["account_key"] is None for row in rows["session"])
    assert any(row["account_key"] is None for row in rows["identity_map"])
    assert {row["reason_code"] for row in rows["quarantine"]} == {
        "ORPHAN_ACCOUNT", "INVALID_INTERVAL", "DUPLICATE_NATURAL_ID", "OVERLAPPING_IDENTITY_INTERVAL",
    }
    assert all(not re.search(r"gold|oracle|answer|latent", name, re.I)
               for spec in data.TABLES for name in spec["columns"])
    assert all(row["successful_checkout"] <= row["eligible_checkout"] and
               row["failed_checkout"] <= row["eligible_checkout"] and
               row["successful_checkout"] + row["failed_checkout"] <= 1 for row in rows["session"])
    failed = [row for row in rows["session"] if row["failed_checkout"]]
    assert failed and all(row["device_key"] == 1 and row["release_version"] == "2.1" for row in failed)
    assert len(rows["order"]) < sum(row["eligible_checkout"] for row in rows["session"])
    assert {row["randomized"] for row in rows["offer_assignment"]} == {True, False}
    assert all(row["assigned"] <= row["eligible"] and row["accepted"] <= row["assigned"]
               for row in rows["offer_assignment"])
    assert all(row["inventory_eod"] >= 0 for row in rows["store_device_day"])


def test_source_settlement_and_monitoring_known_cases(miniature):
    rows = miniature[2]
    july = {row["contract_key"]: row for row in rows["sla_evaluation"] if row["month"] == date(2026, 7, 1)}
    # These source arithmetic unit checks do not implement the independent Q03 oracle.
    assert (july[1]["outage_minutes"], july[1]["eligible_credit_cents"]) == (3600, 5000)
    assert (july[2]["outage_minutes"], july[2]["eligible_credit_cents"]) == (3960, 11790)
    assert sum(row["unavailable_minutes"] for row in rows["site_hour"]) == 10080
    assert sum(row["unavailable_minutes"] for row in rows["site_day"]) == 10080
    assert all(0 <= row["unavailable_minutes"] <= row["eligible_minutes"] for row in rows["site_hour"])
    applied = {(row["contract_key"], row["month"]): row["amount_cents"] for row in rows["applied_credit"]}
    for row in rows["sla_evaluation"]:
        assert row["applied_credit_cents"] == applied.get((row["contract_key"], row["month"]), 0)
        assert 0 <= row["applied_credit_cents"] <= row["eligible_credit_cents"]


def test_source_sla_groups_services_before_contract_cap(monkeypatch):
    source = data.Sources("miniature", data.SEED)
    contract = next(source.contracts())
    contract["credit_cap_cents"] = 50000
    mappings = list(source.mappings())
    for mapping in mappings:
        mapping["contract_key"] = contract["contract_key"]
    monkeypatch.setattr(source, "contracts", lambda: iter([contract]))
    monkeypatch.setattr(source, "mappings", lambda: iter(mappings))
    july = next(row for row in source.settlements() if row["month"] == date(2026, 7, 1))
    assert july["outage_minutes"] == 7920
    assert july["eligible_credit_cents"] == 23580
    contract["credit_cap_cents"] = 20000
    capped = next(row for row in source.settlements() if row["month"] == date(2026, 7, 1))
    assert capped["eligible_credit_cents"] == 20000


def reference_values(rows):
    """Independent elementary arithmetic for catalog measures, not cross-model questions."""
    def total(column):
        return sum(row[column] for row in rows if row[column] is not None)

    def divide(numerator, denominator):
        return numerator / denominator if denominator else None

    def distinct(column):
        return len({row[column] for row in rows if row[column] is not None})

    return total, len(rows), divide, distinct


def expected_metric(metric_id, rows):
    s, count, divide, distinct = reference_values(rows)
    name = metric_id.split(".", 1)[1]
    sums = {
        "active_lines_eom": "active_eom", "opening_active_lines": "opening_active",
        "gross_adds": "gross_add", "disconnects": "disconnect",
        "recognized_service_revenue": "revenue_cents", "billed_charges": "amount_cents",
        "posted_credits": "amount_cents", "service_cost": "service_cost_cents",
        "eligible_checkout_sessions": "eligible_checkout", "successful_checkout_sessions": "successful_checkout",
        "failed_checkouts": "failed_checkout", "open_cases_eod": "open_eod", "resolved_cases": "resolved",
        "repeat_contacts": "repeat_contact", "escalated_cases": "escalated", "eligible_site_minutes": "eligible_minutes",
        "unavailable_site_minutes": "unavailable_minutes", "exposure_minutes": "exposure_minutes",
        "failed_sessions": "failed_sessions", "units": "units", "booked_revenue": "booked_revenue_cents",
        "cancellations": "cancelled", "fulfilled_units": "fulfilled_units", "inventory_eod": "inventory_eod",
        "stockout_store_days": "stockout", "eligible_population": "eligible", "assigned_population": "assigned",
        "accepted_offers": "accepted", "offer_cost": "offer_cost_cents", "contracted_services": "contracted",
        "monitored_service_minutes": "monitored_minutes", "breached_services": "breached",
        "contracted_recurring_revenue": "recurring_revenue_cents", "eligible_credits": "eligible_credit_cents",
        "applied_credits": "amount_cents",
    }
    if name in sums:
        return s(sums[name])
    if name in {"sessions", "new_cases", "orders"}:
        return count
    if name in {"digital_active_accounts", "contacted_accounts"}:
        return distinct("account_key")
    if name == "active_accounts_eom":
        return len({row["account_key"] for row in rows if row["active_eom"]})
    if name == "net_adds":
        return s("gross_add") - s("disconnect")
    if name == "average_active_lines":
        return (s("opening_active") + s("active_eom")) / 2
    if name == "net_recognized_revenue":
        return s("revenue_cents") - s("credit_cents")
    if name == "contribution_margin":
        return s("revenue_cents") - s("credit_cents") - s("service_cost_cents")
    if name == "margin_percent":
        net = s("revenue_cents") - s("credit_cents")
        return divide(net - s("service_cost_cents"), net)
    pairs = {
        "churn_rate": ("disconnect", "opening_active"),
        "revenue_per_active_line": ("revenue_cents", "active_lines_eom"),
        "checkout_conversion": ("successful_checkout", "eligible_checkout"),
        "first_contact_resolution": ("first_contact_resolved", "resolved"),
        "service_degradation_percent": ("failed_sessions", "total_sessions"),
        "device_return_rate": ("returned_units", "fulfilled_units"),
        "acceptance_rate": ("accepted", "assigned"),
    }
    if name in pairs:
        numerator, denominator = pairs[name]
        return divide(s(numerator), s(denominator))
    if name == "authenticated_sessions":
        return sum(row["authenticated"] for row in rows)
    if name == "median_journey_duration":
        return statistics.median(row["duration_seconds"] for row in rows)
    if name == "mean_resolution_hours":
        values = [row["resolution_minutes"] for row in rows if row["resolution_minutes"] is not None]
        return statistics.mean(values) / 60 if values else None
    if name == "availability_percent":
        return divide(s("eligible_minutes") - s("unavailable_minutes"), s("eligible_minutes"))
    if name == "major_incidents":
        return sum(row["severity"] == "major" for row in rows)
    if name == "exposed_lines":
        return len({row["subscription_key"] for row in rows if row["exposure_minutes"] > 0})
    if name in {"treatment_retained", "control_retained", "retention_lift"}:
        arms = [[row for row in rows if row["randomized"] and row["eligible"] and row["treatment"] == arm]
                for arm in (1, 0)]
        retained = [sum(row["retained"] for row in arm) for arm in arms]
        if name != "retention_lift":
            return retained[name == "control_retained"]
        return retained[0] / len(arms[0]) - retained[1] / len(arms[1]) if all(arms) else None
    if name == "sla_compliance":
        return divide(s("contracted") - s("breached"), s("contracted"))
    if name == "credit_exposure":
        return s("eligible_credit_cents") - s("applied_credit_cents")
    raise AssertionError(f"Missing reference check for {metric_id}")


@pytest.mark.parametrize("metric", models.metrics(), ids=lambda metric: metric["id"])
def test_all_catalog_sql_measures_against_reference(metric, miniature, sql):
    home = data.TABLE_BY_ID[metric["table"]]
    required_dimensions = {"account_key", "month", "market_id"} & home["columns"].keys()
    assert required_dimensions <= set(metric["allowedDimensions"])
    assert not re.search(r"\b(SELECT|FROM|JOIN)\b|;", metric["sql"], re.I)
    rows = miniature[2][metric["table"]]
    date_column = metric["dateColumn"]
    selected = min(row[date_column] for row in rows)
    scoped = [row for row in rows if row[date_column] == selected]
    query = f'SELECT {metric["sql"]} AS "{metric["id"]}" FROM "{metric["table"]}" WHERE "{date_column}" = ?'
    cursor = sql.execute(query, [selected])
    assert cursor.description[0][0] == metric["id"]
    value = cursor.fetchone()[0]
    expected = expected_metric(metric["id"], scoped)
    assert value is None if expected is None else value == pytest.approx(expected)
    dimensions = ", ".join(f'"{name}"' for name in metric["allowedDimensions"])
    grouped = sql.execute(
        f'SELECT {dimensions}, {metric["sql"]} AS "{metric["id"]}" FROM "{metric["table"]}" GROUP BY {dimensions}'
    ).fetchall()
    assert grouped


def test_nonadditive_and_snapshot_contracts(sql, miniature):
    contract = {metric["id"]: metric for metric in models.metrics()}
    assert contract["subscriptions.active_lines_eom"]["aggregation"] == "semi_additive"
    assert contract["care.open_cases_eod"]["dateColumn"] == "snapshot_date"
    assert contract["commerce.inventory_eod"]["dateColumn"] == "snapshot_date"
    for metric in contract.values():
        if metric["aggregation"] == "semi_additive":
            assert "HASONEVALUE" in metric["dax"]
    rows = miniature[2]["session"]
    all_distinct = len({row["account_key"] for row in rows if row["account_key"] is not None})
    monthly_distinct = sum(len({row["account_key"] for row in rows if row["month"] == month and row["account_key"] is not None})
                           for month in data.MONTHS)
    assert monthly_distinct > all_distinct
    metric = contract["finance.margin_percent"]
    sql.execute("CREATE TEMP TABLE margins(revenue_cents BIGINT, credit_cents BIGINT, service_cost_cents BIGINT)")
    sql.execute("INSERT INTO margins VALUES (100, 0, 50), (900, 0, 90)")
    assert sql.execute(f'SELECT {metric["sql"]} FROM margins').fetchone()[0] == pytest.approx(0.86)
    assert (0.5 + 0.9) / 2 != pytest.approx(0.86)
    sql.execute("CREATE TEMP TABLE journeys(duration_seconds BIGINT)")
    sql.execute("INSERT INTO journeys VALUES (1), (2), (100)")
    assert sql.execute(f'SELECT {contract["digital.median_journey_duration"]["sql"]} FROM journeys').fetchone()[0] == 2
    for identifier in ("subscriptions.churn_rate", "finance.margin_percent", "growth.retention_lift"):
        metric = contract[identifier]
        assert sql.execute(f'SELECT {metric["sql"]} FROM "{metric["table"]}" WHERE FALSE').fetchone()[0] is None


def test_model_census_and_source_lint(miniature, tmp_path):
    output, contracts = tmp_path / "models", tmp_path / "contracts"
    census = models.build(output, contracts, miniature[0] / "manifest.json")
    contract = json.loads((contracts / "catalog.json").read_text())
    assert len(contract["models"]) == 8 and len(contract["metrics"]) == 64
    assert Counter(metric["model"] for metric in contract["metrics"]) == dict.fromkeys(data.MODEL_NAMES, 8)
    assert set(census["validation"].values()) == {"NOT_RUN", "SEE_TARGETED_TESTS"}
    lineage_tags = []
    for model in contract["models"]:
        folder = output / f'{model["id"]}.SemanticModel'
        definition = folder / "definition"
        model_text = (definition / "model.tmdl").read_text()
        assert "discourageImplicitMeasures" in model_text
        assert "ref role 'East Region'" in model_text
        assert not list(folder.glob("*.pbip")) and not list(folder.glob("*.pbir"))
        assert json.loads((folder / "definition.pbism").read_text())["version"] == "4.2"
        assert json.loads((folder / "smoke.link.json").read_text())["model"] == model["id"]
        query = (folder / "smoke.dax").read_text()
        assert "'Calendar'[date]" in query and "'Markets'[market_id]" in query
        for metric in (metric for metric in contract["metrics"] if metric["model"] == model["id"]):
            assert f'"{metric["id"]}", [{metric["name"]}]' in query
            home = data.TABLE_BY_ID[metric["table"]]
            assert home["model"] == model["id"]
            assert {"month", "market_id"} <= home["columns"].keys()
            assert set(metric["allowedDimensions"]) <= home["columns"].keys()
        expression = (definition / "expressions.tmdl").read_text()
        assert "AzureStorage.DataLake" in expression and "<WORKSPACE_ID>" in expression and "<LAKEHOUSE_ID>" in expression
        own_domains = {data.TABLE_BY_ID[t]["model"] for t in model["tables"]} - {"core"}
        assert own_domains == {model["id"]}
        for table_id in model["tables"]:
            spec = data.TABLE_BY_ID[table_id]
            text = (definition / "tables" / f"{table_id}.tmdl").read_text()
            assert f"ref table {models.quoted(spec['displayName'])}" in model_text
            assert text.startswith("/// ") and "\tdescription:" not in text
            assert " = entity\n\t\tmode: directLake" in text
            assert "expressionSource: 'OneLake Source'" in text
            assert " = m\n" not in text and "dataType: double" not in text
            assert set(re.findall(r"\t\tsourceColumn: (\w+)", text)) == set(spec["columns"])
            assert set(re.findall(r"\tcolumn '([^']+)'", text)) == set(spec["columns"])
            assert len(re.findall(r"\tmeasure ", text)) == len(re.findall(r"\t\tannotation MetricId =", text))
            for table_name, column_name in re.findall(r"'([^']+)'\[([^\]]+)\]", text):
                target = next(item for item in data.TABLES if item["displayName"] == table_name)
                assert column_name in target["columns"]
            lineage_tags.extend(re.findall(r"lineageTag: ([\w-]+)", text))
        for rel in models.relationships(model["id"]):
            left, right = data.TABLE_BY_ID[rel["fromTable"]], data.TABLE_BY_ID[rel["toTable"]]
            assert left["columns"][rel["fromColumn"]] == right["columns"][rel["toColumn"]]
            assert right["primaryKey"] == [rel["toColumn"]]
        role = (definition / "roles" / "East Region.tmdl").read_text()
        assert "modelPermission: read" in role and "tablePermission" in role
        assert "member" not in role.lower().replace("membership absent", "")
    assert all(UUID(tag).version == 5 for tag in lineage_tags)
    assert len(lineage_tags) == len(set(lineage_tags))
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    models.build(output, contracts, miniature[0] / "manifest.json")
    assert before == {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}


def test_smoke_population_and_inventory(tmp_path):
    folder = tmp_path / "smoke"
    manifest = data.build("smoke", data.SEED, folder, batch_size=256)
    counts = {item["id"]: item["rows"] for item in manifest["tables"]}
    assert (counts["account"], counts["subscription"], counts["account_month"], counts["site"]) == (1000, 2400, 6000, 20)
    assert counts["subscription_month"] == 14400
    assert len(counts) == len(data.TABLES) and all(counts.values())
    assert manifest["rows"] < 200000
    fulfilled = Counter()
    for row in pq.read_table(folder / "order.parquet").to_pylist():
        fulfilled[row["store_key"], row["device_key"], row["month"]] += 1 - row["cancelled"]
    snapshots = pq.read_table(folder / "store_device_day.parquet").to_pylist()
    for row in snapshots:
        initial = 2 if row["device_key"] == 1 and row["month"].month == 8 else 20
        expected = initial - (fulfilled[row["store_key"], row["device_key"], row["month"]]
                              if row["snapshot_date"].day == 22 else 0)
        assert row["inventory_eod"] == expected >= 0
    assert any(row["stockout"] for row in snapshots)


def test_standard_rejected_before_writing(tmp_path):
    with pytest.raises(ValueError, match="live miniature proof"):
        data.build("standard", data.SEED, tmp_path / "forbidden")
    assert not (tmp_path / "forbidden").exists()
    import sys
    process = subprocess.run(
        [sys.executable, str(data.ROOT / "tools" / "build_data.py"),
         "--profile", "standard", "--output", str(tmp_path / "forbidden")],
        capture_output=True, text=True, check=False,
    )
    assert process.returncode != 0 and "live miniature proof" in process.stderr
    assert not (tmp_path / "forbidden").exists()


def test_published_contract_and_fixture_readback(miniature):
    published = json.loads((data.ROOT / "data" / "contracts" / "catalog.json").read_text())
    assert published == models.catalog()
    folder = data.ROOT / "data" / "fixtures" / "miniature"
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["content_sha256"] == miniature[1]["content_sha256"]
    for item in manifest["tables"]:
        path = folder / item["file"]
        assert pq.read_schema(path) == data.arrow_schema(data.TABLE_BY_ID[item["id"]])
        assert pq.read_metadata(path).num_rows == item["rows"]
        with path.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == item["file_sha256"]
    census = json.loads((data.ROOT / "fabric" / "semantic-models" / "census.json").read_text())
    assert len(census["models"]) == 8
    assert sum(len(model["measures"]) for model in census["models"]) == 64
