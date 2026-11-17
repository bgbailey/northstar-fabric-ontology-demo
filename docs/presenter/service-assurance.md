# Service assurance run of show

This twenty-minute presenter guide walks through the four-model service assurance scenario ([../architecture/hero-scenario.md](../architecture/hero-scenario.md)) using a [Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) (current experience, in preview, checked 16 November 2026) over **Network**, **Enterprise services**, **Finance**, and **Care** Direct Lake Power BI semantic models.

Lead with the product experience in Microsoft Fabric and connected agent surfaces. Keep the local Northstar web viewer (`http://localhost:8767/`) open in a second browser tab for DAX query receipts, scorecard comparisons, and immediate recorded fallback. All customer names, accounts, contracts, sites, incidents, cases, and amounts are synthetic. An **evaluation arm** is one tested configuration given the same business question, semantic models, and model-level AI preparation.

## Before you start

1. **Review platform status and delivery modes:** Check current preview capabilities and constraints in [../architecture/platform-status.md](../architecture/platform-status.md), recorded server options in [../run/streams.md](../run/streams.md), and delegated demo user safeguards in [../run/live.md](../run/live.md).
2. **Start the local recorded viewer:** Build `apps/web` (`npm --prefix apps/web run build`) if needed, then start the recorded library server from the repository root (works in bash and PowerShell; keep it running on port `8767` even during live sessions so fallback is instant):

```bash
northstar serve --recorded-library recordings/northstar-demo-library-v2-2026-10-28.json --port 8767
```

3. **Confirm recorded tabs load:** Open `http://localhost:8767/` and verify that the recorded viewer renders with zero cloud calls (**Service assurance** stream when present in the loaded library, alongside **Customer360 deep dive** -> **SLA exposure** and **Executive quickstart**). If you also run a live two-arm comparison, start that separate delegated session on port `8765` per [../run/live.md](../run/live.md).
4. **Verify live checks `G1` to `G6` before presenting live in a demo tenant:** Confirm that every check below has passed in your demo tenant and is recorded with a date in [../architecture/platform-status.md](../architecture/platform-status.md#confirmed-in-a-demo-tenant) (pass conditions in [../architecture/hero-scenario.md](../architecture/hero-scenario.md#checks-before-presenting-live)):

| Check | Confirm in demo tenant before going live |
|---|---|
| `G1` Four-model ontology | One ontology item binds Network, Enterprise services, Finance, and Care Direct Lake models, and all 14 metrics link to their home measures |
| `G2` Relationships between models | The `Site` (`site_key`) and `Account` (`account_key`) cross-model bridges and both time-valid relationships exist and appear in query plans |
| `G3` Interactive answer | The service assurance question completes within the interactive time budget on your demo capacity three times in a row |
| `G4` MCP tools | The item-scoped [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) (in preview, checked 16 November 2026) lists its tools under interactive demo user sign-in, with names and schemas reviewed |
| `G5` Agent surfaces | Planned surfaces ([Microsoft Foundry](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/fabric-iq), [Microsoft Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-fabric-iq-ontology), the published agent in Microsoft Teams / Microsoft 365 Copilot, and GitHub Copilot in Visual Studio Code — ontology connectors in preview, checked 16 November 2026) answer the `L2` two-model question. Leave [Fabric data agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-create-data-agent) out unless verified in your demo tenant ([../decisions/0005-agent-surfaces.md](../decisions/0005-agent-surfaces.md)) |
| `G6` Security | A demo user in the `East Region` row-level security role sees only `East Region` accounts on every surface |

If any live check is unverified or slow during rehearsal, present that segment from `http://localhost:8767/` and state clearly that you are showing a sanitized recording.

## Scenario anchor

> **Business question:** Which enterprise customers were served from sites hit by July incidents, what SLA credit exposure do we carry on their contracts, how much revenue and margin is at stake, and who already has escalated cases?

Keep four structural facts in view throughout the talk track ([path and rules](../architecture/hero-scenario.md#the-path)):

- **7 hops across 4 semantic models:** `Incident -> Site -> Service-site assignment -> Contract -> SLA evaluation`, plus `Contract -> Account -> Account month` and `Account -> Case`.
- **3 cross-model bridges:** `Site` (`site_key`) from Network to Enterprise services, `Account` (`account_key`) from Enterprise services to Finance, and `Account` (`account_key`) from Enterprise services to Care.
- **2 time-valid relationships:** half-open UTC intervals (`valid_from <= t < valid_to`) requiring the July incident window, `Service-site assignment`, and `Contract` to overlap (`R1`).
- **3 distinct grains with source-owned DAX:** incident and site-hour operational counts in Network, **contract-month** credit settlement (`Credit Exposure`, `Eligible Credits`, `Applied Credits`) in Enterprise services (`R2`), and **account-month** financials (`Recognized Service Revenue`, `Contribution Margin`) in Finance (`R3`), plus case counts (`Escalated Cases`) in Care.

## Twenty-minute run of show

| Minutes | Segment | Primary surface | Recording fallback on `http://localhost:8767/` |
|---|---|---|---|
| `0–2` | The decision | Microsoft Fabric workspace (`Incidents` view) | **Service assurance** (or **Customer360 deep dive** -> **SLA exposure**) question banner |
| `2–5` | The estate | Four Direct Lake semantic models and `East Region` role | Measure cards and home-model DAX query receipts |
| `5–8` | Semantic models only | Agent on Fabric IQ MCP server (`L1`, then `L4`) | **Models and flat guidance** evaluation arm and receipts |
| `8–12` | The ontology | Service assurance ontology item in Microsoft Fabric | Seven-hop path, ontology graph, metrics, and rules |
| `12–16` | The same question, grounded | Ontology agent in Microsoft Fabric | Grounded answer, contract-month and account-month views, and exclusions |
| `16–19` | One ontology, many agents | Microsoft Foundry, Microsoft Copilot Studio, GitHub Copilot | Recorded ontology answers and [architecture flow](../architecture/README.md) |
| `19–20` | The scorecard | Northstar viewer **Scorecard** (`http://localhost:8767/`) | 17 November 2026 `k = 5` live [scorecard.json](../../recordings/evaluation/2026-11-17/scorecard.json), trap summary, ties/failures, and `Limitations` |

### Minute 0 to 2 — The decision (`0–2 min`)

- **What to click:** Open the Microsoft Fabric workspace in your demo tenant (or transition from [quickstart.md](quickstart.md)). Show the Network `Incidents` view filtered to July 2026 major incidents. Keep the service assurance question visible.
- **What to say:** "A July network incident hit core sites. Before calling enterprise customers, the service-assurance lead needs one ranked answer. Which accounts had active services on those sites during the outage? What SLA credit exposure sits on their contracts, how much monthly revenue and margin is at stake, and who already has escalated care cases?"
- **Recording fallback (`http://localhost:8767/`):** Open the **Service assurance** stream (or **Customer360 deep dive** -> **SLA exposure** in `recordings/northstar-demo-library-v2-2026-10-28.json`) and point to the submitted July business question banner.

### Minute 2 to 5 — The estate (`2–5 min`)

- **What to click:** In the Microsoft Fabric workspace item list or lineage view, highlight the four Direct Lake Power BI semantic models over OneLake: **Network**, **Enterprise services**, **Finance**, and **Care**. Open **Enterprise services** (`SLA Evaluation` -> `Credit Exposure`), **Finance** (`Account Month` -> `Recognized Service Revenue` and `Contribution Margin`), and **Manage roles** (`East Region` row-level security role).
- **What to say:** "Four domain teams already own trusted Direct Lake semantic models. Network owns incident and site-hour measures. Enterprise services settles SLA credits per contract-month. Finance governs recognized service revenue and contribution margin per account-month, and Care tracks escalated cases. Every DAX formula, table grain, and security role stays where it lives today."
- **Recording fallback (`http://localhost:8767/`):** In **Service assurance** (or **Customer360 deep dive** -> **SLA exposure**), inspect the measure cards showing each metric's home model and table. Open one DAX query receipt to show the original measure running unchanged.

### Minute 5 to 8 — Semantic models only (`5–8 min`)

- **What to click:** In an agent connected only to the generally available [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) (checked 16 November 2026; evaluation arm A), ask the `L1` control question (*"How many major incidents were there in July 2026, by market?"*). Next, submit the four-model service assurance question. Show the rule block required in evaluation arm A+ when rules `R1` to `R5` are pasted into individual agent instructions ([../decisions/0004-evaluation-and-claims.md](../decisions/0004-evaluation-and-claims.md)).
- **What to say:** "On one model, semantic models and [AI preparation](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-prepare-data-ai) (in preview, checked 16 November 2026) answer cleanly. Across four models, the agent must infer three cross-model bridges and check two UTC validity windows so expired site assignments are excluded. It must keep monthly contract credits from multiplying across incidents, and collapse contracts to one row per account-month before reading Finance. Without shared context, you risk plausible-looking grain errors or maintain copies of five rules inside every prompt."
- **Recording fallback (`http://localhost:8767/`):** In **Customer360 deep dive** -> **SLA exposure** (or the **Service assurance** comparison view), inspect the **Models and flat guidance** evaluation arm, **Exact submitted business question**, and its per-model DAX query receipts.

### Minute 8 to 12 — The ontology (`8–12 min`)

- **What to click:** Open the service assurance ontology item in Microsoft Fabric (in preview, checked 16 November 2026). Walk the seven-hop graph: highlight the three cross-model bridges (`Site` on `site_key` from Network to Enterprise services, and `Account` on `account_key` from Enterprise services to Finance and Care) plus the Enterprise services links (`Service-site assignment` to `Contract`, and `Contract` to `SLA evaluation`). Click the `Credit Exposure` and `Contribution Margin` [metrics](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics) to show their source-measure links, then open [rules](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-rules) `R1` to `R5`. Keep the [ontology agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-agent) in **Plan** mode unless applying a reviewed change.
- **What to say:** "The ontology stores no duplicated rows and no copied DAX. It connects 13 logical entity types across seven hops, declares three cross-model bridges and two time-valid intervals, and links 14 metrics to source DAX measures across three grains. Five plain-language rules live in one place: overlapping validity (`R1`), one contract-month credit settlement (`R2`), one row per account-month in Finance (`R3`), `account_key` identity rather than display name or billing key (`R4`), and unknown reported as an exclusion (`R5`)."
- **Recording fallback (`http://localhost:8767/`):** Open the **Service assurance** stream's seven-hop path and ontology graph (or the **Customer360 deep dive** ontology structure tab) to inspect entity types, bridges, metrics linked to source measures, and rules.

### Minute 12 to 16 — The same question, grounded (`12–16 min`)

- **What to click:** Ask the service assurance question in the ontology agent in Microsoft Fabric (or run ontology-grounded evaluation arm B). Walk through the seven-hop plan and expand the DAX queries sent to Network, Enterprise services, Finance, and Care. Review the ranked accounts alongside the separate contract-month credit view and exclusions list.
- **What to say:** "Grounded in the ontology, the agent follows all seven hops and runs governed DAX on each semantic model at its own grain. July incidents filter `Service-site assignment` and `Contract` by overlapping UTC validity. `Credit Exposure` stays at contract-month grain, `Recognized Service Revenue` and `Contribution Margin` stay at one row per account-month without fan-out, and `Escalated Cases` joins from Care on `account_key`. Unmapped services and missing credit rows appear as explicit exclusions in a partial result — never as zero."
- **Recording fallback (`http://localhost:8767/`):** Open **Service assurance** (and **Customer360 deep dive** -> **SLA exposure**). Inspect the seven-hop path, the DAX query receipts and watermark clocks per model, the separate contract-month rows (7 rows in the October 28 capture) and account-month priority view (6 accounts), and the partial result notice (excluding unmapped observations and null-key rows).

### Minute 16 to 19 — One ontology, many agents (`16–19 min`)

- **What to click:** Show the same ontology item serving external agent surfaces (ontology connectors in preview, checked 16 November 2026; [../decisions/0005-agent-surfaces.md](../decisions/0005-agent-surfaces.md)):
  1. **Microsoft Foundry:** Run the agent with the [Fabric IQ tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/fabric-iq) (`fabric_iq_preview`) on the ontology MCP endpoint under delegated user identity to rank accounts and draft a human-reviewed outreach plan citing source measures.
  2. **Microsoft Copilot Studio in Microsoft Teams or Microsoft 365 Copilot:** Ask an account-team follow-up through the published [Copilot Studio agent](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-fabric-iq-ontology) (`mcp_FabricIQOntology`), then repeat as an `East Region` demo user (`G6`) to show row-level security filtering.
  3. **GitHub Copilot in Visual Studio Code:** Connected via [../develop/agent-tooling.md](../develop/agent-tooling.md), ask which semantic model owns `Credit Exposure` and propose a measure test without copying DAX into the ontology.
- **What to say:** "One ontology serves pro-code, low-code, collaboration, and developer agents. Microsoft Foundry, Microsoft Copilot Studio in Microsoft Teams and Microsoft 365 Copilot, and GitHub Copilot all read the same entity types, bridges, metrics linked to source measures, and rules. Delegated Microsoft Entra ID identity enforces `East Region` security where DAX executes, and every agent recommends outreach for human approval rather than acting automatically."
- **Recording fallback (`http://localhost:8767/`):** Stay on `http://localhost:8767/` (**Service assurance** and **Executive quickstart** tabs) to show recorded ontology-grounded answers and verified DAX query receipts. Pair with the diagram in [../architecture/README.md](../architecture/README.md) if live agent licensing or capacity is unavailable.

### Minute 19 to 20 — The scorecard (`19–20 min`)

- **What to click:** Switch to `http://localhost:8767/` and open the **Scorecard** stream (backed by [recordings/evaluation/2026-11-17/scorecard.json](../../recordings/evaluation/2026-11-17/scorecard.json) and [scorecard.html](../../recordings/evaluation/2026-11-17/scorecard.html); design and claims rules in [../evaluation/README.md](../evaluation/README.md) and [../decisions/0004-evaluation-and-claims.md](../decisions/0004-evaluation-and-claims.md)). Point to the run conditions (`F8`, `Central US`, `gpt-5-mini (2025-08-07)`, `k = 5`, `405` runs), `L1` parity (`86.7%` across all three evaluation arms), `missing-as-zero` trap hits (`16` in Arm A vs `0` in Arm A+ and Arm B), cohort key F1 (`0.734` in Arm B vs `0.609` in Arm A+ and `0.598` in Arm A), higher tool/token effort in Arm B (`8.91` mean tool calls vs `7.30` and `7.42`), and the inconclusive 95% paired bootstrap intervals on overall exact-match accuracy (`Arm B vs Arm A`: `+4.4%` `[-0.7%, +11.1%]`; `Arm B vs Arm A+`: `+2.2%` `[-0.7%, +6.7%]`). Expand the 60 failed question-by-arm entries (`L3`, `L4`, and `P` row-level security under workspace-admin credentials).
- **What to say:** "We measure across five repeats per question (`405` live runs in [scorecard.json](../../recordings/evaluation/2026-11-17/scorecard.json)) rather than asserting gains from one demo run. Single-model `L1` controls tie at `86.7%` across all three evaluation arms. Overall exact-match accuracy is `28.1%` on semantic models alone, `30.4%` with the five rules pasted into the prompt, and `32.6%` with the ontology — and because the 95% paired bootstrap confidence intervals cross zero, we report overall exact-match accuracy as an inconclusive tie, not a proven win. Both rule-carrying evaluation arms eliminate the `16` `missing-as-zero` trap hits seen in Arm A, and the ontology evaluation arm reaches `0.734` returned-key F1 (`0.880` precision) at the cost of `8.91` mean tool calls and more input tokens for schema and rule discovery. Where `Arm B` and `Arm A+` tie, the value is governing entities, bridges, metrics linked to source measures, and rules in one Microsoft Fabric item instead of maintaining duplicate rule blocks across every agent prompt."
- **Recording fallback (`http://localhost:8767/`):** The **Scorecard** tab and recorded evaluation arm receipts load locally from `recordings/northstar-demo-library-v2-2026-10-28.json` and `recordings/evaluation/2026-11-17/scorecard.json`. Expand the failed-question table and the `Limitations` panel directly in `http://localhost:8767/`.

## What not to claim

| Do not claim | Say instead | Reference |
|---|---|---|
| "Semantic models cannot answer cross-domain questions without an ontology." | "An agent using the generally available Fabric IQ MCP server can query semantic models and join results when it infers bridges or carries rules in its prompt; the ontology provides one shared, reviewable definition across agents." | [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp), [ADR 0004](../decisions/0004-evaluation-and-claims.md) |
| "The ontology computes or replaces DAX measures." | "Power BI semantic models keep all DAX calculations, table grains, and row-level security; ontology metrics link to source measures (in preview, checked 16 November 2026)." | [Ontology metrics](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics), [ADR 0001](../decisions/0001-semantic-models-own-calculations.md) |
| "Ontology rules execute against data, trigger alerts, or enforce security." | "Ontology rules (in preview, checked 16 November 2026) are plain-language definitions read by agents as guidance; access control runs in semantic-model and OneLake security roles, and a person approves any customer outreach." | [Ontology rules](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-rules), [Security notes](../security.md) |
| "Semantic-model bindings project into the ontology-managed graph." | "Ontology-managed graph projection (in preview, checked 16 November 2026) applies only to keyed entity types bound to lakehouse or mirrored-database Delta tables, not semantic-model bindings; here agents plan across ontology relationships and run DAX per model." | [Ontology graph](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-graph), [ADR 0003](../decisions/0003-binding-strategy.md) |
| "The ontology guarantees higher accuracy, lower latency, or fewer tokens." | "Cite only measured differences from [recordings/evaluation/2026-11-17/scorecard.json](../../recordings/evaluation/2026-11-17/scorecard.json): in that `k = 5` run, overall exact-match intervals include zero (`32.6%` vs `30.4%` vs `28.1%`) and `Arm B` uses more tool calls and tokens than `Arm A` and `Arm A+`; report ties, partial results, and failures alongside trap reductions and cohort F1." | [Live scorecard](../../recordings/evaluation/2026-11-17/scorecard.json), [Evaluation design](../evaluation/README.md), [ADR 0004](../decisions/0004-evaluation-and-claims.md) |
| "Recorded viewer playback proves live execution or row-level security." | "Recorded mode on `http://localhost:8767/` replays sanitized captures from `recordings/` with no sign-in, live DAX query, security check, or model call; never present recorded playback as live execution." | [Demo streams](../run/streams.md), [Limitations](../limitations.md) |
| "Fabric IQ ontology or its agent connectors are generally available." | "Fabric IQ ontology (current experience), the ontology agent in Fabric, the ontology MCP endpoint, and Microsoft Foundry / Microsoft Copilot Studio ontology tools are in preview (checked 16 November 2026); point to the public roadmap rather than quoting dates." | [Platform status](../architecture/platform-status.md), [Fabric roadmap](https://roadmap.fabric.microsoft.com/?product=iq) |
| "Missing mappings or absent credit rows equal zero exposure." | "Missing service-to-site mappings, absent credit postings, and unmonitored periods are unknown and reported as exclusions in a partial result, never converted to zero (`R5`)." | [Service assurance scenario](../architecture/hero-scenario.md#rules), [Limitations](../limitations.md) |

## Limitations

See [../limitations.md](../limitations.md) and [../architecture/platform-status.md](../architecture/platform-status.md) for what offline templates and sanitized recordings in `recordings/` establish today versus what requires live verification (`G1` to `G6`) in an approved demo tenant.
