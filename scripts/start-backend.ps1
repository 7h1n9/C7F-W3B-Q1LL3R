param(
    [int]$Port = 8000,
    [string]$RunnerUrl = "http://192.168.236.128:8091",
    [string]$BridgeUrl = "http://127.0.0.1:8090",
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvDir = Join-Path $repoRoot ".venv3.11"
$activateScript = Join-Path $venvDir "Scripts\Activate.ps1"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $activateScript)) {
    throw "Python virtual environment activation script not found: $activateScript"
}
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python executable not found in virtual environment: $pythonExe"
}

. $activateScript

$env:APP_RUNNER_URL = $RunnerUrl
$env:APP_CODEX_BRIDGE_URL = $BridgeUrl
$backendDir = (Resolve-Path (Join-Path $PSScriptRoot "..\backend")).Path
Push-Location $backendDir
try {
    $arguments = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", $Port)
    if ($Reload) { $arguments += "--reload" }
    & $pythonExe @arguments
    if ($LASTEXITCODE -ne 0) { throw "backend exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
