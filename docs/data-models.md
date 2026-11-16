# Offline data and semantic model lane

**Status:** bounded P1/P2 source implementation, not a live Fabric demonstration.
Eight independent model templates contain eight explicit measures each. No cloud
calls, paid models, deployment, membership changes, or independent evaluator code
are part of this lane. `standard` and `scale` are rejected pending live miniature
proof; the 52M-row target has not been generated.

## Commands and persistent artifacts

Run from `C:\path\to\repository` with the parent's restored environment:

```powershell
.venv\Scripts\python.exe tools\build_data.py --profile miniature --seed 20260929 --output data\generated\miniature
.venv\Scripts\python.exe tools\build_data.py --profile smoke --seed 20260929 --output data\generated\smoke
.venv\Scripts\python.exe tools\build_models.py --fixture-manifest data\fixtures\miniature\manifest.json
.venv\Scripts\python.exe -m pytest tests\test_data_models.py -q
```

The published small fixture is `data\fixtures\miniature`. Generated model sources
are under `fabric\semantic-models\<model_id>.SemanticModel`; `census.json` records
tables, fixture row counts, columns, relationships, measure homes, UUID5 lineage,
role predicates, storage mode, and validation status. No report or `.pbip` entry
point is fabricated: each model has `definition.pbism`, a full `definition`
directory, `smoke.dax`, and `smoke.link.json`.

| Profile | Accounts | Subscriptions | Account-month | Service months | Sites | Care cases |
|---|---:|---:|---:|---|---:|---:|
| miniature | 10 | 20 | 60 | March-August 2026 | 4 | 8 |
| smoke | 1,000 | 2,400 | 6,000 | March-August 2026 | 20 | 800 |

Enterprise accounts are a subset, not an additional population. The smoke profile
has all subsidiary schemas but deliberately bounded events: site telemetry covers
five days per month, inventory covers three observed days per month, and case
snapshots cover four days per case. These are not complete daily/monthly histories.

## Runtime contract

`data\contracts\catalog.json` is the version `1.0` runtime interface, with exactly
the shared `models`, `tables`, and `metrics` shapes. It includes all column types,
grains, primary keys, home tables, descriptions, allowed dimensions, units,
aggregation classes, and date columns. `identity-and-time.json` records additional
join, quarantine, and aggregation rules. Runtime need not import either builder.

Semantic table names use the catalog's human-readable `displayName`; **semantic
column names retain the exact snake_case source names**. Catalog DAX, TMDL column
declarations, relationships, role predicates, and smoke queries share that naming
contract. For example: `'Subscription Month'[active_eom]`. No per-column display
mapping or evaluator import is required. This deliberately overrides the
upstream human-readable-column naming preference to match the parent compiler.

| Model ID | Model-owned source tables |
|---|---|
| subscriptions | subscription, subscription_month, lifecycle_event |
| finance | account_month, billing_account_map, charge_line, credit |
| digital | session, interaction_event, identity_map |
| care | case, case_activity, case_daily_snapshot |
| network | incident, site_hour, site_day, subscription_site_exposure |
| commerce | order, order_line, store_device_day |
| growth | offer_assignment, offer_outcome, campaign_touch |
| enterprise | contract, service_site_map, service_month, sla_evaluation, applied_credit |

Conformed projections are account, market, calendar_date, site, device, store,
campaign, and service. Each model includes only its own facts and needed
dimensions, with single-direction active many-to-one relationships. Temporal maps
remain separate facts; there are no invented cross-model relationships. Quarantine
is source evidence only and is excluded from every semantic model.

Every metric home includes `month` and `market_id`, plus `account_key` where
applicable. These columns are also included in each relevant metric's
`allowedDimensions`, including all required subscription and finance measures,
so account/month/market-grained two-model joins need no cross-table metric SQL.
Raw approved table projections for temporal join operators use the catalog's
column schemas; projection approval remains runtime policy, not an automatic
permission granted by appearing in the catalog.

Identifiers and money are int64; money remains **cents**, not dollars.
Dates are Arrow `date32`; timestamps are Arrow `timestamp[us, tz=+00:00]`, actual UTC
instants. The explicit zero offset avoids requiring an unavailable IANA timezone
database on Windows; Python readback returns `datetime.timezone.utc`. TMDL dates
and timestamps map to `dateTime`; timestamp metadata records UTC, not local time.

Metric `sql` is trusted offline arithmetic over **one home table**, not an
implementation or execution of DAX. Quote SQL table names (`case`, `order` are
reserved words), parameterize filters, allow only registered dimensions, and alias
the result using the metric ID:

```sql
SELECT market_id, month,
       SUM(disconnect) * 1.0 / NULLIF(SUM(opening_active), 0)
       AS "subscriptions.churn_rate"
FROM "subscription_month"
GROUP BY market_id, month;
```

The parent's provider may alias a scalar as `value` and attach the metric ID to
its result envelope. Do not interpolate caller-provided expressions. Ratios,
distinct counts, and medians must be recomputed from scoped base rows, never
summed or averaged from partial aggregates. SQL reference tests are not live DAX
reconciliation.

`semi_additive` permits aggregation across non-time group dimensions only.
**Require one `dateColumn` value per result**, through a filter or grouping;
the arithmetic SQL itself does not enforce that policy. TMDL snapshot measures
return blank unless their home date column has one value. Open Cases EOD and
Inventory EOD use `snapshot_date`, not `month`, and their active calendar
relationships use that date. Active Accounts EOM is non-additive across account-
overlapping groups and also requires one month. Churn over multiple months is a
pooled line-month rate, not cumulative unique-customer churn.

Retention Lift excludes observational assignments, requires eligible randomized
units, computes treatment retention minus control retention, and returns null
when either arm is absent. `percentage_points` is represented as a fractional
difference (0.1 means 10 percentage points). Regional filtering of either arm
can make this undefined. No observational campaign is represented as causal.

## Inputs for the independent oracle

| Scenario | Source interface and deliberate edge case |
|---|---|
| Q01 | `subscription_month(subscription_key, account_key, market_id, month, opening_active, active_eom, gross_add, disconnect)`. Monthly lifecycle balances reconcile. Accounts 1 and 2 have July network care contacts and opening-cohort disconnects. |
| Q03 | `incident(incident_key, site_key, start, end, market_id, month)`; `service_site_map(service_key, site_key, contract_key, valid_from, valid_to, market_id, month)`; `contract(contract_key, account_key, market_id, credit_cap_cents, credit_per_minute_cents, threshold_minutes, valid_from, valid_to, month)`; `applied_credit(contract_key, amount_cents, month, market_id, account_key, posted_at)`. Raw outages overlap; service 1 moves sites during an incident; contract 2 starts mid-incident. Clip valid intervals and union by service + contract + month; threshold each service, then apply one monetary cap per contract/month. |
| Q08 | Account display names are nonunique: two pairs share a name but have different account IDs/markets. Finance recognized revenue and commerce booked revenue are different business concepts. Case/inventory observations are dated snapshots; domain watermarks differ. |
| Q14 | `account_month(account_key, billing_key, market_id, month, revenue_cents, credit_cents, service_cost_cents, active_lines_eom)` and `billing_account_map(billing_key, account_key, valid_from, valid_to)`. Billing key **2** belongs to account **1** before June 1 and account **2** afterward. Never join by numeric account/billing-key equality. |

All validity intervals are half-open UTC `[valid_from, valid_to)`. Finance mapping
uses service time, not credit posting time. Recognized credits are in service-
month controls; `credit.month` is posting month, with separate `service_month`.
Enterprise applied credits retain their SLA service month and a later timestamp.
`sla_evaluation` is an operational source settlement fact, not hidden evaluator
gold. The independent Q03 oracle must calculate from raw contracts/mappings/
incidents/applied credits, not read settlement values as its expected answer.

The synthetic SLA rule matches the parent's independent Q03 rule:

1. Intersect outage, serving-site mapping, contract validity, and calendar-month
   intervals; discard empty intersections.
2. Union downtime separately for each `(service_key, contract_key, month)`.
   Simultaneous downtime on different services is not unioned together.
3. Compute service credit cents as
   `max(0, union_minutes - threshold_minutes) * credit_per_minute_cents`.
4. Sum service credit cents per contract/month, then apply `credit_cap_cents`
   **once** to that monetary sum. Subtract applied credits for the same
   contract/month to obtain exposure; do not apply another cap or floor afterward.

Both published profiles currently have one service per contract, so this
clarification leaves fixture values unchanged. A separate source unit test covers
multiple services sharing a contract and verifies cap-after-sum behavior.
Operational source helpers remain private to generation; the independent
evaluator must implement its own clipping and union arithmetic.

Valid anonymous sessions retain null `account_key`. Corrupt orphan identities,
duplicate natural IDs, invalid intervals, and overlapping billing-map inputs are
separate `quarantine` rows with reason codes; duplicate display names remain valid.
No source row contains latent, answer, gold, or oracle labels. Assignment treatment,
incidents, and observed outcomes are ordinary business inputs.

The checkout-release motif affects Device 1 in July. Successful checkout is not
identical to order placement, and abandoned sessions do not imply lost orders.
Orders share session IDs; finite per-store/device/month stock limits fulfilment
and is reconciled to observed inventory. Randomized treatment and observational
campaigns are separate assignment populations.

## Reproducibility and bounds

`build_data.build(profile, seed, output: Path, batch_size=1024)` is the programmatic
entry point; the CLI adds no other behavior. Table-scoped/key-scoped deterministic
PRNG streams do not depend on call order or batch size. Logical SHA256 hashes
include schema and ordered canonical rows, excluding wall-clock generation time.
File hashes may depend on Arrow/Parquet versions and row-group size.

Each `<table_id>.parquet` has one manifest entry with `id`, `file`, `rows`, `bytes`,
`file_sha256`, and `content_sha256`. Manifest totals include `rows`,
`generated_bytes`, `generation_seconds`, `content_sha256`, `profile`, `seed`,
`service_months`, and `as_of`. `watermarks` contains eight unequal synthetic
watermarks. `clocks` separates data observation, source-model revision, ontology
revision, and context-cache revision. Undeployed revisions are not fabricated
refresh timestamps.

Parquet writes use bounded batches (1-65,536 rows, default 1,024) and one temporary
file per table. Generators stream event rows and retain only bounded identity/
contract/stock state. An interrupted rebuild removes the old success manifest;
consumers must require a manifest and verify hashes, not read an in-progress
directory. This is a bounded local builder, not a concurrent transactional lake.

## Model guidance and validation

Reference checkout HEAD was verified as
`6c11ad58c25992e5d1435ce7cd80d217d5598a31`, the supplied refreshed baseline.
Guidance read before authoring:

- `C:\dev\skills-for-fabric\skills\semantic-model-authoring\SKILL.md`
- Its `references\modeling-guidelines.md`, `direct-lake-guidelines.md`,
  `tmdl-guidelines.md`, `dax-guidelines.md`, and `pbip.md`.
- The reference-management Fabric workload-routing guidance.

No second network refresh was attempted because this lane prohibits cloud calls.
The installed modeling MCP is remote-only; the user's explicit offline TMDL
exception applies. Stable UUID5 lineage is intentionally authored as required by
the source plan, rather than relying on first-save engine-generated tags.

Models use named `WorkspaceId`/`LakehouseId` placeholders and one shared
`AzureStorage.DataLake` expression. Entity partitions reference it; they do not
use M table partitions. No tenant IDs, credentials, or real role members are
embedded. `East Region` contains actual table filter definitions, but membership
and source-side enforcement are not established by a local fixture.

Targeted tests check exact miniature counts, smoke dimensions, deterministic
content and file hashes, real Arrow types, key/lifecycle/temporal invariants,
source settlement unit cases, all 64 offline SQL measures against elementary
reference arithmetic, allowed-dimension grouping, non-additive examples, inventory
conservation, and model census/source lint.

Offline receipt, 2026-09-29: **77 targeted tests passed**; owned
Python files passed Ruff. Published fixture readback verified 37 tables, 4,372
rows, and 111,669 compressed bytes. Its logical content SHA256 is
`d90203f642d91af26ee6232af7260a6a83b78575fcc19c43bcdc62bbd7de21b8`.
The smoke population was generated and checked in temporary test staging, not
published as another permanent dataset.

## Evidence gaps / still thin

**Live DAX, Direct Lake binding/refresh, source RLS, and TOM/TMDL engine parsing:
NOT_RUN.** Source lint is not an engine parse. These templates are undeployed until
approved target binding, synthetic Delta-table materialization, engine validation,
and live measure/security reconciliation. There is no OLS demonstration, report
UI, actual Fabric IQ discovery, capacity measurement, or native ontology proof.
Current Learn/blog capability claims were not re-fetched in this offline lane.
The parent owns the independent question oracle, runtime policy enforcement,
deployment approvals, and all live receipts.
