"""Offline review lineage from actually executed projections/joins, never runtime identity authority."""


def review_column_matches(plan):
    result = {}
    for node in plan["nodes"]:
        kind = node["kind"]
        if kind == "QuerySemanticModel":
            result[node["id"]] = {
                column: {(node["id"], column)}
                for column in node["columns"] + node["group_by"] + node["metrics"]
            }
        elif kind == "ProjectColumns":
            result[node["id"]] = {
                alias: result[node["source"]][column] for alias, column in node["columns"].items()
            }
        elif kind in ("JoinResults", "ResolveIdentity", "SemiJoinCohort"):
            value = dict(result[node["left"]])
            if kind != "SemiJoinCohort":
                for column, origins in result[node["right"]].items():
                    if column in value:
                        if column not in node["left_keys"] or column not in node["right_keys"]:
                            raise RuntimeError("Unreviewed duplicate output column in actual plan")
                        value[column] = value[column] | origins
                    else:
                        value[column] = origins
                for left_key, right_key in zip(node["left_keys"], node["right_keys"], strict=True):
                    matched = value[left_key] | value[right_key]
                    # Preserve transitive equalities through earlier aliases, not just the two latest key names.
                    while True:
                        combined = matched | set().union(*(origins for origins in value.values() if origins & matched))
                        if combined == matched:
                            break
                        matched = combined
                    value = {
                        column: origins | matched if origins & matched else origins
                        for column, origins in value.items()
                    }
            if node.get("overlap_start"):
                value[node["overlap_start"]] = set()
                value[node["overlap_end"]] = set()
            result[node["id"]] = value
        elif kind in ("RankCandidates", "SynthesizeAnswer"):
            result[node["id"]] = dict(result[node["source"]])
        elif kind == "AggregateResult":
            result[node["id"]] = {column: result[node["source"]][column] for column in node["group_by"]}
            result[node["id"]].update({column: set() for column in node["sums"]})
        elif kind in ("ApplyDeterministicRule", "ComparePeriods"):
            result[node["id"]] = {**result[node["source"]], node["output"]: set()}
        else:
            result[node["id"]] = {}
    return result
