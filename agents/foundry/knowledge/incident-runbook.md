# Northstar Wireless incident outreach runbook (synthetic reference)

This synthetic runbook defines how Northstar Wireless service assurance and enterprise account teams coordinate customer outreach after network incidents in the demo tenant. Attach it as optional knowledge through [Foundry IQ](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/what-is-foundry-iq) (preview, checked 16 November 2026) when testing the Microsoft Foundry prompt agent.

Agents use this runbook only to structure recommendations for human review. Agents never contact customers, open external notifications, or alter billing and network systems.

## Step 1: Operational severity triage

| Incident classification | Operational trigger | Required internal reviewers |
|---|---|---|
| Critical | Multi-site outage or sustained major incident affecting Tier 1 enterprise services | Service assurance lead, enterprise account director, care escalation manager |
| Major | Single-site major incident overlapping active enterprise contracts | Service assurance lead, assigned account manager |
| Moderate | Degraded site performance with no confirmed contract breach | Assigned account manager during scheduled review |

## Step 2: Care escalation check before outreach

Before scheduling any customer call or sending a status note, verify active support activity in the `Care` semantic model through the ontology:

1. Check `Escalated Cases` and `Repeat Contacts` on `Cases` for every exposed enterprise account.
2. If an account already has one or more escalated cases, schedule a joint 15-minute alignment huddle with the care escalation owner before contacting the customer.
3. Consolidate technical root-cause updates and commercial SLA review timing into a single briefing so the customer does not receive separate messages from Care and Enterprise sales.

## Step 3: Account team briefing template

Prepare one concise draft section per prioritized enterprise account containing:

- **Account identifier**: Source `account_key` and affected `site_key` and `contract_key` identifiers from the ontology path.
- **Source-cited metrics**: Exact values and home tables for `Credit Exposure`, `Recognized Service Revenue`, `Contribution Margin`, and `Escalated Cases`.
- **Ontology relationship path**: The hop-by-hop entity path traversed across `Network`, `Enterprise services`, `Finance`, and `Care`.
- **Exclusions and unknowns**: Explicit list of any unmapped services or missing evaluation records requiring analyst verification.
- **Recommended next step**: Proposed owner (account director or joint care-plus-account lead) and target outreach window from `sla-terms.md`.

## Step 4: Human sign-off gate

Complete all four checks before any external customer contact:

- [ ] Service assurance lead confirms incident start and restoration timestamps against `Network`.
- [ ] Commercial operations analyst reviews cited `Credit Exposure` and any listed exclusions.
- [ ] Care escalation owner confirms current case status for accounts with active escalations.
- [ ] Account director approves the final customer message and conducts the outreach personally.
