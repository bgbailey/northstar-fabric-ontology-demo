import { useMemo } from "react";
import {
  Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import { dependencies, executionState, type ExecutionEvent, type PlanNodeView } from "./execution";

type StepData = { label: string; operation: string; state: string; rows: number | null; milliseconds: number | null };
type StepNode = Node<StepData, "step">;

function Step({ data }: NodeProps<StepNode>) {
  const tone = ["FAILED", "BLOCKED_AUTH", "BLOCKED_CAPABILITY", "TIMED_OUT", "failed"].includes(data.state)
    ? "failed" : data.state === "PARTIAL" || data.state === "skipped" ? "partial"
      : data.state === "COMPLETE" || data.state === "completed" ? "complete" : "pending";
  return <div className={`entity-node execution-node ${tone}`}>
    <Handle type="target" position={Position.Left} />
    <small>{data.operation}</small><strong>{data.label}</strong><span>{data.state}</span>
    {data.rows !== null && <small>{data.rows} rows / {data.milliseconds?.toFixed(1)} ms</small>}
    <Handle type="source" position={Position.Right} />
  </div>;
}
const nodeTypes = { step: Step };

export function ExecutionCanvas({ nodes, events }: { nodes: PlanNodeView[]; events: ExecutionEvent[] }) {
  const graph = useMemo(() => {
    const byId = new Map(nodes.map(node => [node.id, node]));
    const last = new Map<string, ExecutionEvent>();
    events.forEach(event => { if (event.node_id) last.set(event.node_id, event); });
    const levels = new Map<string, number>();
    const level = (id: string, visiting = new Set<string>()): number => {
      if (levels.has(id)) return levels.get(id)!;
      if (visiting.has(id)) return 0;
      visiting.add(id);
      const deps = dependencies(byId.get(id) ?? { id, kind: "" }).filter(key => byId.has(key));
      const depth = deps.length ? 1 + Math.max(...deps.map(key => level(key, new Set(visiting)))) : 0;
      levels.set(id, depth);
      return depth;
    };
    const rows = new Map<number, number>();
    const flowNodes: StepNode[] = nodes.map(node => {
      const depth = level(node.id);
      const row = rows.get(depth) ?? 0;
      rows.set(depth, row + 1);
      const event = last.get(node.id);
      return {
        id: node.id, type: "step", position: { x: depth * 290, y: row * 160 },
        ariaLabel: `${node.id}: ${executionState(event)}`,
        data: { label: node.id, operation: node.kind, state: executionState(event),
          rows: event?.returned_rows ?? null, milliseconds: event?.elapsed_ms ?? null },
      };
    });
    const edges: Edge[] = nodes.flatMap(node => dependencies(node).filter(id => byId.has(id)).map(source => ({
      id: `${source}:${node.id}`, source, target: node.id, type: "smoothstep",
      animated: false, markerEnd: { type: MarkerType.ArrowClosed, color: "var(--cp-border-strong)" },
    })));
    return { nodes: flowNodes, edges };
  }, [nodes, events]);
  return <section className="card">
    <h2>Observed execution</h2>
    <p className="muted">Planned nodes stay grey until an actual runtime event arrives. This is local engine execution,
      not model-generated reasoning or an internal service trace in Fabric.</p>
    <div className="native-graph execution-graph">
      <ReactFlow key={nodes.map(node => node.id).join("|")} nodes={graph.nodes} edges={graph.edges}
        nodeTypes={nodeTypes} fitView minZoom={0.15} maxZoom={1.5} nodesDraggable={false}
        nodesConnectable={false} deleteKeyCode={null}>
        <Background color="var(--cp-border)" gap={24} /><Controls showInteractive={false} />
      </ReactFlow>
    </div>
    <details><summary>{events.length} actual execution events</summary>
      <div className="table-scroll"><table><thead><tr><th>Node</th><th>Action</th><th>State</th><th>Rows</th><th>Elapsed</th></tr></thead>
        <tbody>{events.map((event, index) => <tr key={index}><td>{event.node_id ?? "Plan"}</td>
          <td>{event.operation}</td><td>{event.phase} / {event.status ?? "-"}</td><td>{event.returned_rows ?? "-"}</td>
          <td>{event.elapsed_ms.toFixed(1)} ms</td></tr>)}</tbody></table></div>
    </details>
  </section>;
}
