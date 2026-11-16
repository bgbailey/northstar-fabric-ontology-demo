import { useCallback, useEffect, useRef, useState } from "react";
import { ExecutionCanvas } from "./ExecutionCanvas";
import { MetricInsight } from "./MetricInsight";
import { dependencies, isResult, isTrace, planNodes, type AnswerView, type ExecutionEvent, type ExecutionResult, type PlanNodeView } from "./execution";
import { executionLabel, isReadiness, readinessStages, type Readiness } from "./readiness";

type Arm = "baseline" | "native";
type Outcome = {
  arm: Arm; status: string; plan: unknown; planning_receipt: Record<string, unknown> | null;
  execution: ExecutionResult | null; errors: string[]; elapsed_ms: number;
};
export type PairResult = {
  pair_id: string; question: string; provider: string; model_version: string; mode?: string;
  source_facts_hash: string; context_revision: string; arms: Outcome[];
  comparability: string; comparability_reasons: string[]; limitations: string[];
};
type ArmView = { nodes: PlanNodeView[]; events: ExecutionEvent[]; phase: string; outcome?: Outcome };
const stories = [
  {
    label: "Getting started",
    models: "3 domains / 2 models: Customers, Subscriptions, Finance",
    question: "For July 2026, show each registered customer account's source-owned Active Lines EOM from Subscriptions and Recognized Service Revenue and Contribution Margin from Finance, in cents. Use only these two semantic models. Connect their actual account dimensions through the reviewed shared customer identity and retain one account-month row. Active Lines means the month-end snapshot, not activations or a sum of monthly snapshots. Revenue means recognized service revenue, not billed charges or booked merchandise. Keep accounts with zero or unknown values distinct; never substitute zero for missing or unauthorized data. Explain the source measure ownership and additional customer, period and revenue-meaning context used. Shared execution-format clarification: ResolveConcept, ReadModelSchema and SelectMetric references must be source catalog model IDs, table IDs or metric IDs only. Ontology entity, Rule and relationship IDs in Fabric remain planning context but are not allowed values in those no-data operator references. Keep source ownership and interpretation limits in hypotheses. This requirement applies equally to both approaches and supplies no answer, source values or query itinerary.",
  },
  {
    label: "SLA exposure",
    models: "Network / Enterprise / Finance",
    question: "For July 2026, identify known-mapped enterprise contracts exposed to network incidents overlapping both serving-site and contract validity intervals. Return source-owned Credit Exposure at contract-month grain with original coverage warnings and unknown inputs preserved; incomplete credit values are not a complete exposure total or a credit-based ranking. In a separate account-month view, prioritize the top 10 known-mapped affected accounts by source-owned contribution margin descending, alongside recognized service revenue in cents. Report unknown-mapping exclusions and label known-mapped cohorts as qualified partial subsets. Do not duplicate Finance across contracts, allocate monthly credits to an individual incident, or turn unknown source inputs into zero. Shared execution-format clarification: SemiJoinCohort preserves only its left rows and cannot output per-match overlap columns; leave overlap_start and overlap_end absent for that operator. Only an actual JoinResults may retain per-match temporal intersections. Final separate-view labels should be short, at most40characters, and must not repeat the primary or another source node. These are equal protocol requirements for both approaches, not a source itinerary or a supplied answer.",
  },
  {
    label: "Incident recovery",
    models: "Network / Subscriptions / Care / Finance / Growth",
    question: "For July 2026, identify known-mapped accounts with incident-exposed subscriptions using serving-site validity, summarize opening-cohort churn, repeat care contacts and source-owned contribution margin, and show eligible randomized retention-offer evidence for the August 2026 assignment period. Return the July account assessment and August campaign/assignment evidence as separate source-grain views. Report unknown-mapping exclusions and label known-mapped cohort results as qualified partial subsets, not complete populations. Keep assignment units and treatment/control denominators explicit, preserve consent exclusions, and do not infer causality from incident or care association. Shared execution-format clarification: SemiJoinCohort preserves only its left rows and cannot output per-match overlap columns; leave overlap_start and overlap_end absent for that operator. Only an actual JoinResults may retain per-match temporal intersections. Final separate-view labels should be short, at most40characters, and must not repeat the primary or another source node. These are equal protocol requirements for both approaches, not a source itinerary or a supplied answer.",
  },
  {
    label: "Checkout release",
    models: "Digital / Commerce / Growth / Finance",
    question: "Compare June and July 2026 source-owned eligible checkout conversion and median journey duration by month, release and device, keeping all eligible sessions including anonymous sessions in the governed denominator. Distinguish anonymous or unmapped sessions and relate orders only through their explicit session key. Attribution and financial cohorts may use known mapped identities only with separately reported exclusions and qualified partial labels, never complete population totals. Separate booked merchandise value from recognized service revenue, show fulfillment, return and campaign-mix evidence without double counting, and state what is insufficient to claim that the release caused harm. Use separate release/device, linked-order, campaign and account-month views where their grains differ; account-month service revenue is not checkout attribution. Shared execution-format clarification: SemiJoinCohort preserves only its left rows and cannot output per-match overlap columns; leave overlap_start and overlap_end absent for that operator. Only an actual JoinResults may retain per-match temporal intersections. Final separate-view labels should be short, at most40characters, and must not repeat the primary or another source node. These are equal protocol requirements for both approaches, not a source itinerary or a supplied answer. For both approaches, scope the supporting session-row evidence to checkout-eligible journeys too: non-checkout activity is outside this business question. Keep EVERY eligible journey, including anonymous/unmapped customers and sessions without an order, in the source-owned performance denominator. Do not filter by authentication, account identity or successful purchase. Missing explicit session/order/customer links remain unknown and reported, not zero. Keep the two original months and source-owned measures unchanged.",
  },
] as const;
const initialArms = (): Record<Arm, ArmView> => ({
  baseline: { nodes: [], events: [], phase: "IDLE" },
  native: { nodes: [], events: [], phase: "IDLE" },
});
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === "string");
}
function Qualification({ answer }: { answer: AnswerView }) {
  return <>
    {answer.population_qualification === "known_mapped_subset" &&
      <p className="notice">Known mapped subset only — partial result (unmapped observations and null-key rows excluded), not a complete population ranking or total.</p>}
    {answer.exclusions?.map((item, index) => <p className="muted" key={index}>
      {item.source_node}: {item.unknown_mapping_observations} unknown mapping observations; {item.null_key_rows} null-key rows excluded.
      These are separate diagnostics, not a count of distinct accounts.
    </p>)}
  </>;
}
function isOutcome(value: unknown): value is Outcome {
  return object(value) && (value.arm === "baseline" || value.arm === "native")
    && typeof value.status === "string" && strings(value.errors) && typeof value.elapsed_ms === "number"
    && Number.isFinite(value.elapsed_ms) && value.elapsed_ms >= 0
    && (value.planning_receipt === null || object(value.planning_receipt))
    && (value.execution === null || isResult(value.execution));
}
export function isPairResult(value: unknown): value is PairResult {
  return object(value) && ["pair_id", "question", "provider", "model_version", "source_facts_hash", "context_revision"]
    .every(key => typeof value[key] === "string") && Array.isArray(value.arms) && value.arms.every(isOutcome)
    && value.arms.length === 2 && new Set(value.arms.map(arm => arm.arm)).size === 2
    && (value.mode === undefined || typeof value.mode === "string")
    && ["NOT_COMPARABLE", "COMMON_SOURCE_STATE"].includes(String(value.comparability))
    && strings(value.comparability_reasons) && strings(value.limitations);
}
function outcomeBlockedAuth(value: unknown): boolean {
  return object(value) && (value.status === "BLOCKED_AUTH"
    || (object(value.execution) && value.execution.status === "BLOCKED_AUTH"));
}
function authorizationBlocked(value: Record<string, unknown>): boolean {
  return outcomeBlockedAuth(value) || outcomeBlockedAuth(value.outcome)
    || (object(value.event) && value.event.status === "BLOCKED_AUTH")
    || (Array.isArray(value.arms) && value.arms.some(outcomeBlockedAuth));
}
export function ComparisonView({ csrf, authenticated, recording, recordedEvents, metricLabels = {} }: {
  csrf: string | null; authenticated: boolean; recording?: PairResult;
  recordedEvents?: Record<Arm, ExecutionEvent[]>;
  metricLabels?: Record<string, string>;
}) {
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [question, setQuestion] = useState("");
  const [arms, setArms] = useState(initialArms);
  const [result, setResult] = useState<PairResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const currentReader = useRef(authenticated);
  const generation = useRef(0);
  const verified = useRef(false);
  const columnLabel = (column: string) => metricLabels[column]
    ?? column.replaceAll("_", " ").replace(".", " · ");
  const cellLabel = (value: unknown) => value === null || value === undefined ? "UNKNOWN"
    : typeof value === "number" && Number.isInteger(value) ? value.toLocaleString("en-US") : String(value);
  currentReader.current = authenticated && recording === undefined;
  const clearRun = useCallback(() => {
    generation.current += 1; verified.current = false;
    const abort = controller.current; controller.current = null;
    abort?.abort();
    setArms(initialArms()); setResult(null); setBusy(false);
  }, []);
  const invalidateAuthorization = useCallback((message: string) => {
    clearRun();
    setReadiness(previous => ({
      ...previous, ready: false, status: "BLOCKED_AUTH", blockers: [message],
      reader_session: "UNKNOWN", native_source_probe: "BLOCKED_AUTH",
      application_execution: "NOT_ESTABLISHED",
      claim: "Server authorization was refused. Fresh session/readiness verification is required before another attempt; configuration is not current source authorization.",
    }));
    setError(message);
  }, [clearRun]);
  useEffect(() => {
    if (recording) {
      controller.current?.abort(); generation.current += 1;
      const next = initialArms();
      for (const outcome of recording.arms) next[outcome.arm] = {
        nodes: planNodes(outcome.plan), events: recordedEvents?.[outcome.arm] ?? [],
        phase: "RECORDED", outcome,
      };
      setArms(next); setResult(recording); setBusy(false); setError("");
      return;
    }
    if (!authenticated) {
      invalidateAuthorization("Demo user authorization ended; both evaluation arm answers were discarded.");
    } else {
      clearRun(); setReadiness(null); setError("");
    }
  }, [authenticated, csrf, clearRun, invalidateAuthorization, recording, recordedEvents]);
  useEffect(() => {
    if (recording) return;
    const abort = new AbortController();
    let checking = false;
    const check = async () => {
      if (checking || !currentReader.current) return;
      checking = true;
      const checkedGeneration = generation.current;
      try {
        const response = await fetch("/api/comparison/readiness", { signal: abort.signal });
        if (abort.signal.aborted || generation.current !== checkedGeneration) return;
        if (response.status === 401 || response.status === 403) {
          invalidateAuthorization(`Server authorization refused during verification (HTTP ${response.status}); both evaluation arm answers were discarded.`);
          return;
        }
        const value: unknown = await response.json();
        if (abort.signal.aborted || generation.current !== checkedGeneration || !currentReader.current) return;
        if (!response.ok || !isReadiness(value)) throw new Error("Cannot verify comparison readiness.");
        if (value.status === "BLOCKED_AUTH") {
          invalidateAuthorization("Server authorization remains blocked; both evaluation arm answers were discarded.");
          return;
        }
        if (value.ready) {
          if (!verified.current) setError("");
          verified.current = true;
        } else {
          clearRun();
        }
        setReadiness(value);
      } catch (reason: unknown) {
        if (!abort.signal.aborted && generation.current === checkedGeneration) {
          clearRun(); setReadiness(null);
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      } finally { checking = false; }
    };
    void check();
    window.addEventListener("focus", check);
    const interval = window.setInterval(check, 30_000);
    return () => {
      abort.abort(); generation.current += 1; verified.current = false;
      controller.current?.abort(); controller.current = null;
      window.removeEventListener("focus", check); window.clearInterval(interval);
    };
  }, [authenticated, csrf, clearRun, invalidateAuthorization, recording]);

  async function run() {
    if (recording) {
      setError("Recorded playback cannot execute a source or model request.");
      return;
    }
    if (!authenticated || !verified.current || !readiness?.ready || readiness.blockers.length || !question.trim()) return;
    setBusy(true); setError(""); setResult(null); setArms(initialArms());
    const abort = new AbortController(); controller.current = abort;
    const runGeneration = generation.current;
    const isCurrentRun = () => generation.current === runGeneration && controller.current === abort;
    const requireCurrentRun = () => {
      abort.signal.throwIfAborted();
      if (!isCurrentRun() || !currentReader.current) throw new Error("Demo user authorization ended; no answer accepted.");
    };
    let pairId: string | null = null;
    let terminal = false;
    try {
      const response = await fetch("/api/compare/stream", {
        method: "POST", signal: abort.signal,
        headers: { "Content-Type": "application/json", ...(csrf ? { "X-CSRF-Token": csrf } : {}) },
        body: JSON.stringify({ question }),
      });
      requireCurrentRun();
      if (response.status === 401 || response.status === 403) {
        invalidateAuthorization(`Server authorization refused (HTTP ${response.status}); both evaluation arm answers were discarded.`);
        return;
      }
      if (!response.ok) throw new Error(await response.text());
      if (!response.body || !response.headers.get("content-type")?.startsWith("text/event-stream")) {
        throw new Error("No comparison stream returned.");
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: true });
      let buffer = "";
      try {
        while (true) {
          const { value, done } = await reader.read();
          requireCurrentRun();
          buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
          buffer = buffer.replace(/\r\n/g, "\n");
          let boundary: number;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, boundary).split("\n"); buffer = buffer.slice(boundary + 2);
            const kind = frame.find(line => line.startsWith("event:"))?.slice(6).trim();
            const text = frame.filter(line => line.startsWith("data:")).map(line => line.slice(5).trim()).join("\n");
            if (!text) continue;
            const data: unknown = JSON.parse(text);
            if (!object(data)) throw new Error("Invalid comparison event.");
            if (authorizationBlocked(data)) {
              invalidateAuthorization("Server blocked demo user or source authorization; both evaluation arm answers were discarded.");
              return;
            }
            if (kind === "error") throw new Error(typeof data.detail === "string" ? data.detail : "Comparison failed.");
            if (terminal || typeof data.pair_id !== "string") throw new Error("Invalid comparison event identity.");
            pairId ??= data.pair_id;
            if (data.pair_id !== pairId) throw new Error("Comparison stream changed run identity.");
            if (kind === "pair" && (data.arm === "baseline" || data.arm === "native") && typeof data.phase === "string") {
              const arm = data.arm; const phase = data.phase;
              const nodes = data.plan ? planNodes(data.plan) : null;
              const event = data.event;
              const outcome = data.outcome;
              if (event !== null && event !== undefined && !isTrace(event)) {
                throw new Error("Invalid source execution event.");
              }
              if (outcome !== null && outcome !== undefined && (!isOutcome(outcome) || outcome.arm !== arm)) {
                throw new Error("Invalid comparison outcome.");
              }
              setArms(previous => ({
                ...previous, [arm]: {
                  ...previous[arm], phase,
                  ...(nodes ? { nodes } : {}),
                  ...(isTrace(event) ? { events: [...previous[arm].events, event] } : {}),
                  ...(isOutcome(outcome) ? { outcome } : {}),
                },
              }));
            } else if (kind === "comparison" && isPairResult(data)) {
              terminal = true; setResult(data);
              setArms(previous => {
                const next = { ...previous };
                for (const outcome of data.arms) {
                  next[outcome.arm] = { ...previous[outcome.arm], phase: "arm_finished", outcome };
                }
                return next;
              });
            } else throw new Error("Unrecognized comparison stream event.");
          }
          if (done) break;
        }
        if (!terminal || buffer.trim()) throw new Error("Comparison stream ended without a final result.");
      } finally { reader.releaseLock(); }
    } catch (reason) {
      abort.abort();
      if (isCurrentRun()) {
        setArms(initialArms()); setResult(null);
        setError(reason instanceof Error && reason.name === "AbortError"
          ? "Comparison cancelled. No complete pair was accepted." : String(reason));
      }
    } finally {
      if (isCurrentRun()) { controller.current = null; setBusy(false); }
    }
  }
  return <section>
    {!recording && <div className="card">
      <h2>Ask both approaches</h2>
      <p>One real application model across two evaluation arms, the same source facts, AI preparation and budgets. No fixture or recorded-answer fallback.</p>
      <div className="story-presets" role="group" aria-label="Customer story questions">
        {stories.map(story => <button key={story.label} aria-label={story.label}
          aria-pressed={question === story.question} disabled={busy} onClick={() => setQuestion(story.question)}>
          <strong>{story.label}</strong><small>{story.models}</small>
        </button>)}
      </div>
      <p className="muted">Editable example questions, not precomputed answers. Selecting a story starts no model or source call.</p>
      <aside className="notice">
        <strong>{readiness?.status ?? "CHECKING_READINESS"}</strong>
        <ul>{readiness?.blockers.map(blocker => <li key={blocker}>{blocker}</li>)}</ul>
        <p>{readiness?.claim}</p>
        {readiness && <details><summary>Readiness evidence (not a run)</summary>
          <dl>{readinessStages(readiness).map(([label, state]) =>
            <div key={label}><dt>{label}</dt><dd>{state}</dd></div>)}</dl>
        </details>}
      </aside>
      <label htmlFor="paired-question">Business question</label>
      <textarea id="paired-question" className="paired-question" value={question} disabled={busy}
        onChange={event => setQuestion(event.target.value)}
        placeholder="Ask about affected contracts, governed credit exposure and account financial priority." />
      <button className="primary" disabled={busy || !readiness?.ready || !authenticated || !question.trim()} onClick={run}>
        {busy ? "Planning and executing..." : "Run both approaches"}
      </button>
      {busy && <button onClick={() => controller.current?.abort()}>Cancel comparison</button>}
      {!authenticated && <p className="muted">A current approved demo user sign-in to the demo tenant is required for live comparison.</p>}
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
    {result && <div className="card"><h2>Source-backed business results</h2>
      <p>{result.question.split(". ")[0]}.</p>
      <details><summary>Exact submitted business question</summary><p>{result.question}</p></details>
      <p>Run {result.pair_id} · {recording ? "RECORDED original live execution"
        : result.mode ? executionLabel(result.mode) : "See each arm's execution provenance"}</p>
      <p className="muted">{recording
        ? "Captured numeric values were checked against original source receipts. "
        : "Execution alone is not independent numerical validation. "}
        No accuracy or ontology-context advantage is inferred.</p>
    </div>}
    <div className="comparison-grid">{(["baseline", "native"] as const).map(arm => {
      const view = arms[arm]; const answer = view.outcome?.execution?.answer;
      const columns = [...new Set(answer?.rows.flatMap(row => Object.keys(row)) ?? [])]
        .filter(column => !column.startsWith("__right_"));
      const hypotheses = object(view.outcome?.plan) && strings(view.outcome.plan.hypotheses)
        ? view.outcome.plan.hypotheses : [];
      const rejected = view.outcome?.planning_receipt?.validation_status === "REJECTED_NOT_EXECUTED"
        && view.outcome.plan === null && view.outcome.execution === null;
      const rejectedNodes = rejected ? planNodes(view.outcome?.planning_receipt?.rejected_candidate) : [];
      const candidate = view.outcome?.planning_receipt?.rejected_candidate;
      const clarification = rejected && view.outcome?.status === "AMBIGUOUS" && object(candidate)
        && strings(candidate.hypotheses) ? candidate.hypotheses : [];
      const binding = view.outcome?.planning_receipt?.column_binding;
      const generatedColumns = object(binding) && binding.mode === "source_qualified" && binding.status === "COMPILED"
        && Array.isArray(binding.generated_projection_nodes)
        && binding.generated_projection_nodes.every((value: unknown) => typeof value === "string")
        ? binding.generated_projection_nodes : null;
      const statusPill = view.outcome?.status === "PARTIAL" ? "partial result" : (view.outcome?.status ?? view.phase);
      return <article key={arm}>
        <div className="card"><div className="section-heading">
          <h2>{arm === "baseline" ? "Models & flat guidance" : "Ontology context in Fabric"}</h2>
          <span className="status-pill">{statusPill}</span></div>
          {view.outcome?.execution && <p>{recording ? "RECORDED original live execution"
            : executionLabel(view.outcome.execution.mode)} · {
            recording ? "Historical terminal pair; not current source authorization"
              : result ? "Terminal pair received" : "Provisional arm output; no terminal pair accepted"}</p>}
          {view.outcome?.errors.map(message => <p key={message} className="error">{message}</p>)}
          {view.outcome?.execution?.errors.map(message => <p key={message} className="error">{message}</p>)}
          {view.outcome && !answer && <p>No business answer returned; missing or blocked is not zero.</p>}
          {generatedColumns !== null && <aside className="notice" aria-label="Server column binding">
            <strong>Server-bound column names</strong>
            <p>{generatedColumns.length} compiler-generated column projections; source choices and calculations are unchanged.
              Column binding adds no model or source calls. Execution receipts below show what actually ran.</p>
          </aside>}
          {rejected && <aside className="notice" aria-label="Rejected proposal diagnostic">
            <strong>{clarification.length ? "Clarification required - no source queries executed" : "Rejected proposal - not executed"}</strong>
            {clarification.length ? clarification.map(question => <p key={question}>{question}</p>)
              : <p>Validation stopped this proposal before source execution. These nodes are diagnostics, not an accepted plan or an answer.</p>}
            <details><summary>Inspect rejected proposal nodes</summary>
              {rejectedNodes.length > 0 ? <div className="table-scroll">
                <table aria-label={`${arm} rejected proposal nodes`}><thead><tr>
                  <th>Node</th><th>Operation</th><th>References</th>
                </tr></thead><tbody>{rejectedNodes.map((node, index) => <tr key={`${index}:${node.id}`}>
                  <td>{node.id}</td><td>{node.kind}</td><td>{dependencies(node).join(", ") || "None"}</td>
                </tr>)}</tbody></table>
              </div> : <p>Rejected node details are incomplete. Inspect the planning receipt for the retained original.</p>}
            </details>
          </aside>}
          {answer && view.outcome?.execution?.status !== "COMPLETE" &&
            <p className="notice">{view.outcome?.execution?.status === "PARTIAL" ? "Partial result (incomplete or unmapped rows excluded)." : `Incomplete result (${view.outcome?.execution?.status}).`} These rows are not a complete financial total.</p>}
          {answer && <Qualification answer={answer} />}
          {answer && <MetricInsight answer={answer} />}
          {answer?.status && <p className="muted">Primary source view: {answer.status === "PARTIAL" ? "partial result (see exclusions and coverage)" : answer.status}</p>}
          {answer?.rows.length === 0 && <p>Empty scoped result; no measured financial zero is inferred.</p>}
          {answer && <div className="table-scroll"><table aria-label={`${arm} business answer`}><thead><tr>{columns.map(column =>
            <th key={column} title={column}>{columnLabel(column)}</th>)}</tr></thead><tbody>{answer.rows.map((row, index) =>
            <tr key={index}>{columns.map(column => <td key={column} title={String(row[column])}>
              {cellLabel(row[column])}
            </td>)}</tr>)}</tbody></table></div>}
          {answer && <><details><summary>Coverage and original result fields</summary>
            <pre>Coverage: {JSON.stringify(answer.coverage)}</pre>
            <pre>{JSON.stringify(answer.rows, null, 2)}</pre>
          </details>
            {[...new Set(answer.warnings)].map(warning => <p className="muted" key={warning}>{warning}</p>)}
            {answer.clocks && <details><summary>Data, source model, ontology and context clocks</summary>
              <pre>{JSON.stringify({ clocks: answer.clocks, sources: answer.source_clocks }, null, 2)}</pre>
            </details>}</>}
          {Object.entries(view.outcome?.execution?.additional_answers ?? {}).map(([label, separate]) => {
            const fields = [...new Set(separate.rows.flatMap(row => Object.keys(row)))]
              .filter(column => !column.startsWith("__right_"));
            const separateStatus = separate.status === "PARTIAL"
              ? "partial result (unmapped or incomplete rows excluded)" : separate.status;
            return <section key={label} aria-label={`${arm} ${label} result`}>
              <details><summary>{label} · {separate.rows.length} rows · {separateStatus}</summary>
              <p>{separateStatus} · Source grain: {separate.grain == null ? "Not preserved"
                : separate.grain.length ? separate.grain.join(" + ") : "Scalar"}</p>
              <p className="muted">Separate source-grain view. These rows are not joined to or added into the primary table.</p>
              {separate.status !== "COMPLETE" && <p className="notice">Incomplete source view; no complete total is implied.</p>}
              <Qualification answer={separate} />
              <div className="table-scroll"><table aria-label={`${arm} ${label} business answer`}>
                <thead><tr>{fields.map(field => <th key={field} title={field}>{columnLabel(field)}</th>)}</tr></thead>
                <tbody>{separate.rows.map((row, index) => <tr key={index}>{fields.map(field =>
                  <td key={field} title={String(row[field])}>{cellLabel(row[field])}</td>)}</tr>)}</tbody>
              </table></div>
              <pre>Coverage: {JSON.stringify(separate.coverage)}</pre>
              {[...new Set(separate.warnings)].map(warning => <p className="muted" key={warning}>{warning}</p>)}
              {separate.clocks && <details><summary>Original source-view clocks</summary>
                <pre>{JSON.stringify({ clocks: separate.clocks, sources: separate.source_clocks }, null, 2)}</pre>
              </details>}
              </details>
            </section>;
          })}
          {hypotheses.length > 0 && <details><summary>Planning assumptions and interpretation limits</summary>
            <p className="muted">Model-proposed guidance, not independently verified business facts.</p>
            <ul>{hypotheses.map((item, index) => <li key={index}>{item}</li>)}</ul>
          </details>}
          {view.outcome?.planning_receipt && <details><summary>Metric selection and planning evidence</summary>
            <pre>{JSON.stringify(view.outcome.planning_receipt, null, 2)}</pre></details>}
          {view.outcome?.execution?.receipts.map(receipt => <details key={receipt.id}>
            <summary>{receipt.model}: {receipt.returned_rows} rows / {receipt.status}</summary>
            <pre>{receipt.query}</pre><p>{receipt.completeness_evidence}</p>
            {!!receipt.parameters?.length && <pre>Bound parameters: {JSON.stringify(receipt.parameters)}</pre>}
            <p>Source watermark: {receipt.source_watermark ?? "Not observed"}</p>
            <pre>{JSON.stringify(receipt.clocks, null, 2)}</pre>
          </details>)}
        </div>
        {view.nodes.length > 0 && <ExecutionCanvas nodes={view.nodes} events={view.events} />}
      </article>;
    })}</div>
    {result && <div className="card">
      <h2>{result.comparability === "COMMON_SOURCE_STATE" ? "Common source state verified" : "Not a comparable pair"}</h2>
      <p>{result.provider} / {result.model_version}</p>
      {result.comparability_reasons.map(reason => <p key={reason}>{reason}</p>)}
      <p className="muted">Observed common state is not proof of accuracy or an ontology advantage.</p>
      <details><summary>Shared facts and limitations</summary>
        <pre>{result.source_facts_hash}</pre><ul>{result.limitations.map(item => <li key={item}>{item}</li>)}</ul>
      </details>
    </div>}
  </section>;
}
