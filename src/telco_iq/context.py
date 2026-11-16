import json
from pathlib import Path

from telco_iq.contracts import Context, Relationship, Scope, Status
from telco_iq.errors import LabError


def offline_context(scope: Scope) -> Context:
    return Context(
        mode="OFFLINE_SYNTHETIC", revision="offline-v1", source="reviewed local synthetic contracts",
        scope=scope,
        relationships=[Relationship(
            id="account-month-conformed-v1",
            left_model="subscriptions", right_model="finance",
            left_table="subscription_month", right_table="account_month",
            left_keys=["account_key"], right_keys=["account_key"],
            cardinality="one_to_one", version="1",
            aligned_filters=["month"],
            purposes=["Same-month account-grain governed aggregates; both queries must select the same month"],
        )],
        rules=[
            {"id": "R01", "statement": "Use latest complete month common to required sources."},
            {"id": "R02", "statement": "Financial revenue uses finance source measure, not commerce booked revenue."},
            {"id": "R03", "statement": "Snapshots are not additive over time."},
            {"id": "R04", "statement": "Billing keys and account keys require a time-valid approved mapping."},
            {"id": "R10", "statement": "Unauthorized or unavailable data is not zero."},
        ],
        provenance=["Application-owned synthetic intent; NOT native ontology context"],
    )


def load_native_snapshot(path: Path, authorized_scope: Scope) -> Context:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "snapshot_version" in raw:
        from telco_iq.native_context import load_native_bundle

        return load_native_bundle(path, authorized_scope).context
    context = Context.model_validate(raw)
    if context.mode != "NATIVE_SNAPSHOT" or not context.provenance or not context.schema_hash:
        raise LabError("Snapshot requires captured native provenance and schema hash")
    if context.scope != authorized_scope:
        raise LabError("Snapshot authorization does not match current reader", Status.BLOCKED_AUTH)
    return context
