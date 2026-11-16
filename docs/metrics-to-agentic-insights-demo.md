# Demo overview: trusted metrics become connected business questions

## Two complementary streams

Northstar ships two recorded demonstration streams in `recordings/northstar-demo-library-v2-2026-10-28.json` alongside the target four-model service assurance scenario ([architecture/hero-scenario.md](architecture/hero-scenario.md)):

- **Executive quickstart:** a dedicated small Fabric IQ ontology (current experience, preview checked 16 November 2026) in Fabric with an answer from the ontology agent in Fabric (`Account 1`: `0` active lines, `9,000` cents recognized service revenue, `-1,400` cents contribution margin) verified against independent source queries. See [executive-demo-script.md](executive-demo-script.md).
- **Customer360 and service assurance deep dive:** the wider eight-model ontology in Fabric paired with two-arm comparisons (where each evaluation arm receives identical model-level AI preparation and business facts) and application-owned review cards. See [demo-streams.md](demo-streams.md).

Start recorded playback on port `8767` in bash or PowerShell:

```bash
python -m telco_iq.cli serve --recorded-library recordings/northstar-demo-library-v2-2026-10-28.json --port 8767
```

Switching between streams in recorded mode displays saved captures without running queries or signing in.

## The value statement

**Keep the calculation. Add the business context. Ask a connected question.**

The ontology does not replace DAX formulas. It links metrics to source measures while declaring shared entity types, customer identity, validity periods, and cross-domain relationships in one place that multiple agents can read.

## Five-minute walkthrough

| Beat | Show | Say |
|---|---|---|
| 1. Trusted metrics | Three measure cards (`Active Lines EOM`, `Recognized Service Revenue`, `Contribution Margin`) | "Subscriptions owns `Active Lines EOM`. Finance owns `Recognized Service Revenue` and `Contribution Margin`. We reuse those measures unchanged." |
| 2. Reusable context | Four-entity path and linked rules | "Same customer means `account_key`, not display name. Month-end snapshots are not summed across months. Service revenue is distinct from booked merchandise." |
| 3. Connected review | Account result and cross-domain review card | "When we connect zero active lines with positive revenue and negative margin, Account 1 stands out for analyst review." |
| 4. Honest scope | Original DAX receipts and caveats | "Values come from the semantic models; the review filter is deterministic application logic, not an automated billing action." |
| 5. Service assurance | SLA credit exposure and account priority ([architecture/hero-scenario.md](architecture/hero-scenario.md)) | "After a network outage, connecting incidents, time-valid service-site assignments, contract-month SLA credits, account-month revenue, and care escalations is where shared context matters." |

## Evidence in the October 28 captures

In the two-model July capture, both evaluation arms returned 24 registered accounts using four source DAX queries per arm (144 source-matched cells) with verified common source state. Accounts 1 and 2 share the display name `Northstar Business` with distinct `account_key` values and remain separate in both arms.

| Demonstrated | Not claimed |
|---|---|
| Typed planning over governed metrics and reviewed cross-model relationships | A hard-coded query DAG disguised as live model inference |
| Ontology context in Fabric connecting entity identity, measure links, and grain rules | Proof that every agent automatically enforces plain-language rules |
| Application-owned review candidates over source-owned integers | Causal diagnosis, an accuracy benchmark, or automated customer credits/outreach |

### Rehearsal history retained in the recordings

During October 28 capture rehearsals in the demo tenant, `list_ontology_entities` updated its tool description to advertise resource links while keeping input/output schemas unchanged; schema-hash review verified all 59 entity types and four rules before continuing. An earlier wide-ontology question over all 59 entity types timed out at the 180-second capture limit (motivating the focused four-model service assurance design in [architecture/hero-scenario.md](architecture/hero-scenario.md)), while the dedicated quickstart ontology in Fabric and the three multi-domain comparison runs (SLA exposure, incident recovery, and checkout release, totalling 335 source-checked metric cells with explicit partial result exclusions) completed and are preserved in `recordings/northstar-demo-library-v2-2026-10-28.json` and `recordings/northstar-recorded-live-v6-2026-10-28.json`.

## Limitations

See [limitations.md](limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
