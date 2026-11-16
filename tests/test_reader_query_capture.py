"""Synthetic fixed-query calibration bootstrap; never a real source or model call."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from test_reader_mcp_capture import ROOT, setup

from telco_iq.catalog import Catalog
from telco_iq.contracts import Budgets, QueryNode, Status
from telco_iq.errors import LabError
from telco_iq.live_contracts import SourceCallBudget
from telco_iq.private_evidence import PrivateEvidenceSink
from telco_iq.reader_query_capture import (
    ReaderQueryCalibrationConfig,
    capture_reader_query_calibration,
    producer_code_hash,
)
from telco_iq.source_observation import SourceMetadataCaptureService


def query_setup(tmp_path, monkeypatch, *, change=None):
    metadata, reader, session, state = setup(tmp_path, monkeypatch, change=change, query_mode=True)
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    metrics = {"finance": "finance.recognized_service_revenue", "subscriptions": "subscriptions.active_lines_eom"}
    queries = {
        alias: QueryNode(
            id="calibrate_" + alias, kind="QuerySemanticModel", model=alias,
            table="account_month" if alias == "finance" else "subscription_month",
            metrics=[metric], group_by=["account_key"], expected_grain=["account_key"], date_role="month",
            filters=[{"column": "month", "op": "eq", "values": ["2026-08-01"]}], max_rows=25,
        )
        for alias, metric in metrics.items()
    }
    config = ReaderQueryCalibrationConfig(
        scope=metadata.scope, ontology_workspace_id=metadata.ontology_workspace_id,
        ontology_item_id=metadata.ontology_item_id, native_snapshot_hash=metadata.native_snapshot_hash,
        sources=metadata.sources, catalog_hash=catalog.revision, queries=queries,
        aliases={alias: {"account_key": "account_key", metric: "value"} for alias, metric in metrics.items()},
        int64_identity_metrics={alias: (metric,) for alias, metric in metrics.items()},
        query_tool_hash="d" * 64, producer_code_hash=producer_code_hash(),
        budgets=metadata.budgets, private_evidence=metadata.private_evidence,
        approval=metadata.approval.model_copy(update={"operations": ["reader-source-query-calibration"]}),
        effects="CURRENT_READER_WITNESSED_QUERY_CALIBRATION_ONLY",
    )
    config.approval.artifact_digest, config.approval.target_digest = config.artifact_digest, config.target_digest
    return config, reader, session, state


async def test_fixed_seam_captures_only_exact_witnessed_compiler_queries_not_a_decoded_answer_or_grant(
    tmp_path, monkeypatch,
):
    config, reader, session, state = query_setup(tmp_path, monkeypatch)
    path = tmp_path / "fixed-query-calibration.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    service = SourceMetadataCaptureService(reader, path, ROOT)
    assert not state.token_calls and not state.connections
    result = await service.capture(session)
    assert result["kind"] == "SOURCE_QUERY_CALIBRATION_CAPTURE_ONLY" and result["ready"] is False
    assert result["status"] == "CALIBRATION_CAPTURED_NOT_READER_GRANT"
    assert state.token_calls == ["iq"] and len(state.connections) == 1 and len(state.tool_calls) == 2
    assert state.discovery_calls == 1
    sink = PrivateEvidenceSink(config.private_evidence, ROOT)
    originals = [sink.read(key) for key in result["capture_hashes"]]
    assert len(originals) == 3
    for original in originals:
        if original["tool"] == "MCPInventory":
            continue
        alias = original["source_alias"]
        compiled = Catalog(ROOT / "data" / "contracts" / "catalog.json").compile_dax(
            config.queries[alias], witnessed=True, max_rows=25,
            aliases=config.aliases[alias], int64_identity_metrics=config.int64_identity_metrics[alias],
        )
        assert original["tool"] == "ExecuteQuery"
        assert "capture_purpose" not in original, "A query cannot masquerade as metadata-only evidence"
        assert original["request"] == {
            "artifactId": str(config.sources[alias].dataset_id), "daxQueries": [compiled.text], "maxRows": 25,
        }
        assert original["context"]["kind"] == "SOURCE_BUSINESS_QUERY"
        assert original["context"]["query_node"] == config.queries[alias].model_dump(mode="json")
        assert original["context"]["binding_hash"] == config.binding().binding_hash
    assert all("synthetic-iq-opaque-token" not in json.dumps(sink.derivative(key))
               for key in result["capture_hashes"])
    assert "rows" not in result and "reader_grant" not in result


async def test_query_calibration_is_one_shot_even_when_fixed_config_remains_available(tmp_path, monkeypatch):
    config, reader, session, state = query_setup(tmp_path, monkeypatch)
    await capture_reader_query_calibration(reader, config, ROOT, session)
    with pytest.raises(LabError, match="already spent"):
        await capture_reader_query_calibration(reader, config, ROOT, session)
    assert state.token_calls == ["iq"] and len(state.tool_calls) == 2 and len(state.connections) == 1


async def test_explicit_additional_home_calibration_queries_are_fixed_independent_and_all_required(tmp_path, monkeypatch):
    config, reader, session, state = query_setup(tmp_path, monkeypatch)
    config.additional_queries = {"finance": [QueryNode(
        id="finance_raw_account", kind="QuerySemanticModel", model="finance", table="account",
        columns=["account_key"], expected_grain=["account_key"], max_rows=25,
    )]}
    config.approval.artifact_digest = config.artifact_digest
    config = ReaderQueryCalibrationConfig.model_validate(config.model_dump())
    result = await capture_reader_query_calibration(reader, config, ROOT, session)
    assert result["status"] == "CALIBRATION_CAPTURED_NOT_READER_GRANT"
    assert len(state.tool_calls) == 3
    assert {row["query_node_id"] for row in result["outcomes"]} == {
        "calibrate_finance", "finance_raw_account", "calibrate_subscriptions",
    }
    sink = PrivateEvidenceSink(config.private_evidence, ROOT)
    original = next(sink.read(row["capture_hashes"][0]) for row in result["outcomes"]
                    if row["query_node_id"] == "finance_raw_account")
    assert "'Accounts'[account_key] * 1" in original["request"]["daxQueries"][0]


@pytest.mark.parametrize("change", ["other-source", "duplicate-id", "dependency", "empty"])
def test_additional_calibrations_do_not_expand_source_scope_or_accept_unbounded_programs(tmp_path, monkeypatch, change):
    config, _, _, _ = query_setup(tmp_path, monkeypatch)
    node = QueryNode(id="extra_raw", kind="QuerySemanticModel", model="finance", table="account",
                     columns=["account_key"], expected_grain=["account_key"], max_rows=25)
    raw = config.model_dump()
    if change == "other-source":
        raw["additional_queries"] = {"unbound": [node.model_dump()]}
    else:
        if change == "duplicate-id":
            node.id = config.queries["finance"].id
        elif change == "dependency":
            node.depends_on = ["other"]
        raw["additional_queries"] = {"finance": [] if change == "empty" else [node.model_dump()]}
    with pytest.raises(ValidationError):
        ReaderQueryCalibrationConfig.model_validate(raw)


async def test_exact_optional_coverage_query_retains_its_distinct_original_source_schema_and_compiler(tmp_path, monkeypatch):
    config, reader, session, state = query_setup(tmp_path, monkeypatch)
    catalog = Catalog(ROOT / "data" / "contracts" / "catalog.json")
    catalog.tables["sla_evaluation"]["columns"]["coverage_complete"] = "bool"
    catalog_path = tmp_path / "exact-source-coverage-catalog.json"
    catalog_path.write_text(json.dumps(catalog.raw), encoding="utf-8")
    config.catalog_file, config.catalog_hash = catalog_path, Catalog(catalog_path).revision
    identity = config.sources.pop("finance")
    config.sources["enterprise"] = identity
    config.scope.models = sorted(config.sources)
    reader.config.model_ids["enterprise"] = identity.dataset_id
    original = config.queries.pop("finance")
    primary = original.model_copy(update={
        "model": "enterprise", "table": "sla_evaluation", "metrics": ["enterprise.credit_exposure"],
        "id": "calibrate_enterprise",
    })
    config.queries["enterprise"] = primary
    config.aliases.pop("finance")
    config.aliases["enterprise"] = {"account_key": "account_key", "enterprise.credit_exposure": "value"}
    config.int64_identity_metrics.pop("finance")
    config.int64_identity_metrics["enterprise"] = ("enterprise.credit_exposure",)
    control = primary.model_copy(update={"id": "sla_coverage", "max_rows": 2})
    config.coverage_queries = {"enterprise": [control]}
    config.coverage_diagnostics = {control.id: {"column": "coverage_complete", "kind": "effective_mapping"}}
    config.source_schema_hashes = {"enterprise": "c" * 64}
    config = ReaderQueryCalibrationConfig.model_validate(config.model_dump())
    config.approval.artifact_digest, config.approval.target_digest = config.artifact_digest, config.target_digest
    result = await capture_reader_query_calibration(reader, config, ROOT, session)
    assert result["status"] == "CALIBRATION_CAPTURED_NOT_READER_GRANT" and len(state.tool_calls) == 3
    sink = PrivateEvidenceSink(config.private_evidence, ROOT)
    row = next(row for row in result["outcomes"] if row["query_node_id"] == control.id)
    original = sink.read(row["capture_hashes"][0])
    assert original["context"]["kind"] == "SOURCE_COVERAGE_QUERY"
    assert original["context"]["source_schema_hash"] == "c" * 64
    assert original["request"]["maxRows"] == 2 and "VAR __raw" in original["request"]["daxQueries"][0]
    assert "[Credit Exposure]" not in original["request"]["daxQueries"][0]
    assert result["ready"] is False


async def test_calibration_uses_the_exact_private_source_catalog_instead_of_the_canonical_template(tmp_path, monkeypatch):
    config, reader, session, state = query_setup(tmp_path, monkeypatch)
    path = tmp_path / "reviewed-source-catalog.json"
    raw = Catalog(ROOT / "data" / "contracts" / "catalog.json").raw
    metric = next(item for item in raw["metrics"] if item["id"] == "finance.recognized_service_revenue")
    metric["name"] = "Synthetic Reviewed Source Amount"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config.catalog_file = path
    config.catalog_hash = Catalog(path).revision
    config.approval.artifact_digest = config.artifact_digest
    result = await capture_reader_query_calibration(reader, config, ROOT, session)
    assert result["status"] == "CALIBRATION_CAPTURED_NOT_READER_GRANT"
    assert state.token_calls == ["iq"] and len(state.tool_calls) == 2
    sink = PrivateEvidenceSink(config.private_evidence, ROOT)
    retained = [sink.read(key) for key in result["capture_hashes"]]
    finance = next(item for item in retained if item["tool"] == "ExecuteQuery" and item["source_alias"] == "finance")
    assert "[Synthetic Reviewed Source Amount] * 1" in finance["request"]["daxQueries"][0]


@pytest.mark.parametrize("change", ["repository", "onedrive", "changed"])
async def test_unprivate_or_drifted_source_catalog_does_not_acquire_a_token(tmp_path, monkeypatch, change):
    config, reader, session, state = query_setup(tmp_path, monkeypatch)
    if change == "repository":
        config.catalog_file = ROOT / "data" / "contracts" / "catalog.json"
    else:
        path = tmp_path / ("OneDrive" if change == "onedrive" else "private") / "source-catalog.json"
        path.parent.mkdir()
        raw = Catalog(ROOT / "data" / "contracts" / "catalog.json").raw
        raw["version"] += "-changed"
        path.write_text(json.dumps(raw), encoding="utf-8")
        config.catalog_file = path
    config.approval.artifact_digest = config.artifact_digest
    with pytest.raises(LabError):
        await capture_reader_query_calibration(reader, config, ROOT, session)
    assert not state.token_calls and not state.connections


@pytest.mark.parametrize("change", ["epoch", "model", "approval", "metadata-approval", "expired", "artifact", "code", "catalog"])
async def test_wrong_scope_or_metadata_only_authority_cannot_start_queries(tmp_path, monkeypatch, change):
    config, reader, session, state = query_setup(tmp_path, monkeypatch)
    if change == "epoch":
        session.scope = lambda: config.scope.model_copy(update={"authorization_revision": "other", "models": []})
    elif change == "model":
        reader.config.model_ids["finance"] = config.sources["subscriptions"].dataset_id
    elif change in {"approval", "metadata-approval"}:
        config.approval.operations = [
            "reader-mcp-metadata-capture" if change == "metadata-approval" else "unrelated",
        ]
    elif change == "expired":
        config.approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif change in {"code", "catalog"}:
        setattr(config, "producer_code_hash" if change == "code" else "catalog_hash", "0" * 64)
        config.approval.artifact_digest = config.artifact_digest
    else:
        config.budgets.max_rows = 500
    with pytest.raises(LabError):
        await capture_reader_query_calibration(reader, config, ROOT, session)
    assert not state.token_calls and not state.connections and not state.tool_calls
    assert not config.private_evidence.directory.exists()


@pytest.mark.parametrize("change", [
    "schema", "client-scope", "http-auth", "http-group", "secret", "sdk-error",
    "wrapped-sdk-error", "unknown-rows", "no-capture", "late-schema",
])
async def test_known_block_is_explicit_partial_and_never_a_source_grant(tmp_path, monkeypatch, change):
    config, reader, session, state = query_setup(tmp_path, monkeypatch, change=change)
    result = await capture_reader_query_calibration(reader, config, ROOT, session)
    assert result["kind"] == "SOURCE_QUERY_CALIBRATION_CAPTURE_ONLY" and result["ready"] is False
    assert result["status"] == "PARTIAL_QUERY_CALIBRATION_CAPTURE"
    assert any(outcome["status"] != "CAPTURED_NEEDS_REVIEW" for outcome in result["outcomes"])
    assert len(state.connections) == 1 and len(state.tool_calls) <= 2 and state.token_calls == ["iq"]
    assert "sensitive" not in json.dumps(result) and "synthetic-unsafe-echo" not in json.dumps(result)
    assert (config.private_evidence.directory / "reader-query-calibration.intent.json").exists()
    if change == "unknown-rows":
        assert len(state.tool_calls) == 1 and len(result["capture_hashes"]) == 2
    if change == "late-schema":
        assert len(state.tool_calls) == 2


async def test_tool_error_originals_are_retained_inside_sdk_context_without_reexecution(tmp_path, monkeypatch):
    config, reader, session, state = query_setup(tmp_path, monkeypatch, change="wrapped-tool-error")
    result = await capture_reader_query_calibration(reader, config, ROOT, session)
    assert [outcome["status"] for outcome in result["outcomes"]] == ["TOOL_ERROR", "TOOL_ERROR"]
    assert len(state.tool_calls) == 2 and not result["ready"]
    sink = PrivateEvidenceSink(config.private_evidence, ROOT)
    for outcome in result["outcomes"]:
        assert all(sink.read(key)["response"]["isError"] for key in outcome["capture_hashes"])


@pytest.mark.parametrize("limit", ["requests", "bytes", "rows", "time", "retention"])
async def test_exhausted_budget_or_retention_stops_without_a_second_query(tmp_path, monkeypatch, limit):
    config, reader, session, state = query_setup(
        tmp_path, monkeypatch, change="timeout" if limit == "time" else "wrapped-budget-error",
    )
    if limit == "requests":
        config.budgets.max_queries = 1
    elif limit == "bytes":
        config.budgets.max_bytes = 1
    elif limit == "rows":
        config.budgets.max_rows = 1
    elif limit == "time":
        config.budgets.timeout_seconds = 0.05
    else:
        config.private_evidence.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    config.approval.artifact_digest = config.artifact_digest
    with pytest.raises(LabError) as error:
        await capture_reader_query_calibration(reader, config, ROOT, session)
    assert error.value.status in {Status.TIMED_OUT, Status.PARTIAL, Status.BLOCKED_CAPABILITY}
    assert len(state.tool_calls) <= 1 and len(state.connections) <= 1


async def test_logout_cannot_continue_second_source_or_publish_calibration_success(tmp_path, monkeypatch):
    config, reader, session, state = query_setup(tmp_path, monkeypatch, change="logout")
    with pytest.raises(LabError) as error:
        await capture_reader_query_calibration(reader, config, ROOT, session)
    assert error.value.status == Status.BLOCKED_AUTH and len(state.tool_calls) == 1


async def test_unknown_sdk_defect_is_not_hidden_as_a_partial_calibration(tmp_path, monkeypatch):
    config, reader, session, _ = query_setup(tmp_path, monkeypatch, change="unexpected-group")
    with pytest.raises(ExceptionGroup):
        await capture_reader_query_calibration(reader, config, ROOT, session)


@pytest.mark.parametrize("change", ["missing-source", "other-model", "row-limit", "dependency", "cohort", "effect", "raw-dax"])
def test_config_requires_independent_exact_source_queries_not_caller_supplied_dax(tmp_path, monkeypatch, change):
    config, _, _, _ = query_setup(tmp_path, monkeypatch)
    values = config.model_dump(mode="json")
    if change == "missing-source":
        values["queries"].pop("finance")
    elif change == "other-model":
        values["queries"]["finance"]["model"] = "subscriptions"
    elif change == "row-limit":
        values["queries"]["finance"]["max_rows"] = 1001
    elif change == "dependency":
        values["queries"]["finance"]["depends_on"] = ["prior"]
    elif change == "cohort":
        values["queries"]["finance"]["cohort_filters"] = [
            {"source": "prior", "source_column": "account_key", "target_column": "account_key"},
        ]
    elif change == "effect":
        values["effects"] = "NATIVE_ENTITY_RULE_SOURCE_SCHEMA_METADATA_ONLY"
    else:
        values["daxQueries"] = ["EVALUATE ROW(\"arbitrary\", 1)"]
    with pytest.raises(ValidationError):
        ReaderQueryCalibrationConfig.model_validate(values)


async def test_invalid_compiler_projection_fails_before_token_or_network(tmp_path, monkeypatch):
    config, reader, session, state = query_setup(tmp_path, monkeypatch)
    config.aliases["finance"] = {"account_key": "missing_metric_alias"}
    config.approval.artifact_digest = config.artifact_digest
    with pytest.raises(LabError, match="aliases"):
        await capture_reader_query_calibration(reader, config, ROOT, session)
    assert not state.token_calls and not state.connections


def test_explicit_bounded_allowance_can_cover_observed_metadata_controls_without_raising_the_default():
    assert Budgets().max_queries == 16
    # Two33-response preflights and four source nodes per arm at the observed17/16-response source shapes.
    required = 2 * 33 + 2 * (2 * (2 * 17 + 1) + 2 * (2 * 16 + 1))
    assert required == 338
    budget = SourceCallBudget(Budgets(max_queries=required, timeout_seconds=300))
    for _ in range(required):
        budget.reserve()
    with pytest.raises(LabError) as error:
        budget.reserve()
    assert error.value.status == Status.PARTIAL and budget.requests == required
    with pytest.raises(ValidationError):
        Budgets(max_queries=513)
