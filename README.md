# Northstar Fabric IQ ontology demo

Northstar is a synthetic telecommunications demonstration and accelerator showing a **Fabric IQ ontology (current experience)** over existing Power BI semantic models as one shared context layer for agents. **Semantic models own the calculations; the ontology supplies connected business context.**

All customer names, accounts, contracts, and business records are fictional. No real customer data, demo tenant identifiers, credentials, or private run logs are included.

For the target architecture, four-model service assurance scenario, and current preview capabilities, see [docs/architecture/README.md](docs/architecture/README.md), [docs/architecture/hero-scenario.md](docs/architecture/hero-scenario.md), and [docs/architecture/platform-status.md](docs/architecture/platform-status.md).

## What this demonstrates

| Stream | Scope in Fabric | Business question |
|---|---|---|
| Executive quickstart | Separate Fabric IQ ontology (current experience) over two semantic models (Subscriptions, Finance), four entity types, three relationships, 14 metrics linked to source measures, and two rules | Which customers have zero month-end active lines, positive recognized service revenue, and negative contribution margin? |
| Service assurance and Customer360 deep dive | Four-model service assurance scenario (Network, Enterprise services, Finance, Care) within the wider eight-model estate (59 entity types, 105 relationships, 64 metrics linked to source measures, and four rules) | Which enterprise customers were served from sites hit by July incidents, what SLA credit exposure sits on their contracts, how much revenue and margin is at stake, and who has escalated cases? |

Each semantic model retains ownership of its governed DAX measures (such as `Active Lines EOM`, `Credit Exposure`, `Recognized Service Revenue`, and `Contribution Margin`), grain, and security roles. An evaluation arm (one side of a controlled comparison) that queries semantic models directly can also coordinate cross-domain joins; the value under test is reusable, reviewable identity, validity-period, grain, and metric context shared across agents rather than copied into every prompt.

For live sessions, lead with the ontology agent in Fabric and use the local web viewer for side-by-side evidence and recorded playback. See the [demo run guide](docs/demo-streams.md), [executive script](docs/executive-demo-script.md), and [quickstart ontology setup](docs/executive-native-setup.md).

## Run the recorded demo

Requires Python 3.12 or later and Node.js 24 or later.

Create and activate a virtual environment:

```bash
python -m venv .venv
# Linux / macOS (bash or zsh):
. .venv/bin/activate
# Windows (PowerShell):
# .\.venv\Scripts\Activate.ps1
```

Install dependencies, build the web viewer, and start recorded playback (works in bash and PowerShell):

```bash
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
npm --prefix apps/web ci --no-audit --no-fund
npm --prefix apps/web run build
python -m telco_iq.cli serve --recorded-library recordings/northstar-demo-library-v2-2026-10-28.json --port 8767
```

Open `http://localhost:8767/`. Switching streams displays saved evidence from `recordings/`; it does not sign in, call a model, or query Microsoft Fabric. On Windows PowerShell, `./tools/start-demo.ps1` is also available as an optional wrapper.

Recordings in `recordings/` are sanitized display projections of synthetic demo tenant runs. Environment identifiers are removed, while business values, source-measure ownership, original observation timestamps, partial result qualifications (with excluded unmapped or null-key rows), and historical failed evaluation arms are preserved.

| Mode | Cross-platform command or requirement |
|---|---|
| Both recorded streams | `python -m telco_iq.cli serve --recorded-library recordings/northstar-demo-library-v2-2026-10-28.json --port 8767` |
| Recorded evaluation arm comparisons | `python -m telco_iq.cli serve --recorded-comparisons recordings/northstar-recorded-live-v6-2026-10-28.json --port 8767` |
| Local synthetic fixture workbench | Generate miniature data below, then run `python -m telco_iq.cli serve --data data/generated/miniature --port 8766` |
| Live demo user comparison | Supply private demo user and demo tenant configuration outside the repository; see [live demo user setup](docs/reader-demo.md) |

To generate local synthetic fixture data:

```bash
python tools/build_data.py --profile miniature --seed 20260929 --output data/generated/miniature
python -m telco_iq.cli serve --data data/generated/miniature --port 8766
```

Local fixture execution runs DuckDB SQL over synthetic Parquet files; it is not a live Microsoft Fabric session, and recorded mode never falls back silently to fixtures or live endpoints.

## Current repository status

| Surface | Status in this repository |
|---|---|
| Synthetic data and semantic models | Deterministic data generator plus eight Direct Lake TMDL semantic model templates with 64 governed measures and sample `East Region` role definitions |
| Quickstart ontology and answer in Fabric | Structural contract (`fabric/ontology/executive-demo.json`), setup guide, and sanitized recording of an answer from the ontology agent in Fabric checked against independent source DAX queries |
| Customer360 and service assurance recordings | Sanitized recording of the eight-model ontology in Fabric plus ten recorded evaluation arm runs, including three multi-domain runs with 335 source-checked metric cells and explicit partial result exclusions |
| Architecture and platform tracking | Target architecture with diagrams ([docs/architecture/README.md](docs/architecture/README.md)), service assurance specification ([docs/architecture/hero-scenario.md](docs/architecture/hero-scenario.md)), and preview capability status ([docs/architecture/platform-status.md](docs/architecture/platform-status.md), checked 16 November 2026) |

## Architecture summary

| Layer | Responsibility |
|---|---|
| Power BI semantic models (Direct Lake) | Governed DAX measures, table grains, and row-level security enforced where queries run |
| Fabric IQ ontology (current experience) | Entity types, cross-model relationships, metrics linked to source measures, and plain-language rules |
| Ontology agent in Fabric and MCP endpoints | Interactive Plan/Act authoring and questions in Fabric, plus item-scoped ontology MCP (preview) separate from the generally available Fabric IQ MCP server for semantic models and reports |
| Evaluation and evidence viewer | Fair comparison across evaluation arms with identical model-level AI preparation, guarded execution, and recorded playback |

`fabric/ai-prep/model-guidance.json` contains review-only AI preparation drafts. Apply model preparation through supported Power BI tooling in your demo tenant; both evaluation arms receive the same model instructions and equivalent business facts.

| Path | Purpose |
|---|---|
| `src/telco_iq` | Guarded runtime, MCP clients, typed plan validation/execution, and display contracts |
| `apps/web` | Evidence, ontology, and comparison viewer |
| `fabric/semantic-models` | Direct Lake semantic model templates (TMDL) and source-owned measure definitions |
| `fabric/ontology` | Ontology contracts and authoring guidance |
| `data/contracts`, `data/fixtures` | Synthetic data contracts and miniature Parquet fixtures |
| `eval` | Independent offline oracle, evaluation runners, and validation register (excluded from runtime packaging) |
| `recordings` | Sanitized recorded demo streams and comparison runs |
| `tests/fixtures/acceptance` | Sanitized local acceptance fixtures used by offline deployment tests |

Run offline checks in bash or PowerShell:

```bash
python -m pytest -q
python -m ruff check src tests tools eval
npm --prefix apps/web test
npm --prefix apps/web run build
```

## Security

Keep all demo tenant identifiers, authentication tokens, certificates, and operation approvals outside the repository and outside cloud-synchronized folders. Live queries, model inference, and item creation require explicit human approval for the exact target, and this project never deletes or empties a Microsoft Fabric workspace. Read [docs/security.md](docs/security.md) for architecture security boundaries and [SECURITY.md](SECURITY.md) to report vulnerabilities.

## Limitations

See [docs/limitations.md](docs/limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.

## Licence and notices

Released under the [MIT License](LICENSE). See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [SUPPORT.md](SUPPORT.md), and [CHANGELOG.md](CHANGELOG.md).

**Sample notice:** This repository is a sample demonstration and accelerator, and is not an official Microsoft product or supported offering.

**Trademarks:** Microsoft, Power BI, Microsoft Fabric, Microsoft Purview, Microsoft Entra, Microsoft Foundry, Copilot Studio, Microsoft 365, Copilot, and GitHub are trademarks of the Microsoft group of companies. Product names and icons in documentation and architecture diagrams remain the property of their respective trademark owners and are used for architectural illustration only.
