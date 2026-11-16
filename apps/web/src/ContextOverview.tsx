type ContextRelationship = {
  id: string; left_model: string; right_model: string; left_table: string; right_table: string;
  left_keys: string[]; right_keys: string[]; cardinality: string; version: string; purposes: string[];
};
type ContextProjection = {
  mode: string; source: string; revision: string; schema_hash?: string | null;
  scope: { models: string[] }; relationships: ContextRelationship[];
  rules: Record<string, string>[]; provenance: string[];
};
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === "string");
}
/** Presentation validation only; execution and authorization stay with the server. */
export function isContextProjection(value: unknown): value is ContextProjection {
  return object(value) && ["OFFLINE_SYNTHETIC", "NATIVE_SNAPSHOT", "NATIVE_LIVE"].includes(String(value.mode))
    && typeof value.source === "string" && typeof value.revision === "string"
    && (value.schema_hash === undefined || value.schema_hash === null || typeof value.schema_hash === "string")
    && object(value.scope) && strings(value.scope.models) && strings(value.provenance)
    && Array.isArray(value.relationships) && value.relationships.every(edge => object(edge)
      && ["id", "left_model", "right_model", "left_table", "right_table", "version"].every(key => typeof edge[key] === "string")
      && ["one_to_one", "many_to_one", "one_to_many", "many_to_many"].includes(String(edge.cardinality))
      && strings(edge.left_keys) && strings(edge.right_keys) && strings(edge.purposes))
    && Array.isArray(value.rules) && value.rules.every(rule => object(rule)
      && Object.values(rule).every(field => typeof field === "string"));
}

export function ContextOverview({ context, serverMode }: { context: unknown; serverMode: string }) {
  const projection = isContextProjection(context) ? context : null;
  const offline = projection?.mode === "OFFLINE_SYNTHETIC";
  const summary = object(context) && typeof context.detail === "string" ? context.detail : null;
  return <section className="card current-context" aria-label="Current context provenance">
    <div className="section-heading"><div><p className="eyebrow">CONTEXT PROVIDED TO THIS APP</p>
      <h2>{offline ? "Reviewed synthetic business contracts" : "Current context provenance"}</h2></div>
      <span className="status-pill">{projection?.mode ?? "NO VALIDATED CONTEXT PROJECTION"}</span></div>
    <p>{offline
      ? "These are application-owned local contracts, not native ontology metadata. They make identity, grain and period requirements inspectable before local execution."
      : "A sign-in, configured endpoint or tool inventory is not a native ontology capture. This panel does not perform authorization probes or execute queries."}</p>
    {projection ? <>
      <dl className="context-summary"><dt>Source</dt><dd>{projection.source || "Not provided"}</dd>
        <dt>Revision</dt><dd>{projection.revision || "Not provided"}</dd>
        <dt>Source models</dt><dd>{projection.scope.models.join(", ") || "Not provided"}</dd>
        <dt>Schema hash</dt><dd>{projection.schema_hash ?? "Not provided"}</dd></dl>
      <div className="detail-grid">
        <div><h3>{projection.relationships.length} declared relationship contracts</h3>
          {projection.relationships.map(edge => <details key={edge.id}>
            <summary>{edge.id}</summary><dl className="context-summary">
              <dt>Left binding</dt><dd>{edge.left_model} / {edge.left_table} / {edge.left_keys.join(", ")}</dd>
              <dt>Right binding</dt><dd>{edge.right_model} / {edge.right_table} / {edge.right_keys.join(", ")}</dd>
              <dt>Cardinality</dt><dd>{edge.cardinality}</dd><dt>Version</dt><dd>{edge.version}</dd></dl>
            {edge.purposes.map((purpose, index) => <p key={index}>{purpose}</p>)}</details>)}</div>
        <div><h3>{projection.rules.length} supplied guidance entries</h3>
          <span className="origin-tag">{offline ? "CUSTOM_CONSTRAINT / APPLICATION-OWNED INTENT" : "CONTEXT ENTRY / FORMAL RULE AUTHORITY NOT ESTABLISHED"}</span>
          {projection.rules.map((rule, index) => <blockquote className="guidance" key={index}>
            {rule.id && <strong>{rule.id}: </strong>}{rule.statement ?? "No statement field supplied"}
          </blockquote>)}
          <p className="muted">Listing guidance does not establish its use or enforcement in a run.</p></div>
      </div>
      <h3>Supplied provenance</h3><ul>{projection.provenance.map((text, index) => <li key={index}>{text}</li>)}</ul>
    </> : <p className="muted">{summary ?? "No supported context DTO has been provided for this view."}</p>}
    <p className="muted">Native entity, source-metric binding and formal Rule payloads are not projected by this Context DTO.
      They cannot be inferred from relationship contracts or a tool inventory. The recorded viewer is a separate, explicitly captured experience.</p>
    <details><summary>Inspect actual supplied payload · {serverMode}</summary>
      <pre>{JSON.stringify(context, null, 2) ?? "No payload"}</pre></details>
  </section>;
}
