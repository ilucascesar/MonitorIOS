#Requires -Version 5.1
<#
.SYNOPSIS
  Controla o serviço MonitorIOS: start | stop | restart | status | logs
.EXAMPLE
  .\service.ps1 restart
  .\service.ps1 logs -Lines 100
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet("start", "stop", "restart", "status", "logs")][string]$Action,
    [int]$Lines = 60,
    [string]$DataDir = "$env:ProgramData\MonitorIOS"
)
$ErrorActionPreference = "Stop"
$name = "MonitorIOS"
switch ($Action) {
    "start"   { Start-Service $name; Get-Service $name }
    "stop"    { Stop-Service $name; Get-Service $name }
    "restart" { Restart-Service $name; Get-Service $name }
    "status"  {
        Get-Service $name | Format-List Name, DisplayName, Status, StartType
        $port = (Get-Content (Join-Path $DataDir "settings.json") -Raw | ConvertFrom-Json).http_port
        try { Invoke-RestMethod "http://127.0.0.1:$port/api/v1/health" -TimeoutSec 3 } catch { Write-Warning "API não respondeu na porta $port" }
    }
    "logs"    { Get-Content (Join-Path $DataDir "logs\server.log") -Tail $Lines }
}
