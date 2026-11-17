import ast
import importlib.util
import json
import tomllib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from telco_iq.approval import Approval, private_config_path, teardown_preview
from telco_iq.completeness import embedded_csv
from telco_iq.errors import LabError
from telco_iq.evidence import live_claim_allowed, sanitized, write_receipt
from telco_iq.retry import ReadOnlyRetryTransport

ROOT = Path(__file__).resolve().parents[1]


def test_m5_wheel_force_includes_runtime_assets():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    expected = {
        "tools/build_data.py": "telco_iq/deploy/assets/build_data.py",
        **{
            f"fabric/semantic-models/{alias}.SemanticModel":
                f"telco_iq/deploy/assets/semantic-models/{alias}.SemanticModel"
            for alias in ("network", "enterprise", "finance", "care")
        },
        "tools/build_ontology_prompts.py": (
            "telco_iq/ontology/assets/build_ontology_prompts.py"
        ),
        "tools/check_ontology_conformance.py": (
            "telco_iq/ontology/assets/check_ontology_conformance.py"
        ),
        "fabric/ontology/service-assurance.json": (
            "telco_iq/ontology/assets/service-assurance.json"
        ),
        "fabric/ontology/service-assurance.prompts.md": (
            "telco_iq/ontology/assets/service-assurance.prompts.md"
        ),
    }

    for source, destination in expected.items():
        assert force_include[source] == destination
        assert (ROOT / source).exists()


def test_utc_source_parquet_timestamp_decodes():
    import pyarrow as pa
    import pyarrow.parquet as pq

    timestamp = datetime(2026, 9, 29, 17, 3, tzinfo=timezone.utc)
    output = pa.BufferOutputStream()
    pq.write_table(pa.table({"timestamp": pa.array([timestamp], type=pa.timestamp("us", tz="UTC"))}), output)
    assert pq.read_table(pa.BufferReader(output.getvalue())).to_pylist() == [{"timestamp": timestamp}]


def test_csv_quotes_newlines_null_empty_and_currency():
    rows = embedded_csv(
        'name,empty,count,money\r\n"a,""b""\nnext",,\\N,9000000000.0001\r\n',
        {"name": "string", "empty": "string", "count": "int64", "money": "decimal"},
        null_marker="\\N", expected_rows=1, complete=True,
    )
    assert rows == [{"name": 'a,"b"\nnext', "empty": "", "count": None, "money": Decimal("9000000000.0001")}]


@pytest.mark.parametrize("count,complete", [(None, True), (251, True), (1, False)])
def test_csv_incomplete_is_not_total(count, complete):
    with pytest.raises(LabError):
        embedded_csv("key\n1\n", {"key": "int64"}, null_marker="\\N", expected_rows=count, complete=complete)


def test_unobserved_csv_null_encoding_blocks():
    with pytest.raises(LabError, match="null encoding"):
        embedded_csv("key\n\n", {"key": "string"}, null_marker=None, expected_rows=0, complete=True)


def test_receipts_redact_nested_credentials_and_are_immutable(tmp_path):
    value = {"Authorization": "Bearer private", "nested": {"clientSecret": "private"}, "ok": "hello"}
    assert sanitized(value)["nested"]["clientSecret"] == "[REDACTED]"
    first = write_receipt(tmp_path, value)
    second = write_receipt(tmp_path, value)
    assert first == second
    assert "private" not in first.read_text()


def test_sas_query_is_redacted_in_driver_log_text():
    text = "SasTokenFromConf: ?skoid=synthetic&se=tomorrow&sp=r&sig=synthetic-signature"
    assert sanitized(text) == "SasTokenFromConf: [REDACTED_SAS]"
    assert "synthetic-signature" not in sanitized("https://storage.test/file?sv=1&sig=synthetic-signature")


def test_offline_pass_cannot_promote_live_claim():
    register = json.loads((ROOT / "eval" / "validation" / "register.json").read_text(encoding="utf-8"))
    observations = [{
        "experimentId": e, "lane": "OFFLINE_SYNTHETIC", "status": "PASS",
        "featureOutcome": "SUPPORTED", "evidencePaths": ["offline-tests"],
    } for e in register["releaseClaimGates"]["nativeOntologyCustomApp"]]
    assert not live_claim_allowed(register, observations, "nativeOntologyCustomApp")


def test_unsupported_feature_is_not_demonstrated_even_if_experiment_passed():
    register = {"releaseClaimGates": {"feature": ["E01"]}}
    assert not live_claim_allowed(register, [{
        "experimentId": "E01", "lane": "LIVE_DEV", "status": "PASS",
        "featureOutcome": "UNSUPPORTED", "evidencePaths": ["measured-negative"],
    }], "feature")


def test_approval_revision_cost_target_and_expiry():
    approval = Approval(
        target_alias="APPROVED_LAB_ONLY", target_digest="target", artifact_digest="revision",
        operations=["create-lab-item"], estimated_cost=2, cap=3, currency="USD",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1), ttl_hours=2,
        teardown_owner="lab owner", teardown_mechanism="approved scheduled cleanup",
        confirmation_receipt="explicit confirmation reference",
    )
    approval.authorize("revision", "target", "create-lab-item")
    for revision, target, operation in [
        ("changed", "target", "create-lab-item"), ("revision", "other", "create-lab-item"),
        ("revision", "target", "alter-shared-capacity"),
    ]:
        with pytest.raises(LabError):
            approval.authorize(revision, target, operation)
    approval.estimated_cost = 4
    with pytest.raises(LabError):
        approval.authorize("revision", "target", "create-lab-item")


def test_shared_capacity_never_teardown_owned_item():
    with pytest.raises(LabError):
        teardown_preview([{"id": "capacity", "workspace": "lab", "type": "Capacity",
                           "ownership": "CREATED_BY_THIS_LAB"}], "lab")
    with pytest.raises(LabError):
        private_config_path(ROOT / ".local" / "secret.json", ROOT)


def test_runtime_has_no_evaluator_imports_or_dynamic_code():
    for path in (ROOT / "src" / "telco_iq").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("eval", "tools", "data.generator"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile"}


def test_fairness_rejects_unequal_facts_or_static_native():
    import sys
    spec = importlib.util.spec_from_file_location("fairness_test", ROOT / "eval" / "runners" / "fairness.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    arm = module.EvaluationArm(
        "B", "model-v1", "provider", "prompt", "source", "scope", "budgets",
        "cold", "facts", "FLAT", None,
    )
    native = replace(arm, name="C", context_mode="NATIVE_LIVE", context_receipt="receipt")
    module.validate_pair(arm, native)
    with pytest.raises(ValueError, match="facts_hash"):
        module.validate_pair(arm, replace(native, facts_hash="less-informed"))
    with pytest.raises(ValueError, match="actual live"):
        module.validate_pair(arm, replace(native, context_mode="NATIVE_SNAPSHOT"))


async def test_retry_after_is_honored_without_retry_storm():
    calls = []

    async def respond(request):
        calls.append(request)
        return httpx.Response(
            429 if len(calls) < 3 else 200, headers={"Retry-After": "0"}, json={"status": "test"},
        )
    async with httpx.AsyncClient(transport=ReadOnlyRetryTransport(httpx.MockTransport(respond))) as client:
        assert (await client.get("https://example.test/read")).status_code == 200
    assert len(calls) == 3


async def test_large_retry_after_blocks_rather_than_retrying_early():
    calls = []

    async def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})
    async with httpx.AsyncClient(transport=ReadOnlyRetryTransport(httpx.MockTransport(respond))) as client:
        with pytest.raises(LabError, match="no early retry"):
            await client.get("https://example.test/read")
    assert len(calls) == 1
