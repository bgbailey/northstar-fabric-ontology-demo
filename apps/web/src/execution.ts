export type ExecutionEvent = {
  run_id: string; node_id: string | null; operation: string;
  phase: "started" | "completed" | "failed" | "skipped"; lane: string;
  elapsed_ms: number; returned_rows: number | null; model: string | null;
  receipt_id: string | null; status: string | null; message: string | null; observed_at: string;
};
export type Receipt = {
  id: string; node_id: string; lane: string; tool: string; model: string | null;
  query: string | null; elapsed_ms: number; returned_rows: number; status: string;
  completeness_evidence: string; clocks: Record<string, string>; warnings: string[];
  source_watermark: string | null; parameters?: (string | number | boolean | null)[] | null;
};
export type AnswerView = {
  rows: Record<string, unknown>[]; evidence_ids: string[]; coverage: Record<string, number>; warnings: string[];
  status?: string; grain?: string[] | null;
  clocks?: Record<"data" | "model" | "ontology" | "context", string>;
  source_clocks?: { model: string; table: string; data: string; model_revision: string; table_data?: string | null }[];
  population_qualification?: "unqualified" | "known_mapped_subset";
  exclusions?: {
    source_node: string; source_column: string; unknown_mapping_observations: number;
    null_key_rows: number; evidence_ids: string[];
  }[];
};
export type ExecutionResult = {
  request_id: string; status: string; mode: string; errors: string[]; elapsed_ms: number;
  receipts: Receipt[];
  answer: AnswerView | null;
  additional_answers?: Record<string, AnswerView>;
};
export type PlanNodeView = {
  id: string; kind: string; model?: string; left?: string; right?: string; source?: string;
  table?: string; relationship?: string;
  depends_on?: string[]; cohort_filters?: { source: string }[];
  additional_sources?: Record<string, string>;
};

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === "string");
}
function nullableString(value: unknown) { return value === null || typeof value === "string"; }
function elapsed(value: unknown) { return typeof value === "number" && Number.isFinite(value) && value >= 0; }
function isClocks(value: unknown): boolean {
  return object(value) && ["data", "model", "ontology", "context"].every(key => typeof value[key] === "string");
}

export function isTrace(value: unknown): value is ExecutionEvent {
  return object(value) && typeof value.run_id === "string" && nullableString(value.node_id)
    && typeof value.operation === "string" && typeof value.phase === "string"
    && ["started", "completed", "failed", "skipped"].includes(value.phase)
    && typeof value.lane === "string" && elapsed(value.elapsed_ms)
    && (value.returned_rows === null || (Number.isInteger(value.returned_rows) && Number(value.returned_rows) >= 0))
    && nullableString(value.model) && nullableString(value.receipt_id) && nullableString(value.status)
    && nullableString(value.message) && typeof value.observed_at === "string";
}
function isReceipt(value: unknown): value is Receipt {
  return object(value) && ["id", "node_id", "lane", "tool", "status", "completeness_evidence"]
    .every(key => typeof value[key] === "string")
    && nullableString(value.model) && nullableString(value.query) && nullableString(value.source_watermark)
    && elapsed(value.elapsed_ms) && Number.isInteger(value.returned_rows) && Number(value.returned_rows) >= 0
    && strings(value.warnings) && object(value.clocks) && Object.values(value.clocks).every(v => typeof v === "string")
    && (value.parameters === undefined || value.parameters === null || (Array.isArray(value.parameters) && value.parameters.every(v =>
      v === null || typeof v === "string" || typeof v === "boolean" || (typeof v === "number" && Number.isFinite(v)))));
}
export function isResult(value: unknown): value is ExecutionResult {
  return object(value) && typeof value.request_id === "string" && typeof value.status === "string"
    && typeof value.mode === "string" && strings(value.errors) && elapsed(value.elapsed_ms)
    && Array.isArray(value.receipts) && value.receipts.every(isReceipt)
    && (value.answer === null || isAnswerView(value.answer))
    && (value.additional_answers === undefined || (object(value.additional_answers)
      && Object.keys(value.additional_answers).length <= 4
      && Object.entries(value.additional_answers).every(([label, view]) => label.trim().length > 0 && label.length <= 80
        && isAnswerView(view) && typeof view.status === "string"
        && (view.grain === null || strings(view.grain)))));
}
function isAnswerView(value: unknown): value is AnswerView {
  return object(value) && Array.isArray(value.rows)
      && value.rows.every(row => object(row) && Object.values(row).every(cell =>
        typeof cell !== "number" || (Number.isFinite(cell) && (!Number.isInteger(cell) || Number.isSafeInteger(cell)))))
      && strings(value.evidence_ids) && strings(value.warnings)
      && (value.clocks === undefined || isClocks(value.clocks))
      && (value.source_clocks === undefined || (Array.isArray(value.source_clocks) && value.source_clocks.every(clock =>
        object(clock) && ["model", "table", "data", "model_revision"].every(key => typeof clock[key] === "string")
        && (clock.table_data === undefined || clock.table_data === null || typeof clock.table_data === "string"))))
      && object(value.coverage) && Object.values(value.coverage).every(v => typeof v === "number")
      && (value.population_qualification === undefined || value.population_qualification === "unqualified"
        || value.population_qualification === "known_mapped_subset")
      && (value.exclusions === undefined || (Array.isArray(value.exclusions) && value.exclusions.every(item =>
        object(item) && typeof item.source_node === "string" && typeof item.source_column === "string"
        && Number.isSafeInteger(item.unknown_mapping_observations) && Number(item.unknown_mapping_observations) >= 0
        && Number.isSafeInteger(item.null_key_rows) && Number(item.null_key_rows) >= 0 && strings(item.evidence_ids))))
      && (value.population_qualification !== "known_mapped_subset"
        || (value.status === "PARTIAL" && Array.isArray(value.exclusions) && value.exclusions.length > 0));
}

export function dependencies(node: PlanNodeView): string[] {
  return [...new Set([
    ...(node.depends_on ?? []), ...[node.left, node.right, node.source].filter((id): id is string => Boolean(id)),
    ...(node.cohort_filters ?? []).map(filter => filter.source),
    ...Object.values(node.additional_sources ?? {}),
  ])];
}

export function executionState(event?: ExecutionEvent): string {
  return event?.phase === "skipped" ? "skipped" : event?.status ?? event?.phase ?? "planned";
}

export function planNodes(value: unknown): PlanNodeView[] {
  if (!object(value) || !Array.isArray(value.nodes)) return [];
  const isNode = (node: unknown): node is PlanNodeView => object(node)
    && typeof node.id === "string" && typeof node.kind === "string"
    && ["model", "left", "right", "source", "table", "relationship"]
      .every(key => node[key] === undefined || typeof node[key] === "string")
    && (node.depends_on === undefined || strings(node.depends_on))
    && (node.additional_sources === undefined || (object(node.additional_sources)
      && Object.values(node.additional_sources).every(value => typeof value === "string")))
    && (node.cohort_filters === undefined || (Array.isArray(node.cohort_filters)
      && node.cohort_filters.every(filter => object(filter) && typeof filter.source === "string")));
  return value.nodes.every(isNode) ? value.nodes : [];
}

export async function readExecutionStream(
  response: Response, expectedRun: string, onTrace: (event: ExecutionEvent) => void,
): Promise<ExecutionResult> {
  if (!response.ok) throw new Error(await response.text());
  if (!response.headers.get("content-type")?.startsWith("text/event-stream") || !response.body) {
    throw new Error("The server did not return an execution stream.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let result: ExecutionResult | undefined;
  const consume = (frame: string) => {
    const lines = frame.split("\n");
    const name = lines.find(line => line.startsWith("event:"))?.slice(6).trim();
    const data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).replace(/^ /, ""));
    if (!data.length) return;
    if (result) throw new Error("Unexpected event after the terminal execution result.");
    const value: unknown = JSON.parse(data.join("\n"));
    if (name === "error") {
      throw new Error(object(value) && typeof value.detail === "string" ? value.detail : "Execution stream failed.");
    }
    if (name === "trace" && isTrace(value) && value.run_id === expectedRun) {
      onTrace(value);
    } else if (name === "result" && isResult(value) && value.request_id === expectedRun) {
      result = value;
    } else {
      throw new Error("Unrecognized or mismatched execution event; no answer accepted.");
    }
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        consume(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
      }
      if (done) break;
    }
    if (buffer.trim() || !result) throw new Error("Execution stream ended without a complete terminal result.");
    return result;
  } finally {
    reader.releaseLock();
  }
}
