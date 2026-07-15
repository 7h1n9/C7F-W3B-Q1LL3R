$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = (Get-Command python -ErrorAction Stop).Source
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source

foreach ($name in @("backend", "kali-runner", "codex-bridge", "frontend")) {
    $envFile = Join-Path $repoRoot "$name\.env"
    $example = Join-Path $repoRoot "$name\.env.example"
    if (-not (Test-Path $envFile) -and (Test-Path $example)) {
        Copy-Item $example $envFile
    }
}

function Invoke-InDirectory {
    param([string]$Path, [string]$Command, [string[]]$Arguments)
    Push-Location $Path
    try {
        & $Command @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$Command $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}

Invoke-InDirectory (Join-Path $repoRoot "backend") $python @("-m", "pip", "install", "-e", ".[dev]")
Invoke-InDirectory (Join-Path $repoRoot "kali-runner") $python @("-m", "pip", "install", "-e", ".[dev]")
Invoke-InDirectory (Join-Path $repoRoot "frontend") $npm @("install")
Invoke-InDirectory (Join-Path $repoRoot "codex-bridge") $npm @("install")
