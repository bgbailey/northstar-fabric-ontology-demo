// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ComparisonView, isPairResult } from "./ComparisonView";

function outcome(arm: "baseline" | "native", parameters: unknown) {
  const rows: { unit_value: number | null }[] = [{ unit_value: 17 }];
  const receipt = {
    id: `unit-${arm}`, node_id: "source", lane: "SYNTHETIC_UNIT_TRANSPORT", tool: "unit",
    model: "synthetic", query: `Synthetic ${arm} query, not executed.`, source_watermark: null,
    elapsed_ms: 1, returned_rows: 1, status: "COMPLETE",
    completeness_evidence: "Synthetic receipt contract fixture, not source execution.",
    clocks: { data: "unit-data", model: "unit-model", ontology: "unit-ontology", context: "unit-context" },
    warnings: [], ...(parameters === undefined ? {} : { parameters }),
  };
  return {
    arm, status: "COMPLETE", plan: null, planning_receipt: null, errors: [] as string[], elapsed_ms: 2,
    execution: {
      request_id: `unit-${arm}`, status: "COMPLETE", mode: "SYNTHETIC_UNIT_TRANSPORT",
      errors: [] as string[], elapsed_ms: 1, receipts: [receipt],
      answer: { rows, evidence_ids: [receipt.id], coverage: {}, warnings: [] as string[] },
    },
  };
}
function pair(parameters: unknown) {
  return {
    pair_id: "unit-pair", question: "Synthetic receipt transport test", provider: "SYNTHETIC_UNIT_PROVIDER",
    model_version: "unit-model", source_facts_hash: "unit-facts", context_revision: "unit-context",
    arms: [outcome("baseline", parameters), outcome("native", parameters)],
    comparability: "COMMON_SOURCE_STATE", comparability_reasons: [],
    limitations: ["Synthetic transport test; no tenant, model or source service was called."],
  };
}
function stream() {
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  return {
    response: new Response(body, { headers: { "content-type": "text/event-stream; charset=utf-8" } }),
    send(name: string, data: unknown) {
      const bytes = new TextEncoder().encode(`event: ${name}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`);
      for (let offset = 0; offset < bytes.length; offset += 7) controller.enqueue(bytes.slice(offset, offset + 7));
    },
    close() { controller.close(); },
  };
}

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function startComparison(response?: Response) {
  const events = stream();
  const transport = { response: response ?? events.response };
  const readiness = { ready: true, status: "READY_FOR_CURRENT_READER_PROBE", blockers: [],
    reader_session: "CURRENT_SESSION", native_source_probe: "UNKNOWN_NOT_PROBED",
    claim: "Configuration checks only; current source/native probes have not run." };
  const fetch = vi.fn(async (url: RequestInfo | URL) => {
    if (url === "/api/comparison/readiness") {
      return new Response(JSON.stringify(readiness));
    }
    if (url === "/api/compare/stream") return transport.response;
    throw new Error(`Unexpected request in synthetic comparison test: ${url}`);
  });
  vi.stubGlobal("fetch", fetch);
  await act(async () => root.render(<ComparisonView csrf="unit-csrf" authenticated />));
  const textarea = container.querySelector("textarea");
  const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  if (!textarea || !setValue) throw new Error("Missing comparison question input.");
  await act(async () => {
    setValue.call(textarea, "Synthetic receipt transport test");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const button = container.querySelector<HTMLButtonElement>("button.primary");
  if (!button) throw new Error("Missing comparison run button.");
  expect(button.disabled).toBe(false);
  await act(async () => button.click());
  expect(fetch).toHaveBeenLastCalledWith("/api/compare/stream", expect.objectContaining({
    method: "POST", body: JSON.stringify({ question: "Synthetic receipt transport test" }),
    headers: { "Content-Type": "application/json", "X-CSRF-Token": "unit-csrf" },
  }));
  return { ...events, readiness, transport, fetch };
}

function assertAuthorizationBlocked() {
  expect(container.querySelector("tbody")).toBeNull();
  expect(container.textContent).not.toContain("Common source state verified");
  expect(container.querySelector("aside.notice strong")?.textContent).toBe("BLOCKED_AUTH");
  expect(container.textContent).not.toContain("READY_FOR_CURRENT_READER_PROBE");
  expect(container.querySelector<HTMLButtonElement>("button.primary")?.disabled).toBe(true);
}
async function acceptBaseline(events: ReturnType<typeof stream>) {
  await act(async () => events.send("pair", {
    pair_id: "unit-pair", arm: "baseline", phase: "arm_finished", outcome: outcome("baseline", null),
  }));
  expect(container.querySelectorAll("tbody tr")).toHaveLength(1);
}

describe("customer story questions", () => {
  it("plays an explicitly supplied recording with no readiness, source, auth or model requests", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const recording = pair(null);
    if (!isPairResult(recording)) throw new Error("Synthetic recording fixture violates the display contract.");
    await act(async () => root.render(<ComparisonView csrf={null} authenticated={false}
      recording={recording} recordedEvents={{ baseline: [], native: [] }} />));
    expect(fetch).not.toHaveBeenCalled();
    expect(container.querySelector("textarea")).toBeNull();
    expect(container.querySelector("button.primary")).toBeNull();
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(container.textContent).toContain("RECORDED original live execution");
    expect(container.textContent).toContain("Captured numeric values were checked against original source receipts");
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(fetch).not.toHaveBeenCalled();
  });
  it("selects editable semantic questions without inference, source calls or recorded results", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    await act(async () => root.render(<ComparisonView csrf={null} authenticated={false} />));
    const group = container.querySelector('[aria-label="Customer story questions"]');
    expect(group?.querySelectorAll("button")).toHaveLength(4);
    for (const [label, phrase] of [
      ["Getting started", "Use only these two semantic models"],
      ["SLA exposure", "contract validity intervals"],
      ["Incident recovery", "August 2026 assignment period"],
      ["Checkout release", "explicit session key"],
    ]) {
      const button = group?.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
      if (!button) throw new Error(`Missing story question ${label}`);
      await act(async () => button.click());
      expect(container.querySelector("textarea")?.value).toContain(phrase);
      if (label === "Checkout release") {
        expect(container.querySelector("textarea")?.value).toContain(
          "scope the supporting session-row evidence to checkout-eligible journeys too");
        expect(container.querySelector("textarea")?.value).toContain(
          "including anonymous/unmapped customers and sessions without an order");
        expect(container.querySelector("textarea")?.value).toContain(
          "Do not filter by authentication, account identity or successful purchase");
      }
      expect(button.getAttribute("aria-pressed")).toBe("true");
    }
    expect(fetch).not.toHaveBeenCalled();
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.querySelector<HTMLButtonElement>("button.primary")?.disabled).toBe(true);
  });
  it("does not let a preset alter a running question or trigger a second request", async () => {
    const events = await startComparison();
    const buttons = container.querySelectorAll<HTMLButtonElement>('.story-presets button');
    expect([...buttons].every(button => button.disabled)).toBe(true);
    expect(container.querySelector("textarea")?.value).toBe("Synthetic receipt transport test");
    await act(async () => { events.send("comparison", pair(undefined)); events.close(); });
    expect([...buttons].every(button => !button.disabled)).toBe(true);
  });
});

function rejectedOutcome(arm: "baseline" | "native", candidate: unknown = {
  nodes: [
    { id: "query", kind: "QuerySemanticModel", model: "synthetic" },
    { id: "join", kind: "JoinResults", left: "query", right: "undefined_node" },
  ],
}) {
  return {
    ...outcome(arm, null), status: "FAILED", execution: null,
    errors: ["Missing dependency: node=join, undefined=undefined_node"],
    planning_receipt: {
      validation_status: "REJECTED_NOT_EXECUTED", rejected_candidate: candidate, answer_generated: false,
    },
  };
}

describe("rejected planning diagnostics", () => {
  it("shows the actual clarification instead of implying a source query or accepted answer", async () => {
    const events = await startComparison();
    const clarification = {
      ...rejectedOutcome("baseline", {
        nodes: [{ id: "context", kind: "ReadModelSchema" }],
        hypotheses: ["Confirm separate contract-month and account-month views."],
      }),
      status: "AMBIGUOUS",
    };
    await act(async () => {
      events.send("comparison", {
        ...pair(null), comparability: "NOT_COMPARABLE",
        arms: [clarification, { ...clarification, arm: "native" }],
      });
      events.close();
    });
    expect(container.textContent).toContain("Clarification required - no source queries executed");
    expect(container.textContent).toContain("Confirm separate contract-month and account-month views.");
    expect(container.querySelector('table[aria-label$="business answer"]')).toBeNull();
  });
  it("labels compiler projections separately without creating execution evidence and clears them on auth loss", async () => {
    const events = await startComparison();
    const diagnostic = rejectedOutcome("baseline");
    const compiled = {
      ...diagnostic,
      planning_receipt: {
        ...diagnostic.planning_receipt,
        column_binding: {
          mode: "source_qualified", status: "COMPILED", generated_projection_nodes: ["compiler-column-node"],
          source_queries_at_compilation: 0,
        },
      },
    };
    await act(async () => events.send("pair", {
      pair_id: "unit-pair", arm: "baseline", phase: "arm_finished", outcome: compiled,
    }));
    expect(container.querySelector('aside[aria-label="Server column binding"]')?.textContent)
      .toContain("1 compiler-generated column projections");
    expect(container.querySelector(".execution-graph")).toBeNull();
    expect(container.querySelector('table[aria-label$="business answer"]')).toBeNull();
    await act(async () => events.send("error", { status: "BLOCKED_AUTH", detail: "Synthetic auth loss." }));
    expect(container.querySelector('aside[aria-label="Server column binding"]')).toBeNull();
    assertAuthorizationBlocked();
    await act(async () => events.close());
  });
  it("retains rejected nodes through streamed arms and terminal failure without rendering execution or answers", async () => {
    const events = await startComparison();
    const failed = {
      ...pair(null), comparability: "NOT_COMPARABLE", comparability_reasons: ["Both plans were rejected."],
      arms: [rejectedOutcome("baseline"), rejectedOutcome("native")],
    };
    for (const arm of failed.arms) {
      await act(async () => events.send("pair", {
        pair_id: failed.pair_id, arm: arm.arm, phase: "arm_finished", outcome: arm,
      }));
    }
    expect(container.querySelectorAll('aside[aria-label="Rejected proposal diagnostic"]')).toHaveLength(2);
    expect(container.textContent).toContain("Rejected proposal - not executed");
    expect(container.textContent).toContain("undefined_node");
    expect(container.querySelector('table[aria-label="baseline rejected proposal nodes"] tbody')?.children).toHaveLength(2);
    await act(async () => { events.send("comparison", failed); events.close(); });
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.textContent).toContain("Not a comparable pair");
    expect(container.textContent).not.toContain("Common source state verified");
    expect(container.querySelectorAll('table[aria-label$="business answer"]')).toHaveLength(0);
    expect(container.querySelector(".execution-graph")).toBeNull();
  });
  it("reports malformed diagnostic nodes without promoting them to an executable graph", async () => {
    const events = await startComparison();
    await act(async () => {
      events.send("comparison", {
        ...pair(null), comparability: "NOT_COMPARABLE",
        arms: [rejectedOutcome("baseline", { nodes: ["invalid"] }), rejectedOutcome("native", null)],
      });
      events.close();
    });
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.textContent).toContain("Rejected node details are incomplete");
    expect(container.querySelectorAll('table[aria-label$="rejected proposal nodes"]')).toHaveLength(0);
    expect(container.querySelector(".execution-graph")).toBeNull();
  });
  it("discards rejected proposals as well as answers on source authorization loss", async () => {
    const events = await startComparison();
    await act(async () => events.send("pair", {
      pair_id: "unit-pair", arm: "baseline", phase: "arm_finished", outcome: rejectedOutcome("baseline"),
    }));
    expect(container.textContent).toContain("undefined_node");
    await act(async () => events.send("error", { status: "BLOCKED_AUTH", detail: "Synthetic source permission denied." }));
    expect(container.querySelector('aside[aria-label="Rejected proposal diagnostic"]')).toBeNull();
    expect(container.textContent).not.toContain("undefined_node");
    assertAuthorizationBlocked();
    await act(async () => events.close());
  });
});

describe("separate source-grain answer views", () => {
  it("labels known-mapped partial rows and exclusions without implying a complete population", async () => {
    const events = await startComparison();
    const baseline = outcome("baseline", null);
    await act(async () => events.send("pair", {
      pair_id: "unit-pair", arm: "baseline", phase: "arm_finished",
      outcome: { ...baseline, status: "PARTIAL", execution: {
        ...baseline.execution, status: "PARTIAL", answer: {
          ...baseline.execution.answer, status: "PARTIAL", population_qualification: "known_mapped_subset",
          exclusions: [{ source_node: "mapped-cohort", source_column: "account_key",
            unknown_mapping_observations: 2, null_key_rows: 1, evidence_ids: ["unit-mapping"] }],
        },
      } },
    }));
    expect(container.textContent).toContain("Known mapped subset only");
    expect(container.textContent).toContain("not a complete population ranking or total");
    expect(container.textContent).toContain("2 unknown mapping observations; 1 null-key rows excluded");
    expect(container.textContent).toContain("not a count of distinct accounts");
    await act(async () => events.send("error", { status: "BLOCKED_AUTH", detail: "Synthetic auth loss." }));
    expect(container.textContent).not.toContain("mapped-cohort");
    assertAuthorizationBlocked();
    await act(async () => events.close());
  });
  it("renders independently labelled data and discards every view on authorization loss", async () => {
    const events = await startComparison();
    const baseline = outcome("baseline", null);
    const execution = {
      ...baseline.execution,
      additional_answers: { "Account revenue": {
        rows: [{ account_key: 1, revenue_cents: 321 }], evidence_ids: ["unit-finance"], coverage: {},
        warnings: ["Synthetic source-grain view only."], status: "PARTIAL", grain: ["account_key"],
      } },
    };
    await act(async () => events.send("pair", {
      pair_id: "unit-pair", arm: "baseline", phase: "arm_finished",
      outcome: { ...baseline, status: "PARTIAL", execution: { ...execution, status: "PARTIAL" } },
    }));
    const separate = container.querySelector('section[aria-label="baseline Account revenue result"]');
    expect(separate?.textContent).toContain("Source grain: account_key");
    expect(separate?.textContent).toContain("Incomplete source view");
    expect(separate?.querySelector("tbody")?.textContent).toContain("321");
    expect(container.querySelector('table[aria-label="baseline business answer"]')?.textContent).not.toContain("321");
    await act(async () => events.send("error", { status: "BLOCKED_AUTH", detail: "Synthetic auth loss." }));
    expect(container.textContent).not.toContain("321");
    expect(container.querySelector('section[aria-label="baseline Account revenue result"]')).toBeNull();
    assertAuthorizationBlocked();
    await act(async () => events.close());
  });
});

describe("server authorization invalidation", () => {
  it.each(["error", "trace", "outcome", "execution", "terminal-outcome", "terminal-execution", "terminal-status"])(
    "invalidates both arms and late final frames for BLOCKED_AUTH in %s", async location => {
      const events = await startComparison();
      await acceptBaseline(events);
      const native = outcome("native", null);
      const final = pair(null);
      if (location === "outcome" || location === "terminal-outcome") native.status = "BLOCKED_AUTH";
      if (location === "execution" || location === "terminal-execution") native.execution.status = "BLOCKED_AUTH";
      final.arms[1] = native;
      await act(async () => {
        if (location === "error") {
          events.send("error", { status: "BLOCKED_AUTH", detail: "Synthetic source permission denied." });
        } else if (location === "trace") {
          events.send("pair", { pair_id: "unit-pair", arm: "native", phase: "execution", event: {
            run_id: "unit-native", node_id: "source", operation: "QuerySemanticModel", phase: "failed",
            lane: "SYNTHETIC_UNIT_TRANSPORT", elapsed_ms: 0, returned_rows: null, model: "synthetic",
            receipt_id: null, status: "BLOCKED_AUTH", message: "Synthetic source permission denied.",
            observed_at: "2026-10-15T04:43:00Z",
          } });
        } else if (location.startsWith("terminal")) {
          events.send("comparison", location === "terminal-status" ? { ...final, status: "BLOCKED_AUTH" } : final);
        } else {
          events.send("pair", { pair_id: "unit-pair", arm: "native", phase: "arm_finished", outcome: native });
        }
      });
      assertAuthorizationBlocked();
      expect(container.querySelector('[role="alert"]')?.textContent).toContain("authorization");
      await act(async () => { events.send("comparison", pair(null)); events.close(); });
      assertAuthorizationBlocked();
    },
  );
  it("clears both cached arm answers, not only the arm that lost source permission", async () => {
    const events = await startComparison();
    for (const arm of pair(null).arms) {
      await act(async () => events.send("pair", {
        pair_id: "unit-pair", arm: arm.arm, phase: "arm_finished", outcome: arm,
      }));
    }
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    await act(async () => events.send("error", { status: "BLOCKED_AUTH", detail: "Synthetic permission loss." }));
    assertAuthorizationBlocked();
    await act(async () => events.close());
  });
  it.each([401, 403])("blocks HTTP %s until a fresh read-only verification and accepts only the new run", async status => {
    const events = await startComparison(Response.json({ status: "BLOCKED_AUTH" }, { status }));
    assertAuthorizationBlocked();
    expect(events.fetch.mock.calls.filter(([url]) => url === "/api/comparison/readiness")).toHaveLength(1);
    const recovered = stream();
    events.transport.response = recovered.response;
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(container.querySelector<HTMLButtonElement>("button.primary")?.disabled).toBe(false);
    expect(container.textContent).toContain("UNKNOWN_NOT_PROBED");
    await act(async () => container.querySelector<HTMLButtonElement>("button.primary")?.click());
    await act(async () => { events.send("comparison", pair(null)); events.close(); });
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.textContent).toContain("Planning and executing...");
    await act(async () => {
      recovered.send("comparison", { ...pair(null), pair_id: "unit-recovered-pair" }); recovered.close();
    });
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(container.textContent).toContain("unit-recovered-pair");
  });
  it.each([401, 403])("also invalidates cached arms when read-only verification returns HTTP %s", async status => {
    const events = await startComparison();
    await acceptBaseline(events);
    events.fetch.mockImplementationOnce(async () => Response.json({ status: "BLOCKED_AUTH" }, { status }));
    await act(async () => window.dispatchEvent(new Event("focus")));
    assertAuthorizationBlocked();
    await act(async () => { events.send("comparison", pair(null)); events.close(); });
    assertAuthorizationBlocked();
  });
  it("ignores readiness begun before source denial and late frames while a verified replacement run is active", async () => {
    const events = await startComparison();
    await acceptBaseline(events);
    let resolve: (response: Response) => void = () => { throw new Error("Deferred readiness is not initialized."); };
    const pending = new Promise<Response>(complete => { resolve = complete; });
    events.fetch.mockImplementationOnce(() => pending);
    await act(async () => window.dispatchEvent(new Event("focus")));
    await act(async () => events.send("error", { status: "BLOCKED_AUTH", detail: "Synthetic source denial." }));
    assertAuthorizationBlocked();
    await act(async () => resolve(Response.json(events.readiness)));
    assertAuthorizationBlocked();
    const recovered = stream();
    events.transport.response = recovered.response;
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(container.querySelector<HTMLButtonElement>("button.primary")?.disabled).toBe(false);
    await act(async () => container.querySelector<HTMLButtonElement>("button.primary")?.click());
    await act(async () => { events.send("comparison", pair(null)); events.close(); });
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.textContent).toContain("Planning and executing...");
    await act(async () => { recovered.send("comparison", { ...pair(null), pair_id: "unit-new-pair" }); recovered.close(); });
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(container.textContent).toContain("unit-new-pair");
  });
  it("requires fresh verification for a replacement reader session and rejects the old session's late final", async () => {
    const events = await startComparison();
    await acceptBaseline(events);
    await act(async () => root.render(<ComparisonView csrf={null} authenticated={false} />));
    assertAuthorizationBlocked();
    const recovered = stream();
    events.transport.response = recovered.response;
    await act(async () => root.render(<ComparisonView csrf="unit-replacement-csrf" authenticated />));
    expect(events.fetch.mock.calls.filter(([url]) => url === "/api/comparison/readiness")).toHaveLength(2);
    expect(container.querySelector<HTMLButtonElement>("button.primary")?.disabled).toBe(false);
    await act(async () => container.querySelector<HTMLButtonElement>("button.primary")?.click());
    expect(events.fetch).toHaveBeenLastCalledWith("/api/compare/stream", expect.objectContaining({
      headers: { "Content-Type": "application/json", "X-CSRF-Token": "unit-replacement-csrf" },
    }));
    await act(async () => { events.send("comparison", pair(null)); events.close(); });
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.textContent).toContain("Planning and executing...");
    await act(async () => {
      recovered.send("comparison", { ...pair(null), pair_id: "unit-replacement-session-pair" }); recovered.close();
    });
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(container.textContent).toContain("unit-replacement-session-pair");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
});

describe("comparison receipt wire contract", () => {
  it.each([
    { name: "omitted", parameters: undefined },
    { name: "null", parameters: null },
    { name: "empty array", parameters: [] },
    { name: "scalar array", parameters: ["unit", 17, true, null] },
  ])("retains $name-parameter receipts through arm events and the final pair", async ({ parameters }) => {
    const events = await startComparison();
    const result = pair(parameters);
    for (const arm of result.arms) {
      await act(async () => events.send("pair", {
        pair_id: result.pair_id, arm: arm.arm, phase: "arm_finished", plan: null, event: null, outcome: arm,
      }));
    }
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(container.textContent).toContain("Synthetic baseline query, not executed.");
    expect(container.textContent).toContain("Synthetic native query, not executed.");
    expect(container.textContent).not.toContain("Common source state verified");
    await act(async () => { events.send("comparison", result); events.close(); });
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.textContent).toContain("Common source state verified");
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    expect([...container.querySelectorAll("tbody td")].map(cell => cell.textContent)).toEqual(["17", "17"]);
  });
  it.each([
    { name: "object", parameters: {} },
    { name: "object array member", parameters: [17, {}] },
    { name: "nested array member", parameters: [[]] },
  ])("rejects $name parameters in a streamed arm without accepting an answer", async ({ parameters }) => {
    const events = await startComparison();
    await act(async () => {
      events.send("pair", {
        pair_id: "unit-pair", arm: "baseline", phase: "arm_finished", plan: null, event: null,
        outcome: outcome("baseline", parameters),
      });
      events.close();
    });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Invalid comparison outcome");
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.textContent).not.toContain("Common source state verified");
  });
  it("clears previously streamed answers when the final pair contains malformed receipt parameters", async () => {
    const events = await startComparison();
    const valid = pair(null);
    for (const arm of valid.arms) {
      await act(async () => events.send("pair", {
        pair_id: valid.pair_id, arm: arm.arm, phase: "arm_finished", plan: null, event: null, outcome: arm,
      }));
    }
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    await act(async () => { events.send("comparison", pair([{}])); events.close(); });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Unrecognized comparison stream event");
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.textContent).not.toContain("Common source state verified");
  });
  it("shows configured-but-unprobed readiness rather than hiding a ready status", async () => {
    const events = await startComparison();
    expect(container.textContent).toContain("READY_FOR_CURRENT_READER_PROBE");
    expect(container.textContent).toContain("current source/native probes have not run");
    await act(async () => { events.send("comparison", pair(null)); events.close(); });
  });
  it("uses the terminal pair as the answer authority even without intermediate arm outcomes", async () => {
    const events = await startComparison();
    await act(async () => { events.send("comparison", pair(null)); events.close(); });
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(container.textContent).toContain("SYNTHETIC UNIT TRANSPORT");
    expect(container.textContent).not.toContain("LIVE / EXECUTED");
  });
  it("clears accepted answers on a newly blocked context without replay fallback", async () => {
    const events = await startComparison();
    const result = pair(null);
    for (const arm of result.arms) {
      await act(async () => events.send("pair", {
        pair_id: result.pair_id, arm: arm.arm, phase: "arm_finished", outcome: arm,
      }));
    }
    await act(async () => { events.send("comparison", result); events.close(); });
    expect(container.querySelectorAll("tbody tr")).toHaveLength(2);
    events.readiness.ready = false;
    events.readiness.status = "BLOCKED_CAPABILITY";
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.textContent).not.toContain("Common source state verified");
    expect(container.textContent).toContain("BLOCKED_CAPABILITY");
  });
  it("does not accept a late terminal result after reader authorization is withdrawn", async () => {
    const events = await startComparison();
    await act(async () => root.render(<ComparisonView csrf={null} authenticated={false} />));
    await act(async () => { events.send("comparison", pair(null)); events.close(); });
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.textContent).not.toContain("Common source state verified");
  });
  it("preserves partial, measured zero, unknown and source errors without a completed total", async () => {
    const events = await startComparison();
    const result = pair(null);
    for (const arm of result.arms) {
      arm.status = "PARTIAL";
      arm.execution.status = "PARTIAL";
      arm.execution.errors.push("Source output incomplete; not a full financial total.");
      arm.execution.answer.rows = [{ unit_value: 0 }, { unit_value: null }];
      arm.execution.answer.warnings.push("Coverage is partial.");
    }
    await act(async () => { events.send("comparison", result); events.close(); });
    expect([...container.querySelectorAll("tbody td")].map(cell => cell.textContent)).toEqual(["0", "UNKNOWN", "0", "UNKNOWN"]);
    expect(container.textContent).toContain("Partial result (incomplete or unmapped rows excluded)");
    expect(container.textContent).toContain("Source output incomplete");
    expect(container.textContent).toContain("Coverage is partial");
  });
  it("distinguishes a missing blocked answer from an empty measured cohort", async () => {
    const events = await startComparison();
    const result = pair(null);
    const final = { ...result, arms: result.arms.map(arm => arm.arm === "baseline"
      ? { ...arm, status: "BLOCKED_CAPABILITY", execution: { ...arm.execution, status: "BLOCKED_CAPABILITY", answer: null },
        errors: ["Ontology context is stale; no answer accepted."] }
      : { ...arm, execution: { ...arm.execution, answer: { ...arm.execution.answer, rows: [] } } }) };
    await act(async () => { events.send("comparison", final); events.close(); });
    expect(container.textContent).toContain("missing or blocked is not zero");
    expect(container.textContent).toContain("Empty scoped result");
    expect(container.textContent).toContain("Ontology context is stale");
    expect(container.querySelectorAll("tbody td")).toHaveLength(0);
  });
  it("rejects an arm event that misattributes the other arm's result", async () => {
    const events = await startComparison();
    await act(async () => {
      events.send("pair", { pair_id: "unit-pair", arm: "baseline", phase: "arm_finished",
        outcome: outcome("native", null) });
      events.close();
    });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Invalid comparison outcome");
    expect(container.querySelector("tbody")).toBeNull();
  });
});
