param(
    [int]$BackendPort = 8000,
    [int]$RunnerPort = 8091,
    [int]$BridgePort = 8090,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidsRoot = Join-Path $repoRoot "data\pids"
$services = @{
    backend = @{ Port = $BackendPort; Pattern = "app\.main:app.*--port\s+$BackendPort" }
    bridge = @{ Port = $BridgePort; Pattern = "codex-bridge.*(dist[\\/]server\.js|src[\\/]server\.ts)" }
    frontend = @{ Port = $FrontendPort; Pattern = "frontend[\\/]node_modules.*vite" }
    runner = @{ Port = $RunnerPort; Pattern = "app\.main:app.*--port\s+$RunnerPort" }
}

function Get-Listeners([int]$Port) {
    @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Stop-ServiceTree([string]$Name, [int]$Port, [string]$Pattern) {
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $ids = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($item in @(Get-Listeners $Port)) { [void]$ids.Add([int]$item.OwningProcess) }
    $pidPath = Join-Path $pidsRoot "$Name.pid"
    if (Test-Path -LiteralPath $pidPath) {
        $saved = 0
        if ([int]::TryParse((Get-Content -LiteralPath $pidPath -Raw), [ref]$saved)) { [void]$ids.Add($saved) }
    }
    foreach ($process in $all) {
        if ([string]$process.CommandLine -match $Pattern) { [void]$ids.Add([int]$process.ProcessId) }
    }
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($process in $all) {
            if ($ids.Contains([int]$process.ParentProcessId) -and $ids.Add([int]$process.ProcessId)) { $changed = $true }
        }
    }
    foreach ($id in @($ids)) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $pidPath) { Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue }
}

foreach ($name in @("backend", "bridge", "frontend", "runner")) {
    $item = $services[$name]
    Stop-ServiceTree $name $item.Port $item.Pattern
}
Write-Host "START_ALL_STATUS=STOPPED"
