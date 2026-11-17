# 0002. Four-model service assurance scenario

**Status:** Accepted, 16 November 2026

## Context

The repository includes two existing scopes: a two-model quickstart (Subscriptions and Finance) and an eight-model telecommunications estate (`59` entity types and `105` relationships).

Neither scope alone demonstrates why connected business context matters in a live demo tenant:

- **Two models are too simple.** Subscriptions and Finance share one conformed `account_key` and one billing month. A capable agent can infer that join from table schemas without an ontology, so the quickstart confirms that [ontology metrics](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics) (in preview, checked 16 November 2026) reuse source measures without testing cross-domain ambiguity.
- **Eight models are too wide for interactive rehearsal.** A multi-hop query across the full eight-model ontology exceeded interactive response-time bounds during rehearsal, and wide column bindings are a known constraint in preview ([ontology troubleshooting](https://learn.microsoft.com/en-us/fabric/iq/ontology/resources-troubleshooting), checked 16 November 2026).

## Decision

- Select the four-model [service assurance scenario](../architecture/hero-scenario.md) spanning **Network**, **Enterprise services**, **Finance**, and **Care** as the primary scenario (`D4` in [`PLAN.md`](../../PLAN.md)).
- Centre the scenario on one post-incident decision: identifying enterprise accounts served from incident-affected sites, their contract-month SLA credit exposure, their account-month revenue and margin at stake, and their escalated care cases.
- Model the seven-hop path across three cross-model bridges (`site_key` from Network to Enterprise services, `account_key` to Finance, and `account_key` to Care), two half-open UTC time-valid relationships (`Service Site Map` and `Contracts`), two relationships across tables inside Enterprise services that are not joined in that model (`Service Site Map` to `Contracts` and `Contracts` to `SLA Evaluation`), three measure grains, and five plain-language [ontology rules](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-rules) (`R1` to `R5`, in preview, checked 16 November 2026).
- Generate the ontology from the first semantic model and add the remaining three through the [ontology agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-agent) in Microsoft Fabric ([generate from semantic models](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-generate-from-semantic-models), both in preview, checked 16 November 2026).
- Keep the two-model quickstart as a short introductory check and keep all eight semantic models as the surrounding operator estate, with Subscriptions available as an optional fifth-model extension.

## Consequences

- Each hop exercises a realistic failure mode when cross-model rules are missing: ignoring validity intervals (`R1`), allocating a monthly contract credit cap per incident (`R2`), multiplying account-month revenue across contracts (`R3`), matching on display names or billing keys (`R4`), and turning missing rows into zeros (`R5`).
- Binding only the tables and columns required for the four-model path keeps the ontology small enough to review and sized for interactive responses in a demo tenant (verified by live checks `G1`, `G2`, and `G3` in [platform status](../architecture/platform-status.md#confirmed-in-a-demo-tenant)). Ranked, aggregated queries also stay within the ontology agent's default 200-row DAX limit.
- If preview response times tighten on smaller demo capacities, Care can be asked as a follow-up turn over a three-model core (Network, Enterprise services, Finance) without altering any semantic model.
