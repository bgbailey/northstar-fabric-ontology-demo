export type ConsentResource = "source_metadata" | "storage";
type ResourceState = {
  resource: ConsentResource; configuration: "CONFIGURED" | "NOT_CONFIGURED";
  consent: "COMPLETED_IN_CURRENT_SESSION" | "EXISTING_GRANT_VALIDATED_IN_CURRENT_SESSION"
    | "NOT_ESTABLISHED_IN_CURRENT_SESSION";
  authorization: "UNKNOWN_NOT_PROBED"; execution: "NOT_ESTABLISHED";
};
const purposes: Record<ConsentResource, string> = {
  source_metadata: "Consent to Fabric Item.ReadWrite.All for source-model metadata export",
  storage: "Consent to Storage user_impersonation for OneLake metadata reads",
};
function resourceStates(value: unknown): ResourceState[] {
  if (!Array.isArray(value) || value.length !== 2) return [];
  const valid = value.every(item => typeof item === "object" && item !== null
    && (item.resource === "source_metadata" || item.resource === "storage")
    && ["CONFIGURED", "NOT_CONFIGURED"].includes(item.configuration)
    && ["COMPLETED_IN_CURRENT_SESSION", "EXISTING_GRANT_VALIDATED_IN_CURRENT_SESSION",
      "NOT_ESTABLISHED_IN_CURRENT_SESSION"].includes(item.consent)
    && item.authorization === "UNKNOWN_NOT_PROBED" && item.execution === "NOT_ESTABLISHED");
  return valid && new Set(value.map(item => item.resource)).size === 2 ? value : [];
}
export function hasResourceGrant(value: unknown, resource: ConsentResource): boolean {
  return resourceStates(value).some(state =>
    state.resource === resource && state.consent !== "NOT_ESTABLISHED_IN_CURRENT_SESSION");
}
export function ResourceConsent({ resources, enabled, pending, onConsent, onExistingGrant }: {
  resources: unknown; enabled: boolean; pending: boolean;
  onConsent: (resource: ConsentResource) => Promise<void>;
  onExistingGrant: (resource: ConsentResource) => Promise<void>;
}) {
  const states = resourceStates(resources);
  return <section className="card" aria-label="Reviewed resource consent" style={{ color: "var(--cp-text)" }}>
    <h2>Optional resource permissions</h2>
    <p>Use an existing grant first, or explicitly start browser consent if Microsoft requires it.
      Fabric Item.ReadWrite.All is a read/write delegated permission,
      not a demo user item grant. Storage user_impersonation acts as the same signed-in user.
      Neither grants new workspace or item rights.</p>
    <p className="muted">Configuration and consent are separate from current source authorization,
      probes and execution. Consent does not make the live demo ready.</p>
    {states.length === 0 && <p>Resource configuration is not verified; consent actions are blocked.</p>}
    {states.map(state => <div key={state.resource}>
      <h3>{state.resource === "source_metadata" ? "Source-model metadata" : "OneLake metadata"}</h3>
      <p>{state.configuration} · {state.consent} · {state.authorization} · {state.execution}</p>
      <button disabled={!enabled || pending || state.configuration !== "CONFIGURED"
        || state.consent !== "NOT_ESTABLISHED_IN_CURRENT_SESSION"}
        onClick={() => void onExistingGrant(state.resource)}>
        Use existing {state.resource === "source_metadata" ? "Fabric" : "Storage"} grant
      </button>
      <button disabled={!enabled || pending || state.configuration !== "CONFIGURED"}
        onClick={() => void onConsent(state.resource)}>{purposes[state.resource]}</button>
    </div>)}
    {pending && <p role="status">Resource authorization is in progress. Prior answers were discarded; the resulting fresh session must be verified.</p>}
  </section>;
}
