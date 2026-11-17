# Starter prompts for Microsoft Copilot Studio

Paste these four suggested prompts (`L1` to `L4`) into **Overview -> Suggested prompts** when configuring the `Northstar Service Assurance` agent in Microsoft Copilot Studio in the demo tenant. Each prompt relies on the connected Fabric IQ ontology (`mcp_FabricIQOntology`) for entity types, relationships, metric links, and business rules.

## Suggested prompts table

| Level | Suggested prompt title | Prompt text |
|---|---|---|
| `L1` | July major incidents by market | `How many major incidents were recorded in July 2026, by market? Cite the semantic model, home table, and measure.` |
| `L2` | Exposed enterprise services | `Which enterprise services ran over sites that had a major incident in July 2026, and how many major incidents overlapped each active service and contract? List any exclusions.` |
| `L3` | Credit exposure and revenue | `For the contracts behind enterprise services exposed to July 2026 major incidents, what are the SLA credit exposure and the July 2026 recognized service revenue per account? Cite the ontology path, measures, and any exclusions.` |
| `L4` | Service assurance priorities | `Which enterprise customers were served from sites hit by July 2026 incidents, what SLA credit exposure do we carry on their contracts, how much revenue and margin is at stake, and who already has escalated cases? Rank affected accounts, list exclusions, and suggest review priorities.` |

## Copy-ready blocks

### L1 — July major incidents by market

```text
How many major incidents were recorded in July 2026, by market? Cite the semantic model, home table, and measure.
```

### L2 — Exposed enterprise services

```text
Which enterprise services ran over sites that had a major incident in July 2026, and how many major incidents overlapped each active service and contract? List any exclusions.
```

### L3 — Credit exposure and revenue

```text
For the contracts behind enterprise services exposed to July 2026 major incidents, what are the SLA credit exposure and the July 2026 recognized service revenue per account? Cite the ontology path, measures, and any exclusions.
```

### L4 — Service assurance priorities

```text
Which enterprise customers were served from sites hit by July 2026 incidents, what SLA credit exposure do we carry on their contracts, how much revenue and margin is at stake, and who already has escalated cases? Rank affected accounts, list exclusions, and suggest review priorities.
```
