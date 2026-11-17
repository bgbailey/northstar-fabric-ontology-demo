# Bring your own semantic models

Use the scaffold as a review queue, not as an automatic reproduction of a curated
ontology. The tool reads local TMDL, emits one draft entity per table and one draft
metric per measure, and proposes cross-model bridges only from matching key-like
`sourceColumn` names. It never copies DAX expressions or promotes a bridge into an
ontology relationship.

## Choose a narrow starting point

1. Choose one costly, repeatable decision. Name the user, the action, and the cost
   of a wrong answer.
2. Select the three or four semantic models on that decision path. Include only
   the models needed to answer the decision; a broad inventory is not a business
   design.
3. Generate a starter draft:

   ```bash
   python tools/scaffold_contract.py --models fabric/semantic-models \
     --include-model network --include-model enterprise \
     --include-model finance --include-model care \
     --output build/service-assurance-starter.json
   ```

   Use `--json` instead of `--output` to emit JSON to standard output. The output is
   sorted and deterministic, and every generated entity, metric and bridge is
   marked for review.

## Review the semantic model projection

For each draft entity, confirm the intended logical entity, physical source table,
entity key, and model table. A column ending in `_key` is only a candidate; a shared
name does not prove shared identity. Review each bridge candidate against:

- matching keys and identity, not display names;
- one-to-many or many-to-many cardinality;
- temporal validity and half-open interval behaviour;
- source security, filters and model context;
- the final grain at which the joined data remains meaningful.

For metrics, verify units, additivity, non-additive or snapshot behaviour, and
grain. Never infer these properties from a measure name or DAX shape. Keep
calculations in the semantic model and do not paste DAX into ontology rules.

## Project a reviewed reference

An optional reference contract is a projection mode, not model inference:

```bash
python tools/scaffold_contract.py --models fabric/semantic-models \
  --include-model network --include-model enterprise \
  --include-model finance --include-model care \
  --contract fabric/ontology/service-assurance.json \
  --output build/service-assurance-projection.json
```

`--contract` validates that each referenced model table and metric exists in TMDL,
then projects the contract's 17 entities and 14 metrics. Curated logical names,
keys, validity, grain, additivity and units are copied only from that reviewed
reference. Bridges are still generated candidates and remain `to review`.

Discovery is intentionally broader. The complete eight-model tree contains 59
model-qualified tables and 64 measures; the four service-assurance source models
contain 31 tables and 32 measures. That is why discovery must not claim to recreate
the curated 17-entity, 14-metric service contract.

## Finish the ontology and evaluate it

Write explicit natural-language rules for temporal overlap, deduplication,
unknown values, identity and metric handling. Draft golden questions from the
chosen costly decision, including negative and trap cases for invalid joins,
snapshot summation, rate addition and non-unique display names.

Run the repository evaluation workflow with the same model preparation for every
arm. Compare exact values and exclusions, retain partial or failed runs, and tie
any claim to the resulting scorecard. Repeat after model, bridge or rule changes.

Binding support is separate from scaffolding. Import-mode semantic models are not
supported for ontology binding. Direct Lake models whose lakehouse workspace has
inbound public access disabled also cannot bind (Microsoft Learn ontology
troubleshooting, checked 17 November 2026). Use Direct Lake over OneLake Delta
tables in an approved demo tenant and verify current support before a demo.

## Evidence gaps &#47; still&nbsp;thin

- The scaffold performs local, syntax-aware metadata extraction; it is not live
  TOM or DAX execution and does not prove tenant binding.
- Correct keys, validity, grain, units, additivity and rules require owner review.
  The generated candidate list deliberately omits assertions that would need that
  evidence.
- Validate an adapted ontology against golden questions in the demo tenant and in
  the approved demo Azure subscription before making an accuracy claim.
