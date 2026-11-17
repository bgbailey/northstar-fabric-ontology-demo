---
name: propose-measure-and-tests
agent: "agent"
description: "Propose a new Power BI semantic-model measure in the model generator, link it as a Fabric IQ ontology metric, and design offline tests without copying DAX into the ontology."
---

Propose a new measure requested by the user (or suggest one candidate service-assurance measure if none was specified) while keeping every calculation in the Power BI semantic models and connected context in the [Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) (preview, checked 16 November 2026).

Follow these steps:

1. Check existing ontology entity types, grains, and metrics through `northstarOntology` in your demo tenant (or [`../../fabric/ontology/service-assurance.json`](../../fabric/ontology/service-assurance.json) offline) and existing measures through `fabricIq` (`GetSemanticModelSchema`) or [`../../tools/build_models.py`](../../tools/build_models.py) so you do not duplicate an existing measure or collide with a column name.
2. Draft the semantic-model measure addition for [`../../tools/build_models.py`](../../tools/build_models.py). Specify the target model, home table, measure name, unit, aggregation kind (`additive`, `semi_additive`, or `non_additive`), allowed dimensions, plain-language description, DAX definition, and matching DuckDB SQL check. Never edit generated `.tmdl` files under `fabric/semantic-models/` by hand.
3. Draft the matching metric metadata entry for [`../../fabric/ontology/service-assurance.json`](../../fabric/ontology/service-assurance.json) containing only `name`, `model`, `modelTable`, `entity`, `grain`, `additive`, and `unit`. Never copy DAX formulas, SQL expressions, or ontology rule text into the ontology metric entry or [`../../agents/shared/instructions.md`](../../agents/shared/instructions.md).
4. Outline focused offline unit and contract tests in `tests/` (following [`../../tests/test_data_models.py`](../../tests/test_data_models.py) and [`../../tests/test_ontology_contract.py`](../../tests/test_ontology_contract.py)) that verify regenerated TMDL metadata, ontology-to-measure binding alignment, grain rules, and unknown-value exclusions without calling a live service.
5. Stop and present the proposed plan and test cases for review unless the user explicitly asks to edit repository files.
