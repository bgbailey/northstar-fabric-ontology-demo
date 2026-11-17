# API-first automation boundaries

Use documented REST APIs and MCP tools first. Use a supported product user interface in Microsoft Fabric only after checking the documented public contracts for that operation. Missing documentation is not proof that an API check failed. For step-by-step quickstart ontology reproduction, see [quickstart-ontology.md](quickstart-ontology.md).

| Operation | Supported route and boundary |
|---|---|
| Create a Fabric IQ ontology (current experience) | Published [Create Ontology](https://learn.microsoft.com/en-us/rest/api/fabric/ontology/items/create-ontology) endpoint; an empty create defaults to the current experience (preview, checked 16 November 2026); verify the returned item properties |
| Bootstrap metrics linked to source measures | Documented semantic-model generation through the ontology agent in Fabric; review Plan mode before an approved Act step |
| Add another source model | The ontology agent in Fabric in the documented experience; no guessed private generation endpoint |
| Read the definition | Published `getDefinition` endpoint and ontology MCP metadata in Fabric; verify identities and measure home tables |
| Modify linked rules | Reviewed supported authoring route; `updateDefinition` replaces the full definition, so preserve source bindings and verify readback |
| Consume ontology context | Item-scoped [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) in Fabric (preview, checked 16 November 2026), distinct from the generally available [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp); current `tools/list` and reviewed schemas govern |
| Query semantic models | Fabric IQ MCP server read-only, delegated tools; one Power BI semantic model per `ExecuteQuery` call |
| Present recorded evidence | Explicit display-only mode from `recordings/`; no implicit cloud calls or live fallback |

Bootstrapping the ontology in the Northstar example used the supported ontology experience in Microsoft Fabric because the public item definition API does not author metrics linked to source measures (checked 16 November 2026; see [../architecture/platform-status.md](../architecture/platform-status.md)). The resulting definition was independently read back rather than trusting agent prose about source or metric counts.

During rule authoring, the service normalized partition annotation order. A semantic review confirmed that metric link expressions, source identities, lineage tags, and relationships remained unchanged. Do not overwrite a working ontology with an unverified exported definition.

The Fabric IQ MCP server can return a preview plus embedded CSV for larger responses. The client refuses unreviewed alternate shapes; a preview is not a complete result. Scope queries to the relevant business cohort without removing eligible anonymous, unmapped, or no-order journeys from governed denominators.

A timeout from an ontology call in Fabric is not proof of missing consent or a fixed service time limit. Keep original failure outcomes; do not rotate credentials, replay consumed approvals, or relax freshness and coverage checks merely to obtain a displayable answer.

## Limitations

See [../limitations.md](../limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
