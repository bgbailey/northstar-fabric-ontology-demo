export type Scope = {
  tenant: string; principal: string; authorization_revision: string;
  models: string[]; mode: string;
};
export type Health = {
  mode: string; scope: Scope | null; data_ready: boolean; context_revision: string;
  authenticated?: boolean; csrf_token?: string | null; session_epoch?: string | null;
  signin_url?: string;
};

// Visible submitted-plan fixture; never used to route or answer natural-language requests.
export function examplePlan(health: Health) {
  if (!health.scope) throw new Error("A server-owned scope is required");
  const query = (id: string, model: string, table: string, metric: string) => ({
    id, kind: "QuerySemanticModel", model, table, metrics: [metric],
    group_by: ["account_key"], filters: [{ column: "month", op: "eq", values: ["2026-08-01"] }],
    date_role: "month", expected_grain: ["account_key"], max_rows: 1000,
  });
  return {
    version: "1.0", request_id: crypto.randomUUID(),
    question: "Show August end-of-month lines and recognized service revenue by account.",
    principal_scope: health.scope, as_of: "2026-09-29T00:00:00Z",
    ontology_revision: health.context_revision, clarification_state: "resolved",
    metric_selections: ["subscriptions.active_lines_eom", "finance.recognized_service_revenue"],
    nodes: [
      query("lines", "subscriptions", "subscription_month", "subscriptions.active_lines_eom"),
      query("revenue", "finance", "account_month", "finance.recognized_service_revenue"),
      { id: "aligned", kind: "JoinResults", left: "lines", right: "revenue",
        relationship: "account-month-conformed-v1", left_keys: ["account_key"], right_keys: ["account_key"],
        cardinality: "one_to_one", unmatched: "error", max_fanout: 1 },
      { id: "answer", kind: "SynthesizeAnswer", source: "aligned" },
    ],
    result_node: "answer",
    budgets: { max_rows: 10000, max_bytes: 8000000, max_queries: 4, timeout_seconds: 45 },
  };
}
