"""Reviewed, submitted-plan fixture; no natural-language routing or native context."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from telco_iq.contracts import Context, Plan, Relationship, Scope, Status, TemporalPredicate
from telco_iq.errors import LabError

REVISION = "offline-sla-v1"
REVIEWED_PERIODS = frozenset(f"2026-{month:02d}" for month in range(3, 9))
CONTRACT_METRICS = (
    "enterprise.eligible_credits",
    "enterprise.credit_exposure",
    "enterprise.applied_credits",
)
ACCOUNT_METRICS = ("finance.recognized_service_revenue", "finance.contribution_margin")


def _scope(scope: Scope) -> None:
    if scope.mode != "OFFLINE_SYNTHETIC":
        raise LabError("SLA submitted-plan fixture is OFFLINE_SYNTHETIC only", Status.BLOCKED_AUTH)
    if not {"network", "enterprise"} <= set(scope.models):
        raise LabError("SLA fixture requires Network and Enterprise source scope", Status.BLOCKED_AUTH)


def sla_context(scope: Scope) -> Context:
    """Return app-owned structural contracts, never captured/native ontology facts."""
    _scope(scope)
    specifications = [
        dict(
            id="sla-incident-site-v1", left_model="network", left_table="incident",
            right_model="network", right_table="site", left_keys=["site_key"], right_keys=["site_key"],
            cardinality="many_to_one", purposes=["Validate incident site against the physical site registry."],
        ),
        dict(
            id="sla-incident-mapping-v1", left_model="network", left_table="incident",
            right_model="enterprise", right_table="service_site_map",
            left_keys=["site_key"], right_keys=["site_key"], cardinality="many_to_many",
            temporal=TemporalPredicate(
                left_start="start", left_end="end", right_start="valid_from", right_end="valid_to",
            ),
            purposes=["Bounded raw interval cohort only; retain incident/mapping intersection."],
        ),
        dict(
            id="sla-mapping-contract-v1", left_model="enterprise", left_table="service_site_map",
            right_model="enterprise", right_table="contract",
            left_keys=["contract_key"], right_keys=["contract_key"], cardinality="many_to_one",
            temporal=TemporalPredicate(
                left_start="valid_from", left_end="valid_to", right_start="valid_from", right_end="valid_to",
            ),
            purposes=["Intersect already narrowed incident/mapping time with effective contract validity."],
        ),
        dict(
            id="sla-contract-settlement-v1", left_model="enterprise", left_table="contract",
            right_model="enterprise", right_table="sla_evaluation",
            left_keys=["contract_key"], right_keys=["contract_key"], cardinality="one_to_one",
            purposes=["Reviewed contract identity; query exactly one SLA service month."],
        ),
        dict(
            id="sla-contract-applied-v1", left_model="enterprise", left_table="contract",
            right_model="enterprise", right_table="applied_credit",
            left_keys=["contract_key"], right_keys=["contract_key"], cardinality="one_to_one",
            purposes=["Reviewed contract identity for the Enterprise Applied Credits source measure."],
        ),
        dict(
            id="sla-settlement-applied-v1", left_model="enterprise", left_table="sla_evaluation",
            right_model="enterprise", right_table="applied_credit",
            left_keys=["contract_key"], right_keys=["contract_key"], cardinality="one_to_one",
            aligned_filters=["month"],
            purposes=["Same contract and service month, not posting month; preserve absent applied rows."],
        ),
    ]
    if "finance" in scope.models:
        specifications.extend([dict(
            id="sla-contract-finance-account-v1", left_model="enterprise", left_table="contract",
            right_model="finance", right_table="account_month",
            left_keys=["account_key"], right_keys=["account_key"], cardinality="many_to_one",
            purposes=[
                "Canonical account identity, not billing_key equality. Deduplicate the cohort before "
                "requesting one Finance account-month row; never attach repeated Finance amounts to contracts.",
            ],
        ), dict(
            id="sla-account-priority-v1", left_model="enterprise", left_table="sla_evaluation",
            right_model="finance", right_table="account_month",
            left_keys=["account_key"], right_keys=["account_key"], cardinality="one_to_one",
            aligned_filters=["month"],
            purposes=["One source-governed account-month aggregate per side; same canonical account and month."],
        )])
    return Context(
        mode="OFFLINE_SYNTHETIC", revision=REVISION,
        source="SUBMITTED PLAN FIXTURE: reviewed local Northstar SLA source contracts",
        scope=scope,
        relationships=[Relationship(version="1", **spec) for spec in specifications],
        rules=[
            {"id": "sla-time", "statement": "Validity intervals are half-open UTC; retain each intersection."},
            {"id": "sla-selection", "statement": "Incidents select affected contracts, not marginal credit amounts."},
            {"id": "sla-month", "statement": "All governed measures use the same selected service month."},
            {"id": "sla-money", "statement": "Enterprise owns monthly thresholds, contract cap and residual cents."},
            {"id": "sla-finance", "statement": "Finance credit has no contract_key; do not invent SLA attribution."},
            {"id": "sla-grain", "statement": "Finance account-month amounts are separate from contract-month rows."},
            {"id": "sla-unknown", "statement": "Missing links are reported or excluded with coverage, never zero."},
        ],
        provenance=[
            "Application-owned OFFLINE_SYNTHETIC structural context; NOT native ontology context.",
            "Source schema and metric identities: data/contracts/catalog.json.",
            "SUBMITTED PLAN FIXTURE; no LLM, natural-language planner, MCP, DAX or live authorization claim.",
        ],
    )


def build_sla_plan(
    scope: Scope, *, period: str = "2026-07", incident_keys: list[int] | None = None,
    view: str = "contracts",
) -> Plan:
    """Build a bounded explicit plan; only source-owned measures calculate financial values."""
    _scope(scope)
    if period not in REVIEWED_PERIODS:
        raise LabError("SLA fixture coverage is reviewed only for 2026-03 through 2026-08",
                       Status.BLOCKED_CAPABILITY)
    if view not in {"contracts", "accounts"}:
        raise LabError("SLA fixture view must be contracts or accounts", Status.AMBIGUOUS)
    if view == "accounts" and "finance" not in scope.models:
        raise LabError("Account priority requires Finance source scope", Status.BLOCKED_AUTH)
    if incident_keys is not None and (
        not isinstance(incident_keys, list) or not incident_keys or len(incident_keys) > 250
        or any(type(key) is not int or key <= 0 for key in incident_keys)
    ):
        raise LabError("incident_keys must be None (all incidents) or 1-250 positive integer keys",
                       Status.AMBIGUOUS)
    month = date.fromisoformat(period + "-01")
    next_month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    start, end = (f"{day.isoformat()}T00:00:00+00:00" for day in (month, next_month))

    def window(left: str, right: str) -> list[dict]:
        return [
            {"column": left, "op": "lt", "values": [end]},
            {"column": right, "op": "gt", "values": [start]},
        ]

    def query(identifier: str, model: str, table: str, columns: list[str], grain: list[str], **kwargs):
        return dict(
            id=identifier, kind="QuerySemanticModel", model=model, table=table,
            columns=columns, expected_grain=grain, max_rows=1000, **kwargs,
        )

    def project(identifier: str, source: str, **columns: str) -> dict:
        return dict(id=identifier, kind="ProjectColumns", source=source, columns=columns)

    def cohort(source: str, source_column: str, target_column: str, relationship: str | None = None):
        return [dict(source=source, source_column=source_column, target_column=target_column,
                     relationship=relationship)]

    filters = window("start", "end")
    if incident_keys is not None:
        filters.append({"column": "incident_key", "op": "in", "values": sorted(set(incident_keys))})
    nodes = [
        query("incidents", "network", "incident", ["incident_key", "site_key", "start", "end"],
              ["incident_key"], date_role="start", filters=filters),
        project("incident_columns", "incidents", incident_key="incident_key", incident_site="site_key",
                incident_start="start", incident_end="end"),
        query("sites", "network", "site", ["site_key"], ["site_key"], date_role=None),
        project("site_columns", "sites", registered_site="site_key"),
        dict(
            id="registered_incidents", kind="JoinResults", left="incident_columns", right="site_columns",
            relationship="sla-incident-site-v1", left_keys=["incident_site"], right_keys=["registered_site"],
            cardinality="many_to_one", unmatched="error",
        ),
        query("mappings", "enterprise", "service_site_map",
              ["service_key", "site_key", "contract_key", "valid_from", "valid_to"],
              ["service_key", "valid_from"], date_role="valid_from", filters=window("valid_from", "valid_to")),
        project("mapping_columns", "mappings", service_key="service_key", mapped_site="site_key",
                mapped_contract="contract_key", mapping_start="valid_from", mapping_end="valid_to"),
        dict(
            id="incident_services", kind="JoinResults", left="registered_incidents", right="mapping_columns",
            relationship="sla-incident-mapping-v1", left_keys=["incident_site"], right_keys=["mapped_site"],
            cardinality="many_to_many", unmatched="exclude", max_fanout=100,
            temporal=dict(left_start="incident_start", left_end="incident_end",
                          right_start="mapping_start", right_end="mapping_end"),
            overlap_start="affected_start", overlap_end="affected_end",
        ),
        query("contracts", "enterprise", "contract", ["contract_key", "account_key", "valid_from", "valid_to"],
              ["contract_key"], date_role="valid_from", filters=window("valid_from", "valid_to")),
        project("contract_columns", "contracts", contract_key="contract_key", account_key="account_key",
                contract_start="valid_from", contract_end="valid_to"),
        dict(
            id="affected_contract_intervals", kind="JoinResults", left="incident_services",
            right="contract_columns", relationship="sla-mapping-contract-v1",
            left_keys=["mapped_contract"], right_keys=["contract_key"], cardinality="many_to_one",
            unmatched="exclude", temporal=dict(
                left_start="affected_start", left_end="affected_end",
                right_start="contract_start", right_end="contract_end",
            ),
            overlap_start="contract_affected_start", overlap_end="contract_affected_end",
        ),
        query("affected_contracts", "enterprise", "contract", ["contract_key", "account_key"],
              ["contract_key"], date_role="valid_from", filters=window("valid_from", "valid_to"),
              cohort_filters=cohort("affected_contract_intervals", "contract_key", "contract_key")),
    ]
    month_filter = [{"column": "month", "op": "eq", "values": [month.isoformat()]}]
    nodes.extend([
        dict(
            id="settlement", kind="QuerySemanticModel", model="enterprise", table="sla_evaluation",
            metrics=list(CONTRACT_METRICS[:2]), group_by=["contract_key", "month"],
            expected_grain=["contract_key", "month"], date_role="month", filters=month_filter,
            cohort_filters=cohort("affected_contracts", "contract_key", "contract_key",
                                  "sla-contract-settlement-v1"),
            max_rows=1000,
        ),
        project("settlement_columns", "settlement", settlement_contract="contract_key", month="month",
                **{metric: metric for metric in CONTRACT_METRICS[:2]}),
        dict(
            id="contract_settlement", kind="JoinResults", left="affected_contracts",
            right="settlement_columns", relationship="sla-contract-settlement-v1",
            left_keys=["contract_key"], right_keys=["settlement_contract"],
            cardinality="one_to_one", unmatched="error",
        ),
    ])
    if view == "contracts":
        nodes.extend([
            dict(
                id="applied", kind="QuerySemanticModel", model="enterprise", table="applied_credit",
                metrics=[CONTRACT_METRICS[2]], group_by=["contract_key"], expected_grain=["contract_key"],
                date_role="month", filters=month_filter,
                cohort_filters=cohort("affected_contracts", "contract_key", "contract_key",
                                      "sla-contract-applied-v1"),
                max_rows=1000,
            ),
            project("applied_columns", "applied", applied_contract="contract_key",
                    **{CONTRACT_METRICS[2]: CONTRACT_METRICS[2]}),
            dict(
                id="monthly_credits", kind="JoinResults", left="contract_settlement", right="applied_columns",
                relationship="sla-settlement-applied-v1", left_keys=["settlement_contract"],
                right_keys=["applied_contract"], cardinality="one_to_one", unmatched="preserve",
            ),
            project("answer", "monthly_credits", contract_key="contract_key", account_key="account_key",
                    month="month", **{metric: metric for metric in CONTRACT_METRICS}),
        ])
        selections = list(CONTRACT_METRICS)
    else:
        nodes.extend([
            dict(
                id="account_sla", kind="QuerySemanticModel", model="enterprise", table="sla_evaluation",
                metrics=list(CONTRACT_METRICS[:2]), group_by=["account_key", "month"],
                expected_grain=["account_key", "month"], date_role="month", filters=month_filter,
                cohort_filters=cohort("contract_settlement", "contract_key", "contract_key",
                                      "sla-contract-settlement-v1"),
                max_rows=1000,
            ),
            dict(
                id="finance", kind="QuerySemanticModel", model="finance", table="account_month",
                metrics=list(ACCOUNT_METRICS), group_by=["account_key", "month"],
                expected_grain=["account_key", "month"], date_role="month", filters=month_filter,
                cohort_filters=cohort("contract_settlement", "account_key", "account_key",
                                      "sla-contract-finance-account-v1"),
                max_rows=1000,
            ),
            project("finance_columns", "finance", finance_account="account_key",
                    **{metric: metric for metric in ACCOUNT_METRICS}),
            dict(
                id="account_priority", kind="JoinResults", left="account_sla", right="finance_columns",
                relationship="sla-account-priority-v1", left_keys=["account_key"],
                right_keys=["finance_account"], cardinality="one_to_one", unmatched="error",
            ),
            project("answer", "account_priority", account_key="account_key", month="month",
                    **{metric: metric for metric in (*CONTRACT_METRICS[:2], *ACCOUNT_METRICS)}),
        ])
        selections = [*CONTRACT_METRICS[:2], *ACCOUNT_METRICS]
    return Plan.model_validate(dict(
        request_id=f"sla-fixture-{uuid4().hex}",
        question=f"SUBMITTED PLAN FIXTURE | OFFLINE_SYNTHETIC | {period} SLA {view}",
        principal_scope=scope,
        as_of=datetime(2026, 9, 29, tzinfo=timezone.utc), ontology_revision=REVISION,
        hypotheses=[
            "Explicit submitted plan; not natural-language planning, native context, or live DAX execution.",
            "Selected incidents restrict affected contracts; monthly credit totals are NOT marginal incident credits.",
            "Interval membership uses half-open UTC bounds, not start-month equality.",
            "Enterprise owns the service threshold, one cap per contract-month, applied credits and residual.",
            "Absent Applied Credits rows stay null, not invented values; Credit Exposure remains source-owned.",
            "Unmatched intervals retain coverage; unknown identities block downstream totals. "
            "Missing site or settlement fails.",
            "Finance account-month revenue/margin are a separate deduplicated view, not contract attribution.",
        ],
        metric_selections=selections, nodes=nodes, result_node="answer",
        budgets=dict(max_rows=20000, max_bytes=8000000, max_queries=8, timeout_seconds=45),
    ))
