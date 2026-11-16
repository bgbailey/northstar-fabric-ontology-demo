import { describe, expect, it } from "vitest";
import { metricReviewCandidates } from "./MetricInsight";
import type { AnswerView } from "./execution";

function answer(): AnswerView {
  return {
    status: "COMPLETE", evidence_ids: ["SYNTHETIC_UNIT_RECEIPT"], coverage: {}, warnings: [],
    rows: [
      { account_key: 1, "subscriptions.active_lines_eom": 0,
        "finance.recognized_service_revenue": 123, "finance.contribution_margin": -17 },
      { account_key: 2, "subscriptions.active_lines_eom": 0,
        "finance.recognized_service_revenue": 123, "finance.contribution_margin": null },
      { account_key: 3, "subscriptions.active_lines_eom": 2,
        "finance.recognized_service_revenue": 123, "finance.contribution_margin": -17 },
    ],
  };
}
describe("source metric review criterion", () => {
  it("uses the original aligned values without calculating a replacement measure", () => {
    const value = answer();
    expect(metricReviewCandidates(value)).toEqual([
      { account: "1", activeLines: 0, revenueCents: 123, marginCents: -17 },
    ]);
    expect(value.rows[0]["finance.contribution_margin"]).toBe(-17);
  });
  it("does not turn source unknowns, absent evidence or duplicate account grains into an insight", () => {
    for (const mutation of ["unknown", "evidence", "duplicate"]) {
      const value = answer();
      if (mutation === "unknown") value.rows[0]["subscriptions.active_lines_eom"] = null;
      if (mutation === "evidence") value.evidence_ids = [];
      if (mutation === "duplicate") value.rows.push({ ...value.rows[0] });
      expect(metricReviewCandidates(value)).toEqual([]);
    }
  });
  it("refuses unqualified partials and retains qualified-subset semantics", () => {
    const value = answer();
    value.status = "PARTIAL";
    expect(metricReviewCandidates(value)).toEqual([]);
    value.population_qualification = "known_mapped_subset";
    value.exclusions = [{ source_node: "UNIT", source_column: "account_key",
      unknown_mapping_observations: 1, null_key_rows: 0, evidence_ids: ["UNIT"] }];
    expect(metricReviewCandidates(value)).toHaveLength(1);
    value.coverage.unknown_source_rows = 1;
    expect(metricReviewCandidates(value)).toEqual([]);
  });
  it("does not accept fractional, nonfinite or unsafe metric encodings", () => {
    for (const invalid of [0.5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1, "0"]) {
      const value = answer();
      value.rows[0]["subscriptions.active_lines_eom"] = invalid;
      expect(metricReviewCandidates(value)).toEqual([]);
    }
  });
});
