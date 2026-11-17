# 0004. Three-arm evaluation and evidence-gated claims

**Status:** Accepted, 16 November 2026

## Context

An agent with governed access to Power BI semantic models can compute cross-domain numbers without an ontology when it infers the right joins or carries domain rules in its instructions. Claiming that only an ontology can answer multi-model questions would be false. Conversely, an earlier offline comparison gave every configuration identical pre-assembled facts, producing matching results by design and measuring nothing about cross-model context.

Agent responses are also non-deterministic across runs. Testing where cross-domain meaning belongs requires holding data, models, [AI preparation](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-prepare-data-ai) (in preview, checked 16 November 2026), user permissions, and model deployments equal across repeated runs ([how a question is answered](../architecture/README.md#how-a-question-is-answered)).

## Decision

- Evaluate three **evaluation arms** (referred to below as *arms*) across the [golden question ladder](../architecture/hero-scenario.md#golden-questions) (`L1` single-model control, `L2` to `L4` multi-model hops, `N` unknown and exclusion cases, and `P` row-level security checks) (`D9` and section 5 of [`PLAN.md`](../../PLAN.md)):
  - **Arm A (semantic models only):** Queries the four semantic models through the generally available [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) (checked 16 November 2026, default version 2 toolset), inferring cross-model bridges from schemas and per-model AI preparation.
  - **Arm A+ (semantic models plus prompt rules):** Uses the exact setup of Arm A with business rules `R1` to `R5` pasted into the agent instructions, measuring the trade-off of maintaining domain rules inside individual prompts without weakening the baseline.
  - **Arm B (ontology-grounded):** Reads entity types, bridges, metrics linked to source measures, and rules `R1` to `R5` from the [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) (in preview, checked 16 November 2026) with minimal shared agent instructions that never restate the rules.
- Enforce strict parity across all three arms: identical synthetic data, identical Direct Lake semantic models, identical single-model AI preparation (descriptions, AI data schemas, and verified `L1` answers), identical Microsoft Entra ID demo user permissions, identical model deployment and temperature settings, and `k = 5` repeated runs per question.
- Score every answer deterministically against an independent DuckDB oracle isolated from runtime code (`src/telco_iq` never imports `eval`): exact match on integer cents and counts, account-set precision/recall/F1, named trap hits, unknown and exclusion handling, path correctness, effort (tool calls, tokens, seconds), and repeat stability, using Wilson intervals and paired bootstrap confidence intervals.
- Enforce an evidence gate on all claims: documentation may claim a measurable advantage only when linked to a published scorecard whose confidence interval for the difference excludes zero. Ties, partial results (stating what is excluded), and losses are published openly ([limitations](../limitations.md)).

## Consequences

- `L1` single-model questions verify that every arm starts from a well-prepared semantic-model baseline rather than a strawman.
- Including both Arm A and Arm A+ distinguishes discovery errors (when cross-model rules are absent) from governance trade-offs (copying five rules into every agent prompt versus sharing one reviewed ontology across agents).
- If live demo tenant runs show no statistically significant accuracy gain for Arm B, that result is published as-is and positioning stays strictly on single-definition reuse across agent surfaces.
