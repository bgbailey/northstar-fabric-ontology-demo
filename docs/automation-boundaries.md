# API-first automation boundaries

Use documented APIs/MCP tools first. Use a supported product UI only after
checking the available contracts for that operation. Missing documentation is
not proof that an API experiment failed.

| Operation | Supported route and boundary |
|---|---|
| Create a generation-2 ontology | Published Create Ontology; an empty create defaults to generation 2; verify actual returned generation |
| Bootstrap source-backed metrics | Documented semantic-model generation through the native ontology agent; review Plan before an approved Act |
| Add another source model | Native ontology agent in the documented experience; no guessed private generation endpoint |
| Read the definition | Published getDefinition and actual native metadata; verify identities and metric homes |
| Modify linked Rules | Reviewed supported authoring route; updateDefinition replaces the full definition, so preserve source bindings and verify readback |
| Consume native context | Item-scoped native ontology MCP, distinct from GA Fabric IQ; current tools/list and reviewed schemas govern |
| Query semantic models | GA Fabric IQ's read-only, delegated tools; one model per ExecuteQuery |
| Present recorded evidence | Explicit display-only mode; no implicit cloud calls or live fallback |

Native bootstrap in the synthetic example required the supported ontology UI
because the reviewed public item API did not expose a metric-generation
operation. The resulting definition was independently read back rather than
trusting agent prose about source/proxy counts.

The service normalized some partition annotations during Rule authoring.
A narrow semantic review checked unchanged proxy bodies, source identities,
lineage and relationships. This is not permission to ignore arbitrary changes
or to round-trip the only working ontology through an untested representation.

GA IQ can deliver a preview plus embedded CSV for larger responses. The current
adapter refuses unreviewed alternate shapes; a preview is not a complete result.
Scope queries to the actual business cohort without removing eligible anonymous,
unmapped or no-order journeys from governed denominators.

A native timeout is not proof of missing consent or a service time limit.
Keep original failure outcomes; do not rotate credentials, replay consumed
intents or relax freshness/coverage merely to obtain a displayable answer.

## Evidence gaps / still thin

Public projections omit private operational traces, target identifiers and
operator state. Current tenant rollout and runtime tools must be checked in
the execution environment. Managed-native internal queries, Rule consideration
and automatic source-instruction inheritance remain unexposed.
