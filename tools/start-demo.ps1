[CmdletBinding(DefaultParameterSetName = 'Reader')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Offline')]
    [switch]$Offline,
    [Parameter(Mandatory = $true, ParameterSetName = 'Recorded')]
    [string]$RecordedNative,
    [Parameter(Mandatory = $true, ParameterSetName = 'RecordedComparisons')]
    [string]$RecordedComparisons,
    [Parameter(Mandatory = $true, ParameterSetName = 'RecordedLibrary')]
    [string]$RecordedLibrary,
    [Parameter(ParameterSetName = 'Reader')]
    [string]$ReaderConfig = (Join-Path $env:LOCALAPPDATA 'TelcoIQ\reader\reader-config.json'),
    [Parameter(ParameterSetName = 'Reader')]
    [string]$LiveConfig,
    [ValidateRange(1,65535)][int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Create the project virtual environment using README.md first' }
if (-not (Test-Path (Join-Path $root 'apps\web\dist\index.html'))) {
    throw 'Build apps\web using npm run build first'
}
Set-Location $root
if ($PSCmdlet.ParameterSetName -eq 'RecordedLibrary') {
    if (-not (Test-Path $RecordedLibrary)) { throw 'Recorded two-stream demo library is missing; no fallback' }
    & $python -m telco_iq.cli serve --recorded-library $RecordedLibrary --port $Port
} elseif ($PSCmdlet.ParameterSetName -eq 'RecordedComparisons') {
    if (-not (Test-Path $RecordedComparisons)) { throw 'Recorded comparison packet is missing; no fallback' }
    & $python -m telco_iq.cli serve --recorded-comparisons $RecordedComparisons --port $Port
} elseif ($PSCmdlet.ParameterSetName -eq 'Recorded') {
    if (-not (Test-Path $RecordedNative)) { throw 'Recorded native packet is missing; no fallback' }
    & $python -m telco_iq.cli serve --recorded-native $RecordedNative --port $Port
} elseif ($PSCmdlet.ParameterSetName -eq 'Offline') {
    & $python -m telco_iq.cli serve --port $Port
} else {
    if (-not (Test-Path $ReaderConfig)) {
        throw 'Approved reader configuration is missing. No silent offline fallback. Use -Offline explicitly.'
    }
    $arguments = @('-m','telco_iq.cli','serve','--reader-config',$ReaderConfig,'--port',"$Port")
    if ($LiveConfig) {
        if (-not (Test-Path $LiveConfig)) { throw 'Live comparison config is missing; no fixture fallback' }
        $arguments += @('--live-config',$LiveConfig)
    }
    & $python @arguments
}
if ($LASTEXITCODE -ne 0) { throw 'Demo server stopped with an error' }
