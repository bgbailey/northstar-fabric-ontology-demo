import { describe, expect, it } from "vitest";
import { isRecordedDemoLibrary, isRecordedExecutiveDemo } from "./DemoLibrary";

function fixture() {
  const entities = [
    { id: "SubAccounts", model: "subscriptions", table: "account" },
    { id: "SubMonth", model: "subscriptions", table: "subscription_month" },
    { id: "FinAccounts", model: "finance", table: "account" },
    { id: "FinMonth", model: "finance", table: "account_month" },
  ].map(entity => ({ ...entity, name: entity.id, description: "SYNTHETIC UNIT ONLY", keys: [] }));
  const metrics = [
    { id: "subscriptions.active_lines_eom", model: "subscriptions", table: "subscription_month", entity: "SubMonth" },
    { id: "finance.recognized_service_revenue", model: "finance", table: "account_month", entity: "FinMonth" },
    { id: "finance.contribution_margin", model: "finance", table: "account_month", entity: "FinMonth" },
  ].map(metric => ({ ...metric, name: metric.id, description: "SYNTHETIC UNIT ONLY", representation: "EXTERNALMEASURE" }));
  return {
    schema_version: "1.0", mode: "RECORDED_EXECUTIVE_NATIVE", captured_at: "2026-10-28T12:00:00Z",
    question: "SYNTHETIC NOT EXECUTED", separate_native_item_verified: true,
    native_answer_origin: "MANAGED_NATIVE_SERVICE", native_answer_text: "SYNTHETIC NOT A SERVICE ANSWER",
    native_response_sha256: "a".repeat(64), source_measure_review_sha256: "b".repeat(64),
    native_answer_matches_source_values: true, focus_metric_ids: metrics.map(metric => metric.id),
    ontology: {
      captured_at: "2026-10-28T11:00:00Z", native_snapshot_hash: "c".repeat(64),
      provenance: "ACTUAL_NATIVE_DEFINITION_AND_METADATA_CAPTURE", sources: ["subscriptions", "finance"],
      entities, relationships: [], metrics, rules: [], execution_relationships: {},
      limitations: ["SYNTHETIC UNIT ONLY"],
    },
    rows: [{ account_id: "UNIT", account_key: 1, active_lines_eom: 0,
      recognized_service_revenue_cents: 123, contribution_margin_cents: -17 }],
    source_queries: ["subscriptions", "finance"].map(model => ({
      model, dax: "UNIT QUERY NOT RUN", dax_sha256: "d".repeat(64),
      response_sha256: "e".repeat(64), observed_at: "2026-10-28T11:59:00Z",
    })),
    source_clocks: ["subscriptions", "finance"].map(model => ({
      model, table: "UNIT", data: "UNIT", model_revision: "UNIT",
    })),
    limitations: ["SYNTHETIC UNIT ONLY; no actual native/service claim."],
  };
}
describe("separate executive native archive", () => {
  it("requires the actual separate-item and source-review declarations", () => {
    const value = fixture();
    expect(isRecordedExecutiveDemo(value)).toBe(true);
    expect(isRecordedExecutiveDemo({ ...value, separate_native_item_verified: false })).toBe(false);
    expect(isRecordedExecutiveDemo({ ...value, native_answer_matches_source_values: false })).toBe(false);
    expect(isRecordedExecutiveDemo({ ...value, native_answer_origin: "LOCAL_CRITERION" })).toBe(false);
  });
  it("retains other metrics linked to source measures without expanding the three-measure presentation", () => {
    const value = fixture();
    value.ontology.metrics.push({ ...value.ontology.metrics[1], id: "finance.extra_original" });
    expect(isRecordedExecutiveDemo(value)).toBe(true);
    expect(isRecordedExecutiveDemo({ ...value, focus_metric_ids: ["finance.extra_original"] })).toBe(false);
  });
  it("refuses duplicate identities, missing owners and unsafe or missing source values", () => {
    const value = fixture();
    expect(isRecordedExecutiveDemo({ ...value, rows: [...value.rows, ...value.rows] })).toBe(false);
    expect(isRecordedExecutiveDemo({ ...value, source_queries: value.source_queries.slice(0, 1) })).toBe(false);
    expect(isRecordedExecutiveDemo({ ...value, rows: [{ ...value.rows[0], active_lines_eom: null }] })).toBe(false);
    expect(isRecordedExecutiveDemo({
      ...value, rows: [{ ...value.rows[0], contribution_margin_cents: Number.MAX_SAFE_INTEGER + 1 }],
    })).toBe(false);
    expect(isRecordedExecutiveDemo({ ...value, captured_at: "2026-10-28T12:00:00" })).toBe(false);
  });
  it("never treats a lone executive capture as the two-stream library", () => {
    expect(isRecordedDemoLibrary(fixture())).toBe(false);
    expect(isRecordedDemoLibrary({
      schema_version: "1.0", mode: "RECORDED_DEMO_LIBRARY",
      exported_at: "2026-10-28T12:03:00Z", executive: fixture(), customer360: null,
      limitations: ["SYNTHETIC"],
    })).toBe(false);
  });
});
