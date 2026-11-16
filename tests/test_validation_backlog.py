"""Tests for tools/build_validation_backlog.py and docs/evaluation/validation-backlog.md."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from tools import build_validation_backlog as backlog

ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = ROOT / "eval" / "validation" / "register.json"
BACKLOG_PATH = ROOT / "docs" / "evaluation" / "validation-backlog.md"


def parse_table_rows(markdown: str) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for line in markdown.splitlines():
        if re.match(r"^\|\s*E\d{2}\s*\|", line):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            assert len(cells) == 4
            rows.append((cells[0], cells[1], cells[2], cells[3]))
    return rows


def test_committed_backlog_matches_register_with_24_exact_statuses():
    register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    markdown = BACKLOG_PATH.read_text(encoding="utf-8")
    assert backlog.main(["--check"]) == 0
    assert "[../architecture/platform-status.md](../architecture/platform-status.md)" in markdown
    rows = parse_table_rows(markdown)
    assert len(rows) == 24
    expected = [(entry["id"], backlog.format_status(entry["status"])) for entry in register["experiments"]]
    assert [(row[0], row[2]) for row in rows] == expected
    assert all(row[1].endswith("?") and row[3] for row in rows)


def test_render_backlog_propagates_register_status_and_rejects_drift(tmp_path):
    register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    updated = copy.deepcopy(register)
    updated["experiments"][1]["status"] = "PASS"
    out = tmp_path / "validation-backlog.md"
    reg_file = tmp_path / "register.json"
    reg_file.write_text(json.dumps(updated), encoding="utf-8")
    rendered = backlog.build(reg_file, out)
    rows = dict((row[0], row[2]) for row in parse_table_rows(rendered))
    assert rows["E02"] == backlog.format_status("PASS")
    assert rows["E01"] == backlog.format_status(register["experiments"][0]["status"])

    missing_one = copy.deepcopy(register)
    missing_one["experiments"].pop()
    with pytest.raises(ValueError, match="E01..E24"):
        backlog.render_backlog(missing_one)

    unknown_id = copy.deepcopy(register)
    unknown_id["experiments"][0]["id"] = "E99"
    with pytest.raises(ValueError, match="E99"):
        backlog.render_backlog(unknown_id)
