# Skills for Fabric and MCP configuration

This page explains how to install [Skills for Fabric](https://github.com/microsoft/skills-for-fabric) at a pinned release and how to connect Visual Studio Code to the two Model Context Protocol (MCP) endpoints used by this repository.

Platform capabilities, preview boundaries, and tool configuration formats referenced below were checked on 16 November 2026. Review [../architecture/platform-status.md](../architecture/platform-status.md), [../security.md](../security.md), and [../limitations.md](../limitations.md) before connecting to a demo tenant.

## Install Skills for Fabric at `v0.3.21`

Northstar pins [Skills for Fabric](https://github.com/microsoft/skills-for-fabric) to release tag `v0.3.21` (checked 16 November 2026, when `v0.3.21` was the latest GitHub release tag and `0.3.22` appeared in the `main` branch changelog). That release tag is recorded under `upstream.release` in [`../../eval/validation/register.json`](../../eval/validation/register.json) so contributor and coding-agent guidance stays reproducible.

Clone `microsoft/skills-for-fabric` at `v0.3.21` into a separate directory outside this repository so upstream skill files are never mixed into the project tree:

```bash
git clone --branch v0.3.21 --depth 1 https://github.com/microsoft/skills-for-fabric.git ../skills-for-fabric
```

```powershell
git clone --branch v0.3.21 --depth 1 https://github.com/microsoft/skills-for-fabric.git ..\skills-for-fabric
```

If you install individual skills with Agent Package Manager (`apm`) or GitHub Copilot CLI plugins instead of a sibling checkout, verify that the installed bundle matches `v0.3.21` and pass `--only apm` when you want skill instructions without overwriting your MCP configuration.

## Skills used in this repository

The repository uses six skill areas from `microsoft/skills-for-fabric` (`v0.3.21`):

| Area | Skill (`v0.3.21`) | Purpose in Northstar |
|---|---|---|
| Semantic-model authoring and AI readiness | `semantic-model-authoring` | Author Direct Lake TMDL tables, relationships, DAX measures, column and measure descriptions, and [AI preparation](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-prepare-data-ai) (preview, checked 16 November 2026). |
| Lakehouse and Spark | `spark-cli`, `onelake-catalog-cli` | Load OneLake Delta tables, validate table schemas, inspect catalog metadata, and run Spark notebook jobs. |
| Eventhouse and eventstream | `eventhouse-cli`, `eventstream-cli` | Configure optional [Real-Time Intelligence](https://learn.microsoft.com/en-us/fabric/real-time-intelligence/event-streams/overview) telemetry and live incident streams in a demo tenant. |
| Git integration | `git-integration-operations-cli` | Synchronize workspace item definitions through [Microsoft Fabric Git integration](https://learn.microsoft.com/en-us/fabric/cicd/git-integration/intro-to-git-integration). |
| Deployment pipelines | `deployment-pipelines-authoring-cli` | Promote workspace items across demo tenant stages with [deployment pipelines](https://learn.microsoft.com/en-us/fabric/cicd/deployment-pipelines/intro-to-deployment-pipelines). |
| Cost estimation | `e2e-fabric-cost-estimation` | Plan [Microsoft Fabric capacity](https://learn.microsoft.com/en-us/fabric/enterprise/pause-resume) sizing and estimate demo run costs. |

## Why the ontology is authored with the ontology agent in Fabric

Northstar does **not** use Skills for Fabric to create or edit the ontology item:

- **Skills authoring covers the retiring JSON format only:** In `v0.3.21`, ontology authoring in `skills-for-fabric` (`ontology-cli`) targets the earlier ontology JSON format, which retires on 31 January 2027 (see [Fabric IQ ontology overview](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview), preview, checked 16 November 2026). It does not author or upgrade [Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) (current experience, preview, checked 16 November 2026).
- **Authoring uses the ontology agent in Fabric:** The Northstar ontology is generated from the first Direct Lake Power BI semantic model and extended across the remaining models with the [ontology agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-agent) (preview, checked 16 November 2026) in Fabric. Keep the ontology agent in Plan mode while a person reviews entity keys, cross-model relationships, metrics linked to source measures, and natural-language rules, and switch to Act mode only after review.
- **Definition API round-trip limit:** In the [ontology item definition](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/ontology-definition) (preview, checked 16 November 2026), explicit metrics round-trip through REST definition APIs, whereas metrics linked to source measures cannot be authored that way today.
- **Current Fabric IQ MCP toolset:** In `v0.3.21`, the MCP setup note in `skills-for-fabric` still pinned the version 1 routing header (`Fabric.Routing.FabricIQ.V1`). The [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) (generally available, checked 16 November 2026) now uses the version 2 public toolset (`Fabric.Routing.FabricIQ.V2`) by default (`DiscoverArtifacts`, `ResolveFabricItem`, `GetReportMetadata`, `GetSemanticModelSchema`, `ValueSearch`, `ExecuteQuery`). Do not send the version 1 header.

## Connect Visual Studio Code to the MCP endpoints

[`../../.mcp.example.json`](../../.mcp.example.json) defines the two separate remote HTTP MCP servers used during development and evaluation:

1. **`fabricIq`** — the [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) (`https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq`, generally available, checked 16 November 2026) for Power BI reports and semantic models.
2. **`northstarOntology`** — the item-scoped [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) (`https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/<WORKSPACE_ID>/items/<ONTOLOGY_ITEM_ID>/ontologyEndpoint`, preview, checked 16 November 2026) for ontology entity types, relationships, metrics linked to source measures, and rules on an F2 or P1 capacity or higher.

### Local setup

1. Copy `.mcp.example.json` to `.mcp.json` at the repository root (ignored by `.gitignore`), or merge the `mcpServers` entries into your personal configuration at `~/.copilot/mcp-config.json` (or `$COPILOT_HOME/mcp-config.json`):

   ```bash
   cp .mcp.example.json .mcp.json
   ```

   ```powershell
   Copy-Item .mcp.example.json .mcp.json
   ```

2. Replace `<WORKSPACE_ID>` and `<ONTOLOGY_ITEM_ID>` in `.mcp.json` (or `~/.copilot/mcp-config.json`) with the workspace and ontology item identifiers from your demo tenant.
3. Sign in interactively as an approved demo user in your demo tenant and confirm both the signed-in account and tenant before running any query. Service principals are not supported on the preview [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) (checked 16 November 2026).
4. Discover tools at run time with `tools/list` and inspect their schemas rather than hard-coding tool names for the ontology MCP endpoint.

### Why `.mcp.json` with `mcpServers` is used

Current Visual Studio Code documentation ([Add MCP servers in VS Code](https://code.visualstudio.com/docs/agent-customization/mcp-servers) and [MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration), checked 16 November 2026) specifies a workspace-root `.mcp.json` file (or `~/.copilot/mcp-config.json`) with a top-level `"mcpServers"` object, camelCase server identifiers, `"type": "http"`, `"url"`, and `"tools": ["*"]`, without editor-only keys (`inputs`, `envFile`, `dev`, `sandboxEnabled`).

The older `.vscode/mcp.json` file with a top-level `"servers"` key belongs to the deprecated local configuration format. Both `.mcp.json` and `.vscode/mcp.json` are listed in `.gitignore`, while `!.mcp.example.json` keeps the placeholder template tracked so real workspace identifiers, item identifiers, and tokens are never committed. See [../security.md](../security.md) for full credential and approval rules.
