export type RecordedSource = { id: string; name: string };
export type RecordedEntity = {
  id: string; source_id: string; backing_table: string; description: string;
  keys: string[]; properties: string[]; guidance: string[];
};
export type RecordedRelationship = { id: string; source: string; target: string; guidance: string[] };
export type RecordedMetric = {
  id: string; entity_id: string; source_id: string; home_table: string; name: string; description: string;
};
export type RecordedDemo = {
  schema_version: "1.0"; mode: "RECORDED_NATIVE_DEMO"; captured_at: string; context_captured_at: string;
  created_at?: string | null;
  question: string; sources: RecordedSource[]; entities: RecordedEntity[];
  relationships: RecordedRelationship[]; metrics: RecordedMetric[]; native_rule_count: number;
  focus_entity_ids: string[]; queries: { source_id: string; dax: string }[];
  rows: { account_key: number; active_lines_eom: number; recognized_service_revenue_cents: number }[];
  source_hashes: Record<string, string>; limitations: string[];
};

export function definitionView(recording: RecordedDemo, focus: boolean) {
  const ids = new Set(focus ? recording.focus_entity_ids : recording.entities.map(entity => entity.id));
  return {
    entities: recording.entities.filter(entity => ids.has(entity.id)),
    relationships: recording.relationships.filter(edge => ids.has(edge.source) && ids.has(edge.target)),
  };
}

export function recordedTotals(recording: RecordedDemo) {
  return recording.rows.reduce((total, row) => ({
    lines: total.lines + BigInt(row.active_lines_eom),
    cents: total.cents + BigInt(row.recognized_service_revenue_cents),
  }), { lines: 0n, cents: 0n });
}

export function sourceLabel(id: string) {
  return id.charAt(0).toUpperCase() + id.slice(1);
}

export type DefinitionLens = "connected" | "sources";

export function sourceInventory(recording: RecordedDemo) {
  return recording.sources.map(source => ({
    ...source,
    entities: recording.entities.filter(entity => entity.source_id === source.id),
    metrics: recording.metrics.filter(metric => metric.source_id === source.id),
  }));
}

function daxCode(dax: string): string {
  let code = "";
  for (let index = 0; index < dax.length;) {
    const char = dax[index];
    if (dax.startsWith("//", index) || dax.startsWith("--", index)) {
      while (index < dax.length && dax[index] !== "\n") index++;
      code += " ";
    } else if (dax.startsWith("/*", index)) {
      let depth = 1; index += 2;
      while (index < dax.length && depth) {
        if (dax.startsWith("/*", index)) { depth++; index += 2; }
        else if (dax.startsWith("*/", index)) { depth--; index += 2; }
        else index++;
      }
      code += " ";
    } else if (char === '"' || char === "'") {
      index++;
      while (index < dax.length) {
        if (dax[index] !== char) index++;
        else if (dax[index + 1] === char) index += 2;
        else { index++; break; }
      }
      code += " ";
    } else if (char === "[") {
      const start = index++;
      while (index < dax.length) {
        if (dax[index] !== "]") index++;
        else if (dax[index + 1] === "]") index += 2;
        else { index++; break; }
      }
      code += dax.slice(start, index);
    } else {
      code += char; index++;
    }
  }
  return code;
}

export function metricEvidence(recording: RecordedDemo, metric: RecordedMetric) {
  const reference = `[${metric.name.replaceAll("]", "]]")}]`;
  return {
    source: recording.sources.find(source => source.id === metric.source_id),
    entity: recording.entities.find(entity => entity.id === metric.entity_id),
    queries: recording.queries.filter(query => query.source_id === metric.source_id && daxCode(query.dax).includes(reference)),
  };
}

/** Only the two result columns in the captured DTO have an observed metric binding. */
export function recordedResultColumn(metric: RecordedMetric) {
  if (metric.source_id === "subscriptions" && metric.home_table === "Subscription Month"
    && metric.name === "Active Lines EOM") return "active_lines_eom";
  if (metric.source_id === "finance" && metric.home_table === "Account Month"
    && metric.name === "Recognized Service Revenue") return "recognized_service_revenue_cents";
  return null;
}

export function recordedPeriod(recording: RecordedDemo): string {
  // Read only an explicit, common half-open DATE window from every captured query.
  const periods = recording.queries.map(query => {
    const code = daxCode(query.dax);
    const start = code.match(/>=\s*DATE\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*1\s*\)/i);
    const end = code.match(/<\s*DATE\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*1\s*\)/i);
    if (!start || !end) return null;
    const year = Number(start[1]); const month = Number(start[2]);
    if (month < 1 || month > 12 || Number(end[1]) !== year + (month === 12 ? 1 : 0)
      || Number(end[2]) !== month % 12 + 1) return null;
    return `${start[1]}-${String(month).padStart(2, "0")}`;
  });
  if (!periods.length || periods.some(period => period === null || period !== periods[0])) return "Not established by captured queries";
  const [year, month] = periods[0]!.split("-").map(Number);
  return new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric", timeZone: "UTC" })
    .format(new Date(Date.UTC(year, month - 1, 1)));
}

export function definitionLayout(recording: RecordedDemo, focus: boolean, lens: DefinitionLens) {
  const view = definitionView(recording, focus);
  const positions = new Map<string, { x: number; y: number }>();
  if (lens === "sources") {
    const sources = sourceInventory(recording);
    for (const [sourceIndex, source] of sources.entries()) {
      const entities = view.entities.filter(entity => entity.source_id === source.id);
      entities.forEach((entity, index) => positions.set(entity.id, {
        x: sourceIndex * 560 + index % 2 * 260, y: Math.floor(index / 2) * 148,
      }));
    }
  } else {
    const anchors = recording.focus_entity_ids.filter(id => view.entities.some(entity => entity.id === id));
    if (!anchors.length && view.entities[0]) anchors.push(view.entities[0].id);
    const columnsByEntity = new Map(anchors.map((id, index) => [id, index]));
    const queue = [...anchors];
    for (let index = 0; index < queue.length; index++) {
      const id = queue[index];
      for (const edge of view.relationships) {
        const next = edge.source === id ? edge.target : edge.target === id ? edge.source : null;
        if (next && !columnsByEntity.has(next)) {
          columnsByEntity.set(next, columnsByEntity.get(id) ?? 0); queue.push(next);
        }
      }
    }
    const columns = new Map<number, RecordedEntity[]>();
    const ordered = [...anchors.flatMap(id => view.entities.filter(entity => entity.id === id)),
      ...view.entities.filter(entity => !anchors.includes(entity.id))];
    for (const entity of ordered) {
      const column = columnsByEntity.get(entity.id) ?? anchors.length;
      columns.set(column, [...columns.get(column) ?? [], entity]);
    }
    for (const [level, column] of columns) {
      column.forEach((entity, index) => positions.set(entity.id, {
        x: level * 290, y: index * 148,
      }));
    }
  }
  return { ...view, positions };
}
