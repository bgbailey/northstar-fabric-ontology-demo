# Starter prompts for the service assurance scenario

Use these starter prompts to test any agent connected to the Northstar Wireless [Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) (preview, checked 16 November 2026) in your demo tenant. Pair them with the shared agent instructions in [`instructions.md`](instructions.md).

The four prompts step through the evaluation ladder defined in [the service assurance scenario](../../docs/architecture/hero-scenario.md) and [`eval/questions/service-assurance.jsonl`](../../eval/questions/service-assurance.jsonl). Each step adds Power BI semantic models, cross-model bridges, validity windows, or separate grains while keeping business rules in the ontology and calculations in the semantic models.

| Level | Semantic models | What the prompt exercises |
|---|---|---|
| `L1` | `Network` | Single semantic model control |
| `L2` | `Network`, `Enterprise` | One cross-model site bridge and active service-to-site timing |
| `L3` | `Network`, `Enterprise`, `Finance` | Two cross-model bridges and separate contract-month and account-month grains |
| `L4` | `Network`, `Enterprise`, `Finance`, `Care` | Full four-model service assurance decision with care escalations |

## `L1` — Single semantic model control

```text
How many major network incidents occurred in July 2026 by market?
```

## `L2` — Two models, one bridge, and validity window

```text
Which enterprise services ran over sites that had a major incident in July 2026 during the incident window?
```

## `L3` — Three models, two bridges, and separate grains

```text
For the enterprise contracts behind services exposed to July 2026 major incidents, what is the SLA credit exposure and what is each affected customer account's recognized service revenue for July 2026?
```

## `L4` — Four models, full service assurance decision

```text
Which enterprise customers were served from sites hit by July 2026 major incidents, what SLA credit exposure do we carry on their contracts, how much recognized service revenue and contribution margin is at stake, and which accounts already have escalated care cases?
```
