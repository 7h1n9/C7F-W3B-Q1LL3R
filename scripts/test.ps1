$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = (Get-Command python -ErrorAction Stop).Source
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source

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

Invoke-InDirectory (Join-Path $repoRoot "backend") $python @("-m", "ruff", "check", ".")
Invoke-InDirectory (Join-Path $repoRoot "backend") $python @("-m", "pytest", "-q")
Invoke-InDirectory (Join-Path $repoRoot "kali-runner") $python @("-m", "ruff", "check", ".")
Invoke-InDirectory (Join-Path $repoRoot "kali-runner") $python @("-m", "pytest", "-q")
Invoke-InDirectory (Join-Path $repoRoot "frontend") $npm @("run", "build")
Invoke-InDirectory (Join-Path $repoRoot "codex-bridge") $npm @("run", "build")
