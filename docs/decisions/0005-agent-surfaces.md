# 0005. Lead with product agent surfaces; keep the web app as an evidence viewer

**Status:** Accepted, 16 November 2026

## Context

Earlier baseline material relied on the ontology agent in Microsoft Fabric alongside a custom React web application (`apps/web`) to compare recorded outputs. Leading a customer demonstration with a custom web interface obscures the core architectural goal: one [Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) (current experience, in preview, checked 16 November 2026) serving multiple first-party Microsoft and GitHub agent experiences.

At the same time, several agent connectors are in preview and carry specific capacity, identity, licensing, or binding constraints documented on Microsoft Learn as of 16 November 2026 ([platform status](../architecture/platform-status.md)).

## Decision

- Lead demonstrations and setup guides with four product agent surfaces operating over the same service assurance ontology (`D6` in [`PLAN.md`](../../PLAN.md)):
  1. **The [ontology agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-agent) in Microsoft Fabric** (in preview, checked 16 November 2026) for Plan-then-Act ontology authoring and interactive questions in Fabric.
  2. **A [Microsoft Foundry](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/fabric-iq) agent** using the Fabric IQ tool (`fabric_iq_preview`, in preview, checked 16 November 2026) pointed at the item's [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) (in preview, checked 16 November 2026) with on-behalf-of user identity, optionally enriched through [Foundry IQ](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/what-is-foundry-iq) and the [Fabric ontology knowledge source](https://learn.microsoft.com/en-us/azure/search/agentic-knowledge-source-how-to-fabric-ontology) (in preview, checked 16 November 2026) for synthetic SLA terms and incident runbooks.
  3. **A [Microsoft Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-fabric-iq-ontology) agent** connected to the Fabric IQ Ontology MCP server (`mcp_FabricIQOntology`, in preview, checked 16 November 2026) through Agent 365 and published to **Microsoft 365 Copilot** and Microsoft Teams. Because the built-in [Fabric IQ connection in Microsoft 365 Copilot](https://learn.microsoft.com/en-us/fabric/iq/connectors/microsoft-365-copilot-overview) covers Power BI content rather than ontology items, a published Copilot Studio agent is the primary path into Microsoft 365 Copilot and Teams.
  4. **GitHub Copilot in Visual Studio Code** connected to the ontology MCP endpoint and the generally available [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) (checked 16 November 2026) so developers inspect entity paths, home measures, and tests while editing models and code.
- Treat [Fabric data agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-create-data-agent) with a semantic-model-bound ontology (in preview, checked 16 November 2026; see also [ontology sources](https://learn.microsoft.com/en-us/fabric/data-science/data-agent-ontology-sources) and [Microsoft 365 Copilot consumption](https://learn.microsoft.com/en-us/fabric/data-science/data-agent-microsoft-365-copilot)) as unverified until tested in a demo tenant under check `G5` ([platform status](../architecture/platform-status.md#capabilities)), because current Microsoft Learn pages disagree on semantic-model binding support.
- Position `apps/web` strictly as an offline evidence viewer and scorecard application ([0004. Three-arm evaluation and evidence-gated claims](0004-evaluation-and-claims.md)). It replays sanitized recordings and scorecards locally and never authenticates to or queries Microsoft Fabric.

## Consequences

- Customers see Microsoft Fabric, Microsoft Foundry, Microsoft Copilot Studio, Microsoft 365 Copilot, and GitHub Copilot sharing one governed ontology under Microsoft Entra ID delegated user identity (`G5`, `G6`), while `apps/web` gives reviewers a fast, zero-tenant way to inspect recordings and scorecards.
- MCP client code discovers tools dynamically through `tools/list` and pins reviewed schema snapshots rather than hard-coding preview tool names or legacy version headers.
- Live deployment guides must check preview prerequisites upfront in the demo tenant: paid F2 or P1 capacity or higher, interactive user sign-in (service principals are not supported on the ontology MCP endpoint in preview), and Agent 365, `McpServers.FabricIQOntology.All`, and Microsoft 365 Copilot licensing for Copilot Studio.
