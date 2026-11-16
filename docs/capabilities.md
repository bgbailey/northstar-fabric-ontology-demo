# Capability boundaries

This page summarizes what the Northstar demonstration establishes today versus what remains out of scope. For Microsoft Learn capability statuses checked on 16 November 2026, see [architecture/platform-status.md](architecture/platform-status.md).

| Surface | Demonstrated | Not established |
|---|---|---|
| Source measure reuse | Original semantic-model measures, home-table links, and independent numerical checks | Ontology ownership of replacement calculations |
| Quickstart ontology item in Fabric | Four source-backed entity types, three relationships, 14 retained metrics linked to source measures, and two rules | A filtered wide graph being equivalent to a dedicated small ontology item |
| Executive answer in Fabric | One synthetic account candidate from the ontology agent in Fabric with source-checked values and home tables | Internal agent model/DAX trace, shared observation window, or general accuracy advantage |
| Evaluation arm comparison | Two evaluation arms (semantic models with flat guidance, and ontology-grounded) using the same model, AI preparation, business facts, and validators | An intentionally weakened baseline or an unverified superiority claim |
| Service assurance and deep-dive stories | Historical source execution preserving grains, unknowns, exclusions, and partial results (with unmapped observations and null-key rows excluded) | Population-wide totals, complete credit rankings, or causal release effects |
| Recorded viewer | Explicit playback from `recordings/` with no live sign-in, source query, or model call | Current demo user identity, source freshness, authorization, or new inference |
| Offline fixtures | Local typed plan execution and independent DuckDB reference arithmetic | Live DAX engine execution, ontology service calls in Fabric, row-level security, or cloud deployment |

The generally available Fabric IQ MCP server and the item-scoped ontology MCP endpoint (preview, checked 16 November 2026) are separate surfaces. Each Fabric IQ MCP query targets one semantic model; the calling agent coordinates cross-model steps. Plain-language rules in the ontology provide business guidance, not executable policy enforcement. Ontology-managed graph projection is optional (preview) and is not supported for entity types bound to semantic models.

Sanitized recordings in `recordings/` and local acceptance fixtures in `tests/fixtures/acceptance/` have environment identifiers removed. Their provenance hashes identify privately retained originals rather than the sanitized public files, and they never act as demo user grants or live demo tenant proof.

## Limitations

See [limitations.md](limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
