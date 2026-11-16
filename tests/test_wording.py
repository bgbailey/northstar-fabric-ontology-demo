"""Unit and repository-wide tests for tools/check_wording.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import check_wording

ROOT = Path(__file__).resolve().parents[1]


def test_repository_wording_gate_passes() -> None:
    violations = check_wording.check_files(root=ROOT)
    assert violations == [], "\n".join(
        f"{v.file}:{v.line}: [{v.rule_id}] {v.match!r}" for v in violations
    )


@pytest.mark.parametrize(
    ("filename", "content", "expected_rule"),
    [
        ("docs/guide.md", "## Evidence gaps / still thin\nNeeds work.\n", "evidence-gaps-still-thin"),
        ("docs/guide.md", "Status is NOT_RUN today.\n", "not-run"),
        ("docs/guide.md", "Annotated with LiveDax in TMDL.\n", "livedax"),
        ("docs/guide.md", "Deploy to the DEV workspace for P0.\n", "dev-environment"),
        ("docs/guide.md", "Open the hero packet with a reader account.\n", "hero-packet-or-stream"),
        ("docs/guide.md", "Uses a generation-2 ontology and source proxy.\n", "generation-2"),
        ("docs/guide.md", "See experiment E04 and task TQ-003.\n", "experiment-task-ids"),
        ("docs/guide.md", "Result was PARTIAL in this swarm lane.\n", "partial-caps"),
        ("docs/guide.md", "Read C:\\dev\\skills-for-fabric locally.\n", "local-checkout-paths"),
    ],
)
def test_seeded_markdown_violations_are_reported_with_line(
    tmp_path: Path, filename: str, content: str, expected_rule: str
) -> None:
    target = tmp_path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")

    violations = check_wording.check_files(root=tmp_path, paths=["docs"])
    assert any(v.rule_id == expected_rule and v.line == 1 for v in violations)
    assert check_wording.main(["--root", str(tmp_path), "--paths", "docs"]) == 1


def test_validation_backlog_allows_experiment_ids_with_plain_status_only(tmp_path: Path) -> None:
    backlog = tmp_path / "docs" / "evaluation" / "validation-backlog.md"
    backlog.parent.mkdir(parents=True, exist_ok=True)
    backlog.write_text(
        "| ID | Question | Status |\n|---|---|---|\n| E02 | Can one ontology in Fabric connect models? | not yet run |\n",
        encoding="utf-8",
        newline="\n",
    )
    assert check_wording.check_files(root=tmp_path, paths=["docs"]) == []

    backlog.write_text(
        "| ID | Question | Status |\n|---|---|---|\n| E02 | Managed-native reader packet | NOT_RUN |\n",
        encoding="utf-8",
        newline="\n",
    )
    rules_hit = {v.rule_id for v in check_wording.check_files(root=tmp_path, paths=["docs"])}
    assert {"managed-native", "reader", "packet", "not-run"} <= rules_hit


def test_tsx_ui_strings_flagged_while_wire_protocol_identifiers_preserved(tmp_path: Path) -> None:
    clean_tsx = tmp_path / "apps" / "web" / "src" / "View.tsx"
    clean_tsx.parent.mkdir(parents=True, exist_ok=True)
    clean_tsx.write_text(
        'import { NativeDemo } from "./NativeDemo";\n'
        'export function View({ status, arm }: { status: string; arm: "baseline" | "native" }) {\n'
        '  const isPartial = status === "PARTIAL" && arm === "native";\n'
        '  return <div className="native-graph">{isPartial ? "Partial result (unmapped rows excluded)" : "Ready"}</div>;\n'
        "}\n",
        encoding="utf-8",
        newline="\n",
    )
    assert check_wording.check_files(root=tmp_path, paths=["apps/web/src"]) == []

    dirty_tsx = tmp_path / "apps" / "web" / "src" / "Dirty.tsx"
    dirty_tsx.write_text(
        "export function Dirty() {\n"
        '  const phase = "NOT_RUN";\n'
        "  return <footer>Evidence gaps / still thin: Sign in to DEV as reader</footer>;\n"
        "}\n",
        encoding="utf-8",
        newline="\n",
    )
    violations = check_wording.check_files(root=tmp_path, paths=["apps/web/src"])
    hit_rules = {(v.line, v.rule_id) for v in violations if v.file.endswith("Dirty.tsx")}
    assert (2, "not-run") in hit_rules
    assert (3, "evidence-gaps-still-thin") in hit_rules
    assert (3, "dev-environment") in hit_rules
    assert (3, "reader") in hit_rules
