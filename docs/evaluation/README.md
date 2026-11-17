# Evaluation design and claims policy

This page defines how the Northstar repository evaluates cross-domain question answering over the [service assurance scenario](../architecture/hero-scenario.md) and sets the evidence rules for every benefit claim. An **evaluation arm** (referred to below as an *arm*) is one tested configuration given the same questions, data, and model.

[Fabric IQ ontology](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) (current experience), the [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server), and Power BI semantic-model [AI preparation](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-prepare-data-ai) are in preview (checked 17 November 2026; see [../architecture/platform-status.md](../architecture/platform-status.md)).

## Evaluation arms

Capable agents can join Power BI semantic models without an ontology when they infer the right bridges or carry domain rules in their instructions. Testing only a bare semantic-model baseline against an ontology conflates two distinct questions: whether cross-model rules improve accuracy, and whether those rules belong copied across agent prompts or shared once in an ontology ([ADR 0004](../decisions/0004-evaluation-and-claims.md)). The evaluation therefore compares three arms:

| Arm | Identifier | MCP surface | Cross-model context and instructions | What it measures |
|---|---|---|---|---|
| **Arm A** | `baseline` | Generally available [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) (version 2 default toolset, checked 17 November 2026) | Per-model schemas and AI preparation only; no cross-model bridges, validity windows, or grain rules supplied | Baseline discovery and accuracy over well-prepared semantic models alone |
| **Arm A+** | `baseline_plus_rules` | Generally available [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) (version 2 default toolset, checked 17 November 2026) | Same setup as Arm A plus service assurance rules `R1` to `R5` pasted into the agent instructions | Single-agent accuracy when rules live in prompts, alongside prompt maintenance and token cost |
| **Arm B** | `ontology` | Item-scoped [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) (preview, checked 17 November 2026) plus governed DAX on home semantic models | Entity types, cross-model bridges, metrics linked to source measures, and rules read from the ontology; short shared instructions (`agents/shared/instructions.md`) | Accuracy, path fidelity, effort, and stability when business context comes from one shared ontology |

- **Arm A (`baseline` — semantic models only):** Queries the four [Direct Lake](https://learn.microsoft.com/en-us/fabric/fundamentals/direct-lake-overview) semantic models through the generally available [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) using `DiscoverArtifacts`, `GetSemanticModelSchema`, `ValueSearch`, and `ExecuteQuery` (version 2 default toolset, checked 17 November 2026). Cross-model bridges, validity windows, and grain rules are not supplied; the agent infers relationships from table schemas and per-model AI preparation.
- **Arm A+ (`baseline_plus_rules` — semantic models with rules in prompt):** Uses the same [Fabric IQ MCP server](https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp) as Arm A, with the five service assurance rules (`R1` to `R5` from [../architecture/hero-scenario.md](../architecture/hero-scenario.md#rules)) pasted into the agent's instructions. Shows both accuracy when domain rules are carried in prompts and the token and maintenance cost of duplicating business rules across every agent surface.
- **Arm B (`ontology` — ontology-grounded):** Reads entity types, cross-model relationships, [metrics linked to source measures](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-metrics) (preview, checked 17 November 2026), and [plain-language rules](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-rules) (preview, checked 17 November 2026) from the [ontology MCP endpoint](https://learn.microsoft.com/en-us/fabric/iq/ontology/how-to-use-ontology-mcp-server) (preview, checked 17 November 2026). Uses only the short shared instructions in `agents/shared/instructions.md` without restating domain rules, and executes governed DAX measures on their home semantic models.

## Fairness invariants

Every run holds data, semantic models, single-model preparation, demo user identity, model configuration, and questions constant across all three arms. No semantic model is stripped of descriptions or measures to favour the ontology.

| Invariant | Specification |
|---|---|
| **Seeded dataset** | Same deterministic synthetic dataset (`service-assurance` profile, 240 accounts, seed `20260929`) stored as OneLake Delta tables in a demo tenant and as matching Parquet files for offline oracle checks |
| **Semantic models and AI preparation** | Same four Direct Lake Power BI semantic models (`Network`, `Enterprise`, `Finance`, and `Care`) and the same model-level [AI preparation](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-prepare-data-ai) (preview, checked 17 November 2026): table and measure descriptions, AI data schemas, and verified `L1` answers |
| **Demo user permissions** | Same Microsoft Entra ID demo user permissions across arms, covering both the full-estate `default` demo user and the `East Region` [row-level security](https://learn.microsoft.com/en-us/fabric/security/service-admin-row-level-security) demo user (`east_region`) |
| **Model deployment and budgets** | Same Microsoft Foundry language model deployment, temperature and reasoning settings, canonical output schema, and per-run retry, token, spend, and wall-clock limits |
| **Golden questions and repeats** | Same 27 golden questions in `eval/questions/service-assurance.jsonl` across `L1` (6 single-model controls), `L2` (5 two-model questions), `L3` (5 three-model questions), `L4` (4 four-model questions), `N` (4 unknown and exclusion cases), and `P` (3 `East Region` security checks), evaluated with the same repeat count (`k = 5` by default) |

Single-model `L1` control questions verify that all three arms answer single-domain questions equally well before multi-model hops (`L2` to `L4`), exclusions (`N`), and row-level security (`P`) are tested.

## Metrics and statistical intervals

Answers are scored deterministically against an independent DuckDB oracle (`eval/oracle/service_assurance.py`) computed directly from the seeded Parquet tables. Runtime code in `src/telco_iq` never imports `eval`, and numeric accuracy never uses language-model grading.

| Metric | Definition |
|---|---|
| **Answer correctness** | Returned entity key set matches the DuckDB oracle (`eval/oracle/service_assurance.py`) and every numeric cell matches exactly as integer cents or integer counts |
| **Set quality** | Precision, recall, and F1 on returned entity keys against the oracle key set |
| **Grain and validity violations by trap** | Exact comparison against the five named trap answers (`site-key-only`, `credit-per-incident`, `revenue-fan-out`, `display-name-or-billing-key`, and `missing-as-zero`) from [../architecture/hero-scenario.md](../architecture/hero-scenario.md#what-goes-wrong-without-shared-context), reported by trap name |
| **Unknown handling** | Expected exclusions are reported as a partial result stating what is excluded, and no unknown, unmapped, or absent value is returned as zero (`R5`) |
| **Path correctness** | Reported cross-model bridges and half-open UTC time-validity checks match `fabric/ontology/service-assurance.json` |
| **Effort** | Tool calls, input and output tokens, and wall-clock seconds per run |
| **Stability across repeats** | Share of the `k` repeats for a question that produce the identical canonical answer table, plus unanimous `pass^k` (all `k` repeats matching the oracle) |

### Statistical reporting

- **Per-arm proportions:** Pass rates and trap rates are reported with **95% Wilson score intervals** so uncertainty stays bounded even at small sample sizes or extreme proportions (`0%` or `100%`).
- **Differences between arms:** Pairwise comparisons (`B - A`, `B - A+`, and `A+ - A`) use **paired bootstrap 95% confidence intervals** resampled across the 27 questions, preserving question-by-question pairing across repeated runs.

## Claims policy

1. **Evidence gate for benefit claims:** Any statement of accuracy, reliability, or effort benefit in documentation, architecture pages, or presenter scripts must link to a published scorecard whose paired bootstrap 95% confidence interval for the difference excludes zero.
2. **Open reporting of ties and losses:** Ties (such as `L1` single-model controls or `B` versus `A+` where prompt-carried rules match ontology-grounded accuracy for one agent) and losses are published openly alongside wins. When `B` and `A+` tie on accuracy, positioning stays strictly on single-definition reuse: reviewing entities, bridges, metrics linked to source measures, and rules once in Microsoft Fabric instead of copying and synchronizing rules across Microsoft Foundry, Microsoft Copilot Studio, Microsoft 365 Copilot, and GitHub Copilot prompts.
3. **Offline pipeline validation versus live evidence:** Offline scripted runs (`eval/runners/run_arms.py --mode offline`) validate the evaluation runner, DuckDB oracle, trap detector, and scorecard pipeline in continuous integration without calling live cloud services. Offline runs are never cited as live language-model or Microsoft Fabric evidence. Live runs execute against a demo tenant with explicit target approval and remain subject to the scope boundaries in [../limitations.md](../limitations.md).

## Published scorecards

### 17 November 2026 — Service assurance live run (`k = 5`)

- **Artifacts:** [recordings/evaluation/2026-11-17/scorecard.json](../../recordings/evaluation/2026-11-17/scorecard.json), [scorecard.html](../../recordings/evaluation/2026-11-17/scorecard.html), and sanitized per-run logs in [runs.jsonl](../../recordings/evaluation/2026-11-17/runs.jsonl) (also embedded in `recordings/northstar-demo-library-v2-2026-10-28.json` for offline viewing in `northstar serve`).
- **Run conditions:** `live_demo_tenant`, `service-assurance` profile (seed `20260929`), `27` questions × `3` arms × `k = 5` repeats (`405` runs), Microsoft Fabric `F8` capacity in `Central US`, Microsoft Foundry `gpt-5-mini (2025-08-07)` (`medium` reasoning effort), `service_default` Fabric IQ MCP toolset, executed `2026-11-17T15:45:03Z` to `2026-11-17T17:32:40Z`.

| Arm | Exact-answer accuracy (95% Wilson CI) | Mean key F1 (precision / recall) | Unknown handling (95% Wilson CI) | `missing-as-zero` traps hit | Mean tool calls | Mean input / output tokens |
|---|---|---|---|---|---|---|
| **Arm A (`baseline`)** | `28.1%` (`38/135`, `[21.2%, 36.3%]`) | `0.598` (`0.623` / `0.636`) | `86.7%` (`[79.9%, 91.4%]`) | `16` | `7.42` | `70,928` / `8,823` |
| **Arm A+ (`baseline_plus_rules`)** | `30.4%` (`41/135`, `[23.2%, 38.6%]`) | `0.609` (`0.673` / `0.602`) | `99.3%` (`[95.9%, 99.9%]`) | `0` | `7.30` | `72,067` / `9,198` |
| **Arm B (`ontology`)** | `32.6%` (`44/135`, `[25.3%, 40.9%]`) | `0.734` (`0.880` / `0.700`) | `97.8%` (`[93.7%, 99.2%]`) | `0` | `8.91` | `103,271` / `11,299` |

Key observations under the claims policy above:

- **`L1` single-model parity:** All three arms score `26/30` (`86.7%`, `[70.3%, 94.7%]`) on the six `L1` single-model control questions (`0.0` pairwise difference).
- **Overall exact-answer accuracy is inconclusive (`excludes_zero = false`):** Paired bootstrap 95% intervals for `Arm B vs Arm A` (`+4.4%`, `[-0.7%, +11.1%]`), `Arm B vs Arm A+` (`+2.2%`, `[-0.7%, +6.7%]`), and `Arm A+ vs Arm A` (`+2.2%`, `[0.0%, +5.9%]`) all include zero, so no overall exact-match accuracy claim is made.
- **Unknown handling, cohort F1, effort, and multi-hop misses:** Both `Arm A+` and `Arm B` eliminate the `16` `missing-as-zero` (`R5`) trap hits observed in `Arm A`. `Arm B` achieves higher returned-key cohort F1 overall (`0.734`) and on `L2` (`0.869`), `N` (`0.927`, `13/20` exact vs `10/20` on `Arm A+` and `7/20` on `Arm A`), and `P` (`0.622`), while incurring higher tool-call (`8.91`) and input-token (`103,271`) overhead from ontology discovery calls. Strict exact-cell accuracy across `L3`, `L4`, and `P` (under workspace-admin identity, which bypasses viewer row-level security unless queried as the `East Region` viewer principal) was `0%` across all three arms; all `60` failed question-by-arm entries are listed in [scorecard.json](../../recordings/evaluation/2026-11-17/scorecard.json).

## Related documentation

- [Validation backlog](validation-backlog.md) — Twenty-four live demo tenant validation questions tracked from `eval/validation/register.json`.
- [ADR 0004: Three-arm evaluation and evidence-gated claims](../decisions/0004-evaluation-and-claims.md) — Architectural decision record governing the three-arm design and claims rules.
- [Unified context layer architecture](../architecture/README.md) — Target architecture and how cross-model questions are answered.
- [Service assurance scenario](../architecture/hero-scenario.md) — Four semantic models, seven-hop path, rules `R1` to `R5`, named traps, and golden question ladder.
- [Platform status](../architecture/platform-status.md) — Microsoft Learn preview and general availability status (checked 17 November 2026) and demo tenant readiness checks (`G1` to `G7`).
- [Limitations](../limitations.md) — Boundaries of synthetic data, offline templates, sanitized recordings, partial results, and preview features.
