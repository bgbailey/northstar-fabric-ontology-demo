# Reproduce the small executive ontology

This is the small native stream beside Customer360, not a way to trim or
replace the large item. Its structural contract is
`fabric\ontology\executive-demo.json`; the presentation is
`docs\executive-demo-script.md`.

## Result to keep small

| Surface | Required result |
|---|---|
| Existing source models | Subscriptions and Finance from the same synthetic showcase population; reuse them, do not clone |
| Native entities | Source-specific Subscriptions Accounts, Subscription Month, Finance Accounts and Account Month |
| Native relationships | Each fact to its own account dimension, plus the reviewed conformed customer bridge |
| Focus measures | Active Lines EOM, Recognized Service Revenue, Contribution Margin |
| Actual generated proxies | Preserve the14original source proxies returned on the chosen homes; present three |
| Native Rules | CustomerAndPeriodMeaning and TrustedMeasuresAndReviewLimits |
| Fact keys | Preserve source composite grains; fact entities remain keyless rather than declaring a partial scalar key unique |

## Supported authoring route

Use the approved exact DEV workspace, account and two model identities from
private configuration. Verify the actual signed-in user and tenant; keep the
labelled task/service/account/tenant context in the same browser window.
Do not use an ambient corporate connection or a privileged source-data fallback.

1. Use the published **Create Ontology** REST operation with a new approved
   display name and **no definition**. Current public documentation specifies
   generation2for that bare create. Capture the actual returned item identity
   and verify `properties.generation=2`; never infer ownership from a name.
2. Open that **new small item only**. In the native ontology agent, use
   **Plan** to request the four source-specific entities and three conformed
   relationships above from the two existing semantic models. Ask for the
   three focus measures as original source-backed proxies, not copied DAX.
   Explicitly exclude other entity types. Source tables used internally by a
   measure need not all become ontology entities.
3. Review the actual final draft, warnings, source identities and home tables
   before one separately approved **Act**. Keep the two account copies
   source-specific; do not merge by display name or directly join fact rows.
   Never substitute a legacy ontology payload, private endpoint or copied
   large definition.
4. Read the actual complete generation2TMDL and native MCP metadata back.
   In this build the agent claimed3proxies, but service readback proved14.
   It also issued an invalid shared-account-backing warning while the actual
   source backings were distinct. Actual bindings take precedence over prose;
   preserve generated source measures rather than destructively trimming them.
5. Add the two exact linked Rules from the structural contract using a
   reviewed supported route. This build used the published generation2
   `updateDefinition` contract after the actual source-backed bootstrap.
   Preserve every proxy body, source ID, lineage and non-Rule part, and check
   a fresh base before writing. Do not round-trip the only large native item.
6. Verify the Rule statements/entity references and all source bindings.
   This service normalized partition annotation order and `ONT_ItemKind`
   Dataset to SemanticModel; a narrow read-only semantic review confirmed
   that all proxy bodies and other values survived. Do not broadly ignore
   definition differences or assume round-trip fidelity.
7. Acquire actual delegated small-item metadata, inspect current `tools/list`
   and reviewed read-only schemas, then ask the structural contract's business
   question once. Never send account answers or a scripted query DAG.
   Retain the original native result without executing returned code.
8. Reconcile the original account identities and three measure values against
   independently retained complete source-query results. Verify the native
   home-table prose too. Preserve separate source/native capture times.

No source measure, data, AI instruction, source refresh, role, permission,
shared capacity or existing native item is changed by this procedure.
Never delete or empty a Fabric workspace.

## Rebuild the recorded executive evidence

`tools\build_executive_context.py` projects actual private TMDL/MCP metadata
into the identifier-free four-entity display contract.
`tools\build_executive_demo.py` checks the native answer against the reviewed
complete source reference and rejects wrong homes, changed periods, missing
or unsafe values, duplicate identities and private target identifiers.
Use `--help` for the exact input paths; all auth and original private captures
stay outside the repository and OneDrive.

The exported answer is an archive, not a current reader grant. Its source
queries are independent verification originals, not an exposed native trace.
`-RecordedLibrary` serves both the executive archive and the preserved
Customer360 archive without any auth, source or model calls.

## Current public references

| Contract | Reference |
|---|---|
| Generation2bare create | [Create Ontology](https://learn.microsoft.com/rest/api/fabric/ontology/items/create-ontology) |
| Native semantic-model bootstrap and second source | [Generate from semantic models](https://learn.microsoft.com/fabric/iq/ontology/how-to-generate-from-semantic-models) |
| New-experience Rule/TMDL parts | [Ontology definition](https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/ontology-definition) |
| Full-definition replacement semantics | [Update Ontology Definition](https://learn.microsoft.com/rest/api/fabric/ontology/items/update-ontology-definition) |
| Separate item-scoped consumption | [Ontology MCP](https://learn.microsoft.com/fabric/iq/ontology/how-to-use-ontology-mcp-server) |

Use current Learn and actual generation-2 observations when reproducing this
setup. Cached examples are reference material, not proof of tenant availability
or authority to mutate an existing item. Do not use legacy ontology CLI recipes.

## Evidence gaps / still thin

The small item, real Rules and native business answer are verified for this
synthetic environment. Managed Rules consideration, automatic AI inheritance,
native internal queries/model identity and general accuracy/speed advantages
are not established. Repeating the setup in another tenant still requires
that tenant's actual supported capability, identity and source-binding checks.
