// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { isRecordedOntology } from "./RecordedOntology";

function packet() {
  return {
    captured_at: "2026-10-20T12:00:00Z", native_snapshot_hash: "a".repeat(64),
    provenance: "ACTUAL_NATIVE_DEFINITION_AND_METADATA_CAPTURE", sources: ["finance", "care"],
    entities: [
      { id: "Finance", model: "finance", table: "account", name: "Accounts", description: "UNIT", keys: ["account_key"] },
      { id: "Care", model: "care", table: "account", name: "Accounts", description: "UNIT", keys: ["account_key"] },
    ],
    relationships: [{ id: "bridge", source: "Finance", target: "Care", description: "UNIT" }],
    metrics: [{ id: "finance.amount", model: "finance", table: "account", entity: "Finance",
      name: "Amount", description: "UNIT", representation: "EXTERNALMEASURE" }],
    rules: [{ id: "Scope", statement: "UNIT", referenced_entities: ["Finance"], property_scope: "None" }],
    execution_relationships: { "reviewed-contract": "bridge" },
    limitations: ["UNIT, not an instance graph or execution grant."],
  };
}
describe("captured native metadata", () => {
  it("accepts consistent native objects and source-owned metric homes", () => {
    expect(isRecordedOntology(packet())).toBe(true);
  });
  it("rejects an incorrect metric owner or invented traversal mapping", () => {
    const wrong = packet();
    wrong.metrics[0].model = "care";
    expect(isRecordedOntology(wrong)).toBe(false);
    const missing = packet();
    missing.execution_relationships["reviewed-contract"] = "missing";
    expect(isRecordedOntology(missing)).toBe(false);
  });
  it("rejects duplicated native identities and missing endpoints", () => {
    const duplicate = packet();
    duplicate.entities.push(duplicate.entities[0]);
    expect(isRecordedOntology(duplicate)).toBe(false);
    const missing = packet();
    missing.relationships[0].target = "missing";
    expect(isRecordedOntology(missing)).toBe(false);
  });
});
