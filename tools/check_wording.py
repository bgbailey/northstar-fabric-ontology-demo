"""Check documentation and web UI strings against plain-language wording rules."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = Path(__file__).resolve().with_name("wording-rules.json")

FENCED_CODE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*$|\Z)", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]+`")
MD_LINK_TARGET = re.compile(r"(\[[^\]]*\])\(([^)\n]+)\)")
TS_COMMENT_OR_STRING = re.compile(
    r"//[^\n]*|/\*.*?\*/|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`",
    re.DOTALL,
)
JSX_TEXT = re.compile(r"(?<![=])>([^<>{}\n]+)(?=<|\{)")
SINGLE_TOKEN = re.compile(r"^[A-Za-z0-9_.:/#-]+$")
UI_PROP_PREFIX = re.compile(
    r"(?:label|title|placeholder|aria-label|ariaLabel|summary|claim|detail|eyebrow|heading)\s*[:=]\s*$"
)
IMPORT_PREFIX = re.compile(r"(?:\bfrom|\bimport)\s*$")
CLASS_PREFIX = re.compile(r"\bclassName\s*=\s*\{?\s*$")
NON_PROSE_JSX = re.compile(r"^\s*(?:&&|\|\||\?|:|=>|;|\)|\]|,)+\s*$|\b(?:return|throw|const|let|typeof)\b")


@dataclass(frozen=True)
class Rule:
    id: str
    regex: re.Pattern[str]
    scope: str
    exclude_files: tuple[str, ...]
    replacement: str


@dataclass(frozen=True)
class Violation:
    file: str
    line: int
    rule_id: str
    match: str
    replacement: str


def load_rules(rules_path: Path = DEFAULT_RULES) -> tuple[list[str], list[Rule]]:
    raw = json.loads(rules_path.read_text(encoding="utf-8"))
    compiled: list[Rule] = []
    for entry in raw["rules"]:
        flags = re.IGNORECASE if entry.get("ignoreCase") else 0
        compiled.append(
            Rule(
                id=entry["id"],
                regex=re.compile(entry["pattern"], flags),
                scope=entry["scope"],
                exclude_files=tuple(entry.get("excludeFiles", ())),
                replacement=entry["replacement"],
            )
        )
    return list(raw.get("defaultPaths", ["README.md", "docs", "apps/web/src"])), compiled


def _blank_preserving_newlines(text: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in text)


def markdown_prose(text: str) -> str:
    masked = FENCED_CODE.sub(lambda m: _blank_preserving_newlines(m.group(0)), text)
    masked = INLINE_CODE.sub(lambda m: " " * len(m.group(0)), masked)
    return MD_LINK_TARGET.sub(
        lambda m: f"{m.group(1)}({' ' * len(m.group(2))})",
        masked,
    )


def ts_ui_segments(text: str) -> list[tuple[int, str]]:
    segments: list[tuple[int, str]] = []
    without_comments = list(text)
    for match in TS_COMMENT_OR_STRING.finditer(text):
        token = match.group(0)
        start = match.start()
        line_no = text.count("\n", 0, start) + 1
        if token.startswith("//") or token.startswith("/*"):
            for idx in range(start, match.end()):
                if without_comments[idx] != "\n":
                    without_comments[idx] = " "
            continue
        for idx in range(start, match.end()):
            if without_comments[idx] != "\n":
                without_comments[idx] = " "
        prefix = text[max(0, start - 40) : start]
        if IMPORT_PREFIX.search(prefix) or CLASS_PREFIX.search(prefix):
            continue
        inner = token[1:-1]
        if token.startswith("`"):
            inner = re.sub(r"\$\{[^}]*\}", " ", inner)
        stripped = inner.strip()
        if not stripped or stripped.startswith(("http://", "https://", "/")):
            continue
        if SINGLE_TOKEN.match(stripped) and not UI_PROP_PREFIX.search(prefix):
            continue
        for offset, part in enumerate(inner.split("\n")):
            if part.strip():
                segments.append((line_no + offset, part))

    code_only = "".join(without_comments)
    for match in JSX_TEXT.finditer(code_only):
        chunk = match.group(1)
        if not re.search(r"[A-Za-z]", chunk) or NON_PROSE_JSX.search(chunk):
            continue
        line_no = code_only.count("\n", 0, match.start(1)) + 1
        segments.append((line_no, chunk))
    return segments


def iter_target_files(root: Path, paths: list[str]) -> list[Path]:
    collected: list[Path] = []
    seen: set[Path] = set()
    for rel in paths:
        candidate = (root / rel).resolve()
        if not candidate.exists():
            continue
        if candidate.is_file():
            files = [candidate]
        else:
            files = sorted(
                p
                for p in candidate.rglob("*")
                if p.is_file()
                and (
                    p.suffix == ".md"
                    or (p.suffix in {".ts", ".tsx"} and "node_modules" not in p.parts)
                )
            )
        for file_path in files:
            if file_path not in seen:
                seen.add(file_path)
                collected.append(file_path)
    return collected


def _is_excluded(rel_posix: str, exclude_files: tuple[str, ...]) -> bool:
    return any(rel_posix == pattern or rel_posix.endswith("/" + pattern) for pattern in exclude_files)


def check_files(
    root: Path = ROOT,
    paths: list[str] | None = None,
    rules_path: Path = DEFAULT_RULES,
) -> list[Violation]:
    default_paths, rules = load_rules(rules_path)
    selected_paths = default_paths if paths is None else paths
    violations: list[Violation] = []
    root_resolved = root.resolve()

    for file_path in iter_target_files(root_resolved, selected_paths):
        try:
            rel_posix = file_path.relative_to(root_resolved).as_posix()
        except ValueError:
            rel_posix = file_path.as_posix()
        raw = file_path.read_text(encoding="utf-8")
        raw_lines = raw.splitlines()
        is_test_ts = file_path.name.endswith((".test.ts", ".test.tsx"))

        for rule in rules:
            if _is_excluded(rel_posix, rule.exclude_files):
                continue
            if rule.scope == "all":
                for line_no, line in enumerate(raw_lines, start=1):
                    found = rule.regex.search(line)
                    if found:
                        violations.append(
                            Violation(rel_posix, line_no, rule.id, found.group(0), rule.replacement)
                        )
            elif rule.scope == "prose_and_ui":
                if file_path.suffix == ".md":
                    for line_no, line in enumerate(markdown_prose(raw).splitlines(), start=1):
                        found = rule.regex.search(line)
                        if found:
                            violations.append(
                                Violation(rel_posix, line_no, rule.id, found.group(0), rule.replacement)
                            )
                elif file_path.suffix in {".ts", ".tsx"} and not is_test_ts:
                    seen_line_rule: set[tuple[int, str]] = set()
                    for line_no, segment in ts_ui_segments(raw):
                        found = rule.regex.search(segment)
                        if found and (line_no, rule.id) not in seen_line_rule:
                            seen_line_rule.add((line_no, rule.id))
                            violations.append(
                                Violation(rel_posix, line_no, rule.id, found.group(0), rule.replacement)
                            )
    return sorted(violations, key=lambda item: (item.file, item.line, item.rule_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root directory")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES, help="Path to wording-rules.json")
    parser.add_argument("--paths", nargs="*", help="Optional paths relative to --root")
    args = parser.parse_args(argv)

    violations = check_files(root=args.root, paths=args.paths, rules_path=args.rules)
    if violations:
        for item in violations:
            print(
                f"{item.file}:{item.line}: [{item.rule_id}] matched {item.match!r} -> {item.replacement}",
                file=sys.stderr,
            )
        return 1
    checked = iter_target_files(args.root.resolve(), args.paths or load_rules(args.rules)[0])
    print(f"Wording check passed across {len(checked)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
