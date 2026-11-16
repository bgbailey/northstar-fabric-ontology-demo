"""Render docs/evaluation/validation-backlog.md from eval/validation/register.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTER = ROOT / "eval" / "validation" / "register.json"
DEFAULT_OUTPUT = ROOT / "docs" / "evaluation" / "validation-backlog.md"

BACKLOG_METADATA: dict[str, tuple[str, str]] = {
    "E01": (
        "Can the demo tenant create and read back a Fabric IQ ontology (current experience) item "
        "with the expected TMDL definition?",
        "G1, M5-02, M7-02",
    ),
    "E02": (
        "Can one ontology in Fabric connect multiple semantic models, align shared entities on "
        "verified keys, and keep incompatible keys separate?",
        "G1, G2, M7-02",
    ),
    "E03": (
        "Does each ontology metric link to the right semantic model, home table, and source DAX "
        "measure without merging same-name measures?",
        "G1, M7-02",
    ),
    "E04": (
        "Can a demo user read metric descriptions and query results with Read and Build access "
        "while Write-only DAX expressions and item definitions stay restricted?",
        "G6, M7-02",
    ),
    "E05": (
        "Does the ontology MCP endpoint list read-only discovery and query tools that return "
        "entity types, relationships, metrics, and rules?",
        "G4, M3-01, M7-02",
    ),
    "E06": (
        "Can the client discover semantic models, inspect schemas, search values, and run "
        "per-model DAX queries through the Fabric IQ MCP server?",
        "M3-01, M4-05",
    ),
    "E07": (
        "Does delegated sign-in enforce row-level and object-level security for each demo user "
        "across ontology context and semantic-model queries?",
        "G6, M7-02",
    ),
    "E08": (
        "Does editing a metric description or local formula on a test ontology leave the "
        "semantic model's DAX measure unchanged?",
        "G1, M7-02",
    ),
    "E09": (
        "How do data refreshes, DAX measure edits, and schema changes propagate through "
        "semantic models, ontology metadata, and client caches?",
        "M5-04, M7-02",
    ),
    "E10": (
        "Which parts of an ontology definition survive export and import through the REST "
        "definition API, and which metrics linked to source measures must be authored in Fabric?",
        "M5-02, M5-04",
    ),
    "E11": (
        "Does changing a natural-language rule in the ontology change how an agent plans a "
        "query without prompt edits or unauthorized actions?",
        "G2, M4-05",
    ),
    "E12": (
        "Do entity types and metrics that share a display name across domains stay distinct "
        "unless an explicit relationship aligns their keys?",
        "G1, G2",
    ),
    "E13": (
        "How do entity-type inheritance and shared properties behave in the demo tenant when "
        "base and child entity types are queried?",
        "M7-01, M7-02",
    ),
    "E14": (
        "Does restoring a saved ontology version recover entity types, relationships, metrics, "
        "and rules without altering semantic-model data?",
        "M5-04",
    ),
    "E15": (
        "Can a Fabric data agent answer multi-model questions from an ontology bound to "
        "semantic models in the demo tenant?",
        "G5, M3-05, M7-02",
    ),
    "E16": (
        "Do Direct Lake semantic models over OneLake Delta tables and shortcuts return "
        "identical measure results for the synthetic dataset?",
        "M5-01, M5-05",
    ),
    "E17": (
        "Can spine entity types bound to lakehouse Delta tables use the ontology-managed graph "
        "while metric-bearing entity types remain bound to semantic models?",
        "G7, M6-01, M7-02",
    ),
    "E18": (
        "Do the MCP clients detect row caps, preserve null values and exact decimals, and "
        "report truncated or incomplete responses as partial results?",
        "G3, M3-01, M4-02",
    ),
    "E19": (
        "Are cross-model joins over validity periods and non-additive measures computed "
        "without revenue fan-out or misattributed monthly credits?",
        "G2, M4-03, M4-05",
    ),
    "E20": (
        "Do concurrent requests reuse sessions safely within a user scope, isolate caches "
        "between demo users, and respect retry-after headers?",
        "G3, G6",
    ),
    "E21": (
        "On repeated runs with equal semantic-model preparation, how does the ontology-grounded "
        "evaluation arm compare with semantic models alone and with rules in the prompt?",
        "M4-01, M4-05",
    ),
    "E22": (
        "Can one ontology bind to Direct Lake semantic models hosted in separate domain "
        "workspaces while preserving lineage and permissions?",
        "M5-05, M7-02",
    ),
    "E23": (
        "Does source-controlled semantic-model promotion detect measure drift across models "
        "before ontology refresh?",
        "M5-04",
    ),
    "E24": (
        "Does long-running asynchronous task polling, reconnection, and cancellation report "
        "failed or cancelled runs accurately?",
        "M3-05, M4-02",
    ),
}


STATUS_LABELS: dict[str, str] = {
    "NOT_RUN": "not yet run",
    "INCONCLUSIVE": "inconclusive",
    "PASS": "passed",
    "FAIL": "failed",
    "PARTIAL": "partial result",
    "BLOCKED": "blocked",
}


def format_status(status: str) -> str:
    """Map register status code to Appendix C plain-language backlog label."""
    return STATUS_LABELS.get(status, status.lower().replace("_", " "))


def render_backlog(register: dict) -> str:
    """Render the plain-language validation backlog table from register JSON."""
    experiments = register.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        raise ValueError("Validation register must contain a non-empty experiments list")
    allowed = set(register.get("stateMachine", {}).get("allowed", ()))
    seen_ids: list[str] = []
    rows: list[str] = []
    for entry in experiments:
        exp_id = entry["id"]
        status = entry["status"]
        if exp_id not in BACKLOG_METADATA:
            raise ValueError(f"Missing plain-language mapping for experiment {exp_id}")
        if allowed and status not in allowed:
            raise ValueError(f"Invalid status {status!r} for experiment {exp_id}")
        question, coverage = BACKLOG_METADATA[exp_id]
        seen_ids.append(exp_id)
        rows.append(f"| {exp_id} | {question} | {format_status(status)} | {coverage} |")
    missing = set(BACKLOG_METADATA) - set(seen_ids)
    if missing or len(seen_ids) != len(set(seen_ids)):
        raise ValueError("Register experiment set does not match expected E01..E24 entries")

    lines = [
        "# Validation backlog",
        "",
        "This backlog summarizes the 24 validation questions tracked in `eval/validation/register.json`.",
        "Fabric IQ ontology (current experience) and several connected agent surfaces are in preview "
        "(checked 16 November 2026; see "
        "[Microsoft Learn](https://learn.microsoft.com/en-us/fabric/iq/ontology/overview) and "
        "[../architecture/platform-status.md](../architecture/platform-status.md)).",
        "",
        "Statuses below come directly from `eval/validation/register.json` and change only when live "
        "evidence from a demo tenant is recorded. Each evaluation arm is one tested configuration that "
        "receives the same questions, semantic models, and permissions.",
        "",
        "Regenerate this file with `python tools/build_validation_backlog.py`.",
        "",
        "## Experiments",
        "",
        "| ID | Question | Status | Check or work item |",
        "|---|---|---|---|",
        *rows,
        "",
    ]
    return "\n".join(lines)


def build(register_path: Path = DEFAULT_REGISTER, output_path: Path = DEFAULT_OUTPUT) -> str:
    register = json.loads(register_path.read_text(encoding="utf-8"))
    rendered = render_backlog(register)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(rendered.encode("utf-8"))
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", type=Path, default=DEFAULT_REGISTER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Verify output matches register without writing")
    args = parser.parse_args(argv)

    register = json.loads(args.register.read_text(encoding="utf-8"))
    rendered = render_backlog(register)
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"{args.output} is out of date; run python tools/build_validation_backlog.py", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(rendered.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
