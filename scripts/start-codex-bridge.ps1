param(
    [switch]$UseMockCodex,
    [string]$CtfctlBackendUrl,
    [string]$CtfctlAccessKey,
    [int]$Port = 8090
)

$ErrorActionPreference = "Stop"
if (-not $CtfctlBackendUrl) {
    $CtfctlBackendUrl = if ($env:CTFCTL_BACKEND_URL) { $env:CTFCTL_BACKEND_URL } else { "http://127.0.0.1:8000" }
}
if (-not $CtfctlAccessKey) {
    $CtfctlAccessKey = if ($env:CTFCTL_ACCESS_KEY) { $env:CTFCTL_ACCESS_KEY } else { "development-ctfctl-access-key" }
}
$env:CODEX_MOCK_MODE = if ($UseMockCodex) { "true" } else { "false" }
$env:CODEX_BRIDGE_PORT = "$Port"
$env:CTFCTL_BACKEND_URL = $CtfctlBackendUrl
$env:CTFCTL_ACCESS_KEY = $CtfctlAccessKey
$bridgeDir = (Resolve-Path (Join-Path $PSScriptRoot "..\codex-bridge")).Path
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
Push-Location $bridgeDir
try {
    & $npm run dev
    if ($LASTEXITCODE -ne 0) { throw "codex bridge exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
