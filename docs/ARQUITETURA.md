# Arquitetura

## Gravação (`app/recordings/recorder.py`, `supervisor.py`)

- **Um processo FFmpeg por câmera**, com `-rtsp_transport tcp -timeout 15s` e `-c copy`, sem
  recodificar (uso de CPU de cerca de 1–2 % no total). O áudio é opcional por câmera.
- O muxer `segment` corta nos keyframes, alinhado ao relógio (`-segment_atclocktime`), com 60 s por
  padrão, configurável de 10 a 900 s. Contêineres usados:
  - H.264: MPEG-TS (`.ts`);
  - H.265: fMP4 (`.mp4`) com `hvc1`, que o AVPlayer exige para HEVC.
- O FFmpeg informa cada segmento concluído (`-segment_list pipe:1`). O horário real de início e fim
  vem da ancoragem do relógio do servidor aos timestamps do fluxo. A duração real é medida com
  ffprobe, pacote a pacote, e não pelo valor nominal. Segmentos consecutivos da mesma execução são
  encadeados sem buracos artificiais.
- **Vigia**: arquivo parado por mais de `stall_timeout_seconds`, queda do RTSP ou saída do FFmpeg
  fazem a câmera ser reiniciada com espera exponencial (2 → 60 s). As outras câmeras não são
  afetadas.
- **Validação**: cada segmento passa pelo ffprobe. Arquivo vazio ou truncado é marcado como
  `corrupt` e gera um evento, sem aparecer na linha do tempo. O pedaço final minúsculo de uma parada
  normal é descartado.
- **Recuperação**: ao iniciar, os arquivos do buffer que ficaram fora do índice (queda de energia,
  por exemplo) são reconciliados e indexados como `partial`.

## Armazenamento (`app/storage/manager.py`)

- O FFmpeg grava num **buffer local** (`C:\ProgramData\MonitorIOS\buffer`). Um enviador copia cada
  segmento ao SMB como `.part`, faz fsync, confere o tamanho e só então renomeia e apaga a cópia
  local. Se o roteador ou o pendrive cair, a gravação continua localmente (até `buffer_max_gb`) e é
  enviada quando o compartilhamento voltar.
- Estrutura remota: `MonitorIOS\<Camera>\AAAA-MM-DD\HH\<run>_AAAAMMDD-HHMMSS.ts`.
- O **índice SQLite** fica no disco local, nunca no SMB, porque SQLite em rede corrompe.
- **Saúde**: teste periódico de escrita, leitura e exclusão (64 KB), espaço livre e latência.
- **Retenção**, nesta ordem:
  1. idade (`retention_days`);
  2. limite global (`max_storage_gb`);
  3. espaço livre mínimo (`min_free_gb`).

  Apaga do mais antigo para o mais novo, pula gravações protegidas ("importantes") e só remove
  arquivos que estão no índice, dentro da pasta raiz e com o marcador `.monitorios-root` presente.
- **Reconciliação remota**: arquivos do MonitorIOS no SMB que não estão no índice voltam a ser
  indexados, e entradas cujo arquivo sumiu são marcadas como `missing`.

## Cópia local das gravações (`app/storage/mirror.py`)

O compartilhamento do roteador leva de 0,1 s a mais de 10 s para abrir um arquivo enquanto as
câmeras gravam, e lê a cerca de 9 MB/s. Para a reprodução não esperar por isso, o servidor mantém
uma cópia das gravações no disco do PC (`C:\ProgramData\MonitorIOS\mirror`):

- o arquivo do buffer de cada segmento enviado ao pendrive é **movido** para a cópia local em vez
  de apagado (custo zero);
- ao assistir um segmento que só está no pendrive, ele é copiado no primeiro pedido e os 5
  seguintes da mesma câmera vão para a fila em segundo plano;
- as últimas 48 h são copiadas proativamente;
- gravações do Agent DVR são copiadas (somente leitura) para `mirror\_agentdvr`, em segundo plano.
  O arquivo original nunca é alterado.

O pendrive continua sendo o armazenamento oficial. A cópia é usada só se tiver o mesmo tamanho do
original, segue a retenção (quando o original é apagado, a cópia também é) e tem tamanho máximo
(`local_cache_gb`, padrão 40 GB; 0 desativa), apagando as mais antigas primeiro.

## Linha do tempo e reprodução (`app/recordings/timeline.py`, `app/streaming/hls.py`)

- `/timeline` devolve os intervalos gravados (segmentos unidos com tolerância de 2 s) e as lacunas.
- `/locate?t=` informa se há gravação no instante pedido e, se não houver, a próxima disponível.
- `/playback` gera uma **playlist HLS VOD** para o período, com:
  - `EXT-X-BYTERANGE`: partes de cerca de 4 s (`hls_part_seconds`) dentro de cada arquivo, sempre
    começando num keyframe, para o player começar a tocar com pouco dado;
  - `EXT-X-PROGRAM-DATE-TIME` em cada parte: o iPhone sabe o horário real de cada quadro;
  - `EXT-X-DISCONTINUITY` em lacunas e reinícios: o player pula buracos sozinho.

  O app usa `AVPlayerItem.seek(to: Date)` para ir ao horário escolhido e `currentDate()` para mostrar
  o horário real. Não há conversão de vídeo no servidor durante a reprodução.
- Mídia servida com suporte a `Range` (206) e token de mídia curto e assinado (`?mt=`), já que o
  AVPlayer não envia cabeçalhos de autorização.

## Ao vivo (`app/streaming/hub.py`)

- **Conexão permanente**: com `live_always_on` (padrão), o servidor mantém um FFmpeg por câmera
  ligado o tempo todo (`RTSP → FLV` em `pipe:1`, vídeo `-c:v copy`, áudio G.711 µ-law 8 kHz).
  A imagem secundária é aberta sob demanda e fecha após `live_idle_timeout_seconds` sem espectador.
- O servidor guarda o GOP atual (do último keyframe em diante). Quem abre o ao vivo recebe esse GOP
  de uma vez, marcado como "recuperação": o app decodifica sem exibir e mostra o quadro mais recente.
  Resultado medido: primeira imagem imediata com o servidor já conectado (cerca de 3 s se a câmera
  acabou de ser ligada).
- Transporte: WebSocket `/api/v1/live/{id}/ws?quality=main|sub`, autenticado por `Bearer` (ou
  `?mt=`). Mensagens binárias com cabeçalho de 10 bytes (`tipo, flags, ts_ms, cts_ms`):
  1 configuração do vídeo (avcC/hvcC), 2 quadro, 3 configuração do áudio, 4 áudio. Mensagens de
  texto JSON: `info` e `reset` (câmera reconectando).
- Espectador lento: fila de no máximo 400 KB; acima disso os quadros são descartados até o próximo
  keyframe, para o atraso não crescer.
- Queda da câmera: reconexão com espera crescente (1 → 30 s); os espectadores continuam ligados e
  recebem a imagem quando ela voltar.
- A captura (`snapshot.jpg`) é decodificada do último keyframe guardado (menos de 0,1 s).
- O HLS ao vivo (`app/streaming/live.py`) continua disponível para o painel web.

## Exportação (`app/recordings/export.py`)

- Concatena os segmentos do intervalo com o demuxer `concat`, com recorte exato (inpoint/outpoint) e
  `-c:v copy`. Gera MP4 com `faststart`. O progresso é consultado pela API. Lacunas são informadas
  ao usuário, e o vídeo salta esses trechos. O limite é de 6 h por exportação, e os arquivos são
  apagados após 24 h.

## API

Base `/api/v1`. Autenticação por `Authorization: Bearer <token>` (app) ou por cookie e cabeçalho
anti-CSRF (painel).

- Câmeras: `/cameras` (CRUD), `/cameras/{id}/test|restart|snapshot.jpg|live|days|timeline|locate|playback|protect`
- Ao vivo: WebSocket `/live/{id}/ws`
- Exportação: `/exports` (POST/GET/DELETE, `/download`)
- Sistema: `/system/status|storage|events`, WebSocket `/ws` com o estado em tempo real
- Administração (só localmente): `/admin/settings`, `/admin/storage/*`, `/admin/maintenance/*`,
  `/admin/users`, `/admin/onvif/*`, `/admin/import/agentdvr`, `/admin/logs`
- `/docs` (Swagger) também só abre localmente.

## App iOS

SwiftUI para iOS 17 ou superior, sem dependências externas.

- `AppModel`: servidor, sessão (token no Keychain), câmeras e estado. Usa WebSocket com reconexão,
  com consulta periódica como reserva, e reage a troca entre Wi-Fi e dados móveis (`NWPathMonitor`)
  e à volta do app para o primeiro plano.
- `LivePlayerModel` (`Services/LiveStream.swift`): ao vivo pelo WebSocket, decodificação por
  hardware (VideoToolbox) e exibição em `AVSampleBufferDisplayLayer`. Um relógio adaptativo
  (`JitterClock`) decide quando cada quadro aparece: segue o caminho mais rápido da rede e aumenta
  a margem só quando quadros chegam atrasados. Reconecta sozinho mantendo a última imagem,
  com zoom digital (pinça e toque duplo) e áudio opcional.
- `PlaybackModel`: uma playlist por dia. Cuida da busca por horário, de pular lacunas, de passar ao
  dia seguinte, da velocidade (1–16×) e de recarregar quando a gravação de hoje cresce.
- `TimelineBar`: régua de 24 h desenhada em `Canvas`. Arrastar escolhe o horário, a pinça aproxima
  (5 min a 24 h) e um toque salta ao ponto.
