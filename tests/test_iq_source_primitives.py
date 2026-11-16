"""Synthetic fixtures of observed identity-cleared source primitives; no live calls or oracle arithmetic."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from mcp.types import CallToolResult

from telco_iq.catalog import Catalog
from telco_iq.contracts import QueryNode, Status
from telco_iq.errors import LabError
from telco_iq.mcp_client import ReviewedIQWitnessOutput

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = UUID("00000000-0000-0000-0000-000000000001")
DATASET = UUID("00000000-0000-0000-0000-000000000002")


def case(model, table, *, metrics=(), columns=(), grain=(), date_role="month", types, rows):
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    node = QueryNode(
        id="synthetic-source", kind="QuerySemanticModel", model=model, table=table,
        metrics=list(metrics), columns=list(columns), expected_grain=list(grain),
        date_role=date_role, max_rows=20,
    )
    compiled = catalog.compile_dax(node, max_rows=20, witnessed=True, float64_identity_metrics=tuple(metrics))
    types = {name: "String" if name in compiled.iso_temporal_columns + compiled.boolean_string_columns else value
             for name, value in types.items()}
    rows = [[("true" if value else "false") if name in compiled.boolean_string_columns and type(value) is bool else value
             for name, value in zip(compiled.columns, row, strict=True)] for row in rows]
    profile = ReviewedIQWitnessOutput(
        schema_hash="a" * 64, evidence="Synthetic fixture of actual numeric/RAW primitive protocol",
        artifact_id=DATASET, workspace_id=WORKSPACE, columns=types,
        int64_identity_metrics=(), float64_identity_metrics=tuple(metrics),
    )
    model_info = {
        "ArtifactId": str(DATASET), "Name": "Synthetic source", "Description": "Synthetic only",
        "Url": f"https://app.powerbi.com/groups/{WORKSPACE}/modeling/{DATASET}/modelView",
        "IconUrl": "https://app.powerbi.com/synthetic.svg",
    }
    data = {
        "semanticModel": model_info,
        "executionResult": {"tables": [{
            "columns": [{"name": "__kind", "type": "String"}]
                       + [{"name": name, "type": types[name]} for name in compiled.columns]
                       + ([{"name": "__serialization_errors", "type": "Int64"}] if compiled.iso_temporal_columns else [])
                       + [{"name": "__total_rows", "type": "Int64"}],
            "rows": [["count", *([None] * len(compiled.columns)), *([0] if compiled.iso_temporal_columns else []), len(rows)]]
                    + [["data", *row, *([0] if compiled.iso_temporal_columns else []), len(rows)] for row in rows],
        }]},
    }

    def result():
        return CallToolResult(content=[{"type": "text", "text": json.dumps(data)}],
                              structuredContent={"artifact_citation": model_info})

    return catalog, node, compiled, profile, data, result


@pytest.mark.parametrize("value", [0.5, -0.125, 0.0, None])
def test_source_owned_fractional_metric_identity_preserves_finite_numbers_and_blank(value):
    metric = "finance.margin_percent"
    _, _, compiled, profile, _, result = case(
        "finance", "account_month", metrics=[metric], types={metric: "Double"}, rows=[[value]],
    )
    assert "[Margin Percent] * 1" in compiled.text
    decoded = profile.decode_result(result(), "a" * 64, compiled=compiled)
    assert decoded.status == Status.COMPLETE and decoded.rows == [{metric: value}]


def test_observed_variant_median_is_numeric_only_under_its_reviewed_source_metric_identity():
    metric = "digital.median_journey_duration"
    _, _, compiled, profile, _, result = case(
        "digital", "session", metrics=[metric], types={metric: "Object"}, rows=[[123.5]],
    )
    assert profile.decode(result(), "a" * 64, compiled=compiled) == [{metric: 123.5}]
    with pytest.raises(LabError):
        profile.decode(result(), "a" * 64, compiled=replace(compiled, metric_bindings={}))
    with pytest.raises(LabError):
        replace(profile, float64_identity_metrics=()).decode(result(), "a" * 64, compiled=compiled)


@pytest.mark.parametrize("metric", [
    "commerce.inventory_eod", "commerce.stockout_store_days", "enterprise.monitored_service_minutes",
    "network.eligible_site_minutes", "network.unavailable_site_minutes", "network.exposure_minutes",
])
def test_reviewed_source_integer_minutes_units_and_observation_counts_keep_exact_identity(metric):
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    specification = catalog.metrics[metric]
    period = specification["dateColumn"]
    node = QueryNode(
        id="source_integer_unit", kind="QuerySemanticModel", model=specification["model"],
        table=specification["table"], metrics=[metric], group_by=[period], expected_grain=[period],
        date_role=period, max_rows=20,
    )
    compiled = catalog.compile_dax(node, max_rows=20, witnessed=True, int64_identity_metrics=(metric,))
    assert compiled.identity_metrics == (metric,)
    assert compiled.column_types[metric] == "int64"
    assert "[" + specification["name"] + "] * 1" in compiled.text
    with pytest.raises(LabError):
        catalog.compile_dax(node, max_rows=20, witnessed=True)


@pytest.mark.parametrize("value", ["50.00%", "1,200.50", "0.5", True, {}, [], float("inf"), float("nan")])
def test_formatted_or_nonprimitive_numeric_values_are_never_recovered_by_guessing(value):
    metric = "finance.margin_percent"
    _, _, compiled, profile, _, result = case(
        "finance", "account_month", metrics=[metric], types={metric: "Double"}, rows=[[value]],
    )
    with pytest.raises(LabError):
        profile.decode(result(), "a" * 64, compiled=compiled)


def test_raw_timestamp_and_key_identity_keep_explicit_source_UTC_and_exact_whole_seconds():
    names = ["incident_key", "start", "end"]
    _, _, compiled, profile, _, result = case(
        "network", "incident", columns=names, grain=["incident_key"], date_role="start",
        types={"incident_key": "Int64", "start": "DateTime", "end": "DateTime"},
        rows=[[1, "2026-07-15T00:00:00+00:00", "2026-07-15T00:00:01+00:00"]],
    )
    assert "'Incidents'[incident_key] * 1" in compiled.text
    assert profile.decode(result(), "a" * 64, compiled=compiled) == [{
        "incident_key": 1, "start": "2026-07-15T00:00:00+00:00",
        "end": "2026-07-15T00:00:01+00:00",
    }]
    assert 'FORMAT([start], "yyyy-MM-dd", "en-US")' in compiled.text
    assert "VAR __SerializationErrors" in compiled.text
    assert "SECOND([start])" in compiled.text


def test_subsecond_source_time_or_disagreeing_precision_witness_is_never_silently_truncated():
    _, _, compiled, profile, data, result = case(
        "network", "incident", columns=["incident_key", "start"], grain=["incident_key"], date_role="start",
        types={"incident_key": "Int64", "start": "DateTime"}, rows=[[1, "2026-07-15T00:00:00+00:00"]],
    )
    table = data["executionResult"]["tables"][0]
    table["rows"][0][-2] = 1
    with pytest.raises(LabError, match="precision"):
        profile.decode(result(), "a" * 64, compiled=compiled)
    table["rows"][0][-2] = 0
    table["rows"][1][-2] = 1
    with pytest.raises(LabError, match="guard"):
        profile.decode(result(), "a" * 64, compiled=compiled)


@pytest.mark.parametrize("stamp", [
    "2026-07-15", "07/15/2026 00:00:00", "2026-07-15T00:00:00-04:00",
    "2026-02-30 00:00:00", " 2026-07-15 00:00:00",
])
def test_timestamp_parser_does_not_guess_missing_time_locale_zone_or_calendar_value(stamp):
    _, _, compiled, profile, _, result = case(
        "network", "incident", columns=["incident_key", "start"], grain=["incident_key"], date_role="start",
        types={"incident_key": "Int64", "start": "DateTime"}, rows=[[1, stamp]],
    )
    with pytest.raises(LabError):
        profile.decode(result(), "a" * 64, compiled=compiled)


@pytest.mark.parametrize("stamp", ["2026-07-01", "2026-07-01T00:00:00", "2026-07-01T00:00:00Z"])
def test_source_date_is_distinct_from_timestamp_and_only_midnight_maps_to_the_same_date(stamp):
    _, _, compiled, profile, _, result = case(
        "finance", "account_month", columns=["account_key", "month"], grain=["account_key", "month"],
        types={"account_key": "Int64", "month": "DateTime"}, rows=[[1, stamp]],
    )
    assert profile.decode(result(), "a" * 64, compiled=compiled) == [{"account_key": 1, "month": "2026-07-01"}]


def test_source_date_rejects_time_of_day_and_null_does_not_invent_zero():
    _, _, compiled, profile, data, result = case(
        "finance", "account_month", columns=["account_key", "month", "revenue_cents"],
        grain=["account_key", "month"],
        types={"account_key": "Int64", "month": "DateTime", "revenue_cents": "Int64"},
        rows=[[1, "2026-07-01", None]],
    )
    assert profile.decode(result(), "a" * 64, compiled=compiled)[0]["revenue_cents"] is None
    data["executionResult"]["tables"][0]["rows"][1][2] = "2026-07-01T12:00:00"
    with pytest.raises(LabError):
        profile.decode(result(), "a" * 64, compiled=compiled)


def test_source_boolean_is_explicitly_encoded_before_union_and_never_uses_unreviewed_numeric_bits():
    _, _, compiled, profile, data, result = case(
        "growth", "offer_assignment", columns=["assignment_key", "randomized"], grain=["assignment_key"],
        types={"assignment_key": "Int64", "randomized": "Boolean"}, rows=[[1, True], [2, None]],
    )
    assert profile.decode(result(), "a" * 64, compiled=compiled) == [
        {"assignment_key": 1, "randomized": True}, {"assignment_key": 2, "randomized": None},
    ]
    assert compiled.boolean_string_columns == ("randomized",)
    assert 'IF([randomized], "true", "false")' in compiled.text
    for invalid in ("TRUE", 1, 0, True, 42949673025535, 42949672960000):
        data["executionResult"]["tables"][0]["rows"][1][2] = invalid
        with pytest.raises(LabError):
            profile.decode(result(), "a" * 64, compiled=compiled)


@pytest.mark.parametrize("value,expected", [
    ("1,080", 1080), ("-1,260", -1260), ("9,007,199,254,740,993", 9007199254740993),
    ("9,223,372,036,854,775,807", 2**63 - 1), ("-9,223,372,036,854,775,808", -(2**63)),
])
def test_only_explicitly_reviewed_exact_grouped_integer_columns_accept_the_observed_source_notation(value, expected):
    _, _, compiled, profile, data, result = case(
        "network", "incident", columns=["incident_key"], grain=["incident_key"], date_role="start",
        types={"incident_key": "Int64"}, rows=[[value]],
    )
    with pytest.raises(LabError):
        profile.decode(result(), "a" * 64, compiled=compiled)
    reviewed = replace(profile, grouped_int64_columns=("incident_key",))
    assert reviewed.decode(result(), "a" * 64, compiled=compiled) == [{"incident_key": expected}]
    for invalid in ("1,00", "01,000", "0,000", "1,080.0", "1,080 cents", "1.080", " 1,080", "9,223,372,036,854,775,808"):
        data["executionResult"]["tables"][0]["rows"][1][1] = invalid
        with pytest.raises(LabError):
            reviewed.decode(result(), "a" * 64, compiled=compiled)


def test_primitive_metadata_mismatch_is_blocked_even_if_all_business_values_are_null():
    metric = "finance.margin_percent"
    _, _, compiled, profile, _, result = case(
        "finance", "account_month", metrics=[metric], types={metric: "Boolean"}, rows=[[None]],
    )
    with pytest.raises(LabError, match="metadata"):
        profile.decode(result(), "a" * 64, compiled=compiled)


def test_float_metric_requires_explicit_identity_review_and_cannot_be_relabelled_int64():
    metric = "finance.margin_percent"
    catalog, node, _, _, _, _ = case(
        "finance", "account_month", metrics=[metric], types={metric: "Double"}, rows=[[0.5]],
    )
    with pytest.raises(LabError):
        catalog.compile_dax(node, max_rows=20, witnessed=True)
    with pytest.raises(LabError):
        catalog.compile_dax(node, max_rows=20, witnessed=True, int64_identity_metrics=(metric,))
    with pytest.raises(LabError):
        catalog.compile_dax(node, max_rows=20, witnessed=True, int64_identity_metrics=(metric,),
                            float64_identity_metrics=(metric,))
