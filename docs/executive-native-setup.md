# Reproduce the quickstart ontology in Fabric

This guide reproduces the small two-model quickstart ontology in a demo tenant alongside the wider Customer360 estate. Its structural contract is `fabric/ontology/executive-demo.json`; the presenter talk track is [executive-demo-script.md](executive-demo-script.md).

## Target structure

| Surface | Required result |
|---|---|
| Source semantic models | Subscriptions and Finance Direct Lake models over the synthetic `showcase-v2` data; reuse existing models rather than cloning |
| Entity types in Fabric | Source-qualified `Subscriptions Accounts`, `Subscription Month`, `Finance Accounts`, and `Account Month` |
| Relationships | Each fact entity to its own account entity, plus the conformed customer identity bridge on `account_key` |
| Focus measures | `Active Lines EOM`, `Recognized Service Revenue`, `Contribution Margin` |
| Generated metric links | Retain all 14 metrics linked to source measures generated on the chosen tables; highlight three in presentations |
| Ontology rules | `CustomerAndPeriodMeaning` and `TrustedMeasuresAndReviewLimits` |
| Fact keys | Preserve composite source grains (`subscription_key` + `month`, `account_key` + `month`) without declaring a partial single-column key unique |

## Supported authoring procedure (preview, checked 16 November 2026)

Use your approved demo tenant workspace, signed-in demo user or author account, and deployed Subscriptions and Finance semantic models from private configuration outside the repository. Verify the signed-in user and tenant before making changes, and never delete or empty a Fabric workspace.

1. Call the published **Create Ontology** REST API with an approved display name and an empty definition body so Microsoft Fabric creates a Fabric IQ ontology (current experience). Record the returned item ID.
2. Open that new ontology item in Fabric. In the ontology agent in Fabric, stay in **Plan** mode (default) and request the four source-qualified entity types and three relationships above from the Subscriptions and Finance semantic models, keeping metrics linked to source measures rather than copying DAX.
3. Review the draft entity types, warnings, source model bindings, and home tables before approving **Act** mode. Keep both account entity types source-qualified and joined by `account_key`; never merge on `display_name` and never use earlier ontology JSON formats.
4. Read back the exported TMDL definition (`getDefinition`) and ontology MCP metadata. In our capture, the ontology agent in Fabric summarized three measures in prose while readback showed all 14 metrics linked to source measures on those tables; always verify readback rather than relying on summary prose.
5. Add the two rules from `fabric/ontology/executive-demo.json` (`CustomerAndPeriodMeaning` and `TrustedMeasuresAndReviewLimits`), preserving every existing metric link, source model ID, lineage tag, and relationship part. See [architecture/platform-status.md](architecture/platform-status.md) for the round-trip limitation on metrics linked to source measures before using `updateDefinition`.
6. Confirm rule statements, linked entity references, and unchanged metric links in a readback check.
7. Connect as an authorized demo user, inspect `tools/list` on the ontology MCP endpoint, and ask the business question from `fabric/ontology/executive-demo.json` once without supplying expected account IDs or a query plan.
8. Reconcile the returned account IDs, measure values, and home tables against independent DAX queries run on Subscriptions and Finance, keeping answer capture and source-verification timestamps distinct.

## Rebuild sanitized recorded files

`tools/build_executive_context.py` projects private TMDL and ontology MCP captures into the identifier-free four-entity display structure, and `tools/build_executive_demo.py` checks the captured answer against independent source results:

```bash
python tools/build_executive_context.py --help
python tools/build_executive_demo.py --help
```

To serve the sanitized two-stream recording library locally in bash or PowerShell:

```bash
python -m telco_iq.cli serve --recorded-library recordings/northstar-demo-library-v2-2026-10-28.json --port 8767
```

## Public Microsoft Learn references

| Topic | Reference |
|---|---|
| Create an ontology item | [Create Ontology](https://learn.microsoft.com/en-us/rest/api/fabric/ontology/items/create-ontology) |
| Generate from semantic models and add further models | [Generate from semantic models](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-generate-from-semantic-models) |
| Ontology TMDL definition parts and metric round-trip notes | [Ontology definition](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/ontology-definition) |
| Full-definition replacement semantics | [Update Ontology Definition](https://learn.microsoft.com/en-us/rest/api/fabric/ontology/items/update-ontology-definition) |
| Item-scoped ontology MCP endpoint | [Ontology MCP server](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) |

## Limitations

See [limitations.md](limitations.md) for what the offline repository and sanitized recordings show versus what requires live verification in a demo tenant.
