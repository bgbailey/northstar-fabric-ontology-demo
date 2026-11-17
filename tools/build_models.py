"""Generate local, undeployed TMDL templates and the runtime's metric catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

try:
    from .build_data import MODEL_NAMES, ROOT, TABLE_BY_ID, TABLES, source_tables
except ImportError:
    from build_data import MODEL_NAMES, ROOT, TABLE_BY_ID, TABLES, source_tables

UPSTREAM_COMMIT = "6c11ad58c25992e5d1435ce7cd80d217d5598a31"
GUIDANCE = [
    "skills/semantic-model-authoring/SKILL.md",
    *[f"skills/semantic-model-authoring/references/{name}.md" for name in (
        "modeling-guidelines", "direct-lake-guidelines", "tmdl-guidelines", "dax-guidelines", "pbip")],
]
DIMENSIONS = {
    "account_key": ("account", "account_key"), "market_id": ("market", "market_id"),
    "month": ("calendar_date", "date"), "site_key": ("site", "site_key"),
    "device_key": ("device", "device_key"), "store_key": ("store", "store_key"),
    "campaign_key": ("campaign", "campaign_key"), "service_key": ("service", "service_key"),
}


def quoted(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def lineage(path: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"urn:northstar-wireless:telco-iq:1.0:{path}"))


def ref(table_id: str, column: str) -> str:
    return f"{quoted(TABLE_BY_ID[table_id]['displayName'])}[{column}]"


def metrics() -> list[dict]:
    result = []

    def add(
        model: str, home: str, slug: str, name: str, sql: str, dax: str,
        description: str, unit: str = "count", aggregation: str = "additive",
        date_column: str = "month", extra_dimensions: tuple[str, ...] = (),
    ) -> None:
        spec = TABLE_BY_ID[home]
        dimensions = [name for name in spec["columns"] if name in {
            "month", "market_id", "account_key", "site_key", "device_key", "store_key",
            "campaign_key", "randomized", "treatment", "snapshot_date", "release_version", "reason",
        }]
        if not set(extra_dimensions) <= spec["columns"].keys() or set(extra_dimensions) & set(dimensions):
            raise ValueError("Additional metric dimensions must be distinct source columns")
        dimensions = list(extra_dimensions) + dimensions
        if aggregation == "semi_additive":
            dax = f"IF(HASONEVALUE({ref(home, date_column)}), {dax})"
            description += f" Requires one {date_column} per result; never sum snapshots over time."
        result.append({
            "id": f"{model}.{slug}", "model": model, "table": home, "name": name,
            "dax": dax, "sql": sql, "aggregation": aggregation, "unit": unit,
            "description": description, "dateColumn": date_column, "allowedDimensions": dimensions,
        })

    def total(model: str, home: str, slug: str, name: str, column: str, description: str,
              unit: str = "count", aggregation: str = "additive", date_column: str = "month",
              extra_dimensions: tuple[str, ...] = ()) -> None:
        add(model, home, slug, name, f"SUM({column})", f"SUM({ref(home, column)})",
            description, unit, aggregation, date_column, extra_dimensions)

    def count(model: str, home: str, slug: str, name: str, description: str) -> None:
        add(model, home, slug, name, "COUNT(*)", f"COUNTROWS({quoted(TABLE_BY_ID[home]['displayName'])})", description)

    def ratio(model: str, home: str, slug: str, name: str, numerator: str, denominator: str,
              description: str, unit: str = "ratio") -> None:
        add(model, home, slug, name,
            f"SUM({numerator}) * 1.0 / NULLIF(SUM({denominator}), 0)",
            f"DIVIDE(SUM({ref(home, numerator)}), SUM({ref(home, denominator)}))",
            description, unit, "non_additive")

    m, t = "subscriptions", "subscription_month"
    total(m, t, "active_lines_eom", "Active Lines EOM", "active_eom", "Active subscriptions at month end.", aggregation="semi_additive")
    add(m, t, "active_accounts_eom", "Active Accounts EOM",
        "COUNT(DISTINCT CASE WHEN active_eom = 1 THEN account_key END)",
        f"IF(HASONEVALUE({ref(t, 'month')}), CALCULATE(DISTINCTCOUNT({ref(t, 'account_key')}), KEEPFILTERS({ref(t, 'active_eom')} = 1)))",
        "Distinct accounts with active lines in one month; neither account counts nor snapshots can be summed.",
        aggregation="non_additive")
    total(m, t, "gross_adds", "Gross Adds", "gross_add", "Transitions from inactive to active.")
    total(m, t, "disconnects", "Disconnects", "disconnect", "Disconnects from the opening-active cohort.")
    add(m, t, "net_adds", "Net Adds", "SUM(gross_add) - SUM(disconnect)",
        f"SUM({ref(t, 'gross_add')}) - SUM({ref(t, 'disconnect')})", "Gross additions less cohort-qualified disconnects.")
    total(m, t, "opening_active_lines", "Opening Active Lines", "opening_active",
          "Lines active at the start of the month.", aggregation="semi_additive")
    ratio(m, t, "churn_rate", "Churn Rate", "disconnect", "opening_active",
          "Opening-cohort disconnects divided by opening-active lines; recompute from pooled components.")
    add(m, t, "average_active_lines", "Average Active Lines",
        "(SUM(opening_active) + SUM(active_eom)) / 2.0",
        f"DIVIDE(SUM({ref(t, 'opening_active')}) + SUM({ref(t, 'active_eom')}), 2)",
        "Midpoint of opening and closing line population for a single month.", "lines", "semi_additive")

    m, t = "finance", "account_month"
    total(m, t, "recognized_service_revenue", "Recognized Service Revenue", "revenue_cents",
          "Recognized service revenue before service-month credits, in integer cents.", "cents")
    total(m, "charge_line", "billed_charges", "Billed Charges", "amount_cents",
          "Signed charges including reversals, grouped by service month; not cash receipts.", "cents")
    total(m, "credit", "posted_credits", "Posted Credits", "amount_cents",
          "Credit amounts by posting month; service month remains a separate attribute.", "cents")
    add(m, t, "net_recognized_revenue", "Net Recognized Revenue",
        "SUM(revenue_cents) - SUM(credit_cents)",
        f"SUM({ref(t, 'revenue_cents')}) - SUM({ref(t, 'credit_cents')})",
        "Recognized service revenue minus service-month credits.", "cents")
    total(m, t, "service_cost", "Service Cost", "service_cost_cents",
          "Attributed monthly service cost, not device or offer cost.", "cents")
    margin_sql = "SUM(revenue_cents) - SUM(credit_cents) - SUM(service_cost_cents)"
    margin_dax = f"SUM({ref(t, 'revenue_cents')}) - SUM({ref(t, 'credit_cents')}) - SUM({ref(t, 'service_cost_cents')})"
    add(m, t, "contribution_margin", "Contribution Margin", margin_sql, margin_dax,
        "Net recognized service revenue minus attributed service cost.", "cents")
    add(m, t, "margin_percent", "Margin Percent",
        f"({margin_sql}) * 1.0 / NULLIF(SUM(revenue_cents) - SUM(credit_cents), 0)",
        f"DIVIDE({margin_dax}, SUM({ref(t, 'revenue_cents')}) - SUM({ref(t, 'credit_cents')}))",
        "Pooled contribution margin divided by pooled net recognized revenue; never average account percentages.",
        "ratio", "non_additive")
    ratio(m, t, "revenue_per_active_line", "Revenue per Active Line", "revenue_cents", "active_lines_eom",
          "Service revenue per closing active line in the same account-month scope; multiple months yield a line-month denominator.",
          "cents_per_line")

    m, t = "digital", "session"
    count(m, t, "sessions", "Sessions", "Observed sessions including anonymous browsing.")
    add(m, t, "authenticated_sessions", "Authenticated Sessions",
        "SUM(CASE WHEN authenticated THEN 1 ELSE 0 END)",
        f"CALCULATE(COUNTROWS('Sessions'), KEEPFILTERS({ref(t, 'authenticated')} = TRUE()))",
        "Sessions with resolved authenticated identity.")
    total(m, t, "eligible_checkout_sessions", "Eligible Checkout Sessions", "eligible_checkout", "Sessions eligible to attempt checkout.")
    total(m, t, "successful_checkout_sessions", "Successful Checkout Sessions", "successful_checkout", "Successful eligible checkouts; not necessarily placed orders.")
    ratio(m, t, "checkout_conversion", "Checkout Conversion", "successful_checkout", "eligible_checkout", "Successful divided by eligible checkout sessions.")
    total(m, t, "failed_checkouts", "Failed Checkouts", "failed_checkout", "Observed eligible failures; abandonment is not a lost-order assertion.")
    add(m, t, "digital_active_accounts", "Digital Active Accounts", "COUNT(DISTINCT account_key)",
        f"COUNTROWS(FILTER(VALUES({ref(t, 'account_key')}), NOT ISBLANK({ref(t, 'account_key')})))",
        "Distinct resolved accounts; anonymous/null identities are excluded.", aggregation="non_additive")
    add(m, t, "median_journey_duration", "Median Journey Duration", "MEDIAN(duration_seconds)",
        f"MEDIAN({ref(t, 'duration_seconds')})", "Median of session durations in the requested scope; never average medians.",
        "seconds", "non_additive")

    m, t = "care", "case"
    total(m, "case_daily_snapshot", "open_cases_eod", "Open Cases EOD", "open_eod",
          "Open cases at a specified observed day end.", aggregation="semi_additive", date_column="snapshot_date")
    count(m, t, "new_cases", "New Cases", "Cases by opening month, independent of activity count.")
    total(m, t, "resolved_cases", "Resolved Cases", "resolved", "Cases resolved by source observation, attributed to opening month.")
    total(m, t, "repeat_contacts", "Repeat Contacts", "repeat_contact", "Subsequent same-reason contacts within seven days for the same account.")
    add(m, t, "contacted_accounts", "Contacted Accounts", "COUNT(DISTINCT account_key)",
        f"DISTINCTCOUNT({ref(t, 'account_key')})", "Distinct contacted accounts, not the sum of period-level distinct counts.", aggregation="non_additive")
    total(m, t, "escalated_cases", "Escalated Cases", "escalated",
          "Cases escalated at least once, attributed to case opening month.")
    ratio(m, t, "first_contact_resolution", "First Contact Resolution", "first_contact_resolved", "resolved",
          "First-contact resolutions divided by resolved cases; unresolved cases excluded from denominator.")
    add(m, t, "mean_resolution_hours", "Mean Resolution Hours", "AVG(resolution_minutes) / 60.0",
        f"DIVIDE(AVERAGE({ref(t, 'resolution_minutes')}), 60)",
        "Mean observed duration among resolved cases; unresolved durations remain null.", "hours", "non_additive")

    m, t = "network", "site_hour"
    total(m, t, "eligible_site_minutes", "Eligible Site Minutes", "eligible_minutes", "Monitored site minutes in the bounded telemetry window.", "minutes")
    total(m, t, "unavailable_site_minutes", "Unavailable Site Minutes", "unavailable_minutes", "Per-site interval-unioned unavailability, without double-counting overlaps.", "minutes")
    add(m, t, "availability_percent", "Availability Percent",
        "(SUM(eligible_minutes) - SUM(unavailable_minutes)) * 1.0 / NULLIF(SUM(eligible_minutes), 0)",
        f"DIVIDE(SUM({ref(t, 'eligible_minutes')}) - SUM({ref(t, 'unavailable_minutes')}), SUM({ref(t, 'eligible_minutes')}))",
        "Eligible-minute weighted availability; not the mean of site percentages.", "ratio", "non_additive")
    add(m, "incident", "major_incidents", "Major Incidents",
        "SUM(CASE WHEN severity = 'major' THEN 1 ELSE 0 END)",
        "CALCULATE(COUNTROWS('Incidents'), KEEPFILTERS('Incidents'[severity] = \"major\"))", "Raw major incident count; overlap is allowed.")
    add(m, "subscription_site_exposure", "exposed_lines", "Exposed Lines",
        "COUNT(DISTINCT CASE WHEN exposure_minutes > 0 THEN subscription_key END)",
        "CALCULATE(DISTINCTCOUNT('Subscription Site Exposure'[subscription_key]), KEEPFILTERS('Subscription Site Exposure'[exposure_minutes] > 0))",
        "Distinct active lines exposed to actual serving-site outages.", aggregation="non_additive")
    total(m, "subscription_site_exposure", "exposure_minutes", "Exposure Minutes", "exposure_minutes",
          "Interval-unioned unavailable minutes per exposed line, additive across lines.", "line_minutes")
    total(m, t, "failed_sessions", "Failed Sessions", "failed_sessions",
          "Observed failed radio/network sessions in the hourly monitoring window; not web or app sessions.")
    ratio(m, t, "service_degradation_percent", "Service Degradation Percent", "failed_sessions", "total_sessions", "Failed divided by monitored network sessions.")

    m, t = "commerce", "order_line"
    count(m, "order", "orders", "Orders", "Placed orders, including subsequent cancellations.")
    total(m, t, "units", "Booked Units", "units", "Booked merchandise units.")
    total(m, t, "booked_revenue", "Booked Revenue", "booked_revenue_cents",
          "Booked merchandise and device value in integer cents at order placement, not recurring service billing.",
          "cents")
    total(m, "order", "cancellations", "Cancellations", "cancelled", "Orders cancelled before fulfilment.")
    total(m, t, "fulfilled_units", "Fulfilled Units", "fulfilled_units", "Units actually fulfilled.")
    ratio(m, t, "device_return_rate", "Device Return Rate", "returned_units", "fulfilled_units", "Returned units divided by fulfilled units, attributed to order month.")
    total(m, "store_device_day", "inventory_eod", "Inventory EOD", "inventory_eod",
          "Physical units at one observed day end.", "units", "semi_additive", "snapshot_date")
    total(m, "store_device_day", "stockout_store_days", "Stockout Store Days", "stockout",
          "Store-device-day stockout observations; across devices this is not distinct store days.", "store_device_days")

    m, t = "growth", "offer_assignment"
    total(m, t, "eligible_population", "Eligible Population", "eligible", "Eligible assignment units; separate campaign populations before interpretation.")
    total(m, t, "assigned_population", "Assigned Population", "assigned", "Eligible treatment units actually assigned an offer.")
    total(m, t, "accepted_offers", "Accepted Offers", "accepted", "Assigned eligible offers accepted.")
    ratio(m, t, "acceptance_rate", "Acceptance Rate", "accepted", "assigned", "Accepted divided by assigned offers, not campaign touches.")
    total(m, t, "offer_cost", "Offer Cost", "offer_cost_cents",
          "Observed accepted-offer cost in integer cents for eligible assignments.", "cents")
    for treatment, slug, name, arm_label in (
        (1, "treatment_retained", "Treatment Retained", "treatment-arm"),
        (0, "control_retained", "Control Retained", "control-arm"),
    ):
        add(m, t, slug, name,
            f"SUM(CASE WHEN randomized AND eligible = 1 AND treatment = {treatment} THEN retained ELSE 0 END)",
            f"CALCULATE(SUM({ref(t, 'retained')}), KEEPFILTERS({ref(t, 'randomized')} = TRUE()), KEEPFILTERS({ref(t, 'eligible')} = 1), KEEPFILTERS({ref(t, 'treatment')} = {treatment}))",
            f"Retained eligible randomized {arm_label} units; observational campaign excluded.")
    sql_rates, dax_rates = [], []
    for treatment in (1, 0):
        condition = f"randomized AND eligible = 1 AND treatment = {treatment}"
        sql_rates.append(f"SUM(CASE WHEN {condition} THEN retained ELSE 0 END) * 1.0 / NULLIF(SUM(CASE WHEN {condition} THEN 1 ELSE 0 END), 0)")
        filters = f"KEEPFILTERS({ref(t, 'randomized')} = TRUE()), KEEPFILTERS({ref(t, 'eligible')} = 1), KEEPFILTERS({ref(t, 'treatment')} = {treatment})"
        dax_rates.append(f"DIVIDE(CALCULATE(SUM({ref(t, 'retained')}), {filters}), CALCULATE(COUNTROWS('Offer Assignments'), {filters}))")
    add(m, t, "retention_lift", "Retention Lift", f"({sql_rates[0]}) - ({sql_rates[1]})",
        f"VAR _t = {dax_rates[0]} VAR _c = {dax_rates[1]} RETURN IF(NOT ISBLANK(_t) && NOT ISBLANK(_c), _t - _c)",
        "Eligible randomized intention-to-treat retention rate difference; null if either arm is absent. Not an observational causal claim.",
        "percentage_points", "non_additive")

    m, t = "enterprise", "service_month"
    total(m, t, "contracted_services", "Contracted Services", "contracted", "Valid contracted service population in one month.", aggregation="semi_additive")
    total(m, t, "monitored_service_minutes", "Monitored Service Minutes", "monitored_minutes", "Calendar minutes clipped to contract validity.", "minutes")
    total(m, t, "breached_services", "Breached Services", "breached", "Service-months exceeding their contract threshold.")
    add(m, t, "sla_compliance", "SLA Compliance",
        "(SUM(contracted) - SUM(breached)) * 1.0 / NULLIF(SUM(contracted), 0)",
        f"DIVIDE(SUM({ref(t, 'contracted')}) - SUM({ref(t, 'breached')}), SUM({ref(t, 'contracted')}))",
        "Compliant contracted service-months divided by contracted service-months.", "ratio", "non_additive")
    total(m, t, "contracted_recurring_revenue", "Contracted Recurring Revenue", "recurring_revenue_cents",
          "Contracted monthly recurring service value in integer cents, not billed or recognized revenue.",
          "cents")
    total(m, "sla_evaluation", "eligible_credits", "Eligible Credits", "eligible_credit_cents",
          "Validity-clipped outage union per service-contract-month; threshold per service, then one monetary cap per contract-month.", "cents",
          extra_dimensions=("contract_key",))
    total(m, "applied_credit", "applied_credits", "Applied Credits", "amount_cents",
          "Applied credits attributed to SLA service month, with separate posting timestamp.", "cents",
          extra_dimensions=("contract_key",))
    add(m, "sla_evaluation", "credit_exposure", "Credit Exposure",
        "SUM(eligible_credit_cents) - SUM(applied_credit_cents)",
        "SUM('SLA Evaluation'[eligible_credit_cents]) - SUM('SLA Evaluation'[applied_credit_cents])",
        "Eligible less already applied contract-month credits; not a repeated per-incident cap.", "cents",
        extra_dimensions=("contract_key",))
    return result


def relationships(model_id: str) -> list[dict]:
    result = []
    for spec in TABLES:
        if spec["model"] != model_id:
            continue
        for key, (dimension, target) in DIMENSIONS.items():
            if key in spec["columns"]:
                if key == "month" and spec["id"] in {"contract", "service_site_map"}:
                    continue
                source = "snapshot_date" if key == "month" and "snapshot_date" in spec["columns"] else key
                result.append({"fromTable": spec["id"], "fromColumn": source,
                               "toTable": dimension, "toColumn": target})
    return result


def catalog(*, showcase: bool = False) -> dict:
    models = []
    for model_id, name in MODEL_NAMES.items():
        own = [spec["id"] for spec in TABLES if spec["model"] == model_id]
        shared = sorted({rel["toTable"] for rel in relationships(model_id)})
        models.append({"id": model_id, "name": name, "tables": own + shared})
    return {"version": "1.0", "models": models,
            "tables": source_tables("showcase-v2") if showcase else TABLES, "metrics": metrics()}


COLUMN_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("account", "region"): (
        "Geographic operating region (such as East or West) for regional grouping and row-level security."
    ),
    ("account", "consent"): (
        "Current marketing contact preference flag; does not reconstruct historical campaign eligibility."
    ),
    ("account", "enterprise"): (
        "True for business accounts eligible for service contracts; false for consumer accounts."
    ),
    ("account", "display_name"): (
        "Customer-facing account label; deliberately non-unique across accounts, never use as a join key."
    ),
    ("market", "market_name"): "Human-readable home market label for customer and regional reporting.",
    ("market", "region"): (
        "Parent operating region grouping home markets for regional summaries and security filtering."
    ),
    ("calendar_date", "date"): (
        "Calendar day (yyyy-MM-dd) linking first-of-month facts and daily operational snapshots."
    ),
    ("calendar_date", "year"): "Four-digit calendar year number; group or filter by year, never sum.",
    ("calendar_date", "quarter"): "Calendar quarter number (1 to 4); group or filter by quarter, never sum.",
    ("calendar_date", "month_number"): (
        "Calendar month number (1 to 12) used to sort month names chronologically; never sum."
    ),
    ("calendar_date", "month_name"): "Full English calendar month label sorted chronologically by month_number.",
    ("calendar_date", "day"): "Day-of-month number (1 to 31) for daily snapshot filtering; never sum.",
    ("site", "region"): (
        "Physical site operating region; reflects tower or facility location, not customer home market."
    ),
    ("device", "device_name"): "Commercial handset or hardware model label for device cohort breakdown.",
    ("device", "operating_system"): (
        "Device platform family (such as iOS or Android) for compatibility and cohort analysis."
    ),
    ("store", "store_name"): (
        "Retail or fulfillment location label for store-level order and inventory reporting."
    ),
    ("campaign", "campaign_name"): (
        "Retention or promotional program label; check assignment_method before causal comparison."
    ),
    ("campaign", "assignment_method"): (
        "Experiment design type distinguishing randomized holdout trials from observational targeting."
    ),
    ("subscription", "plan_name"): "Commercial wireless rate-plan tier assigned to the subscription line.",
    ("subscription_month", "month"): (
        "First day of the reporting month for subscription state; select a single month for snapshots."
    ),
    ("lifecycle_event", "month"): (
        "First day of the calendar month in which the activation or disconnection transition occurred."
    ),
    ("lifecycle_event", "event_type"): (
        "Lifecycle transition category (activation or disconnect) recorded for the subscription line."
    ),
    ("account_month", "month"): (
        "First day of the service month for recognized revenue, service-month credits, and cost."
    ),
    ("charge_line", "month"): "First day of the billed service month; separate from the posting timestamp.",
    ("charge_line", "charge_type"): (
        "Billing line category distinguishing recurring service charges, usage charges, and reversals."
    ),
    ("credit", "month"): (
        "First day of the credit posting month (primary calendar link); differs from original service_month."
    ),
    ("credit", "service_month"): (
        "First day of the original service period adjusted by a late-posted billing credit."
    ),
    ("session", "month"): (
        "First day of the calendar month when the digital browsing or checkout session started."
    ),
    ("session", "authenticated"): (
        "True when the session resolved to a signed-in account; false for anonymous browsing."
    ),
    ("session", "release_version"): (
        "Client app or web release identifier active during the session for defect triage."
    ),
    ("interaction_event", "month"): (
        "First day of the calendar month in which the in-session interaction occurred."
    ),
    ("interaction_event", "event_type"): (
        "Step or action category within a session; multiple interaction events occur per session."
    ),
    ("case", "month"): (
        "First day of the calendar month when the support case opened; governs case and resolution cohorts."
    ),
    ("case", "reason"): (
        "Primary customer contact driver (such as connectivity, billing, or device) recorded at intake."
    ),
    ("case_activity", "month"): (
        "First day of the calendar month when the agent or customer case interaction occurred."
    ),
    ("case_activity", "activity_type"): (
        "Support handling step category (such as intake, escalation, or follow-up) within a case."
    ),
    ("case_daily_snapshot", "month"): (
        "First day of the calendar month containing the daily open-case observation."
    ),
    ("case_daily_snapshot", "snapshot_date"): (
        "Observed end-of-day date for open case backlog; select one date, never sum across days."
    ),
    ("incident", "month"): (
        "First day of the calendar month when the outage started; cross-month overlap uses UTC start/end."
    ),
    ("incident", "severity"): (
        "Operational impact tier (major or minor); filter to major for headline outage counts."
    ),
    ("site_hour", "month"): "First day of the calendar month for the bounded hourly site-monitoring window.",
    ("site_day", "month"): "First day of the calendar month containing the daily site-monitoring rollup.",
    ("site_day", "snapshot_date"): "Calendar day of the bounded daily site-availability rollup.",
    ("subscription_site_exposure", "month"): (
        "First day of the service month for interval-unioned serving-site outage exposure."
    ),
    ("order", "month"): (
        "First day of the order placement month; fulfillment and return dates are tracked separately."
    ),
    ("order_line", "month"): (
        "First day of the parent order placement month governing booked units, revenue, and return rates."
    ),
    ("store_device_day", "month"): (
        "First day of the calendar month containing the store-device inventory observation."
    ),
    ("store_device_day", "snapshot_date"): (
        "Observed end-of-day date for physical store inventory; select one date, never sum across days."
    ),
    ("offer_assignment", "month"): (
        "First day of the campaign assignment month; later 28-day outcomes retain this cohort month."
    ),
    ("offer_assignment", "randomized"): (
        "True for randomized treatment/control experiment units; false for observational targeting."
    ),
    ("offer_outcome", "month"): (
        "First day of the original offer assignment month associated with the 28-day retention check."
    ),
    ("campaign_touch", "month"): (
        "First day of the calendar month when the outbound or inbound campaign contact occurred."
    ),
    ("campaign_touch", "channel"): (
        "Outreach delivery medium (such as email, SMS, or in-app) for the campaign touch."
    ),
    ("contract", "month"): (
        "Contract start-month attribute only, not active service-month validity; use UTC valid_from/valid_to."
    ),
    ("service_site_map", "month"): (
        "Mapping start-month attribute only, not active serving-site validity; use UTC valid_from/valid_to."
    ),
    ("service_month", "month"): (
        "First day of the evaluated service month for contracted services, monitored minutes, and breaches."
    ),
    ("sla_evaluation", "month"): "First day of the settled SLA service month at contract-month grain.",
    ("applied_credit", "month"): (
        "First day of the SLA service month credited; separate from the posted_at timestamp."
    ),
    ("quarantine", "source_table"): (
        "Originating staging table identifier from which the rejected record was isolated."
    ),
    ("quarantine", "reason_code"): (
        "Data-quality rejection classification explaining why the record was quarantined."
    ),
    ("quarantine", "raw_record"): (
        "Serialized unparsed source payload retained strictly for data-quality diagnostics."
    ),
}


def column_description(identifier: str, spec: dict, column: str, kind: str) -> str:
    explicit = COLUMN_DESCRIPTIONS.get((identifier, column))
    if explicit is not None:
        return explicit
    if column == "coverage_complete":
        return (
            "Effective-period serving-site mapping covers every expected minute; "
            "false means unknown outage/breach."
        )
    if column == "expected_minutes":
        return "Calendar minutes clipped to contract validity; compare with actual monitored minutes."
    if column.endswith("_cents"):
        return "Hidden integer cents; query explicit currency measures instead of summing raw columns."
    if column.endswith("_minutes"):
        return "Hidden duration in minutes; query explicit duration or availability measures instead."
    if column.endswith("_seconds"):
        return "Hidden duration in seconds; query explicit duration measures instead of summing."
    if kind == "timestamp":
        return "Hidden UTC instant; half-open validity intervals include start and exclude end."
    if kind == "date":
        return "Calendar date (yyyy-MM-dd) with no timezone or time-of-day component."
    if column.endswith(("_key", "_id")):
        return "Hidden technical identifier; join only through declared relationships or identity maps."
    if kind == "int64":
        return f"Hidden numeric input on {spec['displayName']}; use explicit model measures instead."
    label = column.replace("_", " ").capitalize()
    return f"{label} attribute scoped to {spec['displayName']}."


def format_string(metric: dict) -> str:
    if metric["unit"] in ("ratio", "percentage_points"):
        return "0.00%"
    if metric["unit"] == "cents":
        return '#,##0" cents"'
    if metric["aggregation"] == "non_additive" or metric["unit"] == "hours":
        return "#,##0.00"
    return "#,##0"


def _check_description(text: str, target: str) -> str:
    if not text or not text.strip() or len(text) > 200:
        raise ValueError(f"TMDL description must be 1..200 chars ({len(text)}): {target}")
    return text


def table_tmdl(model_id: str, spec: dict, measures: list[dict]) -> str:
    identifier = spec["id"]
    name = quoted(spec["displayName"])
    columns = {column.casefold() for column in spec["columns"]}
    for metric in measures:
        if metric["name"].casefold() in columns:
            raise ValueError(
                f"Measure name collides with a home-table column: {model_id}.{identifier}.{metric['name']}"
            )
    table_desc = _check_description(spec["description"], f"{model_id}.{identifier}")
    lines = [f"/// {table_desc}", f"table {name}",
             f"\tlineageTag: {lineage(model_id + '/table/' + identifier)}"]
    if "coverage" in spec:
        lines += ["\tannotation SourceCoverage = " + json.dumps(spec["coverage"], sort_keys=True)]
    if identifier == "calendar_date":
        lines += ["\tdataCategory: Time"]
    for metric in measures:
        metric_desc = _check_description(metric["description"], metric["id"])
        lines += ["", f"\t/// {metric_desc}", f'\tmeasure {quoted(metric["name"])} = {metric["dax"]}',
                  f"\t\tformatString: {format_string(metric)}",
                  f"\t\tdisplayFolder: {MODEL_NAMES[model_id]}",
                  f'\t\tlineageTag: {lineage(metric["id"])}',
                  f'\t\tannotation MetricId = {metric["id"]}',
                  f'\t\tannotation Aggregation = {metric["aggregation"]}']
    dtype_map = dict(int64="int64", string="string", bool="boolean", date="dateTime", timestamp="dateTime")
    for column, kind in spec["columns"].items():
        hidden = (
            column in {"coverage_complete", "expected_minutes"}
            or column.endswith(("_key", "_id"))
            or kind == "timestamp"
            or (kind == "int64" and identifier != "calendar_date")
        )
        meaning = _check_description(
            column_description(identifier, spec, column, kind), f"{model_id}.{identifier}.{column}"
        )
        lines += ["", f"\t/// {meaning}", f"\tcolumn {quoted(column)}",
                  f"\t\tdataType: {dtype_map[kind]}",
                  "\t\tsummarizeBy: none", f"\t\tsourceColumn: {column}",
                  f"\t\tlineageTag: {lineage(model_id + '/column/' + identifier + '/' + column)}"]
        if hidden:
            lines += ["\t\tisHidden", "\t\tisAvailableInMdx: false"]
        if spec["model"] == "core" and spec["primaryKey"] == [column]:
            lines += ["\t\tisKey"]
        if kind == "date":
            lines += ["\t\tformatString: yyyy-MM-dd"]
        elif kind == "timestamp":
            lines += ["\t\tformatString: yyyy-MM-dd HH:mm:ss", "\t\tannotation Timezone = UTC"]
        elif kind == "int64":
            lines += ["\t\tformatString: #,##0"]
        if column == "month_name":
            lines += ["\t\tsortByColumn: 'month_number'"]
    lines += ["", f"\tpartition {name} = entity", "\t\tmode: directLake", "\t\tsource",
              f"\t\t\tentityName: {identifier}", "\t\t\tschemaName: dbo",
              "\t\t\texpressionSource: 'OneLake Source'", ""]
    return "\n".join(lines)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, indent=2) + "\n")


def build(output: Path, contracts: Path, fixture_manifest: Path | None = None) -> dict:
    row_counts = {}
    manifest = None
    if fixture_manifest is not None:
        manifest = json.loads(fixture_manifest.read_text(encoding="utf-8"))
        row_counts = {item["id"]: item["rows"] for item in manifest["tables"]}
    showcase = manifest is not None and manifest["profile"] in {"showcase-v2", "service-assurance"}
    contract = catalog(showcase=showcase)
    definitions = {spec["id"]: spec for spec in contract["tables"]}
    if manifest is not None and manifest["profile"] == "showcase-v2" and (output.exists() or contracts.exists()):
        raise FileExistsError("Showcase source models/catalog require new output directories")
    census = {
        "version": "1.0", "status": "UNDEPLOYED_TEMPLATE", "upstream_commit": UPSTREAM_COMMIT,
        "guidance_paths": GUIDANCE,
        "validation": {"sql_reference": "SEE_TARGETED_TESTS", "tmdl_engine_parse": "PENDING_DEMO_TENANT",
                       "live_dax": "PENDING_DEMO_TENANT", "direct_lake": "PENDING_DEMO_TENANT",
                       "source_rls": "PENDING_DEMO_TENANT"},
        "models": [],
    }
    if showcase:
        census.update({
            "upstream_commit": "bc42aaf734771bf752c1d7082dd91e5a6ca72b9f",
            "source_manifest_sha256": hashlib.sha256(fixture_manifest.read_bytes()).hexdigest(),
            "population": manifest["population"],
            "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        })
    for model in contract["models"]:
        model_id = model["id"]
        folder = output / f"{model_id}.SemanticModel"
        definition = folder / "definition"
        model_metrics = [metric for metric in contract["metrics"] if metric["model"] == model_id]
        refs = [f"ref table {quoted(TABLE_BY_ID[t]['displayName'])}" for t in model["tables"]]
        write_text(definition / "database.tmdl",
                   f"database {lineage(model_id + '/database')}\n\tcompatibilityLevel: 1702\n\tcompatibilityMode: powerBI\n")
        write_text(definition / "model.tmdl", "\n".join([
            f"/// {model['name']}; undeployed template requiring approved OneLake binding.",
            "model Model", "\tculture: en-US", "\tsourceQueryCulture: en-US",
            "\tdefaultPowerBIDataSourceVersion: powerBI_V3", "\tdiscourageImplicitMeasures",
            "\tannotation SourceRevision = " + (manifest["version"] if showcase else "1.0"), "",
            *refs, "", "ref role 'East Region'", "",
        ]))
        write_text(definition / "expressions.tmdl", "\n".join([
            "/// Target workspace placeholder. No tenant identifiers or credentials are embedded.",
            'expression WorkspaceId = "<WORKSPACE_ID>" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]', "",
            "/// Target lakehouse placeholder; replace only in an approved deployment artifact.",
            'expression LakehouseId = "<LAKEHOUSE_ID>" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]', "",
            "/// Shared OneLake navigation expression used by all entity partitions.",
            "expression 'OneLake Source' =",
            "\tlet",
            '\t\tSource = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/" & WorkspaceId & "/" & LakehouseId, [HierarchicalNavigation=true])',
            "\tin", "\t\tSource", "",
        ]))
        relationship_lines = []
        for relationship in relationships(model_id):
            source = TABLE_BY_ID[relationship["fromTable"]]["displayName"]
            target = TABLE_BY_ID[relationship["toTable"]]["displayName"]
            relation_id = lineage(model_id + "/relationship/" + json.dumps(relationship, sort_keys=True))
            relationship_lines += [
                f"relationship {relation_id}", "\tisActive: true", "\tfromCardinality: many",
                "\ttoCardinality: one", "\tcrossFilteringBehavior: oneDirection",
                f"\tfromColumn: {quoted(source)}.{quoted(relationship['fromColumn'])}",
                f"\ttoColumn: {quoted(target)}.{quoted(relationship['toColumn'])}", "",
            ]
        write_text(definition / "relationships.tmdl", "\n".join(relationship_lines))
        predicates = []
        for identifier in model["tables"]:
            spec = definitions[identifier]
            if "region" in spec["columns"]:
                predicates.append({"table": identifier, "expression": f'{ref(identifier, "region")} = "East"'})
            elif "market_id" in spec["columns"]:
                predicates.append({"table": identifier, "expression": f'{ref(identifier, "market_id")} = "M01"'})
        write_text(definition / "roles" / "East Region.tmdl", "\n".join([
            "/// Synthetic East-region sample role; membership absent and configured per demo tenant.",
            "role 'East Region'", "\tmodelPermission: read", "",
            *[f'\ttablePermission {quoted(TABLE_BY_ID[p["table"]]["displayName"])} = {p["expression"]}' for p in predicates], "",
        ]))
        for identifier in model["tables"]:
            write_text(definition / "tables" / f"{identifier}.tmdl",
                       table_tmdl(model_id, definitions[identifier],
                                  [metric for metric in model_metrics if metric["table"] == identifier]))
        write_json(folder / "definition.pbism", {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
            "version": "4.2", "settings": {"qnaEnabled": True},
        })
        smoke = ["// Model-linked query artifact; execute only after target binding.",
                 f"// model: {model_id}.SemanticModel", "EVALUATE",
                 "SUMMARIZECOLUMNS(", "\t'Calendar'[date],", "\t'Markets'[market_id],"]
        smoke += [f'\t"{metric["id"]}", [{metric["name"]}]' + ("," if index < len(model_metrics) - 1 else "")
                  for index, metric in enumerate(model_metrics)]
        smoke += [")", "ORDER BY 'Calendar'[date], 'Markets'[market_id]", ""]
        write_text(folder / "smoke.dax", "\n".join(smoke))
        write_json(folder / "smoke.link.json", {
            "model": model_id, "definition": "definition.pbism", "query": "smoke.dax",
            "status": "TEMPLATE", "metricIds": [metric["id"] for metric in model_metrics],
        })
        census["models"].append({
            "id": model_id, "source_lineage": lineage(model_id + "/database"), "storage_mode": "directLake",
            "tables": [{"id": t, "rows": row_counts.get(t), "columns": len(definitions[t]["columns"])}
                       for t in model["tables"]],
            "relationships": relationships(model_id),
            "measures": [{"id": metric["id"], "name": metric["name"], "home_table": metric["table"],
                          "lineage": lineage(metric["id"])} for metric in model_metrics],
            "roles": [{"name": "East Region", "predicates": predicates, "membership": "NOT_MANAGED"}],
        })
        if showcase:
            census["models"][-1]["definition_parts"] = [{
                "path": path.relative_to(folder).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            } for path in [folder / "definition.pbism", *sorted(definition.rglob("*"))] if path.is_file()]
    if showcase:
        census["catalog_sha256"] = hashlib.sha256((json.dumps(contract, indent=2) + "\n").encode()).hexdigest()
    write_json(contracts / "catalog.json", contract)
    write_json(output / "census.json", census)
    return census


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "fabric" / "semantic-models")
    parser.add_argument("--contracts", type=Path, default=ROOT / "data" / "contracts")
    parser.add_argument("--fixture-manifest", type=Path)
    args = parser.parse_args()
    census = build(args.output, args.contracts, args.fixture_manifest)
    print(json.dumps({"models": len(census["models"]),
                      "measures": sum(len(model["measures"]) for model in census["models"]),
                      "status": census["status"]}))


if __name__ == "__main__":
    main()
