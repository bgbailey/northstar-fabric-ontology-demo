# Synthetic data and semantic model reference

Northstar provides a deterministic synthetic telecommunications data generator and eight Direct Lake Power BI semantic model templates (TMDL) with eight governed measures each (64 measures total). Generating data or semantic model definitions runs offline with no cloud calls, paid model inference, workspace deployment, or role-membership changes.

## Commands and generated artifacts

Activate your Python 3.12+ virtual environment and run from the repository root (works in bash and PowerShell):

```bash
python tools/build_data.py --profile miniature --seed 20260929 --output data/generated/miniature
python tools/build_data.py --profile smoke --seed 20260929 --output data/generated/smoke
python tools/build_models.py --fixture-manifest data/fixtures/miniature/manifest.json
python -m pytest tests/test_data_models.py -q
```

The committed small fixture is `data/fixtures/miniature`. Generated semantic model definitions sit under `fabric/semantic-models/<model_id>.SemanticModel`; `fabric/semantic-models/census.json` records tables, fixture row counts, columns, relationships, measure home tables, UUID5 lineage tags, sample security role predicates, storage mode, and template validation status. Each model folder contains `definition.pbism`, a `definition/` directory, `smoke.dax`, and `smoke.link.json`.

| Profile | Accounts | Subscriptions | Account-month | Service months | Sites | Care cases |
|---|---:|---:|---:|---|---:|---:|
| miniature | 10 | 20 | 60 | March-August 2026 | 4 | 8 |
| smoke | 1,000 | 2,400 | 6,000 | March-August 2026 | 20 | 800 |

Enterprise accounts are a subset of `account`, not a separate table. The `smoke` profile includes all tables with bounded telemetry windows: site telemetry covers five days per month, device inventory covers three observed days per month, and case snapshots cover four days per case.

## Runtime contract

`data/contracts/catalog.json` is the version `1.0` runtime contract containing `models`, `tables`, and `metrics`: column types, grains, primary keys, home tables, descriptions, allowed dimensions, units, aggregation classes, and date columns. `data/contracts/identity-and-time.json` records join, quarantine, and aggregation rules. Runtime code reads `catalog.json` without importing either generator.

Semantic model table names use human-readable `displayName` values, while column names retain exact `snake_case` source column names across DAX expressions, TMDL column declarations, relationships, role predicates, and smoke queries (for example, `'Subscription Month'[active_eom]`).

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

Shared dimension tables (`account`, `market`, `calendar_date`, `site`, `device`, `store`, `campaign`, `service`) appear in the models that reference them, with single-direction active many-to-one relationships. Time-valid mapping tables (`service_site_map`, `billing_account_map`, `identity_map`) remain separate tables with explicit validity intervals rather than static relationships. `quarantine` holds invalid-input rows for data-quality tests and is excluded from every semantic model.

Every metric home table includes `month` and `market_id` (plus `account_key` where applicable), listed in `allowedDimensions`. Identifiers and currency values are 64-bit integers; currency values are stored in **integer cents**, never floating-point dollars. Dates use Arrow `date32`; timestamps use Arrow `timestamp[us, tz=+00:00]` (UTC) and map to `dateTime` in TMDL with a `Timezone = UTC` annotation.

Metric `sql` expressions in `catalog.json` provide offline single-table DuckDB reference arithmetic for local fixtures:

```sql
SELECT market_id, month,
       SUM(disconnect) * 1.0 / NULLIF(SUM(opening_active), 0)
       AS "subscriptions.churn_rate"
FROM "subscription_month"
GROUP BY market_id, month;
```

Ratios, distinct counts, and medians are recomputed from scoped base rows, never summed or averaged from partial aggregates. Semi-additive snapshot measures (`Active Lines EOM`, `Opening Active Lines`, `Average Active Lines`, `Contracted Services`, `Open Cases EOD`, `Inventory EOD`) require a single date value (`HASONEVALUE` in DAX). `Retention Lift` compares randomized treatment and control evaluation arms (`randomized = TRUE` and `eligible = 1`) and returns blank when either group is empty.

## Designed edge cases for the service assurance scenario and oracle

| Question focus | Source tables and deliberate edge cases |
|---|---|
| Subscription lifecycle (Q01) | `subscription_month(subscription_key, account_key, market_id, month, opening_active, active_eom, gross_add, disconnect)`. Monthly balances reconcile; accounts 1 and 2 have July network care contacts and opening-cohort disconnects. |
| Service assurance SLA settlement (Q03) | `incident`, `service_site_map`, `contract`, `sla_evaluation`, `applied_credit`. Raw outages overlap; service 1 moves sites mid-incident; contract 2 starts mid-incident. Valid intervals are clipped and unioned per `(service_key, contract_key, month)`, thresholded per service, and capped once per contract-month before subtracting applied credits. |
| Customer identity and snapshots (Q08) | Two account pairs share the display name `Northstar Business` with distinct `account_key` values and markets. Finance recognized service revenue and Commerce booked merchandise revenue are distinct measures. |
| Time-valid billing mapping (Q14) | `account_month` and `billing_account_map`. Billing key `2` maps to account `1` before 1 June 2026 and to account `2` from 1 June 2026 onward; numeric equality between `billing_key` and `account_key` is never identity. |

All validity intervals are half-open UTC `[valid_from, valid_to)`. Finance credits record `month` (posting month) separately from `service_month`, and Enterprise applied credits record `month` (SLA service month) separately from `posted_at`. The independent oracle computes expected answers from raw tables without reading `sla_evaluation` as a shortcut.

## Reproducibility and TMDL structure

`build_data.build(profile, seed, output, batch_size=1024)` uses table- and key-scoped deterministic random streams independent of batch size. Each `<table_id>.parquet` entry in `manifest.json` records row counts, byte length, `file_sha256`, and `content_sha256`, along with independent clocks for data watermarks, semantic model revision, ontology revision, and context cache.

TMDL authoring follows the public [Skills for Fabric](https://github.com/microsoft/skills-for-fabric) semantic-model, Direct Lake, TMDL, and DAX guidelines (`skills/semantic-model-authoring/`). Each semantic model uses parameterized `WorkspaceId` and `LakehouseId` expressions (`<WORKSPACE_ID>` and `<LAKEHOUSE_ID>`) feeding a shared `'OneLake Source'` `AzureStorage.DataLake` expression, `entity` Direct Lake partitions, `discourageImplicitMeasures`, explicit `formatString` properties, `///` descriptions above every table, measure, and column, and a sample `East Region` role with table filter predicates and no embedded member identities.

## Limitations

See [limitations.md](limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
