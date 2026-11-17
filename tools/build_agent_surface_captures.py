"""Deterministically build the four sanitized agent-surface captures for the service assurance scenario."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from telco_iq.demo_library import RecordedAgentSurface, SurfaceQuestionTurn

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDINGS_DIR = ROOT / "recordings"
CAPTURE_DATE_TAG = "2026-11-17"
CAPTURED_AT = datetime(2026, 11, 17, 11, 55, 31, tzinfo=timezone.utc)

L2_QUESTION = (
    "Which enterprise services ran over sites that had a major incident in July 2026, "
    "and how many major incidents overlapped each service's active assignment and contract?"
)
L4_QUESTION = (
    "Which enterprise customers were served from sites hit by July 2026 incidents, "
    "what SLA credit exposure do we carry on their contracts, how much revenue and margin "
    "is at stake, and who already has escalated cases?"
)

FOUNDRY_LIVE_L2_MARKDOWN = """Summary answer
- Matches: 53 enterprise services had at least one major-incident overlap in July 2026 (Exposure defined per the ontology rule ExposureNeedsOverlap).
- Top affected services (by number of overlapping major incidents, from the grounded query results):
  - service_key 1 — 3 major incidents
  - service_key 2 — 2 major incidents
  - service_key 6 — 2 major incidents
  - service_key 3 — 1 major incident
  - service_key 4 — 1 major incident
  - service_key 5 — 1 major incident
  - service_key 7 — 1 major incident
  - service_key 8 — 1 major incident
  - service_key 9 — 1 major incident
  - service_key 17 — 1 major incident
- Full detail: the query returned 53 matching service rows (top 10 shown above). If you need the full list, run the same service-site overlap query at enterprise services grain.

Key provenance and rules applied
- Ontology rule used: ExposureNeedsOverlap — "A service is exposed to an incident only when the incident interval, the service-site assignment and the contract are valid at the same time. Validity periods are half-open UTC intervals."
- Relationship path used to evaluate overlap:
  - enterprise_Services -> enterprise_ServiceSiteMap (service-to-site assignment) -> enterprise_Sites -> network_Incidents (Incidents table)
  - enterprise_Services -> enterprise_Contracts (contract validity checked)
- Semantic-model source measures and queries:
  - network model: 'Incidents' table, measure [Major Incidents]. Query used: SUMMARIZECOLUMNS('Incidents'[site_key], ... TREATAS({DATE(2026,7,1)}, 'Incidents'[month]), "major_incidents", [Major Incidents]).
  - enterprise model: 'SLA Evaluation' (for contract/SLA context and credit exposure measures [Credit Exposure], [Eligible Credits]) — used for contract-month level checks (see rules about CreditPerContractMonth).
- Grounded results used: the provided grounded_query_results.top_rows and total_matching_rows.

Known exclusions / unknowns (explicit)
- incident_key 7 at site_key 6 for 2026-07 excluded due to "unmapped_incident_window" — incident window mapping is unknown (cannot count).
- service_key 10 (contract_key 8, account_key 6, site_key 6) for 2026-07 excluded due to "incomplete_mapping_coverage" — service/site/contract mapping incomplete (unknown).
- Per ontology rule UnknownIsNotZero: these exclusions are unknown and must not be treated as zeros.

Concise recommended next actions (for account teams / ops to review)
1. Verify mappings and re-run:
   - Fix the unmapped incident window (incident_key 7) and the incomplete mapping for service_key 10, then re-run the overlap query to update counts.
2. Prioritize triage and account briefings for the highest-count services:
   - Priority order: service_key 1 (3 incidents) → service_key 2 and 6 (2 incidents) → the 1-incident services.
3. Prepare account-level impact pack for each prioritized service:
   - Pull SLA credit exposure per contract-month from 'SLA Evaluation'[Credit Exposure] and eligible credits ([Eligible Credits]) for July 2026.
   - Pull recognized revenue / contribution margin (finance model) for the affected account-month to prioritize commercial risk.
4. Operational investigation:
   - Correlate incident timelines (network Incidents) to service assignment and contract validity windows to produce an incident overlap timeline (per ExposureNeedsOverlap).
   - Attach runbook and incident postmortem notes (use the standard incident runbook and the SLA review checklist).
5. Customer outreach planning (draft only — do not contact without account team approval):
   - For each prioritized account, produce a 1-page executive summary: scope (services/sites impacted), incident timeline with overlap windows, probable SLA exposure, recommended remediation and next steps. Assign an account owner to review before any outreach.

References to use when preparing the account packs
- network model: 'Incidents' table — measure [Major Incidents]; DAX used to pull July 2026 month.
- enterprise model: 'SLA Evaluation' table — measures [Credit Exposure], [Eligible Credits]; enterprise_Contracts for contract validity windows.
- Ontology rule: ExposureNeedsOverlap and UnknownIsNotZero.

If you want, I can:
- Return the full list of all 53 service_key rows (with their major_incident counts).
- Run the additional join to include account_key and contract_key for the top N services so you can assemble account-level briefings. Which would you prefer?"""

FOUNDRY_LIVE_L4_MARKDOWN = """Summary (July 2026, per-month measures)
- Scope: 46 affected enterprise account-month rows matched to July 2026 incidents (top 10 shown below). Exclusions noted at the end.
- Exposure rule applied: ExposureRequiresOverlap (incident interval ∧ service-site assignment ∧ contract validity must overlap). Credit is reported per contract-month and already capped per CreditPerContractMonth.

Top affected enterprise accounts (from semantic models; amounts shown in $; source measures in parentheses)
1) account_key 1
   - Credit exposure: $135.80 (Enterprise → SLA Evaluation [Credit Exposure], month = 2026-07; value shown as 13580 cents)
   - Recognized service revenue: $90.00 (Finance → Account Month [Recognized Service Revenue], 9000 cents)
   - Contribution margin: -$14.00 (Finance → Account Month [Contribution Margin], -1400 cents)
   - Escalated cases: 0 (Care → Cases [Escalated Cases], month = 2026-07)

2) account_key 12
   - Credit exposure: $129.60 (SLA Evaluation [Credit Exposure] = 12960 cents)
   - Revenue: $319.03 (Account Month [Recognized Service Revenue] = 31903 cents)
   - Margin: $181.68 (Account Month [Contribution Margin] = 18168 cents)
   - Escalated cases: 0 (Cases [Escalated Cases])

3) account_key 44
   - Credit exposure: $87.60 (SLA Evaluation = 8760 cents)
   - Revenue: $594.92 (Account Month = 59492 cents)
   - Margin: $365.55 (Account Month = 36555 cents)
   - Escalated cases: 1 (Cases [Escalated Cases])

4) account_key 42
   - Credit exposure: $63.00 (SLA Evaluation = 6300 cents)
   - Revenue: $446.73 (Account Month = 44673 cents)
   - Margin: $259.26 (Account Month = 25926 cents)
   - Escalated cases: 1 (Cases [Escalated Cases])

5) account_key 36
   - Credit exposure: $61.20 (SLA Evaluation = 6120 cents)
   - Revenue: $501.93 (Account Month = 50193 cents)
   - Margin: $288.31 (Account Month = 28831 cents)
   - Escalated cases: 1 (Cases [Escalated Cases])

6) account_key 2
   - Credit exposure: $57.00 (SLA Evaluation = 5700 cents)
   - Revenue: $240.00 (Account Month = 24000 cents)
   - Margin: $0.00 (Account Month = 0 cents)
   - Escalated cases: 1 (Cases [Escalated Cases])

7) account_key 32
   - Credit exposure: $55.80 (SLA Evaluation = 5580 cents)
   - Revenue: $429.25 (Account Month = 42925 cents)
   - Margin: $249.73 (Account Month = 24973 cents)
   - Escalated cases: 0 (Cases [Escalated Cases])

8) account_key 81
   - Credit exposure: $52.20 (SLA Evaluation = 5220 cents)
   - Revenue: $723.67 (Account Month = 72367 cents)
   - Margin: $411.46 (Account Month = 41146 cents)
   - Escalated cases: 1 (Cases [Escalated Cases])

9) account_key 74
   - Credit exposure: $42.30 (SLA Evaluation = 4230 cents)
   - Revenue: $802.70 (Account Month = 80270 cents)
   - Margin: $483.62 (Account Month = 48362 cents)
   - Escalated cases: 0 (Cases [Escalated Cases])

10) account_key 68
    - Credit exposure: $41.40 (SLA Evaluation = 4140 cents)
    - Revenue: $699.20 (Account Month = 69920 cents)
    - Margin: $415.62 (Account Month = 41562 cents)
    - Escalated cases: 0 (Cases [Escalated Cases])

Notes on measures and lineage (required citations)
- Credit exposure: Enterprise semantic model → SLA Evaluation table → measure [Credit Exposure] (reporting is per contract-month; CreditPerContractMonth rule enforced).
- Revenue and margin: Finance semantic model → Account Month table → measures [Recognized Service Revenue], [Contribution Margin] (FinancePerAccountMonth rule: values are per account-month).
- Escalations: Care semantic model → Cases table → measure [Escalated Cases].
- Relationship path (ontology traversal used): Network.Incidents (site_key, month=2026-07) -> enterprise_ServiceSiteMap (site_key -> service/contract assignments) -> enterprise_SLAEvaluation (contract_key, account_key, month) -> finance_AccountMonth (account_key, month) and care_Cases (account_key, month). ExposureNeedsOverlap rule applied to require validity overlap between incident interval, service-site assignment and contract.

Totals and scope
- Results shown are the top rows returned by the semantic-model queries; total matching account-month rows = 46 (grounded_query_results.total_matching_rows). The top-10 listed above are the highest credit exposures in the returned set.

Known exclusions / unknowns (do not assume zero)
- Incident_key 7 at site_key 6 (month 2026-07): exclusion reason = unmapped_incident_window (unknown impact because incident window could not be mapped).
- service_key 10 / contract_key 8 / account_key 6 at site_key 6 (month 2026-07): exclusion reason = incomplete_mapping_coverage.
- account_key 6 / contract_key 8 (month 2026-07): incomplete_mapping_coverage.
- Per the ontology rule UnknownIsNotZero, these are unknown and must be resolved before quoting exposure for those items.

Who already has escalated cases (from Care)
- account_keys with escalated_cases > 0 (in returned top rows): 44, 42, 36, 2, 81.
- Accounts without escalations in top rows: 1, 12, 32, 74, 68.

Recommended immediate next steps for account-team review (concise, for human execution)
1. Validate & reconcile (owners: Service Assurance lead + Commercial Operations)
   - Confirm incident start/restoration timestamps against Network.Incidents and verify ExposureNeedsOverlap for each exposed service/contract.
   - Re-run or inspect rows for the 3 excluded items (site_key 6 / account_key 6 / incident_key 7) to resolve mapping gaps (these remain unknown until fixed).

2. Prioritize outreach queue (owners: Account Directors + Service Assurance)
   - Lead with accounts carrying highest Credit Exposure (account_keys 1, 12, 44, 42, 36), then sort ties by Contribution Margin and active escalations.
   - For accounts with active escalated cases (44, 42, 36, 2, 81): schedule a joint 15-minute huddle with the Care escalation owner before any customer message (see incident_runbook Step 2).

3. Draft briefing per account (owner: Account Director)
   - Include exact source-cited metrics and ontology path for each account (use the measures/tables cited above).
   - Include explicit exclusions and unknowns for analyst follow-up.
   - Request Service Assurance lead sign-off on incident facts and Commercial Operations sign-off on the credit exposure figures (human sign-off gates required).

4. Outreach timing (use SLA tiers as guide)
   - Determine customer service tier from contract metadata and apply timing from sla_terms (e.g., Tier 1: initial notice within 30 minutes; Tier 2: 60 minutes; Tier 3: next business day).
   - If tier is unknown in contract metadata, escalate determination to Service Assurance lead before outreach.

References attached to this recommendation
- Enterprise measures: SLA Evaluation.[Credit Exposure] (Enterprise semantic model)
- Finance measures: Account Month.[Recognized Service Revenue], Account Month.[Contribution Margin] (Finance semantic model)
- Care measures: Cases.[Escalated Cases] (Care semantic model)
- Ontology rules applied: ExposureNeedsOverlap, CreditPerContractMonth, FinancePerAccountMonth, UnknownIsNotZero
- Guidance docs: sla_terms and incident_runbook (attached knowledge docs for outreach timing and runbook steps)

If you want, I can:
- Produce a short per-account briefing note (one paragraph each) for the top 5 accounts including the exact ontology path and sign-off checklist, or
- Return the full list of all 46 matched account-month rows with the same cited measures."""

FABRIC_MCP_L2_MARKDOWN = """Ontology MCP context + Fabric IQ MCP V2 execution (live demo tenant capture, 17 November 2026)

1. Ontology context discovered live (`ontologyEndpoint`, protocol `2025-06-18`):
   - `list_ontology_entities` (1.33s): 17 entity types across `network`, `enterprise`, `finance`, and `care`, including `network_Incidents`, `network_Sites`, `enterprise_Sites`, `enterprise_ServiceSiteMap`, `enterprise_Services`, and `enterprise_Contracts`.
   - `list_ontology_rules` (0.22s): 5 rules returned (`ExposureNeedsOverlap`, `CreditPerContractMonth`, `FinancePerAccountMonth`, `IdentityIsAccountKey`, `UnknownIsNotZero`).

2. Semantic-model queries executed live (`Fabric.Routing.FabricIQ.V2` `ExecuteQuery`, protocol `2025-11-25`):
   - `Northstar_Network` (`Incidents` table, measure `[Major Incidents]`):
     `EVALUATE SUMMARIZECOLUMNS('Incidents'[site_key], 'Incidents'[market_id], TREATAS({DATE(2026, 7, 1)}, 'Incidents'[month]), "major_incidents", [Major Incidents])` -> 13 July 2026 site-market rows (`site_key` 16, 12, 4 in `M04`; `site_key` 14, 10 in `M02`, plus remaining July major-incident sites).
   - `Northstar_Enterprise` (`Service Site Map` and `Contracts` half-open UTC overlap check against incident windows):
     53 exposed enterprise services matched (`service_key=1`: 3 overlapping major incidents; `service_key=2` and `service_key=6`: 2 overlapping major incidents; `service_key=3, 4, 5, 7, 8, 9, 17` and 43 further services: 1 overlapping major incident).

3. Explicit exclusions (`UnknownIsNotZero` / `ExposureNeedsOverlap`):
   - `incident_key=7` at `site_key=6` (`2026-07`): `unmapped_incident_window` (reported as unknown, not zero).
   - `service_key=10` (`contract_key=8`, `account_key=6`, `site_key=6`, `2026-07`): `incomplete_mapping_coverage` (reported as unknown, not zero)."""

FABRIC_MCP_L4_MARKDOWN = """Ontology MCP context + Fabric IQ MCP V2 four-model execution (live demo tenant capture, 17 November 2026)

1. Connected seven-hop path from `list_ontology_entities` + `list_ontology_rules`:
   - `network_Incidents` -> `network_Sites` -> `enterprise_ServiceSiteMap` -> `enterprise_Contracts` -> `enterprise_SlaEvaluation` -> `enterprise_Accounts` -> `finance_AccountMonth` and `care_Cases`.
   - Rules applied: `ExposureNeedsOverlap` (R1), `CreditPerContractMonth` (R2), `FinancePerAccountMonth` (R3), `IdentityIsAccountKey` (R4), and `UnknownIsNotZero` (R5).

2. Live `ExecuteQuery` calls across all four Direct Lake semantic models:
   - `Northstar_Network`: `Incidents[Major Incidents]` (13 site-market rows in July 2026)
   - `Northstar_Enterprise`: `SLA Evaluation[Credit Exposure]` and `[Eligible Credits]` (settled once per contract-month)
   - `Northstar_Finance`: `Account Month[Recognized Service Revenue]` and `[Contribution Margin]` (deduplicated to one row per `account_key` and month)
   - `Northstar_Care`: `Cases[Escalated Cases]` and `[Repeat Contacts]` (65 July 2026 account rows)

3. Top ranked enterprise accounts (46 matched account-month rows; integer cents):
   - `account_key=1`: Credit Exposure `13,580` cents | Revenue `9,000` cents | Margin `-1,400` cents | Escalated Cases `0`
   - `account_key=12`: Credit Exposure `12,960` cents | Revenue `31,903` cents | Margin `18,168` cents | Escalated Cases `0`
   - `account_key=44`: Credit Exposure `8,760` cents | Revenue `59,492` cents | Margin `36,555` cents | Escalated Cases `1`
   - `account_key=42`: Credit Exposure `6,300` cents | Revenue `44,673` cents | Margin `25,926` cents | Escalated Cases `1`
   - `account_key=36`: Credit Exposure `6,120` cents | Revenue `50,193` cents | Margin `28,831` cents | Escalated Cases `1`

4. Explicit exclusions (`UnknownIsNotZero`):
   - `incident_key=7` (`site_key=6`, `2026-07`): `unmapped_incident_window`
   - `service_key=10` (`contract_key=8`, `account_key=6`, `site_key=6`, `2026-07`): `incomplete_mapping_coverage`
   - `account_key=6` (`contract_key=8`, `2026-07`): `incomplete_mapping_coverage`"""

GITHUB_COPILOT_L2_MARKDOWN = """Staged Visual Studio Code developer preview (`.mcp.example.json` + `agents/shared/instructions.md`)

1. MCP servers configured in `.mcp.json` (workspace-local, uncommitted):
   - `northstarOntology` (`.../workspaces/<WORKSPACE_ID>/items/<ONTOLOGY_ITEM_ID>/ontologyEndpoint`): reads `enterprise_Services`, `enterprise_ServiceSiteMap`, `enterprise_Contracts`, `network_Incidents`, and rules `ExposureNeedsOverlap` + `UnknownIsNotZero`.
   - `fabricIq` (`https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq`): inspects `Northstar_Network` and `Northstar_Enterprise` schemas and runs `ExecuteQuery` on `Incidents[Major Incidents]`.

2. Grounded `L2` result (53 exposed services; top rows):
   - `service_key=1`: 3 overlapping July 2026 major incidents
   - `service_key=2`, `service_key=6`: 2 overlapping July 2026 major incidents
   - `service_key=3, 4, 5, 7, 8, 9, 17` (+43 additional services): 1 overlapping July 2026 major incident

3. Exclusions kept explicit (`UnknownIsNotZero`):
   - `incident_key=7` at `site_key=6`: `unmapped_incident_window`
   - `service_key=10` (`contract_key=8`, `account_key=6`, `site_key=6`): `incomplete_mapping_coverage`

4. Developer workflow verification:
   - Pair with `/explain-hero-path` and `/find-credit-exposure-measure` in `.github/prompts/` to inspect measure ownership in `tools/build_models.py` without copying DAX formulas into ontology contracts or prompt files."""

GITHUB_COPILOT_L4_MARKDOWN = """Staged Visual Studio Code developer preview across all four semantic models (`L4`)

1. Traversal path verified from `northstarOntology` (`/explain-hero-path`):
   - Hop 1–3 (`Network` -> `Enterprise`): `Incident` -> `Site` (`site_key`) -> `Service-site assignment` -> `Contract` (half-open UTC overlap across incident, assignment, and contract windows).
   - Hop 4–5 (`Enterprise`): `Contract` -> `SLA evaluation` (`Credit Exposure` once per contract-month) and `Account` (`account_key`).
   - Hop 6 (`Enterprise` -> `Finance`): `Account` -> `Account month` (`Recognized Service Revenue`, `Contribution Margin` at one row per account-month).
   - Hop 7 (`Enterprise` -> `Care`): `Account` -> `Case` (`Escalated Cases`, `Repeat Contacts`).

2. Grounded `L4` result from `fabricIq` `ExecuteQuery` (46 ranked enterprise accounts; top 5 shown in integer cents):
   - `account_key=1`: `13,580` cents exposure | `9,000` cents revenue | `-1,400` cents margin | `0` escalated cases
   - `account_key=12`: `12,960` cents exposure | `31,903` cents revenue | `18,168` cents margin | `0` escalated cases
   - `account_key=44`: `8,760` cents exposure | `59,492` cents revenue | `36,555` cents margin | `1` escalated case
   - `account_key=42`: `6,300` cents exposure | `44,673` cents revenue | `25,926` cents margin | `1` escalated case
   - `account_key=36`: `6,120` cents exposure | `50,193` cents revenue | `28,831` cents margin | `1` escalated case

3. Exclusions reported as unknown (3 items):
   - `incident_key=7` (`site_key=6`): `unmapped_incident_window`
   - `service_key=10` (`contract_key=8`, `account_key=6`, `site_key=6`): `incomplete_mapping_coverage`
   - `account_key=6` (`contract_key=8`): `incomplete_mapping_coverage`"""

COPILOT_STUDIO_L2_MARKDOWN = """Staged Microsoft Copilot Studio -> Microsoft Teams & Microsoft 365 Copilot preview (`L2`)

**Headline:** 53 enterprise services had active site assignments and contracts overlapping July 2026 major incidents (`Network` -> `Enterprise`, rule `ExposureNeedsOverlap`).

**Highest-overlap services**
- `service_key=1` — 3 major incidents (`Network.Incidents[Major Incidents]`)
- `service_key=2` and `service_key=6` — 2 major incidents each
- 50 additional enterprise services — 1 overlapping major incident each

**Unknown / excluded rows (`UnknownIsNotZero`)**
- `incident_key=7` (`site_key=6`, July 2026): unmapped incident window — excluded as unknown.
- `service_key=10` (`contract_key=8`, `account_key=6`, `site_key=6`, July 2026): incomplete serving-site mapping coverage — excluded as unknown.

**Channel routing note (verified 17 November 2026):** Ontology context reaches Microsoft Teams and Microsoft 365 Copilot through this published Copilot Studio agent (`mcp_FabricIQOntology`), not through a Fabric data agent."""

COPILOT_STUDIO_L4_MARKDOWN = """Staged Microsoft Copilot Studio -> Microsoft Teams & Microsoft 365 Copilot preview (`L4`)

**Headline:** 46 enterprise accounts matched across `Network`, `Enterprise`, `Finance`, and `Care` for July 2026 outreach review (ranked by contract-month `Credit Exposure`, rule `CreditPerContractMonth`, with one row per account-month in `Finance`, rule `FinancePerAccountMonth`).

**Priority outreach queue (top 5 of 46; human review required)**
1. `account_key=1` — Credit Exposure `$135.80` (`13,580` cents) · Revenue `$90.00` (`9,000` cents) · Margin `-$14.00` (`-1,400` cents) · Escalated Cases `0`
2. `account_key=12` — Credit Exposure `$129.60` (`12,960` cents) · Revenue `$319.03` (`31,903` cents) · Margin `$181.68` (`18,168` cents) · Escalated Cases `0`
3. `account_key=44` — Credit Exposure `$87.60` (`8,760` cents) · Revenue `$594.92` (`59,492` cents) · Margin `$365.55` (`36,555` cents) · Escalated Cases `1` (active escalation)
4. `account_key=42` — Credit Exposure `$63.00` (`6,300` cents) · Revenue `$446.73` (`44,673` cents) · Margin `$259.26` (`25,926` cents) · Escalated Cases `1` (active escalation)
5. `account_key=36` — Credit Exposure `$61.20` (`6,120` cents) · Revenue `$501.93` (`50,193` cents) · Margin `$288.31` (`28,831` cents) · Escalated Cases `1` (active escalation)

**Unknown / excluded items (3 exclusions; never treated as zero)**
- `incident_key=7` (`site_key=6`): `unmapped_incident_window`
- `service_key=10` (`contract_key=8`, `account_key=6`): `incomplete_mapping_coverage`
- `account_key=6` (`contract_key=8`): `incomplete_mapping_coverage`

**Recommended reviewer actions**
- Hold a joint Care escalation huddle before contacting `account_key` 44, 42, 36, 2, or 81.
- Resolve the `site_key=6` / `account_key=6` mapping gaps before quoting SLA credit figures for those contracts."""


def build_agent_surfaces() -> list[RecordedAgentSurface]:
    return [
        RecordedAgentSurface(
            surface_id="fabric-ontology-mcp",
            surface_name="Ontology MCP endpoint + Fabric IQ MCP server",
            provenance="LIVE_DEMO_TENANT_SANITIZED_CAPTURE",
            captured_at=CAPTURED_AT,
            connection_summary=(
                "Delegated demo user session in the demo tenant (F8, Central US) over "
                "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/<WORKSPACE_ID>/items/"
                "<ONTOLOGY_ITEM_ID>/ontologyEndpoint (protocol 2025-06-18; list_ontology_entities 1.33s, "
                "list_ontology_rules 0.22s) paired with https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq "
                "(protocol 2025-11-25, Fabric.Routing.FabricIQ.V2 ExecuteQuery across all four Direct Lake models)."
            ),
            turns=[
                SurfaceQuestionTurn(
                    question_id="SA-L2-001",
                    level="L2",
                    question=L2_QUESTION,
                    tool_calls=[
                        "ontologyEndpoint:list_ontology_entities",
                        "ontologyEndpoint:list_ontology_rules",
                        "fabriciq:ExecuteQuery(Northstar_Network)",
                        "fabriciq:ExecuteQuery(Northstar_Enterprise)",
                    ],
                    models_queried=["network", "enterprise"],
                    answer_summary=(
                        "53 exposed enterprise services matched across 13 July 2026 major-incident site groups "
                        "after enforcing half-open UTC overlap across incident, service-site assignment, and "
                        "contract windows (R1); 2 unmapped or incomplete items reported as unknown (R5)."
                    ),
                    answer_markdown=FABRIC_MCP_L2_MARKDOWN,
                    row_count=53,
                    exclusions_reported=2,
                ),
                SurfaceQuestionTurn(
                    question_id="SA-L4-001",
                    level="L4",
                    question=L4_QUESTION,
                    tool_calls=[
                        "ontologyEndpoint:list_ontology_entities",
                        "ontologyEndpoint:list_ontology_rules",
                        "fabriciq:ExecuteQuery(Northstar_Network)",
                        "fabriciq:ExecuteQuery(Northstar_Enterprise)",
                        "fabriciq:ExecuteQuery(Northstar_Finance)",
                        "fabriciq:ExecuteQuery(Northstar_Care)",
                    ],
                    models_queried=["network", "enterprise", "finance", "care"],
                    answer_summary=(
                        "46 affected enterprise accounts ranked by contract-month Credit Exposure across Network, "
                        "Enterprise, Finance, and Care without multiplying account-month revenue or margin (R2, R3); "
                        "3 unmapped or incomplete items reported as unknown (R5)."
                    ),
                    answer_markdown=FABRIC_MCP_L4_MARKDOWN,
                    row_count=46,
                    exclusions_reported=3,
                ),
            ],
            limitations=[
                "Sanitized live capture from the demo tenant (F8, Central US, 17 November 2026); tenant, workspace, and semantic-model identifiers are replaced with placeholders.",
                "Ontology MCP returned all 17 entity types, 17 relationships, and 5 rules live (1.33s / 0.22s) while DAX measures executed through Fabric IQ MCP V2 ExecuteQuery; ask_ontology requires an interactive model-binding pass in Fabric (platform-status.md row 15).",
                "Fabric data agent over a semantic-model-bound ontology was tested live on 17 November 2026 and confirmed does not work; use the published Copilot Studio agent for Microsoft 365 Copilot and Microsoft Teams.",
            ],
        ),
        RecordedAgentSurface(
            surface_id="foundry-agent",
            surface_name="Microsoft Foundry agent (gpt-5-mini)",
            provenance="LIVE_DEMO_TENANT_SANITIZED_CAPTURE",
            captured_at=CAPTURED_AT,
            connection_summary=(
                "Microsoft Foundry prompt agent (gpt-5-mini) in the demo Azure subscription and demo tenant "
                "(F8, Central US) grounded on live Ontology MCP metadata (17 entities, 5 rules), live Fabric IQ "
                "MCP V2 semantic-model query results, and synthetic outreach documents (sla-terms.md, incident-runbook.md)."
            ),
            turns=[
                SurfaceQuestionTurn(
                    question_id="SA-L2-001",
                    level="L2",
                    question=L2_QUESTION,
                    tool_calls=[
                        "ontologyEndpoint:list_ontology_entities",
                        "ontologyEndpoint:list_ontology_rules",
                        "fabriciq:ExecuteQuery(Northstar_Network)",
                        "fabriciq:ExecuteQuery(Northstar_Enterprise)",
                        "foundry:responses(gpt-5-mini)",
                    ],
                    models_queried=["network", "enterprise"],
                    answer_summary=(
                        "Live gpt-5-mini response (3,763 tokens) identifying 53 exposed enterprise services led by "
                        "service_key 1 (3 major incidents) and service_keys 2 and 6 (2 major incidents), citing "
                        "ExposureNeedsOverlap and UnknownIsNotZero with 2 explicit exclusions."
                    ),
                    answer_markdown=FOUNDRY_LIVE_L2_MARKDOWN,
                    row_count=53,
                    exclusions_reported=2,
                ),
                SurfaceQuestionTurn(
                    question_id="SA-L4-001",
                    level="L4",
                    question=L4_QUESTION,
                    tool_calls=[
                        "ontologyEndpoint:list_ontology_entities",
                        "ontologyEndpoint:list_ontology_rules",
                        "fabriciq:ExecuteQuery(Northstar_Network)",
                        "fabriciq:ExecuteQuery(Northstar_Enterprise)",
                        "fabriciq:ExecuteQuery(Northstar_Finance)",
                        "fabriciq:ExecuteQuery(Northstar_Care)",
                        "foundry:knowledge(sla-terms.md,incident-runbook.md)",
                        "foundry:responses(gpt-5-mini)",
                    ],
                    models_queried=["network", "enterprise", "finance", "care"],
                    answer_summary=(
                        "Live gpt-5-mini response (6,296 tokens) ranking 46 affected enterprise accounts led by "
                        "account_key 1 ($135.80 / 13,580 cents exposure) and account_key 12 ($129.60 / 12,960 cents), "
                        "flagging active Care escalations (44, 42, 36, 2, 81), listing 3 unknown exclusions, and "
                        "drafting a human sign-off outreach plan."
                    ),
                    answer_markdown=FOUNDRY_LIVE_L4_MARKDOWN,
                    row_count=46,
                    exclusions_reported=3,
                ),
            ],
            limitations=[
                "Sanitized live capture from Microsoft Foundry (gpt-5-mini) and Microsoft Fabric in the demo tenant on 17 November 2026; subscription, project, workspace, and item identifiers are omitted.",
                "Recommendations cite synthetic Northstar Wireless sla-terms.md and incident-runbook.md guidance only; outreach and credit decisions require human sign-off.",
                "Replay displays the recorded response offline and does not issue a live Foundry or Microsoft Fabric request.",
            ],
        ),
        RecordedAgentSurface(
            surface_id="github-copilot-vscode",
            surface_name="GitHub Copilot in Visual Studio Code",
            provenance="STAGED_SURFACE_PREVIEW_CAPTURE",
            captured_at=CAPTURED_AT,
            connection_summary=(
                "Staged developer preview capture using the workspace .mcp.example.json dual-server configuration "
                "(northstarOntology + fabricIq V2) and .github/prompts/*.prompt.md workflows grounded on the "
                "live-verified 17 November 2026 ontology contract and semantic-model query results "
                "(interactive Visual Studio Code OAuth GUI session remains TODO(verify-live): G5)."
            ),
            turns=[
                SurfaceQuestionTurn(
                    question_id="SA-L2-001",
                    level="L2",
                    question=L2_QUESTION,
                    tool_calls=[
                        "northstarOntology:list_ontology_entities",
                        "northstarOntology:list_ontology_rules",
                        "fabricIq:GetSemanticModelSchema(Northstar_Network,Northstar_Enterprise)",
                        "fabricIq:ExecuteQuery(Northstar_Network,Northstar_Enterprise)",
                    ],
                    models_queried=["network", "enterprise"],
                    answer_summary=(
                        "Staged developer workflow preview mapping Hop 1 to Hop 3 across Network and Enterprise "
                        "(site_key bridge + half-open UTC overlap R1), returning 53 exposed services and 2 unknown "
                        "exclusions without copying DAX into prompts."
                    ),
                    answer_markdown=GITHUB_COPILOT_L2_MARKDOWN,
                    row_count=53,
                    exclusions_reported=2,
                ),
                SurfaceQuestionTurn(
                    question_id="SA-L4-001",
                    level="L4",
                    question=L4_QUESTION,
                    tool_calls=[
                        "northstarOntology:list_ontology_entities",
                        "northstarOntology:list_ontology_rules",
                        "fabricIq:DiscoverArtifacts",
                        "fabricIq:ExecuteQuery(Northstar_Network)",
                        "fabricIq:ExecuteQuery(Northstar_Enterprise)",
                        "fabricIq:ExecuteQuery(Northstar_Finance)",
                        "fabricIq:ExecuteQuery(Northstar_Care)",
                    ],
                    models_queried=["network", "enterprise", "finance", "care"],
                    answer_summary=(
                        "Staged developer workflow preview tracing all seven hops across Network, Enterprise, "
                        "Finance, and Care, ranking 46 affected enterprise accounts by Credit Exposure "
                        "(account_key=1 at 13,580 cents) with 3 explicit R5 exclusions."
                    ),
                    answer_markdown=GITHUB_COPILOT_L4_MARKDOWN,
                    row_count=46,
                    exclusions_reported=3,
                ),
            ],
            limitations=[
                "Staged surface preview capture (STAGED_SURFACE_PREVIEW_CAPTURE, 17 November 2026), not a recording of an interactive Visual Studio Code GUI click-through.",
                "Grounded on the live-verified 17 November 2026 Ontology MCP schema/rules and Fabric IQ MCP V2 query results; interactive Visual Studio Code OAuth user sign-off remains tracked as TODO(verify-live): G5 in docs/architecture/platform-status.md.",
                "Workspace .mcp.example.json keeps <WORKSPACE_ID> and <ONTOLOGY_ITEM_ID> out of source control.",
            ],
        ),
        RecordedAgentSurface(
            surface_id="copilot-studio-m365",
            surface_name="Microsoft Copilot Studio (Teams & Microsoft 365 Copilot)",
            provenance="STAGED_SURFACE_PREVIEW_CAPTURE",
            captured_at=CAPTURED_AT,
            connection_summary=(
                "Staged low-code channel preview for the Northstar Service Assurance agent over Agent 365 "
                "mcp_FabricIQOntology (McpServers.FabricIQOntology.All; list_ontology_entity_types, search_ontology) "
                "grounded on the live-verified 17 November 2026 ontology contract and semantic-model query results. "
                "Published Copilot Studio agent is the verified route into Microsoft Teams and Microsoft 365 Copilot "
                "(interactive Agent 365 OAuth user sign-off remains TODO(verify-live): G5)."
            ),
            turns=[
                SurfaceQuestionTurn(
                    question_id="SA-L2-001",
                    level="L2",
                    question=L2_QUESTION,
                    tool_calls=[
                        "mcp_FabricIQOntology:list_ontology_entity_types",
                        "mcp_FabricIQOntology:search_ontology",
                    ],
                    models_queried=["network", "enterprise"],
                    answer_summary=(
                        "Staged Copilot Studio and Microsoft 365 Copilot preview over mcp_FabricIQOntology "
                        "reporting 53 exposed enterprise services in July 2026, citing Network and Enterprise "
                        "measures and flagging 2 unmapped or incomplete exclusions."
                    ),
                    answer_markdown=COPILOT_STUDIO_L2_MARKDOWN,
                    row_count=53,
                    exclusions_reported=2,
                ),
                SurfaceQuestionTurn(
                    question_id="SA-L4-001",
                    level="L4",
                    question=L4_QUESTION,
                    tool_calls=[
                        "mcp_FabricIQOntology:list_ontology_entity_types",
                        "mcp_FabricIQOntology:search_ontology",
                    ],
                    models_queried=["network", "enterprise", "finance", "care"],
                    answer_summary=(
                        "Staged Copilot Studio and Microsoft 365 Copilot preview ranking 46 affected enterprise "
                        "accounts for outreach review (led by account_key 1 at 13,580 cents exposure and "
                        "account_key 44 with active Care escalation) and isolating 3 unknown exclusions."
                    ),
                    answer_markdown=COPILOT_STUDIO_L4_MARKDOWN,
                    row_count=46,
                    exclusions_reported=3,
                ),
            ],
            limitations=[
                "Staged surface preview capture (STAGED_SURFACE_PREVIEW_CAPTURE, 17 November 2026), not a recording of an interactive Copilot Studio, Microsoft Teams, or Microsoft 365 Copilot GUI session.",
                "Grounded on the live-verified 17 November 2026 ontology contract and semantic-model query results; interactive Agent 365 OAuth user sign-off (McpServers.FabricIQOntology.All) remains tracked as TODO(verify-live): G5 in docs/architecture/platform-status.md.",
                "Live testing on 17 November 2026 confirmed that a Fabric data agent over a semantic-model-bound ontology does not work; publishing this Copilot Studio agent is the supported route into Microsoft 365 Copilot and Microsoft Teams.",
            ],
        ),
    ]


def surface_filename(surface_id: str) -> str:
    return f"surface-{surface_id}-{CAPTURE_DATE_TAG}.json"


def write_surface_files(recordings_dir: Path = DEFAULT_RECORDINGS_DIR) -> list[Path]:
    recordings_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for surface in build_agent_surfaces():
        out_path = recordings_dir / surface_filename(surface.surface_id)
        payload = surface.model_dump(mode="json")
        serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        out_path.write_text(serialized, encoding="utf-8", newline="\n")
        if RecordedAgentSurface.read(out_path) != surface:
            raise RuntimeError(f"Surface capture failed exact typed readback: {out_path}")
        written.append(out_path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=DEFAULT_RECORDINGS_DIR,
        help="Output folder for standalone surface JSON captures",
    )
    args = parser.parse_args(argv)
    paths = write_surface_files(recordings_dir=args.recordings_dir)
    print(
        json.dumps(
            {
                "status": "AGENT_SURFACE_CAPTURES_WRITTEN",
                "count": len(paths),
                "files": [path.name for path in paths],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
