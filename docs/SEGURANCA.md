# Segurança

## Credenciais e segredos

- **Senhas de câmeras e do SMB** são cifradas com **DPAPI**, no escopo da máquina e com entropia
  própria, e guardadas no SQLite em `C:\ProgramData\MonitorIOS`, cuja ACL só permite SYSTEM e
  Administradores. Elas são decifradas apenas no momento de abrir o RTSP ou o SMB.
- A API **nunca** devolve senhas, apenas `has_password`. URLs RTSP são exibidas sem `usuário:senha`.
  Se você colar uma URL com credenciais, elas são separadas e cifradas.
- **Logs**: um filtro remove `rtsp://usuário:senha@`, `token=`, `mt=`, `password=`, `Bearer …` e
  `<Password>` do ONVIF antes de gravar. O log de acesso registra só o caminho, sem a query string.
  Há teste automático que verifica isso.
- Usuários do app: senha com **scrypt**. Sessões são tokens aleatórios guardados no banco apenas como
  hash SHA-256, com expiração deslizante (30 dias por padrão). Trocar a senha encerra todas as
  sessões.
- **No iPhone**, o token fica no **Keychain** (`AfterFirstUnlockThisDeviceOnly`). A senha não é
  armazenada.
- Nada disso vai para o repositório. O `.gitignore` bloqueia `*.p12`, `*.mobileprovision`,
  `settings.json` e o banco.

## Proteções da API

- **Limite de tentativas de login** por IP e por usuário: após 5 falhas, o bloqueio cresce
  progressivamente e a API responde 429.
- **Tokens de mídia** (HLS, segmentos, download) são assinados com HMAC, valem por pouco tempo, ficam
  restritos a uma câmera ou exportação e são vinculados à sessão. Encerrar a sessão invalida todos.
- **Ao vivo por WebSocket** (`/api/v1/live/{id}/ws`): exige o token de sessão no cabeçalho
  `Authorization` (ou um token de mídia da câmera). Sem autenticação a conexão é fechada com o
  código 4401, antes de qualquer imagem.
- **Cópia local das gravações** (`C:\ProgramData\MonitorIOS\mirror`) fica sob a mesma ACL (SYSTEM e
  Administradores). Das gravações do Agent DVR o servidor só **lê** o original para copiar; a limpeza
  da cópia local só apaga arquivos dentro dessa pasta.
- **Caminhos**: todo acesso a arquivo passa por `safe_join`, que rejeita `..`, caminhos absolutos e
  qualquer coisa fora da raiz. A mídia é servida pelo ID do índice, nunca por um caminho vindo do
  cliente.
- **Gravações do Agent DVR** são só lidas, com `location='agentdvr'` no índice. A retenção e a cota
  ignoram essa localização, e `.mkv` não é extensão que o MonitorIOS apague. Quando um arquivo some
  ou envelhece, sai apenas a linha do índice. As credenciais do `objects.json` não são usadas nessa
  leitura, só o id e o host de cada câmera.
- **Administração** (`/admin`, `/api/v1/admin/*`, `/docs`) só responde em `127.0.0.1`/`::1`, a menos
  que `admin_remote_access=true`.
- **CORS** desativado. O painel usa cookie `HttpOnly` e `SameSite=Strict`, mais o cabeçalho
  `X-Requested-With` contra CSRF.
- Cabeçalhos `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` e `Cache-Control:
  no-store` nas respostas da API.

## Acesso remoto: Tailscale

- **Nenhuma porta é aberta no roteador.** O iPhone acessa `http://100.113.162.101:8765` pela VPN do
  Tailscale (plano gratuito). A regra de firewall do Windows só aceita a rede local e a faixa
  `100.64.0.0/10`.
- Para entrar na rede do Tailscale, um aparelho precisa estar autenticado na sua conta. Ative MFA na
  conta (Google, Microsoft etc.) e, se quiser, use ACLs do Tailscale para limitar quais aparelhos
  chegam à porta 8765.

### Limites do HTTP sem TLS

O tráfego dentro do Tailscale é **cifrado de ponta a ponta pelo WireGuard**, mesmo usando `http://`.
Ainda assim:

- quem estiver **na mesma rede local** que o servidor (o Wi-Fi de casa) poderia, em tese, capturar o
  tráfego HTTP entre o iPhone e o servidor quando você usa o IP da rede local. Pelo IP `100.x` do
  Tailscale, o tráfego é sempre cifrado;
- o app permite HTTP (`NSAllowsArbitraryLoads`) porque os IPs do Tailscale não têm certificado
  público. Prefira sempre o endereço `100.x`.

### Habilitar HTTPS (recomendado quando possível)

1. No painel do Tailscale, ative **MagicDNS** e **HTTPS Certificates**.
2. No servidor, gere o certificado:
   `tailscale cert <nome-do-pc>.<tailnet>.ts.net`
   Isso cria os arquivos `.crt` e `.key`. Guarde-os em `C:\ProgramData\MonitorIOS\tls`.
3. Configure e reinicie:
   ```powershell
   .venv\Scripts\python.exe -m app.cli settings --set tls_certfile=C:/ProgramData/MonitorIOS/tls/pc.crt tls_keyfile=C:/ProgramData/MonitorIOS/tls/pc.key
   .\service.ps1 restart
   ```
4. No app, use o endereço `https://<nome-do-pc>.<tailnet>.ts.net:8765`.

O certificado do Tailscale vale 90 dias. Repita o passo 2 periodicamente. Um script agendado mensal
resolve isso.
