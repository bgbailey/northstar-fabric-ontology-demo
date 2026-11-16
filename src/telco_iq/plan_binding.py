"""Opt-in column-name compilation; never chooses sources, relationships or calculations."""

from pydantic import ValidationError

from telco_iq.catalog import Catalog, digest
from telco_iq.contracts import (
    AggregateNode,
    Context,
    JoinNode,
    Plan,
    ProjectColumns,
    QueryNode,
    RankNode,
    ResultSchema,
    RuleNode,
    Status,
)
from telco_iq.errors import LabError
from telco_iq.executor import node_schema, plan_order, validate

QUALIFIED_COLUMN_POLICY = {
    "mode": "source_qualified",
    "query_outputs": "Raw columns, or group_by columns plus canonical metric IDs; not measure display names.",
    "query_inputs": (
        "Query columns/group_by/filters/expected_grain/date_role use exact source-column IDs, not qualified "
        "artifact references. Qualification applies only when consuming an existing result artifact."
    ),
    "references": "Use an exact existing output alias or an unambiguous model.table.column source identity.",
    "projections": "Output alias maps to one existing input column; qualification never changes a value.",
    "join_collisions": (
        "The compiler inserts an explicit right-side projection for colliding column names, including keys. "
        "Left aliases stay unchanged; use source-qualified identities to address right-side identities downstream."
    ),
    "ambiguity": "Missing, ambiguous or derived identity references are rejected, never guessed.",
    "scope": "Only column binding/projection lowering; no source, relationship, grain, period, arithmetic or answer repair.",
}


def bind_plan_columns(plan: Plan, catalog: Catalog, context: Context) -> tuple[Plan, dict]:
    if plan.principal_scope != context.scope:
        raise LabError("Column binding cannot change the current reader scope", Status.BLOCKED_AUTH)
    if plan.ontology_revision != context.revision:
        raise LabError("Column binding requires the current context revision", Status.BLOCKED_CAPABILITY)
    if plan.clarification_state != "resolved":
        raise LabError("Clarification proposals are not executable column programs", Status.AMBIGUOUS)
    order = plan_order(plan)
    if any(isinstance(node, QueryNode) and node.model not in context.scope.models for node in order):
        raise LabError("Column binding source is outside current reader scope", Status.BLOCKED_AUTH)
    schemas: dict[str, ResultSchema] = {}
    output = []
    used_ids = {node.id for node in order}
    resolved, projections = [], []

    def resolve(node_id: str, reference: str, schema: ResultSchema, *, identity: bool = False) -> str:
        if reference in schema.lineage:
            return reference
        matches = [
            alias for alias, origins in schema.lineage.items()
            if len(origins) == 1
            and f"{origins[0].model}.{origins[0].table}.{origins[0].column}" == reference
            and (not identity or alias not in schema.derived_columns)
        ]
        if len(matches) != 1:
            raise LabError(
                f"Column reference must bind one existing source identity: node={node_id}, "
                f"reference={reference}, matches={len(matches)}"
            )
        resolved.append({"node_id": node_id, "reference": reference, "column_alias": matches[0]})
        return matches[0]

    def unused(base: str, used: set[str], *, limit: int | None = None) -> str:
        for index in range(101):
            suffix = "" if index == 0 else f"_{index}"
            value = (base[:limit - len(suffix)] if limit is not None else base) + suffix
            if value not in used:
                used.add(value)
                return value
        raise LabError("Column binding exhausted the canonical node/alias namespace")

    for original in order:
        node = original.model_copy(deep=True)
        if isinstance(node, QueryNode):
            node.cohort_filters = [
                binding.model_copy(update={
                    "source_column": resolve(node.id, binding.source_column, schemas[binding.source], identity=True),
                }) for binding in node.cohort_filters
            ]
        elif isinstance(node, ProjectColumns):
            node.columns = {
                alias: resolve(node.id, column, schemas[node.source]) for alias, column in node.columns.items()
            }
        elif isinstance(node, JoinNode):
            left, right = schemas[node.left], schemas[node.right]
            node.left_keys = [resolve(node.id, value, left, identity=True) for value in node.left_keys]
            node.right_keys = [resolve(node.id, value, right, identity=True) for value in node.right_keys]
            if node.temporal:
                node.temporal = node.temporal.model_copy(update={
                    f"{side}_{bound}": resolve(
                        node.id, getattr(node.temporal, f"{side}_{bound}"), schema,
                    )
                    for side, schema in (("left", left), ("right", right)) for bound in ("start", "end")
                })
            collisions = left.lineage.keys() & right.lineage.keys() if node.kind != "SemiJoinCohort" else set()
            if collisions:
                used_columns = set(left.lineage) | set(right.lineage)
                renamed = {
                    column: unused(f"__right_{node.id}_{column}", used_columns) if column in collisions else column
                    for column in right.lineage
                }
                projection = ProjectColumns(
                    id=unused(f"columns_{node.id}", used_ids, limit=64), kind="ProjectColumns",
                    source=node.right, depends_on=[node.right],
                    columns={alias: column for column, alias in renamed.items()},
                )
                schemas[projection.id] = node_schema(projection, schemas, catalog, context)
                output.append(projection)
                projections.append({
                    "join_node": node.id, "projection_node": projection.id,
                    "source_node": projection.source, "columns": dict(projection.columns),
                })
                node.right = projection.id
                node.depends_on = list(dict.fromkeys([*node.depends_on, projection.id]))
                node.right_keys = [renamed[column] for column in node.right_keys]
                if node.temporal:
                    node.temporal = node.temporal.model_copy(update={
                        f"right_{bound}": renamed[getattr(node.temporal, f"right_{bound}")]
                        for bound in ("start", "end")
                    })
        elif isinstance(node, AggregateNode):
            node.group_by = [resolve(node.id, value, schemas[node.source]) for value in node.group_by]
            node.sums = [resolve(node.id, value, schemas[node.source]) for value in node.sums]
        elif isinstance(node, RuleNode):
            node.arguments = [resolve(node.id, value, schemas[node.source]) for value in node.arguments]
            node.group_by = [resolve(node.id, value, schemas[node.source]) for value in node.group_by]
        elif isinstance(node, RankNode):
            node.column = resolve(node.id, node.column, schemas[node.source])
        schemas[node.id] = node_schema(node, schemas, catalog, context)
        output.append(node)
    try:
        compiled = Plan.model_validate({
            **plan.model_dump(mode="json"), "nodes": [node.model_dump(mode="json") for node in output],
        })
    except ValidationError as error:
        raise LabError("Column binding exceeds canonical plan contract limits") from error
    validate(compiled, context.scope, catalog, context)
    return compiled, {
        "mode": "source_qualified", "status": "COMPILED",
        "model_proposal_hash": digest(plan.model_dump(mode="json")),
        "canonical_plan_hash": digest(compiled.model_dump(mode="json")),
        "model_proposal": plan.model_dump(mode="json"),
        "generated_projection_nodes": [value["projection_node"] for value in projections],
        "resolved_source_references": resolved, "right_column_projections": projections,
        "source_choices_calculations_and_guards_changed": False,
        "source_queries_at_compilation": 0,
    }
