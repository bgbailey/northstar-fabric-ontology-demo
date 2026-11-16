import { describe, expect, it } from "vitest";
import { dependencies, executionState, isResult, planNodes, readExecutionStream, type ExecutionEvent, type ExecutionResult } from "./execution";

const event: ExecutionEvent = {
  run_id: "test-run", node_id: "source", operation: "QuerySemanticModel", phase: "completed",
  lane: "SYNTHETIC_UNIT_TRANSPORT", elapsed_ms: 1, returned_rows: 0, model: "synthetic",
  receipt_id: null, status: "COMPLETE", message: "\u0394", observed_at: "2026-10-01T00:00:00Z",
};
const result: ExecutionResult = {
  request_id: "test-run", status: "FAILED", mode: "SYNTHETIC_UNIT_TRANSPORT",
  errors: ["Synthetic transport fixture, not a source execution."], elapsed_ms: 2, receipts: [], answer: null,
};
function resultWithReceipt(parameters: unknown) {
  return {
    ...result, status: "COMPLETE", errors: [],
    receipts: [{
      id: "unit-receipt", node_id: "source", lane: "SYNTHETIC_UNIT_TRANSPORT", tool: "unit",
      model: "synthetic", query: "Synthetic unit query, not executed.", source_watermark: null,
      elapsed_ms: 1, returned_rows: 1, status: "COMPLETE",
      completeness_evidence: "Synthetic receipt contract fixture, not source execution.",
      clocks: { data: "unit-data", model: "unit-model", ontology: "unit-ontology", context: "unit-context" },
      warnings: [], ...(parameters === undefined ? {} : { parameters }),
    }],
    answer: { rows: [{ unit_value: 17 }], evidence_ids: ["unit-receipt"], warnings: [], coverage: {} },
  };
}
const frame = (name: string, data: unknown) => `event: ${name}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
function response(text: string) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (let offset = 0; offset < bytes.length; offset += 7) controller.enqueue(bytes.slice(offset, offset + 7));
      controller.close();
    },
  }), { headers: { "content-type": "text/event-stream; charset=utf-8" } });
}

describe("observed execution streaming", () => {
  it("handles chunked UTF-8 and CRLF frames without inventing a successful result", async () => {
    const seen: ExecutionEvent[] = [];
    const actual = await readExecutionStream(response(frame("trace", event) + frame("result", result)),
      "test-run", item => seen.push(item));
    expect(seen).toEqual([event]);
    expect(actual.status).toBe("FAILED");
    expect(actual.answer).toBeNull();
  });
  it("rejects truncation, a different run and unrecognized events", async () => {
    await expect(readExecutionStream(response(frame("trace", event)), "test-run", () => {}))
      .rejects.toThrow("terminal result");
    await expect(readExecutionStream(response(frame("trace", { ...event, run_id: "other" })), "test-run", () => {}))
      .rejects.toThrow("mismatched");
    await expect(readExecutionStream(response(frame("imaginary-thinking", {})), "test-run", () => {}))
      .rejects.toThrow("Unrecognized");
  });
  it("surfaces server failure and rejects trailing events after a result", async () => {
    await expect(readExecutionStream(response(frame("error", { detail: "Source failed." })), "test-run", () => {}))
      .rejects.toThrow("Source failed.");
    await expect(readExecutionStream(response(frame("result", result) + frame("trace", event)), "test-run", () => {}))
      .rejects.toThrow("after the terminal");
  });
  it("includes cohort dependencies and tolerates temporarily malformed edited plans", () => {
    expect(dependencies({ id: "q", kind: "QuerySemanticModel", depends_on: ["parent"],
      cohort_filters: [{ source: "cohort" }, { source: "parent" }] })).toEqual(["parent", "cohort"]);
    expect(planNodes({ nodes: [{ id: "q", kind: "QuerySemanticModel", cohort_filters: "bad" }] })).toEqual([]);
    expect(planNodes({ nodes: [{ id: "q", kind: "QuerySemanticModel", source: "parent" }] })).toHaveLength(1);
  });
  it("does not show skipped downstream nodes as attempted failures", () => {
    expect(executionState({ ...event, phase: "skipped", status: "BLOCKED_CAPABILITY" })).toBe("skipped");
    expect(executionState({ ...event, phase: "failed", status: "BLOCKED_CAPABILITY" })).toBe("BLOCKED_CAPABILITY");
    expect(executionState()).toBe("planned");
  });
  it("rejects integer values that JSON numeric parsing cannot represent exactly", () => {
    expect(isResult({ ...result, status: "COMPLETE", answer: {
      rows: [{ cents: Number.MAX_SAFE_INTEGER + 1 }], evidence_ids: [], warnings: [], coverage: {},
    } })).toBe(false);
  });
  it("preserves separate source-grain views and rejects unsafe numbers or absent view contracts", () => {
    const payload = resultWithReceipt(null);
    const separate = { ...payload.answer, status: "COMPLETE", grain: ["account_key"] };
    expect(isResult({ ...payload, additional_answers: { "Account revenue": separate } })).toBe(true);
    expect(isResult({ ...payload, additional_answers: { "Account revenue": payload.answer } })).toBe(false);
    expect(isResult({ ...payload, additional_answers: { "Account revenue": {
      ...separate, rows: [{ cents: Number.MAX_SAFE_INTEGER + 1 }],
    } } })).toBe(false);
    expect(dependencies({ id: "answer", kind: "SynthesizeAnswer", source: "contracts",
      additional_sources: { "Account revenue": "finance", "Settlements": "postings" } }))
      .toEqual(["contracts", "finance", "postings"]);
    expect(planNodes({ nodes: [{ id: "answer", kind: "SynthesizeAnswer",
      additional_sources: { "Account revenue": 42 } }] })).toEqual([]);
  });
  it("accepts evidenced known-mapped partial qualification but never a complete qualified total", () => {
    const payload = resultWithReceipt(null);
    const answer = { ...payload.answer, status: "PARTIAL", population_qualification: "known_mapped_subset",
      exclusions: [{ source_node: "cohort", source_column: "account_key", unknown_mapping_observations: 2,
        null_key_rows: 1, evidence_ids: ["unit-evidence"] }] };
    expect(isResult({ ...payload, status: "PARTIAL", answer })).toBe(true);
    expect(isResult({ ...payload, answer: { ...answer, status: "COMPLETE" } })).toBe(false);
    expect(isResult({ ...payload, answer: { ...answer, exclusions: [] } })).toBe(false);
    expect(isResult({ ...payload, answer: { ...answer,
      exclusions: [{ ...answer.exclusions[0], null_key_rows: -1 }] } })).toBe(false);
  });
  it.each([
    { name: "omitted", parameters: undefined },
    { name: "null", parameters: null },
    { name: "empty array", parameters: [] },
    { name: "scalar array", parameters: ["unit", 17, true, null] },
  ])("accepts $name parameters in nonempty receipt results and execution streams", async ({ parameters }) => {
    const payload = resultWithReceipt(parameters);
    expect(isResult(payload)).toBe(true);
    const actual = await readExecutionStream(response(frame("trace", event) + frame("result", payload)),
      "test-run", () => {});
    expect(actual).toEqual(payload);
    expect(actual.receipts).toHaveLength(1);
  });
  it.each([
    { name: "object", parameters: {} },
    { name: "string", parameters: "unit" },
    { name: "object array member", parameters: [17, {}] },
    { name: "nested array member", parameters: [[]] },
    { name: "undefined array member", parameters: [undefined] },
    { name: "nonfinite array member", parameters: [Number.POSITIVE_INFINITY] },
  ])("rejects $name parameters without weakening receipt validation", ({ parameters }) => {
    expect(isResult(resultWithReceipt(parameters))).toBe(false);
  });
});
