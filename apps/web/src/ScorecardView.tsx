export type ScorecardExecutionMode = "offline_scripted" | "live_demo_tenant";

export type WilsonInterval = {
  proportion: number;
  low: number;
  high: number;
};

export type ScorecardConditions = {
  execution_mode: ScorecardExecutionMode;
  profile: string;
  seed: number | null;
  question_count: number;
  repeats: number;
  arms: string[];
  capacity_sku: string | null;
  region: string | null;
  model_deployment: string | null;
  toolset_variant: string | null;
  started_at: string | null;
  completed_at: string | null;
};

export type ScorecardEffort = {
  mean_tool_calls: number;
  mean_input_tokens: number;
  mean_output_tokens: number;
  mean_seconds: number;
  total_tokens: number;
};

export type ScorecardArmSummary = {
  arm: string;
  total_runs: number;
  question_count: number;
  repeats: number;
  answer_correct_runs: number;
  answer_accuracy: WilsonInterval;
  mean_key_precision: number;
  mean_key_recall: number;
  mean_key_f1: number;
  path_accuracy: WilsonInterval;
  unknown_handling_accuracy: WilsonInterval;
  mean_repeat_stability: number;
  unanimous_pass_rate: number;
  traps_hit: Record<string, number>;
  effort: ScorecardEffort;
};

export type ScorecardDifference = {
  comparison: string;
  left_arm: string;
  right_arm: string;
  metric: string;
  diff: number;
  low: number;
  high: number;
  excludes_zero: boolean;
};

export type ScorecardLevelArmSummary = {
  runs: number;
  answer_correct_runs: number;
  answer_accuracy: WilsonInterval;
  mean_key_f1: number;
  path_accuracy: WilsonInterval;
  unknown_handling_accuracy: WilsonInterval | null;
  mean_repeat_stability: number;
};

export type ScorecardLevelSummary = {
  question_count: number;
  arms: Record<string, ScorecardLevelArmSummary>;
  differences: ScorecardDifference[];
};

export type ScorecardFailedQuestion = {
  question_id: string;
  level: string;
  arm: string;
  correct_repeats: number;
  repeats: number;
  traps_hit: string[];
  failure_reasons: string[];
};

export type RecordedScorecard = {
  schema_version: "1.0";
  mode: "RECORDED_SCORECARD";
  generated_at: string;
  conditions: ScorecardConditions;
  arm_labels: Record<string, string>;
  summary_by_arm: Record<string, ScorecardArmSummary>;
  by_level: Record<string, ScorecardLevelSummary>;
  overall_differences: ScorecardDifference[];
  failed_questions: ScorecardFailedQuestion[];
  limitations: string[];
};

const CANONICAL_LEVELS = ["L1", "L2", "L3", "L4", "N", "P"] as const;
const LEVEL_TITLES: Record<string, string> = {
  L1: "Single-model measures",
  L2: "Two-model bridge and timing",
  L3: "Three-model attribution and caps",
  L4: "Four-model service assurance",
  N: "Negative and unknown data",
  P: "Paraphrase robustness",
};

const CANONICAL_TRAPS = [
  "site-key-only",
  "credit-per-incident",
  "revenue-fan-out",
  "display-name-or-billing-key",
  "missing-as-zero",
] as const;

const TRAP_DESCRIPTIONS: Record<string, string> = {
  "site-key-only": "Joining service-site or contract validity on site key alone without checking time overlap.",
  "credit-per-incident": "Summing SLA exposure per incident rather than per contract-month with cap rules applied once.",
  "revenue-fan-out": "Repeating account-month revenue or margin across multiple sites, services, or contracts.",
  "display-name-or-billing-key": "Matching accounts by display name or billing identifier instead of canonical account key.",
  "missing-as-zero": "Treating unmapped, missing, or excluded observations as measured zero.",
};

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function zoned(value: unknown): value is string {
  return typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
}

function nullableZoned(value: unknown): value is string | null | undefined {
  return value === null || value === undefined || zoned(value);
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function nullableString(value: unknown): value is string | null | undefined {
  return value === null || value === undefined || typeof value === "string";
}

function stringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === "string");
}

function nonEmptyStrings(value: unknown): value is string[] {
  return Array.isArray(value) && value.length > 0 && value.every(nonEmptyString);
}

function positiveInt(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 1;
}

function nonNegativeInt(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function nonNegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function unitFloat(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function diffFloat(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= -1 && value <= 1;
}

function isWilsonInterval(value: unknown): value is WilsonInterval {
  return object(value)
    && unitFloat(value.proportion)
    && unitFloat(value.low)
    && unitFloat(value.high)
    && value.low - value.proportion <= 1e-6
    && value.proportion - value.high <= 1e-6;
}

function isScorecardEffort(value: unknown): value is ScorecardEffort {
  return object(value)
    && nonNegativeNumber(value.mean_tool_calls)
    && nonNegativeNumber(value.mean_input_tokens)
    && nonNegativeNumber(value.mean_output_tokens)
    && nonNegativeNumber(value.mean_seconds)
    && nonNegativeInt(value.total_tokens);
}

function isScorecardArmSummary(value: unknown, expectedArm: string): value is ScorecardArmSummary {
  if (!object(value)
    || value.arm !== expectedArm
    || !positiveInt(value.total_runs)
    || !positiveInt(value.question_count)
    || !positiveInt(value.repeats)
    || !nonNegativeInt(value.answer_correct_runs)
    || value.answer_correct_runs > value.total_runs
    || !isWilsonInterval(value.answer_accuracy)
    || !unitFloat(value.mean_key_precision)
    || !unitFloat(value.mean_key_recall)
    || !unitFloat(value.mean_key_f1)
    || !isWilsonInterval(value.path_accuracy)
    || !isWilsonInterval(value.unknown_handling_accuracy)
    || !unitFloat(value.mean_repeat_stability)
    || !unitFloat(value.unanimous_pass_rate)
    || !object(value.traps_hit)
    || !Object.values(value.traps_hit).every(nonNegativeInt)
    || !isScorecardEffort(value.effort)) {
    return false;
  }
  return true;
}

function isScorecardDifference(value: unknown): value is ScorecardDifference {
  return object(value)
    && nonEmptyString(value.comparison)
    && nonEmptyString(value.left_arm)
    && nonEmptyString(value.right_arm)
    && nonEmptyString(value.metric)
    && diffFloat(value.diff)
    && diffFloat(value.low)
    && diffFloat(value.high)
    && value.low - value.diff <= 1e-6
    && value.diff - value.high <= 1e-6
    && typeof value.excludes_zero === "boolean";
}

function isScorecardLevelArmSummary(value: unknown): value is ScorecardLevelArmSummary {
  return object(value)
    && positiveInt(value.runs)
    && nonNegativeInt(value.answer_correct_runs)
    && value.answer_correct_runs <= value.runs
    && isWilsonInterval(value.answer_accuracy)
    && unitFloat(value.mean_key_f1)
    && isWilsonInterval(value.path_accuracy)
    && (value.unknown_handling_accuracy === null
      || value.unknown_handling_accuracy === undefined
      || isWilsonInterval(value.unknown_handling_accuracy))
    && unitFloat(value.mean_repeat_stability);
}

function isScorecardLevelSummary(value: unknown): value is ScorecardLevelSummary {
  return object(value)
    && positiveInt(value.question_count)
    && object(value.arms)
    && Object.keys(value.arms).length > 0
    && Object.values(value.arms).every(isScorecardLevelArmSummary)
    && Array.isArray(value.differences)
    && value.differences.every(isScorecardDifference);
}

function isScorecardFailedQuestion(value: unknown): value is ScorecardFailedQuestion {
  return object(value)
    && nonEmptyString(value.question_id)
    && nonEmptyString(value.level)
    && nonEmptyString(value.arm)
    && nonNegativeInt(value.correct_repeats)
    && positiveInt(value.repeats)
    && value.correct_repeats <= value.repeats
    && stringList(value.traps_hit)
    && stringList(value.failure_reasons);
}

export function isRecordedScorecard(value: unknown): value is RecordedScorecard {
  if (!object(value)
    || value.schema_version !== "1.0"
    || value.mode !== "RECORDED_SCORECARD"
    || !zoned(value.generated_at)
    || !object(value.conditions)
    || !object(value.arm_labels)
    || !object(value.summary_by_arm)
    || !object(value.by_level)
    || !Array.isArray(value.overall_differences)
    || !Array.isArray(value.failed_questions)
    || !nonEmptyStrings(value.limitations)) {
    return false;
  }

  const conditions = value.conditions;
  if ((conditions.execution_mode !== "offline_scripted" && conditions.execution_mode !== "live_demo_tenant")
    || !nonEmptyString(conditions.profile)
    || !(conditions.seed === null || conditions.seed === undefined
      || (typeof conditions.seed === "number" && Number.isSafeInteger(conditions.seed)))
    || !positiveInt(conditions.question_count)
    || !positiveInt(conditions.repeats)
    || !nonEmptyStrings(conditions.arms)
    || new Set(conditions.arms).size !== conditions.arms.length
    || !nullableString(conditions.capacity_sku)
    || !nullableString(conditions.region)
    || !nullableString(conditions.model_deployment)
    || !nullableString(conditions.toolset_variant)
    || !nullableZoned(conditions.started_at)
    || !nullableZoned(conditions.completed_at)) {
    return false;
  }

  const arms = conditions.arms;
  if (!Object.values(value.arm_labels).every(nonEmptyString)
    || !arms.every(arm => nonEmptyString((value.arm_labels as Record<string, unknown>)[arm]))) {
    return false;
  }

  const summaryKeys = Object.keys(value.summary_by_arm);
  if (summaryKeys.length !== arms.length
    || !arms.every(arm => isScorecardArmSummary((value.summary_by_arm as Record<string, unknown>)[arm], arm))) {
    return false;
  }

  const levelEntries = Object.values(value.by_level);
  if (levelEntries.length === 0 || !levelEntries.every(isScorecardLevelSummary)) {
    return false;
  }

  if (!value.overall_differences.every(isScorecardDifference)
    || !value.failed_questions.every(isScorecardFailedQuestion)) {
    return false;
  }

  return true;
}

function formatPct(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function formatSignedPct(value: number): string {
  const pct = value * 100;
  const normalized = Math.abs(pct) < 0.05 ? 0 : pct;
  return `${normalized > 0 ? "+" : ""}${normalized.toFixed(1)}%`;
}

function formatWilson(interval: WilsonInterval): string {
  return `${formatPct(interval.proportion)} [${formatPct(interval.low)}, ${formatPct(interval.high)}]`;
}

function displayCondition(value: string | number | null | undefined, fallback = "Not recorded"): string {
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim();
  return text.length > 0 ? text : fallback;
}

function armLabel(scorecard: RecordedScorecard, arm: string): string {
  return scorecard.arm_labels[arm] ?? arm;
}

function orderedLevels(byLevel: Record<string, ScorecardLevelSummary>): string[] {
  const present = Object.keys(byLevel);
  const canonical = CANONICAL_LEVELS.filter(level => level in byLevel);
  const extras = present.filter(level => !CANONICAL_LEVELS.includes(level as typeof CANONICAL_LEVELS[number]));
  return [...canonical, ...extras];
}

function orderedTraps(scorecard: RecordedScorecard): string[] {
  const observed = new Set<string>();
  for (const arm of scorecard.conditions.arms) {
    const traps = scorecard.summary_by_arm[arm]?.traps_hit ?? {};
    for (const trap of Object.keys(traps)) observed.add(trap);
  }
  const canonical = CANONICAL_TRAPS.filter(trap => observed.has(trap) || observed.size === 0);
  const extras = [...observed].filter(trap => !CANONICAL_TRAPS.includes(trap as typeof CANONICAL_TRAPS[number]));
  return [...new Set([...CANONICAL_TRAPS, ...canonical, ...extras])];
}

export function ScorecardView({ scorecard }: { scorecard: RecordedScorecard }) {
  const isLive = scorecard.conditions.execution_mode === "live_demo_tenant";
  const headerBadge = isLive ? "LIVE DEMO TENANT SCORECARD" : "OFFLINE SCRIPTED HARNESS CHECK";
  const arms = scorecard.conditions.arms;
  const levels = orderedLevels(scorecard.by_level);
  const traps = orderedTraps(scorecard);

  return <main className="scorecard-stage">
    <header>
      <div>
        <p className="eyebrow">NORTHSTAR WIRELESS / EVALUATION SCORECARD</p>
        <h1>Measured across evaluation arms, repeats, and question levels.</h1>
        <p>
          Generated {scorecard.generated_at} · {scorecard.conditions.question_count} questions × {scorecard.conditions.repeats} repeats per evaluation arm ({scorecard.conditions.profile}).
        </p>
      </div>
      <span className="badge" data-scorecard-badge={scorecard.conditions.execution_mode}>{headerBadge}</span>
    </header>

    <aside className="notice" data-execution-mode={scorecard.conditions.execution_mode}>
      {isLive ? (
        <>
          <strong>Live demo tenant scorecard:</strong> Sanitized evaluation output recorded from repeated runs against the demo tenant under shared model preparation, permissions, model deployment, and repeat count. Viewing this scorecard runs offline with no sign-in, model call, or Microsoft Fabric query.
        </>
      ) : (
        <>
          <strong>Offline scripted harness check:</strong> Deterministic local fixture output that verifies the oracle, trap detection, Wilson confidence intervals, and paired bootstrap calculations in continuous integration. Do not treat offline scripted numbers as live language-model or Microsoft Fabric performance.
        </>
      )}
    </aside>

    <section className="card" aria-label="Run conditions">
      <div className="section-heading">
        <div>
          <p className="eyebrow">SHARED EVALUATION PROTOCOL</p>
          <h2>Run conditions</h2>
        </div>
        <span className={isLive ? "status-pill observed" : "status-pill"}>
          {isLive ? "DEMO TENANT EXECUTION" : "OFFLINE SCRIPTED FIXTURE"}
        </span>
      </div>
      <dl className="scorecard-conditions">
        <div><dt>Execution mode</dt><dd>{scorecard.conditions.execution_mode}</dd></div>
        <div><dt>Profile</dt><dd>{scorecard.conditions.profile}</dd></div>
        <div><dt>Seed</dt><dd>{displayCondition(scorecard.conditions.seed)}</dd></div>
        <div><dt>Question count</dt><dd>{scorecard.conditions.question_count.toLocaleString("en-US")}</dd></div>
        <div><dt>Repeats</dt><dd>{scorecard.conditions.repeats.toLocaleString("en-US")}</dd></div>
        <div><dt>Model deployment</dt><dd>{displayCondition(scorecard.conditions.model_deployment)}</dd></div>
        <div><dt>Toolset variant</dt><dd>{displayCondition(scorecard.conditions.toolset_variant)}</dd></div>
        <div><dt>Capacity SKU</dt><dd>{displayCondition(scorecard.conditions.capacity_sku)}</dd></div>
        <div><dt>Region</dt><dd>{displayCondition(scorecard.conditions.region)}</dd></div>
        <div><dt>Started at</dt><dd>{displayCondition(scorecard.conditions.started_at)}</dd></div>
        <div><dt>Completed at</dt><dd>{displayCondition(scorecard.conditions.completed_at)}</dd></div>
        <div><dt>Evaluation arms</dt><dd>{arms.map(arm => armLabel(scorecard, arm)).join(" · ")}</dd></div>
      </dl>
    </section>

    <section className="card" aria-label="Overall summary by evaluation arm">
      <div className="section-heading">
        <div>
          <p className="eyebrow">ACCURACY, STABILITY, AND UNKNOWN HANDLING</p>
          <h2>Overall summary by evaluation arm</h2>
        </div>
        <span className="status-pill">95% WILSON INTERVALS</span>
      </div>
      <div className="table-scroll">
        <table aria-label="Overall summary by evaluation arm">
          <thead>
            <tr>
              <th>Evaluation arm</th>
              <th>Answer accuracy</th>
              <th>Mean key F1</th>
              <th>Path accuracy</th>
              <th>Unknown handling</th>
              <th>Repeat stability</th>
              <th>Unanimous pass rate</th>
            </tr>
          </thead>
          <tbody>
            {arms.map(arm => {
              const summary = scorecard.summary_by_arm[arm];
              return <tr key={arm} data-arm-row={arm}>
                <td>
                  <strong>{armLabel(scorecard, arm)}</strong>
                  <span className="scorecard-cell-detail">
                    {summary.answer_correct_runs.toLocaleString("en-US")} / {summary.total_runs.toLocaleString("en-US")} correct runs
                  </span>
                </td>
                <td>{formatWilson(summary.answer_accuracy)}</td>
                <td>
                  {formatPct(summary.mean_key_f1)}
                  <span className="scorecard-cell-detail">
                    P {formatPct(summary.mean_key_precision)} · R {formatPct(summary.mean_key_recall)}
                  </span>
                </td>
                <td>{formatWilson(summary.path_accuracy)}</td>
                <td>{formatWilson(summary.unknown_handling_accuracy)}</td>
                <td>{formatPct(summary.mean_repeat_stability)}</td>
                <td>{formatPct(summary.unanimous_pass_rate)}</td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>
    </section>

    <section className="card" aria-label="Paired bootstrap differences">
      <div className="section-heading">
        <div>
          <p className="eyebrow">CLAIMS POLICY</p>
          <h2>Paired bootstrap differences</h2>
        </div>
        <span className="status-pill">95% PAIRED BOOTSTRAP</span>
      </div>
      <p className="muted">
        Product-benefit statements require a paired bootstrap interval that excludes zero. Ties and intervals crossing zero remain published without overclaiming.
      </p>
      {scorecard.overall_differences.length === 0 ? (
        <p>No paired arm comparisons recorded in this scorecard.</p>
      ) : (
        <div className="table-scroll">
          <table aria-label="Paired bootstrap differences">
            <thead>
              <tr>
                <th>Comparison</th>
                <th>Difference</th>
                <th>95% bootstrap interval</th>
                <th>Interval status</th>
              </tr>
            </thead>
            <tbody>
              {scorecard.overall_differences.map(item => (
                <tr key={`${item.comparison}:${item.metric}`} data-comparison-row={item.comparison}>
                  <td>
                    <strong>{armLabel(scorecard, item.left_arm)} vs. {armLabel(scorecard, item.right_arm)}</strong>
                    <span className="scorecard-cell-detail">{item.comparison} · {item.metric}</span>
                  </td>
                  <td>{formatSignedPct(item.diff)}</td>
                  <td>[{formatSignedPct(item.low)}, {formatSignedPct(item.high)}]</td>
                  <td>
                    <span className={item.excludes_zero ? "status-pill observed" : "status-pill"}>
                      {item.excludes_zero ? "Interval excludes zero" : "Tie / includes zero"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>

    <section className="card" aria-label="Results by question level">
      <div className="section-heading">
        <div>
          <p className="eyebrow">QUESTION LADDER · L1 TO P</p>
          <h2>Results by question level</h2>
        </div>
        <span className="status-pill">{levels.length} LEVELS</span>
      </div>
      <div className="table-scroll">
        <table aria-label="Results by question level">
          <thead>
            <tr>
              <th>Level</th>
              <th>Questions</th>
              {arms.map(arm => <th key={arm}>{armLabel(scorecard, arm)}</th>)}
              <th>Paired interval check</th>
            </tr>
          </thead>
          <tbody>
            {levels.map(level => {
              const entry = scorecard.by_level[level];
              return <tr key={level} data-level-row={level}>
                <td>
                  <strong>{level}</strong>
                  {LEVEL_TITLES[level] && <span className="scorecard-cell-detail">{LEVEL_TITLES[level]}</span>}
                </td>
                <td>{entry.question_count.toLocaleString("en-US")}</td>
                {arms.map(arm => {
                  const levelArm = entry.arms[arm];
                  if (!levelArm) return <td key={arm}>—</td>;
                  return <td key={arm}>
                    <strong>{formatWilson(levelArm.answer_accuracy)}</strong>
                    <span className="scorecard-cell-detail">
                      {levelArm.answer_correct_runs}/{levelArm.runs} runs · F1 {formatPct(levelArm.mean_key_f1)} · Path {formatPct(levelArm.path_accuracy.proportion)}
                      {levelArm.unknown_handling_accuracy ? ` · Unknown ${formatPct(levelArm.unknown_handling_accuracy.proportion)}` : ""}
                      {` · Stability ${formatPct(levelArm.mean_repeat_stability)}`}
                    </span>
                  </td>;
                })}
                <td>
                  {entry.differences.length === 0 ? (
                    <span className="muted">Not applicable</span>
                  ) : (
                    entry.differences.map(diff => (
                      <div key={`${level}:${diff.comparison}:${diff.metric}`} className="scorecard-level-diff">
                        <code>{diff.comparison}</code>: {formatSignedPct(diff.diff)}{" "}
                        <span className={diff.excludes_zero ? "status-pill observed" : "status-pill"}>
                          {diff.excludes_zero ? "Interval excludes zero" : "Tie / includes zero"}
                        </span>
                      </div>
                    ))
                  )}
                </td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>
    </section>

    <section className="card" aria-label="Grain and validity traps hit">
      <div className="section-heading">
        <div>
          <p className="eyebrow">GRAIN, VALIDITY, AND COMPLETENESS</p>
          <h2>Grain and validity traps hit</h2>
        </div>
        <span className="status-pill">LOWER IS BETTER</span>
      </div>
      <div className="table-scroll">
        <table aria-label="Grain and validity traps hit">
          <thead>
            <tr>
              <th>Trap</th>
              <th>Failure pattern</th>
              {arms.map(arm => <th key={arm}>{armLabel(scorecard, arm)}</th>)}
            </tr>
          </thead>
          <tbody>
            {traps.map(trap => (
              <tr key={trap} data-trap-row={trap}>
                <td><code>{trap}</code></td>
                <td>{TRAP_DESCRIPTIONS[trap] ?? "Observed evaluation trap pattern."}</td>
                {arms.map(arm => {
                  const hits = scorecard.summary_by_arm[arm]?.traps_hit[trap] ?? 0;
                  return <td key={arm}>{hits.toLocaleString("en-US")}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>

    <section className="card" aria-label="Effort comparison">
      <div className="section-heading">
        <div>
          <p className="eyebrow">TOOL CALLS, TOKENS, AND LATENCY</p>
          <h2>Effort comparison</h2>
        </div>
        <span className="status-pill">PER RUN AVERAGES</span>
      </div>
      <div className="table-scroll">
        <table aria-label="Effort comparison">
          <thead>
            <tr>
              <th>Evaluation arm</th>
              <th>Mean tool calls</th>
              <th>Mean input tokens</th>
              <th>Mean output tokens</th>
              <th>Mean seconds</th>
              <th>Total tokens</th>
            </tr>
          </thead>
          <tbody>
            {arms.map(arm => {
              const effort = scorecard.summary_by_arm[arm].effort;
              return <tr key={arm} data-effort-row={arm}>
                <td><strong>{armLabel(scorecard, arm)}</strong></td>
                <td>{effort.mean_tool_calls.toFixed(1)}</td>
                <td>{effort.mean_input_tokens.toLocaleString("en-US", { maximumFractionDigits: 1 })}</td>
                <td>{effort.mean_output_tokens.toLocaleString("en-US", { maximumFractionDigits: 1 })}</td>
                <td>{effort.mean_seconds.toFixed(1)} s</td>
                <td>{effort.total_tokens.toLocaleString("en-US")}</td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>
    </section>

    <section className="card" aria-label="Failed questions">
      <div className="section-heading">
        <div>
          <p className="eyebrow">DIAGNOSTIC BREAKDOWN</p>
          <h2>Failed questions</h2>
        </div>
        <span className="status-pill">{scorecard.failed_questions.length.toLocaleString("en-US")} ENTRIES</span>
      </div>
      {scorecard.failed_questions.length === 0 ? (
        <p>No failed questions recorded across any evaluation arm.</p>
      ) : (
        <div className="table-scroll">
          <table aria-label="Failed questions">
            <thead>
              <tr>
                <th>Question ID</th>
                <th>Level</th>
                <th>Evaluation arm</th>
                <th>Correct repeats</th>
                <th>Traps hit</th>
                <th>Failure reasons</th>
              </tr>
            </thead>
            <tbody>
              {scorecard.failed_questions.map(item => (
                <tr key={`${item.question_id}:${item.arm}`} data-failed-question={`${item.question_id}:${item.arm}`}>
                  <td><code>{item.question_id}</code></td>
                  <td>{item.level}</td>
                  <td>{armLabel(scorecard, item.arm)}</td>
                  <td>{item.correct_repeats} / {item.repeats}</td>
                  <td>{item.traps_hit.length > 0 ? item.traps_hit.join(", ") : "None"}</td>
                  <td>{item.failure_reasons.length > 0 ? item.failure_reasons.join(", ") : "None"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>

    <footer>
      <p><strong>Limits of this recording</strong> (see <code>docs/limitations.md</code>):</p>
      {scorecard.limitations.map(item => <p key={item}>{item}</p>)}
    </footer>
  </main>;
}
