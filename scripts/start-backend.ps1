param(
    [int]$Port = 8000,
    [string]$RunnerUrl = "http://127.0.0.1:8091",
    [string]$BridgeUrl = "http://127.0.0.1:8090",
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$env:APP_RUNNER_URL = $RunnerUrl
$env:APP_CODEX_BRIDGE_URL = $BridgeUrl
$backendDir = (Resolve-Path (Join-Path $PSScriptRoot "..\backend")).Path
Push-Location $backendDir
try {
    $arguments = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", $Port)
    if ($Reload) { $arguments += "--reload" }
    & (Get-Command python -ErrorAction Stop).Source @arguments
    if ($LASTEXITCODE -ne 0) { throw "backend exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
