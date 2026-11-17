# 0001. Semantic models own the calculations

**Status:** Accepted, 16 November 2026

## Context

Telecommunications domain teams already govern commercial and operational calculations in Power BI semantic models. In this repository, eight semantic models in [Direct Lake](https://learn.microsoft.com/en-us/fabric/fundamentals/direct-lake-overview) mode over [OneLake](https://learn.microsoft.com/en-us/fabric/onelake/onelake-overview) Delta tables define 64 DAX measures, units, grains, and sample [row-level security](https://learn.microsoft.com/en-us/fabric/security/service-admin-row-level-security) roles across Network, Enterprise services, Finance, Care, and peer domains.

When cross-domain questions arise, teams often copy DAX formulas or business calculations into agent prompts, custom scripts, or parallel schemas. Duplicating formulas causes numbers to drift from reconciled finance and SLA settlements. Re-aggregating non-additive ratios, distinct counts, or daily snapshots outside the semantic model also produces silent calculation errors.

[Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) (current experience, in preview, checked 16 November 2026) adds a shared layer of entity types, cross-model relationships, metrics, and plain-language rules above existing data items in Microsoft Fabric.

## Decision

- Keep all DAX expressions, units, grain definitions, and row-level and object-level security inside the Power BI Direct Lake semantic models ([architecture overview](../architecture/README.md)).
- Author every [ontology metric](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics) (in preview, checked 16 November 2026) as a metric linked to a source measure in its home semantic model, such as `Credit Exposure`, `Recognized Service Revenue`, `Contribution Margin`, and `Escalated Cases` ([service assurance metrics](../architecture/hero-scenario.md#metrics-and-their-home-measures)).
- Never copy a DAX formula into an ontology contract, prompt file, or agent instruction. Always evaluate ratios, distinct counts, and daily snapshots through their source measures at the final grain.
- Require Power BI Direct Lake semantic models for ontology binding. Document clearly for adopters that ontology binding is not supported for Import-mode semantic models or for Direct Lake models whose lakehouse workspace has inbound public access disabled (in preview, checked 16 November 2026; [ontology troubleshooting](https://learn.microsoft.com/en-us/fabric/iq/ontology/resources-troubleshooting)).

## Consequences

- Reconciled measures and security roles stay governed in one place. Queries run on the home semantic model under the demo user's Microsoft Entra ID delegated identity, requiring Read and Build permissions on the model to generate and query metrics (and Write permission to view DAX).
- [Ontology rules](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-rules) (in preview, checked 16 November 2026) stay plain-language guidance for agents covering overlap windows, grain discipline, and unknown handling; they do not execute calculations or act as access policies.
- In the [ontology item definition](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/ontology-definition) (in preview, checked 16 November 2026), only explicit metrics round-trip through the definition APIs today. Metrics linked to source measures are authored in Fabric and exported for conformance checks without writing the exported definition back over a working ontology ([platform status](../architecture/platform-status.md)).
- Customers whose Power BI estates rely on Import mode must convert target models to Direct Lake before adopting semantic-model ontology binding.
