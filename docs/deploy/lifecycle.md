# Source control and promotion

Use [Microsoft Fabric Git integration](https://learn.microsoft.com/en-us/fabric/cicd/git-integration/intro-to-git-integration) to review and version workspace item definitions. Use a [deployment pipeline](https://learn.microsoft.com/en-us/fabric/cicd/deployment-pipelines/intro-to-deployment-pipelines) to promote supported items between environments. These are complementary controls: a Git commit does not promote an item, and a stage deployment is not a replacement for a reviewed commit.

This page describes the current Northstar pattern. Platform documentation was checked on **17 November 2026**. Git integration, deployment pipelines, and several supported item types include preview behaviour. The supported-item matrix and user interface can change; check Microsoft Learn before each live run.

## Responsibilities

| Need | Use | Boundary |
|---|---|---|
| Keep a reviewable history of item definitions | Workspace-level Git integration | Connect a workspace to one repository, branch, and directory. Git folders mirror workspace folders. A commit or update is a synchronization operation, not a deployment to another environment |
| Promote a reviewed build between environments | Deployment pipeline | Copy supported items only between adjacent stages. Pipeline item pairing and rebinding determine how the target items are related |
| Author metrics linked to semantic-model measures | Ontology agent in Fabric | Current definition APIs can round-trip explicit metrics, but cannot author or round-trip metrics linked to source measures. Keep this as a guided and reviewed step |

Keep one writer for a connected branch. A human author commits from the development workspace and requests review, or automation updates from the authoritative branch. Do not let both paths write independently to the same branch and workspace. For this demo, keep the development workspace Git-connected and treat Test and Production as pipeline-controlled targets unless a separately reviewed design establishes another write path.

## Connect the development workspace to Git

Connecting is a workspace operation. It is not performed per semantic model or ontology item. The examples below are illustrative only; do not put tenant, workspace, repository, item, or credential identifiers in this repository.

### Prerequisites

Confirm all of these before attempting a connection:

- The workspace is assigned to a Fabric capacity.
- The caller is an Admin on the workspace when connecting, disconnecting, or changing the Git connection. Commit and update operations also require write access to every affected item; update can additionally depend on item ownership or build permission on external dependencies.
- The repository, branch, and target directory already exist. The provider connection must have repository read permission, plus write permission when Fabric commits to the branch.
- The required tenant settings allow the chosen provider. GitHub synchronization must be explicitly enabled, and a cross-region Azure DevOps repository can require the cross-geo export setting.
- The branch policy permits the chosen sync direction. Prefer a protected integration branch and review through a pull request rather than granting direct production writes to an unattended identity.

Microsoft Fabric supports Azure DevOps, GitHub, and GitHub Enterprise Git providers. A GitHub connection uses a token-backed configured connection; unattended service-principal operation also uses a configured provider connection. Never store a provider token or client secret in the repository.

### Connection and initial sync

1. Resolve the workspace by listing workspaces and filtering on its `displayName`; do not guess or commit a workspace identifier.
2. In **Workspace settings** > **Git integration**, choose the provider, repository, branch, and an existing directory for the workspace.
3. Review the first-sync choice before accepting it:
   - **Prefer workspace** exports the workspace and overwrites the corresponding branch content.
   - **Prefer remote** imports the branch and overwrites the workspace.
4. Let Fabric initialize the connection. If the initialize response requires `CommitToGit` or `UpdateFromGit`, finish that action explicitly; a connected workspace is not necessarily synchronized.
5. Read Git status until the workspace and remote commit hashes match and there are no remaining changes. A `202 Accepted` response means the operation is still running. Poll the returned operation identifier until it succeeds, then read Git status again.
6. Commit future workspace changes on a short-lived branch, or branch out to a separate workspace, and merge through the normal protected-branch review. Fabric synchronizes the workspace folder structure, including occupied subfolders up to ten levels deep, with the Git directory; plan folder moves deliberately.

The REST lifecycle uses `POST /workspaces/{workspaceId}/git/connect`, `POST .../git/initializeConnection`, `commitToGit`, `updateFromGit`, and `GET .../git/status`. Always read current `workspaceHead` before a commit or update. A stale head, two simultaneous Git operations, or unreviewed conflict policy can overwrite content. For automation, use the Fabric token audience `https://api.fabric.microsoft.com` and poll long-running operations before starting another operation on that workspace.

## Definitions in source control

Fabric represents each supported workspace item in an item directory named from its display name and public item type. The platform-owned `.platform` file carries the item type, display name, and cross-workspace logical identifier. Treat it as service-owned: never reuse or edit a logical identifier to force a relationship between unrelated items.

### Semantic model as PBIP and TMDL

An Enhanced Metadata Power BI semantic model uses the PBIP layout:

```text
network.SemanticModel/
|-- .platform
|-- definition.pbism
`-- definition/
    |-- database.tmdl
    |-- model.tmdl
    |-- expressions.tmdl
    |-- relationships.tmdl
    |-- roles/
    |   `-- East Region.tmdl
    `-- tables/
        |-- Accounts.tmdl
        |-- Incidents.tmdl
        `-- Sites.tmdl
```

`definition.pbism` identifies the model definition format and project settings. Under `definition/`, `database.tmdl` and `model.tmdl` hold the database and model declarations; `expressions.tmdl` holds shared expressions and parameters; `relationships.tmdl` holds model relationships; `roles/` contains security roles; and `tables/` contains one readable TMDL file per table, including columns, partitions, and measures.

For Northstar, expressions and partitions refer to workspace and lakehouse identifiers through replaceable deployment parameters rather than committed tenant identifiers. Keep the semantic model as the home of DAX calculations, row-level security, grain, and model AI preparation. See the repository's [`fabric/semantic-models`](../../fabric/semantic-models) definitions and the [deployment boundaries](automation-boundaries.md).

### Fabric IQ ontology definition

The current Fabric IQ ontology experience uses a TMDL-based definition:

```text
service-assurance.Ontology/
|-- .platform
|-- database.tmdl
|-- model.tmdl
|-- expressions.tmdl
|-- relationships.tmdl
|-- entityRelationships.tmdl
|-- tables/
|   |-- Accounts.tmdl
|   `-- Services.tmdl
|-- metrics/
|   `-- Recognized Service Revenue.tmdl
|-- entities/
|   |-- Account.tmdl
|   `-- service#Service.tmdl
|-- namespaces/
|   |-- default.tmdl
|   `-- service.tmdl
`-- rules/
    `-- TrustedMeasuresAndReviewLimits.tmdl
```

The parts have separate review purposes:

- `.platform`, `database.tmdl`, and `model.tmdl` provide item and model identity.
- `tables/`, `relationships.tmdl`, and `expressions.tmdl` contain backing model structure, table relationships, and shared expressions.
- `metrics/` contains top-level metrics, one TMDL file per metric.
- `entities/` contains entity types and their property bindings. A namespaced file uses `<namespace>#<entity>.tmdl`.
- `entityRelationships.tmdl` contains relationships between ontology entity types.
- `namespaces/` contains namespace declarations; `namespaces/default.tmdl` is required in a read definition.
- `rules/` contains one reviewed natural-language rule per file.

This is the current ontology definition shape, not the earlier entity-type JSON format. The [ontology definition reference](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/ontology-definition) is authoritative if the emitted files differ.

## Metric round-trip limitation

> **Do not overwrite a working ontology with an exported definition to "fix" its metrics.**

An explicit ontology metric can round-trip through the item definition APIs. A metric that proxies a semantic-model source measure cannot currently be authored or round-tripped by using the definition API. It must be created, changed, and reviewed through the ontology agent in Fabric, with a person keeping the agent in Plan mode until the proposed source model, measure, home table, entity type, and grain have been checked. Then apply the reviewed step in Act mode and perform an independent readback.

Git integration and deployment pipelines can carry an ontology item's definition between supported locations, but that alone does **not** prove that metric links to source measures were preserved or recreated. After every import or promotion, compare the source and target ontology definitions and metadata for:

- metric count and names;
- the linked semantic-model identity and measure;
- the home table and measure expression reported by Fabric;
- entity property and relationship bindings;
- natural-language rules and lineage tags.

`updateDefinition` replaces the full item definition. Preserve the working ontology until readback validation passes. If a required binding is missing, return to the supported ontology authoring flow; do not write an exported definition back over the item and assume Git or the pipeline will repair it. This constraint is also tracked in [platform status](../architecture/platform-status.md).

## Review and promotion workflow

1. **Author in Development.** Change the semantic model or ontology, following the same model-preparation and conformance rules used for the rest of Northstar.
2. **Read back.** Obtain definitions and metadata from Fabric rather than trusting an agent summary. Run the ontology conformance check and inspect model relationships, security roles, and metric source bindings.
3. **Commit as a unit.** Include dependent items in the same reviewed change when they must resolve together. Review TMDL, `.platform`, parameter substitutions, and any expected formatting churn.
4. **Merge after human review.** Apply branch protection and required checks. Do not give a source-controlled target two independent authorities.
5. **Promote with the pipeline.** Deploy from Development to Test, validate, and then deploy from Test to Production. A deployment is allowed only to an adjacent stage.
6. **Read back and test the target.** Confirm item identities, pairing, model links, ontology metric links, and source-controlled definitions. Run the relevant smoke and business checks in the target workspace.
7. **Record the operation.** Retain the deployment operation identifier, final state, Git commit, review result, and validation evidence outside any committed tenant configuration.

For a clean initial development workspace, the merge point can be the Git-connected branch and the pipeline's Development stage. Test and Production remain promotion targets. If one workspace is updated from Git and also changed directly, stop and reconcile the conflict before promotion.

## Deployment pipeline pattern

A pipeline has two to ten ordered stages. Assign one workspace to each stage and resolve the pipeline, stage, workspace, and item identifiers at run time by listing resources and filtering on `displayName`. Never guess identifiers from a name or place tenant-specific identifiers in scripts committed to this repository.

### Supported items and semantic model readiness

The [supported-items list](https://learn.microsoft.com/en-us/fabric/cicd/deployment-pipelines/intro-to-deployment-pipelines#supported-items) is the source of truth for the item types a deployment can copy. On 17 November 2026 it included **Semantic Model** and **Ontology**, both preview. The matrix differs from the Git-integration matrix and changes over time. Reconcile both matrices before a run.

Semantic models without Enhanced Metadata are no longer supported for deployment-pipeline promotion. Upgrade the model and verify the readback before using it in a release. Unsupported or skipped item types cannot be assumed to have moved, even when the overall deployment operation succeeds.

### Pairing and automatic rebinding

The first deploy, or a workspace assignment that can identify matching items, creates an item pair between the source and target. Fabric records the association as `sourceItemId` and `targetItemId`; later deployments use it to overwrite the paired target rather than create an unrelated duplicate. Pairing also participates in automatic dependency rebinding, for example a report's semantic-model link or a Direct Lake model's target workspace, lakehouse, and model references.

Pairing is automatic and not settable through a Fabric REST "pair" operation. Read stage items before and after a deploy to observe it. A changed display name does not prove a broken pair, and identical names do not prove a pair. Resolve a pairing issue deliberately; unassigning and reassigning a stage workspace is destructive to deployment history and UI-configured rules and is not a routine repair.

### Selective deployment and target-specific changes

An omitted `items` array requests all supported items in the source stage. An `items` array selects only explicit `{ sourceItemId, itemType }` entries, and each entry is capped as part of the request's 300-item limit. Read the exact `itemType` from the stage-items response.

The stage-items API provides identity, pairing, and last deployment time, not a dependable "changed" flag. If automation needs a changed-item set:

1. List items in both stages and pair by returned item identifiers.
2. Mark new or unpaired items for deployment.
3. For paired items, fetch the two definitions and compare them in a script. Do not hash raw definition payloads and call the result a change.
4. Normalize expected target rebinding before diffing: Direct Lake workspace and lakehouse identifiers, target model links, and other generated environment references.
5. Keep semantic changes, metric source bindings, security roles, and rule contents visible. Do not normalize those away.
6. Include an item when equality cannot be proved, and make the computed selection a reviewable artifact.

A selective deployment cannot propagate a source-side deletion because there is no deleted item identifier to include. Reconcile target-only items separately and use an explicitly reviewed deletion, if required. Do not describe the portal's Compare view, or a self-authored normalized diff, as a public "changed-items" API.

Deployment and parameter rules can be configured in the Fabric UI, but the public deployment-pipeline REST surface does not provide a create or update API for those rules. Capture and review UI rule configuration separately; do not imply that it is reproduced by the deployment request.

### Illustrative API sequence

The following placeholders are resolved at run time:

```http
POST /v1/deploymentPipelines/{resolvedPipelineId}/deploy

{
  "sourceStageId": "{resolvedTestStageId}",
  "targetStageId": "{resolvedProductionStageId}",
  "items": [
    {
      "sourceItemId": "{resolvedSemanticModelId}",
      "itemType": "SemanticModel"
    },
    {
      "sourceItemId": "{resolvedOntologyId}",
      "itemType": "Ontology"
    }
  ],
  "note": "Promote reviewed build to Production"
}
```

Resolve both item identifiers by listing stage items and matching item type and display name. Include all required dependencies; omit `items` only when the reviewed intent is to deploy all supported items. Add `createdWorkspaceDetails` if the target stage has no assigned workspace. This sample performs no tenant operation.

The deploy endpoint can return `202 Accepted`. Capture `x-ms-operation-id` or `Location`, observe `Retry-After`, and poll `/v1/operations/{operationId}` until the terminal state is `Succeeded` or `Failed`. The caller needs pipeline Admin and at least Contributor/Member on both stage workspaces. Delegated clients use `Pipeline.Deploy`; reading pipeline state needs `Pipeline.Read.All` or `Pipeline.ReadWrite.All`, and workspace assignment uses `Pipeline.ReadWrite.All` plus `Workspace.ReadWrite.All`. Service-principal and managed-identity support also depends on tenant settings and the involved item types. Do not start another deploy while the pipeline has an active operation.

## Promotion checklist

### Before the deploy

- [ ] The intended Git commit is reviewed and checks pass.
- [ ] The development workspace and branch have matching heads with no residual Git changes.
- [ ] Every identifier has been resolved by a list-and-name lookup at run time.
- [ ] Semantic models use Enhanced Metadata and source-control definitions are readable TMDL.
- [ ] The source and target stage assignments, item types, and expected pairs are recorded.
- [ ] Direct Lake workspace/lakehouse placeholders and report/model links are ready for target rebinding.
- [ ] Ontology definitions have been read back, and every metric linked to a source measure has been reviewed in the ontology agent.
- [ ] Any selective-deploy omissions are intentional; target-only items and source deletions have separate handling.
- [ ] The caller has the required pipeline, workspace, item, and delegated permissions.
- [ ] The deployment note identifies the reviewed release without embedding tenant identifiers.

### After the deploy

- [ ] The long-running operation reached a terminal state and its result was read.
- [ ] The target stage contains the expected supported items and no unexplained duplicate pairs.
- [ ] Target definitions were fetched independently; expected auto-rebound identifiers were normalized only for comparison.
- [ ] Semantic-model roles, relationships, expressions, partitions, and measures match the reviewed intent.
- [ ] Ontology entities, relationships, namespaces, rules, and explicit metrics match the reviewed intent.
- [ ] Every metric linked to a source measure still points to the intended target model, measure, and home table.
- [ ] Source-model query checks run against the target environment, under the approved identity and permissions.
- [ ] Data loads, refreshes, cache state, and operation timestamps are recorded separately; deployment copies definitions, not data.
- [ ] The operation identifier, result, validation evidence, and any manual deletion or repair are retained for audit.

## Readback validation

For each stage, use the item's `getDefinition` operation and the appropriate metadata endpoint. Compare the target with the reviewed source after expected rebinding has been normalized. A successful HTTP response or a `Succeeded` deployment operation is not sufficient evidence that the target is semantically correct.

At minimum, validate:

1. **Item structure:** expected item paths are present and unrelated items were not changed.
2. **Semantic model integrity:** database, tables, relationships, expressions, roles, measures, and Direct Lake binding targets are intact.
3. **Ontology integrity:** entity types, property bindings, cross-model relationships, metrics, namespaces, and rules match the contract.
4. **Metric links:** explicit metrics read back, and every metric linked to a source measure resolves to the intended semantic model and home table.
5. **Runtime behaviour:** run the smoke query and representative business query in the target environment; compare with the independently validated expected result.

Keep the first two checks independent from runtime answers. A correct answer can be returned from an unintended item or stale source, while a structurally valid deployment can still have a broken metric binding.

## Limitations

The Git connection and pipeline are definition-movement controls, not substitutes for semantic review:

- Git synchronization is workspace level and depends on provider, tenant, workspace, branch, and item permissions.
- Folder preservation and source formats are platform contracts, but unsupported items are ignored during synchronization or deployment.
- Ontology and several related Git/pipeline item types are preview features. Supported item matrices and UI behaviour can move.
- An item definition can contain target-specific object identifiers after automatic rebinding. Raw byte or JSON equality is not evidence of semantic equality.
- There is no public deployment-pipeline REST endpoint that gives a reliable changed-item selection, and there is no public REST API to create or update pipeline rules.
- Selective deploys do not represent deletions. Target-only items require a separate reviewed decision.
- Definition deployment does not copy table data, refresh a model, or prove a working ontology metric link.
- Metrics linked to semantic-model measures currently require supported authoring and readback through the ontology experience in Fabric.

<h2 id="evidence-gaps--still-thin">Evidence&#32;gaps&#32;&#47;&#32;still&#32;thin</h2>

- `TODO(verify-live)`: Connect an approved demo workspace to Git and confirm the first sync, folder layout, branch policy, and readback.
- `TODO(verify-live)`: Promote the reviewed semantic model and ontology through an assigned Development, Test, and Production pipeline, then verify pairing and target bindings.
- This page was prepared without mutating a tenant, creating a Git connection, or invoking a deployment operation.
- The live supported-item matrix and preview UI behaviour can change after the checked date.

## References

- [Overview of Fabric Git integration](https://learn.microsoft.com/en-us/fabric/cicd/git-integration/intro-to-git-integration)
- [Git integration process](https://learn.microsoft.com/en-us/fabric/cicd/git-integration/git-integration-process)
- [Fabric Git source code format](https://learn.microsoft.com/en-us/fabric/cicd/git-integration/source-code-format)
- [Power BI Desktop project semantic model folder](https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-dataset)
- [Overview of Fabric deployment pipelines](https://learn.microsoft.com/en-us/fabric/cicd/deployment-pipelines/intro-to-deployment-pipelines)
- [Automate deployment pipelines with Fabric APIs](https://learn.microsoft.com/en-us/fabric/cicd/deployment-pipelines/pipeline-automation-fabric)
- [Deploy Stage Content REST API](https://learn.microsoft.com/en-us/rest/api/fabric/core/deployment-pipelines/deploy-stage-content)
- [Long-running operations](https://learn.microsoft.com/en-us/rest/api/fabric/articles/long-running-operation)
- [Ontology item definition](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/ontology-definition)
