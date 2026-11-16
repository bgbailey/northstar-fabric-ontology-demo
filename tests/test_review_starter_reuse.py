"""Synthetic unit plans only; do not generate live numerical acceptance."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from tools.review_starter_reuse import METRICS, review


@pytest.mark.parametrize("identity", ["account_key", "account_id"])
def test_source_owned_starter_accepts_either_original_registered_account_identity(tmp_path, monkeypatch, identity):
    row = {identity: 1 if identity == "account_key" else "SYNTHETIC_ACCOUNT"}
    node = SimpleNamespace(
        id="registry", kind="QuerySemanticModel", table="account", columns=[identity],
        filters=[], cohort_filters=[],
    )
    receipt = SimpleNamespace(
        node_id="registry", model="finance", returned_rows=1, status="COMPLETE",
        query="SYNTHETIC NOT EXECUTED", query_hash="a" * 64,
    )
    arms = [SimpleNamespace(
        arm=arm, status="COMPLETE",
        plan=SimpleNamespace(metric_selections=sorted(METRICS), nodes=[node]),
        execution=SimpleNamespace(
            answer=SimpleNamespace(rows=[row]),
            receipts=[receipt, SimpleNamespace(**{**vars(receipt), "model": "subscriptions"})],
        ),
    ) for arm in ("baseline", "native")]
    pair = SimpleNamespace(pair_id="UNIT", arms=arms, comparability="COMMON_SOURCE_STATE")
    monkeypatch.setattr("tools.review_starter_reuse.ComparisonResult.model_validate_json", lambda raw: pair)
    path = tmp_path / "synthetic-pair.json"
    path.write_text(json.dumps({"unit": True}), encoding="utf-8")
    value_review = {
        "pair_id": "UNIT", "original_result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "arms": [{"arm": arm, "metric_cells_checked": 3} for arm in ("baseline", "native")],
    }
    catalog = SimpleNamespace(metrics={
        metric: {key: "UNIT" for key in ("id", "model", "table", "name", "aggregation", "unit")}
        for metric in METRICS
    })
    result = review(path, value_review, catalog)
    assert all(arm["registered_customer_identity_column"] == identity for arm in result["arms"])
    row[identity] = None
    with pytest.raises(ValueError, match="registered-account"):
        review(path, value_review, catalog)
