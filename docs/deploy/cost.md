# Cost controls and safe lifecycle

Northstar is a synthetic demo accelerator, not a capacity-sizing recommendation.
Its cost depends on the active workload, capacity region and billing mode, stored
data, network path, and model-token usage. It has no committed, hard-coded price.

## What consumes capacity and tokens

| Surface | Main drivers | What to measure |
|---|---|---|
| Loader and other Spark jobs | Node count, node size, active session time, refresh concurrency, and job failures or retries | Spark active duration, node vCores, Capacity Units (CUs), and retry count |
| Semantic models | Direct Lake or cache work, scheduled and interactive refreshes, DAX complexity, row cardinality, and concurrency | Refresh duration, query count, query duration, CU consumption, throttling, and model size |
| Ontology | Graph or metadata refresh if a scheduled refresh exists, plus queries and agent operations that reach backing semantic models | Refresh schedule, graph/query calls, downstream DAX and model work, and operation latency |
| Evaluation and agent surfaces | Input and output tokens, reasoning mode, tool-call count, repeated attempts, and concurrent runs | Tokens per provider, model deployment, run count, retry count, exclusions, and per-run ceilings |
| OneLake | Stored Delta, notebook, and file data; duplicates, snapshots, and retained test data | Current GB by item and storage tier, growth, shortcuts, and retention |
| Network | Data leaving its Azure region or cloud, private endpoint processing, and public downloads | Source and destination regions, transferred GB, route, and egress tier |

Fabric capacity compute is shared across active workloads. A longer or repeated
semantic-model refresh, a larger Spark job, and more concurrent DAX queries can
overlap, so measure a representative time-bucketed demand curve rather than
estimating from only the largest individual operation. Token usage for Foundry,
Azure OpenAI, Copilot, or another provider is a separate dimension and is not
replaced by a Fabric CU measurement.

## Review an estimate before deployment

Use the Skills for Fabric
[`e2e-fabric-cost-estimation`](https://github.com/microsoft/skills-for-fabric/tree/main/skills/e2e-fabric-cost-estimation)
workflow. Do not present a price until both of these inputs are explicit:

1. **Region:** read the capacity's current region, SKU, and state from Fabric
   `GET /v1/capacities` or Azure Resource Manager. The approved Northstar rehearsal
   context uses **Central US**, but a later run must verify the current capacity
   region rather than copy that label.
2. **Workload profile:** supply representative Spark node-hours, active job count,
   semantic-model refresh and query volume, ontology refresh schedule, concurrency,
   retained OneLake GB, transfer route and GB, and evaluation or agent token volume.

For that verified region and profile, fetch the current
[Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
results at estimate time. Read the applicable Fabric capacity or Autoscale Spark,
OneLake, and network meters separately; Autoscale for Spark is not the base
capacity meter. Price any capacity reservation term and any consumption meter
using the units returned by the API. Record the source date, region, currency,
meter names, workload profile, and assumptions. No price or dollar value belongs
in this repository because both region and workload are run-specific.

For a first pilot, use measured Capacity Metrics data to validate CU demand before
committing to a reservation. A workload that is only sized from node, DAX, DWU,
or model-size heuristics remains preliminary. Compare storage, capacity,
network, and AI tokens as separate line items.

## Pause only through the approved lease

Pausing a capacity can stop its compute billing, but it also makes assigned
workspaces unavailable. Follow Microsoft guidance for
[pausing and resuming a Fabric capacity](https://learn.microsoft.com/en-us/fabric/enterprise/pause-resume)
and the
[Microsoft Fabric pricing reference](https://azure.microsoft.com/en-us/pricing/details/microsoft-fabric/).
These are the two R-28 references in the plan.

Northstar code does not pause, resume, resize, reassign, or delete a capacity.
Use only the existing approved lease-aware capacity owner, with a fresh explicit
approval for the current task and target. Verify the current subscription,
resource group, capacity, region, and signed-in identity before that owner acts.
The lease must preserve the workspace and its items; bounded expiry and lease-aware
pause are the cost control, never workspace or content deletion.

`northstar teardown` also does not change capacity state. It reads a completed
private deployment journal, confirms the exact workspace display name, and
inventories only the exact item IDs, types, names, and creation markers in that
journal. A dry run performs no authentication or network call.

## Teardown safety reconciliation

The original M5-03 plan proposed tagged item deletion. The standing repository
and owner safety contract is stricter: **never delete a Fabric workspace or its
contents, and never empty workspace contents as a substitute**. That prohibition
applies to tagged Northstar items too. It supersedes the planned mutating
teardown, so the implementation is inventory and refusal only:

- no create, update, emptying, item deletion, or workspace endpoint mutation;
- no `--force` or delete parser option;
- no accepted result or receipt for a destructive operation;
- no capacity pause or resume performed by the command;
- exact journal tag, completed state, workspace identity, item IDs, types, names,
  artifact digests, and creation markers are required;
- an untagged, renamed, retyped, replaced, or name-only match does not qualify.

The command reports `INVENTORY_ONLY_DELETION_REFUSED` and lists manual cost controls
instead. `TODO(reconcile): M5-03` records the conflict between the original plan
and the permanent safety contract for a future plan review. Reconciliation must
not weaken the prohibition.

## Evidence gaps &#47; still&nbsp;thin

No live M5-03 inventory was run for this documentation. A person still needs to
verify the command once with an explicitly approved demo identity and exact private
journal, while confirming that the read-only client makes no mutating request.
Current capacity utilization, OneLake growth, egress, ontology schedule, evaluation
token use, and live regional prices are not known from the offline repository;
collect and date each input before quoting a cost.
