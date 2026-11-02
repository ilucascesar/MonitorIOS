# MonitorIOS — gravador de câmeras IP próprio (Windows + iPhone)

Substituto do Agent DVR para uso doméstico. Ele tem duas partes:

- **Servidor Windows** (`server/`): Python + FastAPI + FFmpeg + SQLite. Grava 24h por câmera com
  `-c copy`, sem recodificar. Envia os segmentos ao compartilhamento SMB do roteador, indexa cada um
  com início e fim reais, aplica a retenção e entrega ao vivo e gravações em HLS. Inclui painel web
  local, API REST e WebSocket. Roda como serviço do Windows com início automático.
- **App iOS** (`ios/`): SwiftUI nativo. Traz grade de câmeras, ao vivo, calendário mensal, linha do
  tempo de 24h interativa, exportação de trechos e captura de imagem. O token fica no Keychain e o
  acesso remoto é feito pelo Tailscale.

```
câmeras RTSP ──► FFmpeg (1 por câmera, -c copy) ──► buffer local ──► \\192.168.1.1\volume(sda1)\Gravacoes_Cameras\MonitorIOS
                                   │                                      ▲
                                   └─► índice SQLite (C:\ProgramData\MonitorIOS) ─┘ retenção / reconciliação
iPhone ◄── HLS (ao vivo / VOD com horário real) ◄── FastAPI :8765 ◄── Tailscale (100.113.162.101)
```

## Documentação

| Documento | Conteúdo |
|---|---|
| [docs/INSTALACAO.md](docs/INSTALACAO.md) | Instalação do serviço, migração do Agent DVR sem desligá-lo, comandos e operação |
| [docs/ARQUITETURA.md](docs/ARQUITETURA.md) | Funcionamento da gravação, do índice, da linha do tempo, do armazenamento e do HLS |
| [docs/SEGURANCA.md](docs/SEGURANCA.md) | Autenticação, segredos, Tailscale, limites do HTTP e como usar HTTPS |
| [docs/IOS.md](docs/IOS.md) | Compilação no GitHub Actions, instalação pelo iLoader e limites de assinatura |
| [docs/TESTES.md](docs/TESTES.md) | O que já foi validado com as câmeras reais e o que ainda falta testar; capacidade do disco |

## Início rápido (servidor)

No PowerShell **como Administrador**, na pasta `server`:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Depois disso, abra `http://localhost:8765/admin` no próprio servidor. O painel só abre localmente.

## Estrutura

```
server/
  app/            código do servidor (gravação, índice, armazenamento, API, painel web)
  service/        serviço do Windows (pywin32)
  tests/          testes automáticos (pytest)
  install.ps1 / service.ps1 / uninstall.ps1
ios/
  project.yml     definição do projeto (XcodeGen gera o .xcodeproj no CI)
  MonitorIOS/     app SwiftUI
  MonitorIOSTests/
.github/workflows/  CI: testes do servidor (Windows) e build do app (macOS)
```

Nenhuma senha, token ou credencial fica no repositório. As credenciais das câmeras e do SMB são
cifradas com DPAPI no servidor, e o token do app fica no Keychain do iPhone.
