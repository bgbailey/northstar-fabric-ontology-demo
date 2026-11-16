"""Strict service JSON shape; all original Plan constraints still apply after decoding."""

import json
from copy import deepcopy
from datetime import datetime

from telco_iq.contracts import Budgets, Plan, Scope

SERVER_FIELDS = {"request_id", "question", "principal_scope", "as_of", "ontology_revision", "budgets"}
WIRE_GUIDANCE = (
    "Return the supplied strict business-proposal wire schema. Every exposed field is required. "
    "Do not emit request_id, question, principal_scope, as_of, ontology_revision or budgets; "
    "the server binds those fields from the authenticated request, not model output. "
    "ProjectColumns.columns is an array of {output,source} alias pairs, not an object; "
    "SynthesizeAnswer.additional_sources is an array of {label,source} pairs for separate result tables, "
    "or an empty array; it does not join or aggregate their incompatible grains. "
    "metric queries use nonempty metrics, group_by for dimensions and empty columns; "
    "raw queries use nonempty columns, empty metrics and empty group_by. "
    "this is only a serialization change, not a source calculation or an execution instruction."
)


def local_plan_constraints() -> dict:
    schema = Plan.model_json_schema()
    restrictions = {
        "minLength", "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum",
        "exclusiveMaximum", "minItems", "maxItems", "minProperties", "maxProperties",
    }
    result = {}
    sections = {"Plan": schema, **schema["$defs"]}
    for name, section in sections.items():
        if name in {"Scope", "Budgets"}:
            continue
        for field, spec in section.get("properties", {}).items():
            if name == "Plan" and field in SERVER_FIELDS:
                continue
            bounds = {key: value for key, value in spec.items() if key in restrictions}
            if bounds:
                result[f"{name}.{field}"] = bounds
    return result


def strict_wire_guidance() -> str:
    return WIRE_GUIDANCE + (
        " Service schema omits unsupported size/pattern bounds, but these original local constraints "
        "still apply. For ProjectColumns.columns, minProperties is the minimum number of alias pairs: "
    ) + json.dumps(local_plan_constraints(), sort_keys=True, separators=(",", ":"))


def strict_plan_schema() -> dict:
    schema = deepcopy(Plan.model_json_schema())
    for field in SERVER_FIELDS:
        del schema["properties"][field]
    for definition in ("Scope", "Budgets"):
        del schema["$defs"][definition]
    schema["$defs"]["ProjectColumns"]["properties"]["columns"] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"output": {"type": "string"}, "source": {"type": "string"}},
            "required": ["output", "source"],
            "additionalProperties": False,
        },
    }
    schema["$defs"]["AnswerNode"]["properties"]["additional_sources"] = {
        "type": "array",
        "items": {
            "type": "object", "properties": {"label": {"type": "string"}, "source": {"type": "string"}},
            "required": ["label", "source"], "additionalProperties": False,
        },
    }

    def convert(value):
        result = {
            key: deepcopy(value[key]) for key in ("type", "enum", "$ref", "description") if key in value
        }
        if "const" in value:
            result["enum"] = [value["const"]]
        if "$defs" in value:
            result["$defs"] = {key: convert(item) for key, item in value["$defs"].items()}
        if "properties" in value:
            if value.get("additionalProperties") is not False:
                raise ValueError("Strict plan wire objects require a closed property set")
            result["properties"] = {key: convert(item) for key, item in value["properties"].items()}
            result["required"] = list(result["properties"])
            result["additionalProperties"] = False
        if "items" in value:
            result["items"] = convert(value["items"])
        for keyword in ("anyOf", "oneOf"):
            if keyword in value:
                result["anyOf"] = [convert(item) for item in value[keyword]]
        return result

    result = convert(schema)

    def property_count(value):
        if isinstance(value, dict):
            return len(value.get("properties", {})) + sum(property_count(item) for item in value.values())
        return sum(property_count(item) for item in value) if isinstance(value, list) else 0

    def depth(value, refs=()):
        if "$ref" in value:
            reference = value["$ref"]
            if not reference.startswith("#/$defs/") or reference in refs:
                raise ValueError("Strict plan wire requires acyclic local definition references")
            return depth(result["$defs"][reference.split("/")[-1]], (*refs, reference))
        nested = [*value.get("properties", {}).values(), *value.get("anyOf", [])]
        if "items" in value:
            nested.append(value["items"])
        return int(value.get("type") == "object") + max((depth(item, refs) for item in nested), default=0)

    if result.get("type") != "object" or property_count(result) > 100 or depth(result) > 5:
        raise ValueError("Strict plan wire exceeds the reviewed service schema bounds")
    return result


def server_plan_fields(payload: dict) -> dict:
    metadata = payload["run_metadata"]
    scope = Scope.model_validate(payload["scope"])
    budgets = Budgets.model_validate(metadata["budgets"])
    as_of = metadata["as_of"]
    parsed = datetime.fromisoformat(as_of)
    request_id, question, revision = metadata["request_id"], payload["question"], payload["context"]["revision"]
    if (
        parsed.tzinfo is None or not all(isinstance(value, str) and value.strip()
                                        for value in (request_id, question, revision))
    ):
        raise ValueError("Strict proposals require complete aware server-owned run metadata")
    return {
        "request_id": request_id, "question": question, "principal_scope": scope.model_dump(mode="json"),
        "as_of": as_of, "ontology_revision": revision, "budgets": budgets.model_dump(mode="json"),
    }


def decode_strict_plan(value: dict, server_fields: dict) -> dict:
    if set(server_fields) != SERVER_FIELDS or set(value) & SERVER_FIELDS:
        raise ValueError("Model proposal cannot supply or replace server-owned metadata")
    decoded = deepcopy(value)
    for node in decoded.get("nodes", []):
        if node.get("kind") == "SynthesizeAnswer":
            pairs = node.get("additional_sources", [])
            if not isinstance(pairs, list) or any(
                not isinstance(pair, dict) or set(pair) != {"label", "source"}
                or not all(isinstance(pair[key], str) and pair[key].strip() for key in ("label", "source"))
                for pair in pairs
            ) or len({pair["label"] for pair in pairs}) != len(pairs):
                raise ValueError("Separate answer wire requires unique explicit label/source pairs")
            node["additional_sources"] = {pair["label"]: pair["source"] for pair in pairs}
        if node.get("kind") != "ProjectColumns":
            continue
        pairs = node.get("columns")
        if not isinstance(pairs, list) or any(
            not isinstance(pair, dict) or set(pair) != {"output", "source"}
            or not all(isinstance(pair[key], str) and pair[key] for key in ("output", "source"))
            for pair in pairs
        ):
            raise ValueError("Projection wire requires explicit nonempty output/source alias pairs")
        if len({pair["output"] for pair in pairs}) != len(pairs):
            raise ValueError("Projection wire contains duplicate output aliases")
        node["columns"] = {pair["output"]: pair["source"] for pair in pairs}
    decoded.update(deepcopy(server_fields))
    return decoded
