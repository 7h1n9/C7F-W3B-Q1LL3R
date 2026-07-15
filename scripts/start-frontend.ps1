param(
    [int]$Port = 5173,
    [string]$BackendUrl = "http://127.0.0.1:8000/api/v1"
)

$ErrorActionPreference = "Stop"
$env:VITE_API_BASE_URL = $BackendUrl.TrimEnd("/")
$frontendDir = (Resolve-Path (Join-Path $PSScriptRoot "..\frontend")).Path
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
Push-Location $frontendDir
try {
    & $npm run dev -- --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw "frontend exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
