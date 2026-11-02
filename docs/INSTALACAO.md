# Instalação e operação do servidor

## Requisitos

- Windows 10/11 com Python 3.11 ou superior (python.org; marque "Add to PATH").
- FFmpeg/FFprobe. O instalador procura no PATH e, se não encontrar, oferece instalar via `winget`.
  Os binários são copiados para `C:\ProgramData\MonitorIOS\bin`, porque o serviço roda como
  LocalSystem e não enxerga o PATH do usuário.
- Acesso ao compartilhamento `\\192.168.1.1\volume(sda1)`.
- Tailscale instalado no servidor e no iPhone, para acesso fora de casa.

Docker, WSL e Frigate não são usados.

## Instalação

PowerShell **como Administrador**, na pasta `server`:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1            # porta 8765 por padrão
# opções: -Port 8765  -DataDir C:\ProgramData\MonitorIOS  -SkipFfmpeg  -NoStart
```

O instalador:

1. Cria o ambiente virtual `server\.venv` e instala as dependências.
2. Cria `C:\ProgramData\MonitorIOS` com permissão restrita a SYSTEM e Administradores. Ali ficam o
   banco SQLite, os logs, o buffer local e os segredos cifrados.
3. Localiza ou instala o FFmpeg.
4. Pede a senha do primeiro usuário `admin`, digitada sem eco e nunca gravada em texto puro.
5. Pede, se você quiser, usuário e senha do compartilhamento SMB do roteador. Eles ficam cifrados
   com DPAPI.
6. Cria a regra de firewall "MonitorIOS API", liberando somente a rede local e a faixa do Tailscale
   (100.64.0.0/10).
7. Instala o serviço **MonitorIOS** com início automático atrasado e reinício automático em caso de
   falha (5 s, 15 s e 60 s), e aguarda o `/api/v1/health` responder.

Rodar o instalador de novo atualiza o serviço sem apagar dados.

## Operação

```powershell
.\service.ps1 status      # estado do serviço + /health
.\service.ps1 restart
.\service.ps1 logs -Lines 100
.\service.ps1 stop
```

O painel local fica em `http://localhost:8765/admin`. Ele tem estas abas:

- **Status**: câmeras, gravação, armazenamento, CPU e eventos, atualizados em tempo real.
- **Câmeras**: cadastrar, editar, testar, descoberta ONVIF, importar do Agent DVR e ligar/desligar a
  gravação. As alterações entram em vigor sem reiniciar o servidor.
- **Armazenamento**: credenciais SMB, teste de escrita, retenção em dias, limite global em GB,
  espaço livre mínimo, limpeza e reconciliação manuais.
- **Usuários**: contas do app. O perfil *viewer* só assiste; *admin* também altera configurações.
- **Logs**.

### Linha de comando

Execute com `.venv\Scripts\python.exe -m app.cli <comando>`. Com o serviço instalado, defina antes
`$env:MONITORIOS_DATA = "C:\ProgramData\MonitorIOS"`, que já é o padrão.

| Comando | Uso |
|---|---|
| `smb-test` | Teste real de escrita, leitura e exclusão no compartilhamento |
| `set-smb --username U` | Grava as credenciais SMB cifradas (a senha é pedida sem eco) |
| `import-agentdvr` | Importa as câmeras do `objects.json` do Agent DVR. Só lê o arquivo, nunca o altera |
| `cameras`, `test-camera ID` | Lista e testa câmeras (RTSP + ONVIF) |
| `discover` | Descoberta ONVIF na rede local |
| `record-test --id ID --seconds 150` | Grava uma câmera por N segundos e mostra os segmentos indexados |
| `reconcile`, `retention` | Reconciliação de arquivos fora do índice e limpeza imediata |
| `settings --set retention_days=15 max_storage_gb=200` | Altera as configurações |
| `add-user NOME --role viewer`, `passwd NOME` | Usuários |

## Migração do Agent DVR, sem desligá-lo

O MonitorIOS grava **somente** em `Gravacoes_Cameras\MonitorIOS`. Ele nunca lê, altera ou apaga a
pasta `video` nem outras pastas do Agent DVR. A limpeza só apaga arquivos que estejam no índice do
MonitorIOS **e** dentro da pasta marcada com o arquivo `.monitorios-root`.

1. Instale o MonitorIOS com o Agent DVR ainda ligado. As câmeras aceitam várias conexões RTSP
   simultâneas, e as três funcionaram em paralelo nos testes.
2. Use **Importar do Agent DVR** no painel (ou `import-agentdvr`). As câmeras, URLs e credenciais
   são copiadas. As senhas vão direto para o armazenamento cifrado e não aparecem na tela.
3. Acompanhe por alguns dias: linha do tempo sem lacunas, ao vivo no iPhone pelo Tailscale e
   exportação.
4. **Atenção ao espaço**: o pendrive é compartilhado. Hoje o Agent DVR pode ocupar até 28 GB (MaxSize
   28000 MB) num disco de 32 GB, e o MonitorIOS está limitado a 18 GB. Enquanto os dois gravarem, o
   espaço livre mínimo (`min_free_gb`) protege o disco: o MonitorIOS apaga primeiro as próprias
   gravações mais antigas. Veja a capacidade em [TESTES.md](TESTES.md#capacidade-do-armazenamento).
5. Quando estiver satisfeito, desative a gravação no Agent DVR. As gravações antigas continuam onde
   estão, e você decide quando apagá-las.

## Desinstalação

```powershell
.\uninstall.ps1               # remove o serviço e a regra de firewall; mantém os dados
.\uninstall.ps1 -RemoveData   # também apaga C:\ProgramData\MonitorIOS (pede confirmação)
```

As gravações no compartilhamento nunca são apagadas pelo desinstalador.
