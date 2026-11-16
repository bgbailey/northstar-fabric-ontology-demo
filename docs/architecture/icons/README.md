# Icons used in the architecture diagrams

The diagrams in `docs/architecture/images` use Microsoft product icons so that they read like any other Microsoft architecture diagram. This page records where each icon came from and the terms that apply.

## Terms of use

- Microsoft permits its Fabric and Azure architecture icons to be used in architecture diagrams, training material and documentation. Use them as they appear in the product. Do not crop, flip, rotate or distort an icon, and do not use a Microsoft product icon to represent your own product or service. See the [Microsoft Fabric icons](https://learn.microsoft.com/en-us/fabric/fundamentals/icons) page and the [Azure architecture icons](https://learn.microsoft.com/en-us/azure/architecture/icons/) terms.
- Product names and icons are trademarks of their owners. They are not covered by this repository's code licence.
- The GitHub mark is a GitHub trademark and is used under GitHub's logo policy. The Octicons set it comes from is published by GitHub under the MIT licence.

## Sources

| File | Represents | Source |
|---|---|---|
| `fabric.svg` | Microsoft Fabric | `@fabric-msft/svg-icons` 8.2.0, `Fabric48Color` |
| `onelake.svg` | OneLake | `@fabric-msft/svg-icons` 8.2.0, `OneLake48Color` |
| `powerbi.svg` | Power BI | `@fabric-msft/svg-icons` 8.2.0, `PowerBi48Color` |
| `purview.svg` | Microsoft Purview | `@fabric-msft/svg-icons` 8.2.0, `Purview48Color` |
| `copilot.svg` | Microsoft 365 Copilot | `@fabric-msft/svg-icons` 8.2.0, `Copilot48Color` |
| `rti.svg` | Real-Time Intelligence | `@fabric-msft/svg-icons` 8.2.0, `RealTimeIntelligence48Color` |
| `graph.svg` | Graph in Fabric | `@fabric-msft/svg-icons` 8.2.0, `GraphIntelligence48Color` |
| `ontology.svg` | Ontology item | `@fabric-msft/svg-icons` 8.2.0, `Ontology64Item` |
| `semantic-model.svg` | Semantic model item | `@fabric-msft/svg-icons` 8.2.0, `SemanticModel64Item` |
| `lakehouse.svg` | Lakehouse item | `@fabric-msft/svg-icons` 8.2.0, `Lakehouse64Item` |
| `eventhouse.svg` | Eventhouse item | `@fabric-msft/svg-icons` 8.2.0, `EventHouse64Item` |
| `eventstream.svg` | Eventstream item | `@fabric-msft/svg-icons` 8.2.0, `Eventstream64Item` |
| `kql-database.svg` | KQL database item | `@fabric-msft/svg-icons` 8.2.0, `KqlDatabase64Item` |
| `data-agent.svg` | Fabric data agent item | `@fabric-msft/svg-icons` 8.2.0, `DataAgent64Item` |
| `operations-agent.svg` | Operations agent item | `@fabric-msft/svg-icons` 8.2.0, `OperationsAgent64Item` |
| `real-time-dashboard.svg` | Real-Time Dashboard item | `@fabric-msft/svg-icons` 8.2.0, `RealTimeDashboard64Item` |
| `report.svg` | Power BI report item | `@fabric-msft/svg-icons` 8.2.0, `Report64Item` |
| `notebook.svg` | Notebook item | `@fabric-msft/svg-icons` 8.2.0, `Notebook64Item` |
| `foundry.svg` | Microsoft Foundry | Microsoft's [Azure/MCP](https://github.com/Azure/MCP/blob/main/community/registry/icons/MicrosoftFoundry.svg) repository, `community/registry/icons/MicrosoftFoundry.svg` |
| `copilot-studio.svg` | Microsoft Copilot Studio | Microsoft's documentation repository [MicrosoftDocs/power-platform](https://github.com/MicrosoftDocs/power-platform/blob/main/power-platform/media/hub-page/copilot-studio.svg), `power-platform/media/hub-page/copilot-studio.svg` |
| `entra-id.svg` | Microsoft Entra ID | Microsoft's documentation repository [MicrosoftDocs/entra-docs](https://github.com/MicrosoftDocs/entra-docs/blob/main/docs/identity/media/index/entra-id.svg), `docs/identity/media/index/entra-id.svg` |
| `fabric-iq.svg` | Fabric IQ workload | Microsoft's documentation repository [MicrosoftDocs/fabric-docs](https://github.com/MicrosoftDocs/fabric-docs/blob/main/docs/media/iq-icon.svg), `docs/media/iq-icon.svg`. The Fabric icon package had no Fabric IQ workload icon in 8.2.0 |
| `github.svg` | GitHub | GitHub's Octicons, repository `primer/octicons`, `icons/mark-github-24.svg` |

`@fabric-msft/svg-icons` is Microsoft's icon package for Fabric, published on npm under the MIT licence. Version 8.2.0 was used; 12.0.0 was the latest on 16 November 2026. The package ships each icon as a small JavaScript module; the SVG files here were extracted from those modules without changing the artwork.

The five files taken from GitHub repositories are unchanged copies. Their Git blob identifiers on 16 November 2026 are listed below; `git hash-object <file>` prints the same value while a file is unchanged.

| File | Git blob identifier |
|---|---|
| `fabric-iq.svg` | `d6d15a25ce29c3deb71a4ace930bf54e6fff64b2` |
| `foundry.svg` | `327df20c3e42d0eb5316a1dcff1ef8a93c8f6117` |
| `copilot-studio.svg` | `01d6824bdaa34261038d8d31d89761bc3c956364` |
| `entra-id.svg` | `0ed35fb73bcf41561166bcf9deebfdeb17c7071f` |
| `github.svg` | `81949e7b460a7bbf1cb2431462f6bd947e32f0ce` |

## Updating an icon

1. Take the icon from the official source listed above. Prefer the 64-pixel item icons for Fabric items and the 48-pixel colour icons for workloads and products.
2. Save it here under the same file name. Keep the original `viewBox`; the diagram builder scales icons without distorting them.
3. Regenerate the diagrams (see the [architecture article](../README.md#regenerate-the-diagrams)) and review the result.
