// @vitest-environment jsdom
import { act, createElement, type MouseEvent } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { Edge, Node } from "@xyflow/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { DemoLibraryView, isRecordedDemoLibrary, type RecordedDemoLibrary } from "./DemoLibrary";
import { App } from "./main";
import { executionLabel, isHealth } from "./readiness";
import { isRecordedScorecard, ScorecardView, type RecordedScorecard } from "./ScorecardView";

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

export function buildScorecardFixture(
  executionMode: "offline_scripted" | "live_demo_tenant" = "offline_scripted",
): RecordedScorecard {
  const levels = ["L1", "L2", "L3", "L4", "N", "P"] as const;
  const questionCounts: Record<typeof levels[number], number> = {
    L1: 6, L2: 5, L3: 5, L4: 4, N: 4, P: 3,
  };
  const byLevel: RecordedScorecard["by_level"] = {};
  for (const level of levels) {
    const qCount = questionCounts[level];
    const runs = qCount * 5;
    const isL1 = level === "L1";
    const baselineCorrect = isL1 ? runs : Math.floor(runs * 0.2);
    const rulesCorrect = isL1 ? runs : Math.floor(runs * 0.6);
    byLevel[level] = {
      question_count: qCount,
      arms: {
        baseline: {
          runs,
          answer_correct_runs: baselineCorrect,
          answer_accuracy: isL1
            ? { proportion: 1.0, low: 0.8865, high: 1.0 }
            : { proportion: baselineCorrect / runs, low: 0.08, high: 0.42 },
          mean_key_f1: isL1 ? 1.0 : 0.41,
          path_accuracy: isL1
            ? { proportion: 1.0, low: 0.8865, high: 1.0 }
            : { proportion: 0.2, low: 0.08, high: 0.42 },
          unknown_handling_accuracy: { proportion: isL1 ? 1.0 : 0.25, low: 0.1, high: 1.0 },
          mean_repeat_stability: isL1 ? 1.0 : 0.8,
        },
        baseline_plus_rules: {
          runs,
          answer_correct_runs: rulesCorrect,
          answer_accuracy: isL1
            ? { proportion: 1.0, low: 0.8865, high: 1.0 }
            : { proportion: rulesCorrect / runs, low: 0.4, high: 0.78 },
          mean_key_f1: isL1 ? 1.0 : 0.72,
          path_accuracy: isL1
            ? { proportion: 1.0, low: 0.8865, high: 1.0 }
            : { proportion: 0.6, low: 0.4, high: 0.78 },
          unknown_handling_accuracy: { proportion: isL1 ? 1.0 : 0.75, low: 0.5, high: 1.0 },
          mean_repeat_stability: isL1 ? 1.0 : 0.88,
        },
        ontology: {
          runs,
          answer_correct_runs: runs,
          answer_accuracy: { proportion: 1.0, low: 0.8865, high: 1.0 },
          mean_key_f1: 1.0,
          path_accuracy: { proportion: 1.0, low: 0.8865, high: 1.0 },
          unknown_handling_accuracy: { proportion: 1.0, low: 0.8865, high: 1.0 },
          mean_repeat_stability: 1.0,
        },
      },
      differences: [
        {
          comparison: "ontology_vs_baseline",
          left_arm: "ontology",
          right_arm: "baseline",
          metric: "answer_accuracy",
          diff: isL1 ? 0.0 : 0.8,
          low: isL1 ? 0.0 : 0.6,
          high: isL1 ? 0.0 : 0.95,
          excludes_zero: !isL1,
        },
      ],
    };
  }

  return {
    schema_version: "1.0",
    mode: "RECORDED_SCORECARD",
    generated_at: "2026-11-17T13:30:00Z",
    conditions: {
      execution_mode: executionMode,
      profile: "service-assurance",
      seed: 20260929,
      question_count: 27,
      repeats: 5,
      arms: ["baseline", "baseline_plus_rules", "ontology"],
      capacity_sku: "F8",
      region: "Central US",
      model_deployment: "gpt-5-mini",
      toolset_variant: "service_default",
      started_at: "2026-11-17T13:00:00Z",
      completed_at: "2026-11-17T13:20:00Z",
    },
    arm_labels: {
      baseline: "Arm A — Semantic models only",
      baseline_plus_rules: "Arm A+ — Semantic models + rules in prompt",
      ontology: "Arm B — Ontology-grounded",
    },
    summary_by_arm: {
      baseline: {
        arm: "baseline",
        total_runs: 135,
        question_count: 27,
        repeats: 5,
        answer_correct_runs: 30,
        answer_accuracy: { proportion: 0.2222, low: 0.1603, high: 0.2995 },
        mean_key_precision: 0.44,
        mean_key_recall: 0.52,
        mean_key_f1: 0.4767,
        path_accuracy: { proportion: 0.2222, low: 0.1603, high: 0.2995 },
        unknown_handling_accuracy: { proportion: 0.25, low: 0.14, high: 0.4 },
        mean_repeat_stability: 0.84,
        unanimous_pass_rate: 0.2222,
        traps_hit: {
          "site-key-only": 25,
          "credit-per-incident": 20,
          "revenue-fan-out": 15,
          "display-name-or-billing-key": 10,
          "missing-as-zero": 20,
        },
        effort: {
          mean_tool_calls: 9.8,
          mean_input_tokens: 4120.0,
          mean_output_tokens: 810.0,
          mean_seconds: 15.6,
          total_tokens: 665550,
        },
      },
      baseline_plus_rules: {
        arm: "baseline_plus_rules",
        total_runs: 135,
        question_count: 27,
        repeats: 5,
        answer_correct_runs: 93,
        answer_accuracy: { proportion: 0.6889, low: 0.6065, high: 0.7608 },
        mean_key_precision: 0.78,
        mean_key_recall: 0.81,
        mean_key_f1: 0.7947,
        path_accuracy: { proportion: 0.6889, low: 0.6065, high: 0.7608 },
        unknown_handling_accuracy: { proportion: 0.75, low: 0.6, high: 0.86 },
        mean_repeat_stability: 0.91,
        unanimous_pass_rate: 0.6296,
        traps_hit: {
          "site-key-only": 5,
          "credit-per-incident": 5,
          "revenue-fan-out": 4,
          "display-name-or-billing-key": 6,
          "missing-as-zero": 5,
        },
        effort: {
          mean_tool_calls: 8.6,
          mean_input_tokens: 6450.0,
          mean_output_tokens: 740.0,
          mean_seconds: 14.2,
          total_tokens: 970650,
        },
      },
      ontology: {
        arm: "ontology",
        total_runs: 135,
        question_count: 27,
        repeats: 5,
        answer_correct_runs: 135,
        answer_accuracy: { proportion: 1.0, low: 0.9723, high: 1.0 },
        mean_key_precision: 1.0,
        mean_key_recall: 1.0,
        mean_key_f1: 1.0,
        path_accuracy: { proportion: 1.0, low: 0.9723, high: 1.0 },
        unknown_handling_accuracy: { proportion: 1.0, low: 0.9723, high: 1.0 },
        mean_repeat_stability: 1.0,
        unanimous_pass_rate: 1.0,
        traps_hit: {
          "site-key-only": 0,
          "credit-per-incident": 0,
          "revenue-fan-out": 0,
          "display-name-or-billing-key": 0,
          "missing-as-zero": 0,
        },
        effort: {
          mean_tool_calls: 5.2,
          mean_input_tokens: 1850.0,
          mean_output_tokens: 420.0,
          mean_seconds: 8.4,
          total_tokens: 306450,
        },
      },
    },
    by_level: byLevel,
    overall_differences: [
      {
        comparison: "ontology_vs_baseline",
        left_arm: "ontology",
        right_arm: "baseline",
        metric: "answer_accuracy",
        diff: 0.7778,
        low: 0.6296,
        high: 0.9259,
        excludes_zero: true,
      },
      {
        comparison: "ontology_vs_baseline_plus_rules",
        left_arm: "ontology",
        right_arm: "baseline_plus_rules",
        metric: "answer_accuracy",
        diff: 0.3111,
        low: 0.1481,
        high: 0.4815,
        excludes_zero: true,
      },
      {
        comparison: "l1_tie_check",
        left_arm: "ontology",
        right_arm: "baseline",
        metric: "l1_answer_accuracy",
        diff: 0.0,
        low: -0.05,
        high: 0.05,
        excludes_zero: false,
      },
    ],
    failed_questions: [
      {
        question_id: "SA-L2-001",
        level: "L2",
        arm: "baseline",
        correct_repeats: 0,
        repeats: 5,
        traps_hit: ["site-key-only"],
        failure_reasons: ["key_mismatch", "trap:site-key-only"],
      },
      {
        question_id: "SA-L4-001",
        level: "L4",
        arm: "baseline_plus_rules",
        correct_repeats: 2,
        repeats: 5,
        traps_hit: ["display-name-or-billing-key", "missing-as-zero"],
        failure_reasons: ["key_mismatch", "trap:display-name-or-billing-key", "trap:missing-as-zero"],
      },
    ],
    limitations: [
      "Synthetic evaluation dataset only; see docs/limitations.md.",
    ],
  };
}

let committedLibrary: RecordedDemoLibrary;
let container: HTMLDivElement;
let root: Root;

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
  const parsed = JSON.parse(raw) as unknown;
  if (!isRecordedDemoLibrary(parsed)) {
    throw new Error("Committed northstar-demo-library-v2-2026-10-28.json failed RecordedDemoLibrary validation");
  }
  committedLibrary = parsed;
});

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function findButton(label: string) {
  const match = [...container.querySelectorAll<HTMLButtonElement>("button")]
    .find(item => item.textContent?.trim() === label || item.getAttribute("aria-label") === label);
  if (!match) throw new Error(`Missing button: ${label}`);
  return match;
}

describe("isRecordedScorecard contract guard", () => {
  it("accepts offline_scripted and live_demo_tenant scorecards and rejects malformed variants", () => {
    const offline = buildScorecardFixture("offline_scripted");
    const live = buildScorecardFixture("live_demo_tenant");
    expect(isRecordedScorecard(offline)).toBe(true);
    expect(isRecordedScorecard(live)).toBe(true);

    expect(isRecordedScorecard({ ...offline, mode: "RECORDED_DEMO_LIBRARY" })).toBe(false);
    expect(isRecordedScorecard({ ...offline, schema_version: "2.0" })).toBe(false);
    expect(isRecordedScorecard({ ...offline, generated_at: "2026-11-17T13:30:00" })).toBe(false);
    expect(isRecordedScorecard({
      ...offline,
      conditions: { ...offline.conditions, execution_mode: "unverified_mode" },
    })).toBe(false);
    expect(isRecordedScorecard({
      ...offline,
      conditions: { ...offline.conditions, started_at: "2026-11-17T13:00:00" },
    })).toBe(false);
    expect(isRecordedScorecard({
      ...offline,
      summary_by_arm: { ontology: offline.summary_by_arm.ontology },
    })).toBe(false);
    expect(isRecordedScorecard({
      ...offline,
      summary_by_arm: {
        ...offline.summary_by_arm,
        ontology: {
          ...offline.summary_by_arm.ontology,
          answer_accuracy: { proportion: 0.8, low: 0.9, high: 1.0 },
        },
      },
    })).toBe(false);
    expect(isRecordedScorecard({
      ...offline,
      overall_differences: [
        { ...offline.overall_differences[0], diff: 0.5, low: 0.6, high: 0.7 },
      ],
    })).toBe(false);
    expect(isRecordedScorecard({
      ...offline,
      failed_questions: [
        { ...offline.failed_questions[0], correct_repeats: 6, repeats: 5 },
      ],
    })).toBe(false);
    expect(isRecordedScorecard({ ...offline, limitations: [] })).toBe(false);
  });

  it("recognizes RECORDED_SCORECARD in health and execution labels", () => {
    expect(executionLabel("RECORDED_SCORECARD")).toBe("RECORDED SCORECARD / NO NEW EXECUTION");
    expect(isHealth({
      mode: "RECORDED_SCORECARD",
      scope: null,
      data_ready: false,
      context_revision: "scorecard-rev",
      authenticated: false,
      readiness: {
        mode: "RECORDED_SCORECARD",
        ready: false,
        status: "RECORDED_ONLY",
        blockers: ["Explicit evaluation scorecard playback; no live execution in this mode."],
        claim: "Recorded evaluation scorecard with explicit run conditions, not a live grant.",
      },
    })).toBe(true);
  });
});

describe("ScorecardView rendering and standalone/library integration", () => {
  it("distinguishes OFFLINE SCRIPTED HARNESS CHECK from LIVE DEMO TENANT SCORECARD and renders all tables", async () => {
    const offline = buildScorecardFixture("offline_scripted");
    await act(async () => root.render(<ScorecardView scorecard={offline} />));

    expect(container.querySelector(".badge")?.textContent).toBe("OFFLINE SCRIPTED HARNESS CHECK");
    expect(container.querySelector('[data-execution-mode="offline_scripted"]')?.textContent)
      .toContain("Do not treat offline scripted numbers as live language-model");

    const conditionsText = container.querySelector('section[aria-label="Run conditions"]')?.textContent ?? "";
    for (const expected of [
      "service-assurance", "20260929", "27", "5", "gpt-5-mini",
      "service_default", "F8", "Central US", "2026-11-17T13:00:00Z", "2026-11-17T13:20:00Z",
    ]) {
      expect(conditionsText).toContain(expected);
    }

    const overallTable = container.querySelector('table[aria-label="Overall summary by evaluation arm"]');
    expect(overallTable?.querySelectorAll("tbody tr")).toHaveLength(3);
    expect(overallTable?.textContent).toContain("Arm A — Semantic models only");
    expect(overallTable?.textContent).toContain("Arm A+ — Semantic models + rules in prompt");
    expect(overallTable?.textContent).toContain("Arm B — Ontology-grounded");
    expect(overallTable?.textContent).toContain("100.0% [97.2%, 100.0%]");
    expect(overallTable?.textContent).toContain("22.2% [16.0%, 29.9%]");

    const diffTable = container.querySelector('table[aria-label="Paired bootstrap differences"]');
    expect(diffTable?.querySelectorAll("tbody tr")).toHaveLength(3);
    expect(diffTable?.textContent).toContain("+77.8%");
    expect(diffTable?.textContent).toContain("[+63.0%, +92.6%]");
    expect(diffTable?.textContent).toContain("Interval excludes zero");
    expect(diffTable?.textContent).toContain("Tie / includes zero");

    const levelTable = container.querySelector('table[aria-label="Results by question level"]');
    const renderedLevels = [...levelTable?.querySelectorAll("tbody tr") ?? []]
      .map(row => row.getAttribute("data-level-row"));
    expect(renderedLevels).toEqual(["L1", "L2", "L3", "L4", "N", "P"]);

    const trapsTable = container.querySelector('table[aria-label="Grain and validity traps hit"]');
    const renderedTraps = [...trapsTable?.querySelectorAll("tbody tr") ?? []]
      .map(row => row.getAttribute("data-trap-row"));
    expect(renderedTraps).toEqual([
      "site-key-only",
      "credit-per-incident",
      "revenue-fan-out",
      "display-name-or-billing-key",
      "missing-as-zero",
    ]);
    expect(trapsTable?.querySelector('[data-trap-row="site-key-only"]')?.textContent).toContain("25");

    const effortTable = container.querySelector('table[aria-label="Effort comparison"]');
    expect(effortTable?.querySelectorAll("tbody tr")).toHaveLength(3);
    expect(effortTable?.textContent).toContain("5.2");
    expect(effortTable?.textContent).toContain("1,850");
    expect(effortTable?.textContent).toContain("8.4 s");
    expect(effortTable?.textContent).toContain("306,450");

    const failedTable = container.querySelector('table[aria-label="Failed questions"]');
    expect(failedTable?.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(failedTable?.textContent).toContain("SA-L2-001");
    expect(failedTable?.textContent).toContain("0 / 5");
    expect(failedTable?.textContent).toContain("trap:site-key-only");

    expect(container.querySelector("footer")?.textContent).toContain("Limits of this recording");
    expect(container.querySelector("footer")?.textContent).toContain("docs/limitations.md");

    const live = buildScorecardFixture("live_demo_tenant");
    await act(async () => root.render(<ScorecardView scorecard={{ ...live, failed_questions: [] }} />));
    expect(container.querySelector(".badge")?.textContent).toBe("LIVE DEMO TENANT SCORECARD");
    expect(container.querySelector('[data-execution-mode="live_demo_tenant"]')?.textContent)
      .toContain("Sanitized evaluation output recorded from repeated runs against the demo tenant");
    expect(container.textContent).toContain("No failed questions recorded across any evaluation arm.");
  });

  it("plays standalone RECORDED_SCORECARD mode via /api/scorecard", async () => {
    const scorecard = buildScorecardFixture("live_demo_tenant");
    const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
      if (url === "/api/health") {
        return Response.json({
          mode: "RECORDED_SCORECARD",
          scope: null,
          data_ready: false,
          context_revision: "unit-scorecard-rev",
          authenticated: false,
          readiness: {
            mode: "RECORDED_SCORECARD",
            ready: false,
            status: "RECORDED_ONLY",
            blockers: ["Explicit evaluation scorecard playback; no live execution in this mode."],
            claim: "Recorded evaluation scorecard with explicit run conditions, not a live grant.",
          },
        });
      }
      if (url === "/api/capabilities") return Response.json({ experiments: [] });
      if (url === "/api/scorecard") return Response.json(scorecard);
      throw new Error(`Unexpected mocked request: ${String(url)}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await act(async () => root.render(createElement(App)));
    expect(container.querySelector(".badge")?.textContent).toBe("LIVE DEMO TENANT SCORECARD");
    expect(container.textContent).toContain("Arm B — Ontology-grounded");
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/health",
      "/api/capabilities",
      "/api/scorecard",
    ]);
  });

  it("refuses malformed /api/scorecard payloads without falling back to offline or live execution", async () => {
    const scorecard = buildScorecardFixture("live_demo_tenant");
    const badFetchMock = vi.fn(async (url: RequestInfo | URL) => {
      if (url === "/api/health") {
        return Response.json({
          mode: "RECORDED_SCORECARD",
          scope: null,
          data_ready: false,
          context_revision: "unit-scorecard-rev",
          authenticated: false,
          readiness: {
            mode: "RECORDED_SCORECARD",
            ready: false,
            status: "RECORDED_ONLY",
            blockers: ["Explicit evaluation scorecard playback; no live execution in this mode."],
          },
        });
      }
      if (url === "/api/capabilities") return Response.json({ experiments: [] });
      if (url === "/api/scorecard") return Response.json({ ...scorecard, mode: "OFFLINE_SYNTHETIC" });
      throw new Error(`Unexpected mocked request: ${String(url)}`);
    });
    vi.stubGlobal("fetch", badFetchMock);

    await act(async () => root.render(createElement(App)));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Invalid recorded scorecard file");
  });

  it("shows Evaluation scorecard in DemoLibraryView when library.scorecard is present and switches cleanly", async () => {
    const libraryWithoutScorecard: RecordedDemoLibrary = { ...committedLibrary, scorecard: undefined };
    await act(async () => root.render(<DemoLibraryView library={libraryWithoutScorecard} initialStream="scorecard" />));
    const withoutLabels = [...container.querySelectorAll(".demo-stream-switch button")].map(btn => btn.textContent?.trim());
    expect(withoutLabels).not.toContain("Evaluation scorecard");
    expect(findButton("Executive quickstart · 2 models").getAttribute("aria-pressed")).toBe("true");

    const libraryWithScorecard: RecordedDemoLibrary = {
      ...committedLibrary,
      scorecard: buildScorecardFixture("live_demo_tenant"),
    };
    await act(async () => root.render(<DemoLibraryView library={libraryWithScorecard} />));
    const withLabels = [...container.querySelectorAll(".demo-stream-switch button")].map(btn => btn.textContent?.trim());
    expect(withLabels).toEqual([
      "Executive quickstart · 2 models",
      "Service assurance · 4 models",
      "Customer360 deep dive · 8 models",
      "Evaluation scorecard",
    ]);

    const scorecardButton = findButton("Evaluation scorecard");
    expect(scorecardButton.getAttribute("aria-pressed")).toBe("false");
    await act(async () => scorecardButton.click());
    expect(scorecardButton.getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelector(".badge")?.textContent).toBe("LIVE DEMO TENANT SCORECARD");
    expect(container.querySelector('table[aria-label="Overall summary by evaluation arm"]')).not.toBeNull();
    expect(container.textContent).toContain("Interval excludes zero");
    expect(container.textContent).toContain("Tie / includes zero");
  });
});
