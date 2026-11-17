# Platform status

Several parts of this architecture are in preview. This page records what Microsoft documents for each capability, what that means for the demo, and what has been confirmed in a demo tenant. Recheck it before every customer session.

**Documentation checked:** 16 November 2026. Where this page and Microsoft Learn differ, Microsoft Learn is right; update this page.

## Capabilities

| # | Capability | Status | What Microsoft documents | What it means for this demo | Source |
|---|---|---|---|---|---|
| 1 | Fabric IQ ontology | Preview | The current ontology experience is in preview. The earlier experience retires on 31 January 2027 | Use the Fabric IQ ontology (current experience) only | [Overview](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) |
| 2 | Generating an ontology from semantic models | Preview | Generation creates entity types, properties with their bindings, relationship types, and metrics based on the model's DAX measures. Entity keys, time-series bindings and relationship bindings are set by hand. A second semantic model is added through the ontology agent | The four-model ontology is authored with the ontology agent (Plan, review, then Act). Keys and the relationships between models are reviewed by a person. The ontology contract and the conformance check exist for this reason | [Generate from semantic models](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-generate-from-semantic-models) |
| 3 | Storage modes for binding | Preview | Binding is not supported for Import-mode semantic models, or for Direct Lake models whose lakehouse sits in a workspace with inbound public access disabled | The Northstar models are Direct Lake. An estate that is mostly Import mode needs a migration step first; say so early | [Troubleshooting](https://learn.microsoft.com/en-us/fabric/iq/ontology/resources-troubleshooting) |
| 4 | Metrics | Preview | "Ontology metrics proxy measures instead of copying them." Generating and querying need Read and Build on the model; seeing the DAX needs Write | Calculations stay in the semantic models. This is the core claim of the demo and it is how the product works | [Metrics](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics) |
| 5 | Rules | Preview | A rule is a definition in natural language. The ontology does not run it against data and does not take actions | Rules guide agents. Enforcement stays in security roles and in human approval | [Rules](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-rules) |
| 6 | Ontology-managed graph | Preview | The graph is optional. Only entity types bound to Delta tables in lakehouses or mirrored databases, with an entity type key, can be projected. "Entities with data bound to a semantic model aren't supported." | With semantic-model binding there is no graph traversal: the agent plans the path and runs DAX on each model. Graph traversal needs the hybrid binding described in the architecture article | [Ontology graph](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-graph) |
| 7 | Ontology agent | Preview | Plan mode is the default and changes nothing; Act mode applies changes. By default DAX returns up to 200 rows and KQL, SQL and GQL up to 1,000 | Ask for ranked, aggregated answers. Keep authoring in Plan until a person has reviewed the draft | [Ontology agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-agent) |
| 8 | Ontology MCP endpoint | Preview | Each ontology item has its own MCP endpoint under `api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/<workspace-ID>/items/<ontology-item-ID>/ontologyEndpoint`. It needs a paid F2 or P1 capacity or higher. Service principals cannot be used because of a known issue. The page does not list tool names | Sign in interactively as a demo user. Discover tools with `tools/list` at run time and record what is returned; do not hard-code tool names | [Ontology MCP server](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) |
| 9 | Fabric IQ MCP server | Generally available | Covers Power BI reports and semantic models. "It doesn't currently support Fabric ontologies or data agents." Tools: `DiscoverArtifacts`, `ResolveFabricItem`, `GetReportMetadata`, `GetSemanticModelSchema`, `ValueSearch`, `ExecuteQuery`. The current public toolset is version 2 and is the default | This is the "semantic models only" arm. Client code that pins the version 1 toolset must be updated | [Fabric IQ MCP](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) |
| 10 | Fabric data agent with an ontology source | Preview | The ontology how-to states that, because of a known issue, a "data agent doesn't work with an ontology that uses semantic models for binding". The data agent article describes ontology sources backed by semantic models and notes that some ontology context might not be used when queries are generated | The two pages do not agree. Treat this path as unverified until it is tested in the demo tenant, and do not make it the main path | [Create a data agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-create-data-agent), [Ontology sources](https://learn.microsoft.com/en-us/fabric/data-science/data-agent-ontology-sources) |
| 11 | Operations agent with an ontology | Preview | The same known issue is stated for ontologies that use semantic models for binding | Stretch only, and only with entity types bound to lakehouse or eventhouse data | [Create an operations agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-create-operations-agent) |
| 12 | Microsoft Foundry agent | Preview | The Fabric IQ tool (`fabric_iq_preview`) accepts the ontology endpoint as its server URL and runs with the signed-in user's identity. It replaces the earlier Fabric data agent tool | Main path for a pro-code agent over the ontology | [Fabric IQ tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/fabric-iq) |
| 13 | Microsoft Copilot Studio agent | Preview | The Fabric IQ Ontology MCP server (`mcp_FabricIQOntology`) is available through Agent 365 with the tools `list_ontology_entity_types` and `search_ontology`. It needs the `McpServers.FabricIQOntology.All` scope, a Microsoft 365 Copilot licence and an F2 or P1 capacity or higher | Main path for a low-code agent, and the route into Teams and Microsoft 365 Copilot | [Fabric IQ Ontology MCP for Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-fabric-iq-ontology) |
| 14 | Microsoft 365 Copilot | See source | The built-in Fabric IQ connection in Microsoft 365 Copilot works with Power BI content | Ontology context reaches Microsoft 365 Copilot and Teams through a published agent, not through the built-in connection | [Microsoft 365 Copilot and Fabric IQ](https://learn.microsoft.com/en-us/fabric/iq/connectors/microsoft-365-copilot-overview) |
| 15 | Ontology definition, Git integration and deployment pipelines | Preview | The definition is a set of TMDL parts and supports Git integration and deployment pipelines. Only explicit metrics round-trip through the definition APIs; metrics that proxy a semantic-model measure cannot be authored through the definition today | Author metrics in Fabric. Export the definition for review and comparison. Do not write an exported definition back over a working ontology | [Ontology definition](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/ontology-definition) |
| 16 | Skills for Fabric | Open source | Release v0.3.21 was current, with 0.3.22 in the changelog on the main branch. Ontology authoring in the skills covers the earlier definition format only; for the current experience the skills support questions, not authoring | Use the skills for semantic-model, lakehouse, real-time and lifecycle work. Author the ontology with the ontology agent | [Skills for Fabric](https://github.com/microsoft/skills-for-fabric) |
| 17 | Roadmap | Public roadmap | General availability and a public query API are listed for Fabric IQ ontology. Roadmap dates change | Do not quote dates to customers; point to the roadmap | [Fabric roadmap](https://roadmap.fabric.microsoft.com/?product=iq) |

## Repository scope and boundaries

This table summarizes what the Northstar repository demonstrates today versus what remains out of scope. An evaluation arm is one tested configuration given the same question, semantic models, and model-level AI preparation.

| Surface | Demonstrated | Not established |
|---|---|---|
| Source measure reuse | Original semantic-model measures, home-table links, and independent numerical checks | Ontology ownership of replacement calculations |
| Quickstart ontology item in Fabric | Four source-backed entity types, three relationships, 14 retained metrics linked to source measures, and two rules | A filtered wide graph being equivalent to a dedicated small ontology item |
| Executive answer in Fabric | One synthetic account candidate from the ontology agent in Fabric with source-checked values and home tables | Internal agent model or DAX trace, shared observation window, or general accuracy advantage |
| Evaluation arm comparison | Two evaluation arms (semantic models with flat guidance, and ontology-grounded) using the same model, AI preparation, business facts, and validators | An intentionally weakened baseline or an unverified superiority claim |
| Service assurance and deep-dive stories | Historical source execution preserving grains, unknowns, exclusions, and partial results (with unmapped observations and null-key rows excluded) | Population-wide totals, complete credit rankings, or causal release effects |
| Recorded viewer | Explicit playback from `recordings/` with no live sign-in, source query, or model call | Current demo user identity, source freshness, authorization, or new inference |
| Offline fixtures | Local typed plan execution and independent DuckDB reference arithmetic | Live DAX engine execution, ontology service calls in Fabric, row-level security, or cloud deployment |

The generally available Fabric IQ MCP server and the item-scoped ontology MCP endpoint (preview, checked 16 November 2026) are separate surfaces. Each Fabric IQ MCP query targets one semantic model; the calling agent coordinates cross-model steps. Plain-language rules in the ontology provide business guidance, not executable policy enforcement. Ontology-managed graph projection is optional (preview) and is not supported for entity types bound to semantic models.

Sanitized recordings in `recordings/` and local acceptance fixtures in `tests/fixtures/acceptance/` have environment identifiers removed. Their provenance hashes identify privately retained originals rather than the sanitized public files, and they never act as demo user grants or live demo tenant proof. See [../limitations.md](../limitations.md) for full repository and recording limits.

## Confirmed in a demo tenant

Record each check from [hero-scenario.md](hero-scenario.md#checks-before-presenting-live) here. Leave a row empty until it has been run.

| Check | Result | Date | Capacity and region | Notes |
|---|---|---|---|---|
| G1 Four-model ontology | | | | |
| G2 Relationships between models | | | | |
| G3 Interactive answer | | | | |
| G4 MCP tools | | | | |
| G5 Agent surfaces | | | | |
| G6 Security | | | | |
| G7 Hybrid binding (optional) | | | | |

Do not record tenant, workspace or item identifiers on this page.

## Keeping this page current

1. Open each source link and compare it with the row.
2. Change the row, the status and the "Documentation checked" date.
3. If a change affects the architecture, update the architecture article and the diagrams in the same pull request.
4. After a Microsoft event with Fabric announcements, review every row.
