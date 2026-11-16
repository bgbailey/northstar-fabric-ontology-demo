from __future__ import annotations

import asyncio
import json
import math
from collections import defaultdict
from datetime import datetime
from time import perf_counter

from telco_iq.catalog import Catalog, compatible_populations
from telco_iq.contracts import (
    AggregateNode,
    AnswerNode,
    Artifact,
    CohortExclusion,
    CohortFilter,
    Context,
    ContextNode,
    ExecutionResult,
    Filter,
    IntervalLineage,
    JoinNode,
    Node,
    Plan,
    ProjectColumns,
    QueryNode,
    RankNode,
    Relationship,
    ResultSchema,
    ReviewedIntervalOrigin,
    RuleNode,
    Scope,
    SourceClock,
    SourceColumn,
    SourcePopulation,
    Status,
)
from telco_iq.errors import LabError
from telco_iq.events import EventSink, ExecutionEvent
from telco_iq.providers import OfflineProvider, QueryProvider


def instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise LabError("Temporal values must be ISO timestamps")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise LabError("Invalid temporal timestamp") from error
    if parsed.tzinfo is None:
        raise LabError("Temporal joins require timezone-aware instants")
    return parsed


def number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LabError("Expected finite numeric operand; missing is not zero")
    return value


def dependencies(node: Node) -> set[str]:
    refs = set(node.depends_on)
    if isinstance(node, JoinNode):
        refs.update([node.left, node.right])
    elif isinstance(node, QueryNode):
        refs.update(f.source for f in node.cohort_filters)
    elif isinstance(node, (ProjectColumns, AggregateNode, RuleNode, RankNode, AnswerNode)):
        refs.add(node.source)
        if isinstance(node, AnswerNode):
            refs.update(node.additional_sources.values())
    return refs


def plan_order(plan: Plan) -> list:
    nodes = {node.id: node for node in plan.nodes}
    if len(nodes) != len(plan.nodes) or plan.result_node not in nodes:
        raise LabError("Duplicate node IDs or absent result node")
    for node in plan.nodes:
        missing = dependencies(node) - nodes.keys()
        if missing:
            raise LabError(f"Missing dependency: node={node.id}, undefined={','.join(sorted(missing))}")
    order, visiting, visited = [], set(), set()

    def visit(key: str) -> None:
        if key in visiting:
            raise LabError("Plan has a dependency cycle")
        if key in visited:
            return
        visiting.add(key)
        for parent in sorted(dependencies(nodes[key])):
            visit(parent)
        visiting.remove(key)
        visited.add(key)
        order.append(nodes[key])

    visit(plan.result_node)
    if len(visited) != len(nodes):
        raise LabError("Plan contains unused nodes; refuse hidden/unnecessary source queries")
    return order


def unique(items: list) -> list:
    return list({item.model_dump_json(): item for item in items}.values())


def schema_values(schema: ResultSchema) -> dict:
    return {name: getattr(schema, name) for name in ResultSchema.model_fields}


def reviewed(context: Context, name: str) -> Relationship:
    matches = [r for r in context.relationships if r.id == name]
    if len(matches) != 1:
        raise LabError("Join/cohort lacks an unambiguous approved identity relationship")
    return matches[0]


def require_identity(
    schema: ResultSchema, alias: str, model: str, table: str, column: str, *, allow_derived: bool = False,
) -> None:
    if alias not in schema.lineage:
        raise LabError("Join/cohort key or interval is not projected; missing columns are not unmapped identities")
    if SourceColumn(model=model, table=table, column=column) not in schema.lineage[alias]:
        raise LabError("Relationship key/interval lineage does not bind the reviewed source identity")
    if not allow_derived and alias in schema.derived_columns:
        raise LabError("Derived value lineage is not an unchanged source-column identity")


def interval_origin_key_aliases(schema: ResultSchema, origin: ReviewedIntervalOrigin) -> list[str]:
    aliases = []
    for key in origin.keys:
        identity = SourceColumn(model=origin.model, table=origin.table, column=key)
        matches = [alias for alias, lineage in schema.lineage.items()
                   if identity in lineage and alias not in schema.derived_columns]
        if len(matches) != 1:
            raise LabError("Carried interval source identity key is missing or ambiguous")
        aliases.append(matches[0])
    return aliases


def aligned_periods(left: ResultSchema, right: ResultSchema, relationship: Relationship) -> None:
    for column in relationship.aligned_filters:
        periods = []
        for schema, model, table in (
            (left, relationship.left_model, relationship.left_table),
            (right, relationship.right_model, relationship.right_table),
        ):
            values = {
                json.dumps(f.values, allow_nan=False) for f in schema.source_filters
                if f.model == model and f.table == table and f.column == column
                and f.op in ("eq", "in") and len(f.values) == 1 and f.values[0] is not None
            }
            periods.append(values)
        if len(periods[0]) != 1 or periods[0] != periods[1]:
            raise LabError("Join/cohort periods differ or are not explicitly single-period aligned")


def cohort_identity(
    binding: CohortFilter, source: ResultSchema, target: ResultSchema, node: QueryNode, context: Context,
) -> None:
    if binding.relationship is None:
        require_identity(source, binding.source_column, node.model, node.table, binding.target_column)
        return
    contract = reviewed(context, binding.relationship)
    if (
        len(contract.left_keys) != 1 or contract.right_keys != [binding.target_column]
        or (contract.right_model, contract.right_table) != (node.model, node.table)
    ):
        raise LabError("Cohort relationship does not bind the target single-column identity")
    require_identity(
        source, binding.source_column, contract.left_model, contract.left_table, contract.left_keys[0],
    )
    if contract.temporal:
        raise LabError("Temporal cohort identity requires an explicit interval join before transfer")
    aligned_periods(source, target, contract)


def project_schema(node: ProjectColumns, source: ResultSchema) -> ResultSchema:
    if not set(node.columns.values()) <= source.lineage.keys() or any(not c for c in node.columns):
        raise LabError("Projection references missing columns or empty aliases")
    aliases = {column: alias for alias, column in reversed(list(node.columns.items()))}
    grain = None
    if source.grain is not None and set(source.grain) <= aliases.keys():
        grain = [aliases[c] for c in source.grain]
    return source.model_copy(update={
        "lineage": {alias: source.lineage[column] for alias, column in node.columns.items()},
        "aggregation": {
            alias: source.aggregation[column] for alias, column in node.columns.items()
            if column in source.aggregation
        },
        "grain": grain,
        "derived_columns": [
            alias for alias, column in node.columns.items() if column in source.derived_columns
        ],
    })


def join_schema(node: JoinNode, left: ResultSchema, right: ResultSchema, context: Context) -> ResultSchema:
    contract = reviewed(context, node.relationship)
    if (
        node.cardinality != contract.cardinality
        or len(node.left_keys) != len(contract.left_keys)
        or len(node.right_keys) != len(contract.right_keys)
        or len(node.left_keys) != len(node.right_keys)
        or bool(node.temporal) != bool(contract.temporal)
    ):
        raise LabError(
            f"Join differs from reviewed key/cardinality/temporal contract: node={node.id}, "
            f"relationship={contract.id}, cardinality={node.cardinality}/{contract.cardinality}, "
            f"key_counts={len(node.left_keys)}:{len(node.right_keys)}/"
            f"{len(contract.left_keys)}:{len(contract.right_keys)}, "
            f"temporal={bool(node.temporal)}/{bool(contract.temporal)}"
        )
    for schema, aliases, keys, model, table in (
        (left, node.left_keys, contract.left_keys, contract.left_model, contract.left_table),
        (right, node.right_keys, contract.right_keys, contract.right_model, contract.right_table),
    ):
        for alias, column in zip(aliases, keys, strict=True):
            require_identity(schema, alias, model, table, column)
    aligned_periods(left, right, contract)
    interval = None
    if node.temporal and contract.temporal:
        for side, schema, model, table, start, end, expected_start, expected_end in (
            ("left", left, contract.left_model, contract.left_table, node.temporal.left_start,
             node.temporal.left_end, contract.temporal.left_start, contract.temporal.left_end),
            ("right", right, contract.right_model, contract.right_table, node.temporal.right_start,
             node.temporal.right_end, contract.temporal.right_start, contract.temporal.right_end),
        ):
            origin = getattr(contract, f"{side}_interval_origin")
            if origin is not None:
                if not set(origin.via_relationships) <= set(schema.relationship_history):
                    raise LabError("Carried interval requires its reviewed native identity traversals")
                for relationship_id in origin.via_relationships:
                    reviewed(context, relationship_id)
                for key in origin.grain:
                    identity = SourceColumn(model=origin.model, table=origin.table, column=key)
                    if not any(
                        identity in schema.lineage.get(alias, []) and alias not in schema.derived_columns
                        for alias in schema.grain or []
                    ):
                        raise LabError("Carried interval lost its unchanged source primary-key grain")
                if len(interval_origin_key_aliases(schema, origin)) != len(getattr(node, f"{side}_keys")):
                    raise LabError("Carried interval identity keys differ from the native endpoint")
                model, table = origin.model, origin.table
            require_identity(schema, start, model, table, expected_start, allow_derived=True)
            require_identity(schema, end, model, table, expected_end, allow_derived=True)
            for constraint in schema.intervals:
                if not (
                    all(c in schema.lineage[start] for c in constraint.starts)
                    and all(c in schema.lineage[end] for c in constraint.ends)
                ):
                    raise LabError("Chained temporal joins must use clipped intersection bounds")
        interval = IntervalLineage(
            starts=unique(left.lineage[node.temporal.left_start] + right.lineage[node.temporal.right_start]),
            ends=unique(left.lineage[node.temporal.left_end] + right.lineage[node.temporal.right_end]),
        )
    if node.cardinality == "many_to_many" and (
        (not node.temporal and node.kind != "SemiJoinCohort")
        or any(v != "raw" for v in [*left.aggregation.values(), *right.aggregation.values()])
    ):
        raise LabError("Many-to-many is restricted to reviewed RAW temporal/cohort joins")
    if node.kind == "SemiJoinCohort":
        return left.model_copy(update={
            "source_filters": unique(left.source_filters + right.source_filters),
            "relationship_history": list(dict.fromkeys(left.relationship_history + right.relationship_history + [contract.id])),
        })
    lineage = dict(left.lineage)
    aggregation = dict(left.aggregation)
    for column, origins in right.lineage.items():
        if column in lineage:
            if not any(
                left_key == right_key == column
                for left_key, right_key in zip(node.left_keys, node.right_keys, strict=True)
            ):
                raise LabError(f"Ambiguous join output column: {column}; use explicit projections")
            # Preserved shared keys come from the left even without a qualified right match.
            # A distinct right alias is required to expose nullable matched-right identity.
            if node.unmatched != "preserve":
                lineage[column] = unique(lineage[column] + origins)
        else:
            lineage[column] = origins
    for schema, repeated in (
        (left, node.cardinality in ("one_to_many", "many_to_many")),
        (right, node.cardinality in ("many_to_one", "many_to_many")),
    ):
        for column, kind in schema.aggregation.items():
            aggregation[column] = "non_additive" if repeated and kind != "raw" else kind
    intervals = unique(left.intervals + right.intervals)
    derived = list(dict.fromkeys(left.derived_columns + right.derived_columns))
    if interval:
        intervals = [interval]
        if node.overlap_start is not None:
            if node.overlap_start in lineage or node.overlap_end in lineage:
                raise LabError("Intersection outputs cannot overwrite existing columns")
            lineage[node.overlap_start] = interval.starts
            lineage[node.overlap_end] = interval.ends
            aggregation[node.overlap_start] = aggregation[node.overlap_end] = "raw"
            derived.extend([node.overlap_start, node.overlap_end])
    grain = left.grain
    if node.cardinality in ("one_to_many", "many_to_many"):
        grain = (
            list(dict.fromkeys(left.grain + right.grain))
            if left.grain is not None and right.grain is not None else None
        )
    return ResultSchema(
        lineage=lineage, aggregation=aggregation, grain=grain,
        derived_columns=derived,
        source_filters=unique(left.source_filters + right.source_filters), intervals=intervals,
        relationship_history=list(dict.fromkeys(left.relationship_history + right.relationship_history + [contract.id])),
    )


def aggregate_schema(node: AggregateNode, source: ResultSchema) -> ResultSchema:
    columns = node.group_by + node.sums
    if len(columns) != len(set(columns)) or not set(columns) <= source.lineage.keys():
        raise LabError("Aggregate references missing/duplicate columns")
    if any(source.aggregation.get(c) != "additive" for c in node.sums):
        raise LabError("Non-additive/raw/snapshot values must be queried at final context, not summed")
    return source.model_copy(update={
        "grain": list(node.group_by),
        "lineage": {c: source.lineage[c] for c in columns},
        "aggregation": {c: source.aggregation[c] for c in columns if c in source.aggregation},
        "derived_columns": list(dict.fromkeys(
            [c for c in columns if c in source.derived_columns] + node.sums,
        )),
    })


def rule_schema(node: RuleNode, source: ResultSchema) -> ResultSchema:
    if not set(node.arguments + node.group_by) <= source.lineage.keys():
        raise LabError("Rule references missing columns")
    if node.output in source.lineage:
        raise LabError("Rule cannot overwrite a source column")
    columns = node.group_by if node.operation == "union_minutes" else list(source.lineage)
    return source.model_copy(update={
        "lineage": {c: source.lineage[c] for c in columns} | {node.output: []},
        "aggregation": (
            {c: source.aggregation[c] for c in columns if c in source.aggregation}
            | {node.output: "non_additive"}
        ),
        "grain": node.group_by if node.operation == "union_minutes" else source.grain,
        "derived_columns": [c for c in columns if c in source.derived_columns] + [node.output],
    })


def node_schema(node: Node, schemas: dict[str, ResultSchema], catalog: Catalog, context: Context) -> ResultSchema:
    if isinstance(node, QueryNode):
        result = catalog.query_schema(node)
        for binding in node.cohort_filters:
            cohort_identity(binding, schemas[binding.source], result, node, context)
        return result
    if isinstance(node, ContextNode):
        return ResultSchema(grain=[], aggregation={})
    if isinstance(node, ProjectColumns):
        return project_schema(node, schemas[node.source])
    if isinstance(node, JoinNode):
        return join_schema(node, schemas[node.left], schemas[node.right], context)
    if isinstance(node, AggregateNode):
        return aggregate_schema(node, schemas[node.source])
    if isinstance(node, RuleNode):
        return rule_schema(node, schemas[node.source])
    if isinstance(node, (RankNode, AnswerNode)):
        if isinstance(node, RankNode) and node.column not in schemas[node.source].lineage:
            raise LabError("Ranking references a missing column")
        return schemas[node.source]
    raise LabError("Unsupported plan node schema")


def validate(plan: Plan, scope: Scope, catalog: Catalog, context: Context) -> list:
    if plan.principal_scope != scope or context.scope != scope:
        raise LabError("Plan/context scope differs from authenticated server scope", Status.BLOCKED_AUTH)
    if plan.ontology_revision != context.revision:
        raise LabError("Context revision changed; replan", Status.BLOCKED_CAPABILITY)
    if plan.clarification_state != "resolved":
        raise LabError("Question needs explicit clarification", Status.AMBIGUOUS)
    ids = [n.id for n in plan.nodes]
    if len(set(ids)) != len(ids) or plan.result_node not in ids:
        raise LabError("Duplicate node IDs or absent result node")
    selected = set(plan.metric_selections)
    if not selected <= catalog.metrics.keys():
        raise LabError("Unknown metric selection")
    if any(catalog.metrics[m]["model"] not in scope.models for m in selected):
        raise LabError("Metric metadata is outside current source scope", Status.BLOCKED_AUTH)
    nodes = {n.id: n for n in plan.nodes}
    if isinstance(nodes[plan.result_node], ContextNode):
        raise LabError("Final node must produce a data artifact")
    for node in plan.nodes:
        missing = dependencies(node) - nodes.keys()
        if missing:
            raise LabError(f"Missing dependency: node={node.id}, undefined={','.join(sorted(missing))}")
        if isinstance(node, QueryNode):
            if node.model not in scope.models:
                raise LabError("Source denied", Status.BLOCKED_AUTH)
            if not set(node.metrics) <= selected:
                raise LabError("Query uses unselected measures")
            if node.max_rows > plan.budgets.max_rows:
                raise LabError("Query row limit exceeds plan budget")
            catalog.validate_query(node)
        if isinstance(node, ContextNode) and not set(node.references) <= (
            catalog.models.keys() | catalog.metrics.keys() | catalog.tables.keys()
        ):
            raise LabError("Unresolved context reference")
        if isinstance(node, AnswerNode) and any(isinstance(nodes[reference], ContextNode)
                                                for reference in node.additional_sources.values()):
            raise LabError("Separate answer views require actual data artifacts, not context nodes")
        if isinstance(node, AnswerNode) and node.additional_sources and node.id != plan.result_node:
            raise LabError("Separate result views belong to the terminal answer")
    order = plan_order(plan)
    if sum(isinstance(n, QueryNode) for n in order) > plan.budgets.max_queries:
        raise LabError("Query budget exceeded")
    schemas: dict[str, ResultSchema] = {}
    for node in order:
        schemas[node.id] = node_schema(node, schemas, catalog, context)
        relationships = (
            [node.relationship] if isinstance(node, JoinNode)
            else [f.relationship for f in node.cohort_filters if f.relationship]
            if isinstance(node, QueryNode) else []
        )
        for name in relationships:
            contract = reviewed(context, name)
            for model, table, columns in (
                (contract.left_model, contract.left_table, contract.left_keys),
                (contract.right_model, contract.right_table, contract.right_keys),
            ):
                if (
                    model not in catalog.models or table not in catalog.models[model]["tables"]
                    or not columns or not set(columns) <= catalog.tables[table]["columns"].keys()
                ):
                    raise LabError("Reviewed relationship has invalid source/target keys")
    return order


def known_mapped_subset(artifact: Artifact) -> bool:
    return (
        artifact.status == Status.PARTIAL and artifact.population_qualification == "known_mapped_subset"
        and bool(artifact.exclusions) and not artifact.coverage.get("unknown_source_rows", 0)
    )


def complete(artifact: Artifact, *, allow_known_mapped=False) -> None:
    if allow_known_mapped and known_mapped_subset(artifact):
        return
    if artifact.status != Status.COMPLETE:
        raise LabError("Incomplete dependency cannot support a population total", artifact.status)


def checked_rows(artifact: Artifact) -> None:
    if artifact.grain is None:
        return
    if not artifact.grain:
        if len(artifact.rows) > 1:
            raise LabError("Scalar grain returned multiple rows")
        return
    if any(not set(artifact.grain) <= r.keys() for r in artifact.rows):
        raise LabError("Source omitted declared grain columns")
    keys = [tuple(r[c] for c in artifact.grain) for r in artifact.rows]
    if len(keys) != len(set(keys)):
        raise LabError("Source result violates declared unique grain")


def artifact_schema(artifact: Artifact) -> ResultSchema:
    if artifact.lineage:
        return ResultSchema.model_validate(artifact.model_dump(include=set(ResultSchema.model_fields)))
    if len(artifact.source_models) != 1 or len(artifact.source_tables) != 1:
        raise LabError("Legacy multi-source artifact lacks explicit column lineage")
    columns = {c for row in artifact.rows for c in row} | artifact.aggregation.keys()
    return ResultSchema(
        lineage={
            c: [SourceColumn(model=artifact.source_models[0], table=artifact.source_tables[0], column=c)]
            for c in columns
        },
        grain=artifact.grain, aggregation={c: artifact.aggregation.get(c, "raw") for c in columns},
        source_filters=artifact.source_filters, intervals=artifact.intervals,
    )


def compatible_clocks(left: Artifact, right: Artifact) -> list[SourceClock]:
    if (left.clocks.context, left.clocks.ontology) != (right.clocks.context, right.clocks.ontology):
        raise LabError("Global context/ontology clocks differ; replan")
    for artifact in (left, right):
        if artifact.source_clocks and (
            {s.model for s in artifact.source_clocks} != set(artifact.source_models)
            or {s.table for s in artifact.source_clocks} != set(artifact.source_tables)
        ):
            raise LabError("Per-source clocks do not cover the artifact provenance")
    if not left.source_clocks or not right.source_clocks:
        if left.clocks != right.clocks:
            raise LabError("Source state changed or legacy clocks differ; align and requery")
        return []
    states = unique(left.source_clocks + right.source_clocks)
    for i, a in enumerate(states):
        for b in states[i + 1:]:
            if (
                (a.model == b.model and (a.model_revision, a.data) != (b.model_revision, b.data))
                or (a.table == b.table and (a.table_data or a.data) != (b.table_data or b.data))
            ):
                raise LabError("Shared source clocks changed; align and requery")
    return states


def population_evidence(artifact: Artifact) -> list[SourcePopulation]:
    entries = artifact.source_populations
    if (
        {entry.model for entry in entries} != set(artifact.source_models)
        or len(entries) != len(artifact.source_models)
        or not set(artifact.source_tables) <= {table for entry in entries for table in entry.table_hashes}
        or (artifact.scope.mode == "LIVE_DELEGATED" and any(
            entry.evidence_kind != "REVIEWED_LIVE" for entry in entries
        ))
    ):
        raise LabError("Incomplete source population provenance; UNKNOWN", Status.BLOCKED_CAPABILITY)
    return entries


class BoundedRows:
    def __init__(self, max_rows: int, max_bytes: int):
        self.rows = []
        self.max_rows, self.max_bytes, self.bytes = max_rows, max_bytes, 2
        if max_rows < 0 or max_bytes < 2:
            raise LabError("Cumulative intermediate budget exceeded", Status.PARTIAL)

    def append(self, row: dict) -> None:
        size = len(json.dumps(row, allow_nan=False).encode()) + (2 if self.rows else 0)
        if len(self.rows) >= self.max_rows or self.bytes + size > self.max_bytes:
            raise LabError("Cumulative intermediate budget exceeded while materializing", Status.PARTIAL)
        self.bytes += size
        self.rows.append(row)


def join(
    node: JoinNode, left: Artifact, right: Artifact, context: Context, *,
    max_rows: int = 10_000, max_bytes: int = 8_000_000,
) -> Artifact:
    complete(left, allow_known_mapped=True)
    complete(right, allow_known_mapped=True)
    if left.scope != right.scope:
        raise LabError("Cross-principal join refused", Status.BLOCKED_AUTH)
    populations = compatible_populations(population_evidence(left), population_evidence(right))
    source_clocks = compatible_clocks(left, right)
    if node.cardinality == "many_to_many" and (not left.lineage or not right.lineage):
        raise LabError("Many-to-many requires explicit RAW source lineage")
    schema = join_schema(node, artifact_schema(left), artifact_schema(right), context)
    contract = reviewed(context, node.relationship)
    enriched_identity = (
        node.kind in ("JoinResults", "ResolveIdentity") and node.cardinality == "one_to_one" and node.temporal is None
        and bool(left.lineage) and bool(left.relationship_history) and bool(left.grain)
        and not set(left.grain) <= set(node.left_keys)
    )
    if enriched_identity:
        checked_rows(left)
    for side, artifact in (("left", left), ("right", right)):
        origin = getattr(contract, f"{side}_interval_origin")
        if origin is not None:
            aliases = interval_origin_key_aliases(artifact_schema(artifact), origin)
            keys = getattr(node, f"{side}_keys")
            if any(
                row.get(alias) is None or row.get(key) is None or row[alias] != row[key]
                for row in artifact.rows for alias, key in zip(aliases, keys, strict=True)
            ):
                raise LabError("Carried interval source key does not match its native endpoint identity")
    if any(not set(node.left_keys) <= row.keys() for row in left.rows) or any(
        not set(node.right_keys) <= row.keys() for row in right.rows
    ):
        raise LabError("Source omitted required join columns; not an unknown identity")
    index = defaultdict(list)
    for j, row in enumerate(right.rows):
        bounds = None
        if node.temporal:
            bounds = (instant(row.get(node.temporal.right_start)), instant(row.get(node.temporal.right_end)))
            if bounds[1] <= bounds[0]:
                raise LabError("Invalid right half-open validity interval")
        key = tuple(row.get(k) for k in node.right_keys)
        if None not in key:
            index[key].append((j, row, bounds))
    output = BoundedRows(max_rows, max_bytes)
    unmatched, right_matches = 0, defaultdict(int)
    unknown = left.coverage.get("unknown_keys", 0) + right.coverage.get("unknown_keys", 0)
    for row in left.rows:
        key = tuple(row.get(k) for k in node.left_keys)
        candidates = [] if None in key else index.get(key, [])
        if None in key or not candidates:
            unknown += 1
        if node.temporal:
            t = node.temporal
            ls, le = instant(row.get(t.left_start)), instant(row.get(t.left_end))
            if le <= ls:
                raise LabError("Invalid left half-open validity interval")
        matches = 0
        for j, r, bounds in candidates:
            if bounds and not (ls < bounds[1] and bounds[0] < le):
                continue
            matches += 1
            if matches > node.max_fanout or (
                node.cardinality in ("one_to_one", "many_to_one") and matches > 1
            ):
                raise LabError("Join fan-out/cardinality violation")
            right_matches[j] += 1
            if node.cardinality in ("one_to_one", "one_to_many") and right_matches[j] > 1 and not enriched_identity:
                raise LabError("Right row reused across forbidden cardinality")
            if node.cardinality == "many_to_many" and right_matches[j] > node.max_fanout:
                raise LabError("Join reverse fan-out budget exceeded")
            if node.kind != "SemiJoinCohort":
                merged = dict(row)
                for col, value in r.items():
                    if col in merged:
                        if col in node.left_keys and col in node.right_keys and merged[col] == value:
                            continue
                        raise LabError(f"Ambiguous join output column: {col}; use explicit projections")
                    merged[col] = value
                if node.overlap_start is not None and bounds:
                    merged[node.overlap_start] = max(ls, bounds[0]).isoformat()
                    merged[node.overlap_end] = min(le, bounds[1]).isoformat()
                output.append(merged)
        if not matches:
            unmatched += 1
            if node.unmatched == "error":
                raise LabError("Identity mapping incomplete")
            if node.unmatched == "preserve":
                preserved = dict(row)
                for column in schema.lineage.keys() - preserved.keys():
                    preserved[column] = None
                output.append(preserved)
        elif node.kind == "SemiJoinCohort":
            output.append(dict(row))
    exclusions = unique(left.exclusions + right.exclusions)
    new_unknown = unknown - left.coverage.get("unknown_keys", 0) - right.coverage.get("unknown_keys", 0)
    if (known_mapped_subset(left) or known_mapped_subset(right)) and new_unknown:
        exclusions = unique(exclusions + [CohortExclusion(
            source_node=node.id, source_column=",".join(node.left_keys),
            unknown_mapping_observations=new_unknown,
            null_key_rows=sum(any(row.get(key) is None for key in node.left_keys) for row in left.rows),
            evidence_ids=list(dict.fromkeys(left.evidence_ids + right.evidence_ids)),
        )])
    return left.model_copy(update={
        **schema_values(schema), "id": node.id, "rows": output.rows,
        "status": Status.PARTIAL if Status.PARTIAL in (left.status, right.status) else Status.COMPLETE,
        "population_qualification": (
            "known_mapped_subset" if known_mapped_subset(left) or known_mapped_subset(right) else "unqualified"
        ),
        "exclusions": exclusions,
        "source_models": sorted(set(left.source_models + right.source_models)),
        "source_tables": sorted(set(left.source_tables + right.source_tables)),
        "source_clocks": source_clocks,
        "source_populations": populations,
        "evidence_ids": list(dict.fromkeys(left.evidence_ids + right.evidence_ids)),
        "warnings": left.warnings + right.warnings,
        "coverage": {
            "left_rows": len(left.rows), "matched": len(left.rows) - unmatched,
            "unmapped": unmatched, "unknown_keys": unknown,
        },
    })


def project(node: ProjectColumns, source: Artifact) -> Artifact:
    schema = project_schema(node, artifact_schema(source))
    if any(not set(node.columns.values()) <= row.keys() for row in source.rows):
        raise LabError("Source omitted projected columns")
    return source.model_copy(update={
        **schema_values(schema), "id": node.id,
        "rows": [{alias: row[column] for alias, column in node.columns.items()} for row in source.rows],
    })


def aggregate(node: AggregateNode, source: Artifact) -> Artifact:
    complete(source)
    if source.coverage.get("unknown_keys", 0):
        raise LabError("Unknown identity coverage cannot support a complete aggregate", Status.PARTIAL)
    schema = aggregate_schema(node, artifact_schema(source))
    groups = {}
    for row in source.rows:
        key = tuple(row[c] for c in node.group_by)
        target = groups.setdefault(key, dict(zip(node.group_by, key, strict=True)) | {c: 0 for c in node.sums})
        for c in node.sums:
            target[c] += number(row[c])
    return source.model_copy(update={**schema_values(schema), "id": node.id, "rows": list(groups.values())})


def rule(node: RuleNode, source: Artifact) -> Artifact:
    complete(source)
    schema = rule_schema(node, artifact_schema(source))
    rows = []
    if node.operation == "union_minutes":
        if len(node.arguments) != 2:
            raise LabError("Interval union requires start/end")
        groups = defaultdict(list)
        for row in source.rows:
            start, end = (instant(row.get(c)) for c in node.arguments)
            if end <= start:
                raise LabError("Invalid interval")
            groups[tuple(row[c] for c in node.group_by)].append((start, end))
        for key, intervals in groups.items():
            merged = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            rows.append(dict(zip(node.group_by, key, strict=True)) | {
                node.output: sum((end - start).total_seconds() / 60 for start, end in merged)
            })
    else:
        required = 3 if node.operation == "capped_difference" else 2
        if len(node.arguments) != required:
            raise LabError("Deterministic rule argument count mismatch")
        for row in source.rows:
            if node.output in row:
                raise LabError("Rule must not overwrite source evidence")
            values = [number(row.get(c)) for c in node.arguments]
            if node.operation == "ratio":
                value = None if values[1] == 0 else values[0] / values[1]
            elif node.operation == "difference":
                value = values[0] - values[1]
            else:
                if values[2] < 0:
                    raise LabError("Credit cap cannot be negative")
                value = max(0, min(values[0], values[2]) - values[1])
            rows.append(row | {node.output: value})
    return source.model_copy(update={
        **schema_values(schema), "id": node.id, "rows": rows,
    })


def resolve_cohort(
    node: QueryNode, artifacts: dict[str, Artifact], catalog: Catalog, limit: int,
    population: SourcePopulation | None = None,
) -> QueryNode:
    if not node.cohort_filters:
        return node
    binding = node.cohort_filters[0]
    source = artifacts[binding.source]
    complete(source, allow_known_mapped=binding.population == "known_mapped_subset")
    compatible_populations(population_evidence(source), [population] if population else [])
    if source.coverage.get("unknown_keys", 0) and binding.population != "known_mapped_subset":
        raise LabError("Cohort contains unknown/unmapped identities; not a complete population", Status.PARTIAL)
    values, seen = [], set()
    column_type = catalog.tables[node.table]["columns"][binding.target_column]
    for row in source.rows:
        if binding.source_column not in row:
            raise LabError("Cohort source omitted its declared key column")
        if row[binding.source_column] is None:
            if binding.population == "known_mapped_subset":
                continue
            raise LabError("Cohort contains missing/null keys; not a complete population", Status.PARTIAL)
        value = row[binding.source_column]
        valid = (
            (column_type == "int64" and type(value) is int)
            or (column_type in ("string", "date", "timestamp") and isinstance(value, str))
            or (column_type == "bool" and type(value) is bool)
        )
        if not valid:
            raise LabError("Cohort key type does not match the reviewed target identity")
        key = (type(value), value)
        if key not in seen:
            if len(values) >= min(250, limit):
                raise LabError("Cohort key budget exceeded; non-additive batching is not supported", Status.PARTIAL)
            seen.add(key)
            values.append(value)
    if not values:
        raise LabError(
            "Empty cohort: target source not queried; no target clock or total observed",
            Status.BLOCKED_CAPABILITY,
        )
    predicate = Filter(column=binding.target_column, op="in", values=values)
    return QueryNode.model_validate(node.model_dump() | {
        "cohort_filters": [], "filters": [f.model_dump() for f in [*node.filters, predicate]],
    })


def query_artifact(node: QueryNode, value: Artifact, catalog: Catalog, scope: Scope) -> Artifact:
    if value.population_qualification != "unqualified" or value.exclusions:
        raise LabError("A source provider cannot mint a runtime cohort qualification")
    if value.scope != scope:
        raise LabError("Provider returned the wrong principal", Status.BLOCKED_AUTH)
    if value.source_models != [node.model] or value.source_tables != [node.table]:
        raise LabError("Provider returned the wrong source identity")
    schema = catalog.query_schema(node)
    if value.lineage and artifact_schema(value) != schema:
        raise LabError("Provider metadata differs from the reviewed query contract")
    if any(set(row) != schema.lineage.keys() for row in value.rows):
        raise LabError("Provider omitted or added unrequested output columns")
    if len(value.rows) > node.max_rows:
        raise LabError("Provider exceeded query row budget", Status.PARTIAL)
    states = value.source_clocks or [SourceClock(
        model=node.model, table=node.table, data=value.clocks.data, model_revision=value.clocks.model,
    )]
    if len(states) != 1 or (states[0].model, states[0].table) != (node.model, node.table):
        raise LabError("Provider source clocks differ from the queried source")
    if states[0].model_revision != value.clocks.model:
        raise LabError("Provider model clocks are inconsistent")
    return value.model_copy(update={**schema_values(schema), "source_clocks": states})


class Executor:
    def __init__(
        self, catalog: Catalog, provider: QueryProvider, context: Context, *,
        source_populations: list[SourcePopulation] | None = None,
    ):
        self.catalog, self.provider, self.context = catalog, provider, context
        self.source_populations = (
            source_populations if source_populations is not None
            else getattr(provider, "source_populations", [])
        )

    def population(self, model: str, scope: Scope) -> SourcePopulation | None:
        if type(self.provider) is OfflineProvider and scope.mode == "OFFLINE_SYNTHETIC":
            return self.catalog.fixture_population(self.provider.data_path, model)
        matches = [entry for entry in self.source_populations if entry.model == model]
        if len(matches) > 1:
            raise LabError("Ambiguous source population provenance", Status.BLOCKED_CAPABILITY)
        value = matches[0] if matches else None
        if value and scope.mode == "LIVE_DELEGATED" and value.evidence_kind != "REVIEWED_LIVE":
            raise LabError("Live source population cannot use local fixture provenance", Status.BLOCKED_CAPABILITY)
        return value

    async def execute(
        self, plan: Plan, scope: Scope, *, on_event: EventSink | None = None,
    ) -> ExecutionResult:
        start, receipts, artifacts = perf_counter(), [], {}
        current, current_value, current_receipt = None, None, None
        node_start = start
        finished = set()

        async def emit(node, phase, *, status=None, message=None, elapsed=0, rows=None, receipt=None):
            if on_event is not None:
                await on_event(ExecutionEvent(
                    run_id=plan.request_id, node_id=node.id if node else None,
                    operation=node.kind if node else "ValidatePlan", phase=phase,
                    lane=self.provider.mode if isinstance(node, QueryNode) else "LOCAL_RUNTIME",
                    elapsed_ms=elapsed, returned_rows=rows, status=status, message=message,
                    model=node.model if isinstance(node, QueryNode) else None,
                    receipt_id=receipt.id if receipt else None,
                ))

        async def failed(status, message):
            await emit(
                current, "failed", status=status, message=message,
                elapsed=(perf_counter() - node_start) * 1000,
                rows=len(current_value.rows) if current_value is not None else None,
                receipt=current_receipt,
            )
            for pending in plan.nodes:
                if pending.id not in finished and (current is None or pending.id != current.id):
                    await emit(pending, "skipped", status=status, message="Execution stopped before this node")

        try:
            order = validate(plan, scope, self.catalog, self.context)
            materialized_rows = materialized_bytes = 0
            observed = []
            async with asyncio.timeout(plan.budgets.timeout_seconds):
                for node in order:
                    current, current_value, current_receipt = node, None, None
                    node_start = perf_counter()
                    await emit(node, "started")
                    if isinstance(node, ContextNode):
                        await emit(
                            node, "completed", status=Status.COMPLETE,
                            elapsed=(perf_counter() - node_start) * 1000, rows=0,
                        )
                        finished.add(node.id)
                        continue
                    if isinstance(node, QueryNode):
                        population = self.population(node.model, scope)
                        if population:
                            compatible_populations([population], [population])
                            if node.table not in population.table_hashes:
                                raise LabError("Source population omits queried table", Status.BLOCKED_CAPABILITY)
                        for previous in observed:
                            if population or previous.source_populations or previous.source_models != [node.model]:
                                compatible_populations(
                                    population_evidence(previous), [population] if population else [],
                                )
                        resolved = resolve_cohort(
                            node, artifacts, self.catalog, plan.budgets.max_rows, population,
                        )
                        value, receipt = await self.provider.query(resolved, scope)
                        current_value, current_receipt = value, receipt
                        receipts.append(receipt)
                        value = query_artifact(resolved, value, self.catalog, scope)
                        if value.source_populations:
                            compatible_populations(population_evidence(value), population_evidence(value))
                        if population:
                            if not value.source_populations and type(self.provider) is not OfflineProvider:
                                raise LabError(
                                    "Returned source population provenance UNKNOWN; cannot relabel cached output",
                                    Status.BLOCKED_CAPABILITY,
                                )
                            if value.source_populations:
                                compatible_populations(value.source_populations, [population])
                            if self.population(node.model, scope) != population:
                                raise LabError("Source population changed during query", Status.BLOCKED_CAPABILITY)
                            value = value.model_copy(update={"source_populations": [population]})
                        for previous in observed:
                            compatible_clocks(previous, value)
                        observed.append(value)
                        checked_rows(value)
                        if node.completeness == "full":
                            complete(value)
                        for binding in node.cohort_filters:
                            source = artifacts[binding.source]
                            states = compatible_clocks(source, value)
                            populations = compatible_populations(
                                population_evidence(source), population_evidence(value),
                            )
                            value = value.model_copy(update={
                                "source_clocks": states,
                                "source_populations": populations,
                                "source_models": sorted(set(source.source_models + value.source_models)),
                                "source_tables": sorted(set(source.source_tables + value.source_tables)),
                                "source_filters": unique(source.source_filters + value.source_filters),
                                "evidence_ids": list(dict.fromkeys(source.evidence_ids + value.evidence_ids)),
                                "warnings": source.warnings + value.warnings,
                                "coverage": source.coverage | value.coverage,
                                "exclusions": unique(source.exclusions + value.exclusions),
                            })
                            if binding.population == "known_mapped_subset":
                                exclusion = CohortExclusion(
                                    source_node=binding.source, source_column=binding.source_column,
                                    unknown_mapping_observations=source.coverage.get("unknown_keys", 0),
                                    null_key_rows=sum(row[binding.source_column] is None for row in source.rows),
                                    evidence_ids=list(source.evidence_ids),
                                )
                                warning = (
                                    "Known mapped cohort only; unknown mapping observations and null-key rows "
                                    "are excluded and reported, not zero or a complete population total."
                                )
                                value = value.model_copy(update={
                                    "status": Status.PARTIAL,
                                    "population_qualification": (
                                        "known_mapped_subset" if value.status == Status.COMPLETE
                                        and not value.coverage.get("unknown_source_rows", 0) else "unqualified"
                                    ),
                                    "exclusions": unique(value.exclusions + [exclusion]),
                                    "warnings": [*value.warnings, warning],
                                })
                                current_receipt = receipt.model_copy(update={
                                    "status": Status.PARTIAL, "warnings": [*receipt.warnings, warning],
                                    "completeness_evidence": receipt.completeness_evidence + "; " + warning,
                                })
                                receipts[-1] = current_receipt
                    elif isinstance(node, ProjectColumns):
                        value = project(node, artifacts[node.source])
                    elif isinstance(node, JoinNode):
                        value = join(
                            node, artifacts[node.left], artifacts[node.right], self.context,
                            max_rows=plan.budgets.max_rows - materialized_rows,
                            max_bytes=plan.budgets.max_bytes - materialized_bytes,
                        )
                    elif isinstance(node, AggregateNode):
                        value = aggregate(node, artifacts[node.source])
                    elif isinstance(node, RuleNode):
                        value = rule(node, artifacts[node.source])
                    elif isinstance(node, RankNode):
                        source = artifacts[node.source]
                        complete(source, allow_known_mapped=node.population == "known_mapped_subset")
                        rows = sorted(
                            source.rows, key=lambda r: (number(r.get(node.column)), json.dumps(r, sort_keys=True)),
                            reverse=node.descending,
                        )[:node.limit]
                        value = source.model_copy(update={
                            "id": node.id, "rows": rows,
                            "warnings": source.warnings + [
                                "Ranked known mapped subset only, not a population ranking or total"
                                if known_mapped_subset(source) else "Ranked subset, not a population total"
                            ],
                        })
                    elif isinstance(node, AnswerNode):
                        value = artifacts[node.source].model_copy(update={"id": node.id})
                    else:
                        raise LabError("Unsupported operator", Status.BLOCKED_CAPABILITY)
                    current_value = value
                    materialized_rows += len(value.rows)
                    materialized_bytes += len(json.dumps(value.rows, allow_nan=False).encode())
                    if materialized_rows > plan.budgets.max_rows or materialized_bytes > plan.budgets.max_bytes:
                        raise LabError("Cumulative intermediate budget exceeded", Status.PARTIAL)
                    artifacts[node.id] = value
                    await emit(
                        node, "completed", status=value.status, rows=len(value.rows), receipt=current_receipt,
                        elapsed=(perf_counter() - node_start) * 1000,
                    )
                    finished.add(node.id)
            answer = artifacts.get(plan.result_node)
            if answer is None:
                raise LabError("Final node did not produce a data artifact")
            terminal = next(node for node in plan.nodes if node.id == plan.result_node)
            additional = {
                label: artifacts[reference]
                for label, reference in terminal.additional_sources.items()
            } if isinstance(terminal, AnswerNode) else {}
            status = answer.status
            if any(value.status != Status.COMPLETE for value in additional.values()):
                status = Status.PARTIAL if status == Status.COMPLETE else status
            return ExecutionResult(
                request_id=plan.request_id, status=status, mode=self.provider.mode,
                answer=answer, additional_answers=additional, receipts=receipts,
                elapsed_ms=(perf_counter() - start) * 1000,
            )
        except LabError as error:
            await failed(error.status, str(error))
            return ExecutionResult(
                request_id=plan.request_id, status=error.status, mode=self.provider.mode,
                receipts=receipts, errors=[str(error)], elapsed_ms=(perf_counter() - start) * 1000,
            )
        except TimeoutError:
            await failed(Status.TIMED_OUT, "Execution deadline exceeded; no complete answer")
            return ExecutionResult(
                request_id=plan.request_id, status=Status.TIMED_OUT, mode=self.provider.mode,
                receipts=receipts, errors=["Execution deadline exceeded; no complete answer"],
                elapsed_ms=(perf_counter() - start) * 1000,
            )
        except asyncio.CancelledError:
            await failed(Status.FAILED, "Execution cancelled; no complete answer")
            raise
        except Exception as error:
            await failed(Status.FAILED, f"Unexpected {type(error).__name__}: {error}")
            raise
