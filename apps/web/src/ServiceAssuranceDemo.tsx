import { isRecordedOntology, RecordedOntologyView, type RecordedOntology } from "./RecordedOntology";

export type ServiceAssuranceHop = {
  hop: number;
  from_entity: string;
  to_entity: string;
  join_keys: string[];
  validity: string;
  crosses_models: boolean;
  models_label: string;
  summary: string;
};

export type ServiceAssuranceModelEvidence = {
  model_id: "network" | "enterprise" | "finance" | "care";
  model_name: string;
  home_tables: string[];
  measures: string[];
  grain: string;
  summary: string;
  row_count: number;
};

export type ServiceAssuranceAccountRow = {
  account_key: number;
  account_id: string;
  display_name: string;
  region: string;
  affected_sites: number[];
  affected_contracts: number[];
  credit_exposure_cents: number;
  recognized_service_revenue_cents: number;
  contribution_margin_cents: number;
  escalated_cases: number;
  repeat_contacts: number;
};

export type ServiceAssuranceExclusion = {
  category: string;
  subject: string;
  rule_id: string;
  reason: string;
};

export type RecordedServiceAssuranceDemo = {
  schema_version: "1.0";
  mode: "RECORDED_SERVICE_ASSURANCE";
  generated_at: string;
  profile: string;
  period: string;
  question: string;
  ontology: RecordedOntology;
  hops: ServiceAssuranceHop[];
  model_evidence: ServiceAssuranceModelEvidence[];
  ranked_accounts: ServiceAssuranceAccountRow[];
  exclusions: ServiceAssuranceExclusion[];
  limitations: string[];
};

const SERVICE_ASSURANCE_MODELS = ["network", "enterprise", "finance", "care"] as const;
const SERVICE_ASSURANCE_FOCUS_METRICS = [
  "network.major_incidents",
  "enterprise.credit_exposure",
  "finance.recognized_service_revenue",
  "finance.contribution_margin",
  "care.escalated_cases",
  "care.repeat_contacts",
];

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function zoned(value: unknown): value is string {
  return typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function nonEmptyStrings(value: unknown): value is string[] {
  return Array.isArray(value) && value.length > 0 && value.every(nonEmptyString);
}

function positiveInts(value: unknown): value is number[] {
  return Array.isArray(value) && value.length > 0
    && value.every(item => typeof item === "number" && Number.isSafeInteger(item) && item >= 1);
}

export function isRecordedServiceAssuranceDemo(value: unknown): value is RecordedServiceAssuranceDemo {
  if (!object(value) || value.schema_version !== "1.0" || value.mode !== "RECORDED_SERVICE_ASSURANCE"
    || !zoned(value.generated_at) || value.profile !== "service-assurance"
    || !nonEmptyString(value.period) || !nonEmptyString(value.question)
    || !isRecordedOntology(value.ontology) || !nonEmptyStrings(value.limitations)) return false;

  const ontology = value.ontology;
  if (ontology.sources.length !== 4
    || !SERVICE_ASSURANCE_MODELS.every(model => ontology.sources.includes(model))
    || ontology.entities.length !== 17 || ontology.relationships.length !== 17
    || ontology.metrics.length !== 14 || ontology.rules.length !== 5) return false;

  if (!Array.isArray(value.hops) || value.hops.length !== 7
    || !value.hops.every((hop, index) => object(hop) && hop.hop === index + 1
      && nonEmptyString(hop.from_entity) && nonEmptyString(hop.to_entity)
      && nonEmptyStrings(hop.join_keys) && nonEmptyString(hop.validity)
      && typeof hop.crosses_models === "boolean" && nonEmptyString(hop.models_label)
      && nonEmptyString(hop.summary))) return false;

  if (!Array.isArray(value.model_evidence) || value.model_evidence.length !== 4
    || !value.model_evidence.every(item => object(item)
      && SERVICE_ASSURANCE_MODELS.includes(item.model_id as typeof SERVICE_ASSURANCE_MODELS[number])
      && nonEmptyString(item.model_name) && nonEmptyStrings(item.home_tables)
      && nonEmptyStrings(item.measures) && nonEmptyString(item.grain)
      && nonEmptyString(item.summary) && typeof item.row_count === "number"
      && Number.isSafeInteger(item.row_count) && item.row_count >= 0)
    || new Set(value.model_evidence.map(item => item.model_id)).size !== 4) return false;

  if (!Array.isArray(value.ranked_accounts) || value.ranked_accounts.length < 1 || value.ranked_accounts.length > 250
    || !value.ranked_accounts.every(row => object(row)
      && typeof row.account_key === "number" && Number.isSafeInteger(row.account_key) && row.account_key >= 1
      && nonEmptyString(row.account_id) && nonEmptyString(row.display_name) && nonEmptyString(row.region)
      && positiveInts(row.affected_sites) && positiveInts(row.affected_contracts)
      && ["credit_exposure_cents", "recognized_service_revenue_cents", "contribution_margin_cents",
        "escalated_cases", "repeat_contacts"].every(key => typeof row[key] === "number" && Number.isSafeInteger(row[key]))
      && Number(row.escalated_cases) >= 0 && Number(row.repeat_contacts) >= 0)
    || new Set(value.ranked_accounts.map(row => row.account_id)).size !== value.ranked_accounts.length
    || new Set(value.ranked_accounts.map(row => row.account_key)).size !== value.ranked_accounts.length) return false;

  for (let index = 1; index < value.ranked_accounts.length; index++) {
    const prev = value.ranked_accounts[index - 1];
    const curr = value.ranked_accounts[index];
    if (curr.credit_exposure_cents > prev.credit_exposure_cents
      || (curr.credit_exposure_cents === prev.credit_exposure_cents
        && curr.contribution_margin_cents > prev.contribution_margin_cents)) {
      return false;
    }
  }

  if (!Array.isArray(value.exclusions) || value.exclusions.length < 1
    || !value.exclusions.every(item => object(item)
      && nonEmptyString(item.category) && nonEmptyString(item.subject)
      && nonEmptyString(item.rule_id) && nonEmptyString(item.reason))) return false;

  return true;
}

export function ServiceAssuranceDemoView({ recording }: { recording: RecordedServiceAssuranceDemo }) {
  const totalExposureCents = recording.ranked_accounts.reduce((sum, row) => sum + row.credit_exposure_cents, 0);
  return <main>
    <header>
      <div>
        <p className="eyebrow">NORTHSTAR WIRELESS / SERVICE ASSURANCE SCENARIO</p>
        <h1>Four models. Seven hops. One grounded outreach plan.</h1>
        <p>Connected path across Network, Enterprise Services, Finance, and Care for {recording.period} ({recording.profile} synthetic population, generated {recording.generated_at}).</p>
      </div>
      <span className="badge">SERVICE ASSURANCE SCENARIO · 4 MODELS</span>
    </header>

    <section className="card question-card" aria-label="Service assurance business question">
      <p className="eyebrow">CONNECTED BUSINESS QUESTION · {recording.period}</p>
      <h2>{recording.question}</h2>
      <div className="result-metrics">
        <div>
          <strong>{recording.ranked_accounts.length.toLocaleString("en-US")}</strong>
          <span>Complete affected enterprise accounts ranked for outreach</span>
        </div>
        <div>
          <strong>{totalExposureCents.toLocaleString("en-US")}</strong>
          <span>Known contract-month Credit Exposure · cents (incomplete contracts excluded)</span>
        </div>
        <div>
          <strong>{recording.exclusions.length.toLocaleString("en-US")} exclusions</strong>
          <span>Partial result (unknown mapping coverage and non-overlapping validity excluded)</span>
        </div>
      </div>
      <p className="muted">Selecting this stream displays saved offline synthetic evidence; it runs no sign-in, language model call, or live Microsoft Fabric query.</p>
    </section>

    <section className="card" aria-label="Path through the seven hops">
      <div className="section-heading">
        <div>
          <p className="eyebrow">SEVEN-HOP TRAVERSAL CONTRACT</p>
          <h2>Path through the seven hops</h2>
        </div>
        <span className="status-pill observed">7 HOPS · 3 MODEL BRIDGES</span>
      </div>
      <p>Two relationships enforce half-open UTC time-validity overlap before settling contract-month credits or looking up account-month financials.</p>
      <div className="table-scroll">
        <table aria-label="Seven-hop service assurance path">
          <thead>
            <tr>
              <th>Hop</th>
              <th>Source to target entity</th>
              <th>Join keys</th>
              <th>Time-validity check</th>
              <th>Model bridge</th>
              <th>Step meaning</th>
            </tr>
          </thead>
          <tbody>
            {recording.hops.map(hop => <tr key={hop.hop}>
              <td><strong>Hop {hop.hop}</strong></td>
              <td>{hop.from_entity} → {hop.to_entity}</td>
              <td><code>{hop.join_keys.join(", ")}</code></td>
              <td>{hop.validity}</td>
              <td>
                <span className={hop.crosses_models ? "status-pill observed" : "status-pill"}>
                  {hop.crosses_models ? `Bridge: ${hop.models_label}` : hop.models_label}
                </span>
              </td>
              <td>{hop.summary}</td>
            </tr>)}
          </tbody>
        </table>
      </div>
    </section>

    <RecordedOntologyView ontology={recording.ontology} focusMetricIds={SERVICE_ASSURANCE_FOCUS_METRICS} />

    <section className="card" aria-label="Evidence per semantic model">
      <div className="section-heading">
        <div>
          <p className="eyebrow">SOURCE MEASURE OWNERSHIP</p>
          <h2>Evidence per semantic model</h2>
        </div>
        <span className="status-pill">4 SEMANTIC MODELS · 14 MEASURES</span>
      </div>
      <p>Every metric links to its home Power BI semantic-model DAX measure. Calculations, table grains, and security roles stay in their owning semantic models.</p>
      <div className="source-models">
        {recording.model_evidence.map(model => <article key={model.model_id} className="source-model" data-model-id={model.model_id}>
          <p className="eyebrow">{model.model_id.toUpperCase()} SEMANTIC MODEL</p>
          <h3>{model.model_name}</h3>
          <p><strong>Grain:</strong> {model.grain}</p>
          <p><strong>Home tables ({model.row_count.toLocaleString("en-US")} rows):</strong></p>
          <div className="source-homes">
            {model.home_tables.map(table => <span key={table}>{table}</span>)}
          </div>
          <p><strong>Measures linked to source DAX:</strong></p>
          <div className="measure-chips">
            {model.measures.map(measure => <span key={measure} className="status-pill">{measure}</span>)}
          </div>
          <p>{model.summary}</p>
        </article>)}
      </div>
    </section>

    <section className="card" aria-label="Ranked enterprise accounts">
      <div className="section-heading">
        <div>
          <p className="eyebrow">JULY 2026 OUTREACH PRIORITY</p>
          <h2>Ranked enterprise accounts</h2>
        </div>
        <span className="status-pill">CREDIT EXPOSURE DESC · MARGIN DESC</span>
      </div>
      <p>Partial result (accounts with incomplete serving-site mapping coverage and non-overlapping validity records are excluded in the next section). Monetary columns report exact integer cents without multiplying account-month totals across contracts.</p>
      <div className="table-scroll">
        <table aria-label="Ranked affected enterprise accounts">
          <thead>
            <tr>
              <th>Account ID</th>
              <th>Display name</th>
              <th>Region</th>
              <th>Affected sites &amp; contracts</th>
              <th>Credit Exposure · cents</th>
              <th>Recognized Service Revenue · cents</th>
              <th>Contribution Margin · cents</th>
              <th>Escalated Cases</th>
              <th>Repeat Contacts</th>
            </tr>
          </thead>
          <tbody>
            {recording.ranked_accounts.map(row => <tr key={row.account_id}>
              <td><code>{row.account_id}</code></td>
              <td>{row.display_name}</td>
              <td>{row.region}</td>
              <td>
                Sites: {row.affected_sites.join(", ")} · Contracts: {row.affected_contracts.join(", ")}
              </td>
              <td>{row.credit_exposure_cents.toLocaleString("en-US")}</td>
              <td>{row.recognized_service_revenue_cents.toLocaleString("en-US")}</td>
              <td>{row.contribution_margin_cents.toLocaleString("en-US")}</td>
              <td>{row.escalated_cases.toLocaleString("en-US")}</td>
              <td>{row.repeat_contacts.toLocaleString("en-US")}</td>
            </tr>)}
          </tbody>
        </table>
      </div>
    </section>

    <section className="card" aria-label="Exclusions and validity boundaries">
      <div className="section-heading">
        <div>
          <p className="eyebrow">EXPLICIT QUALIFICATION</p>
          <h2>Exclusions and validity boundaries</h2>
        </div>
        <span className="status-pill">RULES R1 &amp; R5</span>
      </div>
      <p>Missing serving-site coverage remains unknown rather than zero (R5), and assignments or contracts outside the July outage interval are excluded by half-open UTC overlap checks (R1).</p>
      <div className="table-scroll">
        <table aria-label="Service assurance exclusions">
          <thead>
            <tr>
              <th>Rule</th>
              <th>Category</th>
              <th>Subject</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {recording.exclusions.map(item => <tr key={`${item.rule_id}:${item.subject}`}>
              <td><span className="status-pill observed">{item.rule_id}</span></td>
              <td><code>{item.category}</code></td>
              <td><code>{item.subject}</code></td>
              <td>{item.reason}</td>
            </tr>)}
          </tbody>
        </table>
      </div>
    </section>

    <footer>
      <p><strong>Limits of this recording</strong> (see <code>docs/limitations.md</code>):</p>
      {recording.limitations.map(item => <p key={item}>{item}</p>)}
    </footer>
  </main>;
}
