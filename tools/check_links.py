"""Validate relative links and heading anchors across Markdown documentation."""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = ("README.md", "PLAN.md", "docs")

FENCED_CODE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*$|\Z)", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]+`")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+[ \t]*)?$", re.MULTILINE)
MARKDOWN_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*(?:<([^>\n]+)>|([^)\s]+))(?:\s+[\"'][^\"']*[\"'])?\s*\)")
INLINE_LINK_IN_HEADING = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
HTML_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class LinkError:
    file: str
    line: int
    target: str
    reason: str


@dataclass(frozen=True)
class LinkReport:
    checked_files: int
    resolved_links: int
    external_urls: list[str]
    errors: list[LinkError]


def _blank_preserving_newlines(text: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in text)


def strip_code(markdown: str) -> str:
    masked = FENCED_CODE.sub(lambda m: _blank_preserving_newlines(m.group(0)), markdown)
    return INLINE_CODE.sub(lambda m: " " * len(m.group(0)), masked)


def github_slug(heading_text: str) -> str:
    cleaned = INLINE_LINK_IN_HEADING.sub(r"\1", heading_text.strip())
    cleaned = HTML_TAG.sub("", cleaned)
    cleaned = cleaned.replace("`", "")
    cleaned = re.sub(r"[*_~]", "", cleaned)
    cleaned = cleaned.lower()
    cleaned = re.sub(r"[^\w\s-]", "", cleaned)
    return cleaned.replace(" ", "-")


def heading_anchors(markdown_path: Path, cache: dict[Path, set[str]]) -> set[str]:
    resolved = markdown_path.resolve()
    if resolved in cache:
        return cache[resolved]
    masked = FENCED_CODE.sub(
        lambda m: _blank_preserving_newlines(m.group(0)),
        resolved.read_text(encoding="utf-8"),
    )
    counts: Counter[str] = Counter()
    anchors: set[str] = set()
    for match in HEADING.finditer(masked):
        base = github_slug(match.group(2))
        if not base:
            continue
        occurrence = counts[base]
        counts[base] += 1
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")
    cache[resolved] = anchors
    return anchors


def iter_markdown_files(root: Path, paths: tuple[str, ...] | list[str]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for rel in paths:
        candidate = (root / rel).resolve()
        if not candidate.exists():
            continue
        discovered = [candidate] if candidate.is_file() else sorted(candidate.rglob("*.md"))
        for path in discovered:
            if path.suffix.lower() == ".md" and path not in seen:
                seen.add(path)
                files.append(path)
    return files


def check_links(root: Path = ROOT, paths: tuple[str, ...] | list[str] = DEFAULT_PATHS) -> LinkReport:
    root_resolved = root.resolve()
    md_files = iter_markdown_files(root_resolved, paths)
    anchor_cache: dict[Path, set[str]] = {}
    external_urls: set[str] = set()
    errors: list[LinkError] = []
    resolved_count = 0

    for md_file in md_files:
        rel_source = md_file.relative_to(root_resolved).as_posix()
        scannable = strip_code(md_file.read_text(encoding="utf-8"))
        for match in MARKDOWN_LINK.finditer(scannable):
            raw_target = (match.group(1) or match.group(2) or "").strip()
            if not raw_target:
                continue
            line_no = scannable.count("\n", 0, match.start()) + 1

            if raw_target.startswith(("http://", "https://", "mailto:")):
                external_urls.add(raw_target)
                continue

            path_part, sep, fragment = raw_target.partition("#")
            anchor = unquote(fragment) if sep else ""
            if not path_part:
                target_file = md_file
            else:
                decoded_path = unquote(path_part)
                target_file = (md_file.parent / decoded_path).resolve()
                if not target_file.is_relative_to(root_resolved):
                    errors.append(LinkError(rel_source, line_no, raw_target, "escapes repository root"))
                    continue
                if not target_file.exists():
                    errors.append(LinkError(rel_source, line_no, raw_target, "target path does not exist"))
                    continue

            if anchor:
                anchor_doc = target_file / "README.md" if target_file.is_dir() else target_file
                if anchor_doc.suffix.lower() != ".md" or not anchor_doc.is_file():
                    errors.append(
                        LinkError(rel_source, line_no, raw_target, "heading anchor requires a Markdown target")
                    )
                    continue
                valid_anchors = heading_anchors(anchor_doc, anchor_cache)
                if anchor.lower() not in valid_anchors:
                    errors.append(
                        LinkError(rel_source, line_no, raw_target, f"heading anchor '#{anchor}' not found")
                    )
                    continue

            resolved_count += 1

    return LinkReport(
        checked_files=len(md_files),
        resolved_links=resolved_count,
        external_urls=sorted(external_urls),
        errors=sorted(errors, key=lambda item: (item.file, item.line, item.target)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root directory")
    parser.add_argument("--paths", nargs="*", default=list(DEFAULT_PATHS), help="Markdown files or directories")
    parser.add_argument("--list-external", action="store_true", help="Print every external URL discovered")
    args = parser.parse_args(argv)

    report = check_links(root=args.root, paths=args.paths)
    if args.list_external:
        for url in report.external_urls:
            print(f"EXTERNAL {url}")
    if report.errors:
        for err in report.errors:
            print(f"{err.file}:{err.line}: {err.target} -> {err.reason}", file=sys.stderr)
        return 1
    print(
        f"Link check passed: {report.checked_files} files, {report.resolved_links} relative links/anchors resolved, "
        f"{len(report.external_urls)} external URLs listed (not fetched)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
