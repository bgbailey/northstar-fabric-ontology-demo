import json
from pathlib import Path

from telco_iq.contracts import Context, ExecutionResult, Plan

root = Path(__file__).resolve().parents[1]
target = root / "contracts"
target.mkdir(exist_ok=True)
for name, model in [("plan-v1", Plan), ("context-v1", Context), ("result-v1", ExecutionResult)]:
    (target / f"{name}.schema.json").write_text(
        json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8",
    )
