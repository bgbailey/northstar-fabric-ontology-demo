# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- MIT license, security policy (`SECURITY.md` and `docs/security.md`), Contributor Covenant 3.0 code of conduct, contributing guide, support statement, `CODEOWNERS`, and Dependabot configuration.
- `northstar` console script alongside the preserved `telco-iq` alias under the `northstar-fabric-ontology-demo` Python distribution and `northstar-web` web package.
- Development container definition (`.devcontainer/devcontainer.json`), Copilot cloud agent setup workflow (`.github/workflows/copilot-setup-steps.yml`), and Linux plus Windows CI matrix with dependency audit, diagram, link, wording, and sanitization jobs.

### Changed

- Updated `requirements.lock` with Windows platform markers (`sys_platform == "win32"`) on Windows-only transitive packages so locked installs succeed across Linux, macOS, and Windows.

## [0.1.0] - 2026-11-16

### Added

- Deterministic synthetic telecommunications data generator with seeded profiles, data manifests, and time-valid enterprise contract and service-site relationships.
- Eight Power BI semantic models in Direct Lake TMDL format (Subscriptions, Network, Enterprise services, Finance, Care, Digital, Field, and Usage) owning 64 DAX measures, explicit grains, and sample row-level security roles.
- Two demonstration streams over Microsoft Fabric IQ ontology (current experience, preview checked 16 November 2026): a two-model executive quickstart (Subscriptions and Finance, four entity types, three relationships, 14 metrics linked to source measures, and two rules) and an eight-model Customer360 estate (59 entity types, 105 relationships, 64 metrics linked to source measures, and four rules).
- Guarded Model Context Protocol (MCP) clients keeping the Fabric IQ MCP server and the item-scoped ontology MCP endpoint separate, with runtime tool discovery, local JSON Schema validation, delegated demo user authentication, and explicit human approval gates for live operations.
- Independent offline oracle, evaluation runners for comparing evaluation arms (a tested configuration such as semantic models only versus ontology-grounded), and boundary tests ensuring runtime code never imports evaluation logic.
- Sanitized recordings for offline playback, a React and Vite evidence viewer (`apps/web`), and target architecture documentation with reproducible SVG and PNG diagrams (`docs/architecture/`).
