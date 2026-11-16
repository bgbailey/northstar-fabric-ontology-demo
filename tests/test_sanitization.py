"""Offline unit tests for tools/check_sanitization.py.

All violation payloads are assembled dynamically at runtime so this test module
contains zero literal sanitization violations when scanned by the gate itself.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from tools import check_sanitization as gate

ROOT = Path(__file__).resolve().parents[1]


def _write_utf8(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def _build_jwt() -> str:
    header = "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    payload = "eyJ" + "zdWIiOiJzeW50aGV0aWMtdXNlciIsImlhdCI6MTcwMDAwMDAwMH0"
    signature = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    return f"{header}.{payload}.{signature}"


def test_gate_self_scans_own_source_and_tests_with_lf_no_bom() -> None:
    targets = [
        ROOT / "tools" / "check_sanitization.py",
        ROOT / "tests" / "test_sanitization.py",
    ]
    for target in targets:
        raw = target.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"Unexpected UTF-8 BOM in {target}"
        assert b"\r\n" not in raw, f"Expected LF line endings in {target}"
        was_binary, _, violations = gate.scan_file(
            target,
            rel_path=target.relative_to(ROOT).as_posix(),
        )
        assert not was_binary
        assert violations == []


def test_email_detector_flags_real_domains_and_allows_reserved_or_package_scopes(tmp_path: Path) -> None:
    leaked_email = "alice.operator" + "@" + "contoso" + ".com"
    second_email = "billing" + "@" + "fabrikam" + ".io"
    _write_utf8(
        tmp_path / "contacts.md",
        [
            "import Graph from '@xyflow/react';",
            "uses: actions/checkout@v4",
            "@pytest.mark.parametrize('x', [1])",
            "Allowed RFC 2606: ops" + "@" + "example.com and team" + "@" + "northstar.example",
            "Allowed trailer: 223556219+Copilot" + "@" + "users.noreply.github.com",
            f"Leaked contact: {leaked_email}",
            f"Another contact: {second_email}",
        ],
    )

    summary = gate.scan_repository(tmp_path)
    assert [(v.path, v.line, v.rule) for v in summary.violations] == [
        ("contacts.md", 6, "non-example-email"),
        ("contacts.md", 7, "non-example-email"),
    ]
    assert "alice.operator" not in summary.violations[0].detail


def test_tenant_domain_violation_reports_exact_file_and_line(tmp_path: Path) -> None:
    tenant_host = "northstar-demo" + "." + "onmicrosoft" + ".com"
    _write_utf8(
        tmp_path / "config" / "tenant.json",
        [
            "{",
            '  "environment": "demo tenant",',
            f'  "issuerHost": "{tenant_host}"',
            "}",
        ],
    )

    summary = gate.scan_repository(tmp_path)
    assert len(summary.violations) == 1
    violation = summary.violations[0]
    assert (violation.path, violation.line, violation.rule) == (
        "config/tenant.json",
        3,
        "tenant-domain",
    )


def test_bearer_token_and_jwt_violations_and_placeholder_exemptions(tmp_path: Path) -> None:
    raw_bearer = "Authorization: " + "Bearer " + ("Ab9xK2mP" * 3)
    json_bearer = '"Authorization": ' + '"Bearer ' + ("Zx7qR4nL" * 3) + '"'
    jwt_value = _build_jwt()
    _write_utf8(
        tmp_path / "captures" / "headers.txt",
        [
            "Authorization: Bearer <TOKEN>",
            "Bearer synthetic-unit-token-allowed",
            raw_bearer,
            json_bearer,
            f"id_token={jwt_value}",
        ],
    )

    summary = gate.scan_repository(tmp_path)
    assert [(v.path, v.line, v.rule) for v in summary.violations] == [
        ("captures/headers.txt", 3, "bearer-token"),
        ("captures/headers.txt", 4, "bearer-token"),
        ("captures/headers.txt", 5, "jwt-token"),
    ]
    for violation in summary.violations:
        assert "Ab9xK2mP" not in violation.detail
        assert "Zx7qR4nL" not in violation.detail
        assert "SflKxwRJ" not in violation.detail


def test_pem_private_key_headers_detected_with_file_and_line(tmp_path: Path) -> None:
    rsa_header = "-----BEGIN " + "RSA " + "PRIVATE KEY-----"
    pkcs8_header = "-----BEGIN " + "PRIVATE KEY-----"
    _write_utf8(
        tmp_path / "keys" / "sample.pem",
        [
            "# Safe header comment",
            rsa_header,
            "MIIEpAIBAAKCAQEA0123456789",
            pkcs8_header,
        ],
    )

    summary = gate.scan_repository(tmp_path)
    assert [(v.path, v.line, v.rule) for v in summary.violations] == [
        ("keys/sample.pem", 2, "private-key"),
        ("keys/sample.pem", 4, "private-key"),
    ]


def test_sas_signature_and_connection_string_violations(tmp_path: Path) -> None:
    sas_url = (
        "https://northstar.blob.core.windows.net/drop/file.parquet"
        "?sv=2024-11-04&se=2026-12-31T00%3A00%3A00Z&sp=r&"
        + "sig="
        + ("AbCdEf012345" * 3)
        + "%2B%3D"
    )
    storage_conn = (
        "DefaultEndpointsProtocol=https;AccountName=northstar;"
        + "AccountKey="
        + ("QmFzZTY0S2V5" * 3)
        + "=="
    )
    bus_conn = (
        "Endpoint=sb://northstar.servicebus.windows.net/;SharedAccessKeyName=Root;"
        + "SharedAccessKey="
        + ("U2VjcmV0S2V5" * 2)
        + "="
    )
    sql_conn = (
        "Server=tcp:northstar.database.windows.net,1433;Uid=reader;"
        + "Password="
        + "S3cretSqlPass!"
    )
    _write_utf8(
        tmp_path / "infra" / "secrets.conf",
        [
            "# Safe query string without SAS signature: ?sv=2024-11-04&sp=r",
            sas_url,
            storage_conn,
            bus_conn,
            sql_conn,
        ],
    )

    summary = gate.scan_repository(tmp_path)
    assert [(v.path, v.line, v.rule) for v in summary.violations] == [
        ("infra/secrets.conf", 2, "sas-token"),
        ("infra/secrets.conf", 3, "connection-string"),
        ("infra/secrets.conf", 4, "connection-string"),
        ("infra/secrets.conf", 5, "connection-string"),
    ]


def test_user_profile_paths_detected_across_windows_macos_and_linux(tmp_path: Path) -> None:
    win_backslash = "C:" + "\\Users\\" + "alice\\Documents\\notes.md"
    win_escaped = '{"cwd": "C:' + "\\\\Users\\\\" + 'alice\\\\repo"}'
    win_forward = "D:" + "/Users/" + "bob.smith/settings.json"
    mac_path = "/" + "Users/" + "carol/.azure/msal_token_cache.json"
    linux_path = "/" + "home/" + "dave/.config/credentials"
    _write_utf8(
        tmp_path / "docs" / "paths.txt",
        [
            "Relative slash words like source/home/measure must not false-positive.",
            win_backslash,
            win_escaped,
            win_forward,
            mac_path,
            linux_path,
        ],
    )

    summary = gate.scan_repository(tmp_path)
    assert [(v.path, v.line, v.rule) for v in summary.violations] == [
        ("docs/paths.txt", 2, "user-profile-path"),
        ("docs/paths.txt", 3, "user-profile-path"),
        ("docs/paths.txt", 4, "user-profile-path"),
        ("docs/paths.txt", 5, "user-profile-path"),
        ("docs/paths.txt", 6, "user-profile-path"),
    ]
    for violation in summary.violations:
        for username in ("alice", "bob.smith", "carol", "dave"):
            assert username not in violation.detail


def test_guid_allowlist_and_write_allowlist_cli_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    zero_prefixed = "00000000-0000-0000-0000-" + "000000000042"
    lineage_guid = str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:northstar:test:measure"))
    run_guid = str(uuid.UUID(int=0x4BAA7C396AA64B6694567EC244360BE0))
    svg_guid = str(uuid.UUID(int=0xF8D4D3927C124BD9BAFF66FBF7814B91))
    rogue_guid = str(uuid.UUID(int=0x1234567890ABCDEF1234567890ABCDEF))

    _write_utf8(
        tmp_path / "fabric" / "model.tmdl",
        [
            f"zeroPlaceholder: {gate.ZERO_GUID}",
            f"syntheticTarget: {zero_prefixed}",
            f"lineageTag: {lineage_guid.upper()}",
        ],
    )
    _write_utf8(
        tmp_path / "recordings" / "sample.json",
        [
            "{",
            f'  "pair_id": "{run_guid}"',
            "}",
        ],
    )
    _write_utf8(
        tmp_path / "docs" / "icon.svg",
        [
            f'<svg id="uuid-{svg_guid}" viewBox="0 0 18 18"></svg>',
        ],
    )

    allowlist_path = tmp_path / "tools" / "sanitization-allowlist.json"
    exit_write = gate.main(["--root", str(tmp_path), "--write-allowlist", str(allowlist_path)])
    assert exit_write == 0
    capsys.readouterr()

    written = json.loads(allowlist_path.read_text(encoding="utf-8"))
    assert written["version"] == "1.0"
    guids = written["guids"]
    assert list(guids) == sorted(guids)
    assert guids[gate.ZERO_GUID]["category"] == "zero-prefixed"
    assert guids[zero_prefixed]["category"] == "zero-prefixed"
    assert guids[lineage_guid]["category"] == "synthetic-lineage"
    assert guids[run_guid]["category"] == "synthetic-run"
    assert guids[svg_guid]["category"] == "svg-asset"

    assert gate.main(["--root", str(tmp_path), "--allowlist", str(allowlist_path)]) == 0
    capsys.readouterr()

    _write_utf8(
        tmp_path / "recordings" / "unreviewed.json",
        [
            "{",
            '  "status": "ok",',
            f'  "workspaceId": "{rogue_guid}"',
            "}",
        ],
    )
    exit_fail = gate.main(["--root", str(tmp_path), "--allowlist", str(allowlist_path)])
    captured = capsys.readouterr()
    assert exit_fail == 1
    assert f"recordings/unreviewed.json:3: unallowlisted-guid: GUID {rogue_guid}" in captured.err


def test_large_recording_over_one_megabyte_streams_and_reports_exact_tail_line(tmp_path: Path) -> None:
    recording = tmp_path / "recordings" / "large-capture.json"
    recording.parent.mkdir(parents=True, exist_ok=True)
    seed_line = 22_500
    total_lines = 23_000
    leaked_email = "tail.leak" + "@" + "customer-corp" + ".com"

    with recording.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("[\n")
        for idx in range(2, total_lines):
            if idx == seed_line:
                handle.write(f'  {{"seq": {idx}, "owner": "{leaked_email}"}},\n')
            else:
                padding = "x" * 48
                handle.write(f'  {{"seq": {idx}, "payload": "{padding}"}},\n')
        handle.write('  {"seq": 23000, "done": true}\n')
        handle.write("]\n")

    assert recording.stat().st_size > 1_250_000
    summary = gate.scan_repository(tmp_path)
    assert summary.streamed_large_files == 1
    assert [(v.path, v.line, v.rule) for v in summary.violations] == [
        ("recordings/large-capture.json", seed_line, "non-example-email"),
    ]


def test_binary_files_skipped_while_svg_and_json_are_scanned(tmp_path: Path) -> None:
    leaked = "binary.leak" + "@" + "contoso" + ".com"
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + leaked.encode("utf-8"))
    (tmp_path / "table.parquet").write_bytes(b"PAR1" + leaked.encode("utf-8"))
    (tmp_path / "nul_blob.dat").write_bytes(b"header\x00" + leaked.encode("utf-8"))
    _write_utf8(
        tmp_path / "diagram.svg",
        [
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">',
            f"  <text>{leaked}</text>",
            "</svg>",
        ],
    )

    summary = gate.scan_repository(tmp_path)
    assert summary.skipped_binary_files == 3
    assert summary.scanned_files == 1
    assert [(v.path, v.line, v.rule) for v in summary.violations] == [
        ("diagram.svg", 2, "non-example-email"),
    ]


def test_northstar_denylist_file_matches_case_insensitively_without_echoing_term_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_code_name = "Project-" + "Hyperion-" + "Internal"
    denylist_file = _write_utf8(
        tmp_path / "private-denylist.txt",
        [
            "# Private internal terms (never committed)",
            "",
            f"  {secret_code_name}  ",
        ],
    )
    repo_dir = tmp_path / "repo"
    _write_utf8(
        repo_dir / "docs" / "notes.md",
        [
            "# Public architecture overview",
            "Plain-language summary.",
            f"Reference to {secret_code_name.lower()} in draft paragraph.",
        ],
    )

    monkeypatch.setenv("NORTHSTAR_DENYLIST_FILE", str(denylist_file))
    exit_code = gate.main(["--root", str(repo_dir)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "docs/notes.md:3: denylist-term:" in captured.err
    assert secret_code_name.lower() not in captured.err.lower()
    assert secret_code_name.lower() not in captured.out.lower()

    missing_path = tmp_path / "does-not-exist-denylist.txt"
    monkeypatch.setenv("NORTHSTAR_DENYLIST_FILE", str(missing_path))
    missing_exit = gate.main(["--root", str(repo_dir)])
    missing_captured = capsys.readouterr()
    assert missing_exit == 2
    assert "configuration error" in missing_captured.err

    monkeypatch.setenv("NORTHSTAR_DENYLIST_FILE", "   ")
    empty_exit = gate.main(["--root", str(repo_dir)])
    assert empty_exit == 2


def test_default_write_allowlist_flag_and_invalid_allowlist_fail_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sample_guid = str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:northstar:default-write-test"))
    _write_utf8(tmp_path / "model.tmdl", [f"lineageTag: {sample_guid}"])

    # Invoking --write-allowlist with no path writes <root>/tools/sanitization-allowlist.json.
    assert gate.main(["--root", str(tmp_path), "--write-allowlist"]) == 0
    default_allowlist = tmp_path / "tools" / "sanitization-allowlist.json"
    assert default_allowlist.is_file()
    assert gate.main(["--root", str(tmp_path)]) == 0
    capsys.readouterr()

    # Missing explicit --allowlist fails closed.
    missing_allowlist = tmp_path / "missing-allowlist.json"
    assert gate.main(["--root", str(tmp_path), "--allowlist", str(missing_allowlist)]) == 2
    capsys.readouterr()

    # Malformed allow-list schema fails closed.
    bad_allowlist = _write_utf8(tmp_path / "bad-allowlist.json", ['{"version": "0.0", "guids": []}'])
    assert gate.main(["--root", str(tmp_path), "--allowlist", str(bad_allowlist)]) == 2
    captured = capsys.readouterr()
    assert "configuration error" in captured.err


def test_git_repository_discovery_and_tracked_only_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write_utf8(tmp_path / ".gitignore", ["ignored.txt"])
    _write_utf8(tmp_path / "ignored.txt", ["ignored.user" + "@" + "contoso" + ".com"])
    _write_utf8(tmp_path / "tracked.md", ["Clean tracked file with ops" + "@" + "example.com"])
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore", "tracked.md"], check=True)

    # Git repo without sanitization-allowlist.json fails closed until generated.
    assert gate.main(["--root", str(tmp_path)]) == 2
    capsys.readouterr()

    assert gate.main(["--root", str(tmp_path), "--write-allowlist"]) == 0
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "tools/sanitization-allowlist.json"],
        check=True,
    )
    capsys.readouterr()

    # Ignored file is excluded; tracked files pass.
    assert gate.main(["--root", str(tmp_path)]) == 0
    capsys.readouterr()

    # Unignored untracked file is caught by default, skipped under --tracked-only.
    _write_utf8(tmp_path / "untracked.md", ["leak" + "@" + "contoso" + ".com"])
    assert gate.main(["--root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "untracked.md:1: non-example-email:" in captured.err
    assert gate.main(["--root", str(tmp_path), "--tracked-only"]) == 0


