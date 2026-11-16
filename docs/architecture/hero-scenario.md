# Service assurance scenario

This page specifies the hero scenario of the Northstar demo: one decision, one question, four semantic models, and the ontology that connects them. It is the reference for the ontology contract, the golden questions and the presenter run of show.

All data is synthetic. Names of tables, columns and measures are the ones in `fabric/semantic-models`.

## The decision

After a network incident, the service-assurance lead decides whom to contact first and what the commercial exposure is.

> **Hero question.** Which enterprise customers were served from sites hit by July incidents, what SLA credit exposure do we carry on their contracts, how much revenue and margin is at stake, and who already has escalated cases?

Expected answer: a ranked list of accounts with, for each one, the affected sites and contracts, credit exposure for the contract-month, recognized service revenue and contribution margin for the account-month, escalated cases, and a short list of exclusions (unmapped services, periods with no data).

## Why four models

| Scope | What it proves | Limit |
|---|---|---|
| Two models (Subscriptions, Finance) | Trusted measures are reused through the ontology | One shared key and one shared month. An agent can do this join without an ontology |
| **Four models (Network, Enterprise services, Finance, Care)** | Seven hops, three bridges between models, two time-valid relationships, three grains, one fan-out trap | Sized for an interactive answer |
| Eight models (full estate) | Breadth of the estate | Too wide for an interactive answer in rehearsal; kept as a backdrop |

Subscriptions can be added as a fifth model to extend the same incident to consumer lines through `Subscription Site Exposure`.

## Entity types

Logical entity types for the scenario. When an ontology is generated from semantic models, shared dimensions such as Site and Account can appear once per source model, joined by an identity relationship on the shared key. The contract records both the logical entity type and its source-qualified names.

| Entity type | Home model | Source table (Delta table) | Key | Purpose in the question |
|---|---|---|---|---|
| Incident | Network | `Incidents` (`incident`) | `incident_key` | Start, end, severity and site of the incident |
| Site | Network; also in Enterprise services | `Sites` (`site`) | `site_key` | Bridge between Network and Enterprise services |
| Site hour | Network | `Site Hour` (`site_hour`) | `site_key`, `hour_start` | Availability and unavailable minutes |
| Service-site assignment | Enterprise services | `Service Site Map` (`service_site_map`) | `service_key`, `site_key`, `valid_from` | Which service ran over which site, under which contract, and when |
| Service | Enterprise services | `Services` (`service`) | `service_key` | Enterprise service instance |
| Service month | Enterprise services | `Service Month` (`service_month`) | `service_key`, `month` | SLA compliance and breached services |
| Contract | Enterprise services | `Contracts` (`contract`) | `contract_key` | Validity period, credit terms and cap; holds `account_key` |
| SLA evaluation | Enterprise services | `SLA Evaluation` (`sla_evaluation`) | `contract_key`, `month` | Settled credit for the contract-month |
| Applied credit | Enterprise services | `Applied Credits` (`applied_credit`) | `contract_key`, `month` | Credits already posted against the contract |
| Account | All four models | `Accounts` (`account`) | `account_key` | Bridge from Enterprise services to Finance and Care |
| Account month | Finance | `Account Month` (`account_month`) | `account_key`, `month` | Recognized revenue and margin |
| Case | Care | `Cases` (`case`) | `case_key` | Escalations and repeat contacts |
| Case daily snapshot | Care | `Case Daily Snapshot` (`case_daily_snapshot`) | `case_key`, `snapshot_date` | Open cases at end of day |

Entity types with a composite key may need a single surrogate key column in the Delta table. If so, add it in the data generator; do not change a measure.

## The path

| Hop | From | To | Join | Valid for a period | Crosses models |
|---|---|---|---|---|---|
| 1 | Incident | Site | `site_key` | Incident interval `start` to `end` | No (Network) |
| 2 | Site | Service-site assignment | `site_key` | Assignment `valid_from` to `valid_to` must overlap the incident | **Yes: Network to Enterprise services** |
| 3 | Service-site assignment | Contract | `contract_key` | Contract `valid_from` to `valid_to` must overlap as well | No (Enterprise services) |
| 4 | Contract | SLA evaluation | `contract_key`, month of the incident | Contract-month | No (Enterprise services) |
| 5 | Contract | Account | `account_key` | | No (Enterprise services) |
| 6 | Account | Account month | `account_key`, same service month | Account-month | **Yes: Enterprise services to Finance** |
| 7 | Account | Case | `account_key`, cases opened in the period | | **Yes: Enterprise services to Care** |

Hops 3 and 4 use keys that exist in the Enterprise services tables but are not relationships inside that semantic model. Declaring them in the ontology is part of the value shown.

All validity periods are half-open UTC intervals: `valid_from <= t < valid_to`.

## Metrics and their home measures

Metrics link to measures. The DAX stays in the semantic model.

| Metric | Home model and table | Grain | Additive | Unit |
|---|---|---|---|---|
| Major Incidents | Network, `Incidents` | Incident | Yes | Count |
| Unavailable Site Minutes | Network, `Site Hour` | Site and hour | Yes | Minutes |
| Availability Percent | Network, `Site Hour` | Site and period | No (ratio) | Percent |
| Eligible Credits | Enterprise services, `SLA Evaluation` | Contract and month | Yes, across contracts | Integer cents |
| Credit Exposure | Enterprise services, `SLA Evaluation` | Contract and month | Yes, across contracts | Integer cents |
| Applied Credits | Enterprise services, `Applied Credits` | Contract and month | Yes | Integer cents |
| SLA Compliance | Enterprise services, `Service Month` | Service and month | No (ratio) | Percent |
| Breached Services | Enterprise services, `Service Month` | Service and month | Yes | Count |
| Recognized Service Revenue | Finance, `Account Month` | Account and month | Yes, across accounts | Integer cents |
| Contribution Margin | Finance, `Account Month` | Account and month | Yes, across accounts | Integer cents |
| Margin Percent | Finance, `Account Month` | Account and period | No (ratio) | Percent |
| Escalated Cases | Care, `Cases` | Case | Yes | Count |
| Repeat Contacts | Care, `Cases` | Case | Yes | Count |
| Open Cases EOD | Care, `Case Daily Snapshot` | Case and day | No (snapshot) | Count |

## Rules

Rules are stored in the ontology as plain language and linked to the entity types they govern. Agents read them as guidance. They are not executed and they are not access policies.

| Rule | Statement | Linked to |
|---|---|---|
| R1 Exposure needs overlap | A service is exposed to an incident only when the incident interval, the service-site assignment and the contract are valid at the same time. Validity periods are half-open UTC intervals. | Incident, Service-site assignment, Contract |
| R2 Credit is settled per contract and month | SLA credit is evaluated per service-month against its threshold, then capped once per contract-month. It is a whole-month settlement, not a loss caused by one incident. Never allocate it to an incident and never apply the cap twice. Credit exposure is eligible credit minus applied credit, as defined by the source measure. | Contract, SLA evaluation, Applied credit |
| R3 Finance is per account and month | Recognized revenue and margin are stated per account and month. Bring the list of accounts down to one row per account-month before looking up finance measures, so that several contracts or services never multiply revenue or margin. Credits posted in Finance are not netted against Enterprise services credits. | Account, Account month |
| R4 Identity is the account key | The account key identifies a customer in every model. Display names are not identity. A billing key is not an account key even when the numbers match; map it through the billing account map for the service date. | Account |
| R5 Unknown is not zero | A missing mapping, an absent credit row or a period with no data is unknown. Report it as an exclusion. Do not sum ratios, distinct counts or daily snapshots over time; ask the source measure at the final grain. | All entity types |

## What goes wrong without shared context

Each trap produces an answer that looks plausible. They are the basis of the golden questions.

| Trap | Wrong result | Rule |
|---|---|---|
| Join Site to services on `site_key` only | Services that were not on the site at the time of the incident are counted | R1 |
| Attribute the contract's monthly credit to each incident | Credit exposure is overstated when a month has several incidents | R2 |
| Join account revenue to contracts or services | Revenue and margin are multiplied for accounts with more than one contract or service | R3 |
| Match customers on display name, or treat billing key as account key | Accounts are merged or split | R4 |
| Treat a missing applied-credit row as zero, or sum open cases over days | Exposure and open-case counts are wrong | R5 |

## Golden questions

The evaluation uses a ladder, so that the scorecard shows where connected context starts to matter.

| Level | Models | Example | Purpose |
|---|---|---|---|
| L1 | 1 | How many major incidents were there in July 2026, by market? | Control: both arms should match |
| L2 | 2 | Which enterprise services ran over sites that had a major incident in July 2026? | One bridge and one validity period |
| L3 | 3 | For the contracts behind those services, what is the credit exposure and the recognized service revenue per account for July 2026? | Two bridges, contract-month and account-month grains |
| L4 | 4 | The hero question | Full path with the Care branch |
| N | 2 to 4 | Variants whose correct answer contains an unknown, an exclusion or an empty set | Unknown handling |
| P | 4 | The hero question asked by a user in the `East Region` role | Security is enforced where the query runs |

Every question has an oracle answer computed independently from the generated data. Runtime code never reads the oracle.

## Run of show

About twenty minutes. Lead with the product experience in Fabric; use the viewer in this repository for evidence and the scorecard.

| Minute | Step | Show |
|---|---|---|
| 0 to 2 | The decision | The July incidents and the question the service-assurance lead has to answer today |
| 2 to 5 | The estate | The Network, Enterprise services, Finance and Care semantic models, their measures and the security role. These are trusted and stay as they are |
| 5 to 8 | Semantic models only | Single-model questions work well. For the cross-domain question, show what the agent has to infer and what has to be written into its instructions |
| 8 to 12 | The ontology | Entity types, the relationships between models, a metric and the measure it links to, and the five rules |
| 12 to 16 | The same question, grounded | The plan, the path through seven hops, the queries on each model, the ranked accounts and the exclusions |
| 16 to 19 | One ontology, many agents | A Foundry agent drafts the outreach plan, a Copilot Studio agent answers an account team in Teams, GitHub Copilot uses the ontology while a developer adds a measure |
| 19 to 20 | The scorecard | Measured results for both arms, including the questions where they tie or fail |

## Checks before presenting live

Preview capabilities change. Confirm each item in the demo tenant and record the result and date in [platform-status.md](platform-status.md).

| Check | Pass condition |
|---|---|
| G1 Four-model ontology | One ontology holds entity types from all four Direct Lake models, and every metric in the contract links to its home measure |
| G2 Relationships between models | The Site and Account bridges and the two time-valid relationships exist and are used in a plan |
| G3 Interactive answer | The hero question returns inside the time budget on the demo capacity, three times in a row |
| G4 MCP tools | The ontology MCP endpoint lists its tools; names and schemas are recorded |
| G5 Agent surfaces | Each planned surface (Foundry, Copilot Studio, published agent, GitHub Copilot) answers the L2 question through the ontology |
| G6 Security | The `East Region` role returns only its accounts on every surface |
| G7 Hybrid binding (optional) | Spine entity types bound to lakehouse tables support graph traversal alongside model-bound metrics |
