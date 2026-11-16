import type { AnswerView } from "./execution";

type ReviewCandidate = {
  account: string; activeLines: number; revenueCents: number; marginCents: number;
};
function integer(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value);
}
export function metricReviewCandidates(answer: AnswerView): ReviewCandidate[] {
  const qualified = answer.status === "PARTIAL" && answer.population_qualification === "known_mapped_subset"
    && Boolean(answer.exclusions?.length) && !answer.coverage.unknown_source_rows;
  if ((answer.status !== "COMPLETE" && !qualified) || answer.evidence_ids.length === 0) return [];
  const identities = answer.rows.map(row => row.account_id ?? row.account_key);
  if (identities.some(id => typeof id !== "string" && !integer(id))
    || new Set(identities).size !== identities.length) return [];
  return answer.rows.flatMap(row => {
    const activeLines = row["subscriptions.active_lines_eom"];
    const revenueCents = row["finance.recognized_service_revenue"];
    const marginCents = row["finance.contribution_margin"];
    if (!integer(activeLines) || !integer(revenueCents) || !integer(marginCents)
      || activeLines !== 0 || revenueCents <= 0 || marginCents >= 0) return [];
    return [{
      account: String(row.account_id ?? row.account_key), activeLines, revenueCents, marginCents,
    }];
  });
}
export function MetricInsight({ answer }: { answer: AnswerView }) {
  const candidates = metricReviewCandidates(answer);
  if (!candidates.length) return null;
  return <aside className="notice" aria-label="Cross-domain metric review opportunity">
    <strong>A cross-domain question to investigate</strong>
    <p>Which customers have recognized service revenue and negative contribution margin,
      but no active lines at month end?</p>
    {candidates.slice(0, 3).map(item => <p key={item.account}>
      <strong>{item.account}</strong>: {item.activeLines} month-end lines · {
        item.revenueCents.toLocaleString("en-US")} cents recognized revenue · {
        item.marginCents.toLocaleString("en-US")} cents contribution margin.
    </p>)}
    <p>Subscriptions supplies line status; Finance supplies revenue and margin.
      Shared customer identity and period meaning let an agent bring them into one review.</p>
    <p className="muted">Application-owned review criterion over original source values, not a new measure,
      Rule enforcement in Fabric or proof of a billing error. Investigate credit, cost and timing context;
      no cause, complete-population total or automatic action is inferred.</p>
  </aside>;
}
