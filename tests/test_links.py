"""Unit and repository-wide tests for tools/check_links.py."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from tools import check_links

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        pytest.fail("check_links must never make network connections")

    monkeypatch.setattr(socket, "create_connection", blocked)


def test_repository_markdown_links_and_anchors_resolve() -> None:
    report = check_links.check_links(root=ROOT)
    assert report.errors == [], "\n".join(
        f"{e.file}:{e.line}: {e.target} -> {e.reason}" for e in report.errors
    )
    assert report.checked_files >= 15
    assert report.resolved_links > 0
    assert any(url.startswith("https://learn.microsoft.com/") for url in report.external_urls)


def test_resolves_anchors_lists_external_and_ignores_code_spans(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "README.md").write_text(
        "# Demo title\n\n"
        "See [details](docs/detail.md#how-it-works), [repeat](docs/detail.md#how-it-works-1), "
        "and [Microsoft Learn](https://learn.microsoft.com/en-us/fabric/iq/overview).\n\n"
        "Ignore `[example](missing/in-inline-code.md)` and:\n\n"
        "```markdown\n[broken](missing/in-fenced-block.md)\n```\n",
        encoding="utf-8",
        newline="\n",
    )
    (docs / "detail.md").write_text(
        "# Detail\n\n## How it works\nFirst.\n\n## How it works\nSecond, back to [README](../README.md#demo-title).\n",
        encoding="utf-8",
        newline="\n",
    )

    report = check_links.check_links(root=tmp_path, paths=["README.md", "docs"])
    assert report.errors == []
    assert report.resolved_links == 3
    assert report.external_urls == ["https://learn.microsoft.com/en-us/fabric/iq/overview"]
    assert check_links.main(["--root", str(tmp_path), "--paths", "README.md", "docs", "--list-external"]) == 0


def test_reports_missing_files_broken_anchors_and_root_escapes(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Heading\n\n"
        "- [Missing file](docs/absent.md)\n"
        "- [Missing anchor](#wrong-heading)\n"
        "- [Escape](../outside.md)\n",
        encoding="utf-8",
        newline="\n",
    )
    report = check_links.check_links(root=tmp_path, paths=["README.md"])
    reasons = {(e.line, e.reason) for e in report.errors}
    assert (3, "target path does not exist") in reasons
    assert (4, "heading anchor '#wrong-heading' not found") in reasons
    assert (5, "escapes repository root") in reasons
    assert check_links.main(["--root", str(tmp_path), "--paths", "README.md"]) == 1
