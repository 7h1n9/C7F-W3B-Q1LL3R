param(
    [int]$BackendPort = 8000,
    [int]$RunnerPort = 8091,
    [int]$BridgePort = 8090,
    [int]$FrontendPort = 5173,
    [string]$RunnerUrl = "http://192.168.236.128:8091"
)

$ErrorActionPreference = "SilentlyContinue"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidsRoot = Join-Path $repoRoot "data\pids"
$logsRoot = Join-Path $repoRoot "logs\services"
$services = @(
    @{ Name = "backend"; Port = $BackendPort; Uri = "http://127.0.0.1:$BackendPort/api/v1/health/ready" },
    @{ Name = "bridge"; Port = $BridgePort; Uri = "http://127.0.0.1:$BridgePort/health" },
    @{ Name = "frontend"; Port = $FrontendPort; Uri = "http://127.0.0.1:$FrontendPort" },
    @{ Name = "runner"; Port = $RunnerPort; Uri = "$($RunnerUrl.TrimEnd('/'))/health" }
)

foreach ($service in $services) {
    $uri = [Uri]$service.Uri
    $connection = if ($uri.Host -in @("127.0.0.1", "localhost", "::1")) { Get-NetTCPConnection -State Listen -LocalPort $service.Port | Select-Object -First 1 } else { $null }
    $pidPath = Join-Path $pidsRoot "$($service.Name).pid"
    $pidValue = if (Test-Path -LiteralPath $pidPath) { (Get-Content -LiteralPath $pidPath -Raw).Trim() } else { "-" }
    $health = "DOWN"
    try {
        $response = Invoke-WebRequest -Uri $service.Uri -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { $health = "READY" }
    } catch {}
    Write-Host ("{0,-10} PID={1,-8} PORT={2,-5} HEALTH={3}" -f $service.Name, $pidValue, $service.Port, $health)
    $log = Join-Path $logsRoot "$($service.Name).log"
    if ($health -eq "DOWN" -and (Test-Path -LiteralPath $log)) {
        Write-Host "  last log lines:"
        Get-Content -LiteralPath $log -Tail 50
    }
}
