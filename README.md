# Northstar ontology demo

A synthetic telecommunications example of a Microsoft Fabric generation-2
ontology over existing semantic models. **Semantic models own the calculations;
ontology supplies connected business context.**

All customer names, accounts and business records are fictional. No real
customer data, deployment identifiers, credentials, operator configuration or
internal run logs are included.

## What this demonstrates

| Stream | Native scope | Customer question |
|---|---|---|
| Executive quickstart | Two existing models, four entities, three relationships, fourteen retained source proxies and two Rules | Which customers have no month-end lines, recognized service revenue and negative contribution margin? |
| Customer360 deep dive | Eight models, 59 entities, 105 relationships, 64 source proxies and four Rules | How do incident exposure, recovery and checkout questions connect across domains? |

The executive presentation focuses on three existing measures: Active Lines
EOM, Recognized Service Revenue and Contribution Margin. Their source models
remain authoritative. A consumer can also coordinate those models without
ontology; the benefit here is explicit, reusable customer identity, period,
relationships and metric meaning, not an exclusive ability to join data.

For live presentations, lead with the actual ontology experience in Fabric.
Use this application's comparison, evidence viewer and explicit recorded
playback as supporting material. Start with the small executive stream, then
include the Customer360 SLA exposure/account-priority story.

See [the run guide](docs/demo-streams.md),
[executive script](docs/executive-demo-script.md) and
[native setup procedure](docs/executive-native-setup.md).

## Run the recorded demo

Use Python 3.12 or later and Node.js 24 or later. From PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
Push-Location apps\web
npm ci --no-audit --no-fund
npm run build
Pop-Location
.\tools\start-demo.ps1 -RecordedLibrary .copilot\docs\northstar-demo-library-v2-2026-10-28.json -Port 8767
```

Open `http://localhost:8767/`. Switching streams displays saved evidence; it
does not sign in, call a model or query Fabric.

The recordings are **sanitized display projections** of synthetic lab
observations. Source-environment names and internal references are generalized.
Retained provenance hashes identify privately retained originals, not the
sanitized files. Business values, source-measure ownership, original observation
times, PARTIAL qualifications and historical failed arms are preserved.

| Mode | Command or requirement |
|---|---|
| Both recorded streams | `.\tools\start-demo.ps1 -RecordedLibrary .copilot\docs\northstar-demo-library-v2-2026-10-28.json -Port 8767` |
| Full recorded comparisons | `.\tools\start-demo.ps1 -RecordedComparisons .copilot\docs\northstar-recorded-live-v6-2026-10-28.json -Port 8767` |
| Local synthetic execution | Generate data as below, then use `.\tools\start-demo.ps1 -Offline -Port 8766` |
| Live application comparison | Provide your own separately approved private reader/live configuration and acquire current source/native proof; see [reader setup](docs/reader-demo.md) |

For local fixture execution:

```powershell
.\.venv\Scripts\python.exe tools\build_data.py --profile miniature --seed 20260929 --output data\generated\miniature
.\tools\start-demo.ps1 -Offline -Port 8766
```

Fixture execution is not a live Fabric demonstration. Recorded mode never
silently becomes live or falls back to fixtures.

## Architecture

| Layer | Responsibility |
|---|---|
| Semantic models | Original governed measures, source grains and enforced access |
| Native ontology | Entities, relationships, source-backed metric links and natural-language Rules |
| Managed native consumer | Queries using the supported Fabric experience or separately reviewed native MCP surface |
| Comparison application | Equal source AI preparation and business facts, typed planning, governed per-model queries and reviewed operators |
| Recorded viewer | Display-only projections with explicit capture dates and qualifications |

GA Fabric IQ MCP and the item-scoped ontology MCP are separate clients.
GA IQ targets reports and semantic models; it does not consume ontology items.
The application execution graph is not a managed-native internal trace.
The displayed ontology is a definition graph, not a materialized instance graph.

## Source-model preparation and development

`fabric\ai-prep\model-guidance.json` contains review-only preparation drafts.
Apply preparation through supported Power BI tooling in your own environment;
draft files are not proof that any model is configured.
Both application comparison arms receive the same available instructions and
equivalent business facts.

| Path | Purpose |
|---|---|
| `src\telco_iq` | Guarded runtime, MCP clients, typed planning/execution and display contracts |
| `apps\web` | Comparison, ontology and source-evidence viewer |
| `fabric\semantic-models` | Source model templates and source-owned measure definitions |
| `fabric\ontology` | Structural contracts and supported bootstrap guidance |
| `data\contracts`, `data\fixtures` | Synthetic source contracts and small fixtures |
| `eval` | Independent offline evaluation; excluded from runtime packaging |
| `.copilot\docs` | Selected sanitized recorded display packets |
| `.copilot\evidence` | Sanitized local acceptance fixtures used by deployment tests, not live grants |

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests tools eval
Push-Location apps\web
npm test
npm run build
Pop-Location
```

Keep all target identifiers, authentication state and approval receipts outside
the repository and synchronized folders. Never commit tokens, certificates,
private keys or cloud configuration. Public recordings cannot authorize live
execution. Live operations need a fresh exact-target approval and reader proof.
Never delete or empty a Fabric workspace.

## Evidence gaps / still thin

The included executive native answer is one historical source-checked result.
Independent native/source observations retain different times; matching values
do not establish a shared source-state window. Advanced application stories
remain PARTIAL with explicit exclusions.

Managed-native internal queries/model usage, actual Rule consideration and
automatic source-AI inheritance are not exposed by these captures. No general
accuracy, speed, productivity, RLS/OLS, causal or automatic-action claim is made.
Current live readiness must be established in the environment where you run it.
