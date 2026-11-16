import { useMemo, useRef, useState } from "react";
import {
  Background, Controls, Handle, MarkerType, Position, ReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import {
  definitionLayout, metricEvidence, recordedPeriod, recordedResultColumn, recordedTotals,
  sourceInventory, sourceLabel, type DefinitionLens, type RecordedDemo, type RecordedMetric,
} from "./recorded";
import { captureTime, executionLabel } from "./readiness";
import "@xyflow/react/dist/style.css";

type EntityData = { label: string; source: string; metrics: number; keys: string[]; inspect: () => void };
type EntityNode = Node<EntityData, "entity">;

function EntityCard({ data }: NodeProps<EntityNode>) {
  return <div className="entity-node">
    <Handle type="target" position={Position.Left} />
    <span className="eyebrow">{data.source}</span>
    <button className="entity-title nodrag nopan" onClick={data.inspect}
      aria-label={`Inspect ontology entity ${data.label}`}><strong>{data.label}</strong></button>
    <small>{data.metrics ? `${data.metrics} metrics linked to source measures` : data.keys.join(" · ") || "Business context"}</small>
    <Handle type="source" position={Position.Right} />
  </div>;
}
const nodeTypes = { entity: EntityCard };
const number = new Intl.NumberFormat("en-US");
const known = (value: string | undefined) => value?.trim() || "Not captured";

export function NativeDemo({ recording }: { recording: RecordedDemo }) {
  const [focus, setFocus] = useState(true);
  const [lens, setLens] = useState<DefinitionLens>("connected");
  const [selected, setSelected] = useState(recording.focus_entity_ids[0]);
  const [edgeId, setEdgeId] = useState<string | null>(null);
  const [metricId, setMetricId] = useState<string | null>(null);
  const [accountKey, setAccountKey] = useState<number | null>(null);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const evidenceRef = useRef<HTMLDetailsElement>(null);
  const totals = recordedTotals(recording);
  const inventory = sourceInventory(recording);
  const entity = recording.entities.find(item => item.id === selected);
  const relationship = recording.relationships.find(item => item.id === edgeId);
  const entityMetrics = recording.metrics.filter(metric => metric.entity_id === selected);
  const source = recording.sources.find(item => item.id === entity?.source_id);
  const selectedMetric = recording.metrics.find(metric => metric.id === metricId);
  const binding = selectedMetric ? metricEvidence(recording, selectedMetric) : null;
  const selectedRow = recording.rows.find(row => row.account_key === accountKey);
  const selectedColumn = selectedMetric ? recordedResultColumn(selectedMetric) : null;
  const annotations = relationship?.guidance ?? entity?.guidance ?? [];
  const annotationOwner = relationship?.id ?? entity?.id;
  const annotationCount = recording.entities.reduce((count, item) => count + item.guidance.length, 0)
    + recording.relationships.reduce((count, item) => count + item.guidance.length, 0);
  const graph = useMemo(() => {
    const view = definitionLayout(recording, focus, lens);
    const nodes: EntityNode[] = view.entities.map(item => ({
      id: item.id, type: "entity", selected: item.id === selected,
      position: view.positions.get(item.id) ?? { x: 0, y: 0 },
      data: { label: item.id, source: sourceLabel(item.source_id), keys: item.keys,
        inspect: () => { setSelected(item.id); setEdgeId(null); },
        metrics: recording.metrics.filter(metric => metric.entity_id === item.id).length },
      ariaLabel: `${item.id}, ${sourceLabel(item.source_id)} source. Select to inspect captured context.`,
    }));
    const edges: Edge[] = view.relationships.map(item => ({
      id: item.id, source: item.source, target: item.target, type: "smoothstep", animated: false,
      selected: edgeId === item.id,
      style: { stroke: focus || edgeId === item.id ? "var(--cp-accent)" : "var(--cp-border-strong)",
        strokeWidth: edgeId === item.id ? 3 : focus ? 2 : 1.5 },
      markerEnd: { type: MarkerType.ArrowClosed, color: "var(--cp-accent)" },
      ariaLabel: `${item.id}. Relationship direction in Fabric: ${item.source} to ${item.target}.`,
    }));
    return { nodes, edges };
  }, [recording, focus, lens, selected, edgeId]);

  function inspectEntity(id: string) {
    setSelected(id); setEdgeId(null);
  }
  function showEvidence(metric?: RecordedMetric) {
    setMetricId(metric?.id ?? null); setEvidenceOpen(true);
    evidenceRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }
  const answerMetrics = [
    { value: number.format(totals.lines), label: "Active lines EOM", column: "active_lines_eom" },
    { value: number.format(totals.cents), label: "Recognized revenue · cents", column: "recognized_service_revenue_cents" },
  ];

  return <main className="native-stage">
    <header className="native-header">
      <div><p className="eyebrow">NORTHSTAR WIRELESS / TELCO IQ</p>
        <h1>One question. Connected business meaning.</h1></div>
      <div className="capture-label"><span className="badge">{executionLabel(recording.mode)}</span>
        <small>Answer captured <time dateTime={recording.captured_at}>{captureTime(recording.captured_at)}</time></small></div>
    </header>

    <section className="card answer-hero" aria-label="Recorded business answer">
      <p className="eyebrow">HISTORICAL TWO-MODEL ANSWER IN FABRIC</p>
      <h2>{recording.question.split(". ")[0].replace(/\.$/, "")}.</h2>
      <div className="answer-scope">
        <span><strong>Service period</strong>{recordedPeriod(recording)}</span>
        <span><strong>Returned cohort</strong>{number.format(recording.rows.length)} captured account rows</span>
        <span><strong>Coverage</strong>Captured rows only · not revalidated</span>
      </div>
      <div className="answer-values">
        <div className="answer-count"><strong>{recording.rows.length}</strong><span>accounts in the answer</span></div>
        {answerMetrics.map(item => {
          const metric = recording.metrics.find(candidate => recordedResultColumn(candidate) === item.column);
          return <button className="answer-value" key={item.column} aria-controls="recorded-evidence"
            onClick={() => showEvidence(metric)}>
            <strong>{item.value}</strong><span>{item.label}</span><small>Inspect source evidence →</small>
          </button>;
        })}
      </div>
      <p className="answer-contract">Source measures own the calculations. The connected context makes identities,
        period choices and metric homes inspectable.</p>
      <div className="hero-actions"><button className="primary" onClick={() => showEvidence()}
        aria-controls="recorded-evidence">Inspect the captured answer</button>
        <details><summary>Full recorded request</summary><p>{recording.question}</p></details></div>
    </section>
    <p className="recorded-boundary">Recorded answer from the ontology agent in Fabric and API/MCP metadata. No live connection, new query or model call
      is running. This archived answer in Fabric is not proof of custom-application execution or current demo user access.</p>

    <section className="architecture-strip" aria-label="Source models to ontology to answer">
      <div><span className="eyebrow">SOURCE MODELS</span><strong>{recording.sources.length} calculation owners</strong>
        <small>Exact model · home table · measure</small></div>
      <span className="architecture-arrow" aria-hidden="true">→</span>
      <div><span className="eyebrow">ONTOLOGY BUSINESS LAYER</span><strong>{recording.entities.length} entities · {recording.relationships.length} ontology links</strong>
        <small>Shared identities, relationships and captured meaning</small></div>
      <span className="architecture-arrow" aria-hidden="true">→</span>
      <div><span className="eyebrow">EVIDENCED ANSWER</span><strong>{recording.rows.length} returned account rows</strong>
        <small>Captured DAX and integer-cent results</small></div>
    </section>

    <section className="source-models" aria-label="Captured source semantic models">
      {inventory.map(item => <article className="source-model" key={item.id} data-source-id={item.id}>
        <p className="eyebrow">{sourceLabel(item.id)} / SOURCE SEMANTIC MODEL</p>
        <h2>{known(item.name)}</h2>
        <p><strong>{item.entities.length}</strong> ontology entities · <strong>{item.metrics.length}</strong> metrics linked to source measures</p>
        <div className="source-homes">{[...new Set(item.metrics.map(metric => metric.home_table))].map(home =>
          <span key={home}>{home}</span>)}</div>
        <button onClick={() => {
          setFocus(false); setLens("sources");
          const first = item.entities[0]; if (first) inspectEntity(first.id);
        }}>Explore {sourceLabel(item.id)} entities</button>
      </article>)}
    </section>

    <section className="card graph-card" aria-labelledby="ontology-title">
      <div className="section-heading"><div><p className="eyebrow">CAPTURED DEFINITION IN FABRIC</p>
        <h2 id="ontology-title">See the business layer above the models</h2></div>
        <span className="status-pill observed">{recording.metrics.length} metrics linked to source measures</span></div>
      <div className="ontology-toolbar">
        <div role="group" aria-label="Definition lens" className="segmented-controls">
          <button aria-pressed={lens === "sources"} onClick={() => setLens("sources")}>By source model</button>
          <button aria-pressed={lens === "connected"} onClick={() => setLens("connected")}>Connected ontology</button>
        </div>
        <div role="group" aria-label="Ontology scope" className="segmented-controls">
          <button aria-pressed={focus} onClick={() => {
            setFocus(true); setEdgeId(null); setSelected(recording.focus_entity_ids[0]);
          }}>Account path</button>
          <button aria-pressed={!focus} onClick={() => { setFocus(false); setEdgeId(null); }}>Full ontology</button>
        </div>
      </div>
      <div className="graph-key"><span><i className="legend-entity" />Ontology entity</span>
        <span><i className="legend-edge" />Relationship direction in Fabric</span>
        <span>{graph.nodes.length} of {recording.entities.length} entities · {graph.edges.length} of {recording.relationships.length} links visible</span></div>
      <div className={`native-graph ${focus ? "focused-graph" : "full-graph"}`} aria-label="Recorded ontology definition graph">
        <ReactFlow key={`${focus}-${lens}`} nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes}
          fitView fitViewOptions={{ padding: 0.16 }} minZoom={0.2} maxZoom={2}
          nodesDraggable={false} nodesConnectable={false} deleteKeyCode={null}
          nodesFocusable edgesFocusable
          onNodeClick={(_, node) => inspectEntity(node.id)}
          onEdgeClick={(_, edge) => { setEdgeId(edge.id); }}>
          <Background color="var(--cp-border)" gap={24} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <p className="muted graph-caption">Two lenses, the same captured facts—not a controlled comparison or an accuracy proof.
        Pan, zoom or fit the view. Arrows show definition direction in Fabric, not internal execution or a materialized instance graph.</p>
      <details className="graph-access">
        <summary>Browse entities &amp; relationships by keyboard</summary>
        <div className="entity-picker" aria-label="Captured entities">{recording.entities.map(item =>
          <button key={item.id} aria-pressed={selected === item.id && !edgeId} onClick={() => {
            if (!recording.focus_entity_ids.includes(item.id)) setFocus(false);
            inspectEntity(item.id);
          }}>{item.id}</button>)}</div>
        <label>Inspect a captured relationship<select value={edgeId ?? ""} onChange={event => setEdgeId(event.target.value || null)}>
          <option value="">Select an ontology relationship</option>
          {recording.relationships.map(item => <option key={item.id} value={item.id}>{item.id}</option>)}
        </select></label>
      </details>
    </section>

    <section className="detail-grid" aria-label="Selected ontology context">
      <article className="card entity-detail" aria-live="polite">
        <p className="eyebrow">{relationship ? "ONTOLOGY RELATIONSHIP" : `${sourceLabel(entity?.source_id ?? "")} / ONTOLOGY ENTITY`}</p>
        <h2>{relationship?.id ?? entity?.id ?? "Select an entity"}</h2>
        {relationship ? <><dl><dt>Relationship source</dt><dd>{relationship.source}</dd>
          <dt>Relationship target</dt><dd>{relationship.target}</dd></dl>
          <p>Structural relationship only. Selecting this link does not establish that the ontology agent in Fabric traversed it.</p></>
          : entity && <><p>{known(entity.description)}</p>
            <dl><dt>Source model</dt><dd>{known(source?.name)}</dd>
              <dt>Backing table</dt><dd>{known(entity.backing_table)}</dd>
              <dt>Entity keys</dt><dd>{entity.keys.join(", ") || "Not defined in this capture"}</dd></dl>
            <details><summary>{entity.properties.length} recorded properties</summary>
              <p>{entity.properties.join(", ") || "No properties captured"}</p></details></>}
        <h3>Captured business meaning</h3>
        <span className="origin-tag">CAPTURED_NATIVE_ANNOTATION</span>
        {annotations.length ? annotations.map((text, index) => <blockquote className="guidance" key={index}>{text}</blockquote>)
          : <p className="muted">No guidance annotation captured on this selection.</p>}
        <p className="context-origin">Origin: recorded ontology context in Fabric · {annotationOwner ?? "No selection"}<br />
          Capture: {captureTime(recording.context_captured_at)} · definition/MCP hashes in provenance.</p>
      </article>
      <article className="card metric-detail">
        <p className="eyebrow">SOURCE-OWNED CALCULATIONS</p>
        <h2>{entityMetrics.length} metrics on {entity?.id ?? "the selected entity"}</h2>
        {entityMetrics.length ? <div className="metric-list">{entityMetrics.map(metric => {
          const owner = metricEvidence(recording, metric);
          return <details key={metric.id}>
            <summary>{metric.name}</summary><p>{known(metric.description)}</p>
            <dl><dt>Owning model</dt><dd>{known(owner.source?.name)}</dd>
              <dt>Home table</dt><dd>{known(metric.home_table)}</dd>
              <dt>Source measure</dt><dd>{known(metric.name)}</dd>
              <dt>Metric link</dt><dd><code>{metric.id}</code></dd></dl>
            <button onClick={() => showEvidence(metric)} aria-controls="recorded-evidence">Inspect binding &amp; captured query</button>
          </details>;
        })}</div> : <p className="muted">This entity carries business context; no metric binding is captured on it.</p>}
        <p className="muted">Observed EXTERNALMEASURE metrics linked to source measures. No formula is copied or replaced by this viewer.</p>
      </article>
    </section>

    <section className="card context-authority" aria-label="Context and rule authority">
      <div className="section-heading"><div><p className="eyebrow">MEANING IS NOT AUTOMATIC EXECUTION</p>
        <h2>What context is actually captured?</h2></div><span className="status-pill">{annotationCount} guidance annotations in this recording</span></div>
      <div className="authority-grid">
        <div><span className="origin-tag">CAPTURED_NATIVE_ANNOTATION</span><h3>Linked business guidance</h3>
          <p>Inspect actual entity and relationship annotations above. Their presence is not proof of use or enforcement in Fabric.</p></div>
        <div><span className="origin-tag">FORMAL_NATIVE_RULE</span><h3>Formal Rule count: {recording.native_rule_count}</h3>
          <p>{recording.native_rule_count === 0 ? "No formal Rule objects in this capture."
            : "Count only: no formal Rule payload in this recording."} No rule use or execution is established.</p></div>
        <div><span className="origin-tag">SOURCE_AI_PREPARATION</span><h3>Not captured in this recording</h3>
          <p>Ontology annotations do not establish source AI-preparation content or its propagation to a consumer.</p></div>
        <div><span className="origin-tag">CUSTOM_CONSTRAINT</span><h3>No application run captured</h3>
          <p>This ontology-agent recording does not establish custom planner consumption or deterministic constraint enforcement.</p></div>
      </div>
    </section>

    <details id="recorded-evidence" ref={evidenceRef} className="card evidence-drawer"
      open={evidenceOpen} onToggle={event => setEvidenceOpen(event.currentTarget.open)}>
      <summary>Source queries &amp; recorded account results</summary>
      <p className="muted">DAX displayed by the ontology agent in Fabric with its answer. Internal tool timings and execution steps were not captured.</p>
      {selectedMetric && binding && <section className="binding-evidence" aria-label="Selected metric evidence">
        <p className="eyebrow">INSPECTABLE BINDING · NOT AN INTERNAL REASONING TRACE</p>
        <h3>{selectedMetric.name}</h3>
        <ol className="evidence-path">
          <li><span>Metric link</span><strong>{known(binding.entity?.id)}</strong><code>{selectedMetric.id}</code></li>
          <li><span>Source calculation owner</span><strong>{known(binding.source?.name)}</strong>
            <code>{selectedMetric.home_table} / {selectedMetric.name}</code></li>
          <li><span>Captured source query</span><strong>{binding.queries.length ? `${binding.queries.length} matching captured query` : "Not captured for this binding"}</strong>
            <small>Matched by exact source and measure reference; not proof of an internal execution step.</small></li>
          <li><span>Returned answer column</span><strong>{selectedColumn ?? "Not present in the recorded answer DTO"}</strong>
            <small>No query is rerun when selecting a metric.</small></li>
        </ol>
        {binding.queries.map((query, index) => <pre key={index}>{query.dax}</pre>)}
        {selectedRow && selectedColumn && <p className="selected-account-evidence">Account {selectedRow.account_key} · {selectedMetric.name}: <strong>
          {number.format(selectedRow[selectedColumn])}</strong>{selectedColumn.endsWith("_cents") ? " cents" : " lines"}</p>}
      </section>}
      <div className="comparison-grid">{recording.queries.map((query, index) => <div key={`${query.source_id}-${index}`}>
        <h3>{sourceLabel(query.source_id)} source query</h3>
        <p className="muted">{known(recording.sources.find(item => item.id === query.source_id)?.name)}</p><pre>{query.dax}</pre>
      </div>)}</div>
      <h3>Returned account rows · recorded answer</h3>
      <p className="muted">Select an account to inspect its returned values. These are answer rows, not separately retained raw source responses.
        Zeros remain zero; missing accounts are not filled. Completeness is not revalidated.</p>
      <div className="table-scroll"><table>
        <thead><tr><th scope="col">Account key</th><th scope="col">Active lines EOM</th><th scope="col">Recognized revenue · cents</th></tr></thead>
        <tbody>{recording.rows.map(row => <tr key={row.account_key} className={accountKey === row.account_key ? "selected-account" : undefined}>
          <td><button className="account-key" aria-pressed={accountKey === row.account_key}
            aria-label={`Inspect captured account ${row.account_key}`} onClick={() => setAccountKey(row.account_key)}>{row.account_key}</button></td>
          <td>{number.format(row.active_lines_eom)}</td><td>{number.format(row.recognized_service_revenue_cents)}</td></tr>)}</tbody>
      </table></div>
      {selectedRow && <p aria-live="polite">Captured account {selectedRow.account_key}: {number.format(selectedRow.active_lines_eom)} active lines EOM;
        {" "}{number.format(selectedRow.recognized_service_revenue_cents)} recognized revenue cents. Same captured answer, no new query.</p>}
    </details>

    <section className="card fair-comparison" aria-label="Fair comparison boundary">
      <div className="section-heading"><h2>Why organize context as an ontology?</h2><span className="status-pill">PAIRED BASELINE: NOT RECORDED</span></div>
      <p>Semantic models already own calculations and can perform joins. The ontology organizes reusable business meaning,
        relationships and metric bindings above those models—so a consumer can inspect the right identity, time and grain.</p>
      <p className="muted">A fair baseline must receive equivalent facts, source AI preparation and permissions.
        No paired GA IQ run was captured here. These lenses do not demonstrate a speed or accuracy advantage.</p>
    </section>
    <details className="card provenance-drawer">
      <summary>Limits of this recording</summary>
      <p>Answer origin: BUILT_IN_NATIVE_AGENT. Display mode: {recording.mode}. See <code>docs/limitations.md</code>.</p>
      <p>Context captured <time dateTime={recording.context_captured_at}>{captureTime(recording.context_captured_at)}</time>.</p>
      <p>Recording created {captureTime(recording.created_at)}. Creation time is not answer or context capture time.</p>
      <dl><dt>Source data refresh</dt><dd>Not captured</dd><dt>Source-model revision/refresh</dt><dd>Not captured</dd>
        <dt>Ontology refresh time</dt><dd>Not captured; context acquisition is a separate clock</dd>
        <dt>Context acquisition</dt><dd>{captureTime(recording.context_captured_at)}</dd></dl>
      <p>This recording is never used as a silent fallback for a live query.</p>
      <ul>{recording.limitations.map(text => <li key={text}>{text}</li>)}</ul>
      <h3>Original capture hashes</h3><pre>{JSON.stringify(recording.source_hashes, null, 2)}</pre>
    </details>
    <footer>Display-only recording from synthetic Northstar Wireless demo tenant data. The six additional models, formal Rule payloads,
      three multihop stories, Foundry integration and current live application execution are not demonstrated by this view. See <code>docs/limitations.md</code>.</footer>
  </main>;
}
