import { describe, expect, it } from "vitest";
import { examplePlan, type Health } from "./example";

const health: Health = {
  mode: "OFFLINE_SYNTHETIC", data_ready: true, context_revision: "offline-v1",
  scope: { tenant: "synthetic", principal: "local", authorization_revision: "1",
    models: ["subscriptions", "finance"], mode: "OFFLINE_SYNTHETIC" },
};

describe("visible engineering fixture", () => {
  it("preserves server scope and distinguishes two source models", () => {
    const plan = examplePlan(health);
    expect(plan.principal_scope).toEqual(health.scope);
    expect(plan.nodes.filter(n => n.kind === "QuerySemanticModel")).toHaveLength(2);
    expect(plan.ontology_revision).toBe("offline-v1");
    expect(plan.nodes.find(n => n.kind === "JoinResults")).toMatchObject({ unmatched: "error" });
  });
  it("gives each submitted run a fresh correlation id", () => {
    expect(examplePlan(health).request_id).not.toBe(examplePlan(health).request_id);
  });
});
