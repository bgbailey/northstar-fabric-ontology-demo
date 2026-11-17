from __future__ import annotations

import re
from typing import Any

from .assets import load_asset_module

_BUILDER: Any | None = None


def _builder() -> Any:
    global _BUILDER
    if _BUILDER is None:
        _BUILDER = load_asset_module(
            "assets/build_ontology_prompts.py",
            "telco_iq._ontology_prompt_asset",
        )
    return _BUILDER


def render_ordered_steps(
    contract: dict[str, Any],
    models: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    rendered = _builder().render_prompts(contract)
    marker = "## Plan prompts by source model\n\n"
    if marker not in rendered:
        raise ValueError("Packaged prompt renderer returned no ordered model steps")
    body = rendered.split(marker, 1)[1]
    for alias, item in models.items():
        exact = f"{item['displayName']} (item ID {item['id']})"
        body = body.replace(f"{alias}.SemanticModel", exact)

    ontology_name = contract["displayName"]
    body = re.sub(
        rf"^Generate `{re.escape(ontology_name)}` from .+$",
        (
            "Use the existing ontology item created by `northstar ontology bootstrap`; "
            "do not create another item."
        ),
        body,
        flags=re.MULTILINE,
    )
    body = body.replace(
        f"Review {ontology_name} generated from",
        f"Review {ontology_name} using",
    )
    if "Generate ontology" in body or f"create `{ontology_name}`" in body:
        raise ValueError("Rendered runtime prompts still request duplicate ontology creation")

    blocks = re.split(r"(?=^### Step \d+:)", body, flags=re.MULTILINE)
    steps: list[dict[str, Any]] = []
    for block in blocks:
        match = re.match(r"^### Step (\d+):.*?\(`([^`]+)`\)", block)
        if not match:
            continue
        number = int(match.group(1))
        alias = match.group(2)
        if alias not in models:
            raise ValueError(f"Prompt step model alias is not in the deployment journal: {alias}")
        steps.append(
            {
                "number": number,
                "alias": alias,
                "model": models[alias],
                "markdown": block.strip() + "\n",
            },
        )
    if [step["number"] for step in steps] != list(range(1, len(steps) + 1)):
        raise ValueError("Rendered prompt steps are not ordered")
    if len(steps) != len(models):
        raise ValueError("Rendered prompt steps do not match the four deployed models")
    return steps
