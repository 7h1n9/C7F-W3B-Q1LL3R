param(
    [switch]$SkipDocker,
    [switch]$UseMockCodex,
    [switch]$Install,
    [int]$BackendPort = 8000,
    [int]$RunnerPort = 8091,
    [int]$BridgePort = 8090,
    [int]$FrontendPort = 5173,
    [string]$DatabaseUrl = ""
)

$ErrorActionPreference = "Stop"
$stopArgs = @("-File", (Join-Path $PSScriptRoot "stop-all.ps1"), "-BackendPort", $BackendPort, "-RunnerPort", $RunnerPort, "-BridgePort", $BridgePort, "-FrontendPort", $FrontendPort)
& powershell @stopArgs
if ($LASTEXITCODE -ne 0) { throw "stop-all failed with exit code $LASTEXITCODE" }
$startArgs = @("-File", (Join-Path $PSScriptRoot "start-all.ps1"), "-BackendPort", $BackendPort, "-RunnerPort", $RunnerPort, "-BridgePort", $BridgePort, "-FrontendPort", $FrontendPort)
if ($SkipDocker) { $startArgs += "-SkipDocker" }
if ($UseMockCodex) { $startArgs += "-UseMockCodex" }
if ($Install) { $startArgs += "-Install" }
if ($DatabaseUrl) { $startArgs += @("-DatabaseUrl", $DatabaseUrl) }
& powershell @startArgs
exit $LASTEXITCODE
