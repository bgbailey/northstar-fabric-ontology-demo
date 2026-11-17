"""Build review-only AI guidance from source contracts, never deployed Copilot state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

from telco_iq.catalog import Catalog

ROOT = Path(__file__).resolve().parents[1]
HERO_MODELS = ("network", "enterprise", "finance", "care")
DIAGNOSTIC_COLUMNS = ("coverage_complete", "expected_minutes")
FIELD = re.compile(r"'((?:[^']|'')+)'\[([^\[\]]+)\]")
BRACKET = re.compile(r"(?<!')\[([^\[\]]+)\]")
TMDL_TABLE_RE = re.compile(r"^table '((?:[^']|'')+)'$")
TMDL_MEASURE_RE = re.compile(r"^\tmeasure '((?:[^']|'')+)' = ")
TMDL_COLUMN_RE = re.compile(r"^\tcolumn '((?:[^']|'')+)'$")
COMMON = [
    "Use the source-owned measures and their declared home tables, dimensions and units. Do not replace source calculations with new formulas.",
    "Clarify the metric, population and reporting period when unresolved. A requested date is not evidence of source coverage; do not invent a latest complete period.",
    "Use registered keys and reviewed identity mappings, not display names or coincidentally equal integers. Preserve unknown and excluded mappings.",
    "Missing, partial, stale or denied data is not measured zero or a complete monetary total. Preserve non-additive measures and report coverage limitations.",
    "Keep data, source-model, ontology and context-cache clocks separate. New context does not freshen old source observations.",
    "AI instructions and schema selection are guidance, not permissions or hard enforcement. A local object reference does not prove that an AI consumer exposes that object.",
]
PURPOSES = {
    "subscriptions": "Account and line populations, monthly subscription state and lifecycle movements.",
    "finance": "Recognized service revenue, billed and posted amounts, attributed cost and account-period margin.",
    "digital": "Session journeys, eligible checkout conversion and time-valid digital identity.",
    "care": "Case contacts and resolution, source repeat-contact definitions and observed open-case snapshots.",
    "network": "Monitored availability, physical-site incidents and valid serving-site line exposure.",
    "commerce": "Orders, merchandise units and bookings, fulfillment, returns and observed device inventory.",
    "growth": "Eligible offer assignments, observed outcomes and qualified randomized retention comparisons.",
    "enterprise": "Effective service/contract identity, governed monthly SLA settlement and contracted value.",
}
INHERITED_VOCABULARY = {
    "finance": {
        "recognized service revenue": "finance.recognized_service_revenue",
        "account contribution margin": "finance.contribution_margin",
        "posting-period credits": "finance.posted_credits",
    },
    "network": {
        "monitored availability": "network.availability_percent",
        "outage-exposed lines": "network.exposed_lines",
        "line exposure minutes": "network.exposure_minutes",
    },
    "enterprise": {
        "eligible SLA credits": "enterprise.eligible_credits",
        "signed remaining SLA credits": "enterprise.credit_exposure",
        "contracted recurring value": "enterprise.contracted_recurring_revenue",
    },
}
EXTRA = {
    "subscriptions": {
        "instructions": [
            "Use [Active Lines EOM] for closing subscriptions and [Active Accounts EOM] for distinct closing accounts. They are different populations, not interchangeable customer counts.",
            "'Subscription Month'[subscription_key] plus 'Subscription Month'[month] is line-month grain. 'Accounts'[account_key] identifies registered accounts; 'Accounts'[display_name] is not unique.",
            "[Active Lines EOM], [Opening Active Lines] and [Average Active Lines] require one reporting month per result. Do not sum snapshots over time or sum monthly distinct account counts.",
            "Use [Churn Rate] for opening-cohort line churn and preserve its pooled denominator. It is not unique-customer cumulative churn or an average of monthly percentages.",
            "[Gross Adds], [Disconnects] and [Net Adds] describe lifecycle movement. Ask whether quarter-active means active at quarter end, active at any point or a line-month population.",
            "'Lifecycle Events'[event_at] is an event instant; 'Subscription Month'[month] is the source monthly state role. Do not relabel monthly snapshots as event-date flows.",
        ],
        "vocabulary": {
            "closing active lines": "subscriptions.active_lines_eom",
            "active customer accounts": "subscriptions.active_accounts_eom",
            "line churn": "subscriptions.churn_rate",
        },
    },
    "digital": {
        "instructions": [
            "Use [Checkout Conversion] with its eligible checkout-session denominator, not all [Sessions]. [Successful Checkout Sessions] measure completed session checkouts, not downstream order fulfillment.",
            "'Sessions'[session_key] is session grain. 'Sessions'[account_key] can be null; [Digital Active Accounts] counts resolved accounts, not anonymous visitors.",
            "'Digital Identity Map'[anonymous_id] resolves accounts only within 'Digital Identity Map'[valid_from] and 'Digital Identity Map'[valid_to]. Preserve half-open UTC validity and unresolved identities.",
            "Use 'Sessions'[month] for the governed session measures. 'Sessions'[started_at] and 'Interaction Events'[event_at] are separate instants; event rows must not fan out session metrics.",
            "[Median Journey Duration] is seconds in the requested source scope. Do not average medians or replace it with a mean of session durations.",
            "[Failed Checkouts] is observed eligible failure, not an assertion of lost sales or lost revenue. Clarify failure, abandonment and conversion before choosing a measure.",
            "Keep session metrics at 'Sessions'[session_key] grain when inspecting 'Interaction Events'; multiple interaction events per session must not multiply session counts or conversion denominators.",
        ],
        "vocabulary": {
            "checkout conversion": "digital.checkout_conversion",
            "resolved digital accounts": "digital.digital_active_accounts",
            "journey duration median": "digital.median_journey_duration",
        },
    },
    "care": {
        "instructions": [
            "'Cases'[case_key] is case grain; 'Case Activities'[activity_key] is activity grain. Use [New Cases] for case volume, not activity counts.",
            "'Cases'[month] is opening month. [Resolved Cases] counts source-resolved cases attributed to opening month, not a resolution-month flow; clarify a request for resolution-period throughput.",
            "[Repeat Contacts] uses the source seven-day, same-account, same-reason definition. [Contacted Accounts] is distinct accounts, not a sum of period-level distinct counts.",
            "Use [First Contact Resolution] with its source resolved-case denominator. Unresolved cases do not become resolved failures or zero-duration observations.",
            "[Mean Resolution Hours] is hours among resolved cases; 'Cases'[resolved_at] and 'Cases'[opened_at] remain separate date roles and unresolved durations remain missing.",
            "[Open Cases EOD] uses one observed 'Case Daily Snapshot'[snapshot_date]. Do not sum snapshots over dates or infer continuous observation between snapshots.",
            "Keep case counts at 'Cases'[case_key] grain and activity counts at 'Case Activities'[activity_key] grain; multiple activities per case must not multiply case metrics, and contact volume alone does not establish root cause.",
        ],
        "vocabulary": {
            "case volume": "care.new_cases",
            "repeat contact": "care.repeat_contacts",
            "open case snapshot": "care.open_cases_eod",
        },
    },
    "commerce": {
        "instructions": [
            "'Orders'[order_key] is order grain; 'Order Lines'[order_key] plus 'Order Lines'[line_number] is line grain. [Orders], [Booked Units] and [Fulfilled Units] are different measures.",
            "[Booked Revenue] is merchandise booking value in integer cents at order placement, not recurring service billing. Clarify an unqualified request for revenue.",
            "The governed order/line measures use source order month. 'Orders'[ordered_at], 'Orders'[fulfilled_at] and 'Orders'[returned_at] are different event roles; do not relabel an order-month result as a fulfillment- or return-period flow.",
            "[Device Return Rate] uses source returned units and fulfilled units attributed to order month. Do not replace its denominator with booked units or average rates.",
            "[Inventory EOD] requires one observed 'Store Device Day'[snapshot_date]. Do not sum inventory over time or infer unobserved days as zero stock.",
            "[Stockout Store Days] counts store-device-day observations, not distinct store days across devices. Finite observed stock is not a measured lost-sales estimate.",
            "Keep order counts at 'Orders'[order_key] grain and unit/revenue totals at 'Order Lines' grain; joining multi-line orders to 'Store Device Day' snapshots must not multiply order or inventory counts.",
        ],
        "vocabulary": {
            "device booking value": "commerce.booked_revenue",
            "closing inventory": "commerce.inventory_eod",
            "fulfilled device units": "commerce.fulfilled_units",
        },
    },
    "growth": {
        "instructions": [
            "'Offer Assignments'[assignment_key] is an assignment unit. [Eligible Population] is eligible assignment units, not campaign touches or necessarily unique people across campaigns.",
            "Use [Assigned Population], [Accepted Offers] and [Acceptance Rate] with their source eligible/assigned populations. A campaign touch is not an offer assignment or an acceptance.",
            "[Treatment Retained] and [Control Retained] use eligible randomized assignment units. 'Offer Assignments'[randomized] distinguishes randomized and observational populations.",
            "[Retention Lift] is the source randomized intention-to-treat retention-rate difference. Its raw fractional difference is displayed in percentage points, not relative percent uplift; either missing eligible arm makes it undefined.",
            "Keep treatment/control denominators valid within the same campaign and assignment period. Do not sum rates, merge overlapping campaign populations as unique customers or turn missing outcomes into complete retention totals.",
            "'Offer Assignments'[month] is the source assignment period; 'Offer Outcomes'[observed_at] is a separate observation instant. Later outcomes do not redefine assignment month.",
            "Use source eligibility for the historical assignment scope. 'Accounts'[consent] alone does not reconstruct historical eligibility or grant permission to contact anyone. Recommendations never execute outreach or credits.",
            "[Offer Cost] is observed accepted-offer cost in integer cents, not recurring service operating cost or a measured causal return on investment. Observational associations do not justify uplift claims.",
        ],
        "vocabulary": {
            "randomized retention lift": "growth.retention_lift",
            "eligible assignment units": "growth.eligible_population",
            "accepted offer cost": "growth.offer_cost",
        },
    },
}
HERO_COLUMN_SYNONYMS: dict[str, dict[str, list[str]]] = {
    "network": {
        "'Incidents'[severity]": ["outage severity", "incident impact tier"],
        "'Sites'[region]": ["site operating region", "tower region"],
    },
    "enterprise": {
        "'Service Month'[month]": ["evaluated SLA month", "service period"],
        "'SLA Evaluation'[month]": ["settled SLA month", "contract settlement month"],
    },
    "finance": {
        "'Account Month'[month]": ["recognized service month", "billing service period"],
        "'Credits'[service_month]": ["adjusted service month", "original credit period"],
    },
    "care": {
        "'Cases'[reason]": ["contact reason", "intake driver"],
        "'Case Daily Snapshot'[snapshot_date]": ["backlog observation date", "end-of-day snapshot date"],
    },
}
SERVICE_ASSURANCE_VERIFIED_ANSWERS: dict[str, list[dict]] = {
    "network": [
        {
            "id": "VA-NET-L1-001",
            "level": "L1",
            "question": "How many major incidents occurred in July 2026 by region?",
            "triggerPhrases": [
                "major incidents in July 2026 by region",
                "count of major outages by region",
                "how many major incidents per region",
            ],
            "targetMeasure": "'Incidents'[Major Incidents]",
            "groupBy": ["'Sites'[region]", "'Incidents'[month]"],
            "visualType": "clusteredBarChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
        {
            "id": "VA-NET-L1-002",
            "level": "L1",
            "question": "What was site availability percent by market in July 2026?",
            "triggerPhrases": [
                "site availability percent by market",
                "monitored availability by market in July 2026",
                "radio availability rate per market",
            ],
            "targetMeasure": "'Site Hour'[Availability Percent]",
            "groupBy": ["'Markets'[market_name]", "'Site Hour'[month]"],
            "visualType": "clusteredColumnChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
        {
            "id": "VA-NET-L1-003",
            "level": "L1",
            "question": "How many unavailable site minutes were recorded by region and month?",
            "triggerPhrases": [
                "unavailable site minutes by region",
                "total site outage minutes per month",
                "monitored unavailable minutes by region",
            ],
            "targetMeasure": "'Site Hour'[Unavailable Site Minutes]",
            "groupBy": ["'Markets'[region]", "'Site Hour'[month]"],
            "visualType": "lineChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
    ],
    "enterprise": [
        {
            "id": "VA-ENT-L1-001",
            "level": "L1",
            "question": "What is total SLA credit exposure in July 2026 by market?",
            "triggerPhrases": [
                "SLA credit exposure in July 2026 by market",
                "remaining SLA credits by market",
                "unapplied SLA credit exposure per market",
            ],
            "targetMeasure": "'SLA Evaluation'[Credit Exposure]",
            "groupBy": ["'Markets'[market_name]", "'SLA Evaluation'[month]"],
            "visualType": "clusteredBarChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
        {
            "id": "VA-ENT-L1-002",
            "level": "L1",
            "question": "What is SLA compliance by evaluated service month?",
            "triggerPhrases": [
                "SLA compliance rate by month",
                "monthly SLA compliance percentage",
                "service SLA compliance trend",
            ],
            "targetMeasure": "'Service Month'[SLA Compliance]",
            "groupBy": ["'Service Month'[month]"],
            "visualType": "lineChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
        {
            "id": "VA-ENT-L1-003",
            "level": "L1",
            "question": "How many eligible SLA credits were settled in July 2026 by region?",
            "triggerPhrases": [
                "eligible SLA credits in July 2026 by region",
                "settled eligible credits per region",
                "monthly contract eligible credits by region",
            ],
            "targetMeasure": "'SLA Evaluation'[Eligible Credits]",
            "groupBy": ["'Markets'[region]", "'SLA Evaluation'[month]"],
            "visualType": "clusteredColumnChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
    ],
    "finance": [
        {
            "id": "VA-FIN-L1-001",
            "level": "L1",
            "question": "What is recognized service revenue in July 2026 by market?",
            "triggerPhrases": [
                "recognized service revenue in July 2026 by market",
                "monthly service revenue per market",
                "gross recognized service revenue by market",
            ],
            "targetMeasure": "'Account Month'[Recognized Service Revenue]",
            "groupBy": ["'Markets'[market_name]", "'Account Month'[month]"],
            "visualType": "clusteredBarChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
        {
            "id": "VA-FIN-L1-002",
            "level": "L1",
            "question": "What is contribution margin by region in July 2026?",
            "triggerPhrases": [
                "contribution margin by region in July 2026",
                "account contribution margin per region",
                "service contribution margin by operating region",
            ],
            "targetMeasure": "'Account Month'[Contribution Margin]",
            "groupBy": ["'Markets'[region]", "'Account Month'[month]"],
            "visualType": "clusteredColumnChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
        {
            "id": "VA-FIN-L1-003",
            "level": "L1",
            "question": "How much in posted credits was recorded by posting month?",
            "triggerPhrases": [
                "posted credits by posting month",
                "monthly billing credits posted",
                "total posted credit cents by month",
            ],
            "targetMeasure": "'Credits'[Posted Credits]",
            "groupBy": ["'Credits'[month]"],
            "visualType": "lineChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
    ],
    "care": [
        {
            "id": "VA-CARE-L1-001",
            "level": "L1",
            "question": "How many escalated cases opened in July 2026 by contact reason?",
            "triggerPhrases": [
                "escalated cases in July 2026 by reason",
                "count of escalated support cases by reason",
                "support escalations per contact reason",
            ],
            "targetMeasure": "'Cases'[Escalated Cases]",
            "groupBy": ["'Cases'[reason]", "'Cases'[month]"],
            "visualType": "clusteredBarChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
        {
            "id": "VA-CARE-L1-002",
            "level": "L1",
            "question": "How many repeat contacts occurred by market and opening month?",
            "triggerPhrases": [
                "repeat contacts by market and month",
                "seven-day repeat support contacts per market",
                "repeat case contacts by market",
            ],
            "targetMeasure": "'Cases'[Repeat Contacts]",
            "groupBy": ["'Markets'[market_name]", "'Cases'[month]"],
            "visualType": "clusteredColumnChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
        {
            "id": "VA-CARE-L1-003",
            "level": "L1",
            "question": "What was first contact resolution rate by contact reason in July 2026?",
            "triggerPhrases": [
                "first contact resolution rate by reason",
                "FCR percentage per case reason",
                "resolved first-contact rate in July 2026",
            ],
            "targetMeasure": "'Cases'[First Contact Resolution]",
            "groupBy": ["'Cases'[reason]", "'Cases'[month]"],
            "visualType": "clusteredBarChart",
            "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
    ],
}


def metric_reference(catalog: Catalog, metric: dict) -> str:
    table = catalog.tables[metric["table"]]["displayName"].replace("'", "''")
    return f"'{table}'[{metric['name']}]"


def qualify(text: str, catalog: Catalog, metrics: list[dict]) -> str:
    names = {metric["name"]: metric_reference(catalog, metric) for metric in metrics}
    return BRACKET.sub(lambda match: names.get(match[1], match[0]), text)


def validate_references(text: str, catalog: Catalog, model: dict, metrics: list[dict]) -> None:
    tables = {catalog.tables[key]["displayName"]: catalog.tables[key] for key in model["tables"]}
    for match in FIELD.finditer(text):
        table_name, field = match[1].replace("''", "'"), match[2]
        if table_name not in tables or (
            field not in tables[table_name]["columns"]
            and not any(metric["name"] == field and catalog.tables[metric["table"]]["displayName"] == table_name
                        for metric in metrics)
        ):
            raise ValueError(f"AI draft references an unknown/out-of-model object: {model['id']} {match[0]}")
    known = {metric["name"] for metric in metrics} | {
        column for table in tables.values() for column in table["columns"]
    }
    if any(match[1] not in known for match in BRACKET.finditer(text)):
        raise ValueError(f"AI draft contains an unresolved field/measure reference: {model['id']}")


def validate_no_cross_model_mentions(text: str, model_id: str, models: list[dict]) -> None:
    for other in models:
        other_id = other["id"]
        if other_id == model_id:
            continue
        for term in (other_id, other["name"]):
            if re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE):
                raise ValueError(
                    f"AI instruction crosses model subject-area boundary: {model_id} mentions {other_id}"
                )


def parse_table_tmdl(path: Path) -> dict:
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    if len(raw_lines) < 2 or not raw_lines[0].startswith("/// "):
        raise ValueError(f"Missing table description in TMDL: {path}")
    table_desc = raw_lines[0][4:].strip()
    table_match = TMDL_TABLE_RE.match(raw_lines[1])
    if not table_match:
        raise ValueError(f"Invalid table declaration in TMDL: {path}")
    table_name = table_match.group(1).replace("''", "'")
    if not table_desc or len(table_desc) > 200:
        raise ValueError(f"Table description must be 1..200 chars: {path}")

    measures: dict[str, str] = {}
    columns: list[dict] = []
    pending_comment: str | None = None
    current_column: dict | None = None

    for line in raw_lines[2:]:
        if line.startswith("\t/// "):
            pending_comment = line[5:].strip()
            continue
        measure_match = TMDL_MEASURE_RE.match(line)
        if measure_match:
            current_column = None
            m_name = measure_match.group(1).replace("''", "'")
            if not pending_comment or len(pending_comment) > 200:
                raise ValueError(f"Measure description must be 1..200 chars: {path} {m_name}")
            measures[m_name] = pending_comment
            pending_comment = None
            continue
        column_match = TMDL_COLUMN_RE.match(line)
        if column_match:
            c_name = column_match.group(1).replace("''", "'")
            if not pending_comment or len(pending_comment) > 200:
                raise ValueError(f"Column description must be 1..200 chars: {path} {c_name}")
            current_column = {"name": c_name, "description": pending_comment, "hidden": False}
            columns.append(current_column)
            pending_comment = None
            continue
        if line.startswith("\tpartition "):
            current_column = None
            pending_comment = None
            continue
        if current_column is not None and line == "\t\tisHidden":
            current_column["hidden"] = True

    return {
        "displayName": table_name,
        "description": table_desc,
        "measures": measures,
        "columns": columns,
    }


def build_packet(catalog_file: Path, sla_file: Path) -> dict:
    catalog = Catalog(catalog_file)
    sla = json.loads(sla_file.read_text(encoding="utf-8"))
    if sla["status"] != "DRAFT_NOT_APPLIED":
        raise ValueError("Inherited SLA guidance must remain an explicitly unapplied draft")
    inherited = {entry["id"]: entry for entry in sla["models"]}
    if len(inherited) != len(sla["models"]) or set(inherited) != {"network", "enterprise", "finance"}:
        raise ValueError("Inherited SLA draft source coverage is ambiguous or changed")
    drafts = {**deepcopy(inherited), **deepcopy(EXTRA)}
    if set(inherited) & set(EXTRA) or set(drafts) != set(catalog.models) or set(drafts) != set(PURPOSES):
        raise ValueError("AI draft coverage must match the exact source-model catalog")
    for family in ("models", "tables", "metrics"):
        if len({entry["id"] for entry in catalog.raw[family]}) != len(catalog.raw[family]):
            raise ValueError(f"Ambiguous source contract identities: {family}")
    result = []
    for model in catalog.raw["models"]:
        model_id, draft = model["id"], drafts[model["id"]]
        metrics = [metric for metric in catalog.raw["metrics"] if metric["model"] == model_id]
        if not metrics or draft.get("verifiedAnswers", []):
            raise ValueError("AI preparation needs source metrics and must not embed verified answers")
        if len({metric["name"] for metric in metrics}) != len(metrics):
            raise ValueError(f"Ambiguous source measure names: {model_id}")
        instructions = [*COMMON, *draft["instructions"]]
        if not all(isinstance(text, str) and text.strip() for text in instructions):
            raise ValueError(f"AI instructions must be nonempty text: {model_id}")
        instructions = [qualify(text, catalog, metrics) for text in instructions]
        if len("\n".join(instructions)) > 10_000:
            raise ValueError(f"AI instructions exceed the documented 10000-character limit: {model_id}")
        for text in instructions:
            validate_references(text, catalog, model, metrics)
            validate_no_cross_model_mentions(text, model_id, catalog.raw["models"])
        descriptions = deepcopy(draft.get("recommendedDescriptions", []))
        known_targets = {metric_reference(catalog, metric) for metric in metrics}
        for table_id in model["tables"]:
            table = catalog.tables[table_id]
            name = "'" + table["displayName"].replace("'", "''") + "'"
            known_targets.add(name)
            known_targets.update(f"{name}[{column}]" for column in table["columns"])
        for description in descriptions:
            description["object"] = qualify(description["object"], catalog, metrics)
            if description["object"] not in known_targets:
                raise ValueError(f"AI description target is outside the source model: {model_id}")
        bindings = []
        for metric in metrics:
            table = catalog.tables[metric["table"]]
            if metric["table"] not in model["tables"] or metric["dateColumn"] not in table["columns"]:
                raise ValueError(f"AI metric binding differs from source model membership/date role: {metric['id']}")
            bindings.append({
                "metricId": metric["id"], "sourceModel": model_id, "homeTable": table["displayName"],
                "measureName": metric["name"], "object": metric_reference(catalog, metric),
                "sourceGrain": table["grain"], "dateRole": metric["dateColumn"],
                "allowedDimensions": metric["allowedDimensions"], "unit": metric["unit"],
                "aggregation": metric["aggregation"], "description": metric["description"],
            })
        vocabulary = []
        terms = {**INHERITED_VOCABULARY.get(model_id, {}), **draft.get("vocabulary", {})}
        for term, metric_id in terms.items():
            if metric_id not in {metric["id"] for metric in metrics}:
                raise ValueError(f"AI vocabulary crosses source-model scope: {model_id} {metric_id}")
            vocabulary.append({"term": term, "metricId": metric_id,
                               "object": metric_reference(catalog, catalog.metrics[metric_id])})
        result.append({
            "id": model_id, "name": model["name"], "status": "DRAFT_NOT_APPLIED",
            "purpose": PURPOSES[model_id],
            "instructions": instructions, "sourceMetricBindings": bindings,
            "proposedVocabulary": vocabulary, "verifiedAnswers": [],
            "recommendedDescriptions": descriptions,
        })
    common_arm = (
        "Identical reviewed/applied model instructions, sourceMetricBindings and relevant scenarioGuidance. "
        "No draft is runtime authorization; characterize actual consumer propagation before claiming inheritance."
    )
    return {
        "version": "1.0", "status": "DRAFT_NOT_APPLIED",
        "scope": "All eight synthetic Northstar source models; local preparation, not deployed AI state.",
        "sourceCatalogRevision": catalog.revision,
        "sourceSlaGuidanceSha256": hashlib.sha256(sla_file.read_bytes()).hexdigest(),
        "generatorSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "applicationRoute": (
            "Review and apply through supported Prep data for AI tooling under exact authoring approval; "
            "preserve all existing user-authored Copilot parts. No deployed artifacts are written by this generator."
        ),
        "definitionContract": {
            "reference": "https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/semantic-model-definition",
            "assessment": (
                "Public REST documents Copilot packaging paths, not the complete content schemas reviewed here. "
                "No opaque schema/settings/version representation is invented or rewritten."
            ),
        },
        "aiSchemaReview": {
            "automaticVisibilityChanges": False,
            "guidance": (
                "Review actual AI-visible objects and measure dependencies before proposing visibility changes. "
                "Local physical tables are not an exhaustive AI projection; never expose hidden fields or treat "
                "AI schema as authorization by default."
            ),
        },
        "comparisonArms": {"flat": common_arm, "native": common_arm},
        "modelInstructionReference": "https://learn.microsoft.com/power-bi/create-reports/copilot-prepare-data-ai-instructions",
        "models": result,
        "scenarioGuidance": {"sla": deepcopy(sla["sharedCrossDomainFacts"])},
        "evidenceGaps": [
            "These are reviewed-contract-derived drafts, not applied or validated Copilot/model instructions.",
            "No new source model, native Rule, metric linked to a source measure, permission or live query is created.",
            "Actual consumer propagation and per-model instruction readback are not yet run; no automatic inheritance is assumed.",
            "Source metrics retain their formulas; this artifact contains structural bindings and meaning, not DAX/SQL or showcase answers.",
            "Cached Skills for Fabric semantic-model-authoring AI-readiness and modeling references were consulted alongside current Microsoft Learn documentation.",
        ],
    }


def build_service_assurance_prep(
    catalog_file: Path = ROOT / "data" / "contracts" / "catalog.json",
    sla_file: Path = ROOT / "fabric" / "ai-prep" / "sla-model-guidance.json",
    model_root: Path = ROOT / "fabric" / "semantic-models",
) -> dict:
    catalog = Catalog(catalog_file)
    packet = build_packet(catalog_file, sla_file)
    by_id = {entry["id"]: entry for entry in packet["models"]}

    hero_entries = []
    for model_id in HERO_MODELS:
        model_spec = catalog.models[model_id]
        model_packet = by_id[model_id]
        tables_dir = model_root / f"{model_id}.SemanticModel" / "definition" / "tables"

        table_descriptions: dict[str, str] = {}
        column_descriptions: dict[str, str] = {}
        measure_descriptions: dict[str, str] = {}
        included_tables: list[str] = []
        visible_by_table: dict[str, list[str]] = {}
        hidden_by_table: dict[str, list[str]] = {}
        visible_column_refs: set[str] = set()

        for table_id in model_spec["tables"]:
            table_contract = catalog.tables[table_id]
            parsed = parse_table_tmdl(tables_dir / f"{table_id}.tmdl")
            display_name = parsed["displayName"]
            if display_name != table_contract["displayName"]:
                raise ValueError(f"TMDL table displayName mismatch: {model_id}.{table_id}")
            escaped_table = display_name.replace("'", "''")
            included_tables.append(display_name)
            table_descriptions[display_name] = parsed["description"]

            visible_cols: list[str] = []
            hidden_cols: list[str] = []
            for col in parsed["columns"]:
                col_name = col["name"]
                col_kind = table_contract["columns"][col_name]
                col_ref = f"'{escaped_table}'[{col_name}]"
                column_descriptions[col_ref] = col["description"]
                must_hide = (
                    col_name in DIAGNOSTIC_COLUMNS
                    or col_name.endswith(("_key", "_id"))
                    or col_kind == "timestamp"
                    or (col_kind == "int64" and table_id != "calendar_date")
                )
                if col["hidden"] != must_hide:
                    raise ValueError(f"Unexpected column visibility in TMDL: {model_id}.{table_id}.{col_name}")
                if col["hidden"]:
                    hidden_cols.append(col_name)
                else:
                    visible_cols.append(col_name)
                    visible_column_refs.add(col_ref)

            visible_by_table[display_name] = visible_cols
            hidden_by_table[display_name] = hidden_cols

            for m_name, m_desc in parsed["measures"].items():
                measure_descriptions[f"'{escaped_table}'[{m_name}]"] = m_desc

        included_measures = [binding["object"] for binding in model_packet["sourceMetricBindings"]]
        if set(measure_descriptions) != set(included_measures):
            raise ValueError(f"TMDL measures differ from catalog bindings: {model_id}")

        synonyms: dict[str, list[str]] = {}
        for item in model_packet["proposedVocabulary"]:
            synonyms.setdefault(item["object"], []).append(item["term"])
        for obj_ref, phrases in HERO_COLUMN_SYNONYMS.get(model_id, {}).items():
            if obj_ref not in visible_column_refs and obj_ref not in included_measures:
                raise ValueError(f"Synonym target must be a visible column or measure: {model_id} {obj_ref}")
            synonyms.setdefault(obj_ref, []).extend(phrases)

        candidates = deepcopy(SERVICE_ASSURANCE_VERIFIED_ANSWERS[model_id])
        if not candidates:
            raise ValueError(f"Missing L1 verified answer candidates: {model_id}")
        for candidate in candidates:
            if candidate["level"] != "L1" or candidate["editingRoute"] != "USER_CONFIGURED_PREP_DATA_FOR_AI":
                raise ValueError(f"Verified answer candidate must be L1 user-configured: {candidate['id']}")
            if candidate["targetMeasure"] not in included_measures:
                raise ValueError(f"Verified answer targetMeasure outside model: {candidate['id']}")
            if not candidate["groupBy"] or any(col not in visible_column_refs for col in candidate["groupBy"]):
                raise ValueError(f"Verified answer groupBy must use visible model columns: {candidate['id']}")
            validate_no_cross_model_mentions(candidate["question"], model_id, catalog.raw["models"])
            for phrase in candidate["triggerPhrases"]:
                validate_no_cross_model_mentions(phrase, model_id, catalog.raw["models"])

        hero_entries.append({
            "id": model_id,
            "name": model_spec["name"],
            "status": "DRAFT_READY_FOR_PREP_DATA_FOR_AI",
            "purpose": model_packet["purpose"],
            "descriptions": {
                "tables": table_descriptions,
                "columns": column_descriptions,
                "measures": measure_descriptions,
            },
            "aiInstructions": model_packet["instructions"],
            "aiDataSchema": {
                "editingRoute": "USER_CONFIGURED_PREP_DATA_FOR_AI",
                "includedTables": included_tables,
                "visibleColumnsByTable": visible_by_table,
                "hiddenTechnicalColumnsByTable": hidden_by_table,
                "excludedDiagnosticColumns": list(DIAGNOSTIC_COLUMNS),
                "includedMeasures": included_measures,
                "synonyms": synonyms,
            },
            "verifiedAnswerCandidates": candidates,
        })

    arm_note = (
        "Identical TMDL descriptions, single-model AI instructions, AI data schema, and L1 verified answers "
        "applied equally across Network Operations, Enterprise Services, Billing and Finance, and Customer Care."
    )
    return {
        "version": "1.0",
        "stream": "service-assurance",
        "status": "DRAFT_READY_FOR_PREP_DATA_FOR_AI",
        "liveApplyTodo": (
            "TODO(verify-live): Apply AI instructions, AI data schema, and L1 verified answers in Power BI "
            "Prep data for AI UI for all four models equally across evaluation arms"
        ),
        "editingRoutes": {
            "tmdlDescriptions": "AGENT_EDITABLE_TMDL",
            "aiInstructions": "USER_CONFIGURED_PREP_DATA_FOR_AI",
            "aiDataSchema": "USER_CONFIGURED_PREP_DATA_FOR_AI",
            "verifiedAnswers": "USER_CONFIGURED_PREP_DATA_FOR_AI",
        },
        "references": {
            "microsoftLearnPrepDataForAi": "https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-prepare-data-ai",
            "skillsForFabricCommit": "e31422e7d1045ef3a0906df9ff5991f2338af010",
            "skillsForFabricPaths": [
                "skills/semantic-model-authoring/SKILL.md",
                "skills/semantic-model-authoring/references/semantic-model-ai-readiness.md",
                "skills/semantic-model-authoring/references/modeling-guidelines.md",
                "skills/semantic-model-authoring/references/tmdl-guidelines.md",
                "skills/semantic-model-authoring/references/dax-guidelines.md",
            ],
        },
        "subjectAreaBoundary": (
            "Model-level preparation is limited strictly to each semantic model's own subject area; "
            "cross-model relationships, shared identity bridges, and cross-domain rules belong to the ontology."
        ),
        "evaluationArms": {
            "equalPreparation": True,
            "arms": {
                "Arm A": arm_note,
                "Arm A+": arm_note,
                "Arm B": arm_note,
            },
        },
        "sourceCatalogRevision": catalog.revision,
        "sourceSlaGuidanceSha256": hashlib.sha256(sla_file.read_bytes()).hexdigest(),
        "generatorSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "heroModels": list(HERO_MODELS),
        "models": hero_entries,
    }


def _validate_output_path(target: Path, protected: set[Path]) -> Path:
    resolved = target.resolve()
    if resolved in protected:
        raise ValueError("AI draft output must not replace its source catalog or inherited SLA guidance")
    if resolved.suffix.lower() != ".json" or any(
        part.casefold() == "copilot" or part.casefold().endswith(".semanticmodel")
        for part in resolved.parts
    ):
        raise ValueError("Write review JSON outside SemanticModel/Copilot persisted artifacts")
    return resolved


def main() -> None:
    default_output = ROOT / "fabric" / "ai-prep" / "model-guidance.json"
    default_sa_output = ROOT / "fabric" / "ai-prep" / "service-assurance-ai-prep.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "contracts" / "catalog.json")
    parser.add_argument("--sla-guidance", type=Path, default=ROOT / "fabric" / "ai-prep" / "sla-model-guidance.json")
    parser.add_argument("--model-root", type=Path, default=ROOT / "fabric" / "semantic-models")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--service-assurance-output", type=Path, default=None)
    args = parser.parse_args()

    protected = {args.catalog.resolve(), args.sla_guidance.resolve()}
    output = _validate_output_path(args.output or default_output, protected)
    sa_target = (
        args.service_assurance_output
        if args.service_assurance_output is not None
        else (default_sa_output if args.output is None else None)
    )
    sa_output = _validate_output_path(sa_target, protected | {output}) if sa_target is not None else None

    packet = build_packet(args.catalog, args.sla_guidance)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8", newline="\n")
    if sa_output is not None:
        sa_packet = build_service_assurance_prep(args.catalog, args.sla_guidance, args.model_root)
        sa_output.parent.mkdir(parents=True, exist_ok=True)
        sa_output.write_text(json.dumps(sa_packet, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({
        "status": packet["status"], "models": len(packet["models"]),
        "sourceMeasures": sum(len(model["sourceMetricBindings"]) for model in packet["models"]),
        "deployed_or_applied": False,
    }))


if __name__ == "__main__":
    main()
