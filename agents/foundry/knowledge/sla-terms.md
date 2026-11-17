# Northstar Wireless enterprise SLA terms (synthetic reference)

This synthetic reference document supports the Northstar Wireless demonstration in a demo tenant. You can attach it as optional unstructured knowledge through [Foundry IQ](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/what-is-foundry-iq) (preview, checked 16 November 2026) so the Microsoft Foundry agent grounds outreach timing and human review commitments.

All numeric figures for availability, breached services, credits, revenue, and margin come exclusively from the governed semantic-model measures linked through the Fabric IQ ontology. Never calculate or estimate settlement amounts from this document.

## Enterprise service tiers and communication targets

| Service tier | Initial incident notice | Account team update cadence | Executive summary target |
|---|---|---|---|
| Tier 1 — Mission Critical | Within 30 minutes of confirmed major incident | Every 60 minutes until service restoration | Within 24 hours of restoration |
| Tier 2 — Business Priority | Within 60 minutes of confirmed major incident | Every 2 hours during business hours | Within 2 business days of restoration |
| Tier 3 — Standard Enterprise | Within 4 hours of confirmed major incident | Next business day summary | Monthly service review |

## Outreach prioritization factors

When preparing a draft outreach queue for human review after network incidents, rank affected enterprise accounts using source-cited measures from the connected ontology:

1. **Commercial credit exposure**: Lead with enterprise accounts carrying the highest positive `Credit Exposure` from the `Enterprise` semantic model (`SLA Evaluation` table).
2. **Revenue and margin at stake**: Include `Recognized Service Revenue` and `Contribution Margin` from the `Finance` semantic model (`Account Month` table) so account directors see commercial context alongside credit risk.
3. **Active care escalations**: Flag accounts that already show positive `Escalated Cases` or `Repeat Contacts` in the `Care` semantic model (`Cases` table) so the account director and care lead coordinate one message.
4. **Data exclusions**: List any unmapped services, missing credit records, or unauthorized rows in a separate exclusions note for analyst follow-up.

## Commercial credit and communication governance

- **Human sign-off required**: Every customer notice, service review invitation, and commercial credit memo requires review and sign-off by the assigned account director and service assurance lead.
- **No automated credit issuance**: Agents may summarize settled measures and draft briefing notes, but they never post billing adjustments, promise settlement amounts, or modify contract terms.
- **Disputed or incomplete records**: When an evaluation record or mapping is absent in the source models, mark the item as unknown for commercial operations review rather than quoting zero exposure.
