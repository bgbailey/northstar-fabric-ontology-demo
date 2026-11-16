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
FIELD = re.compile(r"'((?:[^']|'')+)'\[([^\[\]]+)\]")
BRACKET = re.compile(r"(?<!')\[([^\[\]]+)\]")
COMMON = [
    "Use the source-owned measures and their declared home tables, dimensions and units. Do not replace source calculations with new formulas.",
    "Clarify the metric, population and reporting period when unresolved. A requested date is not evidence of source coverage; do not invent a latest complete period.",
    "Use registered keys and reviewed identity mappings, not display names or coincidentally equal integers. Preserve unknown and excluded mappings.",
    "Missing, partial, stale or denied data is not measured zero or a complete financial total. Preserve non-additive measures and report coverage limitations.",
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
            "Use [Checkout Conversion] with its eligible checkout-session denominator, not all [Sessions]. [Successful Checkout Sessions] are not necessarily placed Commerce orders.",
            "'Sessions'[session_key] is session grain. 'Sessions'[account_key] can be null; [Digital Active Accounts] counts resolved accounts, not anonymous visitors.",
            "'Digital Identity Map'[anonymous_id] resolves accounts only within 'Digital Identity Map'[valid_from] and 'Digital Identity Map'[valid_to]. Preserve half-open UTC validity and unresolved identities.",
            "Use 'Sessions'[month] for the governed session measures. 'Sessions'[started_at] and 'Interaction Events'[event_at] are separate instants; event rows must not fan out session metrics.",
            "[Median Journey Duration] is seconds in the requested source scope. Do not average medians or replace it with a mean of session durations.",
            "[Failed Checkouts] is observed eligible failure, not an assertion of lost orders or lost revenue. Clarify failure, abandonment and conversion before choosing a measure.",
            "A session-to-order association needs a reviewed attribution path and period. A shared account or a later order alone does not establish conversion attribution or root cause.",
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
            "Keep one Finance account-period result when relating case cohorts to financial priority. Multiple cases or activities must not duplicate revenue; an association is not proof that care caused churn.",
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
            "[Booked Revenue] is merchandise booking value in integer cents, not Finance recognized service revenue or Enterprise contracted recurring value. Clarify an unqualified request for revenue.",
            "The governed order/line measures use source order month. 'Orders'[ordered_at], 'Orders'[fulfilled_at] and 'Orders'[returned_at] are different event roles; do not relabel an order-month result as a fulfillment- or return-period flow.",
            "[Device Return Rate] uses source returned units and fulfilled units attributed to order month. Do not replace its denominator with booked units or average rates.",
            "[Inventory EOD] requires one observed 'Store Device Day'[snapshot_date]. Do not sum inventory over time or infer unobserved days as zero stock.",
            "[Stockout Store Days] counts store-device-day observations, not distinct store days across devices. Finite observed stock is not a measured lost-sales estimate.",
            "Checkout-session attribution requires the reviewed identity/time path. Orders and line rows must not fan out session conversion, and a stockout association does not prove the cause of a failed checkout.",
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
            "[Offer Cost] is observed accepted-offer cost in cents, not Finance service cost or a measured causal return on investment. Observational associations do not justify uplift claims.",
        ],
        "vocabulary": {
            "randomized retention lift": "growth.retention_lift",
            "eligible assignment units": "growth.eligible_population",
            "accepted offer cost": "growth.offer_cost",
        },
    },
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
    common_arm = "Identical reviewed/applied model instructions, sourceMetricBindings and relevant scenarioGuidance. No draft is runtime authorization; characterize actual consumer propagation before claiming inheritance."
    return {
        "version": "1.0", "status": "DRAFT_NOT_APPLIED",
        "scope": "All eight synthetic Northstar source models; local preparation, not deployed AI state.",
        "sourceCatalogRevision": catalog.revision,
        "sourceSlaGuidanceSha256": hashlib.sha256(sla_file.read_bytes()).hexdigest(),
        "generatorSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "applicationRoute": "Review and apply through supported Prep data for AI tooling under exact authoring approval; preserve all existing user-authored Copilot parts. No deployed artifacts are written by this generator.",
        "definitionContract": {
            "reference": "https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/semantic-model-definition",
            "assessment": "Public REST documents Copilot packaging paths, not the complete content schemas reviewed here. No opaque schema/settings/version representation is invented or rewritten.",
        },
        "aiSchemaReview": {
            "automaticVisibilityChanges": False,
            "guidance": "Review actual AI-visible objects and measure dependencies before proposing visibility changes. Local physical tables are not an exhaustive AI projection; never expose hidden fields or treat AI schema as authorization by default.",
        },
        "comparisonArms": {"flat": common_arm, "native": common_arm},
        "modelInstructionReference": "https://learn.microsoft.com/power-bi/create-reports/copilot-prepare-data-ai-instructions",
        "models": result,
        "scenarioGuidance": {"sla": deepcopy(sla["sharedCrossDomainFacts"])},
        "evidenceGaps": [
            "These are reviewed-contract-derived drafts, not applied or validated Copilot/model instructions.",
            "No new source model, native Rule, metric proxy, permission or live query is created.",
            "Actual consumer propagation and per-model instruction readback remain NOT_RUN; no native automatic inheritance is assumed.",
            "Source metrics retain their formulas; this artifact contains structural bindings and meaning, not DAX/SQL or showcase answers.",
            "The prescribed reference refresh is blocked by unrelated local checkout changes; cached Fabric commit bc42aaf734771bf752c1d7082dd91e5a6ca72b9f and its semantic-model-authoring AI-readiness/modeling references were consulted, not claimed fresh.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "contracts" / "catalog.json")
    parser.add_argument("--sla-guidance", type=Path, default=ROOT / "fabric" / "ai-prep" / "sla-model-guidance.json")
    parser.add_argument("--output", type=Path, default=ROOT / "fabric" / "ai-prep" / "model-guidance.json")
    args = parser.parse_args()
    output = args.output.resolve()
    if output in {args.catalog.resolve(), args.sla_guidance.resolve()}:
        raise ValueError("AI draft output must not replace its source catalog or inherited SLA guidance")
    if output.suffix.lower() != ".json" or any(
        part.casefold() == "copilot" or part.casefold().endswith(".semanticmodel")
        for part in output.parts
    ):
        raise ValueError("Write review JSON outside SemanticModel/Copilot persisted artifacts")
    packet = build_packet(args.catalog, args.sla_guidance)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({
        "status": packet["status"], "models": len(packet["models"]),
        "sourceMeasures": sum(len(model["sourceMetricBindings"]) for model in packet["models"]),
        "deployed_or_applied": False,
    }))


if __name__ == "__main__":
    main()
