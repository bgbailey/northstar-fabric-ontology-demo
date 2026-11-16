# Security and Governance Notes

This page explains the security boundaries of the Northstar demonstration and accelerator across Microsoft Fabric, Power BI semantic models, Microsoft Fabric IQ ontology, agent connections, and this repository. To report a security vulnerability privately, follow [SECURITY.md](../SECURITY.md).

Platform capabilities referenced below were checked against Microsoft Learn on 16 November 2026 and are tracked in [architecture/platform-status.md](architecture/platform-status.md). See [architecture/README.md](architecture/README.md) for the end-to-end architecture diagram.

## 1. Delegated identity end to end

Every live query in this architecture runs under the signed-in demo user's [Microsoft Entra ID](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow) identity from the agent surface down to the semantic model:

- **[Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp)** (generally available, checked 16 November 2026): authenticates with delegated Power BI and Microsoft Fabric user scopes to discover semantic models and execute DAX queries as the signed-in demo user.
- **[Ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server)** (preview, checked 16 November 2026): exposes an item-scoped Model Context Protocol (MCP) endpoint per ontology item on paid F2 or P1 capacities or higher. Service principals are not supported in preview due to a known limitation; interactive demo user sign-in is required.
- **[Microsoft Foundry Fabric IQ tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/fabric-iq)** (`fabric_iq_preview`, preview, checked 16 November 2026): connects to the ontology MCP endpoint using on-behalf-of user identity so downstream queries evaluate the caller's permissions.
- **[Copilot Studio Fabric IQ Ontology MCP server](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-fabric-iq-ontology)** (`mcp_FabricIQOntology`, preview, checked 16 November 2026): connects through Agent 365 with the delegated `McpServers.FabricIQOntology.All` scope under the signed-in user's identity.

In the local guarded client, sign-in checks verify both the signed-in demo user's object identifier and the approved demo tenant identifier before issuing any live call. The client never falls back to ambient credentials, service principals, or privileged administrator accounts, and OAuth callback parameters are excluded from HTTP access logs.

## 2. Where security is enforced

Access control is enforced where the data resides and where queries execute, never in the ontology layer and never in the web viewer:

- **Workspace and item permissions:** Microsoft Fabric workspace roles and Power BI semantic-model permissions govern who can view items and run queries. Generating an ontology from a semantic model and querying linked metrics require Read and Build permissions on the source semantic model, and viewing the underlying DAX expression requires Write permission (see [Use metrics in an ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics), preview, checked 16 November 2026).
- **Row-level and object-level security:** Power BI [row-level security](https://learn.microsoft.com/en-us/fabric/security/service-admin-row-level-security) (for example, the sample `East Region` role in the Northstar Direct Lake semantic models) and object-level security filter rows and columns inside the semantic model engine when DAX runs.
- **Storage security:** [OneLake security](https://learn.microsoft.com/en-us/fabric/onelake/security/get-started-security) and lakehouse permissions govern direct access to underlying Delta tables.
- **Offline web viewer:** The React web application in `apps/web` is a local display viewer for sanitized recordings and offline evaluation output. It never signs in, never holds tokens, never calls Microsoft Fabric, and never acts as a security boundary.

## 3. Why ontology rules are not security policies

Rules in [Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-rules) (preview, checked 16 November 2026) are plain-language business definitions attached to entity types and relationships. Agents read these rules as context so they select the right time-validity checks, join keys, and metric grains.

The ontology service does **not** execute rules against data, filter query results, block unauthorized requests, or trigger external actions. Never rely on ontology rules or agent system instructions for access control, tenant isolation, row filtering, or compliance enforcement. Keep security policies in Microsoft Entra ID, Microsoft Fabric workspace permissions, Power BI row-level security roles, and human approval workflows.

## 4. How retrieved text is treated as data

All text retrieved at run time — including ontology entity descriptions, relationship descriptions, natural-language rules, semantic-model metadata, MCP tool descriptions and input schemas, query result rows, and web pages — is treated strictly as untrusted data:

- Retrieved text never grants permission to call unreviewed tools, switch MCP endpoints, widen OAuth scopes, change user identities, modify workspace permissions, or trigger customer-facing actions.
- The Fabric IQ MCP server and the ontology MCP endpoint are kept on separate validated client configurations. Endpoint URLs must match documented HTTPS hosts without embedded credentials, query strings, or fragments.
- Tool schemas discovered via `tools/list` are validated with an empty local JSON Schema registry so `$ref` values cannot trigger external network requests, and live calls check discovered schemas against reviewed tool hashes.

## 5. What must stay outside the repository

This repository contains synthetic data, templates with placeholders, offline tests, and sanitized recordings only. Keep all sensitive or environment-specific material outside the Git working tree and outside cloud-synchronized folders (such as OneDrive):

- Client secrets, OAuth access and refresh tokens, client certificates, private keys, shared-access signatures, and connection strings
- Demo tenant identifiers, demo Azure subscription identifiers, workspace GUIDs, lakehouse GUIDs, semantic-model GUIDs, and ontology item GUIDs
- Raw un-sanitized captures, local session receipts containing environment metadata, and live target configuration files
- Live operation approval files
- Private organization or programme deny-lists pointed to by the `NORTHSTAR_DENYLIST_FILE` environment variable (one term per line, read locally by `tools/check_sanitization.py` and never committed)

The runtime rejects private authentication and live configuration files whose resolved paths sit inside the repository or inside a synchronized OneDrive directory. In continuous integration, `tools/check_sanitization.py` scans tracked files for non-example email addresses, tenant domains, tokens, keys, user-profile paths, and unapproved GUIDs.

## 6. How live operations are approved

Cloning this repository, running `pytest`, or opening the web viewer makes zero live calls to Microsoft Fabric or Microsoft Foundry.

Every live operation against a demo tenant — including live MCP queries, paid model inference, lakehouse or semantic-model deployment, ontology authoring, permission changes, or agent publishing — requires an explicit, unexpired human approval file checked before execution:

- **Scoped to the exact target and artifact:** Approval files bind a target digest, an artifact digest, an explicit list of permitted operations, an expiration timestamp with timezone, and a cost ceiling.
- **No implicit reuse:** Synthetic fixtures, sanitized recordings, offline test passes, and expired or prior approvals never authorize a new live call or another workspace target.
- **Non-destructive workspace and capacity boundaries:** The accelerator never deletes a Microsoft Fabric workspace, never empties workspace contents as a substitute for teardown, and never pauses, resizes, reassigns, or deletes a capacity. Item teardown operates only on individual items explicitly tagged as created by the deployment tool after human confirmation.
