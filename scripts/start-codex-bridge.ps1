param(
    [switch]$UseMockCodex,
    [string]$CtfctlBackendUrl,
    [string]$CtfctlAccessKey
)

$ErrorActionPreference = "Stop"
if (-not $CtfctlBackendUrl) {
    $CtfctlBackendUrl = if ($env:CTFCTL_BACKEND_URL) { $env:CTFCTL_BACKEND_URL } else { "http://127.0.0.1:18000" }
}
if (-not $CtfctlAccessKey) {
    $CtfctlAccessKey = if ($env:CTFCTL_ACCESS_KEY) { $env:CTFCTL_ACCESS_KEY } else { "development-ctfctl-access-key" }
}
$env:CODEX_MOCK_MODE = if ($UseMockCodex) { "true" } else { "false" }
$env:CTFCTL_BACKEND_URL = $CtfctlBackendUrl
$env:CTFCTL_ACCESS_KEY = $CtfctlAccessKey
Push-Location "$PSScriptRoot\..\codex-bridge"
npm run dev
