"""Render the deterministic authoring prompt pack for the service assurance ontology."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "fabric" / "ontology" / "service-assurance.json"
DEFAULT_OUTPUT = ROOT / "fabric" / "ontology" / "service-assurance.prompts.md"

MODEL_LABELS: dict[str, str] = {
    "network": "Network",
    "enterprise": "Enterprise services",
    "finance": "Finance",
    "care": "Care",
    "subscriptions": "Subscriptions",
    "digital": "Digital experience",
    "commerce": "Commerce",
    "growth": "Growth",
}

LEARN_GENERATE = "https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-generate-from-semantic-models"
LEARN_AGENT = "https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-agent"
LEARN_METRICS = "https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics"
LEARN_RULES = "https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-rules"
LEARN_DEFINITION = (
    "https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/"
    "ontology-definition"
)


def _model_label(model_id: str) -> str:
    return MODEL_LABELS.get(model_id, model_id.replace("_", " ").capitalize())


def _format_entity_line(entity: dict[str, Any]) -> str:
    keys = ", ".join(f"`{col}`" for col in entity.get("key", []))
    model_table = entity.get("modelTable", entity["table"])
    details = [
        f"logical type **{entity.get('logical', entity['name'])}**",
        f"semantic model table `{model_table}` (Delta table `{entity['table']}`)",
        f"key {keys}",
    ]
    if "interval" in entity:
        span = entity["interval"]
        details.append(f"UTC interval `{span['from']}` to `{span['to']}`")
    if "validity" in entity:
        span = entity["validity"]
        details.append(f"half-open UTC validity `{span['from']}` to `{span['to']}`")
    return f"- `{entity['name']}`: " + "; ".join(details) + "."


def _format_relationship_line(rel: dict[str, Any]) -> str:
    join_cols = ", ".join(f"`{col}`" for col in rel.get("on", []))
    parts: list[str] = []
    if "hop" in rel:
        parts.append(f"hop {rel['hop']}")
    if rel.get("crossesModels"):
        parts.append("cross-model bridge")
    else:
        parts.append("within-model link")
    if rel.get("identity"):
        parts.append("shared identity")
    if "validity" in rel:
        parts.append(f"validity: {rel['validity']}")
    if "period" in rel:
        parts.append(f"period: {rel['period']}")
    qualifier = f" ({'; '.join(parts)})" if parts else ""
    return f"- `{rel['from']}` to `{rel['to']}` on {join_cols}{qualifier}: {rel['meaning']}"


def _format_metric_line(metric: dict[str, Any]) -> str:
    grain = ", ".join(f"`{col}`" for col in metric["grain"])
    additivity = "additive" if metric["additive"] else "non-additive"
    return (
        f"- `{metric['name']}` on `{metric['entity']}` "
        f"(home table `{metric['modelTable']}`, grain {grain}, {additivity}, unit `{metric['unit']}`)"
    )


def _format_rule_line(rule: dict[str, Any]) -> str:
    linked = ", ".join(f"`{name}`" for name in rule.get("linkedTo", []))
    prefix = f"{rule['id']} " if "id" in rule else ""
    return f"- **{prefix}`{rule['name']}`** (linked to {linked}): {rule['statement']}"


def _relationships_for_step(
    relationships: list[dict[str, Any]],
    entity_models: dict[str, str],
    current_model: str,
    seen_models: set[str],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for rel in relationships:
        left_model = entity_models[rel["from"]]
        right_model = entity_models[rel["to"]]
        if {left_model, right_model} <= seen_models and current_model in {left_model, right_model}:
            selected.append(rel)
    return selected


def _rules_for_step(
    rules: list[dict[str, Any]],
    entity_models: dict[str, str],
    seen_models: set[str],
    emitted_rules: set[str],
) -> list[dict[str, Any]]:
    ready: list[dict[str, Any]] = []
    for rule in rules:
        rule_key = rule.get("id", rule["name"])
        if rule_key in emitted_rules:
            continue
        referenced_models = {entity_models[target] for target in rule.get("linkedTo", [])}
        if referenced_models and referenced_models <= seen_models:
            ready.append(rule)
            emitted_rules.add(rule_key)
    return ready


def render_prompts(contract: dict[str, Any]) -> str:
    source_models: list[str] = list(contract["sourceModels"])
    entities: list[dict[str, Any]] = list(contract["entities"])
    relationships: list[dict[str, Any]] = list(contract["relationships"])
    metrics: list[dict[str, Any]] = list(contract["metrics"])
    rules: list[dict[str, Any]] = list(contract["rules"])
    checks = ", ".join(f"`{check}`" for check in contract.get("checks", []))

    entity_models = {entity["name"]: entity["model"] for entity in entities}
    ordered_labels = ", ".join(f"**{_model_label(m)}**" for m in source_models)

    lines: list[str] = [
        "# Service assurance ontology authoring prompts",
        "",
        "This generated file provides the step-by-step authoring prompts, review checklist, and test "
        f"questions for `{contract['displayName']}` (contract stream `{contract['stream']}`, schema "
        f"`{contract['schemaVersion']}`). Regenerate it with `python tools/build_ontology_prompts.py` "
        "whenever `fabric/ontology/service-assurance.json` changes.",
        "",
        "Fabric IQ ontology and the ontology agent in Fabric are preview features (checked 16 November "
        "2026). Run these steps in a demo tenant on a workspace with Direct Lake semantic models and "
        "both Read and Build permissions on each model. Follow Microsoft Learn guidance to "
        f"[generate an ontology from semantic models]({LEARN_GENERATE}), "
        f"[use the ontology agent]({LEARN_AGENT}), "
        f"[keep metrics linked to source measures]({LEARN_METRICS}), and "
        f"[define plain-language business rules]({LEARN_RULES}).",
        "",
        "## Authoring sequence",
        "",
        f"Add the four semantic models in exact order: {ordered_labels}.",
        "",
        f"1. Open the **{_model_label(source_models[0])}** Direct Lake semantic model in the demo tenant, "
        f"select **Generate ontology**, and create `{contract['displayName']}` in the current Fabric IQ "
        "ontology experience.",
        "2. Open the ontology agent in Fabric and keep the toggle in **Plan mode** while drafting each "
        "step below.",
        "3. Inspect the read-only draft preview in the ontology canvas, complete the human review "
        "checklist, and switch to **Act mode** only to apply a reviewed step.",
        "4. Set composite keys, validity intervals, and cross-model relationship bindings by hand in the "
        "ontology canvas whenever automatic generation leaves them unset.",
        "5. Keep every metric linked to a source measure in its home semantic model. Do not copy or edit "
        "calculations in the ontology.",
        "6. Never write an exported ontology definition back over a working item because links from "
        "metrics to source measures do not round-trip through the definition API "
        f"([Ontology item definition]({LEARN_DEFINITION}), checked 16 November 2026).",
        "",
        "## Plan prompts by source model",
        "",
    ]

    seen_models: set[str] = set()
    emitted_rules: set[str] = set()

    for idx, model_id in enumerate(source_models, start=1):
        seen_models.add(model_id)
        label = _model_label(model_id)
        model_entities = [e for e in entities if e["model"] == model_id]
        model_metrics = [m for m in metrics if m["model"] == model_id]
        step_relationships = _relationships_for_step(relationships, entity_models, model_id, seen_models)
        step_rules = _rules_for_step(rules, entity_models, seen_models, emitted_rules)

        lines.append(f"### Step {idx}: {label} (`{model_id}`)")
        lines.append("")
        if idx == 1:
            lines.append(
                f"Generate `{contract['displayName']}` from `{model_id}.SemanticModel`, then send this "
                "prompt in **Plan mode** to verify and refine the initial model scope before adding "
                "other semantic models:"
            )
        else:
            lines.append(
                f"With the ontology agent in **Plan mode**, send this prompt to extend "
                f"`{contract['displayName']}` with `{model_id}.SemanticModel`:"
            )
        lines.append("")
        lines.append("```text")
        if idx == 1:
            lines.append(
                f"Stay in Plan mode. Review {contract['displayName']} generated from the {label} "
                f"semantic model ({model_id}.SemanticModel) for the service assurance scenario."
            )
        else:
            lines.append(
                f"Stay in Plan mode. Extend {contract['displayName']} with the {label} semantic model "
                f"({model_id}.SemanticModel) for the service assurance scenario."
            )
        lines.append(
            "Keep source-qualified entity names so shared dimensions remain distinct per semantic model "
            "and each metric stays on its home table."
        )
        lines.append("")
        lines.append(f"Entity types to include from {label}:")
        for entity in model_entities:
            key_list = ", ".join(entity.get("key", []))
            extra_notes: list[str] = []
            if "interval" in entity:
                span = entity["interval"]
                extra_notes.append(f"interval {span['from']}..{span['to']}")
            if "validity" in entity:
                span = entity["validity"]
                extra_notes.append(f"validity {span['from']}..{span['to']}")
            suffix = f" ({'; '.join(extra_notes)})" if extra_notes else ""
            lines.append(
                f"- {entity['name']} ({entity.get('logical', entity['name'])}) bound to "
                f"{entity.get('modelTable', entity['table'])} [{entity['table']}], key [{key_list}]{suffix}"
            )

        lines.append("")
        lines.append("Relationships to verify or bind in this step:")
        for rel in step_relationships:
            join_list = ", ".join(rel.get("on", []))
            tags: list[str] = []
            if rel.get("crossesModels"):
                tags.append("cross-model")
            if rel.get("identity"):
                tags.append("identity")
            if "validity" in rel:
                tags.append(rel["validity"])
            if "period" in rel:
                tags.append(rel["period"])
            tag_text = f" [{'; '.join(tags)}]" if tags else ""
            lines.append(f"- {rel['from']} -> {rel['to']} on [{join_list}]{tag_text}: {rel['meaning']}")

        lines.append("")
        lines.append("Metrics to keep linked to source measures without copying formulas:")
        for metric in model_metrics:
            grain_list = ", ".join(metric["grain"])
            add_flag = "additive" if metric["additive"] else "non-additive"
            lines.append(
                f"- {metric['name']} on {metric['entity']} (table {metric['modelTable']}, "
                f"grain [{grain_list}], {add_flag}, unit {metric['unit']})"
            )

        if step_rules:
            lines.append("")
            lines.append("Plain-language business rules to attach once their entity types exist:")
            for rule in step_rules:
                targets = ", ".join(rule.get("linkedTo", []))
                lines.append(f"- {rule['id']} {rule['name']} [{targets}]: {rule['statement']}")
        else:
            lines.append("")
            lines.append("Defer cross-model business rules until their linked entity types are added.")

        lines.append("")
        lines.append(
            "Do not alter semantic model tables, measures, or security roles. Post the read-only draft "
            "preview and list any key or relationship binding that still needs manual setup before Act mode."
        )
        lines.append("```")
        lines.append("")
        lines.append(f"Expected `{label}` elements in the contract:")
        lines.append("")
        for entity in model_entities:
            lines.append(_format_entity_line(entity))
        for rel in step_relationships:
            lines.append(_format_relationship_line(rel))
        for metric in model_metrics:
            lines.append(_format_metric_line(metric))
        for rule in step_rules:
            lines.append(_format_rule_line(rule))
        lines.append("")

    lines.extend(
        [
            "## Human review checklist before Act mode",
            "",
            "Complete every item below in the read-only canvas preview before switching the ontology "
            "agent from **Plan mode** to **Act mode** (and re-verify after the final step for checks "
            f"{checks}):",
            "",
            f"- [ ] **Scope and entity count**: All {len(entities)} source-qualified entity types across "
            f"{ordered_labels} appear in `{contract['displayName']}`, with no unintended duplicate or "
            "dropped tables.",
            "- [ ] **Keys and time bounds**: Every scalar key (`incident_key`, `site_key`, `service_key`, "
            "`contract_key`, `account_key`, `case_key`), composite grain (`site_key` + `hour_start`, "
            "`service_key` + `site_key` + `valid_from`, `service_key` + `month`, `contract_key` + "
            "`month`, `account_key` + `month`, `case_key` + `snapshot_date`), and UTC interval "
            "(`start`..`end`, `valid_from`..`valid_to`) is bound to the exact source column by hand "
            "where automatic generation left it blank.",
            f"- [ ] **Relationships and bridges**: All {len(relationships)} contract relationships exist, "
            "including the cross-model bridges on `site_key` and `account_key` and the Enterprise "
            "services links from `enterprise_ServiceSiteMap` to `enterprise_Contracts`, "
            "`enterprise_SlaEvaluation`, and `enterprise_AppliedCredits`. No relationship joins on "
            "`display_name` or `billing_key`.",
            f"- [ ] **Metrics linked to source measures**: All {len(metrics)} metrics sit on their home "
            "entity and remain linked to source measures in the four semantic models, with zero copied "
            "or hand-edited formulas.",
            f"- [ ] **Plain-language rules**: All {len(rules)} rules (`R1` to `R5`) match the contract "
            "statements word for word and link to every listed entity type.",
            "- [ ] **Source safety**: Semantic models, `East Region` row-level security roles, and "
            "workspace settings in the demo tenant are untouched, and any definition export is inspected "
            "read-only with `python tools/check_ontology_conformance.py` rather than written back.",
            "",
            "## Question prompts (L1 to L4)",
            "",
            "Run these prompts in **Plan mode** (or through a connected demo user session) after "
            "publishing the reviewed ontology:",
            "",
            "### L1: Single model control (`Network`)",
            "",
            "```text",
            "How many major incidents were there in July 2026, by market? Use the Major Incidents metric "
            "from Network and report exact integer counts.",
            "```",
            "",
            "### L2: Two models and one validity interval (`Network`, `Enterprise services`)",
            "",
            "```text",
            "Which enterprise services ran over sites that had a major incident in July 2026? Join "
            "sites on site_key and enforce half-open UTC overlap across the incident interval, the "
            "service-site assignment, and the contract validity period (rule R1). Report any unmapped "
            "service as an exclusion rather than zero (rule R5).",
            "```",
            "",
            "### L3: Three models and two grains (`Network`, `Enterprise services`, `Finance`)",
            "",
            "```text",
            "For the contracts behind those services, what is the credit exposure and the recognized "
            "service revenue per account for July 2026? Keep Credit Exposure at contract and month "
            "(rule R2), deduplicate accounts on account_key before looking up Recognized Service "
            "Revenue at account and month in integer cents (rules R3 and R4), and list exclusions "
            "instead of substituting zero for unknown rows (rule R5).",
            "```",
            "",
            "### L4: Four-model service assurance hero question (`Network`, `Enterprise services`, "
            "`Finance`, `Care`)",
            "",
            "```text",
            f"{contract['businessQuestion']} Return a ranked list of enterprise accounts by Credit "
            "Exposure with affected sites and contracts, Credit Exposure for the contract-month, "
            "Recognized Service Revenue and Contribution Margin for the account-month in integer cents, "
            "Escalated Cases from Care, and an explicit list of exclusions where mappings or credit rows "
            "are absent (rules R1, R2, R3, R4, and R5).",
            "```",
            "",
        ]
    )

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        type=Path,
        default=DEFAULT_CONTRACT,
        help="Path to the ontology contract JSON file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path to the rendered markdown prompt file.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify that --output matches the rendered contract without modifying files.",
    )
    args = parser.parse_args(argv)

    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    rendered = render_prompts(contract)

    if args.check:
        if not args.output.is_file():
            print(f"Prompt pack is missing: {args.output}", file=sys.stderr)
            return 1
        existing = args.output.read_text(encoding="utf-8").replace("\r\n", "\n")
        if existing != rendered:
            print(
                f"Prompt pack is out of date with {args.contract}: regenerate {args.output}",
                file=sys.stderr,
            )
            return 1
        print(f"Prompt pack is up to date: {args.output}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
