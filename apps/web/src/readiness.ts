import type { Health } from "./example";
import type { RecordedDemo } from "./recorded";

export type Readiness = {
  ready: boolean; status: string; blockers: string[]; claim?: string;
  mode?: string; implementation?: string; configuration?: string; reader_session?: string;
  native_source_probe?: string; application_execution?: string; demonstration?: string;
};
export type HealthState = Health & { readiness: Readiness };

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === "string");
}
function scope(value: unknown): boolean {
  return value === null || (object(value) && strings(value.models)
    && ["tenant", "principal", "authorization_revision", "mode"].every(key => typeof value[key] === "string"));
}
export function isReadiness(value: unknown): value is Readiness {
  return object(value) && typeof value.ready === "boolean" && typeof value.status === "string"
    && strings(value.blockers) && (!value.ready || value.blockers.length === 0)
    && ["claim", "mode", "implementation", "configuration", "reader_session",
      "native_source_probe", "application_execution", "demonstration"]
      .every(key => value[key] === undefined || typeof value[key] === "string");
}
export function isHealth(value: unknown): value is HealthState {
  return object(value) && ["OFFLINE_SYNTHETIC", "RECORDED_NATIVE_DEMO", "RECORDED_LIVE_COMPARISON",
    "RECORDED_DEMO_LIBRARY", "RECORDED_AGENT_SURFACE", "DELEGATED_READER_SETUP", "LIVE_CONFIGURED"]
    .includes(String(value.mode)) && typeof value.data_ready === "boolean"
    && typeof value.context_revision === "string" && isReadiness(value.readiness) && value.readiness.mode === value.mode
    && scope(value.scope)
    && (value.authenticated === undefined || typeof value.authenticated === "boolean")
    && ["csrf_token", "session_epoch"].every(key =>
      value[key] === undefined || value[key] === null || typeof value[key] === "string")
    && (value.signin_url === undefined || value.signin_url === "/auth/login");
}
export function readerMode(mode: string): boolean {
  return mode === "LIVE_CONFIGURED" || mode === "DELEGATED_READER_SETUP";
}
export function signInFailureMessage(reason: string | null): string {
  if (reason === "sign_in_expired") {
    return "This sign-in request expired. Start a new sign-in and complete it within 10 minutes. No permissions were changed automatically.";
  }
  if (reason === "sign_in_restart") {
    return "This sign-in request is missing, replaced or already used. Start a fresh sign-in and use only the newest sign-in window. No permissions were changed automatically.";
  }
  if (reason === "reader_window_expired") {
    return "The approved demo user window has expired. A new approved window is required before signing in again. No permissions were changed automatically.";
  }
  return "Sign-in could not be completed. This alone does not establish an account or permission problem. Start a fresh sign-in; no permissions were changed automatically.";
}
export function executionLabel(mode: string): string {
  if (mode === "LIVE_DELEGATED") return "LIVE / EXECUTED";
  if (mode === "LIVE_CONFIGURED") return "LIVE / CONFIGURATION ONLY";
  if (mode === "OFFLINE_SYNTHETIC") return "OFFLINE SYNTHETIC";
  if (mode === "RECORDED_NATIVE_DEMO") return "RECORDED";
  if (mode === "RECORDED_LIVE_COMPARISON") return "RECORDED LIVE / NO NEW EXECUTION";
  if (mode === "RECORDED_DEMO_LIBRARY") return "RECORDED DEMO STREAMS / NO NEW EXECUTION";
  if (mode === "RECORDED_AGENT_SURFACE") return "RECORDED AGENT SURFACE / NO NEW EXECUTION";
  if (mode === "DELEGATED_READER_SETUP") return "DEMO USER SETUP / NOT EXECUTED";
  return mode.replaceAll("_", " ");
}
export function readinessStages(value: Readiness): [string, string][] {
  return [
    ["Implementation", value.implementation ?? "UNKNOWN"],
    ["Configuration", value.configuration ?? "UNKNOWN"],
    ["Demo user session (not source authorization)", value.reader_session ?? "UNKNOWN"],
    ["Current Fabric and source authorization probes", value.native_source_probe ?? "UNKNOWN_NOT_PROBED"],
    ["Application execution (requires separate run evidence)", value.application_execution ?? "NOT_ESTABLISHED"],
    ["Customer demonstration (requires separate evidence)", value.demonstration ?? "NOT_ESTABLISHED"],
  ];
}
function timestamp(value: unknown): value is string {
  return typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    && Number.isFinite(Date.parse(value));
}
export function captureTime(value: string | null | undefined): string {
  return value && timestamp(value) ? value : "Not recorded";
}
export function isRecording(value: unknown): value is RecordedDemo & { created_at?: string | null } {
  return object(value) && value.schema_version === "1.0" && value.mode === "RECORDED_NATIVE_DEMO"
    && timestamp(value.captured_at) && timestamp(value.context_captured_at)
    && (value.created_at === undefined || value.created_at === null || timestamp(value.created_at))
    && typeof value.question === "string" && Array.isArray(value.sources) && value.sources.length > 0
    && value.sources.every(source => object(source) && typeof source.id === "string" && typeof source.name === "string")
    && Array.isArray(value.entities) && value.entities.length > 0 && value.entities.every(entity =>
      object(entity) && ["id", "source_id", "backing_table", "description"].every(key => typeof entity[key] === "string")
      && ["keys", "properties", "guidance"].every(key => strings(entity[key])))
    && Array.isArray(value.relationships) && value.relationships.every(edge => object(edge)
      && ["id", "source", "target"].every(key => typeof edge[key] === "string") && strings(edge.guidance))
    && Array.isArray(value.metrics) && value.metrics.length > 0 && value.metrics.every(metric => object(metric)
      && ["id", "entity_id", "source_id", "home_table", "name", "description"].every(key => typeof metric[key] === "string"))
    && Number.isInteger(value.native_rule_count) && Number(value.native_rule_count) >= 0
    && strings(value.focus_entity_ids) && value.focus_entity_ids.length >= 2
    && Array.isArray(value.queries) && value.queries.every(query =>
      object(query) && typeof query.source_id === "string" && typeof query.dax === "string")
    && Array.isArray(value.rows) && value.rows.length > 0 && value.rows.every(row => object(row)
      && ["account_key", "active_lines_eom", "recognized_service_revenue_cents"].every(key =>
        typeof row[key] === "number" && Number.isSafeInteger(row[key]))
      && Number(row.account_key) >= 0 && Number(row.active_lines_eom) >= 0)
    && object(value.source_hashes) && Object.keys(value.source_hashes).length > 0
    && Object.values(value.source_hashes).every(hash => typeof hash === "string")
    && strings(value.limitations) && value.limitations.length > 0;
}
