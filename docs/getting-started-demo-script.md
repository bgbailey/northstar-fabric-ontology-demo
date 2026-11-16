# Getting started: reuse trusted measures across customer domains

For the standalone two-model quickstart ontology in Fabric, see [executive-demo-script.md](executive-demo-script.md) and [quickstart ontology setup](executive-native-setup.md). This script walks through the controlled two-arm comparison over the focused four-entity slice of the Customer360 ontology in Northstar. Lead with the product experience in Fabric, then use this comparison for supporting evidence; see [demo-streams.md](demo-streams.md) for startup commands.

- **Scenario:** three business domains across two semantic models (Subscriptions and Finance), before moving to the four-model service assurance scenario ([architecture/hero-scenario.md](architecture/hero-scenario.md)).
- **Audience:** a first introduction to Fabric IQ ontology (current experience, preview checked 16 November 2026).
- **Purpose:** show reusable business context without moving or duplicating semantic-model DAX calculations.

## The small picture

| Business domain | Semantic model | What it contributes |
|---|---|---|
| Customers | Subscriptions and Finance account dimensions | Reviewed shared customer identity on `account_key`; display names alone are not join keys |
| Subscriptions | Subscriptions | Source-owned `Active Lines EOM` |
| Finance | Finance | Source-owned `Recognized Service Revenue` and `Contribution Margin` |

The ontology path connects four captured entity types: `subscriptions_SubscriptionMonth -> subscriptions_Accounts -> finance_Accounts <- finance_AccountMonth`. These are ontology definition relationships in Fabric, not a materialized instance graph. Ontology context names the home measure and supplies shared business meaning while DAX runs on the semantic models.

## Presenter run of show

Each evaluation arm (one side of a controlled comparison) receives the same semantic models, the same model-level AI preparation, and equivalent business facts.

| Beat | Show or do | Suggested words | What the evidence shows |
|---|---|---|---|
| Start with ownership | Select **Getting started** and inspect the two semantic models and three measure cards | "We already trust these semantic models. Subscriptions owns active lines. Finance owns revenue and margin. We are not rebuilding those calculations." | Each metric card links to its source semantic model, home table, and measure |
| Show semantic models with flat guidance | In the baseline arm's receipts, open the Subscriptions and Finance DAX queries | "Each semantic model answers its part, and an agent can join the results. The extra work is carrying customer identity, period, and revenue definitions in every prompt." | Source DAX reuses the same governed measures without a weakened baseline |
| Add connected context | Open the focused ontology graph in Fabric and inspect the rule and metric links | "The ontology makes shared meaning explicit and reusable: the same customer key, the same completed month, month-end lines, and recognized service revenue." | Four entity types, the conformed account bridge, metrics linked to source measures, and capture provenance |
| Ask once across domains | In live mode, run both arms; in recorded mode, select the dated capture | "For July, put the trusted measures together by customer. We want the business answer, not a rewritten formula." | Account-month rows, source-query receipts, and capture timestamps |
| Prove reuse, not replacement | Open one Finance query receipt and its measure card | "That is still the Finance measure. The ontology added context; it did not become a second owner of revenue or margin." | Existing source-measure reference with no copied cross-model DAX formula |
| Close on the takeaway | Compare both arms and expand assumptions and limitations | "Calculations stay where they are governed while cross-domain meaning becomes one reviewable asset shared across agents." | Honest reporting: matching numbers are expected here, and any failed arm stays visible |

## First question

> For July 2026, show each registered customer account's Active Lines EOM from Subscriptions and Recognized Service Revenue and Contribution Margin from Finance, in cents. Use the reviewed shared customer identity and preserve one account-month row.

Visible business clarifications in **Exact submitted business question** apply equally to both arms:

- Active lines means the month-end snapshot, not activations or a sum of monthly snapshots.
- Revenue means recognized service revenue, not billed charges or booked merchandise.
- A measured zero is distinct from missing, unmapped, or unauthorized data.

## Follow-up month variation

Changing July to August in live mode generates a fresh plan and source queries rather than replaying a stored answer. In the recorded library (`recordings/northstar-demo-library-v2-2026-10-28.json`), both July and August captures returned 24 registered accounts in each arm with four source-query receipts per arm and 144 source-matched measure cells per month. Selecting a story in recorded mode switches saved captures without calling Microsoft Fabric.

### Concrete July walkthrough

| Example row | Active Lines EOM | Recognized Service Revenue | Contribution Margin |
|---|---:|---:|---:|
| Account 3 / Harbor Business | 3 lines | 80,000 cents | 58,000 cents |

Accounts 1 and 2 share the display name **Northstar Business** while holding distinct `account_key` values and financial figures. Point out that both arms keep those accounts separate because customer identity is `account_key`, never `display_name`.

## Claims to avoid

| Do not say | Say instead |
|---|---|
| "Semantic models alone cannot answer cross-domain questions." | "An agent can coordinate semantic models directly; the ontology gives agents one shared definition of cross-domain meaning." |
| "The ontology calculates a better revenue number." | "Finance still calculates revenue; the metric links to the source measure." |
| "This starter proves an accuracy or speed gain." | "This starter demonstrates measure reuse and shared vocabulary; multi-hop evaluation belongs to the service assurance scorecard." |
| "Ontology rules are enforced as security policies." | "Rules are plain-language guidance for agents; security is enforced in semantic models and OneLake." |
| "Missing values are zero." | "Only a measured zero is zero; unknown and unmapped rows are reported separately." |

## From metric reuse to a review candidate

In the October 28 July capture, Account 1 has `0` month-end active lines, `9,000` cents recognized service revenue, and `-1,400` cents contribution margin. The viewer highlights that row as an application-owned **review candidate** for human investigation — not as a new DAX measure, rule execution, billing defect proof, or automated customer action. For the standalone answer captured directly from the ontology agent in Fabric, use [executive-demo-script.md](executive-demo-script.md).

## Limitations

See [limitations.md](limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
