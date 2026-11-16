[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TargetResourceId,
    [Parameter(Mandatory = $true)][string]$TargetSha256,
    [Parameter(Mandatory = $true)][string]$TenantId,
    [Parameter(Mandatory = $true)][string]$DeadlineUtc,
    [ValidateSet('Inspect', 'SuspendWhenDue')][string]$Mode = 'Inspect'
)

$ErrorActionPreference = 'Stop'
$pattern = '^/subscriptions/([0-9a-f-]{36})/resourceGroups/([^/]+)/providers/Microsoft\.Fabric/capacities/([a-z][a-z0-9]{2,62})$'
if ($TargetResourceId -notmatch $pattern) { throw 'Target is not one exact Fabric capacity ARM ID' }
$subscription = $Matches[1]
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($TargetResourceId.ToLowerInvariant())
    $observedHash = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
} finally {
    $sha.Dispose()
}
if ($observedHash -ne $TargetSha256) { throw 'Target differs from approved resource digest' }
if ($DeadlineUtc -notmatch '(Z|[+-]\d{2}:\d{2})$') { throw 'Deadline must carry an explicit timezone' }
$deadline = [DateTimeOffset]::Parse($DeadlineUtc).ToUniversalTime()
if ($Mode -eq 'SuspendWhenDue' -and [DateTimeOffset]::UtcNow -lt $deadline) {
    Write-Output '{"status":"NOT_DUE","mutations":0}'
    return
}

foreach ($command in 'Disable-AzContextAutosave', 'Connect-AzAccount', 'Set-AzContext', 'Invoke-AzRestMethod') {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required Az.Accounts command unavailable: $command"
    }
}
Disable-AzContextAutosave -Scope Process | Out-Null
$context = (Connect-AzAccount -Identity).Context
if ($context.Tenant.Id -ne $TenantId) { throw 'Managed identity tenant does not match approved target' }
Set-AzContext -SubscriptionId $subscription -TenantId $TenantId | Out-Null
$path = $TargetResourceId + '?api-version=2023-11-01'
$response = Invoke-AzRestMethod -Path $path -Method GET
if ($response.StatusCode -ne 200) { throw 'Capacity read failed' }
$capacity = $response.Content | ConvertFrom-Json
if ($capacity.id -ne $TargetResourceId -or $capacity.sku.name -ne 'F8') {
    throw 'Resource identity or SKU changed; new approval required'
}
if ($Mode -eq 'Inspect') {
    @{ status = 'INSPECTED'; state = $capacity.properties.state; mutations = 0 } |
        ConvertTo-Json -Compress
    return
}
if ($capacity.properties.state -in @('Paused', 'Suspended')) {
    Write-Output '{"status":"ALREADY_PAUSED","mutations":0}'
    return
}
if ($capacity.properties.state -ne 'Active') { throw 'Unexpected capacity lifecycle state' }

$response = Invoke-AzRestMethod -Path ($TargetResourceId + '/suspend?api-version=2023-11-01') -Method POST
if ($response.StatusCode -notin @(200, 202)) { throw 'Capacity suspension request failed' }
$stopAt = [DateTimeOffset]::UtcNow.AddMinutes(8)
do {
    $delay = 10
    if ($response.Headers -and $response.Headers['Retry-After']) {
        $delay = [int]($response.Headers['Retry-After'] | Select-Object -First 1)
    }
    if ([DateTimeOffset]::UtcNow.AddSeconds($delay) -ge $stopAt) {
        throw 'Suspension verification exceeded bounded deadline'
    }
    Start-Sleep -Seconds $delay
    $response = Invoke-AzRestMethod -Path $path -Method GET
    if ($response.StatusCode -ne 200) { throw 'Capacity suspension readback failed' }
    $capacity = $response.Content | ConvertFrom-Json
    if ($capacity.properties.state -in @('Paused', 'Suspended')) {
        Write-Output '{"status":"PAUSED_VERIFIED","mutations":1}'
        return
    }
    if ($capacity.properties.state -eq 'Failed') { throw 'Capacity entered failed state' }
} while ([DateTimeOffset]::UtcNow -lt $stopAt)
throw 'Capacity suspension not verified'
