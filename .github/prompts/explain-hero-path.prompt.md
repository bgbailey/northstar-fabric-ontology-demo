---
name: explain-hero-path
agent: "agent"
description: "Inspect the Fabric IQ ontology to explain the entity path, cross-model bridges, validity windows, and rules for the service assurance question."
---

Explain how an agent answers the Northstar Wireless service assurance question across the `Network`, `Enterprise`, `Finance`, and `Care` Power BI semantic models before running any data query.

Follow these steps:

1. Check the `northstarOntology` [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) (preview, checked 16 November 2026) in your demo tenant and discover available tools (`list_ontology_entities` or `list_ontology_entity_types`, `list_ontology_rules`, and `ask_ontology` or `search_ontology`). If no live MCP server is connected, read [`../../fabric/ontology/service-assurance.json`](../../fabric/ontology/service-assurance.json) and state clearly that you are reading the local ontology contract offline.
2. Walk through every hop from `Incident` to `Site`, `Service-site assignment`, `Contract`, `SLA evaluation`, `Account`, `Account month`, and `Case` (see [`../../docs/architecture/hero-scenario.md`](../../docs/architecture/hero-scenario.md)). Mark which hops cross semantic models and which hops depend on active validity windows.
3. List each metric required for the answer alongside its home semantic model, home table, unit, and grain.
4. Summarize the natural-language rules returned by the ontology and state which join, timing, or grain mistake each rule prevents.
5. Do not execute DAX queries and do not invent formulas. Cite the discovered MCP tool names or repository files you inspected.
