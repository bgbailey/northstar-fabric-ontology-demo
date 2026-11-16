# Security Policy

## Reporting a vulnerability

Do not open a public GitHub issue, pull request, or discussion for a security vulnerability.

Report suspected vulnerabilities privately through [GitHub Private Vulnerability Reporting](https://github.com/bgbailey/northstar-fabric-ontology-demo/security/advisories/new).

Include clear steps to reproduce the issue, the affected file paths, and the potential security impact. Never include customer data, credentials, OAuth tokens, certificates, or real demo tenant, workspace, or item identifiers in a report.

## Synthetic data only

All accounts, contracts, incidents, service records, and recordings in this repository are synthetic. Northstar Wireless is a fictional telecommunications operator. No customer data, production credentials, or demo tenant secrets belong in this repository or in security reports.

## Scope

In scope for this repository:

- Python runtime, CLI commands, and local API surfaces under `src/telco_iq/`
- Model Context Protocol (MCP) client validation, schema checks, and live-operation approval guards
- Sanitization checks and repository boundary scripts under `tools/`
- React and TypeScript evidence viewer under `apps/web/`
- GitHub Actions workflow definitions under `.github/workflows/`

Out of scope for this repository:

- Vulnerabilities in Microsoft Fabric, Fabric IQ, Power BI, OneLake, Microsoft Foundry, Copilot Studio, Microsoft 365 Copilot, or Microsoft Entra ID themselves. Report issues in Microsoft products and cloud services directly to the [Microsoft Security Response Center](https://msrc.microsoft.com/).

## Security architecture

Read [docs/security.md](docs/security.md) for how delegated identity, data-plane enforcement, ontology rules, untrusted text handling, secret boundaries, and live operation approvals work in this project.
