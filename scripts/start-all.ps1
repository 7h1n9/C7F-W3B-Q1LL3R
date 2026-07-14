param(
    [switch]$SkipDocker,
    [switch]$UseMockCodex
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendDir = Join-Path $repoRoot "backend"
$frontendDir = Join-Path $repoRoot "frontend"
$bridgeDir = Join-Path $repoRoot "codex-bridge"
$runnerDir = Join-Path $repoRoot "kali-runner"
$logsRoot = Join-Path $repoRoot "data\runtime-logs\start-all"

New-Item -ItemType Directory -Force -Path $logsRoot | Out-Null

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    Set-Item -Path ("Env:{0}" -f $Name) -Value $Value
}

function Test-PortListening {
    param([Parameter(Mandatory = $true)][int]$Port)

    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Wait-PortListening {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$TimeoutSeconds = 180
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening -Port $Port) {
            return
        }
        Start-Sleep -Seconds 2
    }

    throw "$Name did not start listening on port $Port within $TimeoutSeconds seconds."
}

function Wait-HttpOk {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$TimeoutSeconds = 180
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 10
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                return $response
            }
        } catch {
            Start-Sleep -Seconds 2
            continue
        }
        Start-Sleep -Seconds 2
    }

    throw "$Name did not become healthy at $Uri within $TimeoutSeconds seconds."
}

function Get-CommandPath {
    param([Parameter(Mandatory = $true)][string]$Name)

    $cmd = Get-Command $Name -ErrorAction Stop
    if ($cmd.Source) {
        return $cmd.Source
    }
    if ($cmd.Path) {
        return $cmd.Path
    }
    return $cmd.Name
}

function Invoke-CommandInDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "[$Name] $Command $($Arguments -join ' ')"
    Push-Location $WorkingDirectory
    try {
        & $Command @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Name failed with exit code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
}

function Start-BackgroundService {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$HealthyUri
    )

    if (Test-PortListening -Port $Port) {
        Write-Host "[$Name] already listening on port $Port, skipping start."
        return
    }

    $stdout = Join-Path $logsRoot "$Name.out.log"
    $stderr = Join-Path $logsRoot "$Name.err.log"
    if (Test-Path $stdout) { Remove-Item $stdout -Force }
    if (Test-Path $stderr) { Remove-Item $stderr -Force }

    Write-Host "[$Name] starting on port $Port..."
    Start-Process -FilePath $Command -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden | Out-Null
    Wait-PortListening -Port $Port -Name $Name
    Wait-HttpOk -Uri $HealthyUri -Name $Name | Out-Null
    Write-Host "[$Name] healthy."
}

$runnerToken = "development-runner-token"
$frontendApiBase = "http://127.0.0.1:8000/api/v1"
$bridgePort = "8090"
$backendRunnerUrl = "http://127.0.0.1:8091"
$backendBridgeUrl = "http://127.0.0.1:8090"
$backendCorsOrigins = "http://localhost:5173,http://127.0.0.1:5173"
$backendEncryptionKey = "development-only-change-me"
$ctfctlAccessKey = "development-ctfctl-access-key"
$backendAllowedCidrs = "127.0.0.0/8,192.168.56.0/24,192.168.236.0/24"
$sqliteFallbackUrl = "sqlite+aiosqlite:///./local-dev.db"
$mysqlUrl = "mysql+asyncmy://ctf_agent:ctf_agent@127.0.0.1:3307/ctf_agent"
$databaseUrl = $sqliteFallbackUrl

$dockerUsed = $false
if (-not $SkipDocker) {
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerCmd) {
        try {
            Write-Host "[docker] starting mysql..."
            & docker compose up -d mysql | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "docker compose up failed with exit code $LASTEXITCODE"
            }

            $deadline = (Get-Date).AddMinutes(3)
            while ((Get-Date) -lt $deadline) {
                $containerId = (& docker compose ps -q mysql).Trim()
                if ($containerId) {
                    $health = (& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $containerId).Trim()
                    if ($health -eq "healthy" -or $health -eq "running") {
                        $databaseUrl = $mysqlUrl
                        $dockerUsed = $true
                        break
                    }
                }
                Start-Sleep -Seconds 2
            }

            if (-not $dockerUsed) {
                throw "mysql container did not become healthy"
            }
        } catch {
            Write-Warning "[docker] mysql startup failed, falling back to SQLite: $($_.Exception.Message)"
        }
    } else {
        Write-Warning "[docker] docker command not found, falling back to SQLite."
    }
} else {
    Write-Warning "[docker] skipped by request, using SQLite fallback."
}

Set-EnvValue -Name "APP_DATABASE_URL" -Value $databaseUrl
Set-EnvValue -Name "APP_WORKSPACE_ROOT" -Value "../data/workspaces"
Set-EnvValue -Name "APP_RUNNER_URL" -Value $backendRunnerUrl
Set-EnvValue -Name "APP_RUNNER_API_TOKEN" -Value $runnerToken
Set-EnvValue -Name "APP_CODEX_BRIDGE_URL" -Value $backendBridgeUrl
Set-EnvValue -Name "APP_CORS_ORIGINS" -Value $backendCorsOrigins
Set-EnvValue -Name "APP_ENCRYPTION_KEY" -Value $backendEncryptionKey
Set-EnvValue -Name "APP_CTFCTL_INTERNAL_ACCESS_KEY" -Value $ctfctlAccessKey
Set-EnvValue -Name "APP_ALLOWED_SERVICE_CIDRS" -Value $backendAllowedCidrs
Set-EnvValue -Name "APP_ENVIRONMENT" -Value "development"

Set-EnvValue -Name "RUNNER_WORKSPACE_ROOT" -Value "../data/workspaces"
Set-EnvValue -Name "RUNNER_MAX_OUTPUT_BYTES" -Value "1048576"
Set-EnvValue -Name "RUNNER_JOB_TIMEOUT_SECONDS" -Value "30"
Set-EnvValue -Name "RUNNER_API_TOKEN" -Value $runnerToken
Set-EnvValue -Name "RUNNER_ENVIRONMENT" -Value "development"

Set-EnvValue -Name "CODEX_BRIDGE_PORT" -Value $bridgePort
Set-EnvValue -Name "CODEX_MOCK_MODE" -Value ($(if ($UseMockCodex) { "true" } else { "false" }))
Set-EnvValue -Name "CTFCTL_BACKEND_URL" -Value "http://127.0.0.1:8000"
Set-EnvValue -Name "CTFCTL_ACCESS_KEY" -Value $ctfctlAccessKey
Set-EnvValue -Name "VITE_API_BASE_URL" -Value $frontendApiBase

$pythonExe = Get-CommandPath -Name "python"
$npmCmd = (Get-Command npm.cmd -ErrorAction SilentlyContinue)
if ($npmCmd -and $npmCmd.Source) {
    $npmExe = $npmCmd.Source
} elseif ($npmCmd -and $npmCmd.Path) {
    $npmExe = $npmCmd.Path
} else {
    $npmExe = "npm"
}

if (-not $SkipDocker -and $dockerUsed) {
    Write-Host "[docker] mysql is ready on localhost:3307."
}

Write-Host "[install] preparing backend and runner dependencies..."
Invoke-CommandInDirectory -Name "backend deps" -WorkingDirectory $backendDir -Command $pythonExe -Arguments @("-m", "pip", "install", "-e", ".[dev]")
Invoke-CommandInDirectory -Name "runner deps" -WorkingDirectory $runnerDir -Command $pythonExe -Arguments @("-m", "pip", "install", "-e", ".[dev]")

if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
    Invoke-CommandInDirectory -Name "frontend deps" -WorkingDirectory $frontendDir -Command $npmExe -Arguments @("install")
}

if (-not (Test-Path (Join-Path $bridgeDir "node_modules"))) {
    Invoke-CommandInDirectory -Name "bridge deps" -WorkingDirectory $bridgeDir -Command $npmExe -Arguments @("install")
}

Write-Host "[migrate] applying backend migrations..."
Invoke-CommandInDirectory -Name "backend migrate" -WorkingDirectory $backendDir -Command $pythonExe -Arguments @("-m", "alembic", "upgrade", "head")

Start-BackgroundService -Name "runner" -Port 8091 -WorkingDirectory $runnerDir -Command $pythonExe -Arguments @("-m", "uvicorn", "app.main:app", "--port", "8091") -HealthyUri "http://127.0.0.1:8091/health"
Start-BackgroundService -Name "backend" -Port 8000 -WorkingDirectory $backendDir -Command $pythonExe -Arguments @("-m", "uvicorn", "app.main:app", "--reload", "--port", "8000") -HealthyUri "http://127.0.0.1:8000/api/v1/health"
Start-BackgroundService -Name "bridge" -Port 8090 -WorkingDirectory $bridgeDir -Command $npmExe -Arguments @("run", "dev", "--", "--host", "127.0.0.1") -HealthyUri "http://127.0.0.1:8090/health"
Start-BackgroundService -Name "frontend" -Port 5173 -WorkingDirectory $frontendDir -Command $npmExe -Arguments @("run", "dev", "--", "--host", "127.0.0.1") -HealthyUri "http://127.0.0.1:5173"

Write-Host ""
Write-Host "[state]"
Write-Host "  docker mysql : $([bool]$dockerUsed)"
Write-Host "  backend      : http://127.0.0.1:8000/api/v1/health"
Write-Host "  runner       : http://127.0.0.1:8091/health"
Write-Host "  bridge       : http://127.0.0.1:8090/health"
Write-Host "  frontend     : http://127.0.0.1:5173"
Write-Host "  logs         : $logsRoot"
