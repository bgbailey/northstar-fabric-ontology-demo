import { describe, expect, it } from "vitest";
import {
  definitionLayout, definitionView, metricEvidence, recordedPeriod, recordedResultColumn,
  recordedTotals, sourceInventory, type RecordedDemo, type RecordedMetric,
} from "./recorded";

const recording: RecordedDemo = {
  schema_version: "1.0", mode: "RECORDED_NATIVE_DEMO",
  captured_at: "2026-09-30T21:00:00Z", context_captured_at: "2026-09-30T20:59:00Z",
  question: "Synthetic unit fixture, not a live receipt.", sources: [{ id: "source", name: "Synthetic" }],
  entities: ["Lines", "Accounts", "Finance", "Other"].map(id => ({
    id, source_id: "source", backing_table: id, description: "", keys: [], properties: [], guidance: [],
  })),
  relationships: [
    { id: "line-account", source: "Lines", target: "Accounts", guidance: [] },
    { id: "finance-account", source: "Finance", target: "Accounts", guidance: [] },
    { id: "other-account", source: "Other", target: "Accounts", guidance: [] },
  ],
  metrics: [], native_rule_count: 0, focus_entity_ids: ["Lines", "Accounts", "Finance"],
  queries: [], rows: [], source_hashes: { synthetic: "unit-only" }, limitations: ["Synthetic unit fixture."],
};

describe("recorded-native display", () => {
  it("highlights only existing native links without reversing their direction", () => {
    const view = definitionView(recording, true);
    expect(view.entities).toHaveLength(3);
    expect(view.relationships).toHaveLength(2);
    expect(view.relationships[1]).toMatchObject({ source: "Finance", target: "Accounts" });
    expect(definitionView(recording, false).relationships).toHaveLength(3);
  });
  it("keeps integer cents exact even when the displayed total exceeds safe Number arithmetic", () => {
    const withRows = { ...recording, rows: [1, 2].map(account_key => ({
      account_key, active_lines_eom: 1, recognized_service_revenue_cents: Number.MAX_SAFE_INTEGER,
    })) };
    expect(recordedTotals(withRows)).toEqual({ lines: 2n, cents: 18014398509481982n });
  });
  it("keeps the same entities and directed edges in both full-definition lenses", () => {
    const connected = definitionLayout(recording, false, "connected");
    const sources = definitionLayout(recording, false, "sources");
    expect(connected.entities).toEqual(sources.entities);
    expect(connected.relationships).toEqual(sources.relationships);
    for (const view of [connected, sources]) {
      expect(view.positions.size).toBe(recording.entities.length);
      expect(new Set([...view.positions.values()].map(position => JSON.stringify(position))).size)
        .toBe(recording.entities.length);
    }
    expect(definitionLayout(recording, true, "connected").relationships).toEqual(definitionView(recording, true).relationships);
  });
  it("groups metrics by their own source and resolves their exact owner, not the entity source", () => {
    const metric: RecordedMetric = { id: "foreign-owner-unit", entity_id: "Lines",
      source_id: "finance", home_table: "Authoritative Home", name: "Amount ] exact", description: "" };
    // Deliberately inconsistent direct input tests defensive presentation, not an accepted service packet.
    const packet = { ...recording, sources: [...recording.sources, { id: "finance", name: "Unit Finance owner" }],
      metrics: [metric], queries: [
        { source_id: "source", dax: "EVALUATE [Amount ]] exact]" },
        { source_id: "finance", dax: "EVALUATE [Amount ]] exact]" },
        { source_id: "finance", dax: "EVALUATE [Other Amount]" },
      ] };
    expect(sourceInventory(packet).map(source => [source.entities.length, source.metrics.length])).toEqual([[4, 0], [0, 1]]);
    expect(metricEvidence(packet, metric)).toMatchObject({
      source: { name: "Unit Finance owner" }, entity: { id: "Lines", source_id: "source" },
      queries: [{ source_id: "finance", dax: "EVALUATE [Amount ]] exact]" }],
    });
    expect(metricEvidence({ ...packet, sources: [] }, metric).source).toBeUndefined();
    expect(recordedResultColumn(metric)).toBeNull();
  });
  it("identifies only the two exact observed source/home/measure result bindings", () => {
    const metric = { id: "unit", entity_id: "Lines", source_id: "subscriptions",
      home_table: "Subscription Month", name: "Active Lines EOM", description: "" };
    expect(recordedResultColumn(metric)).toBe("active_lines_eom");
    expect(recordedResultColumn({ ...metric, source_id: "finance" })).toBeNull();
    expect(recordedResultColumn({ ...metric, home_table: "Other" })).toBeNull();
    expect(recordedResultColumn({ ...metric, name: "Active Accounts EOM" })).toBeNull();
  });
  it("does not treat measure names in DAX comments or string/table-name literals as query evidence", () => {
    const metric = { id: "unit", entity_id: "Lines", source_id: "source",
      home_table: "Lines", name: "Amount", description: "" };
    const queries = [
      'EVALUATE [Other] // [Amount]',
      'EVALUATE [Other] -- [Amount]',
      'EVALUATE [Other] /* outer /* nested */ [Amount] */',
      'EVALUATE ROW("quoted ""[Amount]""", 0)',
      "EVALUATE 'Table ''[Amount]'''",
      'EVALUATE [Amount]',
    ].map(dax => ({ source_id: "source", dax }));
    expect(metricEvidence({ ...recording, queries }, metric).queries).toEqual([queries[5]]);
    expect(recordedPeriod({ ...recording, queries: [
      { source_id: "source", dax: 'EVALUATE [Other] // >= DATE(2026,8,1) && < DATE(2026,9,1)' },
    ] })).toBe("Not established by captured queries");
  });
  it("extracts a period only from consistent explicit month windows, never from the current clock", () => {
    const window = (year: number, month: number, nextYear: number, nextMonth: number) =>
      `FILTER('Unit', 'Unit'[month] >= DATE(${year},${month},1) && 'Unit'[month] < DATE(${nextYear},${nextMonth},1))`;
    const packet = { ...recording, queries: [{ source_id: "source", dax: window(2026, 8, 2026, 9) }] };
    expect(recordedPeriod(packet)).toBe("August 2026");
    expect(recordedPeriod({ ...packet, queries: [{ source_id: "source", dax: window(2026, 12, 2027, 1) }] })).toBe("December 2026");
    expect(recordedPeriod(recording)).toBe("Not established by captured queries");
    for (const dax of ["EVALUATE [Unit]", window(2026, 13, 2027, 2), window(2026, 8, 2026, 10)]) {
      expect(recordedPeriod({ ...packet, queries: [...packet.queries, { source_id: "source", dax }] }))
        .toBe("Not established by captured queries");
    }
  });
  it("does not invent links for a disconnected entity", () => {
    const packet = { ...recording, entities: [...recording.entities, { ...recording.entities[0], id: "Unlinked" }] };
    const view = definitionLayout(packet, false, "connected");
    expect(view.positions.has("Unlinked")).toBe(true);
    expect(view.relationships).toEqual(recording.relationships);
    expect(packet).toEqual({ ...recording, entities: [...recording.entities, { ...recording.entities[0], id: "Unlinked" }] });
  });
});
