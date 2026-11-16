# Getting started: reuse trusted measures across customer domains

For the separate small native executive stream, use
`docs\executive-demo-script.md` and `docs\executive-native-setup.md`.
This script preserves the controlled application comparison over the focused
slice of the full Customer360 item; the two are distinct demos.
Lead with native Fabric using the executive script, then use this comparison
as supporting evidence. `docs\demo-streams.md` owns current routing/readiness.

**Scenario:** three business domains, two semantic models.
**Audience:** a first introduction to generation-2 ontology.
**Purpose:** show reusable business context without moving or duplicating
semantic-model calculations.

This is a focused, queryable slice of the environment's existing native
ontology, not a newly created standalone ontology item. The complete customer
presentation also includes the Customer360 SLA segment; recovery and checkout
are further deep-dive choices.

## The small picture

| Business domain | Semantic model | What it contributes |
|---|---|---|
| Customers | Subscriptions and Finance account dimensions | Reviewed shared customer identity; account names alone are not join keys |
| Subscriptions | Subscriptions | Source-owned **Active Lines EOM** |
| Finance | Finance | Source-owned **Recognized Service Revenue** and **Contribution Margin** |

The native path connects four captured entities:
`subscriptions_SubscriptionMonth -> subscriptions_Accounts ->
finance_Accounts <- finance_AccountMonth`.
These are definition relationships, not a claim of a materialized instance
graph. The native context names the source/home measure and supplies common
business meaning; the application executes the reviewed source queries.

## Presenter run of show

| Beat | Show or do | Suggested words | What the evidence must show |
|---|---|---|---|
| Start with ownership | Select **Getting started**. Identify the two source models and three measure cards | “We already trust these models. Subscriptions owns active lines. Finance owns revenue and margin. We are not rebuilding those calculations.” | Each metric card points to its actual source model, home table and source measure |
| Establish the ordinary approach | In the traditional arm's receipts, open the individual Subscriptions and Finance queries | “Each model can answer its part. An application can also join the results. The extra work is deciding which customer identity, period and revenue definition to carry across them.” | Actual source DAX reuses the same governed measures; do not substitute a deliberately weak baseline |
| Add connected context | Open the focused native graph and the relevant Rule/metric details | “The ontology makes that shared meaning explicit and reusable: the same customer, the same completed month, month-end lines, and recognized service revenue.” | The four captured entities and reviewed account bridge; source-owned metric links; original native capture provenance |
| Ask once across domains | In live mode, run both approaches with the question below. In recorded mode, select its explicitly labelled capture | “For July, put the trusted measures together by customer. I want the business answer, not a new formula.” | A real account-month result, original source-query receipts and current or captured execution provenance |
| Prove reuse, not replacement | Open one financial source query and its source/home card | “That is still the Finance measure. The ontology added context; it did not become a second owner of revenue or margin.” | Existing source-measure reference, no copied cross-model revenue formula |
| Close on the net benefit | Compare the results and expand assumptions/limits | “We kept trusted calculations where they belong and made the cross-domain interpretation a governed, reusable asset. That is the benefit: less repeated semantic assembly, not an exclusive ability to perform joins.” | Report the observed outcome honestly. Matching answers are welcome; a failed arm remains failed |

## First question

> For July 2026, show each registered customer account's Active Lines EOM
> from Subscriptions and Recognized Service Revenue and Contribution Margin
> from Finance, in cents. Use the reviewed shared customer identity and
> preserve one account-month row.

The application preset contains additional safeguards, which remain visible
under **Exact submitted business question**:

- Active lines means the source month-end snapshot, not activations or a sum
  of monthly snapshots.
- Revenue means recognized service revenue, not billed charges or booked
  merchandise.
- A real measured zero is distinct from missing, unmapped or unauthorized
  data. Preserve source-owned nulls and coverage.

These are context requirements, not hidden answers or a scripted query DAG.
Both arms receive the same source AI preparation and equivalent business facts.
In this controlled comparison, the traditional arm receives a flat projection
of those facts. It is not an uninformed “bare model” baseline, and the demo
does not measure the manual effort of assembling that guidance.

## A simple follow-up

Change July to August and ask the same question. In live mode, that must
produce a new plan and source calls; it must not retrieve a stored answer.
Keep the same source-owned measures and account identity.

This follow-up was also executed: both arms returned24registered accounts
with4fresh source-query receipts each, and another144measure cells were
matched to their original source results. Both months are COMPLETE with
common source state; their original capture times remain separate.

If showing a recording, changing the story selector only changes playback.
Do not imply that a recorded month selector has rerun Fabric.

In the latest two-stream library, select **Customer360 deep dive** ->
**Getting started: final Oct 28 source reference** for this comparison.
The library opens on the separate executive native stream, and Customer360
starts with SLA, not this starter. The July and August captures dated
October22are also preserved; none is rewritten as today's execution.

## What to say if both approaches get it right

“Good. The numbers should agree because the same semantic models still own
them. We are not claiming ontology is the only way to join data. The difference
we are demonstrating is where the shared business meaning is represented and
maintained, so another consumer can reuse it without redefining the measures.”

### A concrete July walkthrough

The captured July example returns24registered accounts in each arm,
with4actual source queries per arm. Every displayed measure value was matched
back to its original source result:72cells per arm,144in total.

| Actual example | Active Lines EOM | Recognized Service Revenue | Contribution Margin |
|---|---|---|---|
| Account3 / Harbor Business |3lines |80,000cents |58,000cents |

Point out that accounts1and2both have the display name **Northstar Business**,
but different account IDs and source values. Say:

“A shared display name is not a shared customer identity. The captured account
bridge and source keys make that explicit. We keep both accounts rather than
merging them by name. The same safeguard applies in both arms.”

Do not demonstrate this by deliberately weakening the traditional query.
Use the actual account IDs, source cards and query receipts already shown.

The three takeaways are:

| Benefit | Where to point |
|---|---|
| Preserve existing semantic-model investment | The original source measures and actual query receipts |
| Make shared meaning inspectable | The reviewed customer bridge, period/grain context and revenue meaning |
| Keep context reusable rather than embedded only in one question | The real native entities, metric links and formal Rule objects |

Do not turn these into an invented productivity percentage, query-count
reduction or model-accuracy claim. Source-measure edits and ontology refresh
are not exercised by this script and are not presented as automatic propagation.

## Claims to avoid

| Do not say | Say instead |
|---|---|
| “Individual semantic models cannot answer cross-domain questions.” | “A consumer can orchestrate multiple models; ontology gives that consumer connected business context.” |
| “Ontology calculates a better revenue number.” | “Finance still calculates revenue; we inspect its original measure and source receipt.” |
| “This proves an accuracy or speed improvement.” | “This demonstrates source-measure reuse and inspectable shared semantics. It is not a benchmark.” |
| “Those Rules are automatically enforced by every native agent.” | “These are captured native Rules. This application explicitly supplies reviewed guidance; managed consideration is not established by listing them.” |
| “All missing values are zero.” | “Only an actual source zero is zero; unknown and unauthorized stay distinct.” |

## From metric reuse to an agentic insight

After showing the three source-measure cards, ask:

> Which July customers deserve a financial review when we connect service
> status with recognized revenue and contribution margin?

Use the source-backed review observation shown in each successful result.
In the fresh October28July capture, account1has0month-end lines,9,000cents recognized
revenue and-1,400cents contribution margin. The application marks this
combination as a **review candidate**, not as a new measure or native Rule.
Missing metric values never satisfy the criterion.

Say:

“The Subscriptions model tells us about lines. Finance tells us about revenue
and margin. Neither model on its own owns this combined customer question.
The ontology supplies the identity, metric meaning and period context an agent
can reuse to connect them. Now we have a specific question to investigate,
without copying a trusted formula into the ontology.”

Then ask what evidence is still needed. Review service-month credits, attributed
cost and timing before claiming a billing defect or cause. No outreach,
refund, credit or other transaction is executed.

This is the sense in which ontology unlocks an agentic insight: a connected
business question becomes available through reusable governed context, rather
than independent domain answers. It is not proof that an equally informed
consumer could never build the join. Keep the traditional arm equally prepared.

The displayed review criterion is deterministic application logic over the
returned source values. It is not a new insight authored by the managed native
agent. An earlier October28large-item managed-native question timed out
without an answer; that failure remains historical evidence.
The later separate small-item managed-native question succeeded with correct
source homes and independently matched values. Use the executive script for
that genuine native answer, not this deterministic application card.

## Evidence gaps / still thin

The October28July two-model capture is COMPLETE in both arms with common source state
and144source-matched values. This demonstrates the accepted historical
full-population example, not the earlier miniature warm-up. It does not prove
an accuracy, performance or productivity improvement. Managed-native Rule
consideration and automatic AI-instruction inheritance remain unverified;
this application supplies the reviewed guidance explicitly.
Earlier advanced timeouts are retained; the final October28SLA, recovery and
checkout pairs subsequently succeeded in both arms with335known source-matched
metric values and PARTIAL qualifications. They are actual application
executions using native context, not managed-native internal traces.
Current live readiness remains a separate gate; an archive is not a grant.
