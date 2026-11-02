# Testes: o que foi validado e o que falta

## Testes automáticos

**Servidor** (`server/tests`, `python -m pytest`): 53 testes passando no Windows 11. Também rodam no
CI (`.github/workflows/server.yml`).

| Área | Cobertura |
|---|---|
| Gravação (FFmpeg real, vídeo sintético `testsrc2`) | Gravação contínua, índice e envio ao "SMB"; playlist com byte offsets válidos; câmera offline sem afetar as outras; recuperação após falha e reconciliação de órfãos; exportação com recorte exato |
| Armazenamento | Armazenamento indisponível → buffer local → envio quando volta; estouro do buffer gera evento; reconciliação remota |
| Retenção | Por idade, por limite global, gravações protegidas preservadas, **arquivos de outros programas intocados**, nada é apagado sem o marcador `.monitorios-root`, caminhos adulterados no banco rejeitados |
| Linha do tempo | União de intervalos, lacunas, localização do próximo trecho, dias do mês com fuso |
| Segurança | Limite de login (429), admin bloqueado para IP remoto, CSRF do cookie, escopo do token de mídia, `Range` 206, **log sem tokens nem senhas**, `safe_join` |
| Saúde das câmeras | Falha isolada de conexão não gera alarme; 3 falhas seguidas geram um único aviso |

**App iOS** (`ios/MonitorIOSTests`, rodam no CI em simulador): matemática da linha do tempo
(posição ↔ horário, zoom, lacunas, deslocamento na playlist) e decodificação do JSON real da API.

## Validado com as câmeras reais (servidor de desenvolvimento, 01/11/2026)

- Importação das 3 câmeras do Agent DVR, via ONVIF/RTSP na porta 8554, sem alterar o Agent DVR.
- `record-test` e gravação contínua das **3 câmeras simultaneamente** com o Agent DVR ligado:
  - segmentos de 60 s contíguos, sem lacunas, enviados a `Gravacoes_Cameras\MonitorIOS`;
  - CPU do processo do servidor em cerca de 1,5 %.
- Ao vivo: imagem principal e secundária em HLS em cerca de 4 s. A captura (snapshot) é a imagem
  real, e o horário na imagem da câmera bate com o do servidor.
- Playlist de gravação com horário real (`PROGRAM-DATE-TIME`) e byte ranges; `Range` 206.
- Endpoints de dias, linha do tempo e localização.
- Exportação real de 60 s: MP4 de cerca de 4,5 MB baixado com sucesso.
- Todos os modelos do app iOS conferidos campo a campo contra as respostas reais da API.
- Painel web: login, status em tempo real, câmeras, armazenamento, usuários e logs.

## Ainda precisa ser testado no ambiente real

| Item | Por quê / como |
|---|---|
| **Compilar o app iOS** | O código Swift nunca foi compilado: não há Mac. O primeiro build no GitHub Actions pode apontar erros de compilação, que serão corrigidos a partir do log |
| App no iPhone | Ao vivo, linha do tempo, exportação e compartilhamento no aparelho real, instalado pelo iLoader |
| Acesso pelo Tailscale com dados móveis | Abrir o app fora do Wi-Fi, alternar Wi-Fi ↔ 4G/5G durante o ao vivo e conferir a reconexão |
| Instalação como serviço | `install.ps1` como Administrador. O serviço LocalSystem precisa das credenciais SMB (`set-smb`), pois não usa as do seu usuário |
| Reinício do Windows | Confirmar que o serviço sobe sozinho e volta a gravar sem intervenção |
| Queda do roteador ou pendrive | Desconectar o pendrive: a gravação continua no buffer local e é enviada quando ele volta (coberto em teste automático; falta o teste físico) |
| Câmera desligada da tomada | Evento "não responde", as outras seguem gravando, e ela volta sozinha (coberto em teste automático com fonte falsa) |
| 24 h ou mais de operação | Retenção real, crescimento do banco e virada do dia na linha do tempo |
| HTTPS | Opcional, com `tailscale cert` (ver SEGURANCA.md) |

## Capacidade do armazenamento

Medido nas câmeras atuais: cerca de **0,67 Mbit/s por câmera ≈ 7,2 GB/dia**. Com 3 câmeras, isso dá
cerca de **21,7 GB/dia**.

| Disco no roteador | Dias de gravação (3 câmeras, só MonitorIOS) |
|---|---|
| Pendrive atual, 32 GB (cerca de 23 GB livres, dividido com o Agent DVR) | **menos de 1 dia** |
| 256 GB | cerca de 11 dias |
| 1 TB (HD/SSD USB) | cerca de 45 dias |
| 2 TB | cerca de 90 dias |

Os padrões atuais são conservadores para não lotar o pendrive compartilhado: `max_storage_gb=18` e
`min_free_gb=2`. **Recomenda-se um HD/SSD USB de 1 TB ou mais.** Depois de trocar o disco, ajuste
com `settings --set max_storage_gb=900 retention_days=30` (ou pelo painel). Se o roteador não
alimentar bem um HD de 2,5", use um com fonte própria ou um hub alimentado.
