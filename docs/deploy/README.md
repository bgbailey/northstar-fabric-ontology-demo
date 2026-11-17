# Deploy the Northstar service-assurance accelerator

M5-01 deploys synthetic Delta tables, one loader notebook, one lakehouse, and the four
service-assurance semantic models into an **existing** Microsoft Fabric workspace. It
preserves the checked-in TMDL and DAX definitions, replacing only the two deployment
placeholders `<WORKSPACE_ID>` and `<LAKEHOUSE_ID>`.

This command never creates, deletes, pauses, resumes, resizes, or reassigns a workspace
or capacity. It has no rollback-delete path.

## Prerequisites

- Python 3.12+ and Azure CLI (`az`).
- An interactive Fabric user with the workspace permissions needed to create a lakehouse,
  notebook, and semantic models and to run jobs.
- An existing workspace assigned to an active paid capacity. The command verifies the
  workspace has a capacity assignment but makes no capacity mutation.
- The tenant settings required for Fabric IQ ontology preview and agents when continuing
  with M5-02; those preview capabilities are not needed for the M5-01 item deployment.
- A demo workspace containing no same-name Northstar items. Same-name collisions fail
  closed rather than adopting existing items.

Use synthetic data only. Do not put a workspace ID, item ID, token, tenant name, user
identity, approval, or deployment journal in this repository.

## Install and inspect the complete offline plan

From a checkout:

```bash
python -m pip install .
northstar deploy --dry-run
```

`--dry-run` also works with no workspace environment variable. It does not authenticate,
run a subprocess, generate data, contact a network, construct a Fabric client, or write
the journal. The printed plan includes:

- the 21-table service-assurance source set, profile, and fixed seed `20260929`;
- the lakehouse and loader notebook;
- the Network, Enterprise services, Finance, and Care semantic models;
- exact placeholder substitutions and TMDL readback checks;
- Delta error-if-exists, schema, key, row, and logical-content checks;
- mutation, journal-recovery, marker, and no-capacity-change safeguards;
- the private journal path.

## Select the live target

Set the exact interactive identity and existing workspace through arguments or
environment variables. CLI arguments take precedence over the environment.

Bash:

```bash
export NORTHSTAR_FABRIC_WORKSPACE="<existing-workspace-UUID-or-exact-name>"
export NORTHSTAR_FABRIC_TENANT_ID="<tenant-UUID>"
export NORTHSTAR_FABRIC_USER="<expected-user-UPN>"
northstar deploy
```

PowerShell:

```powershell
$env:NORTHSTAR_FABRIC_WORKSPACE = "<existing-workspace-UUID-or-exact-name>"
$env:NORTHSTAR_FABRIC_TENANT_ID = "<tenant-UUID>"
$env:NORTHSTAR_FABRIC_USER = "<expected-user-UPN>"
northstar deploy
```

Equivalent explicit arguments:

```text
northstar deploy --workspace <workspace> --tenant <tenant-UUID> --expected-user <UPN>
```

Live mode never launches `az login` or a device-code flow. It first verifies that an
interactive Azure CLI session already has the exact expected tenant and user. If a
subscription is pinned through the optional `NORTHSTAR_FABRIC_SUBSCRIPTION_ID`
environment variable or the library's `subscription_id` option, that exact selected
subscription is required too.

If any identity check fails, the command makes no authentication, network, or
deployment mutation. It prints the exact required context label:

```text
Scout | M5-01 deployment / run <run ID> | Microsoft Fabric | <tenant> | <expected UPN>
```

The operator must first open or identify a compliant, persistently labelled browser
context with that value, sign in there as the expected user in the expected tenant,
verify the actual account, tenant, and any expected subscription with
`az account show`, then rerun the command. The deployer does not use service
principals, switch accounts, or fall back to an ambient identity.

## Private journal and reruns

By default, the versioned journal is under the operating-system local application
state directory, not under the checkout:

- Windows: `%LOCALAPPDATA%\northstar\deploy\`
- Linux/macOS: `$XDG_STATE_HOME/northstar/deploy/` or `~/.local/state/northstar/deploy/`

To choose a path explicitly, set `NORTHSTAR_DEPLOY_JOURNAL` or pass `--journal`. The
path must be outside the repository and OneDrive. The journal records the deployment
tag, tenant and workspace names/IDs, exact model names/IDs, source and artifact digests,
accepted operation/job paths, and readback receipts. It never stores tokens.

Rerun the same command after an interruption. Completed steps are read back but are
not recreated, updated, rerun, or refreshed. An accepted create, notebook job, or
refresh is resumed from its exact recorded receipt. An unknown POST outcome is never
blindly retried; resolve it by rerunning for read-only marker/receipt reconciliation,
or inspect the private journal and the Fabric operation/job explicitly. Same-name
items without this deployment's marker remain collisions.

A definitive HTTP 4xx rejection is recorded with its safe status and error. A rerun
reports that rejection without resubmitting it. Transport failures after dispatch
remain `submitting`/unknown and also cannot be resubmitted blindly.

The deployer writes:

1. one schema-enabled lakehouse;
2. one loader notebook and one `RunNotebook` job;
3. four complete semantic models from the existing TMDL definitions;
4. one Power BI refresh per semantic model through
   `https://api.powerbi.com/v1.0/myorg/groups/<workspace>/datasets/<model>/refreshes`.

Fabric item and notebook operations use a Fabric-audience token and the required
`x-ms-fabric-skill` header. Semantic-model refresh uses a separate Power BI-audience
token, reads the latest ten refreshes before submitting, and polls only the exact
accepted `Location`; the Fabric skill header is not sent to `api.powerbi.com`.

The notebook verifies every embedded Parquet file hash, schema, row count, primary
key, and logical content hash. Missing Delta tables are written with
`errorifexists`; existing tables are checked bidirectionally and are never overwritten
or appended. Semantic-model and notebook definitions are read back and compared to
the submitted complete definitions.

## Library API for CLI integration

The package exports `build_deploy_parser(subparsers)`, `deploy_from_args(args)`,
`DeployOptions`, `build_plan(options)`, and `run_deploy(options)`. All deploy runtime
code is under `src/telco_iq/deploy`; it does not import `infra` or `tools` at runtime.
Packaged wheels contain the generated-data builder and only the four hero TMDL source
trees from `fabric/semantic-models`.

## Limitations

`TODO(verify-live): M5-01` — a person must deploy once to an empty demo workspace by
following this page and record separately that lakehouse creation, Delta load,
four-model creation, complete TMDL readback, and all four refreshes succeeded. No
live tenant, workspace, capacity, item, source DAX, or refresh was executed in the
offline implementation.
