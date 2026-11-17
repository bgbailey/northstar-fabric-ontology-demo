# Bootstrap the Northstar ontology

M5-02 is a **`[LIVE]` human-guided** bootstrap command. It creates the empty ontology
item and keeps the operator in control of all ontology-agent authoring. It makes no
live tenant call while this implementation is being tested.

Run it only after M5-01 has completed successfully and after saving its private
`northstar-deploy-v1` journal outside the repository and outside synchronized folders.
The journal must contain `completed: true`, the exact workspace and interactive
identity, and verified records for all four semantic models:

- `Northstar Network Service Assurance`
- `Northstar Enterprise Services Service Assurance`
- `Northstar Billing and Finance Service Assurance`
- `Northstar Customer Care Service Assurance`

The command fails on a requested tenant, user, workspace, model name, model ID, or
marker mismatch. It does not infer a target from similarly named items and never
adopts an ontology by display name.

## Current platform boundary

Microsoft Learn's [ontology definition](https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/ontology-definition)
reference defines the current new-experience ontology as a TMDL-based definition.
The smallest create payload contains `.platform` and `database.tmdl`; the service
synthesizes the minimal model and default namespace. The command therefore creates
only an empty, exact `Northstar_Service_Assurance` item in the deployed workspace.

The [semantic-model generation](https://learn.microsoft.com/fabric/iq/ontology/how-to-generate-from-semantic-models)
flow normally starts from **Generate ontology**, but that action would create a
second item after the REST create. The runtime prompt pack does not print that
instruction. Open the existing item and use the ontology agent as described below.

Metrics that proxy source semantic-model measures are links, not copied formulas.
The [metrics page](https://learn.microsoft.com/fabric/iq/ontology/how-to-use-metrics)
documents that the ontology retrieves the source DAX on demand. Projected and
enrichment metric links cannot currently be authored or round-tripped through the
definition API. The command never calls `updateDefinition`, and it never replays an
exported definition over a working item.

## Inspect the offline plan

Install the wheel and pass the completed private deployment journal:

```text
northstar ontology bootstrap --dry-run --deploy-journal <private-deployment-journal>
```

`--dry-run` reads only the local completed journal and packaged contract. It does not
authenticate, construct a client, run a subprocess, write either journal, contact
Fabric, or export a definition. The plan shows the exact deployed model display names
and IDs, minimal definition parts, marker, state phases, private paths, and ordered
prompts.

## `[LIVE]` operator sequence

1. Verify that the four Direct Lake semantic models are refreshed and that the operator
   has Read and Build access to each model. Keep the target capacity assigned and
   active; this command never changes capacity state.
2. Supply the exact expected workspace, tenant UUID, interactive user, optional
   subscription UUID, and completed deployment journal:

   ```text
   northstar ontology bootstrap \
     --workspace <existing-workspace-UUID-or-exact-name> \
     --tenant <tenant-UUID> \
     --expected-user <interactive-user-UPN> \
     --subscription <subscription-UUID> \
     --deploy-journal <private-deployment-journal>
   ```

   The client verifies an already active Azure CLI account. It never starts a sign-in.
   If verification fails, it prints a labelled `Scout | M5-02 ontology bootstrap /
   exact run ... | Microsoft Fabric | <tenant> | <expected UPN>` browser context for
   the operator to use, and then requires a new manual verification run.

3. The command creates one empty Ontology item through `POST`, with the exact
   `Northstar service-assurance; deployment=<deployment-id>; asset=ontology` marker.
   It records the accepted operation before polling and records the verified item ID.
   A timed-out or unknown create outcome blocks another POST.
4. Open the created ontology and start the ontology agent in **Plan mode**. For each
   model, review the printed prompt's exact deployed item name and ID, entity types,
   relationships, metrics, keys, and rules. Correct the draft through the agent
   before applying it.
5. Type the exact ontology item name at the review prompt. Then switch to **Act mode**,
   apply only that reviewed step, and type the exact phrase `apply step N`.
6. Repeat in Network, Enterprise services, Finance, then Care order. The source models
   own all DAX; the operator keeps every proxy metric linked to its source measure and
   manually checks keys, validity intervals, and cross-model relationship bindings.
7. Let the command export the published definition through `getDefinition` and check
   it against the packaged service-assurance contract. A non-conformant export fails;
   the command does not write it back.

Confirmation is deliberately local and exact. EOF, cancellation, or an incorrect
confirmation leaves the state journal paused at that review or apply phase. Rerunning
the command prints the same step and continues without creating another ontology.

## Private state and exports

By default, the bootstrap state is stored beside the deployment journal as
`<deployment-stem>.ontology-bootstrap-v1.json`; the final export is stored as
`<deployment-stem>.ontology-definition-v1.json`. Explicit `--state-journal` and
`--export` paths are also supported. All paths must be outside the repository and
OneDrive, and the journals never contain tokens.

The v1 state journal records:

- the exact source deployment journal and hashes;
- expected tenant, workspace, and four deployed model identities;
- the optional expected subscription and exact run label;
- creation submission, accepted receipt, and verified ontology item ID;
- completed human review/apply steps and the paused phase;
- the private definition-export hash and conformance report.

The contract, prompt renderer, packaged prompt document, and conformance checker are
wheel assets. Runtime code imports only `telco_iq`, not `tools` or `infra`.

## G1 evidence

The existing M3 G1 evidence in [platform status](../architecture/platform-status.md#confirmed-in-a-demo-tenant)
proves a previously guided four-model ontology could be exported and checked. It does
not prove this M5-02 command created, recovered, prompted, exported, and conformed an
ontology in a fresh deployed target.

`TODO(verify-live): G1` — run the new `northstar ontology bootstrap` command once in
an approved demo workspace, retain the create/readback operation evidence and private
export receipt, and record the operator-confirmed conformance result separately.

## Evidence gaps

- No live tenant, workspace, item, ontology agent, export, or metric binding was
  exercised for M5-02.
- Unit fakes verify create-once state, accepted-operation recovery, pause/resume,
  target mismatch, export, and conformance failure behavior; they do not prove the
  Fabric preview UI preserves proxy-measure links.
- The command checks ontology definition structure through conformance. Independent
  operator review of the live source-measure, home-table, key, and relationship
  bindings remains required for G1.
