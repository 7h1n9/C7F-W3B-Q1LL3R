param(
    [switch]$SkipDocker,
    [switch]$UseMockCodex,
    [switch]$Install,
    [int]$BackendPort = 8000,
    [int]$RunnerPort = 8091,
    [int]$BridgePort = 8090,
    [int]$FrontendPort = 5173,
    [string]$DatabaseUrl = "",
    [string]$RunnerUrl = "http://192.168.236.128:8091",
    # Protocol switch passthrough; see start-all.ps1 for what these mean.
    [switch]$SkipSwitch,
    [int]$SwitchPort = 8790,
    [string]$SwitchUpstreamBaseUrl = "",
    [string]$SwitchUpstreamApiKey = "",
    [string]$SwitchUpstreamWire = "",
    [string]$SwitchModelAliases = ""
)

$ErrorActionPreference = "Stop"
$stopArgs = @("-File", (Join-Path $PSScriptRoot "stop-all.ps1"), "-BackendPort", $BackendPort, "-RunnerPort", $RunnerPort, "-BridgePort", $BridgePort, "-FrontendPort", $FrontendPort, "-SwitchPort", $SwitchPort)
& powershell @stopArgs
if ($LASTEXITCODE -ne 0) { throw "stop-all failed with exit code $LASTEXITCODE" }
$startArgs = @("-File", (Join-Path $PSScriptRoot "start-all.ps1"), "-BackendPort", $BackendPort, "-RunnerPort", $RunnerPort, "-BridgePort", $BridgePort, "-FrontendPort", $FrontendPort, "-RunnerUrl", $RunnerUrl, "-SwitchPort", $SwitchPort)
if ($SkipDocker) { $startArgs += "-SkipDocker" }
if ($UseMockCodex) { $startArgs += "-UseMockCodex" }
if ($Install) { $startArgs += "-Install" }
if ($DatabaseUrl) { $startArgs += @("-DatabaseUrl", $DatabaseUrl) }
if ($SkipSwitch) { $startArgs += "-SkipSwitch" }
if ($SwitchUpstreamBaseUrl) { $startArgs += @("-SwitchUpstreamBaseUrl", $SwitchUpstreamBaseUrl) }
if ($SwitchUpstreamApiKey) { $startArgs += @("-SwitchUpstreamApiKey", $SwitchUpstreamApiKey) }
if ($SwitchUpstreamWire) { $startArgs += @("-SwitchUpstreamWire", $SwitchUpstreamWire) }
if ($SwitchModelAliases) { $startArgs += @("-SwitchModelAliases", $SwitchModelAliases) }
& powershell @startArgs
exit $LASTEXITCODE
