"""Bounded, offline synthetic sources. No evaluator answers or cloud dependencies."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
SEED = 20260929
UTC = timezone.utc
MONTHS = tuple(date(2026, month, 1) for month in range(3, 9))
SA_MONTHS = (
    *(date(2025, month, 1) for month in range(9, 13)),
    *(date(2026, month, 1) for month in range(1, 9)),
)
START = datetime(2026, 3, 1, tzinfo=UTC)
SA_START = datetime(2025, 9, 1, tzinfo=UTC)
STOP = datetime(2026, 9, 1, tzinfo=UTC)
AS_OF = datetime(2026, 9, 29, tzinfo=UTC)
PROFILES = {
    "miniature": {"accounts": 10, "subscriptions": 20, "sites": 4, "cases": 8},
    "smoke": {"accounts": 1000, "subscriptions": 2400, "sites": 20, "cases": 800},
    "showcase-v2": {"accounts": 24, "subscriptions": 72, "sites": 8, "cases": 24},
    "service-assurance": {"accounts": 240, "subscriptions": 480, "sites": 40, "cases": 1500},
}
SHOWCASE_REVISION = "showcase-v2.3"
SERVICE_ASSURANCE_REVISION = "service-assurance-v1.0"
VERSIONED_REVISIONS = {
    "showcase-v2": SHOWCASE_REVISION,
    "service-assurance": SERVICE_ASSURANCE_REVISION,
}
MODEL_NAMES = {
    "subscriptions": "Customer and Subscriptions",
    "finance": "Billing and Finance",
    "digital": "Digital Experience",
    "care": "Customer Care",
    "network": "Network Operations",
    "commerce": "Commerce and Devices",
    "growth": "Offers and Retention",
    "enterprise": "Enterprise Services",
}
ARROW_TYPES = {
    "int64": pa.int64(), "string": pa.string(), "bool": pa.bool_(),
    # Fixed UTC offset round-trips on Windows without an optional IANA tzdata package.
    "date": pa.date32(), "timestamp": pa.timestamp("us", tz="+00:00"),
}


def table(
    identifier: str, model: str, display: str, grain: str, columns: str, description: str
) -> dict:
    return {
        "id": identifier, "model": model, "displayName": display,
        "grain": grain.split(), "primaryKey": grain.split(),
        "columns": dict(field.split(":") for field in columns.split()),
        "description": description,
    }


TABLES = [
    table("account", "core", "Accounts", "account_key",
          "account_key:int64 account_id:string market_id:string region:string consent:bool enterprise:bool display_name:string",
          "Registered accounts, not active accounts; names are deliberately nonunique."),
    table("market", "core", "Markets", "market_id",
          "market_id:string market_name:string region:string",
          "Four synthetic home markets; home market is not network exposure."),
    table("calendar_date", "core", "Calendar", "date",
          "date:date year:int64 quarter:int64 month_number:int64 month_name:string day:int64",
          "Contiguous daily calendar covering service and late posting dates."),
    table("site", "core", "Sites", "site_key",
          "site_key:int64 site_id:string market_id:string region:string",
          "Physical network sites, distinct from service and account identities."),
    table("device", "core", "Devices", "device_key",
          "device_key:int64 device_name:string operating_system:string",
          "Device cohorts for checkout and finite inventory analysis."),
    table("store", "core", "Stores", "store_key",
          "store_key:int64 store_name:string market_id:string",
          "Synthetic fulfilment locations."),
    table("campaign", "core", "Campaigns", "campaign_key",
          "campaign_key:int64 campaign_name:string assignment_method:string",
          "Randomized experiment and observational operational campaign are separate."),
    table("service", "core", "Services", "service_key",
          "service_key:int64 service_id:string account_key:int64",
          "Enterprise service registry; serving sites are resolved with time-valid mappings."),
    table("subscription", "subscriptions", "Subscriptions", "subscription_key",
          "subscription_key:int64 account_key:int64 market_id:string subscription_id:string plan_name:string",
          "Registered subscription identities; lifecycle state lives in monthly facts."),
    table("subscription_month", "subscriptions", "Subscription Month", "subscription_key month",
          "subscription_key:int64 account_key:int64 market_id:string month:date opening_active:int64 active_eom:int64 gross_add:int64 disconnect:int64",
          "One registered line per service month, including inactive states; disconnects require opening activity."),
    table("lifecycle_event", "subscriptions", "Lifecycle Events", "event_key",
          "event_key:int64 subscription_key:int64 account_key:int64 market_id:string month:date event_at:timestamp event_type:string",
          "Observed monthly activation or disconnection transitions."),
    table("account_month", "finance", "Account Month", "account_key month",
          "account_key:int64 billing_key:int64 market_id:string month:date revenue_cents:int64 credit_cents:int64 service_cost_cents:int64 active_lines_eom:int64",
          "Account service-month control totals; credits recognized in service month, not posting month."),
    table("billing_account_map", "finance", "Billing Account Map", "billing_key valid_from",
          "billing_key:int64 account_key:int64 valid_from:timestamp valid_to:timestamp",
          "Half-open UTC identity intervals. Billing key 2 is reused; numeric key equality is invalid."),
    table("charge_line", "finance", "Charge Lines", "charge_key",
          "charge_key:int64 account_key:int64 billing_key:int64 market_id:string month:date service_at:timestamp posted_at:timestamp amount_cents:int64 charge_type:string",
          "Signed service charges and reversals; month is service month, posting time is separate."),
    table("credit", "finance", "Credits", "credit_key",
          "credit_key:int64 account_key:int64 billing_key:int64 market_id:string month:date service_month:date posted_at:timestamp amount_cents:int64",
          "Credits by posting month; late credits retain their original service month."),
    table("session", "digital", "Sessions", "session_key",
          "session_key:int64 account_key:int64 market_id:string month:date started_at:timestamp anonymous_id:string authenticated:bool eligible_checkout:int64 successful_checkout:int64 failed_checkout:int64 duration_seconds:int64 device_key:int64 release_version:string",
          "Session grain; anonymous identity stays null. Eligible checkout is the conversion denominator."),
    table("interaction_event", "digital", "Interaction Events", "event_key",
          "event_key:int64 session_key:int64 account_key:int64 market_id:string month:date event_at:timestamp event_type:string",
          "Two observed interactions per session; not another session or order count."),
    table("identity_map", "digital", "Digital Identity Map", "anonymous_id valid_from",
          "anonymous_id:string account_key:int64 valid_from:timestamp valid_to:timestamp",
          "Time-valid synthetic digital identities, including an explicitly unresolved identity."),
    table("case", "care", "Cases", "case_key",
          "case_key:int64 account_key:int64 market_id:string month:date opened_at:timestamp resolved_at:timestamp resolved:int64 repeat_contact:int64 escalated:int64 first_contact_resolved:int64 resolution_minutes:int64 reason:string",
          "One care case; repeat contact means same account and reason within seven days."),
    table("case_activity", "care", "Case Activities", "activity_key",
          "activity_key:int64 case_key:int64 account_key:int64 market_id:string month:date activity_at:timestamp activity_type:string",
          "Case interactions; must not fan out case or financial counts."),
    table("case_daily_snapshot", "care", "Case Daily Snapshot", "case_key snapshot_date",
          "case_key:int64 account_key:int64 market_id:string month:date snapshot_date:date open_eod:int64",
          "End-of-day case state; additive across cases but never across dates."),
    table("incident", "network", "Incidents", "incident_key",
          "incident_key:int64 site_key:int64 start:timestamp end:timestamp market_id:string month:date severity:string",
          "Raw outage intervals may overlap; union intervals before measuring unavailable minutes."),
    table("site_hour", "network", "Site Hour", "site_key hour_start",
          "site_key:int64 market_id:string month:date hour_start:timestamp eligible_minutes:int64 unavailable_minutes:int64 failed_sessions:int64 total_sessions:int64",
          "Hourly monitoring for a bounded five-day window per month; overlapping outages are normalized."),
    table("site_day", "network", "Site Day", "site_key snapshot_date",
          "site_key:int64 market_id:string month:date snapshot_date:date eligible_minutes:int64 unavailable_minutes:int64",
          "Daily rollup of the same bounded hourly monitoring window, not full-month telemetry."),
    table("subscription_site_exposure", "network", "Subscription Site Exposure", "subscription_key month",
          "subscription_key:int64 account_key:int64 site_key:int64 market_id:string month:date valid_from:timestamp valid_to:timestamp exposure_minutes:int64",
          "Actual serving-site exposure window; home and serving markets can differ."),
    table("order", "commerce", "Orders", "order_key",
          "order_key:int64 account_key:int64 market_id:string month:date ordered_at:timestamp fulfilled_at:timestamp returned_at:timestamp store_key:int64 device_key:int64 session_key:int64 cancelled:int64",
          "Placed orders, not checkout attempts; order, fulfilment and return clocks differ."),
    table("order_line", "commerce", "Order Lines", "order_key line_number",
          "order_key:int64 line_number:int64 account_key:int64 market_id:string month:date store_key:int64 device_key:int64 units:int64 booked_revenue_cents:int64 fulfilled_units:int64 returned_units:int64",
          "Booked merchandise revenue is distinct from recognized service revenue."),
    table("store_device_day", "commerce", "Store Device Day", "store_key device_key snapshot_date",
          "store_key:int64 device_key:int64 market_id:string month:date snapshot_date:date inventory_eod:int64 stockout:int64",
          "Finite end-of-day inventory; repeated daily balances are not additive over time."),
    table("offer_assignment", "growth", "Offer Assignments", "assignment_key",
          "assignment_key:int64 account_key:int64 market_id:string month:date campaign_key:int64 eligible:int64 assigned:int64 treatment:int64 randomized:bool accepted:int64 offer_cost_cents:int64 retained:int64",
          "Assignment-unit outcomes; randomized eligible treatment/control separate from observational targeting."),
    table("offer_outcome", "growth", "Offer Outcomes", "assignment_key",
          "assignment_key:int64 account_key:int64 market_id:string month:date observed_at:timestamp retained:int64",
          "Observed 28-day retention outcomes, not latent counterfactual labels."),
    table("campaign_touch", "growth", "Campaign Touches", "touch_key",
          "touch_key:int64 assignment_key:int64 account_key:int64 market_id:string month:date campaign_key:int64 touched_at:timestamp channel:string",
          "Observed campaign contacts; touch counts are not assignment counts."),
    table("contract", "enterprise", "Contracts", "contract_key",
          "contract_key:int64 account_key:int64 market_id:string credit_cap_cents:int64 credit_per_minute_cents:int64 threshold_minutes:int64 valid_from:timestamp valid_to:timestamp month:date",
          "Time-valid SLA terms; threshold per service-month, then one monetary cap per contract-month."),
    table("service_site_map", "enterprise", "Service Site Map", "service_key valid_from",
          "service_key:int64 site_key:int64 contract_key:int64 valid_from:timestamp valid_to:timestamp market_id:string month:date",
          "Half-open serving-site history; service 1 moves mid-incident without overlapping mappings."),
    table("service_month", "enterprise", "Service Month", "service_key month",
          "service_key:int64 contract_key:int64 account_key:int64 market_id:string month:date contracted:int64 monitored_minutes:int64 breached:int64 recurring_revenue_cents:int64",
          "Service-month operations with validity-clipped monitored minutes."),
    table("sla_evaluation", "enterprise", "SLA Evaluation", "contract_key month",
          "contract_key:int64 account_key:int64 market_id:string month:date outage_minutes:int64 eligible_credit_cents:int64 applied_credit_cents:int64",
          "Operational contract-month SLA settlement; raw intervals remain available for independent reconciliation."),
    table("applied_credit", "enterprise", "Applied Credits", "contract_key month",
          "contract_key:int64 amount_cents:int64 month:date market_id:string account_key:int64 posted_at:timestamp",
          "Actually applied SLA credits, which can be less than eligible credit."),
    table("quarantine", "core", "Quarantine", "record_key",
          "record_key:int64 source_table:string reason_code:string raw_record:string quarantined_at:timestamp",
          "Intentional corrupt inputs, isolated from all analytical facts and semantic models."),
]
TABLE_BY_ID = {spec["id"]: spec for spec in TABLES}


def source_tables(profile: str) -> list[dict]:
    if profile not in VERSIONED_REVISIONS:
        return TABLES
    return [
        spec | {
            "columns": spec["columns"] | (
                {"expected_minutes": "int64", "coverage_complete": "bool"}
                if spec["id"] == "service_month" else {"coverage_complete": "bool"}
            ),
            "coverage": {"column": "coverage_complete", "kind": "effective_mapping"},
        } if spec["id"] in {"service_month", "sla_evaluation"} else spec
        for spec in TABLES
    ]


def stamp(day: date, hour: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=iso)


def iso(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def union_minutes(intervals: list[tuple[datetime, datetime]]) -> int:
    """Source-side interval normalization, never imported by the independent oracle."""
    end = None
    seconds = 0.0
    for left, right in sorted(intervals):
        if right <= left:
            continue
        start = max(left, end) if end is not None else left
        seconds += max(0, (right - start).total_seconds())
        end = max(end, right) if end is not None else right
    return int(seconds // 60)


class Sources:
    def __init__(self, profile: str, seed: int):
        if profile not in PROFILES:
            raise ValueError("standard/scale are disabled pending live miniature proof; use miniature or smoke")
        self.profile, self.seed = profile, seed
        self.size = PROFILES[profile]
        self.start = START
        self.months = MONTHS
        self.enterprise_count = max(2, self.size["accounts"] // 20)

    def rng(self, scope: str, key: int) -> random.Random:
        digest = hashlib.sha256(f"{self.seed}:{scope}:{key}".encode()).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    @staticmethod
    def market(key: int) -> str:
        return f"M{(key - 1) % 4 + 1:02}"

    def owner(self, subscription: int) -> int:
        return (subscription - 1) % self.size["accounts"] + 1

    def lifecycle(self, key: int) -> Iterator[dict]:
        account = self.owner(key)
        active = int(key % 5 != 0)
        for index, month in enumerate(MONTHS):
            opening = active
            add = int(not active and ((key % 5 == 0 and index == 2) or (key % 7 == 0 and index == 4)))
            disconnect = int(opening and (
                (index == 4 and (account <= 2 or (account % 4 in (1, 2) and key % 3 == 0)))
                or (index == 5 and key % 11 == 0)
            ))
            active += add - disconnect
            yield dict(subscription_key=key, account_key=account, market_id=self.market(account),
                       month=month, opening_active=opening, active_eom=active,
                       gross_add=add, disconnect=disconnect)

    @staticmethod
    def billing_key(account: int, month: date) -> int:
        cutoff = date(2026, 6, 1)
        if (account == 1 and month < cutoff) or (account == 2 and month >= cutoff):
            return 2
        return 1000 + account

    def financial(self, account: int) -> Iterator[dict]:
        lines = [list(self.lifecycle(key)) for key in
                 range(account, self.size["subscriptions"] + 1, self.size["accounts"])]
        price = self.rng("price", account).randint(3800, 7200)
        for index, month in enumerate(self.months):
            active = sum(line[index]["active_eom"] for line in lines)
            revenue = active * price
            credit = 900 if account % 5 == 1 and month == date(2026, 7, 1) else 0
            yield dict(account_key=account, billing_key=self.billing_key(account, month),
                       market_id=self.market(account), month=month, revenue_cents=revenue,
                       credit_cents=credit, service_cost_cents=active * 1800 + 400,
                       active_lines_eom=active)

    def sessions(self) -> Iterator[dict]:
        stride = len(self.months) * 2
        for account in range(1, self.size["accounts"] + 1):
            for index, month in enumerate(self.months):
                for attempt in range(2):
                    key = (account - 1) * stride + index * 2 + attempt + 1
                    eligible = int(attempt == 1 or account % 3 == 0)
                    failed = int(eligible and month == date(2026, 7, 1) and account % 2 == 1)
                    success = int(eligible and not failed and (account + index) % 3 != 0)
                    unknown = key == 1
                    yield dict(session_key=key, account_key=None if unknown else account,
                               market_id=self.market(account), month=month,
                               started_at=stamp(month + timedelta(days=19), 10 + attempt),
                               anonymous_id="anon:unresolved" if unknown else f"anon:{account}",
                               authenticated=not unknown, eligible_checkout=eligible,
                               successful_checkout=success, failed_checkout=failed,
                               duration_seconds=self.rng("session", key).randint(20, 900),
                               device_key=1 if account % 2 else 2,
                               release_version="2.1" if month == date(2026, 7, 1) else "2.0")

    def cases(self) -> Iterator[dict]:
        for key in range(1, self.size["cases"] + 1):
            account = ((key - 1) // 2) % self.size["accounts"] + 1
            opened = datetime(2026, 7 if key <= 4 else 8, 16 + (key - 1) % 2, 9, tzinfo=UTC)
            minutes = 60 * (12 + (key % 24) * 3)
            resolved = key % 4 != 0
            yield dict(case_key=key, account_key=account, market_id=self.market(account),
                       month=opened.date().replace(day=1), opened_at=opened,
                       resolved_at=opened + timedelta(minutes=minutes) if resolved else None,
                       resolved=int(resolved), repeat_contact=int(key % 2 == 0),
                       escalated=int(key % 3 == 0), first_contact_resolved=int(resolved and key % 3 != 0 and key % 2 != 0),
                       resolution_minutes=minutes if resolved else None,
                       reason="network" if account % 4 in (1, 2) else "billing")

    def incidents(self) -> list[dict]:
        values = [
            (1, 1, 15, 0, 18, 0), (2, 1, 16, 0, 17, 0),
            (3, 2, 15, 0, 18, 0), (4, 3, 16, 12, 17, 12),
        ]
        return [dict(incident_key=key, site_key=site,
                     start=datetime(2026, 7, d1, h1, tzinfo=UTC),
                     end=datetime(2026, 7, d2, h2, tzinfo=UTC),
                     market_id=self.market(site), month=date(2026, 7, 1), severity="major")
                for key, site, d1, h1, d2, h2 in values]

    def outage(self, site: int, left: datetime, right: datetime) -> int:
        return union_minutes([(max(left, i["start"]), min(right, i["end"]))
                              for i in self.incidents() if i["site_key"] == site])

    def contracts(self) -> Iterator[dict]:
        for key in range(1, self.enterprise_count + 1):
            yield dict(contract_key=key, account_key=key, market_id=self.market(key),
                       credit_cap_cents=5000 if key == 1 else 20000,
                       credit_per_minute_cents=3, threshold_minutes=30,
                       valid_from=datetime(2026, 7, 15, 6, tzinfo=UTC) if key == 2 else START,
                       valid_to=STOP, month=date(2026, 7, 1) if key == 2 else MONTHS[0])

    def mappings(self) -> Iterator[dict]:
        for key in range(1, self.enterprise_count + 1):
            split = datetime(2026, 7, 16, 12, tzinfo=UTC)
            periods = [(START, split, 1), (split, STOP, 3)] if key == 1 else [
                (START, STOP, (key - 1) % self.size["sites"] + 1)]
            for left, right, site in periods:
                yield dict(service_key=key, site_key=site, contract_key=key,
                           valid_from=left, valid_to=right, market_id=self.market(key),
                           month=left.date().replace(day=1))

    def settlements(self) -> Iterator[dict]:
        mappings = list(self.mappings())
        incidents = self.incidents()
        for contract in self.contracts():
            key = contract["contract_key"]
            for month in self.months:
                left = max(stamp(month), contract["valid_from"])
                right = min(stamp(next_month(month)), contract["valid_to"])
                service_intervals = {}
                for mapping in mappings:
                    if mapping["contract_key"] != key:
                        continue
                    intervals = service_intervals.setdefault(mapping["service_key"], [])
                    intervals.extend(
                        (max(left, mapping["valid_from"], incident["start"]),
                         min(right, mapping["valid_to"], incident["end"]))
                        for incident in incidents if incident["site_key"] == mapping["site_key"]
                    )
                minutes_by_service = [union_minutes(intervals) for intervals in service_intervals.values()]
                minutes = sum(minutes_by_service)
                uncapped = sum(max(0, duration - contract["threshold_minutes"]) *
                               contract["credit_per_minute_cents"] for duration in minutes_by_service)
                eligible = min(contract["credit_cap_cents"], uncapped)
                yield dict(contract_key=key, account_key=contract["account_key"],
                           market_id=contract["market_id"], month=month,
                           outage_minutes=minutes, eligible_credit_cents=eligible,
                           applied_credit_cents=eligible // 2)

    def offers(self) -> Iterator[dict]:
        for account in range(1, self.size["accounts"] + 1):
            for campaign in (1, 2):
                eligible = int(account % 4 != 0)
                treatment = int(self.rng("assignment", account).random() < 0.5) if campaign == 1 else int(account % 4 in (1, 2))
                assigned = eligible * treatment
                accepted = int(assigned and self.rng("acceptance", account + campaign * 10000).random() < 0.6)
                retained = int(self.rng("retention", account + campaign * 10000).random() < (0.65 + 0.18 * accepted))
                yield dict(assignment_key=(account - 1) * 2 + campaign, account_key=account,
                           market_id=self.market(account), month=date(2026, 8, 1),
                           campaign_key=campaign, eligible=eligible, assigned=assigned,
                           treatment=treatment, randomized=campaign == 1, accepted=accepted,
                           offer_cost_cents=accepted * 1200, retained=retained)

    def orders(self) -> Iterator[dict]:
        remaining = {}
        for session in self.sessions():
            # Some successful checkouts do not place orders; failures are not lost-order labels.
            if not session["successful_checkout"] or session["session_key"] % 5 == 0:
                continue
            key = session["session_key"]
            store = (key - 1) % 4 + 1
            stock_key = (store, session["device_key"], session["month"])
            initial = 2 if session["device_key"] == 1 and session["month"].month == 8 else 20
            available = remaining.setdefault(stock_key, initial)
            cancelled = key % 7 == 0 or available == 0
            if not cancelled:
                remaining[stock_key] -= 1
            when = session["started_at"] + timedelta(minutes=5)
            yield dict(order_key=key, account_key=session["account_key"],
                       market_id=session["market_id"], month=session["month"], ordered_at=when,
                       fulfilled_at=None if cancelled else when + timedelta(days=2),
                       returned_at=when + timedelta(days=5) if not cancelled and key % 11 == 0 else None,
                       store_key=store, device_key=session["device_key"],
                       session_key=key, cancelled=int(cancelled))

    def rows(self, identifier: str) -> Iterator[dict]:
        n = self.size["accounts"]
        if identifier == "account":
            for key in range(1, n + 1):
                name = "Northstar Business" if key <= 2 else "Harbor Business" if key <= 4 else f"Account {key:04}"
                yield dict(account_key=key, account_id=f"acct:{key:06}", market_id=self.market(key),
                           region=("East", "West", "North", "South")[(key - 1) % 4],
                           consent=key % 4 != 0, enterprise=key <= self.enterprise_count, display_name=name)
        elif identifier == "market":
            for key, region in enumerate(("East", "West", "North", "South"), 1):
                yield dict(market_id=self.market(key), market_name=f"{region} Market", region=region)
        elif identifier == "calendar_date":
            for offset in range((AS_OF - self.start).days):
                day = (self.start + timedelta(days=offset)).date()
                yield dict(date=day, year=day.year, quarter=(day.month - 1) // 3 + 1,
                           month_number=day.month, month_name=calendar.month_name[day.month], day=day.day)
        elif identifier == "site":
            for key in range(1, self.size["sites"] + 1):
                yield dict(site_key=key, site_id=f"site:{key:04}", market_id=self.market(key),
                           region=("East", "West", "North", "South")[(key - 1) % 4])
        elif identifier == "device":
            for key, operating_system in ((1, "OS A"), (2, "OS B")):
                yield dict(device_key=key, device_name=f"Device {key}", operating_system=operating_system)
        elif identifier == "store":
            for key in range(1, 5):
                yield dict(store_key=key, store_name=f"Store {key}", market_id=self.market(key))
        elif identifier == "campaign":
            for key, method in ((1, "randomized"), (2, "observational")):
                yield dict(campaign_key=key, campaign_name=f"Retention {method}", assignment_method=method)
        elif identifier == "service":
            for key in range(1, self.enterprise_count + 1):
                yield dict(service_key=key, service_id=f"service:{key:05}", account_key=key)
        elif identifier == "subscription":
            for key in range(1, self.size["subscriptions"] + 1):
                account = self.owner(key)
                yield dict(subscription_key=key, account_key=account, market_id=self.market(account),
                           subscription_id=f"line:{key:06}", plan_name="Promotion" if key % 5 == 0 else "Standard")
        elif identifier == "subscription_month":
            for key in range(1, self.size["subscriptions"] + 1):
                yield from self.lifecycle(key)
        elif identifier == "lifecycle_event":
            for row in self.rows("subscription_month"):
                for offset, flag, name in ((0, "gross_add", "activation"), (1, "disconnect", "disconnect")):
                    if row[flag]:
                        yield dict(event_key=row["subscription_key"] * 100 + row["month"].month * 2 + offset,
                                   subscription_key=row["subscription_key"], account_key=row["account_key"],
                                   market_id=row["market_id"], month=row["month"],
                                   event_at=stamp(row["month"] + timedelta(days=21)), event_type=name)
        elif identifier == "account_month":
            for account in range(1, n + 1):
                yield from self.financial(account)
        elif identifier == "billing_account_map":
            split = datetime(2026, 6, 1, tzinfo=UTC)
            for account in range(1, n + 1):
                periods = [(self.start, split), (split, STOP)] if account <= 2 else [(self.start, STOP)]
                for left, right in periods:
                    yield dict(billing_key=self.billing_key(account, left.date()), account_key=account,
                               valid_from=left, valid_to=right)
        elif identifier == "charge_line":
            for row in self.rows("account_month"):
                key = row["account_key"] * 100 + row["month"].month * 2
                common = {field: row[field] for field in ("account_key", "billing_key", "market_id", "month")}
                when = stamp(row["month"] + timedelta(days=14))
                yield dict(common, charge_key=key, service_at=when, posted_at=when + timedelta(days=1),
                           amount_cents=row["revenue_cents"] + row["credit_cents"], charge_type="service")
                if row["credit_cents"]:
                    yield dict(common, charge_key=key + 1, service_at=when, posted_at=when + timedelta(days=2),
                               amount_cents=-row["credit_cents"], charge_type="reversal")
        elif identifier == "credit":
            for row in self.rows("account_month"):
                if row["credit_cents"]:
                    posted = stamp(next_month(row["month"]) + timedelta(days=4))
                    yield dict(credit_key=row["account_key"], account_key=row["account_key"],
                               billing_key=row["billing_key"], market_id=row["market_id"],
                               month=posted.date().replace(day=1), service_month=row["month"],
                               posted_at=posted, amount_cents=row["credit_cents"])
        elif identifier == "session":
            yield from self.sessions()
        elif identifier == "interaction_event":
            for row in self.sessions():
                for offset, kind in enumerate(("view", "checkout" if row["eligible_checkout"] else "browse")):
                    yield dict(event_key=row["session_key"] * 2 + offset, session_key=row["session_key"],
                               account_key=row["account_key"], market_id=row["market_id"], month=row["month"],
                               event_at=row["started_at"] + timedelta(seconds=offset * 10), event_type=kind)
        elif identifier == "identity_map":
            yield dict(
                anonymous_id="anon:unresolved", account_key=None, valid_from=self.start, valid_to=AS_OF,
            )
            for account in range(1, n + 1):
                yield dict(anonymous_id=f"anon:{account}", account_key=account,
                           valid_from=self.start, valid_to=AS_OF)
        elif identifier == "case":
            yield from self.cases()
        elif identifier == "case_activity":
            for row in self.cases():
                for offset, kind in enumerate(("created", "reviewed")):
                    yield dict(activity_key=row["case_key"] * 2 + offset, case_key=row["case_key"],
                               account_key=row["account_key"], market_id=row["market_id"], month=row["month"],
                               activity_at=row["opened_at"] + timedelta(minutes=offset * 20), activity_type=kind)
        elif identifier == "case_daily_snapshot":
            for row in self.cases():
                for offset in range(4):
                    day = row["opened_at"].date() + timedelta(days=offset)
                    cutoff = stamp(day + timedelta(days=1))
                    yield dict(case_key=row["case_key"], account_key=row["account_key"],
                               market_id=row["market_id"], month=day.replace(day=1), snapshot_date=day,
                               open_eod=int(row["resolved_at"] is None or row["resolved_at"] >= cutoff))
        elif identifier == "incident":
            yield from self.incidents()
        elif identifier in ("site_hour", "site_day"):
            for site in range(1, self.size["sites"] + 1):
                for month in self.months:
                    for day in range(14, 19):
                        left = stamp(month.replace(day=day))
                        for hour in range(24) if identifier == "site_hour" else (None,):
                            start = left + timedelta(hours=hour or 0)
                            eligible = 60 if hour is not None else 1440
                            unavailable = self.outage(site, start, start + timedelta(minutes=eligible))
                            row = dict(site_key=site, market_id=self.market(site), month=month,
                                       eligible_minutes=eligible, unavailable_minutes=unavailable)
                            if hour is None:
                                yield dict(row, snapshot_date=left.date())
                            else:
                                yield dict(row, hour_start=start, failed_sessions=unavailable // 2,
                                           total_sessions=100 + site)
        elif identifier == "subscription_site_exposure":
            for row in self.rows("subscription_month"):
                site = (row["subscription_key"] - 1) % self.size["sites"] + 1
                left = stamp(row["month"].replace(day=15))
                right = left + timedelta(days=3)
                yield dict(subscription_key=row["subscription_key"], account_key=row["account_key"],
                           site_key=site, market_id=self.market(site), month=row["month"],
                           valid_from=left, valid_to=right,
                           exposure_minutes=self.outage(site, left, right) * row["opening_active"])
        elif identifier == "order":
            yield from self.orders()
        elif identifier == "order_line":
            for row in self.orders():
                yield dict(order_key=row["order_key"], line_number=1, account_key=row["account_key"],
                           market_id=row["market_id"], month=row["month"], store_key=row["store_key"],
                           device_key=row["device_key"], units=1, booked_revenue_cents=45000,
                           fulfilled_units=1 - row["cancelled"], returned_units=int(row["returned_at"] is not None))
        elif identifier == "store_device_day":
            fulfilled = {}
            for order in self.orders():
                key = (order["store_key"], order["device_key"], order["month"])
                fulfilled[key] = fulfilled.get(key, 0) + (1 - order["cancelled"])
            for store in range(1, 5):
                for device in (1, 2):
                    for month in self.months:
                        initial = 2 if device == 1 and month == date(2026, 8, 1) else 20
                        for day in range(20, 23):
                            inventory = initial - (fulfilled.get((store, device, month), 0) if day == 22 else 0)
                            yield dict(store_key=store, device_key=device, market_id=self.market(store),
                                       month=month, snapshot_date=month.replace(day=day),
                                       inventory_eod=inventory, stockout=int(inventory == 0))
        elif identifier == "offer_assignment":
            yield from self.offers()
        elif identifier == "offer_outcome":
            for row in self.offers():
                yield dict(assignment_key=row["assignment_key"], account_key=row["account_key"],
                           market_id=row["market_id"], month=row["month"],
                           observed_at=datetime(2026, 8, 29, tzinfo=UTC), retained=row["retained"])
        elif identifier == "campaign_touch":
            for row in self.offers():
                if row["assigned"]:
                    yield dict(touch_key=row["assignment_key"], assignment_key=row["assignment_key"],
                               account_key=row["account_key"], market_id=row["market_id"], month=row["month"],
                               campaign_key=row["campaign_key"], touched_at=stamp(row["month"]), channel="email")
        elif identifier == "contract":
            yield from self.contracts()
        elif identifier == "service_site_map":
            yield from self.mappings()
        elif identifier == "sla_evaluation":
            yield from self.settlements()
        elif identifier == "service_month":
            contracts = {row["contract_key"]: row for row in self.contracts()}
            for row in self.settlements():
                contract = contracts[row["contract_key"]]
                left = max(stamp(row["month"]), contract["valid_from"])
                right = min(stamp(next_month(row["month"])), contract["valid_to"])
                monitored = max(0, int((right - left).total_seconds() // 60))
                yield dict(service_key=row["contract_key"], contract_key=row["contract_key"],
                           account_key=row["account_key"], market_id=row["market_id"], month=row["month"],
                           contracted=int(monitored > 0), monitored_minutes=monitored,
                           breached=int(row["outage_minutes"] > contract["threshold_minutes"]),
                           recurring_revenue_cents=150000 if monitored else 0)
        elif identifier == "applied_credit":
            for row in self.settlements():
                if row["applied_credit_cents"]:
                    yield dict(contract_key=row["contract_key"], account_key=row["account_key"],
                               market_id=row["market_id"], month=row["month"],
                               amount_cents=row["applied_credit_cents"],
                               posted_at=stamp(next_month(row["month"]) + timedelta(days=9)))
        elif identifier == "quarantine":
            records = [
                ("subscription", "ORPHAN_ACCOUNT", {"subscription_key": 900001, "account_key": 999999}),
                ("incident", "INVALID_INTERVAL", {"start": "2026-07-18", "end": "2026-07-15"}),
                ("account", "DUPLICATE_NATURAL_ID", {"account_key": 900002, "account_id": "acct:000001"}),
                ("billing_account_map", "OVERLAPPING_IDENTITY_INTERVAL", {"billing_key": 2, "account_key": 3}),
            ]
            for key, (source, reason, raw) in enumerate(records, 1):
                yield dict(record_key=key, source_table=source, reason_code=reason,
                           raw_record=canonical(raw), quarantined_at=AS_OF)
        else:
            raise KeyError(f"No source generator for {identifier}")


class ShowcaseSources(Sources):
    """An opt-in population revision; historical Sources arithmetic/bytes stay intact."""

    ID_PREFIX = "showcase-v2"
    EXPLICIT_ZERO_CONTRACTS = frozenset({2})
    # Actual source registry, not evaluator labels or a scenario-to-answer lookup.
    SERVICE_CONTRACTS = {
        1: 1, 2: 1, 3: 2, 4: 3, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7, 10: 8, 11: 8, 12: 9, 13: 10,
    }
    CONTRACT_ACCOUNTS = {1: 1, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 6, 9: 4, 10: 5}

    def __init__(self, profile: str, seed: int):
        super().__init__(profile, seed)
        self.enterprise_count = 6

    def financial(self, account: int) -> Iterator[dict]:
        july = {
            1: (9000, 400, 10000), 2: (24000, 1000, 23000),
            3: (80000, 2000, 20000), 4: (0, 0, 400),
            5: (16000, 500, 5000), 6: (60000, 600, 16000),
        }
        for row in super().financial(account):
            if row["month"] == date(2026, 7, 1) and account in july:
                row["revenue_cents"], row["credit_cents"], row["service_cost_cents"] = july[account]
            elif row["month"] == date(2026, 6, 1) and account <= 6:
                row["credit_cents"] = account * 100
            yield row

    def incidents(self) -> list[dict]:
        rows = super().incidents()
        for key, site, start, end in (
            (5, 4, datetime(2026, 7, 16, tzinfo=UTC), datetime(2026, 7, 16, 0, 20, tzinfo=UTC)),
            (6, 5, datetime(2026, 6, 30, 23, tzinfo=UTC), datetime(2026, 7, 1, 2, tzinfo=UTC)),
            (7, 6, datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 1, 1, tzinfo=UTC)),
        ):
            rows.append(dict(incident_key=key, site_key=site, start=start, end=end,
                             market_id=self.market(site), month=start.date().replace(day=1),
                             severity="major"))
        return rows

    def contracts(self) -> Iterator[dict]:
        post_incident = datetime(2026, 7, 20, tzinfo=UTC)
        for key, account in self.CONTRACT_ACCOUNTS.items():
            starts = post_incident if key == 10 else self.start
            yield dict(contract_key=key, account_key=account, market_id=self.market(account),
                       credit_cap_cents=5000 if key == 1 else 30000,
                       credit_per_minute_cents=2, threshold_minutes=10000 if key == 5 else 30,
                       valid_from=starts, valid_to=STOP, month=starts.date().replace(day=1))

    def _periods(self, service: int) -> list[tuple[datetime, datetime, int]]:
        split = datetime(2026, 7, 16, 12, tzinfo=UTC)
        ended_early = datetime(2026, 7, 10, tzinfo=UTC)
        sites = {2: 1, 3: 2, 4: 2, 5: 3, 6: 1, 7: 4, 8: 5, 9: 3, 11: 7, 13: 1}
        if service == 1:
            return [(self.start, split, 1), (split, STOP, 3)]
        if service == 10:
            # Known service with a real source-mapping gap; not an unaffected observation.
            return [(self.start, stamp(date(2026, 6, 30)), 6), (stamp(date(2026, 7, 2)), STOP, 6)]
        if service == 12:
            # Assignment to site 2 ends before July 15 incident 3 and moves to unaffected site 7.
            return [(self.start, ended_early, 2), (ended_early, STOP, 7)]
        return [(self.start, STOP, sites[service])]

    def mappings(self) -> Iterator[dict]:
        for service, contract in self.SERVICE_CONTRACTS.items():
            for left, right, site in self._periods(service):
                yield dict(service_key=service, site_key=site, contract_key=contract,
                           valid_from=left, valid_to=right, market_id=self.market(site),
                           month=left.date().replace(day=1))

    def _applied_settlement(self, key: int, month: date, eligible: int) -> int:
        if key in {3, 6}:
            if eligible % 2:
                raise ValueError("Partial settlement requires exact integer cents; no rounding")
            return eligible // 2
        if key in {4, 7}:
            return eligible
        return 0

    def settlements(self) -> Iterator[dict]:
        contracts = {row["contract_key"]: row for row in self.contracts()}
        by_service: dict[int, list[dict]] = {}
        for mapping in self.mappings():
            by_service.setdefault(mapping["service_key"], []).append(mapping)
        services_by_contract: dict[int, list[int]] = {}
        for service, owner in self.SERVICE_CONTRACTS.items():
            services_by_contract.setdefault(owner, []).append(service)
        for row in super().settlements():
            key, eligible = row["contract_key"], row["eligible_credit_cents"]
            contract = contracts[key]
            left, right = max(stamp(row["month"]), contract["valid_from"]), min(
                stamp(next_month(row["month"])), contract["valid_to"])
            expected_minutes = max(0, int((right - left).total_seconds() // 60))
            incomplete = expected_minutes > 0 and any(
                union_minutes([(max(left, mapping["valid_from"]), min(right, mapping["valid_to"]))
                               for mapping in by_service.get(service, ())]) < expected_minutes
                for service in services_by_contract.get(key, ())
            )
            if incomplete:
                yield row | {
                    "outage_minutes": None, "eligible_credit_cents": None,
                    "applied_credit_cents": None, "coverage_complete": False,
                }
                continue
            applied = self._applied_settlement(key, row["month"], eligible)
            yield row | {"applied_credit_cents": applied, "coverage_complete": True}

    def rows(self, identifier: str) -> Iterator[dict]:
        if identifier == "service":
            for service, contract in self.SERVICE_CONTRACTS.items():
                yield dict(service_key=service, service_id=f"{self.ID_PREFIX}:service:{service:05}",
                           account_key=self.CONTRACT_ACCOUNTS[contract])
        elif identifier == "service_month":
            contracts = {row["contract_key"]: row for row in self.contracts()}
            by_service: dict[int, list[dict]] = {}
            for mapping in self.mappings():
                by_service.setdefault(mapping["service_key"], []).append(mapping)
            by_site: dict[int, list[dict]] = {}
            for incident in self.incidents():
                by_site.setdefault(incident["site_key"], []).append(incident)
            for service, key in self.SERVICE_CONTRACTS.items():
                contract = contracts[key]
                spans = by_service.get(service, [])
                for month in self.months:
                    left, right = max(stamp(month), contract["valid_from"]), min(
                        stamp(next_month(month)), contract["valid_to"])
                    monitored = union_minutes([(max(left, row["valid_from"]), min(right, row["valid_to"]))
                                               for row in spans])
                    expected = max(0, int((right - left).total_seconds() // 60))
                    complete = monitored == expected
                    outage = union_minutes([
                        (max(left, row["valid_from"], incident["start"]),
                         min(right, row["valid_to"], incident["end"]))
                        for row in spans for incident in by_site.get(row["site_key"], ())
                    ])
                    yield dict(service_key=service, contract_key=key, account_key=contract["account_key"],
                               market_id=contract["market_id"], month=month, contracted=int(expected > 0),
                               monitored_minutes=monitored, expected_minutes=expected,
                               coverage_complete=complete,
                               breached=int(outage > contract["threshold_minutes"]) if complete else None,
                               recurring_revenue_cents=150000 if monitored else 0)
        elif identifier == "credit":
            for row in super().rows(identifier):
                # One source posting per account/service-month. Account alone is not unique.
                yield row | {"credit_key": (row["service_month"].year * 100 + row["service_month"].month)
                             * 1_000_000 + row["account_key"]}
        elif identifier == "applied_credit":
            for row in self.settlements():
                explicit_zero = (
                    row["contract_key"] in self.EXPLICIT_ZERO_CONTRACTS
                    and row["month"] == date(2026, 7, 1)
                    and row["applied_credit_cents"] == 0
                )
                if row["applied_credit_cents"] or explicit_zero:
                    yield dict(contract_key=row["contract_key"], account_key=row["account_key"],
                               market_id=row["market_id"], month=row["month"],
                               amount_cents=row["applied_credit_cents"],
                               posted_at=stamp(next_month(row["month"]) + timedelta(days=9)))
        elif identifier in {"account", "subscription", "site"}:
            id_fields = {"account": "account_id", "subscription": "subscription_id", "site": "site_id"}
            for row in super().rows(identifier):
                field = id_fields[identifier]
                yield row | {field: f"{self.ID_PREFIX}:" + row[field]}
        else:
            yield from super().rows(identifier)


class ServiceAssuranceSources(ShowcaseSources):
    """Twelve-month, 240-account service-assurance population with fixed hero anchor keys."""

    ID_PREFIX = "service-assurance"
    EXPLICIT_ZERO_CONTRACTS = frozenset({2, 20})
    CONTRACT_ACCOUNTS = (
        ShowcaseSources.CONTRACT_ACCOUNTS
        | {key: key - 4 for key in range(11, 105)}
        | {key: key - 94 for key in range(105, 121)}
    )
    SERVICE_CONTRACTS = (
        ShowcaseSources.SERVICE_CONTRACTS
        | {service: service - 3 for service in range(14, 124)}
        | {service: service - 110 for service in range(124, 181)}
    )

    def __init__(self, profile: str, seed: int):
        super().__init__(profile, seed)
        self.start = SA_START
        self.months = SA_MONTHS
        self.enterprise_count = 100
        self._incidents = self._build_incidents()
        self._incidents_by_site: dict[int, list[dict]] = {}
        for item in self._incidents:
            self._incidents_by_site.setdefault(item["site_key"], []).append(item)

    def _build_incidents(self) -> list[dict]:
        rows = super().incidents()
        july_extras = (
            (8, 9, 14, 6, 15, 18, "major"),
            (9, 10, 15, 8, 16, 20, "major"),
            (10, 11, 15, 12, 16, 6, "major"),
            (11, 12, 16, 0, 17, 6, "major"),
            (12, 13, 16, 4, 16, 16, "major"),
            (13, 14, 17, 0, 18, 0, "major"),
            (14, 15, 17, 6, 17, 18, "major"),
            (15, 16, 14, 12, 15, 0, "major"),
            (16, 17, 15, 10, 15, 14, "minor"),
            (17, 18, 16, 8, 16, 11, "minor"),
            (18, 19, 17, 9, 17, 11, "minor"),
            (19, 20, 18, 2, 18, 4, "minor"),
        )
        for key, site, d1, h1, d2, h2, severity in july_extras:
            rows.append(dict(
                incident_key=key, site_key=site,
                start=datetime(2026, 7, d1, h1, tzinfo=UTC),
                end=datetime(2026, 7, d2, h2, tzinfo=UTC),
                market_id=self.market(site), month=date(2026, 7, 1), severity=severity,
            ))
        for index, month in enumerate(self.months[:10]):
            first_site = 9 + (index * 3) % 18
            second_site = 10 + (index * 5) % 17
            rows.append(dict(
                incident_key=20 + index * 2, site_key=first_site,
                start=datetime(month.year, month.month, 15, 6, tzinfo=UTC),
                end=datetime(month.year, month.month, 16, 0, tzinfo=UTC),
                market_id=self.market(first_site), month=month, severity="major",
            ))
            rows.append(dict(
                incident_key=21 + index * 2, site_key=second_site,
                start=datetime(month.year, month.month, 16, 12, tzinfo=UTC),
                end=datetime(month.year, month.month, 16, 18, tzinfo=UTC),
                market_id=self.market(second_site), month=month,
                severity="minor" if index % 2 else "major",
            ))
        return rows

    def incidents(self) -> list[dict]:
        return list(self._incidents)

    def outage(self, site: int, left: datetime, right: datetime) -> int:
        return union_minutes([
            (max(left, item["start"]), min(right, item["end"]))
            for item in self._incidents_by_site.get(site, ())
        ])

    def lifecycle(self, key: int) -> Iterator[dict]:
        account = self.owner(key)
        active = int(key % 5 != 0)
        for month in self.months:
            opening = active
            add = int(not active and (
                (key % 5 == 0 and month == date(2026, 5, 1))
                or (key % 7 == 0 and month == date(2026, 7, 1))
                or (key % 13 == 0 and month == date(2025, 11, 1))
            ))
            disconnect = int(opening and (
                (month == date(2026, 7, 1) and (account <= 2 or (account % 4 in (1, 2) and key % 3 == 0)))
                or (month == date(2026, 8, 1) and key % 11 == 0)
                or (month == date(2026, 2, 1) and key % 17 == 0 and account > 6)
            ))
            active += add - disconnect
            yield dict(subscription_key=key, account_key=account, market_id=self.market(account),
                       month=month, opening_active=opening, active_eom=active,
                       gross_add=add, disconnect=disconnect)

    def financial(self, account: int) -> Iterator[dict]:
        ent_rev = 14000 + account * 720 + self.rng("ent_rev", account).randint(0, 600)
        ent_cost = 6000 + account * 290 + self.rng("ent_cost", account).randint(0, 500)
        for row in super().financial(account):
            if 6 < account <= self.enterprise_count:
                row["revenue_cents"] += ent_rev
                row["service_cost_cents"] += ent_cost
                if account in {14, 22, 38} and row["month"] == date(2026, 7, 1):
                    row["service_cost_cents"] = row["revenue_cents"] + 2500
                elif row["month"] == date(2026, 3, 1) and account % 6 == 0:
                    row["credit_cents"] = 450
            yield row

    def contracts(self) -> Iterator[dict]:
        base = {key: row for key, row in zip(range(1, 11), super().contracts()) if key <= 10}
        yield from base.values()
        for key, account in self.CONTRACT_ACCOUNTS.items():
            if key <= 10:
                continue
            if key == 12:
                starts = datetime(2026, 7, 22, tzinfo=UTC)
            elif key in {85, 95, 115}:
                starts = datetime(2026, 1, 1, tzinfo=UTC)
            else:
                starts = self.start
            cap = 6000 if key in {14, 18, 22} else (12000, 20000, 30000, 44000)[(key - 11) % 4]
            rate = 2 if key == 12 else (2, 4, 6)[(key - 11) % 3]
            threshold = 10000 if key == 25 else 30 if key == 12 else (30, 45, 60)[(key - 11) % 3]
            yield dict(contract_key=key, account_key=account, market_id=self.market(account),
                       credit_cap_cents=cap, credit_per_minute_cents=rate,
                       threshold_minutes=threshold, valid_from=starts, valid_to=STOP,
                       month=starts.date().replace(day=1))

    def _periods(self, service: int) -> list[tuple[datetime, datetime, int]]:
        if service <= 13:
            return super()._periods(service)
        if service == 14:
            cutoff = datetime(2026, 7, 12, tzinfo=UTC)
            return [(self.start, cutoff, 1), (cutoff, STOP, 8)]
        if service == 15:
            return [(self.start, STOP, 2)]
        if service == 16:
            return [(self.start, stamp(date(2026, 7, 10)), 28), (stamp(date(2026, 7, 12)), STOP, 28)]
        pinned = {17: 9, 18: 10, 21: 11, 22: 14, 108: 13}
        site = pinned.get(service, 9 + ((service - 17) * 5) % 32)
        if service in {30, 45, 60}:
            move = datetime(2026, 2, 1, tzinfo=UTC)
            return [(self.start, move, site), (move, STOP, 9 + (site - 8) % 32)]
        return [(self.start, STOP, site)]

    def _applied_settlement(self, key: int, month: date, eligible: int) -> int:
        if key <= 10:
            return super()._applied_settlement(key, month, eligible)
        if key % 4 == 0:
            return 0
        if key % 4 in (1, 2):
            if eligible % 2:
                raise ValueError("Partial settlement requires exact integer cents; no rounding")
            return eligible // 2
        return eligible

    def cases(self) -> Iterator[dict]:
        july_pool = [acct for acct in range(7, self.size["accounts"] + 1) if acct not in {15, 19, 23}]
        other_pool = [acct for acct in range(1, self.size["accounts"] + 1) if acct != 15]
        other_months = [month for month in self.months if month != date(2026, 7, 1)]
        for key in range(1, self.size["cases"] + 1):
            if key <= 24:
                account = ((key - 1) // 2) % 24 + 1
                opened = datetime(2026, 7 if key <= 4 else 8, 16 + (key - 1) % 2, 9, tzinfo=UTC)
                minutes = 60 * (12 + (key % 24) * 3)
                resolved = key % 4 != 0
                repeat = int(key % 2 == 0)
                escalated = int(key % 3 == 0)
                yield dict(
                    case_key=key, account_key=account, market_id=self.market(account),
                    month=opened.date().replace(day=1), opened_at=opened,
                    resolved_at=opened + timedelta(minutes=minutes) if resolved else None,
                    resolved=int(resolved), repeat_contact=repeat, escalated=escalated,
                    first_contact_resolved=int(resolved and not escalated and not repeat),
                    resolution_minutes=minutes if resolved else None,
                    reason="network" if account % 4 in (1, 2) else "billing",
                )
                continue
            rng = self.rng("sa_case", key)
            month = date(2026, 7, 1) if rng.random() < 0.2 else rng.choice(other_months)
            account = rng.choice(july_pool if month == date(2026, 7, 1) else other_pool)
            day = rng.randint(1, 24)
            hour = rng.randint(0, 23)
            minute = rng.choice((0, 15, 30, 45))
            opened = datetime(month.year, month.month, day, hour, minute, tzinfo=UTC)
            resolved = rng.random() < 0.78
            minutes = 60 * rng.randint(4, 72)
            repeat = int(rng.random() < 0.25)
            esc_rate = 0.35 if month == date(2026, 7, 1) and account <= self.enterprise_count else 0.18
            escalated = int(rng.random() < esc_rate)
            reason = (
                "network"
                if (month == date(2026, 7, 1) and account <= self.enterprise_count) or rng.random() < 0.45
                else "billing"
            )
            yield dict(
                case_key=key, account_key=account, market_id=self.market(account),
                month=month, opened_at=opened,
                resolved_at=opened + timedelta(minutes=minutes) if resolved else None,
                resolved=int(resolved), repeat_contact=repeat, escalated=escalated,
                first_contact_resolved=int(resolved and not escalated and not repeat),
                resolution_minutes=minutes if resolved else None, reason=reason,
            )


def arrow_schema(spec: dict) -> pa.Schema:
    return pa.schema([pa.field(name, ARROW_TYPES[kind], nullable=name not in spec["primaryKey"])
                      for name, kind in spec["columns"].items()])


def write_table(spec: dict, rows: Iterator[dict], output: Path, batch_size: int, *, strict: bool = False) -> dict:
    path = output / f'{spec["id"]}.parquet'
    temporary = path.with_suffix(".parquet.tmp")
    logical = hashlib.sha256(canonical(spec["columns"]).encode())
    schema = arrow_schema(spec)
    count = 0
    batch = []
    keys = set()
    try:
        with pq.ParquetWriter(temporary, schema, compression="zstd") as writer:
            for row in rows:
                if set(row) != set(spec["columns"]):
                    raise ValueError(f'{spec["id"]}: source columns differ from contract')
                if strict:
                    key = tuple(row[column] for column in spec["primaryKey"])
                    if None in key or key in keys:
                        raise ValueError(f'Duplicate/null primary key in {spec["id"]}: {key}')
                    keys.add(key)
                    for column, kind in spec["columns"].items():
                        value = row[column]
                        if kind == "int64" and value is not None and type(value) is not int:
                            raise ValueError(f'{spec["id"]}.{column} requires exact integer values')
                        if (spec["id"] in {"incident", "contract", "service_site_map"} and
                                isinstance(value, datetime) and (value.second or value.microsecond)):
                            raise ValueError("Showcase intervals require whole-minute boundaries; no implicit rounding")
                    if spec["id"] == "applied_credit" and row["amount_cents"] < 0:
                        raise ValueError("Invalid settlement: negative applied posting is not a Finance reversal")
                    if spec["id"] == "sla_evaluation":
                        applied, eligible = row["applied_credit_cents"], row["eligible_credit_cents"]
                        if (applied is None) != (eligible is None) or (
                            applied is not None and not 0 <= applied <= eligible
                        ):
                            raise ValueError("Invalid settlement: signed residual must not conceal invalid source postings")
                logical.update(b"\n" + canonical(row).encode())
                batch.append(row)
                count += 1
                if len(batch) == batch_size:
                    writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                    batch.clear()
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    with path.open("rb") as stream:
        file_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"id": spec["id"], "file": path.name, "rows": count, "bytes": path.stat().st_size,
            "file_sha256": file_hash, "content_sha256": logical.hexdigest(),
            **({"coverage": spec["coverage"]} if "coverage" in spec else {})}


def build(profile: str, seed: int, output: Path, batch_size: int = 1024) -> dict:
    showcase = profile == "showcase-v2"
    versioned = VERSIONED_REVISIONS.get(profile)
    if profile == "service-assurance":
        sources: Sources = ServiceAssuranceSources(profile, seed)
    elif showcase:
        sources = ShowcaseSources(profile, seed)
    else:
        sources = Sources(profile, seed)
    if not 1 <= batch_size <= 65536:
        raise ValueError("batch_size must be between 1 and 65536")
    started = time.perf_counter()
    if showcase and output.exists():
        raise FileExistsError("Versioned showcase output already exists; choose a new revision directory")
    output.mkdir(parents=True, exist_ok=True)
    # An interrupted rebuild must not retain a success-shaped old manifest.
    (output / "manifest.json").unlink(missing_ok=True)
    files = [write_table(spec, sources.rows(spec["id"]), output, batch_size, strict=versioned is not None)
             for spec in source_tables(profile)]
    service_months = [month.isoformat() for month in sources.months]
    watermarks = {model: (AS_OF - timedelta(days=index, hours=2)).isoformat()
                  for index, model in enumerate(MODEL_NAMES)}
    manifest = {
        "version": "1.0", "profile": profile, "seed": seed, "as_of": AS_OF.isoformat(),
        "service_months": service_months,
        "tables": files, "rows": sum(item["rows"] for item in files),
        "generated_bytes": sum(item["bytes"] for item in files),
        "generation_seconds": round(time.perf_counter() - started, 6), "batch_size": batch_size,
        "content_sha256": hashlib.sha256(canonical(
            {item["id"]: item["content_sha256"] for item in files}).encode()).hexdigest(),
        "watermarks": watermarks,
        "clocks": {
            "data": {"as_of": AS_OF.isoformat(), "watermarks": watermarks},
            "source_model": {"revision": "1.0", "status": "UNDEPLOYED_TEMPLATE"},
            "ontology": {"revision": None, "status": "NOT_RUN"},
            "context_cache": {"revision": None, "status": "NOT_RUN"},
        },
        "validation": {"live_dax": "NOT_RUN", "direct_lake": "NOT_RUN", "source_rls": "NOT_RUN"},
    }
    if versioned is not None:
        identities = [spec for spec in TABLES if
                      (spec["model"] == "core" and spec["id"] != "quarantine") or
                      spec["id"].endswith("_map") or spec["id"] in {"subscription", "contract"}]
        table_hashes = {item["id"]: item["content_sha256"] for item in files}
        manifest.update({
            "version": versioned,
            # Wall-clock timing is deliberately outside the deterministic revision manifest.
            "generation_seconds": None,
            "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "population": {
                "profile": profile, "seed": seed, "generatorRevision": versioned,
                "identityRevision": hashlib.sha256(canonical({
                    spec["id"]: {key: spec[key] for key in ("columns", "primaryKey")}
                    for spec in sorted(identities, key=lambda spec: spec["id"])
                }).encode()).hexdigest(),
                "sharedMapHashes": {spec["id"]: table_hashes[spec["id"]]
                                    for spec in sorted(identities, key=lambda spec: spec["id"])},
            },
            "source_policy": {
                "rounding": "EXACT_WHOLE_MINUTES_INTEGER_CENTS_NO_ROUNDING",
                "credit_key": "YYYYMM(service_month) * 1000000 + account_key; one posting per account/service-month",
                "settlement": "Whole contract-service-month eligible minus applied, signed and never clamped",
                "finance": "Account-period distinct; posted Finance credits have no contract attribution",
                "missing_mapping": "Known registry gap is unknown membership, never observed zero impact",
                "growth": "Synthetic seeded randomized eligible arms; observed retention, not causal proof",
            },
            "coverage": {
                "service_months": service_months,
                "network_monitoring": "Only days 14-18 inclusive per service month; incidents have separate exact intervals",
                "case_snapshots": "Only four observed dates per case, not a contiguous full-period census",
                "inventory": "Only days 20-22 per month; finite fixture snapshots, not event-based stock accounting",
                "growth_outcomes": "2026-08-29 observed synthetic outcomes for August assignments only",
                "august_incidents": "No recorded incidents; cohort blocked, not complete zero Finance",
                "enterprise_settlement": "Null outage/eligible/applied facts when any registered service lacks full effective-period mapping coverage. Explicit coverage_complete diagnostic is checked on actual filtered source rows before aggregation; unknown is never complete zero.",
                "enterprise_services": "expected_minutes clips calendar to contract validity; monitored_minutes is mapped coverage. Unknown breached stays null; continuous mapping with no outage is measured zero.",
            },
        })
        manifest["clocks"]["source_model"]["revision"] = versioned
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=(*PROFILES, "standard", "scale"), default="miniature")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "generated" / "miniature")
    parser.add_argument("--batch-size", type=int, default=1024)
    args = parser.parse_args()
    if args.profile not in PROFILES:
        parser.error("standard/scale disabled pending live miniature proof")
    manifest = build(args.profile, args.seed, args.output, args.batch_size)
    print(json.dumps({key: manifest[key] for key in
                      ("profile", "rows", "generated_bytes", "generation_seconds", "content_sha256")}))


if __name__ == "__main__":
    main()
