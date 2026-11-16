# Limitations

This page summarizes what the offline repository and sanitized recordings in `recordings/` demonstrate today, and what requires live verification in a demo tenant. Platform capabilities and preview constraints checked against Microsoft Learn on 16 November 2026 are tracked in [architecture/platform-status.md](architecture/platform-status.md); security boundaries are documented in [security.md](security.md).

## Offline repository and templates

- **Synthetic data only:** All customer names, accounts, contracts, sites, incidents, care cases, and financial amounts are fictional. Cloning this repository or running automated tests makes no cloud calls, incurs no model spend, and provisions no Microsoft Fabric workspace or item.
- **Undeployed semantic model templates:** The eight Direct Lake semantic model definitions under `fabric/semantic-models/` use `<WORKSPACE_ID>` and `<LAKEHOUSE_ID>` placeholders and an `East Region` sample role with no assigned members. Offline SQL reference checks and Tabular Object Model metadata checks do not execute live DAX in the Fabric engine, validate Direct Lake framing against OneLake, or test row-level or object-level security enforcement.
- **Unapplied AI preparation drafts:** Files under `fabric/ai-prep/` are review drafts derived from source contracts. They do not prove that AI instructions, schemas, or verified answers have been applied in Power BI or automatically inherited by downstream agents.

## Sanitized recordings

- **Display-only playback:** Selecting a stream or story in recorded mode replays saved files from `recordings/`. It never signs in, runs a query, or calls a language model, and recorded mode never falls back silently to live execution or local fixtures.
- **Redacted environment provenance:** Recordings are sanitized projections of synthetic demo tenant runs with workspace, item, and account identifiers removed. Provenance SHA-256 digests identify privately retained capture receipts rather than the sanitized public JSON files, and recordings never grant demo user access.
- **Separate observation clocks:** Answer capture timestamps, independent source-query verification timestamps, ontology definition capture timestamps, and archive export timestamps are distinct clocks. Matching numeric values confirm measure reuse on synthetic data, not a shared transactional snapshot window.
- **Unexposed service internals:** For answers captured from the ontology agent in Fabric, independent DAX queries verify the returned numbers against their home semantic models, but internal agent prompts, model versions, token counts, automatic rule consideration, and semantic-model AI instruction propagation are not exposed by the service.

## Partial results and exclusions

- **Qualified cohorts, not population totals:** Recorded cross-domain stories for service assurance, incident recovery, and checkout release return a partial result (excluding unmapped service-to-site observations, null-key sessions, or contracts with missing credit rows) rather than population-wide totals or complete credit rankings.
- **Unknown is not zero:** Missing mappings, absent credit postings, denied rows, and unmonitored periods remain unknown and are reported as explicit exclusions rather than converted to zero.
- **Separate source grains:** Contract-month SLA credit settlement and account-month finance measures remain in separate grain-specific views so multi-contract accounts never multiply recognized service revenue or contribution margin.
- **Recommendations only:** Review candidates highlight synthetic accounts for human investigation; they do not diagnose billing root causes, prove causal campaign lift, or trigger customer outreach or credits.

## Evaluation and preview platform constraints

- **Fair evaluation arms:** Each evaluation arm (one side of a controlled comparison) receives the same semantic models, the same model-level AI preparation, and equivalent business facts. A capable agent using semantic models alone can also join cross-domain results; this repository claims no accuracy, latency, or token advantage unless demonstrated by a published scorecard.
- **Preview boundaries (checked 16 November 2026):** Fabric IQ ontology (current experience), the ontology agent in Fabric, the ontology MCP endpoint, and Foundry and Copilot Studio ontology integrations are in preview. Import-mode semantic models are not supported for ontology binding, metrics linked to source measures do not author cleanly through item definition round-trip APIs, service principals are not supported on the ontology MCP endpoint, and entity types bound to semantic models are not projected into the ontology-managed graph. Confirm current behaviour in your demo tenant against [architecture/platform-status.md](architecture/platform-status.md) before presenting live.
