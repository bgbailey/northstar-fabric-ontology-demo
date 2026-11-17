# 0003. Semantic-model binding by default, with a hybrid graph spike

**Status:** Accepted, 16 November 2026

## Context

[Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) (current experience, in preview, checked 16 November 2026) allows entity types to bind either to Power BI Direct Lake semantic models ([generate from semantic models](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-generate-from-semantic-models)) or directly to [OneLake](https://learn.microsoft.com/en-us/fabric/onelake/onelake-overview) Delta tables in lakehouses and mirrored databases.

How entity types are bound determines both measure governance and runtime traversal ([binding strategies](../architecture/README.md#binding-strategies)):

- **Strategy A (semantic-model binding):** Every entity type is generated from a semantic-model table and metrics link to source DAX measures. Microsoft Learn documents that entity types bound to semantic models are not projected into the optional [ontology-managed graph](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-graph) (in preview, checked 16 November 2026).
- **Strategy B (hybrid binding):** Spine entity types (`Site`, `Incident`, `Service-site assignment`, `Contract`, `Account`) bind with entity keys to the OneLake Delta tables that the Direct Lake models already read, while metric-bearing entity types stay bound to the semantic models.
- **Strategy C (lakehouse-only binding):** Every entity type binds to Delta tables and calculations move out of Power BI semantic models.

## Decision

- Adopt **Strategy A (semantic-model binding)** as the default binding strategy (`D5` in [`PLAN.md`](../../PLAN.md)) for the [service assurance scenario](../architecture/hero-scenario.md) so that [semantic models keep every calculation](0001-semantic-models-own-calculations.md) and security role untouched.
- State openly in architecture and presenter guidance that Strategy A does not project entity types into the ontology-managed graph. At run time, the agent reads relationships and rules from the ontology, plans the multi-hop path, and runs DAX queries on each home semantic model.
- Schedule **Strategy B (hybrid Delta-table spine plus semantic-model metrics)** as a bounded live spike (`M6-01`, recorded under check `G7` in [platform status](../architecture/platform-status.md#confirmed-in-a-demo-tenant)) in a separate ontology copy in a demo tenant.
- Reject Strategy C for this accelerator because operators with governed Power BI semantic models should not rebuild trusted SLA and finance measures in a second calculation layer.

## Consequences

- Existing Direct Lake semantic models, DAX measures, and row-level security roles work as-is with one copy of data in OneLake.
- Entity keys and cross-model relationship bindings must be set and reviewed by a person after generating from semantic models, backed by an offline conformance check against the ontology contract.
- Because Microsoft Learn notes known issues when connecting a [Fabric data agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-create-data-agent) or [operations agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-create-operations-agent) (both in preview, checked 16 November 2026) to semantic-model-bound ontologies, the primary narrative relies on agent surfaces connected to the ontology MCP endpoint ([0005. Lead with product agent surfaces](0005-agent-surfaces.md)) until verified live.
- Spike `M6-01` (`G7`) will determine with measured demo tenant evidence whether hybrid graph traversal combines cleanly with semantic-model metrics, updating this record before any graph chapter enters the demo.
