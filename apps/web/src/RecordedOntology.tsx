import { useMemo, useState } from "react";
import {
  Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import { planNodes, type ExecutionEvent } from "./execution";
import type { PairResult } from "./ComparisonView";

export type RecordedOntology = {
  captured_at: string; native_snapshot_hash: string; provenance: "ACTUAL_NATIVE_DEFINITION_AND_METADATA_CAPTURE";
  sources: string[];
  entities: { id: string; model: string; table: string; name: string; description: string; keys: string[] }[];
  relationships: { id: string; source: string; target: string; description: string }[];
  metrics: { id: string; model: string; table: string; entity: string; name: string;
    description: string; representation: string }[];
  rules: { id: string; statement: string; referenced_entities: string[]; property_scope: string }[];
  execution_relationships: Record<string, string>; limitations: string[];
};
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === "string");
}
function fields(value: unknown, names: string[]): value is Record<string, string> {
  return object(value) && names.every(name => typeof value[name] === "string");
}
export function isRecordedOntology(value: unknown): value is RecordedOntology {
  if (!object(value) || value.provenance !== "ACTUAL_NATIVE_DEFINITION_AND_METADATA_CAPTURE"
    || typeof value.captured_at !== "string" || !Number.isFinite(Date.parse(value.captured_at))
    || typeof value.native_snapshot_hash !== "string" || !/^[0-9a-f]{64}$/.test(value.native_snapshot_hash)
    || !strings(value.sources)) return false;
  const sources = value.sources;
  if (!Array.isArray(value.entities)
    || !value.entities.every(entity => fields(entity, ["id", "model", "table", "name", "description"])
      && strings(entity.keys) && sources.includes(entity.model))
    || !Array.isArray(value.relationships) || !value.relationships.every(edge =>
      fields(edge, ["id", "source", "target", "description"]))
    || !Array.isArray(value.metrics) || !value.metrics.every(metric =>
      fields(metric, ["id", "model", "table", "entity", "name", "description", "representation"]))
    || !Array.isArray(value.rules) || !value.rules.every(rule =>
      fields(rule, ["id", "statement", "property_scope"]) && strings(rule.referenced_entities))
    || !object(value.execution_relationships) || !Object.values(value.execution_relationships)
      .every(id => typeof id === "string") || !strings(value.limitations)) return false;
  const ids = new Set(value.entities.map(entity => entity.id));
  const entities = new Map(value.entities.map(entity => [entity.id, entity]));
  const edges = new Set(value.relationships.map(edge => edge.id));
  return ids.size === value.entities.length && edges.size === value.relationships.length
    && value.relationships.every(edge => ids.has(edge.source) && ids.has(edge.target))
    && value.metrics.every(metric => {
      const entity = entities.get(metric.entity);
      return entity !== undefined && metric.model === entity.model && metric.table === entity.table;
    })
    && Object.values(value.execution_relationships).every(id => typeof id === "string" && edges.has(id));
}
type EntityNode = Node<{ name: string; model: string; used: boolean; metrics: number }, "nativeEntity">;
function Entity({ data }: NodeProps<EntityNode>) {
  return <div className={`entity-node ${data.used ? "focus-node" : ""}`}>
    <Handle type="target" position={Position.Left} />
    <small>{data.model}</small><strong>{data.name}</strong><span>{data.metrics} source measures</span>
    <Handle type="source" position={Position.Right} />
  </div>;
}
const nodeTypes = { nativeEntity: Entity };
const emptyNativeEvents: ExecutionEvent[] = [];

export function RecordedOntologyView({ ontology, result, events, gettingStarted = false, focusMetricIds }: {
  ontology: RecordedOntology; result?: PairResult; events?: Record<"baseline" | "native", ExecutionEvent[]>;
  gettingStarted?: boolean;
  focusMetricIds?: string[];
}) {
  const [source, setSource] = useState(result ? "used" : "all");
  const [selected, setSelected] = useState<string | null>(null);
  const native = result?.arms.find(arm => arm.arm === "native");
  const nativeEvents = events?.native ?? emptyNativeEvents;
  const displayedMetrics = new Set(focusMetricIds ?? (result ? Object.keys(native?.execution?.answer?.rows[0] ?? {})
    : ontology.metrics.map(metric => metric.id)));
  const graph = useMemo(() => {
    const completed = new Set(nativeEvents.filter(event => event.phase === "completed").map(event => event.node_id));
    const nodes = planNodes(native?.plan);
    const used = new Set(nodes.filter(node => node.kind === "QuerySemanticModel" && completed.has(node.id))
      .map(node => `${node.model}:${node.table}`));
    const traversed = new Set(nodes.filter(node => completed.has(node.id) && node.relationship)
      .map(node => ontology.execution_relationships[node.relationship!]).filter(Boolean));
    const entities = ontology.entities.filter(entity => source === "all"
      || (source === "used" ? used.has(`${entity.model}:${entity.table}`) : entity.model === source));
    const levels = new Map<string, number>();
    if (gettingStarted && (source === "used" || !result)) {
      const anchor = ontology.metrics.find(metric => metric.id === "subscriptions.active_lines_eom")?.entity;
      if (anchor && entities.some(entity => entity.id === anchor)) {
        levels.set(anchor, 0);
        const queue = [anchor];
        for (let index = 0; index < queue.length; index++) {
          const current = queue[index];
          for (const edge of ontology.relationships) {
            const next = edge.source === current ? edge.target : edge.target === current ? edge.source : null;
            if (next && !levels.has(next) && entities.some(entity => entity.id === next)) {
              levels.set(next, levels.get(current)! + 1);
              queue.push(next);
            }
          }
        }
      }
    }
    const rows = new Map<string, number>();
    const models = [...new Set(entities.map(entity => entity.model))];
    const flowNodes: EntityNode[] = entities.map(entity => {
      const index = models.indexOf(entity.model);
      const row = rows.get(entity.model) ?? 0;
      rows.set(entity.model, row + 1);
      return {
        id: entity.id, type: "nativeEntity", selected: entity.id === selected,
        position: levels.size === entities.length ? { x: levels.get(entity.id)! * 310, y: 0 }
          : { x: index * 310, y: row * 120 },
        data: { name: gettingStarted && entity.table === "account" ? "Customer accounts" : entity.name,
          model: entity.model, used: used.has(`${entity.model}:${entity.table}`),
          metrics: ontology.metrics.filter(metric => metric.entity === entity.id
            && (!gettingStarted || displayedMetrics.has(metric.id))).length },
      };
    });
    const ids = new Set(entities.map(entity => entity.id));
    const flowEdges: Edge[] = ontology.relationships.filter(edge => ids.has(edge.source) && ids.has(edge.target))
      .map(edge => ({
        id: edge.id, source: edge.source, target: edge.target, type: "smoothstep",
        label: !gettingStarted && traversed.has(edge.id) ? "Observed traversal" : undefined,
        style: { stroke: traversed.has(edge.id) ? "var(--cp-accent)" : "var(--cp-border-strong)",
          strokeWidth: traversed.has(edge.id) ? 2.5 : 1 },
        markerEnd: { type: MarkerType.ArrowClosed, color: "var(--cp-border-strong)" },
      }));
    return { nodes: flowNodes, edges: flowEdges, entities, used, traversed };
  }, [ontology, nativeEvents, native?.plan, native?.execution?.answer, source, selected, gettingStarted, result]);
  const entity = ontology.entities.find(item => item.id === selected)
    ?? graph.entities.find(item => ontology.metrics.some(metric => metric.entity === item.id));
  const metrics = ontology.metrics.filter(metric => metric.entity === entity?.id
    && (!gettingStarted || displayedMetrics.has(metric.id)));
  const rules = ontology.rules.filter(rule => !gettingStarted || rule.referenced_entities
    .some(id => graph.entities.some(item => item.id === id)));
  return <section className="card" aria-label="Captured ontology in Fabric">
    <div className="section-heading"><div><p className="eyebrow">FABRIC IQ ONTOLOGY (CURRENT EXPERIENCE) IN FABRIC</p>
      <h2>Connected business meaning</h2></div><span className="status-pill">ONTOLOGY METADATA / RECORDED</span></div>
    {gettingStarted && <p><strong>{result
      ? `${new Set((native?.execution?.receipts ?? []).map(receipt => receipt.model)).size} source models in this query`
      : `${ontology.sources.length} source models in this ontology`}</strong> · {graph.entities.length} captured source entities.
      Explore the account bridge and each source-owned measure below.</p>}
    <details open={!gettingStarted}><summary>Full captured environment</summary>
    <p>{ontology.sources.length} prepared source models · {ontology.entities.length} entities · {
      ontology.relationships.length} relationships · {ontology.metrics.length} source measures · {
      ontology.rules.length} formal Rules</p>
    </details>
    <p className="muted">Definition graph, not an instance graph. Rose edges map to completed application
      traversals; grey edges are captured definitions, not proof that they ran.</p>
    <label htmlFor="ontology-source">Explore ontology sources</label>
    <select id="ontology-source" value={source} onChange={event => { setSource(event.target.value); setSelected(null); }}>
      {result && <option value="used">Entities queried by this ontology-grounded run</option>}
      <option value="all">Full captured definition</option>
      {ontology.sources.map(model => <option key={model} value={model}>{model}</option>)}
    </select>
    <div className="native-graph focused-graph">
      <ReactFlow key={`${result?.pair_id ?? ontology.native_snapshot_hash}:${source}`}
        nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes}
        fitView minZoom={0.1} maxZoom={1.5} nodesDraggable={false} nodesConnectable={false}
        deleteKeyCode={null} onNodeClick={(_, node) => setSelected(node.id)}>
        <Background color="var(--cp-border)" gap={24} /><Controls showInteractive={false} />
      </ReactFlow>
    </div>
    {entity && <div className="detail-grid">
      <div><h3>{entity.model} / {entity.name}</h3><p>{entity.description}</p>
        <p className="muted">Captured ontology entity: {entity.id}.</p>
        <p className="muted">Declared entity key: {entity.keys.join(", ") || "None captured"}.
          Execution still uses reviewed complete source/validity grains.</p>
        {metrics.map(metric => <details key={metric.id}><summary>{metric.name}</summary>
          <p>{metric.description}</p><p>Source: {metric.model} / {metric.table} / {metric.name}</p>
          <p className="muted">{metric.representation} · Calculation remains in the source semantic model.</p>
        </details>)}
      </div>
      <div><h3>Captured cross-domain Rules</h3>
        {rules.map(rule => <details key={rule.id}><summary>{rule.id}</summary>
          <p>{rule.statement}</p><p className="muted">Scope: {rule.property_scope};
            references {rule.referenced_entities.join(", ")}.</p>
        </details>)}
        <p className="muted">These are Rule objects in Fabric. Their presence does not prove
          ask_ontology consideration or deterministic rule execution.</p>
      </div>
    </div>}
    <details><summary>Ontology capture provenance</summary><p>{ontology.captured_at}</p>
      <pre>{ontology.native_snapshot_hash}</pre>
      {ontology.limitations.map(item => <p key={item}>{item}</p>)}
    </details>
  </section>;
}
