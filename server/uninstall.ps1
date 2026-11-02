#Requires -Version 5.1
<#
.SYNOPSIS
  Remove o serviço MonitorIOS e a regra de firewall.
.DESCRIPTION
  Por padrão preserva a pasta de dados (índice, configurações, logs). Com -RemoveData apaga
  também C:\ProgramData\MonitorIOS. As gravações no pendrive NUNCA são apagadas por este script.
#>
param([switch]$RemoveData, [string]$DataDir = "$env:ProgramData\MonitorIOS")
$ErrorActionPreference = "Stop"
$ServerDir = $PSScriptRoot
$svc = Get-Service MonitorIOS -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -ne "Stopped") { Stop-Service MonitorIOS -Force; Start-Sleep 3 }
    & (Join-Path $ServerDir ".venv\Scripts\python.exe") (Join-Path $ServerDir "service\winservice.py") remove
}
Get-NetFirewallRule -DisplayName "MonitorIOS API" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($RemoveData) {
    $ans = Read-Host "Apagar $DataDir (índice, logs, configurações)? Digite SIM"
    if ($ans -eq "SIM") { Remove-Item -Recurse -Force $DataDir }
}
Write-Host "MonitorIOS removido. Gravações no pendrive foram preservadas."
