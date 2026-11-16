// @vitest-environment jsdom
import { act, type MouseEvent } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { Edge, Node } from "@xyflow/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NativeDemo } from "./NativeDemo";
import { ContextOverview, isContextProjection } from "./ContextOverview";
import type { RecordedDemo } from "./recorded";

// JSDOM has no XYFlow layout engine. Only its renderer is replaced: the actual
// projection, source ownership, selection, context and evidence logic run unchanged.
vi.mock("@xyflow/react", () => ({
  ReactFlow: ({ nodes, edges, onNodeClick, onEdgeClick }: {
    nodes: Node[]; edges: Edge[];
    onNodeClick: (event: MouseEvent<HTMLButtonElement>, node: Node) => void;
    onEdgeClick: (event: MouseEvent<HTMLButtonElement>, edge: Edge) => void;
  }) => <div data-testid="graph">
    {nodes.map(node => <button key={node.id} data-node-id={node.id} aria-label={node.ariaLabel}
      aria-pressed={node.selected} onClick={event => onNodeClick(event, node)}>{node.id}</button>)}
    {edges.map(edge => <button key={edge.id} data-edge-id={edge.id} data-source={edge.source}
      data-target={edge.target} onClick={event => onEdgeClick(event, edge)}>{edge.id}</button>)}
  </div>,
  Background: () => null, Controls: () => null, Handle: () => null,
  MarkerType: { ArrowClosed: "arrow" }, Position: { Left: "left", Right: "right" },
}));

// Explicit synthetic transport fixture, not a replacement for the P0 recording.
const packet: RecordedDemo = {
  schema_version: "1.0", mode: "RECORDED_NATIVE_DEMO",
  captured_at: "2026-09-30T21:01:00Z", context_captured_at: "2026-09-30T21:00:00Z",
  question: "For August 2026, inspect the synthetic unit account cohort. Not a native evidence receipt.",
  sources: [{ id: "subscriptions", name: "Unit Subscriptions model" }, { id: "finance", name: "Unit Finance model" }],
  entities: [
    ["Subscription Month", "subscriptions", "Subscription Month"],
    ["Subscription Accounts", "subscriptions", "Accounts"],
    ["Finance Accounts", "finance", "Accounts_2"],
    ["Account Month", "finance", "Account Month"],
    ["Credits", "finance", "Credits"],
  ].map(([id, source_id, backing_table]) => ({
    id, source_id, backing_table, description: `Unit description for ${id}`,
    keys: id.endsWith("Accounts") ? ["account_key"] : [], properties: ["account_key", "month"],
    guidance: id === "Subscription Month" ? ["Unit snapshot guidance: do not sum over time."] : [],
  })),
  relationships: [
    { id: "Subscription Month to Subscription Accounts", source: "Subscription Month", target: "Subscription Accounts",
      guidance: ["Unit account-key identity guidance."] },
    { id: "Subscription Accounts to Finance Accounts", source: "Subscription Accounts", target: "Finance Accounts",
      guidance: ["Unit lineage verification guidance."] },
    { id: "Account Month to Finance Accounts", source: "Account Month", target: "Finance Accounts", guidance: [] },
    { id: "Credits to Finance Accounts", source: "Credits", target: "Finance Accounts", guidance: [] },
  ],
  metrics: [
    { id: "subscriptions:Subscription Month:Active Lines EOM", entity_id: "Subscription Month", source_id: "subscriptions",
      home_table: "Subscription Month", name: "Active Lines EOM", description: "Unit source-owned snapshot measure." },
    { id: "finance:Account Month:Recognized Service Revenue", entity_id: "Account Month", source_id: "finance",
      home_table: "Account Month", name: "Recognized Service Revenue", description: "Unit integer-cent measure." },
    { id: "finance:Credits:Posted Credits", entity_id: "Credits", source_id: "finance",
      home_table: "Credits", name: "Posted Credits", description: "Unit posting-month measure, not in answer." },
  ],
  native_rule_count: 0,
  focus_entity_ids: ["Subscription Month", "Subscription Accounts", "Finance Accounts", "Account Month"],
  queries: [
    { source_id: "subscriptions", dax: 'EVALUATE SUMMARIZECOLUMNS(\'Subscription Month\'[account_key], FILTER(\'Subscription Month\', [month] >= DATE(2026,8,1) && [month] < DATE(2026,9,1)), "active_lines_eom", [Active Lines EOM])' },
    { source_id: "finance", dax: 'EVALUATE SUMMARIZECOLUMNS(\'Account Month\'[account_key], FILTER(\'Account Month\', [month] >= DATE(2026,8,1) && [month] < DATE(2026,9,1)), "recognized_service_revenue_cents", [Recognized Service Revenue])' },
  ],
  rows: [
    { account_key: 3, active_lines_eom: 0, recognized_service_revenue_cents: 0 },
    { account_key: 7, active_lines_eom: 2, recognized_service_revenue_cents: 12345 },
  ],
  source_hashes: { definition: "unit-definition-hash", native_mcp: "unit-context-hash", native_answer: "unit-answer-hash" },
  limitations: ["Synthetic transport fixture; not native proof."],
};

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals();
});
function button(label: string) {
  const result = [...container.querySelectorAll<HTMLButtonElement>("button")]
    .find(item => item.textContent === label || item.getAttribute("aria-label") === label);
  if (!result) throw new Error(`Missing button: ${label}`);
  return result;
}
async function click(label: string) { await act(async () => button(label).click()); }
function facts() {
  return {
    nodes: [...container.querySelectorAll("[data-node-id]")].map(node => node.getAttribute("data-node-id")),
    edges: [...container.querySelectorAll("[data-edge-id]")].map(edge =>
      [edge.getAttribute("data-edge-id"), edge.getAttribute("data-source"), edge.getAttribute("data-target")]),
  };
}
async function render(value = packet) { await act(async () => root.render(<NativeDemo recording={value} />)); }

describe("recorded ontology customer experience DOM", () => {
  it("leads with question, exact source-owned units and recorded coverage, not a fabricated live badge", async () => {
    await render();
    const hero = container.querySelector(".answer-hero");
    expect(hero?.textContent).toContain("For August 2026");
    expect(hero?.textContent).toContain("August 2026");
    expect(hero?.textContent).toContain("2 captured account rows");
    expect(hero?.textContent).toContain("12,345");
    expect(hero?.textContent).toContain("Recognized revenue · cents");
    expect(hero?.textContent).toContain("not revalidated");
    expect(container.querySelector(".badge")?.textContent).toBe("RECORDED");
    expect(container.textContent).not.toContain("RECORDED LIVE");
    expect([...container.querySelectorAll("time")].map(time => time.dateTime)).toEqual([packet.captured_at, packet.context_captured_at]);
    expect(container.textContent).toContain("Packet created Not recorded");
    expect(container.textContent).toContain("Source data refresh");
    expect(container.textContent).toContain("Not captured; context acquisition is a separate clock");
  });
  it("keeps source panels, counts and identical native facts in both lenses", async () => {
    await render();
    expect(container.querySelector('[data-source-id="subscriptions"]')?.textContent).toContain("2 native entities · 1 source-backed");
    expect(container.querySelector('[data-source-id="finance"]')?.textContent).toContain("3 native entities · 2 source-backed");
    await click("Full ontology");
    const connected = facts();
    expect(connected.nodes).toHaveLength(5); expect(connected.edges).toHaveLength(4);
    await click("By source model");
    expect(facts()).toEqual(connected);
    expect(button("By source model").getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelectorAll(".source-model")).toHaveLength(2);
    await click("Account path");
    expect(facts().nodes).toEqual(packet.focus_entity_ids);
    expect(facts().edges).toHaveLength(3);
    expect(facts().edges).toContainEqual(["Account Month to Finance Accounts", "Account Month", "Finance Accounts"]);
    expect(container.textContent).toContain("not a controlled comparison");
  });
  it("selects actual graph entities and relationship guidance with captured origin", async () => {
    await render();
    const graphEntity = container.querySelector<HTMLButtonElement>('[data-node-id="Finance Accounts"]');
    if (!graphEntity) throw new Error("Missing captured node");
    await act(async () => graphEntity.click());
    expect(container.querySelector(".entity-detail h2")?.textContent).toBe("Finance Accounts");
    expect(container.querySelector(".entity-detail")?.textContent).toContain("Unit Finance model");
    expect(container.querySelector(".entity-detail")?.textContent).toContain("Accounts_2");
    const edge = container.querySelector<HTMLButtonElement>('[data-edge-id="Subscription Accounts to Finance Accounts"]');
    if (!edge) throw new Error("Missing captured edge");
    await act(async () => edge.click());
    const inspector = container.querySelector(".entity-detail");
    expect(inspector?.textContent).toContain("Unit lineage verification guidance.");
    expect(inspector?.textContent).toContain("CAPTURED_NATIVE_ANNOTATION");
    expect(inspector?.textContent).toContain(packet.context_captured_at);
    expect(inspector?.textContent).toContain("does not establish that the native agent traversed it");
  });
  it("provides real keyboard-focusable entity controls and reveals out-of-focus nodes", async () => {
    await render();
    const entity = container.querySelector<HTMLButtonElement>('.entity-picker button:last-child');
    if (!entity) throw new Error("Missing keyboard entity browser");
    entity.closest("details")?.setAttribute("open", "");
    entity.focus(); expect(document.activeElement).toBe(entity);
    await act(async () => entity.click());
    expect(container.querySelector(".entity-detail h2")?.textContent).toBe("Credits");
    expect(button("Full ontology").getAttribute("aria-pressed")).toBe("true");
    expect(facts().nodes).toContain("Credits");
  });
  it("links metric binding to its own model/home, actual query and returned account value", async () => {
    await render();
    await click("12,345Recognized revenue · centsInspect source evidence →");
    const drawer = container.querySelector<HTMLDetailsElement>("details.evidence-drawer");
    expect(drawer?.open).toBe(true);
    const proof = container.querySelector(".binding-evidence");
    expect(proof?.textContent).toContain("Unit Finance model");
    expect(proof?.textContent).toContain("Account Month / Recognized Service Revenue");
    expect(proof?.textContent).toContain("finance:Account Month:Recognized Service Revenue");
    expect(proof?.querySelector("pre")?.textContent).toBe(packet.queries[1].dax);
    expect(proof?.textContent).toContain("recognized_service_revenue_cents");
    expect(proof?.textContent).not.toContain("Unit Subscriptions model");
    await click("Inspect captured account 3");
    expect(container.querySelector(".selected-account-evidence")?.textContent).toContain("0 cents");
    expect([...drawer?.querySelectorAll("tbody tr") ?? []]).toHaveLength(2);
    expect(drawer?.textContent).toContain("not separately retained raw source responses");
  });
  it("does not invent queries or returned metric columns for another source-owned binding", async () => {
    await render();
    await click("Explore Finance entities");
    const credits = container.querySelector<HTMLButtonElement>('.entity-picker button:last-child');
    if (!credits) throw new Error("Missing Credits selection");
    await act(async () => credits.click());
    await click("Inspect binding & captured query");
    const proof = container.querySelector(".binding-evidence");
    expect(proof?.textContent).toContain("Unit Finance model");
    expect(proof?.textContent).toContain("Credits / Posted Credits");
    expect(proof?.textContent).toContain("Not captured for this binding");
    expect(proof?.textContent).toContain("Not present in the recorded answer DTO");
    expect(proof?.querySelector("pre")).toBeNull();
  });
  it.each([0, 3])("does not turn Rule count %i into a formal payload or execution claim", async count => {
    await render({ ...packet, native_rule_count: count });
    const context = container.querySelector(".context-authority");
    expect(context?.textContent).toContain(`Formal Rule count: ${count}`);
    expect(context?.textContent).toContain("CAPTURED_NATIVE_ANNOTATION");
    expect(context?.textContent).toContain("SOURCE_AI_PREPARATION");
    expect(context?.textContent).toContain("CUSTOM_CONSTRAINT");
    expect(context?.textContent).toContain("No rule use or execution is established");
    expect(context?.textContent).toContain(count ? "no formal Rule payload" : "No formal Rule objects");
    expect(container.textContent).not.toContain("guaranteed");
  });
  it("preserves capture origins and unknown metadata without current-time or model-owner fallbacks", async () => {
    await render({ ...packet, created_at: "2026-10-01T08:00:00Z",
      sources: packet.sources.map(source => ({ ...source, name: "" })), queries: [] });
    expect(container.textContent).toContain("Packet created 2026-10-01T08:00:00Z");
    expect(container.textContent).toContain("Not established by captured queries");
    expect(container.querySelector(".source-model h2")?.textContent).toBe("Not captured");
    expect(container.querySelector(".entity-detail")?.textContent).toContain("Source modelNot captured");
    expect(container.querySelector(".fair-comparison")?.textContent).toContain("equivalent facts, source AI preparation and permissions");
  });
});

describe("current Context DTO presentation", () => {
  const context = {
    mode: "OFFLINE_SYNTHETIC", source: "reviewed local synthetic contracts", revision: "unit-revision",
    schema_hash: null, scope: { models: ["subscriptions", "finance"] },
    relationships: [{ id: "unit-edge", left_model: "subscriptions", right_model: "finance", left_table: "month_a",
      right_table: "month_b", left_keys: ["account_key"], right_keys: ["account_key"],
      cardinality: "one_to_one", version: "1", purposes: ["Same-month unit contract."] }],
    rules: [{ id: "R-unit", statement: "Unknown is not zero." }],
    provenance: ["Application-owned synthetic intent; NOT native ontology context"],
  };
  it("renders supplied contracts and origins without presenting an offline Rule as native", async () => {
    expect(isContextProjection(context)).toBe(true);
    await act(async () => root.render(<ContextOverview context={context} serverMode="OFFLINE_SYNTHETIC" />));
    expect(container.textContent).toContain("Reviewed synthetic business contracts");
    expect(container.textContent).toContain("subscriptions / month_a / account_key");
    expect(container.textContent).toContain("CUSTOM_CONSTRAINT / APPLICATION-OWNED INTENT");
    expect(container.textContent).toContain("Unknown is not zero.");
    expect(container.textContent).toContain("not projected by this Context DTO");
    expect(container.textContent).not.toContain("FORMAL_NATIVE_RULE");
  });
  it.each([null, {}, { ...context, relationships: [{}] }, { ...context, rules: [{ payload: {} }] }])(
    "does not promote missing or unsupported metadata to a native graph", async value => {
      expect(isContextProjection(value)).toBe(false);
      await act(async () => root.render(<ContextOverview context={value} serverMode="LIVE_CONFIGURED" />));
      expect(container.textContent).toContain("NO VALIDATED CONTEXT PROJECTION");
      expect(container.textContent).toContain("No supported context DTO");
      expect(container.querySelector(".native-graph")).toBeNull();
    });
  it("preserves an actual blocked detail without inventing a live entity/rule contract", async () => {
    await act(async () => root.render(<ContextOverview serverMode="LIVE_CONFIGURED"
      context={{ status: "BLOCKED_CAPABILITY", detail: "No native context bound; no static context fallback in reader mode." }} />));
    expect(container.textContent).toContain("No native context bound");
    expect(container.textContent).toContain("not a native ontology capture");
    expect(container.querySelector(".authority-grid")).toBeNull();
  });
});
