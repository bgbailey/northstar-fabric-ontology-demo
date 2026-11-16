# Validation backlog

This backlog summarizes the 24 validation questions tracked in `eval/validation/register.json`.
Fabric IQ ontology (current experience) and several connected agent surfaces are in preview (checked 16 November 2026; see [Microsoft Learn](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) and [../architecture/platform-status.md](../architecture/platform-status.md)).

Statuses below come directly from `eval/validation/register.json` and change only when live evidence from a demo tenant is recorded. Each evaluation arm is one tested configuration that receives the same questions, semantic models, and permissions.

Regenerate this file with `python tools/build_validation_backlog.py`.

## Experiments

| ID | Question | Status | Check or work item |
|---|---|---|---|
| E01 | Can the demo tenant create and read back a Fabric IQ ontology (current experience) item with the expected TMDL definition? | inconclusive | G1, M5-02, M7-02 |
| E02 | Can one ontology in Fabric connect multiple semantic models, align shared entities on verified keys, and keep incompatible keys separate? | not yet run | G1, G2, M7-02 |
| E03 | Does each ontology metric link to the right semantic model, home table, and source DAX measure without merging same-name measures? | not yet run | G1, M7-02 |
| E04 | Can a demo user read metric descriptions and query results with Read and Build access while Write-only DAX expressions and item definitions stay restricted? | not yet run | G6, M7-02 |
| E05 | Does the ontology MCP endpoint list read-only discovery and query tools that return entity types, relationships, metrics, and rules? | not yet run | G4, M3-01, M7-02 |
| E06 | Can the client discover semantic models, inspect schemas, search values, and run per-model DAX queries through the Fabric IQ MCP server? | not yet run | M3-01, M4-05 |
| E07 | Does delegated sign-in enforce row-level and object-level security for each demo user across ontology context and semantic-model queries? | not yet run | G6, M7-02 |
| E08 | Does editing a metric description or local formula on a test ontology leave the semantic model's DAX measure unchanged? | not yet run | G1, M7-02 |
| E09 | How do data refreshes, DAX measure edits, and schema changes propagate through semantic models, ontology metadata, and client caches? | not yet run | M5-04, M7-02 |
| E10 | Which parts of an ontology definition survive export and import through the REST definition API, and which metrics linked to source measures must be authored in Fabric? | not yet run | M5-02, M5-04 |
| E11 | Does changing a natural-language rule in the ontology change how an agent plans a query without prompt edits or unauthorized actions? | not yet run | G2, M4-05 |
| E12 | Do entity types and metrics that share a display name across domains stay distinct unless an explicit relationship aligns their keys? | not yet run | G1, G2 |
| E13 | How do entity-type inheritance and shared properties behave in the demo tenant when base and child entity types are queried? | not yet run | M7-01, M7-02 |
| E14 | Does restoring a saved ontology version recover entity types, relationships, metrics, and rules without altering semantic-model data? | not yet run | M5-04 |
| E15 | Can a Fabric data agent answer multi-model questions from an ontology bound to semantic models in the demo tenant? | not yet run | G5, M3-05, M7-02 |
| E16 | Do Direct Lake semantic models over OneLake Delta tables and shortcuts return identical measure results for the synthetic dataset? | not yet run | M5-01, M5-05 |
| E17 | Can spine entity types bound to lakehouse Delta tables use the ontology-managed graph while metric-bearing entity types remain bound to semantic models? | not yet run | G7, M6-01, M7-02 |
| E18 | Do the MCP clients detect row caps, preserve null values and exact decimals, and report truncated or incomplete responses as partial results? | not yet run | G3, M3-01, M4-02 |
| E19 | Are cross-model joins over validity periods and non-additive measures computed without revenue fan-out or misattributed monthly credits? | not yet run | G2, M4-03, M4-05 |
| E20 | Do concurrent requests reuse sessions safely within a user scope, isolate caches between demo users, and respect retry-after headers? | not yet run | G3, G6 |
| E21 | On repeated runs with equal semantic-model preparation, how does the ontology-grounded evaluation arm compare with semantic models alone and with rules in the prompt? | not yet run | M4-01, M4-05 |
| E22 | Can one ontology bind to Direct Lake semantic models hosted in separate domain workspaces while preserving lineage and permissions? | not yet run | M5-05, M7-02 |
| E23 | Does source-controlled semantic-model promotion detect measure drift across models before ontology refresh? | not yet run | M5-04 |
| E24 | Does long-running asynchronous task polling, reconnection, and cancellation report failed or cancelled runs accurately? | not yet run | M3-05, M4-02 |
