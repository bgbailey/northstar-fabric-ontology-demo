import { useState } from "react";
import {
  isRecordedComparisons, RecordedComparisonsView, type RecordedComparisons,
} from "./RecordedComparisons";
import { isRecordedOntology, RecordedOntologyView, type RecordedOntology } from "./RecordedOntology";
import {
  isRecordedServiceAssuranceDemo, ServiceAssuranceDemoView, type RecordedServiceAssuranceDemo,
} from "./ServiceAssuranceDemo";

type ExecutiveAccount = {
  account_id: string; account_key: number; active_lines_eom: number;
  recognized_service_revenue_cents: number; contribution_margin_cents: number;
};
type ExecutiveSourceQuery = {
  model: "subscriptions" | "finance"; dax: string; dax_sha256: string;
  response_sha256: string; observed_at: string;
};
export type RecordedExecutiveDemo = {
  schema_version: "1.0"; mode: "RECORDED_EXECUTIVE_NATIVE"; captured_at: string;
  question: string; ontology: RecordedOntology; separate_native_item_verified: true;
  focus_metric_ids: string[];
  native_answer_origin: "MANAGED_NATIVE_SERVICE"; native_answer_text: string;
  native_response_sha256: string; source_measure_review_sha256: string;
  native_answer_matches_source_values: true; rows: ExecutiveAccount[];
  source_queries: ExecutiveSourceQuery[];
  source_clocks: { model: string; data: string; model_revision: string; table: string | null }[];
  limitations: string[];
};
export type RecordedDemoLibrary = {
  schema_version: "1.0"; mode: "RECORDED_DEMO_LIBRARY"; exported_at: string;
  executive: RecordedExecutiveDemo; customer360: RecordedComparisons;
  service_assurance?: RecordedServiceAssuranceDemo | null;
  limitations: string[];
};
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function zoned(value: unknown): value is string {
  return typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
}
function hash(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.length > 0 && value.every(item => typeof item === "string");
}
export function isRecordedExecutiveDemo(value: unknown): value is RecordedExecutiveDemo {
  if (!object(value) || value.schema_version !== "1.0" || value.mode !== "RECORDED_EXECUTIVE_NATIVE"
    || !zoned(value.captured_at) || typeof value.question !== "string" || !value.question
    || !isRecordedOntology(value.ontology) || value.ontology.entities.length !== 4
    || value.ontology.sources.length !== 2
    || value.separate_native_item_verified !== true || value.native_answer_origin !== "MANAGED_NATIVE_SERVICE"
    || typeof value.native_answer_text !== "string" || !value.native_answer_text
    || !hash(value.native_response_sha256) || !hash(value.source_measure_review_sha256)
    || value.native_answer_matches_source_values !== true || !strings(value.limitations)) return false;
  const ontology = value.ontology;
  if (!["subscriptions", "finance"].every(model => ontology.sources.includes(model))) return false;
  const expectedMetrics = ["subscriptions.active_lines_eom", "finance.recognized_service_revenue", "finance.contribution_margin"];
  if (!Array.isArray(value.focus_metric_ids) || value.focus_metric_ids.length !== 3) return false;
  const focusMetrics = value.focus_metric_ids;
  if (!expectedMetrics.every(id => focusMetrics.includes(id) && ontology.metrics.some(metric => metric.id === id))
    || !Array.isArray(value.rows) || !value.rows.length || value.rows.length > 250
    || !value.rows.every(row => object(row) && typeof row.account_id === "string" && row.account_id.length > 0
      && ["account_key", "active_lines_eom", "recognized_service_revenue_cents", "contribution_margin_cents"]
        .every(key => typeof row[key] === "number" && Number.isSafeInteger(row[key]))
      && Number(row.account_key) >= 0 && Number(row.active_lines_eom) >= 0)
    || new Set(value.rows.map(row => row.account_id)).size !== value.rows.length
    || new Set(value.rows.map(row => row.account_key)).size !== value.rows.length
    || !Array.isArray(value.source_queries) || value.source_queries.length < 2
    || !value.source_queries.every(query => object(query) && ["subscriptions", "finance"].includes(String(query.model))
      && typeof query.dax === "string" && query.dax.length > 0 && hash(query.dax_sha256)
      && hash(query.response_sha256) && zoned(query.observed_at))
    || new Set(value.source_queries.map(query => query.model)).size !== 2
    || !Array.isArray(value.source_clocks) || value.source_clocks.length < 2
    || !value.source_clocks.every(clock => object(clock) && ["subscriptions", "finance"].includes(String(clock.model))
      && typeof clock.data === "string" && typeof clock.model_revision === "string"
      && (clock.table === null || typeof clock.table === "string"))
    || new Set(value.source_clocks.map(clock => clock.model)).size !== 2) return false;
  return true;
}
export function isRecordedDemoLibrary(value: unknown): value is RecordedDemoLibrary {
  if (!object(value) || value.schema_version !== "1.0" || value.mode !== "RECORDED_DEMO_LIBRARY"
    || !zoned(value.exported_at) || !isRecordedExecutiveDemo(value.executive)
    || !isRecordedComparisons(value.customer360) || value.customer360.ontology === undefined
    || value.customer360.ontology.sources.length <= 2 || value.customer360.ontology.entities.length <= 4
    || value.customer360.ontology.native_snapshot_hash === value.executive.ontology.native_snapshot_hash
    || !strings(value.limitations)) return false;
  if (value.service_assurance !== undefined && value.service_assurance !== null) {
    if (!isRecordedServiceAssuranceDemo(value.service_assurance)) return false;
    const saHash = value.service_assurance.ontology.native_snapshot_hash;
    if (saHash === value.executive.ontology.native_snapshot_hash
      || saHash === value.customer360.ontology.native_snapshot_hash) return false;
  }
  return true;
}

function ExecutiveDemoView({ recording }: { recording: RecordedExecutiveDemo }) {
  return <main>
    <header><div><p className="eyebrow">NORTHSTAR WIRELESS / EXECUTIVE QUICKSTART</p>
      <h1>Keep the measures. Connect the meaning.</h1>
      <p>A separate small Fabric IQ ontology (current experience) in Fabric, not a filtered Customer360 graph.</p></div>
      <span className="badge">RECORDED ANSWER IN FABRIC</span></header>
    <section className="card" aria-label="Executive demo scope">
      <h2>Three domains. Two semantic models. Four entities.</h2>
      <p>Customers, Subscriptions and Finance share customer identity and period meaning.
        The original models still own all three calculations.</p>
      <div className="table-scroll"><table><thead><tr><th>Measure</th><th>Owning model</th><th>Original home</th></tr></thead>
        <tbody>{recording.ontology.metrics.filter(metric => recording.focus_metric_ids.includes(metric.id)).map(metric => <tr key={metric.id}>
          <td>{metric.name}</td><td>{metric.model}</td><td>{
            recording.ontology.entities.find(entity => entity.id === metric.entity)?.name ?? metric.table
          }</td>
        </tr>)}</tbody></table></div>
    </section>
    <section className="card" aria-label="Executive business answer in Fabric">
      <p className="eyebrow">ONE CONNECTED QUESTION</p>
      <h2>Which July customers have no active lines, positive recognized revenue and negative margin?</h2>
      <details><summary>Exact submitted question</summary><p>{recording.question}</p></details>
      <p className="muted">Answer captured in Fabric {recording.captured_at}. Selecting this stream does not run a query.</p>
      <div className="table-scroll"><table aria-label="Source-checked executive answer in Fabric">
        <thead><tr><th>Customer account</th><th>Active Lines EOM</th><th>Recognized revenue · cents</th>
          <th>Contribution margin · cents</th></tr></thead>
        <tbody>{recording.rows.map(row => <tr key={row.account_id}><td>{row.account_id}</td>
          <td>{row.active_lines_eom.toLocaleString("en-US")}</td>
          <td>{row.recognized_service_revenue_cents.toLocaleString("en-US")}</td>
          <td>{row.contribution_margin_cents.toLocaleString("en-US")}</td></tr>)}</tbody>
      </table></div>
      <p>Review candidates only, not a billing diagnosis, causal conclusion, complete-population total or automatic action.</p>
      <details><summary>Original response from the ontology agent in Fabric, identifier-redacted</summary><pre>{recording.native_answer_text}</pre></details>
    </section>
    <RecordedOntologyView ontology={recording.ontology} gettingStarted focusMetricIds={recording.focus_metric_ids} />
    <section className="card" aria-label="Executive presenter talk track">
      <h2>Why ontology?</h2>
      <p><strong>Keep trusted calculations.</strong> Subscriptions owns lines; Finance owns revenue and margin.</p>
      <p><strong>Make shared meaning explicit.</strong> The same customer, the same month and the correct revenue definition.</p>
      <p><strong>Ask a connected question.</strong> An agent can reuse those decisions rather than redefine them for each consumer.</p>
      <p className="muted">A capable consumer can also join the models. This is not an exclusive computation,
        accuracy or speed claim. The answer from the ontology agent in Fabric is separate from the full stream's paired evaluation arm comparison.</p>
    </section>
    <details className="card"><summary>Original source-measure verification and clocks</summary>
      <p className="muted">These are independent source-verification queries, not an internal trace from the ontology agent in Fabric.
        Answer capture and source-observation times remain separate.</p>
      {recording.source_queries.map((query, index) => <details key={`${query.model}:${index}`}>
        <summary>{query.model} · original source query</summary><pre>{query.dax}</pre>
        <p>Observed {query.observed_at}</p><pre>{query.response_sha256}</pre>
      </details>)}
      <pre>{JSON.stringify(recording.source_clocks, null, 2)}</pre>
      <p>Response hash in Fabric:</p><pre>{recording.native_response_sha256}</pre>
      <p>Source-value review:</p><pre>{recording.source_measure_review_sha256}</pre>
    </details>
    <footer><p><strong>Limits of this recording</strong> (see <code>docs/limitations.md</code>):</p>{recording.limitations.map(item => <p key={item}>{item}</p>)}</footer>
  </main>;
}

export function DemoLibraryView({ library, initialStream = "executive" }: {
  library: RecordedDemoLibrary;
  initialStream?: "executive" | "service_assurance" | "customer360";
}) {
  const hasServiceAssurance = Boolean(library.service_assurance);
  const [stream, setStream] = useState<"executive" | "service_assurance" | "customer360">(
    initialStream === "service_assurance" && !hasServiceAssurance ? "executive" : initialStream,
  );
  return <>
    <section className="card" aria-label="Choose the demo stream">
      <p className="eyebrow">{hasServiceAssurance
        ? "RECORDED DEMO STREAMS / THREE SCOPES"
        : "TWO ONTOLOGIES IN FABRIC / TWO CONVERSATIONS"}</p>
      <div className={`story-presets demo-stream-switch${hasServiceAssurance ? " three-streams" : ""}`}
        role="group" aria-label="Demo streams">
        <button aria-pressed={stream === "executive"} onClick={() => setStream("executive")}>
          Executive quickstart · 2 models
        </button>
        {library.service_assurance && (
          <button aria-pressed={stream === "service_assurance"} onClick={() => setStream("service_assurance")}>
            Service assurance · 4 models
          </button>
        )}
        <button aria-pressed={stream === "customer360"} onClick={() => setStream("customer360")}>
          Customer360 deep dive · 8 models
        </button>
      </div>
      <p className="muted">Explicit recorded playback. Separate ontology definitions and original capture clocks;
        no sign-in, model call or source query runs when switching streams.</p>
    </section>
    {stream === "executive" ? <ExecutiveDemoView recording={library.executive} />
      : stream === "service_assurance" && library.service_assurance
        ? <ServiceAssuranceDemoView recording={library.service_assurance} />
        : <RecordedComparisonsView recording={library.customer360} startWithAdvanced />}
  </>;
}
