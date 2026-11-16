// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./main";
import { NativeDemo } from "./NativeDemo";
import { captureTime, executionLabel, isHealth, isRecording, signInFailureMessage, type HealthState } from "./readiness";
import type { RecordedDemo } from "./recorded";

// These tests exercise real app/result DOM, not ReactFlow layout or a browser.
vi.mock("@xyflow/react", () => ({
  ReactFlow: () => null, Background: () => null, Controls: () => null, Handle: () => null,
  MarkerType: { ArrowClosed: "arrow" }, Position: { Left: "left", Right: "right" },
}));

const packet: RecordedDemo = {
  schema_version: "1.0", mode: "RECORDED_NATIVE_DEMO",
  captured_at: "2026-09-30T21:01:00Z", context_captured_at: "2026-09-30T21:00:00Z",
  question: "July recorded cohort and period, from synthetic unit transport.", sources: [{ id: "source", name: "Synthetic source" }],
  entities: ["Accounts", "Month"].map(id => ({
    id, source_id: "source", backing_table: id, description: "Synthetic entity", keys: [], properties: [], guidance: [],
  })),
  relationships: [{ id: "link", source: "Accounts", target: "Month", guidance: [] }],
  metrics: [{ id: "metric", entity_id: "Month", source_id: "source", home_table: "Month", name: "Amount", description: "" }],
  native_rule_count: 0, focus_entity_ids: ["Accounts", "Month"],
  queries: [{ source_id: "source", dax: "Synthetic captured query; not executed by this test." }],
  rows: [{ account_key: 1, active_lines_eom: 0, recognized_service_revenue_cents: 0 }],
  source_hashes: { synthetic: "not-live-evidence" }, limitations: ["Synthetic unit transport, not native proof."],
};
function health(mode = "LIVE_CONFIGURED"): HealthState {
  return {
    mode, scope: null, data_ready: false, context_revision: "CURRENT_READER_PROBE_REQUIRED",
    authenticated: mode === "LIVE_CONFIGURED", csrf_token: "unit-csrf", session_epoch: "unit-epoch",
    readiness: {
      mode, ready: mode === "LIVE_CONFIGURED", status: "READY_FOR_CURRENT_READER_PROBE", blockers: [],
      claim: "Local configuration checks only; current source/native probes have not run.",
      implementation: "LOCALLY_IMPLEMENTED", configuration: "CONFIGURED", reader_session: "CURRENT_SESSION",
      native_source_probe: "UNKNOWN_NOT_PROBED", application_execution: "NOT_ESTABLISHED", demonstration: "NOT_ESTABLISHED",
    },
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
  container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/");
});
function transport(state: HealthState, recording: unknown = packet, packetStatus = 200) {
  const fetch = vi.fn(async (url: RequestInfo | URL) => {
    if (url === "/api/health") return Response.json(state);
    if (url === "/api/capabilities") return Response.json({ experiments: [] });
    if (url === "/api/native-demo") return Response.json(recording, { status: packetStatus });
    throw new Error(`Unexpected mocked request: ${url}`);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("readiness and provenance contracts", () => {
  it("distinguishes expired and replaced sign-in requests without assuming an identity failure", () => {
    expect(signInFailureMessage("sign_in_expired")).toContain("expired");
    expect(signInFailureMessage("sign_in_expired")).toContain("10 minutes");
    expect(signInFailureMessage("sign_in_restart")).toContain("newest");
    expect(signInFailureMessage("reader_window_expired")).toContain("approved reader window");
    expect(signInFailureMessage("untrusted-private-response")).not.toContain("untrusted-private-response");
    expect(signInFailureMessage("sign_in_failed")).not.toContain("identity is not approved");
  });
  it("does not equate configured, offline or recorded with executed live", () => {
    expect(executionLabel("LIVE_CONFIGURED")).toBe("LIVE / CONFIGURATION ONLY");
    expect(executionLabel("LIVE_DELEGATED")).toBe("LIVE / EXECUTED");
    expect(executionLabel("OFFLINE_SYNTHETIC")).toBe("OFFLINE SYNTHETIC");
    expect(executionLabel(packet.mode)).toBe("RECORDED");
    expect(isHealth(health())).toBe(true);
    expect(isHealth({ ...health(), readiness: null })).toBe(false);
    expect(isHealth({ ...health(), mode: "OFFLINE_SYNTHETIC" })).toBe(false);
    expect(isHealth({ ...health(), readiness: { ...health().readiness, blockers: ["Expired"] } })).toBe(false);
  });
  it("rejects mode impersonation and invalid dates without substituting today's timestamp", () => {
    expect(isRecording(packet)).toBe(true);
    expect(isRecording({ ...packet, mode: "LIVE_DELEGATED" })).toBe(false);
    expect(isRecording({ ...packet, captured_at: "2026-09-30T21:01:00" })).toBe(false);
    expect(isRecording({ ...packet, created_at: "not-a-date" })).toBe(false);
    expect(captureTime(undefined)).toBe("Not recorded");
  });
});
describe("actual readiness and recorded DOM", () => {
  it("renders a fixed expired-sign-in explanation and removes the callback marker without automatic login", async () => {
    window.history.replaceState(null, "", "/?auth_error=sign_in_expired");
    const state = health("DELEGATED_READER_SETUP");
    const fetch = transport(state);
    await act(async () => root.render(createElement(App)));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("10 minutes");
    expect(container.textContent).not.toContain("identity is not approved");
    expect(window.location.search).toBe("");
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(["/api/health", "/api/capabilities"]);
    expect(container.textContent).toContain("Sign in to DEV");
  });
  it("does not contradict configured readiness with a hardcoded unconfigured banner", async () => {
    const fetch = transport(health());
    await act(async () => root.render(createElement(App)));
    expect(container.textContent).toContain("LIVE / CONFIGURATION ONLY");
    expect(container.textContent).toContain("CONFIGURED");
    expect(container.textContent).toContain("UNKNOWN_NOT_PROBED");
    expect(container.textContent).not.toContain("Native context and live result mapping are not configured");
    expect(fetch).not.toHaveBeenCalledWith("/api/context", expect.anything());
    expect(container.textContent).not.toContain("unit-csrf");
  });
  it("keeps blocked expiry visible rather than turning configuration presence into ready", async () => {
    const state = health();
    state.authenticated = false;
    state.readiness.ready = false;
    state.readiness.status = "BLOCKED_CAPABILITY";
    state.readiness.configuration = "BLOCKED";
    state.readiness.reader_session = "BLOCKED_AUTH";
    state.readiness.blockers = ["Private evidence approval expired or clock has no timezone"];
    transport(state);
    await act(async () => root.render(createElement(App)));
    expect(container.textContent).toContain("Private evidence approval expired");
    expect(container.textContent).toContain("Sign in to DEV");
    expect(container.textContent).not.toContain("Current delegated reader session");
  });
  it("rechecks a reader expiry on focus and removes current-reader claims", async () => {
    const state = health();
    transport(state);
    await act(async () => root.render(createElement(App)));
    expect(container.textContent).toContain("Current delegated reader session");
    state.authenticated = false; state.csrf_token = null;
    state.readiness.ready = false; state.readiness.reader_session = "BLOCKED_AUTH";
    state.readiness.status = "BLOCKED_AUTH";
    state.readiness.blockers = ["The approved reader session window has expired"];
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(container.textContent).not.toContain("Current delegated reader session");
    expect(container.textContent).toContain("The approved reader session window has expired");
    expect(container.textContent).toContain("Sign in to DEV");
  });
  it("leads with the actual offline question and preserves zero, unknown and receipt clocks", async () => {
    const state = health("OFFLINE_SYNTHETIC");
    state.scope = { tenant: "SYNTHETIC_LOCAL", principal: "LOCAL_PROCESS", authorization_revision: "unit",
      models: ["subscriptions", "finance"], mode: "OFFLINE_SYNTHETIC" };
    state.data_ready = true;
    const fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (url === "/api/health") return Response.json(state);
      if (url === "/api/capabilities") return Response.json({ experiments: [] });
      if (url === "/api/context") return Response.json({ source: "synthetic fixture" });
      if (url === "/api/execute/stream") {
        const plan: { request_id: string } = JSON.parse(String(init?.body));
        const result = {
          request_id: plan.request_id, mode: "OFFLINE_SYNTHETIC", status: "COMPLETE", elapsed_ms: 2, errors: [],
          answer: { rows: [{ amount: 0 }, { amount: null }], evidence_ids: ["unit-receipt"], coverage: {}, warnings: [] },
          receipts: [{
            id: "unit-receipt", node_id: "unit", lane: "OFFLINE_SYNTHETIC", tool: "unit", model: "synthetic",
            query: "Synthetic query; not a DAX source call.", elapsed_ms: 1, returned_rows: 2, status: "COMPLETE",
            completeness_evidence: "Synthetic transport receipt.", source_watermark: null, parameters: null, warnings: [],
            clocks: { data: "unit-data", model: "unit-model", ontology: "unit-ontology", context: "unit-context" },
          }],
        };
        return new Response(`event: result\ndata: ${JSON.stringify(result)}\n\n`,
          { headers: { "content-type": "text/event-stream" } });
      }
      throw new Error(`Unexpected mocked request: ${url}`);
    });
    vi.stubGlobal("fetch", fetch);
    await act(async () => root.render(createElement(App)));
    const button = container.querySelector<HTMLButtonElement>("button.primary");
    if (!button) throw new Error("Missing offline execute button");
    await act(async () => button.click());
    const answer = container.querySelector(".execution-result");
    expect(answer?.querySelector("h2")?.textContent).toBe("Show August end-of-month lines and recognized service revenue by account.");
    expect(answer?.textContent).toContain("OFFLINE SYNTHETIC");
    expect(answer?.textContent).not.toContain("LIVE / EXECUTED");
    expect([...answer?.querySelectorAll("tbody td") ?? []].map(cell => cell.textContent)).toEqual(["0", "UNKNOWN"]);
    expect(answer?.textContent).toContain("unit-data");
    expect(answer?.textContent).toContain("unit-model");
    expect(answer?.textContent).toContain("unit-ontology");
    expect(answer?.textContent).toContain("unit-context");
    const editor = container.querySelector("textarea#plan");
    if (!answer || !editor) throw new Error("Missing answer or plan editor");
    expect(answer.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });
  it("keeps answer capture, context capture and optional packet creation separate", async () => {
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-15T06:00:00Z"));
    await act(async () => root.render(createElement(NativeDemo, { recording: packet })));
    expect(container.querySelector(".badge")?.textContent).toBe("RECORDED");
    expect(container.textContent).not.toContain("RECORDED LIVE");
    expect(container.textContent).toContain("not proof of custom-application execution");
    expect(container.textContent).toContain("Packet created Not recorded");
    expect([...container.querySelectorAll("time")].map(time => time.dateTime)).toEqual([
      packet.captured_at, packet.context_captured_at,
    ]);
    expect(container.textContent).not.toContain("2026-10-15");
    const drawer = container.querySelector("details.evidence-drawer");
    expect(drawer?.textContent).toContain("Synthetic captured query");
    expect([...drawer?.querySelectorAll("tbody td") ?? []].map(cell => cell.textContent)).toEqual(["1", "0", "0"]);
    expect(drawer?.textContent).toContain("Internal tool timings and execution steps were not captured");
  });
  it.each([
    { name: "absent", value: { detail: "Recording absent" }, status: 409 },
    { name: "wrong-provenance", value: { ...packet, mode: "LIVE_DELEGATED" }, status: 200 },
  ])("blocks $name recording without selecting an offline or live fallback", async ({ value, status }) => {
    const fetch = transport(health("RECORDED_NATIVE_DEMO"), value, status);
    await act(async () => root.render(createElement(App)));
    expect(container.querySelector('[role="alert"]')).not.toBeNull();
    expect(container.textContent).not.toContain("SLA engine fixture");
    expect(container.querySelector("table")).toBeNull();
    expect(fetch.mock.calls.map(([path]) => path)).toEqual(["/api/health", "/api/capabilities", "/api/native-demo"]);
  });
});
