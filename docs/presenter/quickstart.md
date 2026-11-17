# Executive quickstart script: why ontology, without a giant model

**Target:** a small [Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) (current experience, preview checked 16 November 2026) over the Subscriptions and Finance Power BI semantic models in Microsoft Fabric: four entity types, three focus measures, and two short rules. It sits alongside the wider Northstar estate rather than replacing or trimming it.

**Recorded October 28 evidence:** the dedicated ontology item in Fabric, four-entity / three-relationship structure, two linked rules, and answer from the ontology agent in Fabric are captured in `recordings/northstar-demo-library-v2-2026-10-28.json`. Generation created 14 metrics linked to source measures on the selected tables; the presentation highlights three focus measures while retaining all 14 bindings.

## Two streams, different conversations

| Stream | Audience and purpose | Scope in Fabric |
|---|---|---|
| Executive quickstart | Explain why an ontology sits above semantic models using one compact question | Dedicated small item: Customers, Subscriptions, and Finance across two semantic models |
| Service assurance and Customer360 deep dive | Show multi-hop service assurance (SLA credit exposure and account priority), incident recovery, and checkout questions | Four-model service assurance scenario ([../architecture/hero-scenario.md](../architecture/hero-scenario.md)) and eight-model Customer360 item (59 entity types, 105 relationships, 64 metrics linked to source measures, and four rules) |

Both streams keep calculations inside the semantic models and give every evaluation arm (one side of a controlled comparison) the same AI preparation.

## Talk track

Lead with the ontology item in Microsoft Fabric when presenting live after rehearsal, and use the recorded viewer (`http://localhost:8767/`) for supporting evidence or fallback. See [../run/streams.md](../run/streams.md) for startup commands.

| Beat | Show | Say |
|---|---|---|
| Existing investment | Subscriptions and Finance semantic models | "We already trust these semantic models. We are not moving the DAX calculations." |
| Shared business vocabulary | Four ontology entity types and the customer bridge | "An account display name is not customer identity, and a month-end snapshot is not a monthly flow. The ontology makes that shared meaning explicit." |
| Three trusted measures | `Active Lines EOM`, `Recognized Service Revenue`, `Contribution Margin` | "Subscriptions owns the line snapshot. Finance owns revenue and margin. These are metrics linked to source measures, not copied formulas." |
| One connected question | Question and source-checked answer from the ontology agent in Fabric | "Which customers have zero month-end lines, positive recognized revenue, and negative margin? That question crosses both models." |
| Review, not automatic action | Source values, home tables, and caveats | "Now we have a specific account to investigate. A person still reviews credits, cost timing, and contracts before taking action." |
| Reusable rules | The two plain-language rules | "Calculations stay in the models while cross-domain identity and period meaning become reusable across agents." |

The historical prompt is preserved verbatim in `fabric/ontology/executive-demo.json` and in the recording disclosure. For a fresh live rehearsal in your demo tenant, ask:

> For July 2026, which registered customer accounts have zero Active Lines EOM, positive Recognized Service Revenue, and negative Contribution Margin? Show account IDs, the three original measure values in their source units, and their source models and home tables.

To show a live follow-up in a fresh conversation:

> For August 2026, which registered customer accounts have zero Active Lines EOM, positive Recognized Service Revenue, and negative Contribution Margin? Show account IDs, the three original measure values in their source units, and their source models and home tables.

Verify any live answer against independent source queries on Subscriptions and Finance, preserving empty sets, unknown values, and separate observation timestamps.

## Recorded answer in Fabric

On October 28, the ontology agent in Fabric returned one July review candidate:

| Customer | Active Lines EOM | Recognized Service Revenue | Contribution Margin |
|---|---:|---:|---:|
| Account 1 / `showcase-v2:acct:000001` | 0 | 9,000 cents | -1,400 cents |

The response named the home tables (`Subscriptions` / `Subscription Month` for `Active Lines EOM`, and `Finance` / `Account Month` for both financial measures) and matched the independent 24-account source reference.

## Fair comparison and hand-off to service assurance

An agent that queries the two semantic models directly can also join on `account_key`; two models share a single conformed key, so the quickstart shows measure reuse and shared vocabulary rather than a hard multi-hop path.

Hand off to the deeper scenario:

> "Two models share one key. Where connected context really earns its keep is service assurance after a network outage, where an agent has to cross Network, Enterprise services, Finance, and Care across time-valid site assignments and separate contract-month and account-month grains."

Switch to **Customer360 deep dive** -> **SLA exposure and account priority (Oct 28 live)** (or walk through [../architecture/hero-scenario.md](../architecture/hero-scenario.md)), keeping partial results (with excluded unmapped observations and null-key rows) and failed evaluation arms visible.

## Limitations

See [../limitations.md](../limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
