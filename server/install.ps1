#Requires -Version 5.1
<#
.SYNOPSIS
  Instala o servidor MonitorIOS como serviço do Windows.

.DESCRIPTION
  - cria o ambiente Python (server\.venv) e instala as dependências;
  - localiza (ou instala via winget, com confirmação) o FFmpeg e copia para a pasta de dados;
  - cria a pasta de dados com permissões restritas (SYSTEM e Administradores);
  - cria o usuário administrador e configura as credenciais SMB (senhas pedidas de forma oculta);
  - libera a porta no firewall apenas para a rede local e para o Tailscale (100.64.0.0/10);
  - registra o serviço "MonitorIOS" (início automático atrasado + reinício automático em falhas).

  Pode ser executado novamente para atualizar a instalação (preserva dados e configurações).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param(
    [string]$DataDir = "$env:ProgramData\MonitorIOS",
    [int]$Port = 8765,
    [switch]$SkipFfmpeg,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$ServerDir = $PSScriptRoot
$Venv = Join-Path $ServerDir ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$ServiceName = "MonitorIOS"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Execute este script em um PowerShell aberto como Administrador."
}

# ---------------------------------------------------------------- Python
Step "Verificando Python 3.11+"
$py = $null
foreach ($cand in @("py -3", "python")) {
    try {
        $v = & ([scriptblock]::Create("$cand -c `"import sys;print('%d.%d'%sys.version_info[:2])`"")) 2>$null
        if ($v -and [version]$v -ge [version]"3.11") { $py = $cand; break }
    } catch {}
}
if (-not $py) { throw "Python 3.11 ou superior não encontrado. Instale com: winget install Python.Python.3.13" }
Write-Host "Python: $py ($v)"

Step "Criando ambiente virtual e instalando dependências"
if (-not (Test-Path $VenvPy)) { & ([scriptblock]::Create("$py -m venv `"$Venv`"")) }
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r (Join-Path $ServerDir "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar dependências" }

# --------------------------------------------------------------- Pasta de dados
Step "Preparando pasta de dados em $DataDir"
New-Item -ItemType Directory -Force -Path $DataDir, (Join-Path $DataDir "bin") | Out-Null
# Apenas SYSTEM e Administradores (o banco guarda segredos cifrados com DPAPI de máquina).
icacls $DataDir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
$env:MONITORIOS_DATA = $DataDir

# ------------------------------------------------------------------ FFmpeg
if (-not $SkipFfmpeg) {
    Step "Verificando FFmpeg"
    $binDir = Join-Path $DataDir "bin"
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
    if (-not $ff) {
        $ff = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $ff -and -not (Test-Path (Join-Path $binDir "ffmpeg.exe"))) {
        $ans = Read-Host "FFmpeg não encontrado. Instalar agora via winget (Gyan.FFmpeg)? [s/N]"
        if ($ans -match '^[sSyY]') {
            winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
            $ff = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue |
                Select-Object -First 1 -ExpandProperty FullName
        }
    }
    if ($ff) {
        # Copia para a pasta de dados: o serviço (LocalSystem) não usa o PATH do usuário.
        Copy-Item $ff (Join-Path $binDir "ffmpeg.exe") -Force
        Copy-Item (Join-Path (Split-Path $ff) "ffprobe.exe") (Join-Path $binDir "ffprobe.exe") -Force
        Write-Host "FFmpeg copiado para $binDir"
    } elseif (-not (Test-Path (Join-Path $binDir "ffmpeg.exe"))) {
        throw "FFmpeg é obrigatório. Instale e execute novamente."
    }
}

# ---------------------------------------------------------- Configuração
Step "Inicializando configuração"
Push-Location $ServerDir
try {
    & $VenvPy -m app.cli settings --set "http_port=$Port" "ffmpeg_path=" "ffprobe_path=" | Out-Null

    $hasUsers = & $VenvPy -c "from app.runtime import Runtime; print(Runtime(start_workers=False).auth.has_users())"
    if ($hasUsers -ne "True") {
        Step "Criando usuário administrador"
        $admin = Read-Host "Nome do administrador [admin]"
        if (-not $admin) { $admin = "admin" }
        & $VenvPy -m app.cli init-admin --username $admin
        if ($LASTEXITCODE -ne 0) { throw "Falha ao criar administrador" }
    }

    $smbUser = & $VenvPy -c "from app.config import Config; print(Config().settings.smb_username)"
    if (-not $smbUser) {
        Step "Credenciais do compartilhamento SMB do roteador"
        Write-Host "O serviço roda como LocalSystem e precisa de usuário/senha próprios para o SMB."
        $u = Read-Host "Usuário SMB do roteador (Enter para pular)"
        if ($u) { & $VenvPy -m app.cli set-smb --username $u }
    }
} finally { Pop-Location }

# -------------------------------------------------------------- Firewall
Step "Configurando firewall (rede local + Tailscale)"
Get-NetFirewallRule -DisplayName "MonitorIOS API" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "MonitorIOS API" -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress @("LocalSubnet", "100.64.0.0/10") -Action Allow -Profile Any | Out-Null

# --------------------------------------------------------------- Serviço
Step "Registrando serviço $ServiceName"
$svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -ne "Stopped") { Stop-Service $ServiceName -Force; Start-Sleep 3 }
    & $VenvPy (Join-Path $ServerDir "service\winservice.py") --startup delayed update
} else {
    & $VenvPy (Join-Path $ServerDir "service\winservice.py") --startup delayed install
}
if ($LASTEXITCODE -ne 0) { throw "Falha ao registrar o serviço" }
# Reinício automático: 5 s, 15 s e depois a cada 60 s; contador zera após 1 dia.
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null
sc.exe failureflag $ServiceName 1 | Out-Null
# Mais tempo para encerrar com segurança no desligamento do Windows.
sc.exe config $ServiceName start= delayed-auto | Out-Null

if (-not $NoStart) {
    Step "Iniciando serviço"
    Start-Service $ServiceName
    $ok = $false
    foreach ($i in 1..30) {
        Start-Sleep 1
        try { Invoke-RestMethod "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 2 | Out-Null; $ok = $true; break } catch {}
    }
    if ($ok) {
        Write-Host "`nMonitorIOS em execução." -ForegroundColor Green
        Write-Host "Painel:        http://127.0.0.1:$Port/admin/"
        Write-Host "App (Tailscale): http://100.113.162.101:$Port"
    } else {
        Write-Warning "O serviço não respondeu. Veja os logs em $DataDir\logs\server.log"
    }
}
