# Service assurance ontology authoring prompts

This generated file provides the step-by-step authoring prompts, review checklist, and test questions for `Northstar_Service_Assurance` (contract stream `service-assurance`, schema `1.0`). Regenerate it with `python tools/build_ontology_prompts.py` whenever `fabric/ontology/service-assurance.json` changes.

Fabric IQ ontology and the ontology agent in Fabric are preview features (checked 16 November 2026). Run these steps in a demo tenant on a workspace with Direct Lake semantic models and both Read and Build permissions on each model. Follow Microsoft Learn guidance to [generate an ontology from semantic models](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-generate-from-semantic-models), [use the ontology agent](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-agent), [keep metrics linked to source measures](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics), and [define plain-language business rules](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-rules).

## Authoring sequence

Add the four semantic models in exact order: **Network**, **Enterprise services**, **Finance**, **Care**.

1. Open the **Network** Direct Lake semantic model in the demo tenant, select **Generate ontology**, and create `Northstar_Service_Assurance` in the current Fabric IQ ontology experience.
2. Open the ontology agent in Fabric and keep the toggle in **Plan mode** while drafting each step below.
3. Inspect the read-only draft preview in the ontology canvas, complete the human review checklist, and switch to **Act mode** only to apply a reviewed step.
4. Set composite keys, validity intervals, and cross-model relationship bindings by hand in the ontology canvas whenever automatic generation leaves them unset.
5. Keep every metric linked to a source measure in its home semantic model. Do not copy or edit calculations in the ontology.
6. Never write an exported ontology definition back over a working item because links from metrics to source measures do not round-trip through the definition API ([Ontology item definition](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/ontology-definition), checked 16 November 2026).

## Plan prompts by source model

### Step 1: Network (`network`)

Generate `Northstar_Service_Assurance` from `network.SemanticModel`, then send this prompt in **Plan mode** to verify and refine the initial model scope before adding other semantic models:

```text
Stay in Plan mode. Review Northstar_Service_Assurance generated from the Network semantic model (network.SemanticModel) for the service assurance scenario.
Keep source-qualified entity names so shared dimensions remain distinct per semantic model and each metric stays on its home table.

Entity types to include from Network:
- network_Incidents (Incident) bound to Incidents [incident], key [incident_key] (interval start..end)
- network_Sites (Site) bound to Sites [site], key [site_key]
- network_SiteHour (Site hour) bound to Site Hour [site_hour], key [site_key, hour_start]
- network_Accounts (Account) bound to Accounts [account], key [account_key]

Relationships to verify or bind in this step:
- network_Incidents -> network_Sites on [site_key]: An incident occurs at one site.
- network_SiteHour -> network_Sites on [site_key]: Hourly availability telemetry belongs to one site.

Metrics to keep linked to source measures without copying formulas:
- Major Incidents on network_Incidents (table Incidents, grain [incident_key], additive, unit count)
- Unavailable Site Minutes on network_SiteHour (table Site Hour, grain [site_key, hour_start], additive, unit minutes)
- Availability Percent on network_SiteHour (table Site Hour, grain [site_key, hour_start], non-additive, unit percent)

Defer cross-model business rules until their linked entity types are added.

Do not alter semantic model tables, measures, or security roles. Post the read-only draft preview and list any key or relationship binding that still needs manual setup before Act mode.
```

Expected `Network` elements in the contract:

- `network_Incidents`: logical type **Incident**; semantic model table `Incidents` (Delta table `incident`); key `incident_key`; UTC interval `start` to `end`.
- `network_Sites`: logical type **Site**; semantic model table `Sites` (Delta table `site`); key `site_key`.
- `network_SiteHour`: logical type **Site hour**; semantic model table `Site Hour` (Delta table `site_hour`); key `site_key`, `hour_start`.
- `network_Accounts`: logical type **Account**; semantic model table `Accounts` (Delta table `account`); key `account_key`.
- `network_Incidents` to `network_Sites` on `site_key` (hop 1; within-model link): An incident occurs at one site.
- `network_SiteHour` to `network_Sites` on `site_key` (within-model link): Hourly availability telemetry belongs to one site.
- `Major Incidents` on `network_Incidents` (home table `Incidents`, grain `incident_key`, additive, unit `count`)
- `Unavailable Site Minutes` on `network_SiteHour` (home table `Site Hour`, grain `site_key`, `hour_start`, additive, unit `minutes`)
- `Availability Percent` on `network_SiteHour` (home table `Site Hour`, grain `site_key`, `hour_start`, non-additive, unit `percent`)

### Step 2: Enterprise services (`enterprise`)

With the ontology agent in **Plan mode**, send this prompt to extend `Northstar_Service_Assurance` with `enterprise.SemanticModel`:

```text
Stay in Plan mode. Extend Northstar_Service_Assurance with the Enterprise services semantic model (enterprise.SemanticModel) for the service assurance scenario.
Keep source-qualified entity names so shared dimensions remain distinct per semantic model and each metric stays on its home table.

Entity types to include from Enterprise services:
- enterprise_Sites (Site) bound to Sites [site], key [site_key]
- enterprise_ServiceSiteMap (Service-site assignment) bound to Service Site Map [service_site_map], key [service_key, site_key, valid_from] (validity valid_from..valid_to)
- enterprise_Services (Service) bound to Services [service], key [service_key]
- enterprise_ServiceMonth (Service month) bound to Service Month [service_month], key [service_key, month]
- enterprise_Contracts (Contract) bound to Contracts [contract], key [contract_key] (validity valid_from..valid_to)
- enterprise_SlaEvaluation (SLA evaluation) bound to SLA Evaluation [sla_evaluation], key [contract_key, month]
- enterprise_AppliedCredits (Applied credit) bound to Applied Credits [applied_credit], key [contract_key, month]
- enterprise_Accounts (Account) bound to Accounts [account], key [account_key]

Relationships to verify or bind in this step:
- network_Sites -> enterprise_Sites on [site_key] [cross-model; identity]: Same site in both models; the site key is the identity.
- enterprise_Sites -> enterprise_ServiceSiteMap on [site_key] [assignment period overlaps the incident interval]: A site serves a service for a period.
- enterprise_ServiceSiteMap -> enterprise_Contracts on [contract_key] [contract period overlaps the incident interval]: The assignment is covered by a contract.
- enterprise_Contracts -> enterprise_SlaEvaluation on [contract_key] [month of the incident]: Credit is settled per contract and month.
- enterprise_Contracts -> enterprise_Accounts on [account_key]: A contract is held by one account.
- network_Accounts -> enterprise_Accounts on [account_key] [cross-model; identity]: Same customer account across Network and Enterprise services.
- enterprise_ServiceSiteMap -> enterprise_Services on [service_key]: Each serving-site assignment belongs to one enterprise service.
- enterprise_ServiceMonth -> enterprise_Services on [service_key]: Monthly SLA performance belongs to one enterprise service.
- enterprise_ServiceMonth -> enterprise_Contracts on [contract_key]: Monthly service performance is governed by one contract.
- enterprise_Contracts -> enterprise_AppliedCredits on [contract_key] [same service month]: Applied SLA credits are recorded per contract and service month.

Metrics to keep linked to source measures without copying formulas:
- Eligible Credits on enterprise_SlaEvaluation (table SLA Evaluation, grain [contract_key, month], additive, unit cents)
- Credit Exposure on enterprise_SlaEvaluation (table SLA Evaluation, grain [contract_key, month], additive, unit cents)
- Applied Credits on enterprise_AppliedCredits (table Applied Credits, grain [contract_key, month], additive, unit cents)
- SLA Compliance on enterprise_ServiceMonth (table Service Month, grain [service_key, month], non-additive, unit percent)
- Breached Services on enterprise_ServiceMonth (table Service Month, grain [service_key, month], additive, unit count)

Plain-language business rules to attach once their entity types exist:
- R1 ExposureNeedsOverlap [network_Incidents, enterprise_ServiceSiteMap, enterprise_Contracts]: A service is exposed to an incident only when the incident interval, the service-site assignment and the contract are valid at the same time. Validity periods are half-open UTC intervals.
- R2 CreditPerContractMonth [enterprise_Contracts, enterprise_SlaEvaluation, enterprise_AppliedCredits]: SLA credit is evaluated per service-month against its threshold, then capped once per contract-month. It is a whole-month settlement, not a loss caused by one incident. Never allocate it to an incident and never apply the cap twice. Credit exposure is eligible credit minus applied credit, as defined by the source measure.

Do not alter semantic model tables, measures, or security roles. Post the read-only draft preview and list any key or relationship binding that still needs manual setup before Act mode.
```

Expected `Enterprise services` elements in the contract:

- `enterprise_Sites`: logical type **Site**; semantic model table `Sites` (Delta table `site`); key `site_key`.
- `enterprise_ServiceSiteMap`: logical type **Service-site assignment**; semantic model table `Service Site Map` (Delta table `service_site_map`); key `service_key`, `site_key`, `valid_from`; half-open UTC validity `valid_from` to `valid_to`.
- `enterprise_Services`: logical type **Service**; semantic model table `Services` (Delta table `service`); key `service_key`.
- `enterprise_ServiceMonth`: logical type **Service month**; semantic model table `Service Month` (Delta table `service_month`); key `service_key`, `month`.
- `enterprise_Contracts`: logical type **Contract**; semantic model table `Contracts` (Delta table `contract`); key `contract_key`; half-open UTC validity `valid_from` to `valid_to`.
- `enterprise_SlaEvaluation`: logical type **SLA evaluation**; semantic model table `SLA Evaluation` (Delta table `sla_evaluation`); key `contract_key`, `month`.
- `enterprise_AppliedCredits`: logical type **Applied credit**; semantic model table `Applied Credits` (Delta table `applied_credit`); key `contract_key`, `month`.
- `enterprise_Accounts`: logical type **Account**; semantic model table `Accounts` (Delta table `account`); key `account_key`.
- `network_Sites` to `enterprise_Sites` on `site_key` (hop 2; cross-model bridge; shared identity): Same site in both models; the site key is the identity.
- `enterprise_Sites` to `enterprise_ServiceSiteMap` on `site_key` (hop 2; within-model link; validity: assignment period overlaps the incident interval): A site serves a service for a period.
- `enterprise_ServiceSiteMap` to `enterprise_Contracts` on `contract_key` (hop 3; within-model link; validity: contract period overlaps the incident interval): The assignment is covered by a contract.
- `enterprise_Contracts` to `enterprise_SlaEvaluation` on `contract_key` (hop 4; within-model link; period: month of the incident): Credit is settled per contract and month.
- `enterprise_Contracts` to `enterprise_Accounts` on `account_key` (hop 5; within-model link): A contract is held by one account.
- `network_Accounts` to `enterprise_Accounts` on `account_key` (cross-model bridge; shared identity): Same customer account across Network and Enterprise services.
- `enterprise_ServiceSiteMap` to `enterprise_Services` on `service_key` (within-model link): Each serving-site assignment belongs to one enterprise service.
- `enterprise_ServiceMonth` to `enterprise_Services` on `service_key` (within-model link): Monthly SLA performance belongs to one enterprise service.
- `enterprise_ServiceMonth` to `enterprise_Contracts` on `contract_key` (within-model link): Monthly service performance is governed by one contract.
- `enterprise_Contracts` to `enterprise_AppliedCredits` on `contract_key` (within-model link; period: same service month): Applied SLA credits are recorded per contract and service month.
- `Eligible Credits` on `enterprise_SlaEvaluation` (home table `SLA Evaluation`, grain `contract_key`, `month`, additive, unit `cents`)
- `Credit Exposure` on `enterprise_SlaEvaluation` (home table `SLA Evaluation`, grain `contract_key`, `month`, additive, unit `cents`)
- `Applied Credits` on `enterprise_AppliedCredits` (home table `Applied Credits`, grain `contract_key`, `month`, additive, unit `cents`)
- `SLA Compliance` on `enterprise_ServiceMonth` (home table `Service Month`, grain `service_key`, `month`, non-additive, unit `percent`)
- `Breached Services` on `enterprise_ServiceMonth` (home table `Service Month`, grain `service_key`, `month`, additive, unit `count`)
- **R1 `ExposureNeedsOverlap`** (linked to `network_Incidents`, `enterprise_ServiceSiteMap`, `enterprise_Contracts`): A service is exposed to an incident only when the incident interval, the service-site assignment and the contract are valid at the same time. Validity periods are half-open UTC intervals.
- **R2 `CreditPerContractMonth`** (linked to `enterprise_Contracts`, `enterprise_SlaEvaluation`, `enterprise_AppliedCredits`): SLA credit is evaluated per service-month against its threshold, then capped once per contract-month. It is a whole-month settlement, not a loss caused by one incident. Never allocate it to an incident and never apply the cap twice. Credit exposure is eligible credit minus applied credit, as defined by the source measure.

### Step 3: Finance (`finance`)

With the ontology agent in **Plan mode**, send this prompt to extend `Northstar_Service_Assurance` with `finance.SemanticModel`:

```text
Stay in Plan mode. Extend Northstar_Service_Assurance with the Finance semantic model (finance.SemanticModel) for the service assurance scenario.
Keep source-qualified entity names so shared dimensions remain distinct per semantic model and each metric stays on its home table.

Entity types to include from Finance:
- finance_Accounts (Account) bound to Accounts [account], key [account_key]
- finance_AccountMonth (Account month) bound to Account Month [account_month], key [account_key, month]

Relationships to verify or bind in this step:
- enterprise_Accounts -> finance_Accounts on [account_key] [cross-model; identity]: Same customer; display names are not identity.
- finance_Accounts -> finance_AccountMonth on [account_key] [same service month]: Finance measures stay at account and month.

Metrics to keep linked to source measures without copying formulas:
- Recognized Service Revenue on finance_AccountMonth (table Account Month, grain [account_key, month], additive, unit cents)
- Contribution Margin on finance_AccountMonth (table Account Month, grain [account_key, month], additive, unit cents)
- Margin Percent on finance_AccountMonth (table Account Month, grain [account_key, month], non-additive, unit percent)

Plain-language business rules to attach once their entity types exist:
- R3 FinancePerAccountMonth [enterprise_Accounts, finance_Accounts, finance_AccountMonth]: Recognized revenue and margin are stated per account and month. Bring the list of accounts down to one row per account-month before looking up finance measures, so that several contracts or services never multiply revenue or margin. Credits posted in Finance are not netted against Enterprise services credits.

Do not alter semantic model tables, measures, or security roles. Post the read-only draft preview and list any key or relationship binding that still needs manual setup before Act mode.
```

Expected `Finance` elements in the contract:

- `finance_Accounts`: logical type **Account**; semantic model table `Accounts` (Delta table `account`); key `account_key`.
- `finance_AccountMonth`: logical type **Account month**; semantic model table `Account Month` (Delta table `account_month`); key `account_key`, `month`.
- `enterprise_Accounts` to `finance_Accounts` on `account_key` (hop 6; cross-model bridge; shared identity): Same customer; display names are not identity.
- `finance_Accounts` to `finance_AccountMonth` on `account_key` (hop 6; within-model link; period: same service month): Finance measures stay at account and month.
- `Recognized Service Revenue` on `finance_AccountMonth` (home table `Account Month`, grain `account_key`, `month`, additive, unit `cents`)
- `Contribution Margin` on `finance_AccountMonth` (home table `Account Month`, grain `account_key`, `month`, additive, unit `cents`)
- `Margin Percent` on `finance_AccountMonth` (home table `Account Month`, grain `account_key`, `month`, non-additive, unit `percent`)
- **R3 `FinancePerAccountMonth`** (linked to `enterprise_Accounts`, `finance_Accounts`, `finance_AccountMonth`): Recognized revenue and margin are stated per account and month. Bring the list of accounts down to one row per account-month before looking up finance measures, so that several contracts or services never multiply revenue or margin. Credits posted in Finance are not netted against Enterprise services credits.

### Step 4: Care (`care`)

With the ontology agent in **Plan mode**, send this prompt to extend `Northstar_Service_Assurance` with `care.SemanticModel`:

```text
Stay in Plan mode. Extend Northstar_Service_Assurance with the Care semantic model (care.SemanticModel) for the service assurance scenario.
Keep source-qualified entity names so shared dimensions remain distinct per semantic model and each metric stays on its home table.

Entity types to include from Care:
- care_Accounts (Account) bound to Accounts [account], key [account_key]
- care_Cases (Case) bound to Cases [case], key [case_key]
- care_CaseDailySnapshot (Case daily snapshot) bound to Case Daily Snapshot [case_daily_snapshot], key [case_key, snapshot_date]

Relationships to verify or bind in this step:
- enterprise_Accounts -> care_Accounts on [account_key] [cross-model; identity]: Same customer.
- care_Accounts -> care_Cases on [account_key]: An account raises cases.
- care_CaseDailySnapshot -> care_Cases on [case_key]: End-of-day open-case snapshots track one care case.

Metrics to keep linked to source measures without copying formulas:
- Escalated Cases on care_Cases (table Cases, grain [case_key], additive, unit count)
- Repeat Contacts on care_Cases (table Cases, grain [case_key], additive, unit count)
- Open Cases EOD on care_CaseDailySnapshot (table Case Daily Snapshot, grain [case_key, snapshot_date], non-additive, unit count)

Plain-language business rules to attach once their entity types exist:
- R4 IdentityIsAccountKey [network_Accounts, enterprise_Accounts, finance_Accounts, care_Accounts]: The account key identifies a customer in every model. Display names are not identity. A billing key is not an account key even when the numbers match; map it through the billing account map for the service date.
- R5 UnknownIsNotZero [network_Incidents, network_Sites, network_SiteHour, network_Accounts, enterprise_Sites, enterprise_ServiceSiteMap, enterprise_Services, enterprise_ServiceMonth, enterprise_Contracts, enterprise_SlaEvaluation, enterprise_AppliedCredits, enterprise_Accounts, finance_Accounts, finance_AccountMonth, care_Accounts, care_Cases, care_CaseDailySnapshot]: A missing mapping, an absent credit row or a period with no data is unknown. Report it as an exclusion. Do not sum ratios, distinct counts or daily snapshots over time; ask the source measure at the final grain.

Do not alter semantic model tables, measures, or security roles. Post the read-only draft preview and list any key or relationship binding that still needs manual setup before Act mode.
```

Expected `Care` elements in the contract:

- `care_Accounts`: logical type **Account**; semantic model table `Accounts` (Delta table `account`); key `account_key`.
- `care_Cases`: logical type **Case**; semantic model table `Cases` (Delta table `case`); key `case_key`.
- `care_CaseDailySnapshot`: logical type **Case daily snapshot**; semantic model table `Case Daily Snapshot` (Delta table `case_daily_snapshot`); key `case_key`, `snapshot_date`.
- `enterprise_Accounts` to `care_Accounts` on `account_key` (hop 7; cross-model bridge; shared identity): Same customer.
- `care_Accounts` to `care_Cases` on `account_key` (hop 7; within-model link): An account raises cases.
- `care_CaseDailySnapshot` to `care_Cases` on `case_key` (within-model link): End-of-day open-case snapshots track one care case.
- `Escalated Cases` on `care_Cases` (home table `Cases`, grain `case_key`, additive, unit `count`)
- `Repeat Contacts` on `care_Cases` (home table `Cases`, grain `case_key`, additive, unit `count`)
- `Open Cases EOD` on `care_CaseDailySnapshot` (home table `Case Daily Snapshot`, grain `case_key`, `snapshot_date`, non-additive, unit `count`)
- **R4 `IdentityIsAccountKey`** (linked to `network_Accounts`, `enterprise_Accounts`, `finance_Accounts`, `care_Accounts`): The account key identifies a customer in every model. Display names are not identity. A billing key is not an account key even when the numbers match; map it through the billing account map for the service date.
- **R5 `UnknownIsNotZero`** (linked to `network_Incidents`, `network_Sites`, `network_SiteHour`, `network_Accounts`, `enterprise_Sites`, `enterprise_ServiceSiteMap`, `enterprise_Services`, `enterprise_ServiceMonth`, `enterprise_Contracts`, `enterprise_SlaEvaluation`, `enterprise_AppliedCredits`, `enterprise_Accounts`, `finance_Accounts`, `finance_AccountMonth`, `care_Accounts`, `care_Cases`, `care_CaseDailySnapshot`): A missing mapping, an absent credit row or a period with no data is unknown. Report it as an exclusion. Do not sum ratios, distinct counts or daily snapshots over time; ask the source measure at the final grain.

## Human review checklist before Act mode

Complete every item below in the read-only canvas preview before switching the ontology agent from **Plan mode** to **Act mode** (and re-verify after the final step for checks `G1`, `G2`, `G3`, `G4`, `G5`, `G6`, `G7`):

- [ ] **Scope and entity count**: All 17 source-qualified entity types across **Network**, **Enterprise services**, **Finance**, **Care** appear in `Northstar_Service_Assurance`, with no unintended duplicate or dropped tables.
- [ ] **Keys and time bounds**: Every scalar key (`incident_key`, `site_key`, `service_key`, `contract_key`, `account_key`, `case_key`), composite grain (`site_key` + `hour_start`, `service_key` + `site_key` + `valid_from`, `service_key` + `month`, `contract_key` + `month`, `account_key` + `month`, `case_key` + `snapshot_date`), and UTC interval (`start`..`end`, `valid_from`..`valid_to`) is bound to the exact source column by hand where automatic generation left it blank.
- [ ] **Relationships and bridges**: All 17 contract relationships exist, including the cross-model bridges on `site_key` and `account_key` and the Enterprise services links from `enterprise_ServiceSiteMap` to `enterprise_Contracts`, `enterprise_SlaEvaluation`, and `enterprise_AppliedCredits`. No relationship joins on `display_name` or `billing_key`.
- [ ] **Metrics linked to source measures**: All 14 metrics sit on their home entity and remain linked to source measures in the four semantic models, with zero copied or hand-edited formulas.
- [ ] **Plain-language rules**: All 5 rules (`R1` to `R5`) match the contract statements word for word and link to every listed entity type.
- [ ] **Source safety**: Semantic models, `East Region` row-level security roles, and workspace settings in the demo tenant are untouched, and any definition export is inspected read-only with `python tools/check_ontology_conformance.py` rather than written back.

## Question prompts (L1 to L4)

Run these prompts in **Plan mode** (or through a connected demo user session) after publishing the reviewed ontology:

### L1: Single model control (`Network`)

```text
How many major incidents were there in July 2026, by market? Use the Major Incidents metric from Network and report exact integer counts.
```

### L2: Two models and one validity interval (`Network`, `Enterprise services`)

```text
Which enterprise services ran over sites that had a major incident in July 2026? Join sites on site_key and enforce half-open UTC overlap across the incident interval, the service-site assignment, and the contract validity period (rule R1). Report any unmapped service as an exclusion rather than zero (rule R5).
```

### L3: Three models and two grains (`Network`, `Enterprise services`, `Finance`)

```text
For the contracts behind those services, what is the credit exposure and the recognized service revenue per account for July 2026? Keep Credit Exposure at contract and month (rule R2), deduplicate accounts on account_key before looking up Recognized Service Revenue at account and month in integer cents (rules R3 and R4), and list exclusions instead of substituting zero for unknown rows (rule R5).
```

### L4: Four-model service assurance hero question (`Network`, `Enterprise services`, `Finance`, `Care`)

```text
Which enterprise customers were served from sites hit by July incidents, what SLA credit exposure do we carry on their contracts, how much revenue and margin is at stake, and who already has escalated cases? Return a ranked list of enterprise accounts by Credit Exposure with affected sites and contracts, Credit Exposure for the contract-month, Recognized Service Revenue and Contribution Margin for the account-month in integer cents, Escalated Cases from Care, and an explicit list of exclusions where mappings or credit rows are absent (rules R1, R2, R3, R4, and R5).
```
