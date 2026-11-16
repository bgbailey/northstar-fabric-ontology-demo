# Northstar demo run guide

**Keep the trusted calculations. Connect their meaning.**

All customer names and business data in Northstar are synthetic. Public recordings in `recordings/` are sanitized display projections, not deployment configuration or live demo tenant grants.

| Segment | Live primary surface | Supporting recorded view |
|---|---|---|
| Executive quickstart | Small Fabric IQ ontology (current experience) in your approved demo tenant | **Executive quickstart**: four entity types, three focus measures, and one source-checked answer from the ontology agent in Fabric |
| Service assurance and Customer360 deep dive | Service assurance ontology or full Customer360 ontology in Fabric after rehearsal | **Customer360 deep dive** -> **SLA exposure and account priority (Oct 28 live)** |

Include both segments. Open with the small four-entity quickstart rather than the full 59-entity graph, then transition to the service assurance SLA exposure and account-priority story. Incident recovery and checkout release remain available as extra deep-dive stories.

## Run of show

1. Show the existing semantic models and original measure home tables.
2. Show the quickstart ontology's customer bridge, period meaning, and rules in Fabric.
3. Ask the connected July business question. Treat the returned candidate as a review prompt for an analyst, not a billing diagnosis or automated action.
4. Run a month variation (such as August) live only after a fresh rehearsal in your demo tenant, without supplying expected values or a query plan in the prompt.
5. Move to the service assurance SLA exposure segment (`SLA exposure and account priority`). Keep contract-month credit settlement separate from account-month finance measures, with unknown inputs and exclusions reported explicitly.
6. Use the two-arm comparison viewer (where each evaluation arm receives the same model-level AI preparation and business facts) to show source-query receipts. Matching numbers confirm that both arms use the same semantic model calculations.

See [executive-demo-script.md](executive-demo-script.md), [architecture/hero-scenario.md](architecture/hero-scenario.md), and [presenter-guide.md](presenter-guide.md) for talk tracks.

## Recorded playback

Start the recorded demo server from the repository root (works in bash and PowerShell after building `apps/web`):

```bash
python -m telco_iq.cli serve --recorded-library recordings/northstar-demo-library-v2-2026-10-28.json --port 8767
```

On Windows PowerShell, `./tools/start-demo.ps1 -RecordedLibrary recordings/northstar-demo-library-v2-2026-10-28.json -Port 8767` is an optional wrapper.

Open `http://localhost:8767/`. Selecting a stream or story makes no sign-in, model call, or Fabric query. The executive stream displays the October 28 answer from the ontology agent in Fabric alongside independent source-verification queries. The Customer360 stream includes ten historical evaluation arm pairs; the latest SLA exposure, incident recovery, and checkout release pairs ran in both arms and report partial results (excluding unmapped observations and null-key rows) with verified common source state.

The recorded SLA exposure run returns seven contract-month rows and a separate six-account financial priority table, keeping unknown credit inputs distinct from zero. Its execution graph shows local application steps, not an internal trace of the ontology agent in Fabric.

## Live readiness and fallback

Verify recorded playback before every session. Live readiness in a demo tenant is checked separately per item and per demo user: verify signed-in user and tenant identity, target items, permissions, discovered MCP tools, and source freshness (see [live demo user setup](reader-demo.md) and [architecture/platform-status.md](architecture/platform-status.md)).

If a live call times out or fails, keep that outcome honest and switch explicitly to the dated recording rather than silently substituting local fixture data.

## Limitations

See [limitations.md](limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
