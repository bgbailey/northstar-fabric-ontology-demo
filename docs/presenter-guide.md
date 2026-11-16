# Northstar presenter guide

## Run of show

Use [demo-streams.md](demo-streams.md) as the entry point for startup and readiness checks. Open in Microsoft Fabric with the two-model quickstart ontology ([executive-demo-script.md](executive-demo-script.md)), then transition to the service assurance SLA exposure and account-priority segment ([architecture/hero-scenario.md](architecture/hero-scenario.md)).

The local web viewer (`apps/web`) provides side-by-side evidence, DAX query receipts, and recorded fallback; it does not replace the product experience in Fabric. Before presenting live, verify recorded playback first and run live questions only after fresh demo user sign-in and rehearsal in your demo tenant.

## Two ontology scopes

Northstar includes a compact quickstart ontology over two semantic models (four entity types, three relationships, 14 metrics linked to source measures, and two rules; contract at `fabric/ontology/executive-demo.json`) alongside the eight-model Customer360 estate (59 entity types, 105 relationships, 64 metrics linked to source measures, and four rules) and the four-model service assurance scenario. Calculations remain in their source semantic models.

## Choose the server mode

Run from the repository root after building `apps/web/dist` (`npm --prefix apps/web run build`). These commands work in bash and PowerShell (`./tools/start-demo.ps1` is an optional PowerShell wrapper):

| Mode | Command | Meaning |
|---|---|---|
| Both recorded streams | `python -m telco_iq.cli serve --recorded-library recordings/northstar-demo-library-v2-2026-10-28.json --port 8767` | Quickstart answer from the ontology agent in Fabric first, Customer360 and service assurance stories second; no sign-in or live calls |
| Recorded comparisons only | `python -m telco_iq.cli serve --recorded-comparisons recordings/northstar-recorded-live-v6-2026-10-28.json --port 8767` | Ten recorded evaluation arm pairs with no live fallback |
| Live demo user comparison | `python -m telco_iq.cli serve --reader-config <private-demo-user.json> --live-config <private-live.json> --port 8765` | Approved demo user session in a demo tenant (see [live demo user setup](reader-demo.md)); no silent recording or fixture fallback |
| Single ontology-agent recording | `python -m telco_iq.cli serve --recorded-native recordings/northstar-two-model-recorded-demo-2026-09-30.json --port 8768` | Historical two-model capture from the ontology agent in Fabric |

Keep private configuration files outside the repository and outside synchronized folders. For the two-model evaluation arm walkthrough inside Customer360, see [getting-started-demo-script.md](getting-started-demo-script.md).

## Explain the two evaluation arms

Each evaluation arm (one side of a controlled comparison) uses the same language model, the same model-level AI preparation, and the same execution validators:

- **Models and flat guidance:** starts from semantic-model schemas and equivalent flat business instructions.
- **Ontology context in Fabric:** reads entity types, relationships, metrics linked to source measures, and plain-language rules from the Fabric IQ ontology (current experience, preview checked 16 November 2026).

Both arms execute governed DAX queries on the semantic models that own the measures. The execution canvas displays observed application steps, not an internal reasoning trace from the ontology agent in Fabric.

## Three multi-domain stories

| Story | Business question | Interpretation boundary |
|---|---|---|
| Service assurance (SLA exposure) | Which enterprise contracts were exposed to July incidents, what contract-month credit exposure applies, and which affected accounts rank highest by margin and revenue? | Incident, service-site assignment, and contract validity intervals must overlap; contract-month credits and account-month finance stay in separate views; unknown credit rows exclude a complete credit ranking |
| Incident recovery | Which exposed accounts show opening-cohort churn and repeat care contacts, and what August randomized retention-offer evidence exists? | Keep July account metrics separate from August offer assignments; preserve randomized treatment/control denominators and consent exclusions; association is not causation |
| Checkout release | How do June and July checkout conversion and median journey duration vary by release and device, alongside linked orders and campaign mix? | Keep all eligible sessions (including anonymous sessions) in conversion denominators; attribute orders only by explicit `session_key`; booked merchandise is not recognized service revenue |

### Service assurance live rehearsal prompt

> For July 2026, identify known-mapped enterprise contracts exposed to network incidents overlapping serving-site and contract validity. Show source-owned Credit Exposure at contract-month grain, preserving unknown inputs and coverage warnings. In a separate account-month view, prioritize the top 10 known-mapped affected accounts by Contribution Margin descending, with Recognized Service Revenue in cents. Report unknown-mapping exclusions. Do not duplicate account Finance across contracts, allocate monthly credit to one incident, or treat partial results as a complete exposure total.

When using recorded playback (`Customer360 deep dive` -> `SLA exposure and account priority (Oct 28 live)`), point out the seven contract-month rows, the separate six-account priority view, and the partial result notice (showing excluded unmapped observations and null-key rows). Across the October 28 SLA exposure (7 contract-month rows), incident recovery (12 account rows), and checkout release (4 month/release/device rows) runs, 335 known metric cells were verified against source query responses.

## Presenting the evidence

1. Select a story preset to populate the business question without triggering a hidden call or canned answer.
2. Read the primary answer table and its status pill first; a partial result (with excluded unmapped observations or null-key rows) describes a known-mapped cohort rather than the full population, and unknown values are never zero.
3. Expand additional grain-specific views separately so contract-month amounts, distinct counts, ratios, and medians are never summed across incompatible grains.
4. Open a DAX receipt and the execution canvas to show the exact semantic-model query and watermark clocks.
5. Keep data, semantic-model, ontology, and context-cache clocks distinct; exporting a recording does not refresh source data.

## Limitations

See [limitations.md](limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
