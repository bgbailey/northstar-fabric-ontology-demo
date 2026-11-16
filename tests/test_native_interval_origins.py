"""Synthetic native-edge review tests; no source files, provider calls or business answers."""

from pathlib import Path

import pytest

from telco_iq.catalog import Catalog
from telco_iq.contracts import Relationship, ReviewedIntervalOrigin, TemporalPredicate
from telco_iq.errors import LabError
from telco_iq.native_context import ReviewedExecutionRelationship, _execution_relationships

ROOT = Path(__file__).resolve().parents[1]


def case():
    def relationship(name, left, right, cardinality):
        return Relationship(
            id=name, left_model=left[0], left_table=left[1], right_model=right[0], right_table=right[1],
            left_keys=["site_key"], right_keys=["site_key"], cardinality=cardinality,
            version="synthetic-only", purposes=["Explicit synthetic identity traversal"],
        )

    incident = relationship("incident-site", ("network", "incident"), ("network", "site"), "many_to_one")
    bridge = relationship("site-identity", ("network", "site"), ("enterprise", "site"), "one_to_one")
    mapping = relationship("map-site", ("enterprise", "service_site_map"), ("enterprise", "site"), "many_to_many")
    mapping.temporal = TemporalPredicate(
        left_start="valid_from", left_end="valid_to", right_start="start", right_end="end",
    )
    mapping.right_interval_origin = ReviewedIntervalOrigin(
        model="network", table="incident", grain=["incident_key"], keys=["site_key"],
        via_relationships=["incident-site", "site-identity"],
    )
    edges, reviews = [], []
    for value in (incident, bridge, mapping):
        native_id = "synthetic-native-" + value.id
        edges.append({
            "id": native_id, "left_model": value.left_model, "left_table": value.left_table,
            "right_model": value.right_model, "right_table": value.right_table,
            "left_keys": value.left_keys, "right_keys": value.right_keys,
            "is_active": None, "join_on_date": None, "from_cardinality": None, "to_cardinality": None,
        })
        reviews.append(ReviewedExecutionRelationship(
            native_relationship=native_id, relationship=value, review_reference="Synthetic native-shape unit review only",
        ))
    return Catalog(ROOT / "data" / "contracts" / "catalog.json"), edges, reviews


def reversed_mapping(review):
    value = review.relationship
    return ReviewedExecutionRelationship(
        native_relationship=review.native_relationship, review_reference="Synthetic exact inverse",
        relationship=Relationship(
            id="site-map", left_model=value.right_model, left_table=value.right_table,
            right_model=value.left_model, right_table=value.left_table,
            left_keys=value.right_keys, right_keys=value.left_keys, cardinality="many_to_many",
            version=value.version, purposes=value.purposes,
            temporal=TemporalPredicate(
                left_start=value.temporal.right_start, left_end=value.temporal.right_end,
                right_start=value.temporal.left_start, right_end=value.temporal.left_end,
            ),
            left_interval_origin=value.right_interval_origin,
        ),
    )


def test_carried_interval_review_keeps_each_captured_native_edge_and_exact_reverse():
    catalog, edges, reviews = case()
    reviews.append(reversed_mapping(reviews[-1]))
    result = _execution_relationships(reviews, edges, catalog)
    assert len(result) == 4
    assert result[-1].left_interval_origin == result[-2].right_interval_origin
    assert result[-1].left_table == "site" and result[-1].right_table == "service_site_map"
    assert all(not (r.left_table == "incident" and r.right_table == "service_site_map") for r in result)


@pytest.mark.parametrize("change", [
    "grain", "origin-key", "unknown-model", "unknown-support", "disconnected-support",
    "nonunique-target", "key-changing", "missing-origin", "inactive-support",
])
def test_carried_interval_never_accepts_unreviewed_grain_path_or_dimension_time(change):
    catalog, edges, reviews = case()
    origin = reviews[-1].relationship.right_interval_origin
    if change == "grain":
        origin.grain = ["site_key"]
    elif change == "origin-key":
        origin.keys = ["incident_key"]
    elif change == "unknown-model":
        origin.model = "finance"
    elif change == "unknown-support":
        origin.via_relationships = ["not-captured"]
    elif change == "disconnected-support":
        origin.via_relationships = ["site-identity"]
    elif change == "nonunique-target":
        reviews[0].relationship.cardinality = "many_to_many"
    elif change == "key-changing":
        reviews[1].relationship.left_keys = ["market_id"]
    elif change == "missing-origin":
        reviews[-1].relationship.right_interval_origin = None
    else:
        edges[0]["is_active"] = "false"
    with pytest.raises(LabError):
        _execution_relationships(reviews, edges, catalog)


def test_inverse_carried_interval_cannot_change_origin_side_or_support_path():
    catalog, edges, reviews = case()
    reverse = reversed_mapping(reviews[-1])
    reverse.relationship.left_interval_origin = None
    with pytest.raises(LabError, match="mirror"):
        _execution_relationships([*reviews, reverse], edges, catalog)
