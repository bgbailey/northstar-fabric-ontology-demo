// @vitest-environment jsdom
import { act, type MouseEvent } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { Edge, Node } from "@xyflow/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { DemoLibraryView, isRecordedDemoLibrary, type RecordedDemoLibrary } from "./DemoLibrary";

vi.mock("@xyflow/react", () => ({
  ReactFlow: ({ nodes, edges, onNodeClick }: {
    nodes: Node[];
    edges: Edge[];
    onNodeClick?: (event: MouseEvent<HTMLButtonElement>, node: Node) => void;
  }) => <div data-testid="ontology-graph">
    {nodes.map(node => <button key={node.id} data-node-id={node.id}
      aria-pressed={node.selected} onClick={event => onNodeClick?.(event, node)}>{node.id}</button>)}
    {edges.map(edge => <span key={edge.id} data-edge-id={edge.id}
      data-source={edge.source} data-target={edge.target}>{edge.id}</span>)}
  </div>,
  Background: () => null,
  Controls: () => null,
  Handle: () => null,
  MarkerType: { ArrowClosed: "arrow" },
  Position: { Left: "left", Right: "right" },
}));

let library: RecordedDemoLibrary;

beforeAll(async () => {
  const fsSpecifier = "node:" + "fs";
  const pathSpecifier = "node:" + "path";
  const { readFileSync } = await import(/* @vite-ignore */ fsSpecifier) as {
    readFileSync: (path: string, encoding: string) => string;
  };
  const { resolve } = await import(/* @vite-ignore */ pathSpecifier) as {
    resolve: (...segments: string[]) => string;
  };
  const raw = readFileSync(
    resolve("..", "..", "recordings", "northstar-demo-library-v2-2026-10-28.json"),
    "utf-8",
  );
  const parsedLibrary = JSON.parse(raw) as unknown;
  if (!isRecordedDemoLibrary(parsedLibrary)) {
    throw new Error("Committed northstar-demo-library-v2-2026-10-28.json failed RecordedDemoLibrary validation");
  }
  library = parsedLibrary;
});

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

function findButton(label: string) {
  const match = [...container.querySelectorAll<HTMLButtonElement>("button")]
    .find(item => item.textContent?.trim() === label || item.getAttribute("aria-label") === label);
  if (!match) throw new Error(`Missing button: ${label}`);
  return match;
}

describe("Service assurance recorded stream in DemoLibraryView", () => {
  it("switches to Service assurance · 4 models and renders hops, graph, measures, ranked accounts, and exclusions", async () => {
    await act(async () => root.render(<DemoLibraryView library={library} />));

    expect(findButton("Executive quickstart · 2 models").getAttribute("aria-pressed")).toBe("true");
    const saButton = findButton("Service assurance · 4 models");
    expect(saButton.getAttribute("aria-pressed")).toBe("false");

    await act(async () => saButton.click());
    expect(saButton.getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelector(".badge")?.textContent).toBe("SERVICE ASSURANCE SCENARIO · 4 MODELS");
    expect(container.textContent).toContain("Which enterprise customers were served from sites hit by July incidents");

    const hopRows = container.querySelectorAll('table[aria-label="Seven-hop service assurance path"] tbody tr');
    expect(hopRows).toHaveLength(7);
    for (let hop = 1; hop <= 7; hop++) {
      expect(container.textContent).toContain(`Hop ${hop}`);
    }
    expect(container.textContent).toContain("Bridge: Network -> Enterprise Services");
    expect(container.textContent).toContain("Bridge: Enterprise Services -> Finance");
    expect(container.textContent).toContain("Bridge: Enterprise Services -> Care");

    const graphNodes = [...container.querySelectorAll("[data-node-id]")].map(el => el.getAttribute("data-node-id"));
    const graphEdges = container.querySelectorAll("[data-edge-id]");
    expect(graphNodes).toHaveLength(17);
    expect(graphEdges).toHaveLength(17);
    expect(graphNodes).toEqual(expect.arrayContaining([
      "network_Incidents",
      "enterprise_SlaEvaluation",
      "finance_AccountMonth",
      "care_Cases",
    ]));

    const slaNode = container.querySelector<HTMLButtonElement>('[data-node-id="enterprise_SlaEvaluation"]');
    if (!slaNode) throw new Error("Missing enterprise_SlaEvaluation node");
    await act(async () => slaNode.click());
    expect(container.textContent).toContain("enterprise / SLA Evaluation");
    for (const ruleId of ["R1", "R2", "R3", "R4", "R5"]) {
      expect(container.textContent).toContain(ruleId);
    }

    const modelCards = container.querySelectorAll(".source-model");
    expect(modelCards).toHaveLength(4);
    for (const modelName of ["Network", "Enterprise Services", "Finance", "Care"]) {
      expect(container.textContent).toContain(modelName);
    }
    for (const measureName of [
      "Major Incidents",
      "Credit Exposure",
      "Recognized Service Revenue",
      "Contribution Margin",
      "Escalated Cases",
      "Repeat Contacts",
    ]) {
      expect(container.textContent).toContain(measureName);
    }

    const accountRows = container.querySelectorAll('table[aria-label="Ranked affected enterprise accounts"] tbody tr');
    expect(accountRows.length).toBeGreaterThanOrEqual(15);
    expect(accountRows[0]?.textContent).toContain("service-assurance:acct:000001");
    expect(accountRows[0]?.textContent).toContain("13,580");
    expect(accountRows[0]?.textContent).toContain("9,000");
    expect(accountRows[0]?.textContent).toContain("-1,400");
    const rankedAccountText = [...accountRows].map(row => row.textContent ?? "").join("\n");
    expect(rankedAccountText).not.toContain("service-assurance:acct:000006");

    const exclusionRows = container.querySelectorAll('table[aria-label="Service assurance exclusions"] tbody tr');
    expect(exclusionRows.length).toBeGreaterThanOrEqual(5);
    const exclusionText = [...exclusionRows].map(row => row.textContent ?? "").join("\n");
    expect(exclusionText).toContain("account_key=6");
    expect(exclusionText).toContain("contract_key=8");
    expect(exclusionText).toContain("service_key=10");
    expect(exclusionText).toContain("service_key=12");
    expect(exclusionText).toContain("contract_key=10");

    expect(container.querySelector("footer")?.textContent).toContain("Limits of this recording");
    expect(container.querySelector("footer")?.textContent).toContain("docs/limitations.md");
  });

  it("omits the service assurance switcher button when a two-stream library is loaded", async () => {
    await act(async () => root.render(
      <DemoLibraryView library={{ ...library, service_assurance: undefined }} initialStream="service_assurance" />,
    ));
    const labels = [...container.querySelectorAll(".demo-stream-switch button")].map(btn => btn.textContent?.trim());
    expect(labels).toEqual([
      "Executive quickstart · 2 models",
      "Customer360 deep dive · 8 models",
    ]);
    expect(findButton("Executive quickstart · 2 models").getAttribute("aria-pressed")).toBe("true");
  });
});
