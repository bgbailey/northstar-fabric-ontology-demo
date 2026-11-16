# Northstar: Fabric IQ ontology demo for telco

Purpose: show a Fabric IQ ontology over existing Power BI semantic models as one context
layer for many agents. Semantic models own the calculations; the ontology adds entity
types, relationships across models, metrics that link to source measures, and rules.

Read first: `AGENTS.md` (rules), `PLAN.md` (work items), `docs/architecture/`.

Layout: `src/telco_iq` runtime and MCP clients · `apps/web` evidence viewer · `fabric/`
semantic models (TMDL), ontology contracts, AI preparation · `eval/` questions, oracle,
runners, scoring · `tools/` generators and checks · `recordings/` sanitized captures ·
`agents/` agent definitions and instructions.

Checks before a pull request: `python -m pytest -q`, `python -m ruff check src tests tools
eval`, `npm test` and `npm run build` in `apps/web`, `python tools/check_sanitization.py`,
`python tools/check_wording.py`.

Style: Python 3.12+, type hints, line length 110, no network in tests. TypeScript strict.
Documentation in plain language; mark preview features with the date checked; link to
Microsoft Learn; no benefit claim without a scorecard link.

Never: commit identifiers or secrets, edit generated TMDL by hand, change a measure
expression, import `eval` from `src`, call a live service without an approval file.
