"""Normalize a private author capture for local review; never mount it as a live reader."""

import argparse
import json
from pathlib import Path

from telco_iq.approval import private_config_path
from telco_iq.catalog import Catalog
from telco_iq.contracts import Scope
from telco_iq.native_context import (
    ReviewedExecutionRelationship,
    ReviewedModel,
    load_native_bundle,
    normalize_native_capture,
    write_native_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--definition", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--authorized-scope", type=Path, required=True)
    parser.add_argument("--reviewed-models", type=Path, required=True)
    parser.add_argument("--execution-reviews", type=Path)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "contracts" / "catalog.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    def private_json(path: Path):
        return json.loads(private_config_path(path, ROOT).read_text(encoding="utf-8-sig"))

    scope = Scope.model_validate(private_json(args.authorized_scope))
    snapshot = normalize_native_capture(
        private_json(args.definition), private_json(args.receipt),
        authorized_scope=scope, authorization_reference=args.authorization_reference,
        reviewed_models=[ReviewedModel.model_validate(item) for item in private_json(args.reviewed_models)],
        reviewed_relationships=[
            ReviewedExecutionRelationship.model_validate(item)
            for item in private_json(args.execution_reviews)
        ] if args.execution_reviews else [],
        catalog=Catalog(args.catalog),
    )
    path = write_native_snapshot(snapshot, args.output, repo=ROOT)
    persisted = load_native_bundle(path, scope)
    print(json.dumps({
        "entities": len(persisted.entities), "native_relationships": len(persisted.native_relationships),
        "metrics": len(persisted.metrics), "annotation_guidance": len(persisted.annotation_guidance),
        "formal_rules": len(persisted.context.rules),
        "reviewed_execution_relationships": len(persisted.context.relationships),
        "schema_hash": persisted.context.schema_hash, "content_hash": persisted.content_hash,
    }))


if __name__ == "__main__":
    main()
