param(
    [switch]$UseMockCodex
)

$ErrorActionPreference = "Stop"
$env:CODEX_MOCK_MODE = if ($UseMockCodex) { "true" } else { "false" }
Push-Location "$PSScriptRoot\..\codex-bridge"
npm run dev
