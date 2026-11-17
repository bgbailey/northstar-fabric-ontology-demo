// @vitest-environment jsdom
import { act, createElement, type MouseEvent } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { Edge, Node } from "@xyflow/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { DemoLibraryView, isRecordedDemoLibrary, type RecordedDemoLibrary } from "./DemoLibrary";
import { App } from "./main";
import {
  AgentSurfaceCaptureView,
  isRecordedAgentSurface,
  type AgentSurfaceId,
  type RecordedAgentSurface,
} from "./ServiceAssuranceDemo";

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
const surfaceRecordings = new Map<AgentSurfaceId, RecordedAgentSurface>();
const SURFACE_FILES: readonly [AgentSurfaceId, string][] = [
  ["fabric-ontology-mcp", "surface-fabric-ontology-mcp-2026-11-17.json"],
  ["foundry-agent", "surface-foundry-agent-2026-11-17.json"],
  ["github-copilot-vscode", "surface-github-copilot-vscode-2026-11-17.json"],
  ["copilot-studio-m365", "surface-copilot-studio-m365-2026-11-17.json"],
];

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

  for (const [surfaceId, fileName] of SURFACE_FILES) {
    const surfaceRaw = readFileSync(resolve("..", "..", "recordings", fileName), "utf-8");
    const parsedSurface = JSON.parse(surfaceRaw) as unknown;
    if (!isRecordedAgentSurface(parsedSurface) || parsedSurface.surface_id !== surfaceId) {
      throw new Error(`Committed ${fileName} failed RecordedAgentSurface validation`);
    }
    surfaceRecordings.set(surfaceId, parsedSurface);
  }
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
      <DemoLibraryView
        library={{ ...library, service_assurance: undefined, scorecard: undefined }}
        initialStream="service_assurance"
      />,
    ));
    const labels = [...container.querySelectorAll(".demo-stream-switch button")].map(btn => btn.textContent?.trim());
    expect(labels).toEqual([
      "Executive quickstart · 2 models",
      "Customer360 deep dive · 8 models",
    ]);
    expect(findButton("Executive quickstart · 2 models").getAttribute("aria-pressed")).toBe("true");
  });

  it("switches across all four agent surfaces and toggles L2, L4, and Both turns", async () => {
    await act(async () => root.render(<DemoLibraryView library={library} initialStream="service_assurance" />));

    const surfaceButtons = [...container.querySelectorAll<HTMLButtonElement>("[data-surface-id]")];
    expect(surfaceButtons.map(btn => btn.getAttribute("data-surface-id"))).toEqual([
      "fabric-ontology-mcp",
      "foundry-agent",
      "github-copilot-vscode",
      "copilot-studio-m365",
    ]);
    expect(findButton("L4 · Service assurance").getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelectorAll("[data-turn-level]")).toHaveLength(1);
    expect(container.querySelector('[data-turn-level="L4"]')).not.toBeNull();

    const foundryBtn = container.querySelector<HTMLButtonElement>('[data-surface-id="foundry-agent"]');
    if (!foundryBtn) throw new Error("Missing foundry-agent surface button");
    await act(async () => foundryBtn.click());
    expect(foundryBtn.getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelector(".surface-provenance-banner")?.getAttribute("data-provenance"))
      .toBe("LIVE_DEMO_TENANT_SANITIZED_CAPTURE");
    expect(container.textContent).toContain("foundry:responses(gpt-5-mini)");
    expect(container.textContent).toContain("account_key 1");

    await act(async () => findButton("L2 · Exposed services").click());
    expect(findButton("L2 · Exposed services").getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelector('[data-turn-level="L2"]')?.textContent).toContain("53");
    expect(container.querySelector('[data-turn-level="L2"]')?.textContent).toContain("service_key 1");
    expect(container.querySelector('[data-turn-level="L4"]')).toBeNull();

    await act(async () => findButton("Both L2 and L4").click());
    expect(container.querySelectorAll("[data-turn-level]")).toHaveLength(2);

    const copilotStudioBtn = container.querySelector<HTMLButtonElement>('[data-surface-id="copilot-studio-m365"]');
    if (!copilotStudioBtn) throw new Error("Missing copilot-studio-m365 surface button");
    await act(async () => copilotStudioBtn.click());
    expect(container.querySelector(".surface-provenance-banner")?.getAttribute("data-provenance"))
      .toBe("STAGED_SURFACE_PREVIEW_CAPTURE");
    expect(container.textContent).toContain("mcp_FabricIQOntology:search_ontology");
  });

  it.each([
    "fabric-ontology-mcp",
    "foundry-agent",
    "github-copilot-vscode",
    "copilot-studio-m365",
  ] as const)("plays standalone surface capture %s in AgentSurfaceCaptureView and App", async surfaceId => {
    const surface = surfaceRecordings.get(surfaceId);
    if (!surface) throw new Error(`Missing loaded surface ${surfaceId}`);

    await act(async () => root.render(<AgentSurfaceCaptureView surface={surface} />));
    expect(container.querySelector("h1")?.textContent).toBe(surface.surface_name);
    expect(container.querySelectorAll("[data-turn-level]")).toHaveLength(2);
    expect(container.textContent).toContain("53");
    expect(container.textContent).toContain("46");
    expect(container.querySelector("footer")?.textContent).toContain("Limits of this recording");

    const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
      if (url === "/api/health") {
        return Response.json({
          mode: "RECORDED_AGENT_SURFACE",
          scope: null,
          data_ready: false,
          context_revision: "unit-surface-rev",
          authenticated: false,
          readiness: {
            mode: "RECORDED_AGENT_SURFACE",
            ready: false,
            status: "RECORDED_ONLY",
            blockers: ["Explicit single-surface capture playback; no live execution in this mode."],
            claim: "Sanitized agent surface capture with explicit provenance, not a demo user grant.",
          },
        });
      }
      if (url === "/api/capabilities") return Response.json({ experiments: [] });
      if (url === "/api/agent-surface") return Response.json(surface);
      throw new Error(`Unexpected mocked request: ${String(url)}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await act(async () => root.render(createElement(App)));
    expect(container.querySelector("h1")?.textContent).toBe(surface.surface_name);
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/health",
      "/api/capabilities",
      "/api/agent-surface",
    ]);
  });
});
