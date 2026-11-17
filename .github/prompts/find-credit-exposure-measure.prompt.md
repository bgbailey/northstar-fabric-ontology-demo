---
name: find-credit-exposure-measure
agent: "agent"
description: "Trace the Credit Exposure ontology metric to its governing Power BI semantic-model measure without copying DAX into the ontology."
---

Find which Power BI semantic-model measure defines `Credit Exposure`, where that measure lives, and how the [Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics) (preview, checked 16 November 2026) links to it.

Follow these steps:

1. Inspect `Credit Exposure` through the `northstarOntology` [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) (preview, checked 16 November 2026) in your demo tenant, or read [`../../fabric/ontology/service-assurance.json`](../../fabric/ontology/service-assurance.json) when working offline. Record its bound entity type, model identifier, home table name, grain, additivity, unit, and linked ontology rules.
2. Inspect the `Enterprise` semantic model and `SLA Evaluation` table through the [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) (`DiscoverArtifacts` and `GetSemanticModelSchema` on `fabricIq`, generally available, checked 16 November 2026), or read [`../../tools/build_models.py`](../../tools/build_models.py) and [`../../fabric/semantic-models/enterprise.SemanticModel/definition/tables/sla_evaluation.tmdl`](../../fabric/semantic-models/enterprise.SemanticModel/definition/tables/sla_evaluation.tmdl) when working offline.
3. Report the full lineage: ontology metric -> entity type -> semantic model -> home table -> governing measure, including unit, allowed dimensions, and measure description.
4. Confirm that the calculation remains inside the Power BI semantic model while the ontology contract stores only the metric link and grain. Never copy DAX expressions into ontology contracts or agent instructions.
