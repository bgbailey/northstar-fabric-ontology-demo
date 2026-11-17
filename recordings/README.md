# Sanitized recordings

This folder holds read-only display recordings captured on 28 October 2026 from synthetic datasets in a demo tenant. Fabric IQ ontology (current experience) is in preview (checked 16 November 2026; see [Microsoft Learn](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) and [platform status](../docs/architecture/platform-status.md)).

## Sanitization statement

Public files in this folder are sanitized display recordings. Tenant, workspace, and item identifiers, operator history, tokens, and raw private captures are omitted. Provenance hashes identify privately retained originals and do not authorize live operations. Synthetic business values, Power BI semantic-model measure ownership, original observation timestamps, partial results with explicit exclusions, and historical failed runs are preserved.

The JSON files in this folder are marked `-text` in `.gitattributes` so that Git preserves the exact bytes referenced by their hashes.

## Files

| File | Stream | Capture date | What it shows | What it does not prove |
|---|---|---|---|---|
| `northstar-demo-library-v2-2026-10-28.json` | Quickstart and Customer360 | 28 October 2026 | Combines the two-model Quickstart answer from the ontology agent in Fabric with the ten Customer360 side-by-side comparisons in one playback file for `--recorded-library`. | Switching streams displays saved results only. It runs no sign-in, model call, or Fabric query, and it does not prove live demo tenant readiness, row-level security, or general speed or accuracy gains. |
| `northstar-executive-native-context-2026-10-28.json` | Quickstart | 28 October 2026 | Exported Fabric IQ ontology (current experience) definition and metadata over `Subscriptions` and `Finance`: four entity types, three relationships, fourteen metrics linked to source measures, and two natural-language rules. | This is a definition snapshot, not a materialized instance graph, a query result, or a demo user permission grant. Recorded rules do not prove that an agent evaluated or enforced them. |
| `northstar-executive-native-demo-2026-10-28.json` | Quickstart | 28 October 2026 | One July 2026 answer from the ontology agent in Fabric for `Active Lines EOM`, `Recognized Service Revenue`, and `Contribution Margin`, paired with independent Power BI semantic-model verification queries over the 24 synthetic accounts. | The Fabric service did not expose its internal DAX trace, model deployment, token counts, or rule evaluation. The ontology answer and verification queries ran at separate timestamps, so matching numbers do not prove a shared live transaction window, a billing defect, or multi-hop performance. |
| `northstar-recorded-live-v6-2026-10-28.json` | Customer360 | 28 October 2026 | Ten side-by-side comparison entries over the eight-model Customer360 ontology (59 entity types, 105 relationships, 64 metrics linked to source measures, and four rules) for `--recorded-comparisons`, including partial results that list excluded unmapped rows and preserved earlier failed attempts. | Each comparison pairs two evaluation arms (an evaluation arm is one tested configuration given the same question and semantic-model preparation). Playback calls no live service and does not establish statistical accuracy superiority, an exclusive ability to join models, causal root cause, or live demo user access. |

## Play back locally

Run the combined viewer without signing in or calling a live service (see [../docs/run/streams.md](../docs/run/streams.md) for all playback modes):

```bash
northstar serve --recorded-library recordings/northstar-demo-library-v2-2026-10-28.json --port 8767
```
