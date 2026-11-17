# Northstar documentation

This index organizes the architecture, scenarios, demo runbooks, deployment guides, evaluation artifacts, and governance notes for the Northstar [Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) (preview, checked 16 November 2026) demonstration and accelerator. For the ten-minute recorded quickstart, start at the [repository README](../README.md).

## Architecture and scenario

| Document | Summary |
|---|---|
| [Unified context layer architecture](architecture/README.md) | Target architecture across OneLake, Power BI semantic models, Fabric IQ ontology, Microsoft Foundry, Microsoft Copilot Studio, Microsoft 365 Copilot, and GitHub Copilot |
| [Service assurance scenario](architecture/hero-scenario.md) | Four-model service assurance question across Network, Enterprise services, Finance, and Care |
| [Platform status and repository scope](architecture/platform-status.md) | Microsoft Learn preview and general availability status (checked 16 November 2026), demonstrated repository scope, and demo tenant readiness checks |
| [Architecture diagram icons](architecture/icons/README.md) | Official icon sources, blob hashes, and trademark terms |
| [Two-model quickstart scenario](scenario/quickstart.md) | Compact Subscriptions and Finance opener showing measure reuse and shared customer identity |

## Run and present

| Document | Summary |
|---|---|
| [Demo streams and server modes](run/streams.md) | Recorded playback modes, local synthetic fixture workbench, and live fallback rules |
| [Live demo user setup](run/live.md) | Delegated Microsoft Entra ID sign-in, private configuration, and live execution safeguards in a demo tenant |
| [Presenter guide](presenter/README.md) | Run of show, two-arm comparison framing, and evidence walkthrough |
| [Service assurance run of show](presenter/service-assurance.md) | Twenty-minute presenter script across Network, Enterprise services, Finance, and Care with recording fallbacks and claims boundaries |
| [Executive quickstart talk track](presenter/quickstart.md) | Six-beat presenter script for the small Subscriptions and Finance ontology in Microsoft Fabric |
| [Customer360 comparison talk track](presenter/customer360.md) | Guided walkthrough of the two-arm comparison over Subscriptions and Finance |

## Deploy and develop

| Document | Summary |
|---|---|
| [Reproduce the quickstart ontology](deploy/quickstart-ontology.md) | Supported Plan and Act authoring steps in Microsoft Fabric and sanitized recording rebuild commands |
| [Automation boundaries](deploy/automation-boundaries.md) | API-first boundaries across REST item endpoints, the ontology agent in Fabric, and MCP servers |
| [Agent tooling and MCP setup](develop/agent-tooling.md) | Pinned Skills for Fabric setup and VS Code MCP configuration for Power BI semantic models and ontology items |

## Evaluation, reference, and governance

| Document | Summary |
|---|---|
| [Validation backlog](evaluation/validation-backlog.md) | Twenty-four live validation questions tracked from `eval/validation/register.json` |
| [Synthetic data and semantic models](reference/data-models.md) | Deterministic Parquet generator, eight Direct Lake TMDL semantic models, and designed edge cases |
| [Public product references](reference/sources.md) | Microsoft Learn and open-source reference links governing this repository |
| [ADR 0001: Semantic models own calculations](decisions/0001-semantic-models-own-calculations.md) | Why DAX formulas, table grains, and row-level security stay in Power BI semantic models |
| [ADR 0002: Four-model service assurance scenario](decisions/0002-four-model-hero.md) | Why four semantic models balance multi-hop value against interactive latency |
| [ADR 0003: Binding strategy](decisions/0003-binding-strategy.md) | Direct Lake semantic-model binding and optional hybrid lakehouse spine binding |
| [ADR 0004: Evaluation and claims](decisions/0004-evaluation-and-claims.md) | Equal semantic-model AI preparation across evaluation arms and evidence-backed claims |
| [ADR 0005: Agent surfaces](decisions/0005-agent-surfaces.md) | Routing across Microsoft Fabric, Microsoft Foundry, Microsoft Copilot Studio, Microsoft 365 Copilot, and GitHub Copilot |
| [Security and governance notes](security.md) | Delegated identity, data-plane enforcement, untrusted text handling, and live operation approvals |
| [Limitations](limitations.md) | What offline templates and sanitized recordings establish versus what requires live verification in a demo tenant |
