import { useState } from "react";
import { ComparisonView, isPairResult, type PairResult } from "./ComparisonView";
import { isTrace, type ExecutionEvent } from "./execution";
import { isRecordedOntology, RecordedOntologyView, type RecordedOntology } from "./RecordedOntology";

type RecordedEntry = {
  kind?: "getting_started" | "story";
  title: string; run_started_at: string; original_result_sha256: string;
  original_server_receipt_sha256: string; source_value_review_sha256: string;
  result: PairResult; events: Record<"baseline" | "native", ExecutionEvent[]>;
};
export type RecordedComparisons = {
  schema_version: "1.0"; mode: "RECORDED_LIVE_COMPARISON"; exported_at: string;
  entries: RecordedEntry[]; limitations: string[];
  ontology?: RecordedOntology;
};
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function timestamp(value: unknown) {
  return typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
}
export function isRecordedComparisons(value: unknown): value is RecordedComparisons {
  return object(value) && value.schema_version === "1.0" && value.mode === "RECORDED_LIVE_COMPARISON"
    && timestamp(value.exported_at) && Array.isArray(value.entries) && value.entries.length > 0
    && value.entries.length <= 10 && value.entries.every(entry => {
      if (!object(entry) || typeof entry.title !== "string" || !entry.title.trim() || !timestamp(entry.run_started_at)
        || (entry.kind !== undefined && entry.kind !== "getting_started" && entry.kind !== "story")
        || !["original_result_sha256", "original_server_receipt_sha256", "source_value_review_sha256"]
          .every(key => typeof entry[key] === "string" && /^[0-9a-f]{64}$/.test(entry[key]))
        || !isPairResult(entry.result) || entry.result.mode !== "LIVE_DELEGATED" || !object(entry.events)) return false;
      const native = entry.result.arms.find(arm => arm.arm === "native");
      const eventsByArm = entry.events;
      return native !== undefined && ["COMPLETE", "PARTIAL"].includes(native.status) && native.errors.length === 0
        && native.execution !== null && native.execution.errors.length === 0
        && native.execution.answer !== null && native.execution.answer.rows.length > 0
        && entry.result.arms.every(arm => {
          const events = eventsByArm[arm.arm];
          return Array.isArray(events) && events.every(event =>
            isTrace(event) && arm.execution !== null && event.run_id === arm.execution.request_id);
        });
    }) && new Set(value.entries.map(entry => entry.result.pair_id)).size === value.entries.length
    && Array.isArray(value.limitations) && value.limitations.length > 0
    && value.limitations.every(item => typeof item === "string")
    && (value.ontology === undefined || isRecordedOntology(value.ontology));
}
export function RecordedComparisonsView({ recording, startWithAdvanced = false }: {
  recording: RecordedComparisons; startWithAdvanced?: boolean;
}) {
  const [selected, setSelected] = useState(() => startWithAdvanced
    ? Math.max(0, recording.entries.findIndex(entry => entry.kind !== "getting_started")) : 0);
  const entry = recording.entries[selected];
  return <main>
    <header><div><p className="eyebrow">NORTHSTAR WIRELESS / RECORDED LIVE CAPTURE</p>
      <h1>Telco IQ</h1><p>Original business results, source queries and execution evidence.</p></div>
      <span className="badge">RECORDED LIVE</span></header>
    <aside className="notice">
      Deliberately selected historical playback. No sign-in, current source access, inference or new query runs here.
      PARTIAL results, exclusions and failed comparison arms remain exactly qualified.
    </aside>
    <section className="card">
      <div className="story-presets" role="group" aria-label="Recorded customer stories">
        {recording.entries.map((item, index) => <button key={item.result.pair_id}
          aria-pressed={selected === index} onClick={() => setSelected(index)}>{item.title}</button>)}
      </div>
      <p>Original run started {entry.run_started_at}</p>
      <p className="muted">Packet exported {recording.exported_at}. Export time is not a refreshed source clock.</p>
      <details><summary>Original capture and numerical-review hashes</summary>
        <pre>{JSON.stringify({
          result: entry.original_result_sha256, server_receipt: entry.original_server_receipt_sha256,
          source_value_review: entry.source_value_review_sha256,
        }, null, 2)}</pre>
      </details>
    </section>
    {entry.kind === "getting_started" && <section className="card" aria-label="Getting started: three domains, two models">
      <p className="eyebrow">START HERE / THREE DOMAINS, TWO SEMANTIC MODELS</p>
      <h2>Reuse the measures. Connect their meaning.</h2>
      <p>A focused slice of the existing native ontology, not a newly created standalone ontology item.</p>
      <div className="table-scroll"><table><thead><tr><th>Domain</th><th>Source model</th><th>Reusable context or measure</th></tr></thead>
        <tbody><tr><td>Customers</td><td>Subscriptions + Finance</td><td>Reviewed shared account identity</td></tr>
          <tr><td>Subscriptions</td><td>Subscriptions</td><td>Active Lines EOM: month-end snapshot</td></tr>
          <tr><td>Finance</td><td>Finance</td><td>Recognized Service Revenue and Contribution Margin</td></tr></tbody>
      </table></div>
      <p>Same customer. Same completed month. Recognized service revenue, not booked merchandise.
        The semantic models still own every calculation.</p>
      <p><strong>From metrics to an agentic review:</strong> connect line status, recognized revenue and margin
        by customer, then ask which account deserves investigation. A single domain model does not own
        that combined question; the ontology makes its shared meaning reusable.</p>
      <p className="muted">A capable traditional consumer can also join the models. The demonstrated benefit is
        connected, inspectable business context and source-measure reuse—not exclusive computation or a benchmark claim.</p>
    </section>}
    <ComparisonView key={entry.result.pair_id} csrf={null} authenticated={false}
      recording={entry.result} recordedEvents={entry.events}
      metricLabels={Object.fromEntries((recording.ontology?.metrics ?? []).map(metric =>
        [metric.id, `${metric.model} · ${metric.name}`]))} />
    {recording.ontology && <RecordedOntologyView ontology={recording.ontology}
      result={entry.result} events={entry.events} gettingStarted={entry.kind === "getting_started"} />}
    <footer>{recording.limitations.map(item => <p key={item}>{item}</p>)}</footer>
  </main>;
}
