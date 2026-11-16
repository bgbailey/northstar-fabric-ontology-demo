# Public product references

Current Microsoft Learn documentation and verified observations in your demo tenant take precedence over cached examples. Preview capabilities change; check [architecture/platform-status.md](architecture/platform-status.md) (checked 16 November 2026) before presenting live.

| Reference | Relevant contract |
|---|---|
| [Fabric IQ ontology overview](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) | Fabric IQ ontology (current experience, preview); business-context layer over trusted data |
| [Generate from semantic models](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-generate-from-semantic-models) | Generating from Direct Lake semantic models, adding further models through the ontology agent in Fabric, and required permissions (preview) |
| [Use metrics in an ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics) | Metrics linked to source measures in their home semantic models (preview) |
| [Use the ontology agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-agent) | Plan and Act modes in Fabric and query row caps (preview) |
| [Use rules in an ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-rules) | Plain-language business rules linked to entity types; not executed against data (preview) |
| [Use the ontology MCP server](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) | Item-scoped ontology MCP endpoint in Fabric (preview) |
| [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) | Generally available read-only access to Power BI reports and semantic models with delegated user identity |
| [Create Ontology](https://learn.microsoft.com/en-us/rest/api/fabric/ontology/items/create-ontology) | REST item creation and long-running operation polling |
| [Ontology definition](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/ontology-definition) | TMDL definition parts and round-trip notes for metrics linked to source measures (preview) |
| [Update Ontology Definition](https://learn.microsoft.com/en-us/rest/api/fabric/ontology/items/update-ontology-definition) | Full-definition replacement semantics |
| [Prepare your data for AI](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-prepare-data-ai) | Semantic-model AI instructions, AI data schemas, and verified answers (preview) |
| [Use the ontology-managed graph](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-graph) | Optional graph projection and lakehouse Delta table binding requirements (preview) |
| [Skills for Fabric](https://github.com/microsoft/skills-for-fabric) | Open-source semantic-model, Direct Lake, lakehouse, real-time, and lifecycle skills |

Use only the Fabric IQ ontology (current experience); do not apply earlier ontology JSON recipes. Preserve semantic-model AI preparation across evaluation arms and use documented endpoints only.

## Limitations

See [limitations.md](limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
