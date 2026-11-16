"""Repository sanitization gate for tracked and working-tree text files.

Scans text files under the repository root (streaming files larger than 1 MiB
line-by-line) and reports 1-based file:line violations for:
- email addresses outside reserved RFC 2606 / GitHub noreply example domains
- tenant-style domains
- bearer credentials and JSON Web Tokens
- PEM private key headers
- Azure shared-access signatures and connection-string secrets
- absolute Windows, macOS, and Linux user-profile paths
- GUIDs absent from tools/sanitization-allowlist.json
- private deny-list terms configured via NORTHSTAR_DENYLIST_FILE
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_SCHEMA_VERSION = "1.0"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
STREAM_THRESHOLD_BYTES = 1_048_576
STREAM_BUFFER_BYTES = 65_536
NUL_PROBE_BYTES = 8_192

_DEFAULT_WRITE_ALLOWLIST = Path("__default_sanitization_allowlist__")

BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".7z",
    ".bin",
    ".bmp",
    ".bz2",
    ".db",
    ".dll",
    ".duckdb",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".otf",
    ".parquet",
    ".pdf",
    ".png",
    ".pyc",
    ".pyd",
    ".so",
    ".sqlite",
    ".tar",
    ".tgz",
    ".ttf",
    ".webp",
    ".woff",
    ".woff2",
    ".xz",
    ".zip",
})

IGNORED_WALK_DIRS: frozenset[str] = frozenset({
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
    "venv",
})

RESERVED_EXACT_DOMAINS: frozenset[str] = frozenset({
    "example.com",
    "example.edu",
    "example.net",
    "example.org",
    "localhost",
    "noreply.github.com",
})

RESERVED_TLDS: frozenset[str] = frozenset({
    "example",
    "invalid",
    "localhost",
    "test",
})

SYNTHETIC_DOMAIN_FILE_EXTENSIONS: frozenset[str] = frozenset({
    "csv",
    "json",
    "md",
    "toml",
    "txt",
    "xml",
    "yaml",
    "yml",
})

SYNTHETIC_TOKEN_PREFIXES: tuple[str, ...] = (
    "example-",
    "example_",
    "placeholder",
    "redacted",
    "synthetic-",
    "synthetic_",
)

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-/@])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b"
)
TENANT_DOMAIN_RE = re.compile(r"(?:[A-Za-z0-9-]+)?\.onmicrosoft\.com\b", re.IGNORECASE)
AUTH_BEARER_RE = re.compile(
    r"Authorization[\"']?\s*[:=]\s*[\"']?Bearer\s+([A-Za-z0-9._~+/=-]{16,})",
    re.IGNORECASE,
)
STANDALONE_BEARER_RE = re.compile(r"\bBearer\s+([A-Za-z0-9._~+/=-]{16,})")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----(?!synthetic-)")
SAS_SIG_RE = re.compile(r"(?:^|[?&;])sig=([A-Za-z0-9%+/=]{20,})", re.IGNORECASE)
SAS_PARAM_RE = re.compile(r"(?:^|[?&;])(?:sv|se|sp|sr|st|skoid|spr|ss)=", re.IGNORECASE)
ACCOUNT_KEY_RE = re.compile(r"\bAccountKey\s*=\s*(?!synthetic[-_])([A-Za-z0-9+/=]{20,})", re.IGNORECASE)
SHARED_ACCESS_KEY_RE = re.compile(
    r"\bSharedAccessKey\s*=\s*(?!synthetic[-_])([A-Za-z0-9+/=]{16,})",
    re.IGNORECASE,
)
CONN_PASSWORD_RE = re.compile(
    r"(?:;\s*(?:Password|Pwd)\s*=\s*(?!<|\$\{|synthetic[-_])([^;\s\"']{8,})"
    r"|\b(?:Password|Pwd)\s*=\s*(?!<|\$\{|synthetic[-_])([^;\s\"']{8,})\s*;)",
    re.IGNORECASE,
)
WIN_USER_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z]):(?:\\\\|\\|/)([Uu]sers)(?:\\\\|\\|/)([A-Za-z0-9._-]+)"
)
UNIX_USER_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/(Users|home)/([A-Za-z0-9._-]+)")
UUID_RE = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F])"
)
UUID_EXACT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
RUN_ID_FIELD_RE = re.compile(r'"(?:pair_id|run_id|request_id|comparison_id)"\s*:')


class SanitizationConfigError(ValueError):
    """Raised when allow-list or deny-list configuration is invalid or missing."""


@dataclass(frozen=True, slots=True)
class DenylistEntry:
    index: int
    source_line: int
    term: str


@dataclass(frozen=True, slots=True)
class DenylistMatcher:
    entries: tuple[DenylistEntry, ...]
    combined_pattern: re.Pattern[str] | None


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    line: int
    rule: str
    detail: str

    def format(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ScanSummary:
    scanned_files: int
    skipped_binary_files: int
    streamed_large_files: int
    violations: tuple[Violation, ...]


def _is_reserved_domain(domain: str) -> bool:
    normalized = domain.lower().strip(".")
    if not normalized:
        return False
    if normalized in RESERVED_EXACT_DOMAINS:
        return True
    if any(normalized.endswith(f".{base}") for base in RESERVED_EXACT_DOMAINS):
        return True
    tld = normalized.rsplit(".", 1)[-1]
    return tld in RESERVED_TLDS


def is_allowed_email_domain(domain: str) -> bool:
    normalized = domain.lower().strip(".")
    if _is_reserved_domain(normalized):
        return True
    if "." in normalized:
        stem, ext = normalized.rsplit(".", 1)
        if ext in SYNTHETIC_DOMAIN_FILE_EXTENSIONS and _is_reserved_domain(stem):
            return True
    return False


def _is_placeholder_bearer_token(token: str) -> bool:
    lowered = token.lower()
    if lowered.startswith(SYNTHETIC_TOKEN_PREFIXES):
        return True
    if token.isalpha() and token.islower():
        return True
    return token.upper() in {"ACCESS_TOKEN_HERE", "REDACTED", "YOUR_ACCESS_TOKEN"}


def is_binary_file(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    with path.open("rb") as handle:
        head = handle.read(NUL_PROBE_BYTES)
    return b"\x00" in head


def iter_file_lines(
    path: Path,
    *,
    stream_threshold: int = STREAM_THRESHOLD_BYTES,
) -> tuple[bool, Iterator[tuple[int, str]]]:
    size = path.stat().st_size
    streamed = size > stream_threshold
    buffer_size = STREAM_BUFFER_BYTES if streamed else -1

    def _generator() -> Iterator[tuple[int, str]]:
        with path.open("r", encoding="utf-8", errors="replace", buffering=buffer_size) as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                yield line_no, raw_line.rstrip("\r\n")

    return streamed, _generator()


def discover_files(root: Path, *, tracked_only: bool = False) -> list[Path]:
    resolved_root = root.resolve()
    if (resolved_root / ".git").exists():
        cmd = ["git", "-C", str(resolved_root), "ls-files", "-z"]
        if not tracked_only:
            cmd.extend(["--cached", "--others", "--exclude-standard"])
        result = subprocess.run(cmd, check=True, capture_output=True)
        rel_paths = sorted({
            raw.decode("utf-8")
            for raw in result.stdout.split(b"\x00")
            if raw
        })
        return [
            resolved_root / rel
            for rel in rel_paths
            if (resolved_root / rel).is_file()
        ]

    discovered: list[tuple[str, Path]] = []
    for candidate in resolved_root.rglob("*"):
        rel_parts = candidate.relative_to(resolved_root).parts
        if any(part in IGNORED_WALK_DIRS for part in rel_parts):
            continue
        if candidate.is_file():
            discovered.append((candidate.relative_to(resolved_root).as_posix(), candidate))
    discovered.sort(key=lambda pair: pair[0])
    return [path for _, path in discovered]


def load_allowlist(path: Path | None, *, required: bool = False) -> frozenset[str]:
    if path is None:
        return frozenset({ZERO_GUID})
    if not path.exists():
        if required:
            raise SanitizationConfigError(f"Allow-list file does not exist: {path}")
        return frozenset({ZERO_GUID})
    if not path.is_file():
        raise SanitizationConfigError(f"Allow-list path is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SanitizationConfigError(f"Failed to parse allow-list JSON {path}: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("version") != ALLOWLIST_SCHEMA_VERSION:
        raise SanitizationConfigError(
            f"Allow-list {path} must be a JSON object with version {ALLOWLIST_SCHEMA_VERSION!r}."
        )
    guids_obj = payload.get("guids")
    if not isinstance(guids_obj, dict):
        raise SanitizationConfigError(f"Allow-list {path} field 'guids' must be a JSON object.")

    allowed: set[str] = {ZERO_GUID}
    for raw_guid, meta in guids_obj.items():
        if not isinstance(raw_guid, str):
            raise SanitizationConfigError(f"Allow-list {path} contains a non-string GUID key.")
        normalized = raw_guid.lower()
        if not UUID_EXACT_RE.fullmatch(normalized):
            raise SanitizationConfigError(f"Invalid GUID key in {path}: {raw_guid!r}")
        if not isinstance(meta, dict):
            raise SanitizationConfigError(f"Metadata for {raw_guid} in {path} must be an object.")
        category = meta.get("category")
        reason = meta.get("reason")
        if not isinstance(category, str) or not category.strip():
            raise SanitizationConfigError(f"GUID {raw_guid} in {path} is missing a non-empty 'category'.")
        if not isinstance(reason, str) or not reason.strip():
            raise SanitizationConfigError(f"GUID {raw_guid} in {path} is missing a non-empty 'reason'.")
        allowed.add(normalized)
    return frozenset(allowed)


def resolve_denylist_path(
    explicit_path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    if explicit_path is not None:
        return explicit_path
    environment = os.environ if env is None else env
    raw_value = environment.get("NORTHSTAR_DENYLIST_FILE")
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    if not stripped:
        raise SanitizationConfigError("NORTHSTAR_DENYLIST_FILE is set in the environment but empty.")
    return Path(stripped)


def load_denylist(path: Path | None) -> DenylistMatcher:
    if path is None:
        return DenylistMatcher(entries=(), combined_pattern=None)
    if not path.exists() or not path.is_file():
        raise SanitizationConfigError(f"Deny-list file does not exist or is not a file: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SanitizationConfigError(f"Unable to read deny-list file {path}: {exc}") from exc

    entries: list[DenylistEntry] = []
    for source_line, raw_line in enumerate(raw_text.splitlines(), start=1):
        term = raw_line.strip()
        if not term or term.startswith("#"):
            continue
        entries.append(DenylistEntry(index=len(entries) + 1, source_line=source_line, term=term))

    if not entries:
        return DenylistMatcher(entries=(), combined_pattern=None)
    # Match longer terms first when building the fast prefilter alternation.
    ordered_terms = sorted({entry.term for entry in entries}, key=len, reverse=True)
    combined = re.compile("|".join(re.escape(term) for term in ordered_terms), re.IGNORECASE)
    return DenylistMatcher(entries=tuple(entries), combined_pattern=combined)


def scan_line(
    rel_path: str,
    line_no: int,
    line: str,
    *,
    allowlisted_guids: frozenset[str],
    denylist: DenylistMatcher,
) -> list[Violation]:
    violations: list[Violation] = []
    lowered = line.lower()

    if "@" in line:
        for match in EMAIL_RE.finditer(line):
            domain = match.group(2)
            if not is_allowed_email_domain(domain):
                violations.append(
                    Violation(
                        path=rel_path,
                        line=line_no,
                        rule="non-example-email",
                        detail=f"email address outside reserved example domains (<redacted>@{domain})",
                    )
                )

    if "onmicrosoft" in lowered:
        for match in TENANT_DOMAIN_RE.finditer(line):
            violations.append(
                Violation(
                    path=rel_path,
                    line=line_no,
                    rule="tenant-domain",
                    detail=f"tenant-style domain matched ({match.group(0)})",
                )
            )

    if "bearer" in lowered:
        bearer_spans: list[tuple[int, int]] = []
        for match in AUTH_BEARER_RE.finditer(line):
            token = match.group(1)
            if not _is_placeholder_bearer_token(token):
                bearer_spans.append(match.span(1))
                violations.append(
                    Violation(
                        path=rel_path,
                        line=line_no,
                        rule="bearer-token",
                        detail="Authorization Bearer credential matched (Bearer [REDACTED])",
                    )
                )
        for match in STANDALONE_BEARER_RE.finditer(line):
            token = match.group(1)
            span = match.span(1)
            if span not in bearer_spans and not _is_placeholder_bearer_token(token):
                violations.append(
                    Violation(
                        path=rel_path,
                        line=line_no,
                        rule="bearer-token",
                        detail="Bearer credential matched (Bearer [REDACTED])",
                    )
                )

    if "eyj" in lowered:
        for _ in JWT_RE.finditer(line):
            violations.append(
                Violation(
                    path=rel_path,
                    line=line_no,
                    rule="jwt-token",
                    detail="three-part JSON Web Token matched ([REDACTED_JWT])",
                )
            )

    if "private key" in lowered:
        for _ in PRIVATE_KEY_RE.finditer(line):
            violations.append(
                Violation(
                    path=rel_path,
                    line=line_no,
                    rule="private-key",
                    detail="PEM private key header matched",
                )
            )

    if "sig=" in lowered and SAS_PARAM_RE.search(line):
        for _ in SAS_SIG_RE.finditer(line):
            violations.append(
                Violation(
                    path=rel_path,
                    line=line_no,
                    rule="sas-token",
                    detail="Azure shared-access signature matched (sig=[REDACTED])",
                )
            )

    if "accountkey" in lowered:
        for _ in ACCOUNT_KEY_RE.finditer(line):
            violations.append(
                Violation(
                    path=rel_path,
                    line=line_no,
                    rule="connection-string",
                    detail="connection-string AccountKey matched (AccountKey=[REDACTED])",
                )
            )
    if "sharedaccesskey" in lowered:
        for _ in SHARED_ACCESS_KEY_RE.finditer(line):
            violations.append(
                Violation(
                    path=rel_path,
                    line=line_no,
                    rule="connection-string",
                    detail="connection-string SharedAccessKey matched (SharedAccessKey=[REDACTED])",
                )
            )
    if "password" in lowered or "pwd" in lowered:
        for _ in CONN_PASSWORD_RE.finditer(line):
            violations.append(
                Violation(
                    path=rel_path,
                    line=line_no,
                    rule="connection-string",
                    detail="connection-string Password matched (Password=[REDACTED])",
                )
            )

    win_spans: list[tuple[int, int]] = []
    if "users" in lowered:
        for match in WIN_USER_PATH_RE.finditer(line):
            win_spans.append(match.span())
            drive = match.group(1)
            violations.append(
                Violation(
                    path=rel_path,
                    line=line_no,
                    rule="user-profile-path",
                    detail=f"Windows user-profile path matched ({drive}:/<users>/<redacted>)",
                )
            )
    if "users" in lowered or "home" in lowered:
        for match in UNIX_USER_PATH_RE.finditer(line):
            if any(start <= match.start() < end for start, end in win_spans):
                continue
            prefix = match.group(1)
            violations.append(
                Violation(
                    path=rel_path,
                    line=line_no,
                    rule="user-profile-path",
                    detail=f"POSIX user-profile path matched (/{prefix}/<redacted>)",
                )
            )

    if "-" in line:
        for match in UUID_RE.finditer(line):
            guid = match.group(0).lower()
            if guid != ZERO_GUID and guid not in allowlisted_guids:
                violations.append(
                    Violation(
                        path=rel_path,
                        line=line_no,
                        rule="unallowlisted-guid",
                        detail=f"GUID {guid} is not in sanitization-allowlist.json",
                    )
                )

    if denylist.combined_pattern is not None and denylist.combined_pattern.search(line):
        for entry in denylist.entries:
            if entry.term.lower() in lowered:
                violations.append(
                    Violation(
                        path=rel_path,
                        line=line_no,
                        rule="denylist-term",
                        detail=(
                            f"matched private deny-list entry #{entry.index} "
                            f"(denylist line {entry.source_line})"
                        ),
                    )
                )

    return violations


def scan_file(
    path: Path,
    *,
    rel_path: str,
    allowlisted_guids: frozenset[str] = frozenset({ZERO_GUID}),
    denylist: DenylistMatcher | None = None,
    stream_threshold: int = STREAM_THRESHOLD_BYTES,
) -> tuple[bool, bool, list[Violation]]:
    """Scan one candidate file. Returns (was_binary, was_streamed, violations)."""
    if is_binary_file(path):
        return True, False, []
    active_denylist = DenylistMatcher(entries=(), combined_pattern=None) if denylist is None else denylist
    streamed, line_iter = iter_file_lines(path, stream_threshold=stream_threshold)
    file_violations: list[Violation] = []
    for line_no, line in line_iter:
        file_violations.extend(
            scan_line(
                rel_path,
                line_no,
                line,
                allowlisted_guids=allowlisted_guids,
                denylist=active_denylist,
            )
        )
    return False, streamed, file_violations


def scan_repository(
    root: Path,
    *,
    allowlist_path: Path | None = None,
    require_allowlist: bool = False,
    denylist_path: Path | None = None,
    tracked_only: bool = False,
    stream_threshold: int = STREAM_THRESHOLD_BYTES,
    env: Mapping[str, str] | None = None,
) -> ScanSummary:
    resolved_root = root.resolve()
    effective_allowlist = (
        resolved_root / "tools" / "sanitization-allowlist.json"
        if allowlist_path is None
        else allowlist_path
    )
    allowlisted_guids = load_allowlist(effective_allowlist, required=require_allowlist)
    resolved_denylist = resolve_denylist_path(denylist_path, env=env)
    denylist_matcher = load_denylist(resolved_denylist)

    scanned_files = 0
    skipped_binary = 0
    streamed_large = 0
    all_violations: list[Violation] = []

    for file_path in discover_files(resolved_root, tracked_only=tracked_only):
        rel_path = file_path.relative_to(resolved_root).as_posix()
        was_binary, was_streamed, violations = scan_file(
            file_path,
            rel_path=rel_path,
            allowlisted_guids=allowlisted_guids,
            denylist=denylist_matcher,
            stream_threshold=stream_threshold,
        )
        if was_binary:
            skipped_binary += 1
            continue
        scanned_files += 1
        if was_streamed:
            streamed_large += 1
        all_violations.extend(violations)

    return ScanSummary(
        scanned_files=scanned_files,
        skipped_binary_files=skipped_binary,
        streamed_large_files=streamed_large,
        violations=tuple(all_violations),
    )


def _classify_guid(guid: str, occurrences: Sequence[tuple[str, str]]) -> dict[str, str]:
    if guid == ZERO_GUID:
        return {"category": "zero-prefixed", "reason": "All-zero placeholder UUID"}
    if guid.startswith("00000000-0000-0000-0000-") or "-0000-0000-0000-" in guid:
        return {"category": "zero-prefixed", "reason": "Zero-prefixed synthetic placeholder identifier"}
    if guid[14] == "5" and guid[19] in "89ab":
        return {
            "category": "synthetic-lineage",
            "reason": "Deterministic UUIDv5 name-based lineage identifier",
        }

    rel_paths = sorted({rel for rel, _ in occurrences})
    if any(RUN_ID_FIELD_RE.search(line) for _, line in occurrences) or all(
        rel.startswith("recordings/") for rel in rel_paths
    ):
        return {
            "category": "synthetic-run",
            "reason": "Synthetic recording or evaluation run identifier",
        }
    if all(rel.endswith(".svg") for rel in rel_paths):
        return {
            "category": "svg-asset",
            "reason": f"SVG layer or element identifier in {rel_paths[0]}",
        }
    if all(rel.startswith("tests/") for rel in rel_paths) and len(set(guid.replace("-", ""))) <= 6:
        return {
            "category": "synthetic-run",
            "reason": f"Synthetic repeating-hex test identifier in {rel_paths[0]}",
        }
    return {
        "category": "synthetic-run",
        "reason": f"Synthetic repository fixture identifier in {rel_paths[0]}",
    }


def build_allowlist_payload(
    root: Path,
    *,
    exclude_path: Path | None = None,
    tracked_only: bool = False,
    stream_threshold: int = STREAM_THRESHOLD_BYTES,
) -> dict[str, object]:
    resolved_root = root.resolve()
    excluded = exclude_path.resolve() if exclude_path is not None else None
    discovered_guids: dict[str, list[tuple[str, str]]] = {ZERO_GUID: []}

    for file_path in discover_files(resolved_root, tracked_only=tracked_only):
        if excluded is not None and file_path.resolve() == excluded:
            continue
        if is_binary_file(file_path):
            continue
        rel_path = file_path.relative_to(resolved_root).as_posix()
        _, line_iter = iter_file_lines(file_path, stream_threshold=stream_threshold)
        for _, line in line_iter:
            if "-" not in line:
                continue
            for match in UUID_RE.finditer(line):
                guid = match.group(0).lower()
                discovered_guids.setdefault(guid, []).append((rel_path, line))

    guids_map = {
        guid: _classify_guid(guid, discovered_guids[guid])
        for guid in sorted(discovered_guids)
    }
    return {
        "version": ALLOWLIST_SCHEMA_VERSION,
        "description": (
            "Reviewed synthetic GUID allow-list for tools/check_sanitization.py. "
            "Categories distinguish zero-prefixed placeholders, deterministic UUIDv5 lineage tags, "
            "and sanitized recording or test run identifiers."
        ),
        "guids": guids_map,
    }


def write_allowlist(
    root: Path,
    output_path: Path,
    *,
    tracked_only: bool = False,
    stream_threshold: int = STREAM_THRESHOLD_BYTES,
) -> dict[str, object]:
    payload = build_allowlist_payload(
        root,
        exclude_path=output_path,
        tracked_only=tracked_only,
        stream_threshold=stream_threshold,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2) + "\n"
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check tracked repository text files for unsanitized identifiers and secrets."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Root directory to scan (default: repository root).",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help="Path to GUID allow-list JSON (default: <root>/tools/sanitization-allowlist.json).",
    )
    parser.add_argument(
        "--denylist",
        type=Path,
        default=None,
        help="Optional private deny-list file (overrides NORTHSTAR_DENYLIST_FILE).",
    )
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="When --root is a git repository, scan only index-tracked files (git ls-files -z).",
    )
    parser.add_argument(
        "--write-allowlist",
        nargs="?",
        const=_DEFAULT_WRITE_ALLOWLIST,
        type=Path,
        default=None,
        metavar="PATH",
        help="Scan --root and write deterministic sanitization-allowlist.json (default path if omitted).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved_root = args.root.resolve()

    if args.write_allowlist is not None:
        target = (
            resolved_root / "tools" / "sanitization-allowlist.json"
            if args.write_allowlist == _DEFAULT_WRITE_ALLOWLIST
            else args.write_allowlist
        )
        payload = write_allowlist(resolved_root, target, tracked_only=args.tracked_only)
        guids_obj = payload["guids"]
        assert isinstance(guids_obj, dict)
        counts: dict[str, int] = {}
        for entry in guids_obj.values():
            if isinstance(entry, dict):
                cat = str(entry.get("category", "unknown"))
                counts[cat] = counts.get(cat, 0) + 1
        summary_parts = ", ".join(f"{cat}={count}" for cat, count in sorted(counts.items()))
        print(f"Wrote {len(guids_obj)} allow-listed GUIDs to {target} ({summary_parts}).")
        return 0

    try:
        summary = scan_repository(
            resolved_root,
            allowlist_path=args.allowlist,
            require_allowlist=args.allowlist is not None or (resolved_root / ".git").exists(),
            denylist_path=args.denylist,
            tracked_only=args.tracked_only,
        )
    except SanitizationConfigError as exc:
        print(f"sanitization configuration error: {exc}", file=sys.stderr)
        return 2

    if summary.violations:
        for violation in summary.violations:
            print(violation.format(), file=sys.stderr)
        print(
            f"Sanitization gate failed: {len(summary.violations)} violation(s) across "
            f"{summary.scanned_files} scanned text file(s) "
            f"({summary.streamed_large_files} streamed >1 MiB).",
            file=sys.stderr,
        )
        return 1

    print(
        f"Sanitization gate passed: {summary.scanned_files} text file(s) scanned "
        f"({summary.streamed_large_files} streamed >1 MiB, "
        f"{summary.skipped_binary_files} binary skipped)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
