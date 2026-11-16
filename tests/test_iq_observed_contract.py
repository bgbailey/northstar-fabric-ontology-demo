"""Actual sanitized captures exercise a local codec, not a live/reader acceptance."""

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from time import perf_counter
from uuid import UUID

import pytest
from mcp.types import CallToolResult
from test_mcp_provider import (
    METRIC,
    MODEL,
    ROOT,
    WORKSPACE,
    attach_private_sink,
    capture_review,
    setup_provider,
)

from telco_iq.catalog import Catalog, digest
from telco_iq.contracts import Budgets, CohortFilter, Filter, QueryNode, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import SourceCallBudget
from telco_iq.mcp_client import (
    IQAdapter,
    ReviewedIQWitnessOutput,
    observed_int64,
    observed_iq_table,
    observed_iq_transport_rows,
)
from telco_iq.mcp_configuration import QueryProfileConfig
from telco_iq.mcp_provider import ReturnedColumn, bounded_dax

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "ga_iq_actual_2026_10_15.json"
FIXTURE_HASH = "8ae92a1458c0d9913cb8138b988ae7d911f3a0a6a91ed728912eb0f3ce0b27f7"
ACTUAL = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
WORKSPACE_CAPTURE = UUID("00000000-0000-0000-0000-000000000100")
CASES = ACTUAL["cases"]


def query_case(case, *, max_rows=25):
    alias = case["source_alias"]
    metric = METRIC if alias == "finance" else "subscriptions.active_lines_eom"
    node = QueryNode(
        id="observed", kind="QuerySemanticModel", model=alias,
        table="account_month" if alias == "finance" else "subscription_month",
        metrics=[metric], group_by=["account_key"], expected_grain=["account_key"], date_role="month",
        filters=[Filter(column="month", op="eq", values=[
            "2099-01-01" if case["probe"] == "empty-witness" else "2026-08-01",
        ])], max_rows=max_rows,
    )
    profile = ReviewedIQWitnessOutput(
        schema_hash=ACTUAL["tool_schema_hash"], evidence="Actual sanitized calibration; not production enrollment",
        artifact_id=UUID(case["request"]["artifactId"]), workspace_id=WORKSPACE_CAPTURE,
        columns={"account_key": "Int64", "value": "Int64"}, int64_identity_metrics=(metric,),
    )
    compiled = Catalog(ROOT / "data" / "contracts" / "catalog.json").compile_dax(
        node, max_rows=max_rows, witnessed=True,
        aliases={"account_key": "account_key", metric: "value"}, int64_identity_metrics=(metric,),
    )
    return node, profile, compiled


def result_body(result):
    return json.loads(result.content[0].text)


def set_body(result, body):
    result.content[0].text = json.dumps(body, ensure_ascii=False)
    return result


def test_capture_hash_protocol_and_tool_hash_are_exact():
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_HASH
    assert ACTUAL["protocol"] == "2025-11-25" and len(CASES) == 6
    assert ACTUAL["tool_schema_hash"] == "093addc3e3ef56eccbebbf9b25aa6064d02650fc7682e2cb6d96ce721df9e615"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_all_six_actual_envelopes_are_read_without_invented_completeness(case):
    result = CallToolResult.model_validate(case["result"])
    table = observed_iq_table(result, UUID(case["request"]["artifactId"]), WORKSPACE_CAPTURE)
    assert observed_iq_transport_rows(case["result"]) == len(table["rows"])
    _, profile, compiled = query_case(case)
    if "serialization-calibration" in case["probe"]:
        cells = table["rows"][0]
        assert cells[:3] == [None, 9007199254740993, -9007199254740993]
        assert observed_int64(cells[1]) == 9007199254740993
        assert observed_int64(cells[2]) == -9007199254740993
        if case["source_alias"] == "finance":
            assert cells[3:] == ["585,656 cents", 585656]
            with pytest.raises(LabError, match="no formatted"):
                observed_int64(cells[3])
        else:
            assert cells[3:] == [None, None]
        with pytest.raises(LabError, match="column order"):
            profile.decode_result(result, profile.schema_hash, compiled=compiled)
    else:
        decoded = profile.decode_result(result, profile.schema_hash, compiled=compiled)
        assert decoded.status == Status.COMPLETE
        assert decoded.total_rows == (0 if case["probe"] == "empty-witness" else 10)
        assert decoded.transport_rows == decoded.total_rows + 1 and decoded.witness_rows == 1
        if decoded.rows:
            assert decoded.rows[0] == {"account_key": 1, "value": 0}
            assert decoded.rows[-1]["account_key"] == 10
            assert decoded.rows[2]["value"] == (10238 if case["source_alias"] == "finance" else 2)
        else:
            assert decoded.rows == []


@pytest.mark.parametrize("value", [
    9007199254740993, -9007199254740993, 0, -(2**63), 2**63 - 1,
    "9007199254740993", "-9007199254740993", "0", str(-(2**63)), str(2**63 - 1),
])
def test_lossless_int64_accepts_only_canonical_integer_values(value):
    assert observed_int64(value) == int(value)


@pytest.mark.parametrize("value", [
    True, False, 1.0, None, "10,238 cents", "1,000", "1.0", "1e3", " 1", "+1", "-0", "01", "",
    2**63, -(2**63) - 1, str(2**63), str(-(2**63) - 1), "9" * 100,
])
def test_no_formatter_stripping_float_cast_bool_or_overflow(value):
    with pytest.raises(LabError, match="exact int64"):
        observed_int64(value)


def test_blank_values_and_signed_wide_integers_survive_witness_decode():
    case = CASES[0]
    result = CallToolResult.model_validate(case["result"])
    body = result_body(result)
    rows = body["executionResult"]["tables"][0]["rows"]
    rows[1][2], rows[2][2], rows[3][2] = None, 9007199254740993, -9007199254740993
    _, profile, compiled = query_case(case)
    decoded = profile.decode_result(set_body(result, body), profile.schema_hash, compiled=compiled)
    assert [row["value"] for row in decoded.rows[:3]] == [None, 9007199254740993, -9007199254740993]


@pytest.mark.parametrize("key", [1, 9007199254740993])
def test_duplicate_canonical_int64_grain_is_refused_after_normalization(key):
    result = CallToolResult.model_validate(CASES[0]["result"])
    body = result_body(result)
    body["executionResult"]["tables"][0]["rows"] = [
        ["count", None, None, 2], ["data", str(key), 0, 2], ["data", key, 1, 2],
    ]
    _, profile, compiled = query_case(CASES[0])
    with pytest.raises(LabError, match="unique nonnull expected grain") as error:
        profile.decode_result(set_body(result, body), profile.schema_hash, compiled=compiled)
    assert error.value.status == Status.BLOCKED_CAPABILITY


def test_large_valid_unique_int64_witness_avoids_quadratic_grain_scan():
    count = 32_000
    result = CallToolResult.model_validate(CASES[0]["result"])
    body = result_body(result)
    body["executionResult"]["tables"][0]["rows"] = (
        [["count", None, None, count]] + [["data", key, 0, count] for key in range(count)]
    )
    set_body(result, body)
    _, profile, compiled = query_case(CASES[0], max_rows=count + 1)
    started = perf_counter()
    decoded = profile.decode_result(result, profile.schema_hash, compiled=compiled)
    elapsed = perf_counter() - started
    assert decoded.status == Status.COMPLETE and decoded.total_rows == count
    assert len(decoded.rows) == count and decoded.transport_rows == count + 1
    assert decoded.rows[0]["account_key"] == 0 and decoded.rows[-1]["account_key"] == count - 1
    # A deliberately generous ceiling catches the observed ~54-second list scan.
    assert elapsed < 5, f"Unique witnessed decode stalled for {elapsed:.3f}s"


@pytest.mark.parametrize("mutation,status", [
    ("column-order", Status.BLOCKED_CAPABILITY), ("column-type", Status.BLOCKED_CAPABILITY),
    ("column-type-shape", Status.BLOCKED_CAPABILITY), ("column-metadata", Status.BLOCKED_CAPABILITY),
    ("duplicate-column", Status.BLOCKED_CAPABILITY), ("width", Status.BLOCKED_CAPABILITY),
    ("row-object", Status.BLOCKED_CAPABILITY), ("extra-table", Status.BLOCKED_CAPABILITY),
    ("table-error", Status.BLOCKED_CAPABILITY), ("execution-error", Status.BLOCKED_CAPABILITY),
    ("body-truncated", Status.BLOCKED_CAPABILITY), ("extra-content", Status.BLOCKED_CAPABILITY),
    ("content-metadata", Status.BLOCKED_CAPABILITY), ("result-metadata", Status.BLOCKED_CAPABILITY),
    ("structured-metadata", Status.BLOCKED_CAPABILITY), ("citation-metadata", Status.BLOCKED_CAPABILITY),
    ("model-metadata", Status.BLOCKED_CAPABILITY), ("metadata-disagreement", Status.BLOCKED_CAPABILITY),
    ("wrong-artifact", Status.BLOCKED_AUTH), ("wrong-citation", Status.BLOCKED_AUTH),
    ("wrong-url-artifact", Status.BLOCKED_AUTH), ("wrong-url-workspace", Status.BLOCKED_AUTH),
    ("absent-witness", Status.PARTIAL), ("duplicate-witness", Status.PARTIAL),
    ("witness-position", Status.BLOCKED_CAPABILITY), ("witness-business", Status.BLOCKED_CAPABILITY),
    ("negative-count", Status.BLOCKED_CAPABILITY), ("count-disagreement", Status.BLOCKED_CAPABILITY),
    ("more-data-than-count", Status.BLOCKED_CAPABILITY), ("unknown-kind", Status.BLOCKED_CAPABILITY),
    ("duplicate-grain", Status.BLOCKED_CAPABILITY), ("null-grain", Status.BLOCKED_CAPABILITY),
    ("wrong-order", Status.BLOCKED_CAPABILITY), ("formatted-value", Status.BLOCKED_CAPABILITY),
    ("boolean-value", Status.BLOCKED_CAPABILITY), ("float-value", Status.BLOCKED_CAPABILITY),
])
def test_closed_envelope_witness_and_grain_fail_closed(mutation, status):
    case = CASES[0]
    raw = deepcopy(case["result"])
    body = json.loads(raw["content"][0]["text"])
    table = body["executionResult"]["tables"][0]
    columns, rows = table["columns"], table["rows"]
    if mutation == "column-order":
        columns[1:3] = reversed(columns[1:3])
    elif mutation == "column-type":
        columns[2]["type"] = "Double"
    elif mutation == "column-type-shape":
        columns[2]["type"] = {}
    elif mutation == "column-metadata":
        columns[2]["errors"] = []
    elif mutation == "duplicate-column":
        columns[2]["name"] = columns[1]["name"]
    elif mutation == "width":
        rows[1].append(None)
    elif mutation == "row-object":
        rows[1] = {"account_key": 1}
    elif mutation == "extra-table":
        body["executionResult"]["tables"].append(deepcopy(table))
    elif mutation == "table-error":
        table["error"] = "not usable"
    elif mutation == "execution-error":
        body["executionResult"]["errors"] = []
    elif mutation == "body-truncated":
        body["truncated"] = True
    elif mutation == "extra-content":
        raw["content"].append({"type": "text", "text": "{}"})
    elif mutation == "content-metadata":
        raw["content"][0]["annotations"] = {"priority": 1}
    elif mutation == "result-metadata":
        raw["_meta"] = {"truncated": True}
    elif mutation == "structured-metadata":
        raw["structuredContent"]["error"] = "failed"
    elif mutation == "citation-metadata":
        raw["structuredContent"]["artifact_citation"]["extra"] = 1
    elif mutation == "model-metadata":
        body["semanticModel"]["error"] = "failed"
    elif mutation == "metadata-disagreement":
        body["semanticModel"]["Name"] = "different"
    elif mutation == "wrong-artifact":
        body["semanticModel"]["ArtifactId"] = str(MODEL)
    elif mutation == "wrong-citation":
        raw["structuredContent"]["artifact_citation"]["ArtifactId"] = str(MODEL)
    elif mutation == "wrong-url-artifact":
        body["semanticModel"]["Url"] = body["semanticModel"]["Url"].replace(
            case["request"]["artifactId"], str(MODEL),
        )
    elif mutation == "wrong-url-workspace":
        for model in (body["semanticModel"], raw["structuredContent"]["artifact_citation"]):
            model["Url"] = model["Url"].replace(str(WORKSPACE_CAPTURE), str(WORKSPACE))
    elif mutation == "absent-witness":
        del rows[0]
    elif mutation == "duplicate-witness":
        rows.insert(1, deepcopy(rows[0]))
    elif mutation == "witness-position":
        rows[0], rows[1] = rows[1], rows[0]
    elif mutation == "witness-business":
        rows[0][1] = 0
    elif mutation == "negative-count":
        rows[0][-1] = -1
    elif mutation == "count-disagreement":
        rows[1][-1] = 9
    elif mutation == "more-data-than-count":
        for row in rows:
            row[-1] = 9
    elif mutation == "unknown-kind":
        rows[1][0] = "subtotal"
    elif mutation == "duplicate-grain":
        rows[2][1] = rows[1][1]
    elif mutation == "null-grain":
        rows[1][1] = None
    elif mutation == "wrong-order":
        rows[1], rows[2] = rows[2], rows[1]
    elif mutation == "formatted-value":
        rows[1][2] = "10,238 cents"
    elif mutation == "boolean-value":
        rows[1][2] = True
    elif mutation == "float-value":
        rows[1][2] = 2.0
    else:
        pytest.fail("Missing mutation")
    raw["content"][0]["text"] = json.dumps(body)
    _, profile, compiled = query_case(case)
    with pytest.raises(LabError) as error:
        profile.decode_result(CallToolResult.model_validate(raw), profile.schema_hash, compiled=compiled)
    assert error.value.status == status


@pytest.mark.parametrize("text", [
    "{", "[]", "{}{}", '{"executionResult":{},"executionResult":{}}',
    '{"a":{"rows":[],"rows":[]}}', '{"n":NaN}', '{"n":Infinity}',
])
def test_malformed_json_duplicate_keys_and_non_json_constants_are_not_data(text):
    result = CallToolResult.model_validate(CASES[0]["result"])
    result.content[0].text = text
    _, profile, compiled = query_case(CASES[0])
    with pytest.raises(LabError, match="strict JSON"):
        profile.decode_result(result, profile.schema_hash, compiled=compiled)


def test_truncation_full_count_and_exact_transport_cap_including_witness():
    result = CallToolResult.model_validate(CASES[0]["result"])
    _, profile, compiled = query_case(CASES[0], max_rows=4)
    with pytest.raises(LabError, match="transport cap") as error:
        profile.decode_result(result, profile.schema_hash, compiled=compiled)
    assert error.value.status == Status.PARTIAL
    body = result_body(result)
    body["executionResult"]["tables"][0]["rows"] = body["executionResult"]["tables"][0]["rows"][:4]
    decoded = profile.decode_result(set_body(result, body), profile.schema_hash, compiled=compiled)
    assert decoded.status == Status.PARTIAL and decoded.total_rows == 10
    assert len(decoded.rows) == 3 and decoded.transport_rows == 4
    with pytest.raises(LabError, match="not returned"):
        profile.decode(result, profile.schema_hash, compiled=compiled)
    _, _, exact = query_case(CASES[0], max_rows=11)
    assert profile.decode_result(
        CallToolResult.model_validate(CASES[0]["result"]), profile.schema_hash, compiled=exact,
    ).status == Status.COMPLETE


def test_no_compiler_contract_and_no_review_never_prove_completeness():
    result = CallToolResult.model_validate(CASES[0]["result"])
    _, profile, compiled = query_case(CASES[0])
    for supplied in (None, replace(compiled, witnessed=False)):
        with pytest.raises(LabError, match="trusted witnessed"):
            profile.decode_result(result, profile.schema_hash, compiled=supplied)
    with pytest.raises(LabError, match="unreviewed"):
        profile.decode_result(result, "0" * 64, compiled=compiled)


def test_string_column_and_raw_query_compilation_preserve_values_and_aliases():
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    node = QueryNode(
        id="raw", kind="QuerySemanticModel", model="finance", table="account",
        columns=["account_key", "display_name"], expected_grain=["account_key"], max_rows=2,
        filters=[Filter(column="display_name", op="eq", values=['a,"b"'])],
    )
    compiled = catalog.compile_dax(
        node, max_rows=2, witnessed=True, aliases={"account_key": "account_key", "display_name": "value"},
    )
    _, profile, _ = query_case(CASES[0])
    profile = replace(profile, columns={"account_key": "Int64", "value": "String"})
    result = CallToolResult.model_validate(CASES[0]["result"])
    body = result_body(result)
    body["executionResult"]["tables"][0] = {
        "columns": [{"name": "__kind", "type": "String"}, {"name": "account_key", "type": "Int64"},
                    {"name": "value", "type": "String"}, {"name": "__total_rows", "type": "Int64"}],
        "rows": [["count", None, None, 1], ["data", "1", 'a,"b"\nline', 1]],
    }
    assert profile.decode_result(set_body(result, body), profile.schema_hash, compiled=compiled).rows == [
        {"account_key": 1, "value": 'a,"b"\nline'},
    ]
    assert 'TREATAS({"a,""b"""},' in compiled.text
    assert '"value", \'Accounts\'[display_name]' in compiled.text
    assert 'SELECTCOLUMNS(TOPN(1, __Result, [account_key], ASC)' in compiled.text


def test_compiler_counts_full_same_filter_grain_once_before_bounds_without_business_rewrite():
    node, profile, compiled = query_case(CASES[0], max_rows=11)
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    before = deepcopy(catalog.metrics)
    assert compiled.text.count("[Active Lines EOM] * 1") == 1
    assert compiled.text.count("SUMMARIZECOLUMNS(") == 1 and compiled.text.count("TREATAS(") == 1
    assert "COUNTROWS(__Result)" in compiled.text and "ISEMPTY(__Result), 0" in compiled.text
    assert compiled.text.index("COUNTROWS(__Result)") < compiled.text.index("TOPN(10, __Result")
    assert "'Subscription Month'[account_key]" in compiled.text
    assert "TREATAS({DATE(2026, 8, 1)}, 'Subscription Month'[month])" in compiled.text
    assert compiled.text.endswith("ORDER BY [__kind], [account_key]")
    assert catalog.metrics == before
    legacy = bounded_dax(catalog, node)
    assert "* 1" not in legacy and "TOPN(12," in legacy
    assert profile.int64_identity_metrics == compiled.identity_metrics


@pytest.mark.parametrize("failure", ["date", "home", "grain", "cohort", "identity", "ratio", "reserved"])
def test_compiler_preserves_source_authority_and_rejects_unreviewed_contracts(failure):
    node, profile, _ = query_case(CASES[0])
    aliases = {"account_key": "account_key", node.metrics[0]: "value"}
    identity = profile.int64_identity_metrics
    if failure == "date":
        node.date_role = "account_key"
    elif failure == "home":
        node.table = "account"
    elif failure == "grain":
        node.expected_grain = []
    elif failure == "cohort":
        node.cohort_filters = [CohortFilter(source="other", source_column="key", target_column="account_key")]
    elif failure == "identity":
        identity = ()
    elif failure == "ratio":
        node.metrics = ["subscriptions.churn_rate"]
        aliases = {"account_key": "account_key", node.metrics[0]: "value"}
        identity = tuple(node.metrics)
    elif failure == "reserved":
        aliases[node.metrics[0]] = "__kind"
    with pytest.raises(LabError):
        Catalog(ROOT / "data" / "contracts" / "catalog.json").compile_dax(
            node, max_rows=11, witnessed=True, aliases=aliases, int64_identity_metrics=identity,
        )


def test_witness_compiler_matches_legacy_date_range_cohort_and_multicolumn_grain():
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    node, profile, _ = query_case(CASES[3])
    node.group_by = node.expected_grain = ["account_key", "market_id"]
    node.filters.extend([
        Filter(column="account_key", op="in", values=[1, 3]),
        Filter(column="account_key", op="gte", values=[1]),
        Filter(column="month", op="lt", values=["2026-09-01"]),
    ])
    aliases = {"account_key": "account_key", "market_id": "market", METRIC: "value"}
    witnessed = catalog.compile_dax(
        node, max_rows=3, witnessed=True, aliases=aliases, int64_identity_metrics=profile.int64_identity_metrics,
    ).text
    legacy = catalog.dax(node)
    for expression in (
        "TREATAS({DATE(2026, 8, 1)}, 'Account Month'[month])",
        "TREATAS({1, 3}, 'Account Month'[account_key])",
        "FILTER(ALL('Account Month'[account_key]), 'Account Month'[account_key] >= 1)",
        "FILTER(ALL('Account Month'[month]), 'Account Month'[month] < DATE(2026, 9, 1))",
    ):
        assert expression in witnessed and expression in legacy
        assert witnessed.count(expression) == 1
    assert witnessed.endswith("ORDER BY [__kind], [account_key], [market]")


async def observed_provider(*, max_rows=11):
    provider, session, node, active = await setup_provider()
    raw = json.dumps(CASES[3]["result"]).replace(
        CASES[3]["request"]["artifactId"], str(MODEL),
    ).replace(str(WORKSPACE_CAPTURE), str(WORKSPACE))
    result = CallToolResult.model_validate_json(raw)
    session.responses["ExecuteQuery"] = result
    provider.client.protocol = "2025-11-25"
    output = ReviewedIQWitnessOutput(
        schema_hash=provider.client.schema_hash("ExecuteQuery"), evidence="Explicit synthetic use of actual shape",
        artifact_id=MODEL, workspace_id=WORKSPACE,
        columns={"account_key": "Int64", "value": "Int64"}, int64_identity_metrics=(METRIC,),
    )
    provider.source_bindings["finance"] = replace(
        provider.source_bindings["finance"], output=output, review=capture_review(result),
        columns={"account_month": {
            "account_key": ReturnedColumn("account_key", "int64", False),
            METRIC: ReturnedColumn("value", "int64", True),
        }},
    )
    provider._configuration_hash = provider._configuration()
    node.max_rows = max_rows
    node.filters = [Filter(column="month", op="eq", values=["2026-08-01"])]
    return provider, session, node, active


async def test_provider_uses_exact_cap_and_all_transport_rows_retained_before_decode(tmp_path):
    provider, session, node, _ = await observed_provider()
    budget = provider.request_budget = SourceCallBudget(Budgets(max_queries=1, max_rows=11))
    sink = attach_private_sink(provider, tmp_path)
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.status == receipt.status == Status.COMPLETE
    assert len(artifact.rows) == 10 and artifact.rows[2][METRIC] == 10238
    assert session.calls[0][1]["maxRows"] == 11
    assert budget.requests == 1 and budget.rows == 11 and budget.bytes > 0
    assert artifact.coverage == {
        "returned": 10, "bounded_response_total": 10, "transport_returned": 11,
        "transport_limit": 11, "witness_rows": 1,
    }
    assert "including 1 count witness" in receipt.completeness_evidence
    captures = list(sink.directory.glob("tq005-*.json"))
    assert len(captures) == 1
    retained = sink.read(captures[0].stem.removeprefix("tq005-"))
    assert retained["response"] == session.responses["ExecuteQuery"].model_dump(mode="json", exclude_unset=True)
    assert retained["request"] == session.calls[0][1]
    assert retained["context"]["query_node"] == node.model_dump(mode="json")


async def test_provider_cap_truncation_is_partial_not_smaller_complete_total():
    provider, session, node, _ = await observed_provider(max_rows=4)
    body = result_body(session.responses["ExecuteQuery"])
    body["executionResult"]["tables"][0]["rows"] = body["executionResult"]["tables"][0]["rows"][:4]
    set_body(session.responses["ExecuteQuery"], body)
    provider.request_budget = SourceCallBudget(Budgets(max_rows=4))
    artifact, receipt = await provider.query(node, provider.binding.scope)
    assert artifact.status == receipt.status == Status.PARTIAL and len(artifact.rows) == 3
    assert artifact.coverage["bounded_response_total"] == 10 and provider.request_budget.rows == 4
    assert session.calls[0][1]["maxRows"] == 4


@pytest.mark.parametrize("failure", ["rows", "bytes", "request", "malformed", "extra-table", "retention"])
async def test_provider_budgets_and_decoder_failures_retain_obtainable_originals(tmp_path, failure):
    provider, session, node, _ = await observed_provider()
    sink = attach_private_sink(provider, tmp_path)
    limits = Budgets(max_queries=1, max_rows=10 if failure == "rows" else 30,
                     max_bytes=1 if failure == "bytes" else 100_000)
    provider.request_budget = SourceCallBudget(limits)
    if failure == "request":
        provider.request_budget.reserve()
    elif failure == "malformed":
        body = result_body(session.responses["ExecuteQuery"])
        body["executionResult"]["tables"][0]["rows"][1][2] = "10,238 cents"
        set_body(session.responses["ExecuteQuery"], body)
    elif failure == "extra-table":
        body = result_body(session.responses["ExecuteQuery"])
        body["executionResult"]["tables"].append(deepcopy(body["executionResult"]["tables"][0]))
        set_body(session.responses["ExecuteQuery"], body)
    elif failure == "retention":
        sink.config.max_capture_bytes = 1
        provider._configuration_hash = provider._configuration()
    with pytest.raises(LabError):
        await provider.query(node, provider.binding.scope)
    captures = list(sink.directory.glob("tq005-*.json"))
    assert len(captures) == (0 if failure in {"request", "retention"} else 1)
    if failure not in {"request", "retention", "bytes"}:
        assert provider.request_budget.rows == (22 if failure == "extra-table" else 11)


@pytest.mark.parametrize("failure", ["cancel", "timeout", "revocation"])
async def test_observed_call_cancel_timeout_and_current_reader_stop_without_answer(failure):
    provider, session, node, active = await observed_provider()
    if failure == "revocation":
        session.on_call = lambda *_: active.update(value=False)
        expected = LabError
    else:
        async def wait(*args, **kwargs):
            if failure == "cancel":
                raise asyncio.CancelledError
            await asyncio.sleep(5)
        session.call_tool = wait
        expected = asyncio.CancelledError if failure == "cancel" else LabError
    provider.request_budget = SourceCallBudget(Budgets(timeout_seconds=0.1))
    with pytest.raises(expected) as error:
        await provider.query(node, provider.binding.scope)
    if failure == "timeout":
        assert error.value.status == Status.TIMED_OUT
    if failure == "revocation":
        assert error.value.status == Status.BLOCKED_AUTH


@pytest.mark.parametrize("failure", ["protocol", "artifact", "workspace", "type", "review", "state"])
async def test_provider_observed_review_and_source_gates_are_not_bypassed(failure):
    provider, session, node, _ = await observed_provider()
    source = provider.source_bindings["finance"]
    if failure == "protocol":
        provider.client.protocol = "2025-06-18"
    elif failure in {"artifact", "workspace"}:
        source = replace(source, output=replace(
            source.output, **{"artifact_id" if failure == "artifact" else "workspace_id":
                              UUID("00000000-0000-0000-0000-000000000999")},
        ))
    elif failure == "type":
        source.columns["account_month"][METRIC] = ReturnedColumn("value", "float64", True)
    elif failure == "review":
        source = replace(source, output=replace(source.output, schema_hash="0" * 64))
    elif failure == "state":
        provider.state_probe = None
    provider.source_bindings["finance"] = source
    provider._configuration_hash = provider._configuration()
    with pytest.raises(LabError):
        await provider.query(node, provider.binding.scope)
    assert session.calls == []


async def test_production_observed_profile_cannot_use_author_or_fixture_calibration():
    provider, session, node, _ = await observed_provider()
    provider.synthetic = False
    source = provider.source_bindings["finance"]
    source = replace(source, review=replace(source.review, provenance="author_shape_capture"))
    with pytest.raises(LabError, match="current-reader calibration"):
        provider._observed_query(node, source, provider._mapping(node, source))
    source = replace(source, review=replace(source.review, provenance="reader_capture"))
    with pytest.raises(LabError, match="current-reader calibration"):
        provider._observed_query(node, source, provider._mapping(node, source))
    assert not session.calls


@pytest.mark.parametrize("failure", [
    None, "epoch", "source", "schema", "review-hash", "request", "query", "partial", "metric",
])
async def test_production_calibration_linkage_in_memory_only_not_reader_enrollment(failure):
    provider, session, node, _ = await observed_provider()
    source = provider.source_bindings["finance"]
    compiled = provider._observed_query(node, source, provider._mapping(node, source))
    raw = session.responses["ExecuteQuery"].model_dump(mode="json", exclude_unset=True)
    capture_id = "f" * 64
    source = replace(
        source, output=replace(source.output, review_capture_id=capture_id),
        review=replace(source.review, provenance="reader_capture", capture_hash=digest(raw)),
    )
    original = {
        "lane": "LIVE_DELEGATED", "tool": "ExecuteQuery", "source_alias": "finance",
        "response_kind": "MCP_TOOL_RESULT", "response": raw,
        "request": {"artifactId": str(MODEL), "daxQueries": [compiled.text], "maxRows": node.max_rows},
        "context": {
            "kind": "SOURCE_BUSINESS_QUERY", "binding_hash": provider.binding.binding_hash,
            "source": source.identity.model_dump(mode="json"), "schema_hash": source.output.schema_hash,
            "query_node": node.model_dump(mode="json"),
        },
    }
    if failure == "epoch":
        original["context"]["binding_hash"] = "a" * 64
    elif failure == "source":
        original["context"]["source"]["dataset_id"] = str(WORKSPACE)
    elif failure == "schema":
        original["context"]["schema_hash"] = "a" * 64
    elif failure == "review-hash":
        source = replace(source, review=replace(source.review, capture_hash="a" * 64))
    elif failure == "request":
        original["request"]["maxRows"] += 1
    elif failure == "query":
        original["context"]["query_node"] = None
    elif failure == "partial":
        response = CallToolResult.model_validate(raw)
        body = result_body(response)
        body["executionResult"]["tables"][0]["rows"].pop()
        raw = set_body(response, body).model_dump(mode="json", exclude_unset=True)
        original["response"] = raw
        source = replace(source, review=replace(source.review, capture_hash=digest(raw)))
    elif failure == "metric":
        metric = "finance.net_recognized_revenue"
        node.metrics = [metric]
        source.columns["account_month"][metric] = ReturnedColumn("other_value", "int64", True)
        source = replace(source, output=replace(
            source.output, int64_identity_metrics=(METRIC, metric),
            columns={**source.output.columns, "other_value": "Int64"},
        ))

    class InMemoryCalibration:
        def read(self, value):
            assert value == capture_id
            return deepcopy(original)

    provider.evidence_sink = InMemoryCalibration()
    provider.synthetic = False
    if failure is None:
        assert provider._observed_query(node, source, provider._mapping(node, source)) == compiled
    else:
        with pytest.raises(LabError):
            provider._observed_query(node, source, provider._mapping(node, source))
    assert not session.calls


@pytest.mark.parametrize("failure", [None, "raw-only", "wrong-first-scope", "wrong-list-review"])
async def test_multiple_current_reader_calibrations_cover_exact_source_homes_without_fallback(failure):
    provider, session, node, _ = await observed_provider()
    source = provider.source_bindings["finance"]
    metric_compiled = provider._observed_query(node, source, provider._mapping(node, source))
    raw_node = QueryNode(
        id="calibrate_registered_accounts", kind="QuerySemanticModel", model="finance", table="account",
        columns=["account_key", "display_name"], expected_grain=["account_key"], max_rows=node.max_rows,
    )
    source.columns["account"] = {
        "account_key": ReturnedColumn("account_key", "int64", False),
        "display_name": ReturnedColumn("display_name", "string", False),
    }
    source = replace(source, output=replace(source.output, columns={**source.output.columns, "display_name": "String"}))
    raw_compiled = provider.catalog.compile_dax(
        raw_node, max_rows=raw_node.max_rows, witnessed=True,
        aliases={"account_key": "account_key", "display_name": "display_name"},
        int64_identity_metrics=source.output.int64_identity_metrics,
    )
    numeric_result = session.responses["ExecuteQuery"]
    body = result_body(numeric_result)
    body["executionResult"]["tables"][0]["columns"][2] = {"name": "display_name", "type": "String"}
    for index, row in enumerate(body["executionResult"]["tables"][0]["rows"][1:]):
        row[2] = "Synthetic account " + str(index)
    raw_result = set_body(deepcopy(numeric_result), body)
    ids = ("e" * 64, "f" * 64) if failure != "raw-only" else ("e" * 64,)
    records = {}
    for capture_id, sample, compiled, result in (
        ("e" * 64, raw_node, raw_compiled, raw_result), ("f" * 64, node, metric_compiled, numeric_result),
    ):
        records[capture_id] = {
            "lane": "LIVE_DELEGATED", "tool": "ExecuteQuery", "source_alias": "finance", "response_kind": "MCP_TOOL_RESULT",
            "response": result.model_dump(mode="json", exclude_unset=True),
            "request": {"artifactId": str(MODEL), "daxQueries": [compiled.text], "maxRows": sample.max_rows},
            "context": {
                "kind": "SOURCE_BUSINESS_QUERY", "binding_hash": provider.binding.binding_hash,
                "source": source.identity.model_dump(mode="json"), "schema_hash": source.output.schema_hash,
                "query_node": sample.model_dump(mode="json"),
            },
        }
    if failure == "wrong-first-scope":
        records["e" * 64]["context"]["binding_hash"] = "0" * 64
    source = replace(source, output=replace(source.output, review_capture_ids=ids),
                     review=replace(source.review, provenance="reader_capture",
                                    capture_hash="0" * 64 if failure == "wrong-list-review" else digest(list(ids))))

    class Captures:
        def read(self, value):
            return deepcopy(records[value])

    provider.evidence_sink, provider.synthetic = Captures(), False
    if failure is None:
        assert provider._observed_query(node, source, provider._mapping(node, source)) == metric_compiled
        assert provider._observed_query(raw_node, source, provider._mapping(raw_node, source)) == raw_compiled
    else:
        with pytest.raises(LabError):
            provider._observed_query(node, source, provider._mapping(node, source))
    assert not session.calls


async def test_provider_executes_only_reviewed_unformatted_fractional_source_values():
    provider, session, node, _ = await observed_provider()
    source = provider.source_bindings["finance"]
    metric = "finance.synthetic_rate"
    specification = {**provider.catalog.metrics[METRIC], "id": metric, "name": "Synthetic Source Rate",
                     "unit": "ratio", "aggregation": "non_additive"}
    provider.catalog.raw["metrics"].append(specification)
    provider.catalog.metrics[metric] = specification
    provider.catalog.revision = digest(provider.catalog.raw)
    node.metrics = [metric]
    source.columns["account_month"][metric] = ReturnedColumn("rate", "float64", True)
    provider.source_bindings["finance"] = replace(
        source, output=replace(source.output, columns={**source.output.columns, "rate": "Double"},
                               float64_identity_metrics=(metric,)),
    )
    body = result_body(session.responses["ExecuteQuery"])
    body["executionResult"]["tables"][0]["columns"][2] = {"name": "rate", "type": "Double"}
    for row in body["executionResult"]["tables"][0]["rows"][1:]:
        row[2] = 0.5
    session.responses["ExecuteQuery"] = set_body(session.responses["ExecuteQuery"], body)
    provider._configuration_hash = provider._configuration()
    artifact, _ = await provider.query(node, provider.binding.scope)
    assert artifact.status == Status.COMPLETE and all(row[metric] == 0.5 for row in artifact.rows)
    assert all(value == "non_additive" for key, value in artifact.aggregation.items() if key == metric)
    assert "[Synthetic Source Rate] * 1" in session.calls[0][1]["daxQueries"][0]


def test_original_envelope_error_type_cannot_be_normalized_into_success():
    raw = deepcopy(CASES[0]["result"])
    raw["isError"] = "false"
    with pytest.raises(LabError, match="single JSON-text"):
        observed_iq_transport_rows(raw)


@pytest.mark.parametrize("failure", ["no-compiler", "different-dax", "different-cap", "different-artifact"])
async def test_adapter_rejects_unbound_observed_request_before_traffic(failure):
    provider, session, node, _ = await observed_provider()
    source = provider.source_bindings["finance"]
    compiled = provider._observed_query(node, source, provider._mapping(node, source))
    dax, cap, artifact = compiled.text, node.max_rows, str(MODEL)
    if failure == "no-compiler":
        compiled = None
    elif failure == "different-dax":
        dax = "EVALUATE ROW(\"injected\", 1)"
    elif failure == "different-cap":
        cap += 1
    else:
        artifact = "00000000-0000-0000-0000-000000000999"
    with pytest.raises(LabError, match="bound witnessed"):
        await IQAdapter(provider.client, source.output).execute_result(artifact, dax, cap, compiled=compiled)
    assert session.calls == []


async def test_new_configuration_is_explicit_closed_opt_in_and_old_profiles_still_load():
    provider, _, _, _ = await observed_provider()
    source = provider.source_bindings["finance"]
    payload = {
        "identity": source.identity.model_dump(mode="json"), "artifact_id": str(source.artifact_id),
        "columns": {table: {name: asdict(column) for name, column in columns.items()}
                    for table, columns in source.columns.items()},
        "review": asdict(source.review),
        "output": {**asdict(source.output), "kind": "observed_iq_witness_v1"},
    }
    config = QueryProfileConfig.model_validate(payload)
    assert config.build() == source
    assert config.output.review_capture_id is None
    for field in ("kind", "protocol", "int64_identity_metrics", "artifact_id", "workspace_id"):
        invalid = deepcopy(payload)
        del invalid["output"][field]
        with pytest.raises(ValueError):
            QueryProfileConfig.model_validate(invalid)
    invalid = deepcopy(payload)
    invalid["output"]["complete"] = True
    with pytest.raises(ValueError):
        QueryProfileConfig.model_validate(invalid)
    old, _, _, _ = await setup_provider()
    payload["output"] = {**asdict(old.source_bindings["finance"].output), "kind": "structured"}
    assert QueryProfileConfig.model_validate(payload).build().output == old.source_bindings["finance"].output
