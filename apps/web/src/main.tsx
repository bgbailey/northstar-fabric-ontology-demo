import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { examplePlan } from "./example";
import { NativeDemo } from "./NativeDemo";
import type { RecordedDemo } from "./recorded";
import { ExecutionCanvas } from "./ExecutionCanvas";
import { ComparisonView } from "./ComparisonView";
import { RecordedComparisonsView, isRecordedComparisons, type RecordedComparisons } from "./RecordedComparisons";
import { ContextOverview } from "./ContextOverview";
import { DemoLibraryView, isRecordedDemoLibrary, type RecordedDemoLibrary } from "./DemoLibrary";
import {
  AgentSurfaceCaptureView, isRecordedAgentSurface, type RecordedAgentSurface,
} from "./ServiceAssuranceDemo";
import {
  isRecordedScorecard, ScorecardView, type RecordedScorecard,
} from "./ScorecardView";
import { hasResourceGrant, ResourceConsent, type ConsentResource } from "./ResourceConsent";
import {
  executionLabel, isHealth, isRecording, readerMode, readinessStages, signInFailureMessage, type HealthState,
} from "./readiness";
import { planNodes, readExecutionStream, type ExecutionEvent, type ExecutionResult, type PlanNodeView } from "./execution";
import "./style.css";

type Experiment = { id: string; title: string; phase: string; status: string };
let csrfToken: string | null = null;

async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST", signal,
    headers: { "Content-Type": "application/json", "X-Telco-Local": "submitted-plan",
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data: unknown = await response.json();
  if (!response.ok) throw new Error(JSON.stringify(data));
  return data as T;
}

export function App({ navigate = (uri: string) => window.location.assign(uri) }: {
  navigate?: (uri: string) => void;
} = {}) {
  const [health, setHealth] = useState<HealthState | null>(null);
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [tab, setTab] = useState("Execute");
  const [plan, setPlan] = useState("");
  const [result, setResult] = useState<ExecutionResult | null>(null);
  const [events, setEvents] = useState<ExecutionEvent[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [question, setQuestion] = useState("");
  const [context, setContext] = useState<unknown>(null);
  const [recording, setRecording] = useState<RecordedDemo | null>(null);
  const [recordedComparisons, setRecordedComparisons] = useState<RecordedComparisons | null>(null);
  const [demoLibrary, setDemoLibrary] = useState<RecordedDemoLibrary | null>(null);
  const [agentSurface, setAgentSurface] = useState<RecordedAgentSurface | null>(null);
  const [scorecard, setScorecard] = useState<RecordedScorecard | null>(null);
  const [readerVerified, setReaderVerified] = useState(false);
  const [consentPending, setConsentPending] = useState(false);
  const readerGeneration = useRef(0);
  const authTransition = useRef(false);
  const [fixturePeriod, setFixturePeriod] = useState("2026-07");
  const [fixtureView, setFixtureView] = useState<"contracts" | "accounts">("contracts");
  const [incidentKey, setIncidentKey] = useState("");
  const [fixtureLoading, setFixtureLoading] = useState(false);
  const [executedQuestion, setExecutedQuestion] = useState("");
  const executionAbort = useRef<AbortController | null>(null);
  useEffect(() => () => executionAbort.current?.abort(), []);
  useEffect(() => {
    const callback = new URLSearchParams(window.location.search);
    if (callback.has("auth_error")) {
      setError(signInFailureMessage(callback.get("auth_error")));
      window.history.replaceState(null, "", "/");
    }
    const controller = new AbortController();
    Promise.all([
      request<unknown>("/api/health", undefined, controller.signal),
      request<{ experiments: Experiment[] }>("/api/capabilities", undefined, controller.signal),
    ]).then(async ([h, c]) => {
      if (!isHealth(h)) throw new Error("Cannot verify the server's mode and readiness. No fallback was selected.");
      csrfToken = h.csrf_token ?? null;
      setReaderVerified(h.authenticated === true);
      setHealth(h); setExperiments(c.experiments);
      if (h.mode === "RECORDED_DEMO_LIBRARY") {
        const packet = await request<unknown>("/api/demo-library", undefined, controller.signal);
        if (!isRecordedDemoLibrary(packet)) throw new Error("Invalid two-stream demo recording archive; no fallback selected.");
        if (!controller.signal.aborted) setDemoLibrary(packet);
      } else if (h.mode === "RECORDED_AGENT_SURFACE") {
        const surfaceCapture = await request<unknown>("/api/agent-surface", undefined, controller.signal);
        if (!isRecordedAgentSurface(surfaceCapture)) {
          throw new Error("Invalid recorded agent surface file; no fallback selected.");
        }
        if (!controller.signal.aborted) setAgentSurface(surfaceCapture);
      } else if (h.mode === "RECORDED_SCORECARD") {
        const scorecardPayload = await request<unknown>("/api/scorecard", undefined, controller.signal);
        if (!isRecordedScorecard(scorecardPayload)) {
          throw new Error("Invalid recorded scorecard file; no fallback selected.");
        }
        if (!controller.signal.aborted) setScorecard(scorecardPayload);
      } else if (h.mode === "RECORDED_LIVE_COMPARISON") {
        const packet = await request<unknown>("/api/recorded-comparisons", undefined, controller.signal);
        if (!isRecordedComparisons(packet)) throw new Error("Invalid recorded comparison file; no fallback selected.");
        if (!controller.signal.aborted) setRecordedComparisons(packet);
      } else if (h.mode === "RECORDED_NATIVE_DEMO") {
        const packet = await request<unknown>("/api/native-demo", undefined, controller.signal);
        if (!isRecording(packet)) throw new Error("Missing or invalid recording. No live or offline fallback was selected.");
        if (!controller.signal.aborted) setRecording(packet);
      } else if (h.mode === "OFFLINE_SYNTHETIC") {
        setContext(await request<unknown>("/api/context", undefined, controller.signal));
        setPlan(JSON.stringify(examplePlan(h), null, 2));
      } else {
        setTab("Comparison");
        setContext({ status: h.readiness.status, detail: h.readiness.claim });
      }
    }).catch((e: unknown) => {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e));
    });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!health || !readerMode(health.mode)) return;
    const abort = new AbortController();
    const check = () => {
      if (authTransition.current) return;
      const generation = readerGeneration.current;
      request<unknown>("/api/health", undefined, abort.signal).then(h => {
        if (abort.signal.aborted || generation !== readerGeneration.current || authTransition.current) return;
        if (!isHealth(h) || h.mode !== health.mode) {
          throw new Error("Server mode changed or cannot be verified. Reload explicitly; no fallback was selected.");
        }
        csrfToken = h.csrf_token ?? null;
        if (h.session_epoch !== health.session_epoch || !h.authenticated || !h.readiness.ready
          || h.context_revision !== health.context_revision) {
          setResult(null); setEvents([]); setContext(null); executionAbort.current?.abort();
        }
        setHealth(h); setReaderVerified(h.authenticated === true);
      }).catch((reason: unknown) => {
        if (abort.signal.aborted || generation !== readerGeneration.current || authTransition.current) return;
        const message = reason instanceof Error ? reason.message : "Demo user session could not be verified.";
        csrfToken = null;
        setReaderVerified(false); setResult(null); setEvents([]); setContext(null); executionAbort.current?.abort();
        setHealth(previous => previous && { ...previous, authenticated: false, csrf_token: null,
          readiness: { ...previous.readiness, ready: false, status: "BLOCKED_AUTH",
            reader_session: "UNKNOWN", blockers: [message] } });
        setError(message);
      });
    };
    window.addEventListener("focus", check);
    const interval = window.setInterval(check, 30_000);
    return () => {
      abort.abort(); window.removeEventListener("focus", check); window.clearInterval(interval);
    };
  }, [health]);

  function invalidateReaderAnswers() {
    readerGeneration.current += 1; authTransition.current = true;
    setReaderVerified(false); setResult(null); setEvents([]); setContext(null);
    executionAbort.current?.abort();
    return readerGeneration.current;
  }
  async function beginResourceConsent(resource: ConsentResource) {
    if (!health?.authenticated || !readerVerified || authTransition.current || !csrfToken) return;
    const generation = invalidateReaderAnswers();
    setConsentPending(true); setError("");
    try {
      const response = await request<unknown>(`/auth/consent/${resource}`, {});
      if (generation !== readerGeneration.current) return;
      if (typeof response !== "object" || response === null || !("resource" in response)
        || response.resource !== resource || !("authorization_uri" in response)
        || typeof response.authorization_uri !== "string") throw new Error("Invalid bounded resource consent response.");
      const uri = new URL(response.authorization_uri);
      if (uri.protocol !== "https:" || uri.host !== "login.microsoftonline.com"
        || uri.username || uri.password || uri.hash
        || !/^\/[0-9a-f-]{36}\/oauth2\/v2\.0\/authorize$/.test(uri.pathname)) {
        throw new Error("Invalid bounded resource consent destination.");
      }
      navigate(response.authorization_uri);
    } catch {
      if (generation !== readerGeneration.current) return;
      authTransition.current = false; setConsentPending(false);
      setError("Resource consent did not start. Prior answers remain discarded. Verify the current session explicitly before retrying.");
    }
  }
  async function authorizeExistingResource(resource: ConsentResource) {
    if (!health?.authenticated || !readerVerified || authTransition.current || !csrfToken) return;
    const previousEpoch = health.session_epoch;
    const generation = invalidateReaderAnswers();
    setConsentPending(true); setError("");
    try {
      const response = await request<unknown>(`/auth/authorize-existing/${resource}`, {});
      if (generation !== readerGeneration.current) return;
      if (typeof response !== "object" || response === null || !("resource" in response)
        || response.resource !== resource || !("status" in response)
        || response.status !== "EXISTING_GRANT_VALIDATED_CURRENT_SESSION"
        || !("ready" in response) || response.ready !== false) {
        throw new Error("Invalid existing-grant response.");
      }
      const current = await request<unknown>("/api/health");
      if (generation !== readerGeneration.current) return;
      if (!isHealth(current) || current.mode !== health.mode || !current.authenticated
        || !current.session_epoch || current.session_epoch === previousEpoch || !current.csrf_token
        || !("resource_consent" in current) || !hasResourceGrant(current.resource_consent, resource)) {
        throw new Error("Existing grant did not produce a verified fresh demo user session.");
      }
      csrfToken = current.csrf_token;
      setHealth(current); setReaderVerified(true);
      authTransition.current = false; setConsentPending(false);
    } catch {
      if (generation !== readerGeneration.current) return;
      csrfToken = null;
      authTransition.current = false; setConsentPending(false);
      setError("Existing resource authorization could not be verified. Microsoft may require interactive sign-in or consent. Prior answers remain discarded; no permissions were granted automatically.");
    }
  }
  async function logout() {
    const generation = invalidateReaderAnswers();
    setConsentPending(false);
    try { await request("/auth/logout", {}); if (generation === readerGeneration.current) navigate("/"); }
    catch {
      if (generation === readerGeneration.current) {
        authTransition.current = false;
        setError("Sign-out could not be confirmed. Prior answers remain discarded; verify the current session explicitly.");
      }
    }
  }
  async function execute() {
    if (health?.mode !== "OFFLINE_SYNTHETIC" || !health.data_ready) return;
    setBusy(true); setError(""); setResult(null); setEvents([]);
    const controller = new AbortController();
    executionAbort.current = controller;
    try {
      const value: unknown = JSON.parse(plan);
      if (typeof value !== "object" || value === null || !("request_id" in value)
        || typeof value.request_id !== "string") throw new Error("Plan requires a request_id.");
      setExecutedQuestion("question" in value && typeof value.question === "string" ? value.question : "Submitted plan result");
      const response = await fetch("/api/execute/stream", {
        method: "POST", signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-Telco-Local": "submitted-plan",
          ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) },
        body: JSON.stringify(value),
      });
      const accepted = await readExecutionStream(response, value.request_id, event => {
        if (!controller.signal.aborted) setEvents(previous => [...previous, event]);
      });
      controller.signal.throwIfAborted();
      setResult(accepted);
    } catch (e: unknown) {
      controller.abort();
      setError(e instanceof Error && e.name === "AbortError"
        ? "Execution cancelled; no complete result was accepted." : e instanceof Error ? e.message : String(e));
    } finally { executionAbort.current = null; setBusy(false); }
  }
  async function loadSlaFixture() {
    setFixtureLoading(true); setError(""); setResult(null); setEvents([]);
    try {
      const params = new URLSearchParams({ period: fixturePeriod, view: fixtureView });
      if (incidentKey.trim()) {
        const key = Number(incidentKey);
        if (!Number.isSafeInteger(key) || key < 1) throw new Error("Incident key must be a positive integer.");
        params.set("incident_key", String(key));
      }
      const value = await request<unknown>(`/api/fixtures/sla?${params}`);
      setPlan(JSON.stringify(value, null, 2));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setFixtureLoading(false); }
  }
  async function ask() {
    setError("");
    try { await request("/api/plan", { question }); }
    catch (e: unknown) { setError(e instanceof Error ? e.message : String(e)); }
  }
  let nodes: PlanNodeView[] = [];
  try { nodes = planNodes(JSON.parse(plan)); }
  catch { /* Editable JSON can be temporarily incomplete. Execution displays parse errors. */ }
  const columns = [...new Set(result?.answer?.rows.flatMap(row => Object.keys(row)) ?? [])];

  if (!health) return <main><h1>Telco IQ</h1>
    {error ? <p className="error" role="alert">{error}</p> : <p role="status">Connecting to the local app...</p>}
  </main>;

  if (health.mode === "RECORDED_DEMO_LIBRARY") {
    if (demoLibrary) return <DemoLibraryView library={demoLibrary} />;
    return <main><h1>Demo streams in Fabric</h1>
      {error ? <p className="error" role="alert">{error}</p> : <p role="status">Loading the two recorded streams...</p>}
    </main>;
  }
  if (health.mode === "RECORDED_AGENT_SURFACE") {
    if (agentSurface) return <AgentSurfaceCaptureView surface={agentSurface} />;
    return <main><h1>Recorded agent surface</h1>
      {error ? <p className="error" role="alert">{error}</p> : <p role="status">Loading the recorded surface capture...</p>}
    </main>;
  }
  if (health.mode === "RECORDED_SCORECARD") {
    if (scorecard) return <ScorecardView scorecard={scorecard} />;
    return <main><h1>Evaluation scorecard</h1>
      {error ? <p className="error" role="alert">{error}</p> : <p role="status">Loading the recorded scorecard...</p>}
    </main>;
  }
  if (health?.mode === "RECORDED_NATIVE_DEMO") {
    if (recording) return <NativeDemo recording={recording} />;
    return <main><h1>Recorded ontology demo in Fabric</h1>
      {error ? <p className="error" role="alert">{error}</p> : <p role="status">Loading the recorded capture...</p>}
    </main>;
  }
  if (health.mode === "RECORDED_LIVE_COMPARISON") {
    if (recordedComparisons) return <RecordedComparisonsView recording={recordedComparisons} />;
    return <main><h1>Recorded live comparisons</h1>
      {error ? <p className="error" role="alert">{error}</p> : <p role="status">Loading original captures...</p>}
    </main>;
  }

  return <main>
    <header><div><p className="eyebrow">NORTHSTAR WIRELESS / {readerMode(health.mode) ? "CONNECTED BUSINESS CONTEXT" : "LOCAL WORKBENCH"}</p>
      <h1>Telco IQ</h1><p>{readerMode(health.mode)
        ? "Source-owned measures. Shared business meaning. Two transparent approaches."
        : "Governed source queries. Explicit joins. Evidence before claims."}</p></div>
      <span className="badge">{executionLabel(health.mode)}</span></header>
    <aside className="notice">
      {readerMode(health.mode)
        ? "Delegated BFF: tokens stay server-side (authorization-code + PKCE, not OBO). Configuration is not current authorization in Fabric or a successful run. Use Comparison for the guarded evaluation arm path."
        : "Local synthetic execution only. Not Fabric DAX, an ontology in Fabric, a signed-in demo user identity in a demo tenant, or RLS proof. Natural-language planning and live cloud execution are not enabled."}
    </aside>
    <section className="card" aria-label="Service readiness">
      <h2>{health.readiness.status}</h2><p>{health.readiness.claim}</p>
      <ul>{health.readiness.blockers.map(blocker => <li key={blocker}>{blocker}</li>)}</ul>
      <details><summary>Configuration versus execution evidence</summary>
        <dl>{readinessStages(health.readiness).map(([label, state]) =>
          <div key={label}><dt>{label}</dt><dd>{state}</dd></div>)}</dl>
      </details>
    </section>
    {readerMode(health.mode) && <section className="card">
      <h2>Demo user sign-in (demo tenant)</h2>
      {!health.authenticated
        ? <a href={health.signin_url ?? "/auth/login"}>Sign in to demo tenant</a>
        : <div className="row">
          <button disabled={!readerVerified || consentPending} onClick={async () => {
            try { setContext(await request("/api/mcp/inventory", {})); }
            catch (e: unknown) { setError(e instanceof Error ? e.message : String(e)); }
          }}>Discover real GA IQ MCP tools</button>
          <button onClick={logout}>Sign out</button>
        </div>}
      <p className="muted">A developer or author sign-in is not a demo user RLS/OLS test.</p>
    </section>}
    {readerMode(health.mode) && <ResourceConsent
      resources={"resource_consent" in health ? health.resource_consent : undefined}
      enabled={health.authenticated === true && readerVerified} pending={consentPending}
      onConsent={beginResourceConsent} onExistingGrant={authorizeExistingResource} />}
    <section className="identity">
      <strong>{health.mode === "OFFLINE_SYNTHETIC" ? "Local synthetic process"
        : health.authenticated && readerVerified ? "Current delegated demo user session" : "Not signed in"}</strong>
      <span>{health.mode === "OFFLINE_SYNTHETIC" ? `Fixture models: ${health.scope?.models.join(", ") ?? "Unknown"}`
        : "Source authorization: current probes required; sign-in alone is not source access"}</span>
      <span>Submitted-plan fixture: {health.data_ready ? "Available" : "Not enabled in this mode"}</span>
    </section>
    <nav aria-label="Application views">{["Execute", "Context", "Experiments", "Comparison"].map(t =>
      <button key={t} aria-current={tab === t ? "page" : undefined} onClick={() => setTab(t)}>{t}</button>
    )}</nav>
    {error && <pre className="error" role="alert">{error}</pre>}
    {tab === "Execute" && <>
      {result && <section className="card execution-result" aria-live="polite">
        <h2>{executedQuestion}</h2>
        <p>{executionLabel(result.mode)} · {result.status === "PARTIAL" ? "partial result (incomplete or unmapped rows excluded)" : result.status} · {result.elapsed_ms.toFixed(1)} ms</p>
        {!result.answer && <p>No business answer returned. Missing, blocked or stale is not measured zero.</p>}
        {result.answer && result.status !== "COMPLETE" &&
          <p className="notice">Incomplete result. Displayed rows are not a complete financial total.</p>}
        {result.errors.map(e => <p key={e} className="error">{e}</p>)}
        <div className="table-scroll"><table><thead><tr>{columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>{result.answer?.rows.map((row, i) => <tr key={i}>
            {columns.map(c => <td key={c}>{row[c] === null || row[c] === undefined ? "UNKNOWN" : String(row[c])}</td>)}
          </tr>)}</tbody></table></div>
        {result.answer?.rows.length === 0 && <p>No rows in this scoped result. Unavailable data is not treated as zero.</p>}
        <p>Currency results are integer cents; blank is not zero.</p>
        <pre>Coverage: {JSON.stringify(result.answer?.coverage ?? {})}</pre>
        {result.answer?.warnings.map(warning => <p className="muted" key={warning}>{warning}</p>)}
        <h3>Source receipts</h3>{result.receipts.map(r => <details key={r.id} open>
          <summary>{r.node_id}: {r.tool} / {r.lane} / {r.returned_rows} rows / {r.elapsed_ms.toFixed(1)} ms</summary>
          <p>{r.status}: {r.completeness_evidence}</p><pre>{r.query}</pre>
          {!!r.parameters?.length && <pre>Bound parameters: {JSON.stringify(r.parameters)}</pre>}
          <p>Source watermark: {r.source_watermark ?? "Not observed"}</p>
          <p className="muted">Citation: {r.id}</p><pre>{JSON.stringify(r.clocks, null, 2)}</pre>
        </details>)}
      </section>}
      <section className="card">
        <h2>Question</h2><label htmlFor="question">Submitted-plan workbench (not the paired application path)</label>
        <div className="row"><input id="question" value={question} onChange={e => setQuestion(e.target.value)}
          placeholder="Ask about source-owned measures" />
          <button onClick={ask} disabled={!question.trim()}>Check planner</button></div>
        <p className="muted">This does not route to a canned answer. The guarded paired application path is on Comparison.</p>
      </section>
      {health.mode === "OFFLINE_SYNTHETIC" && <section className="card">
        <div className="section-heading"><h2>SLA engine fixture</h2><span className="status-pill">SUBMITTED PLAN / NOT AN AGENT RUN</span></div>
        <p>Network incident exposure to effective contracts, then governed monthly credit measures or deduplicated account-level Finance priority.</p>
        <div className="fixture-controls">
          <label>Service month<select value={fixturePeriod} disabled={busy || fixtureLoading}
            onChange={event => setFixturePeriod(event.target.value)}>
            <option value="2026-07">July 2026 - populated incidents</option>
            <option value="2026-08">August 2026 - empty incident cohort</option>
          </select></label>
          <label>Result grain<select value={fixtureView} disabled={busy || fixtureLoading} onChange={event => {
            if (event.target.value === "contracts" || event.target.value === "accounts") setFixtureView(event.target.value);
          }}><option value="contracts">Contract-month exposure</option><option value="accounts">Account-month financial priority</option></select></label>
          <label>Incident key (optional)<input type="number" min="1" step="1" value={incidentKey}
            disabled={busy || fixtureLoading} onChange={event => setIncidentKey(event.target.value)} placeholder="All incidents in the month" /></label>
          <button onClick={loadSlaFixture} disabled={busy || fixtureLoading}>{fixtureLoading ? "Preparing plan..." : "Load SLA fixture"}</button>
        </div>
        <p className="muted">This loads an editable deterministic test plan. It does not call an LLM.
          Incident selection narrows the affected cohort; credit exposure remains a contract-month amount, not an incident-only allocation.</p>
      </section>}
      <section className="grid">
        <div className="card"><h2>Submitted typed plan</h2>
          <p>Editable engineering input, not a generated plan. The example is a test fixture, not planner logic.</p>
          <label className="sr-only" htmlFor="plan">Plan JSON</label>
          <textarea id="plan" spellCheck={false} disabled={busy || fixtureLoading} value={plan} onChange={e => setPlan(e.target.value)} />
          <button className="primary" onClick={execute}
            disabled={busy || fixtureLoading || health.mode !== "OFFLINE_SYNTHETIC" || !health.data_ready}>
            {busy ? "Executing source queries..." : "Validate and execute locally"}
          </button>
          {busy && <button onClick={() => executionAbort.current?.abort()}>Cancel run</button>}</div>
        <div className="card"><h2>Plan dependencies</h2>
          <ol className="dag">{nodes.map(n => <li key={n.id}><strong>{n.id}</strong> / {n.kind}
            <small>{n.model ?? [n.left, n.right, n.source].filter(Boolean).join(" + ")}</small></li>)}</ol>
          <p className="muted">Only actual returned tool receipts appear below. No simulated thinking.</p></div>
      </section>
      {nodes.length > 0 && <ExecutionCanvas nodes={nodes} events={events} />}
    </>}
    {tab === "Context" && <ContextOverview context={context} serverMode={health.mode} />}
    {tab === "Experiments" && <section className="card"><h2>Live capability register</h2>
      <table><thead><tr><th>ID</th><th>Phase</th><th>Experiment</th><th>Observed status</th></tr></thead>
        <tbody>{experiments.map(e => <tr key={e.id}><td>{e.id}</td><td>{e.phase}</td><td>{e.title}</td>
          <td>{e.status}</td></tr>)}</tbody></table></section>}
    {tab === "Comparison" && <ComparisonView key={health.session_epoch ?? health.mode} csrf={csrfToken}
      authenticated={readerMode(health.mode) && health.authenticated === true && readerVerified} />}
    <footer><strong>Limits of this recording:</strong> Configuration and local tests do not demonstrate a current live application run.
      Submitted-plan fixtures are local SQL. Recorded answers from the ontology agent in Fabric are separate evidence; no comparative advantage is inferred. See <code>docs/limitations.md</code>.</footer>
  </main>;
}

const rootElement = document.getElementById("root");
if (rootElement) createRoot(rootElement).render(<React.StrictMode><App /></React.StrictMode>);
