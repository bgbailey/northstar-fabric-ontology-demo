import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGET = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/synthetic/providers/Microsoft.Fabric/capacities/fixture"
TENANT = "00000000-0000-0000-0000-000000000002"


def invoke(mode="Inspect", deadline="2000-01-01T00:00:00Z", target_hash=None, state="Active"):
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell (pwsh) is required to test infra/pause-capacity.ps1")
    code = r"""
$global:calls = @()
function Disable-AzContextAutosave { param($Scope) }
function Connect-AzAccount { param([switch]$Identity); @{ Context = @{ Tenant = @{ Id = $env:TEST_TENANT } } } }
function Set-AzContext { param($SubscriptionId, $TenantId) }
function Start-Sleep { param($Seconds) }
function Invoke-AzRestMethod {
    param($Path, $Method)
    $global:calls += "$Method $Path"
    if ($Method -eq 'POST') { return @{ StatusCode = 202; Headers = @{ 'Retry-After' = '0' } } }
    $state = if ($global:calls.Count -gt 2) { 'Paused' } else { $env:TEST_STATE }
    return @{ StatusCode = 200; Content = (@{ id = $env:TEST_TARGET; sku = @{ name = 'F8' }; properties = @{ state = $state } } | ConvertTo-Json -Depth 5) }
}
try {
    & $env:TEST_SCRIPT -TargetResourceId $env:TEST_TARGET -TargetSha256 $env:TEST_HASH -TenantId $env:TEST_TENANT -DeadlineUtc $env:TEST_DEADLINE -Mode $env:TEST_MODE
    @{ calls = $global:calls } | ConvertTo-Json -Compress
} catch {
    Write-Error $_
    exit 1
}
"""
    import os
    env = os.environ | {
        "TEST_TARGET": TARGET, "TEST_TENANT": TENANT,
        "TEST_SCRIPT": str(ROOT / "infra" / "pause-capacity.ps1"),
        "TEST_HASH": target_hash or hashlib.sha256(TARGET.lower().encode()).hexdigest(),
        "TEST_DEADLINE": deadline, "TEST_MODE": mode, "TEST_STATE": state,
    }
    return subprocess.run(["pwsh", "-NoProfile", "-Command", code], env=env, capture_output=True, text=True)


def test_cloud_runbook_inspect_never_mutates():
    result = invoke()
    assert result.returncode == 0, result.stderr
    assert "INSPECTED" in result.stdout and "POST" not in result.stdout


def test_cloud_runbook_not_due_does_not_even_connect():
    result = invoke(mode="SuspendWhenDue", deadline="2099-01-01T00:00:00Z")
    assert result.returncode == 0
    assert "NOT_DUE" in result.stdout and '"calls":[]' in result.stdout


def test_cloud_runbook_due_suspends_exact_target_and_reads_back():
    result = invoke(mode="SuspendWhenDue")
    assert result.returncode == 0, result.stderr
    assert "PAUSED_VERIFIED" in result.stdout
    assert result.stdout.count("POST ") == 1
    assert "/suspend?api-version=2023-11-01" in result.stdout


@pytest.mark.parametrize("options", [
    {"target_hash": "not-approved"},
    {"deadline": "2026-10-01T11:30:00"},
    {"mode": "SuspendWhenDue", "state": "Provisioning"},
])
def test_cloud_runbook_fails_closed(options):
    result = invoke(**options)
    assert result.returncode != 0
    assert "PAUSED_VERIFIED" not in result.stdout


def test_role_does_not_grant_shared_resource_delete_or_resume():
    proposal = json.loads((ROOT / "infra" / "lifecycle-proposal.example.json").read_text(encoding="utf-8"))
    assert proposal["customRoleActions"] == [
        "Microsoft.Fabric/capacities/read", "Microsoft.Fabric/capacities/suspend/action",
    ]
    assert proposal["status"] == "UNAPPROVED"
