// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./main";

vi.mock("@xyflow/react", () => ({
  ReactFlow: () => null, Background: () => null, Controls: () => null, Handle: () => null,
  MarkerType: { ArrowClosed: "arrow" }, Position: { Left: "left", Right: "right" },
}));

const resources = ["source_metadata", "storage"] as const;
const permissionNames = { source_metadata: "Item.ReadWrite.All", storage: "user_impersonation" };
const uri = "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000001/oauth2/v2.0/authorize?unit=synthetic";
function health() {
  return {
    mode: "LIVE_CONFIGURED", scope: null, data_ready: false, context_revision: "CURRENT_READER_PROBE_REQUIRED",
    authenticated: true, csrf_token: "unit-csrf", session_epoch: "unit-original-epoch",
    readiness: { mode: "LIVE_CONFIGURED", ready: true, status: "READY_FOR_CURRENT_READER_PROBE", blockers: [],
      reader_session: "CURRENT_SESSION", native_source_probe: "UNKNOWN_NOT_PROBED",
      application_execution: "NOT_ESTABLISHED", demonstration: "NOT_ESTABLISHED",
      claim: "Local configuration only; native/source probes and execution are not established." },
    resource_consent: resources.map(resource => ({
      resource, configuration: "CONFIGURED", consent: "NOT_ESTABLISHED_IN_CURRENT_SESSION",
      authorization: "UNKNOWN_NOT_PROBED", execution: "NOT_ESTABLISHED",
    })),
  };
}
function deferred<T>() {
  let resolve: (value: T) => void = () => { throw new Error("Deferred transport not initialized"); };
  const promise = new Promise<T>(complete => { resolve = complete; });
  return { promise, resolve };
}
function stream() {
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  return {
    response: new Response(body, { headers: { "content-type": "text/event-stream" } }),
    send(kind: string, value: unknown) {
      controller.enqueue(new TextEncoder().encode(`event: ${kind}\ndata: ${JSON.stringify(value)}\n\n`));
    },
    close() { controller.close(); },
  };
}
function outcome(arm: "baseline" | "native") {
  return {
    arm, status: "COMPLETE", plan: null, planning_receipt: null, errors: [], elapsed_ms: 1,
    execution: {
      request_id: `unit-${arm}`, status: "COMPLETE", mode: "SYNTHETIC_UNIT_TRANSPORT", errors: [], elapsed_ms: 1,
      receipts: [], answer: { rows: [{ value: 17 }], evidence_ids: [], coverage: {}, warnings: [] },
    },
  };
}
function pair() {
  return {
    pair_id: "unit-old-pair", question: "Synthetic question", provider: "SYNTHETIC_UNIT_PROVIDER",
    model_version: "unit-model", source_facts_hash: "unit-facts", context_revision: "unit-context",
    arms: [outcome("baseline"), outcome("native")], comparability: "COMMON_SOURCE_STATE",
    comparability_reasons: [], limitations: ["Synthetic transport only; no live execution."],
  };
}
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});
function button(text: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find(item => item.textContent?.includes(text));
  if (!found) throw new Error(`Missing unit action: ${text}`);
  return found;
}
async function mount(state = health()) {
  const consent = deferred<Response>();
  const existing = deferred<Response>();
  const events = stream();
  const fetch = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
    if (url === "/api/health") return Response.json(state);
    if (url === "/api/capabilities") return Response.json({ experiments: [] });
    if (url === "/api/comparison/readiness") return Response.json(state.readiness);
    if (url === "/api/compare/stream") return events.response;
    if (String(url).startsWith("/auth/consent/")) return consent.promise;
    if (String(url).startsWith("/auth/authorize-existing/")) return existing.promise;
    if (url === "/auth/logout") return Response.json({ signed_out: true });
    throw new Error(`Unexpected request in synthetic consent DOM test: ${url}`);
  });
  const navigate = vi.fn();
  vi.stubGlobal("fetch", fetch);
  await act(async () => root.render(<App navigate={navigate} />));
  return { state, consent, existing, events, fetch, navigate };
}
async function acceptedArm(events: ReturnType<typeof stream>) {
  await act(async () => button("Comparison").click());
  const textarea = container.querySelector("textarea");
  const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  if (!textarea || !setValue) throw new Error("Missing comparison input");
  await act(async () => {
    setValue.call(textarea, "Synthetic question");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => container.querySelector<HTMLButtonElement>("button.primary")?.click());
  await act(async () => events.send("pair", {
    pair_id: "unit-old-pair", arm: "baseline", phase: "arm_finished", outcome: outcome("baseline"),
  }));
  expect(container.querySelectorAll("tbody tr")).toHaveLength(1);
}

describe("explicit reviewed resource consent", () => {
  it.each(resources)("validates an existing %s grant only on explicit action and verifies a new epoch", async resource => {
    const { fetch, existing, navigate, state } = await mount();
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/health", "/api/capabilities", "/api/comparison/readiness",
    ]);
    await act(async () => button(`Use existing ${resource === "source_metadata" ? "Fabric" : "Storage"} grant`).click());
    expect(fetch).toHaveBeenLastCalledWith(`/auth/authorize-existing/${resource}`, expect.objectContaining({
      method: "POST", body: "{}", headers: expect.objectContaining({ "X-CSRF-Token": "unit-csrf" }),
    }));
    state.session_epoch = "unit-existing-grant-epoch";
    state.csrf_token = "unit-rotated-csrf";
    const entry = state.resource_consent.find(item => item.resource === resource);
    if (!entry) throw new Error("Missing synthetic resource");
    entry.consent = "EXISTING_GRANT_VALIDATED_IN_CURRENT_SESSION";
    await act(async () => existing.resolve(Response.json({
      resource, status: "EXISTING_GRANT_VALIDATED_CURRENT_SESSION", ready: false,
    })));
    expect(navigate).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledWith("/api/health", expect.objectContaining({ method: "GET" }));
    expect(fetch).toHaveBeenLastCalledWith("/api/comparison/readiness", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(container.textContent).toContain("EXISTING_GRANT_VALIDATED_IN_CURRENT_SESSION");
    expect(container.textContent).toContain("UNKNOWN_NOT_PROBED");
    expect(container.textContent).not.toContain("LIVE / EXECUTED");
    expect(button(`Use existing ${resource === "source_metadata" ? "Fabric" : "Storage"} grant`).disabled).toBe(true);
  });
  it.each([401, 403])("does not auto-consent or restore old results after existing-grant HTTP %s", async status => {
    const { existing, events, navigate, fetch } = await mount();
    await acceptedArm(events);
    await act(async () => button("Use existing Fabric grant").click());
    expect(container.querySelector("tbody")).toBeNull();
    await act(async () => existing.resolve(Response.json({ status: "BLOCKED_AUTH" }, { status })));
    await act(async () => { events.send("comparison", pair()); events.close(); });
    expect(container.querySelector("tbody")).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
    expect(fetch.mock.calls.some(([url]) => String(url).startsWith("/auth/consent/"))).toBe(false);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("may require interactive sign-in");
  });
  it("rejects an existing-grant result without an actual fresh epoch", async () => {
    const { existing, navigate } = await mount();
    await act(async () => button("Use existing Storage grant").click());
    await act(async () => existing.resolve(Response.json({
      resource: "storage", status: "EXISTING_GRANT_VALIDATED_CURRENT_SESSION", ready: false,
    })));
    expect(navigate).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("could not be verified");
    expect(container.textContent).not.toContain("EXISTING_GRANT_VALIDATED_IN_CURRENT_SESSION");
  });
  it("keeps old streamed answers discarded after successful existing-grant session rotation", async () => {
    const { existing, events, state } = await mount();
    await acceptedArm(events);
    await act(async () => button("Use existing Fabric grant").click());
    expect(container.querySelector("tbody")).toBeNull();
    state.session_epoch = "unit-new-existing-epoch";
    state.csrf_token = "unit-new-existing-csrf";
    state.resource_consent[0].consent = "EXISTING_GRANT_VALIDATED_IN_CURRENT_SESSION";
    state.readiness.ready = false;
    state.readiness.status = "BLOCKED_AUTH";
    await act(async () => existing.resolve(Response.json({
      resource: "source_metadata", status: "EXISTING_GRANT_VALIDATED_CURRENT_SESSION", ready: false,
    })));
    await act(async () => { events.send("comparison", pair()); events.close(); });
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.textContent).not.toContain("Common source state verified");
    expect(container.querySelector<HTMLButtonElement>("button.primary")?.disabled).toBe(true);
  });
  it("logout prevents a late existing-grant response from reactivating the old reader", async () => {
    const { existing, navigate, fetch } = await mount();
    await act(async () => button("Use existing Storage grant").click());
    await act(async () => button("Sign out").click());
    const healthReads = fetch.mock.calls.filter(([url]) => url === "/api/health").length;
    await act(async () => existing.resolve(Response.json({
      resource: "storage", status: "EXISTING_GRANT_VALIDATED_CURRENT_SESSION", ready: false,
    })));
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/");
    expect(fetch.mock.calls.filter(([url]) => url === "/api/health")).toHaveLength(healthReads);
    expect(container.textContent).not.toContain("EXISTING_GRANT_VALIDATED_IN_CURRENT_SESSION");
  });
  it.each(resources)("starts %s only on a user POST with current CSRF and top-level navigation", async resource => {
    const { fetch, consent, navigate } = await mount();
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/health", "/api/capabilities", "/api/comparison/readiness",
    ]);
    expect(container.textContent).toContain("not a demo user item grant");
    expect(container.textContent).not.toContain("unit-csrf");
    await act(async () => button(permissionNames[resource]).click());
    expect(fetch).toHaveBeenLastCalledWith(`/auth/consent/${resource}`, expect.objectContaining({
      method: "POST", body: "{}", headers: expect.objectContaining({ "X-CSRF-Token": "unit-csrf" }),
    }));
    expect(navigate).not.toHaveBeenCalled();
    expect(button(permissionNames[resource]).disabled).toBe(true);
    await act(async () => consent.resolve(Response.json({ resource, authorization_uri: uri })));
    expect(navigate).toHaveBeenCalledExactlyOnceWith(uri);
    expect(container.textContent).toContain("UNKNOWN_NOT_PROBED");
    expect(container.textContent).toContain("NOT_ESTABLISHED");
    expect(container.textContent).not.toContain("LIVE / EXECUTED");
  });
  it("blocks unconfigured, forged or unverified resource projections without automatic consent", async () => {
    const state = health();
    state.resource_consent[0].configuration = "NOT_CONFIGURED";
    const { fetch } = await mount(state);
    expect(button("Item.ReadWrite.All").disabled).toBe(true);
    await act(async () => button("Item.ReadWrite.All").click());
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/health", "/api/capabilities", "/api/comparison/readiness",
    ]);
    state.resource_consent[0].resource = "graph" as "source_metadata";
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(container.querySelector('[aria-label="Reviewed resource consent"] button')).toBeNull();
    expect(container.textContent).toContain("consent actions are blocked");
    expect(fetch.mock.calls.every(([url]) => !String(url).startsWith("/auth/"))).toBe(true);
  });
  it.each([401, 403])("clears both arms on consent initiation and ignores late frames after HTTP %s", async status => {
    const { consent, events, navigate, fetch } = await mount();
    await acceptedArm(events);
    await act(async () => button("Item.ReadWrite.All").click());
    expect(container.querySelector("tbody")).toBeNull();
    const run = fetch.mock.calls.find(([url]) => url === "/api/compare/stream");
    expect((run?.[1] as RequestInit | undefined)?.signal?.aborted).toBe(true);
    await act(async () => consent.resolve(Response.json({ status: "BLOCKED_AUTH" }, { status })));
    await act(async () => { events.send("comparison", pair()); events.close(); });
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.textContent).not.toContain("Common source state verified");
    expect(container.querySelector<HTMLButtonElement>("button.primary")?.disabled).toBe(true);
    expect(navigate).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Prior answers remain discarded");
  });
  it("does not restore old readiness or answers when health begun before consent completes late", async () => {
    const { fetch, consent, events, navigate, state } = await mount();
    await acceptedArm(events);
    const pendingHealth = deferred<Response>();
    fetch.mockImplementationOnce(() => pendingHealth.promise);
    await act(async () => window.dispatchEvent(new Event("focus")));
    await act(async () => button("user_impersonation").click());
    await act(async () => pendingHealth.resolve(Response.json(state)));
    expect(container.querySelector("tbody")).toBeNull();
    expect(container.querySelector<HTMLButtonElement>("button.primary")?.disabled).toBe(true);
    await act(async () => consent.resolve(Response.json({ resource: "storage", authorization_uri: uri })));
    expect(navigate).toHaveBeenCalledExactlyOnceWith(uri);
    await act(async () => { events.send("comparison", pair()); events.close(); });
    expect(container.querySelector("tbody")).toBeNull();
  });
  it("logout cancels a pending resource navigation and clears old answers before the response", async () => {
    const { consent, events, navigate } = await mount();
    await acceptedArm(events);
    await act(async () => button("Item.ReadWrite.All").click());
    await act(async () => button("Sign out").click());
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/");
    await act(async () => consent.resolve(Response.json({ resource: "source_metadata", authorization_uri: uri })));
    await act(async () => { events.send("comparison", pair()); events.close(); });
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(container.querySelector("tbody")).toBeNull();
  });
  it("does not turn successful consent projection into current authorization or execution proof", async () => {
    const state = health();
    state.session_epoch = "unit-fresh-epoch";
    state.resource_consent[0].consent = "COMPLETED_IN_CURRENT_SESSION";
    const { fetch } = await mount(state);
    expect(container.textContent).toContain("COMPLETED_IN_CURRENT_SESSION");
    expect(container.textContent).toContain("UNKNOWN_NOT_PROBED");
    expect(container.textContent).toContain("Consent does not make the live demo ready");
    expect(container.textContent).not.toContain("LIVE / EXECUTED");
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/health", "/api/capabilities", "/api/comparison/readiness",
    ]);
  });
  it("rejects a forged authorization destination without navigating", async () => {
    const { consent, navigate } = await mount();
    await act(async () => button("Item.ReadWrite.All").click());
    await act(async () => consent.resolve(Response.json({
      resource: "source_metadata", authorization_uri: "https://unreviewed.test/authorize",
    })));
    expect(navigate).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')).not.toBeNull();
  });
});
