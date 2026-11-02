# App iOS: compilação e instalação

## Compilação no GitHub Actions

Não há Mac no ambiente de desenvolvimento, então o app é compilado no CI
(`.github/workflows/ios.yml`, runner `macos-15`):

1. Gera o projeto Xcode a partir de `ios/project.yml` com **XcodeGen**. O `.xcodeproj` não fica no
   repositório.
2. Compila em Release para `iphoneos` **sem assinatura** e empacota `Payload/MonitorIOS.app` como
   **`MonitorIOS-unsigned.ipa`**.
3. Publica o IPA como *artifact* da execução: aba Actions → execução → Artifacts.
4. Depois, roda os testes unitários (`MonitorIOSTests`) num simulador. Uma falha nos testes não
   impede o download do IPA.

O repositório é **público** (github.com/ilucascesar/MonitorIOS) para que o macOS do Actions não
consuma a cota da conta. Ele não contém senhas nem tokens.

Para compilar num Mac: `brew install xcodegen && cd ios && xcodegen && open MonitorIOS.xcodeproj`.

## Instalação com o iLoader

O IPA gerado pelo CI **não é instalável diretamente**: o iOS só executa apps assinados. O
iLoader (ou ferramentas equivalentes, como Sideloadly e AltStore/SideStore) **assina o IPA com o seu
Apple ID no momento da instalação**.

1. Baixe `MonitorIOS-unsigned-ipa.zip` dos *artifacts* e extraia o `.ipa`.
2. Conecte o iPhone ao PC e abra o iLoader. Entre com o seu Apple ID e escolha o `.ipa`.
3. No iPhone, ative **Ajustes → Privacidade e Segurança → Modo de Desenvolvedor** (iOS 16+) e confie
   no perfil de desenvolvedor em **Ajustes → Geral → VPN e Gerenciamento de Dispositivos**.
4. Abra o app. O servidor já vem preenchido como `http://100.113.162.101:8765`. Com o Tailscale
   ligado, toque em "Testar conexão" e entre com seu usuário.

### Limites da assinatura (não contornáveis pelo CI)

| Conta | Validade do app | Outros limites |
|---|---|---|
| Apple ID **gratuito** | **7 dias**. Depois disso, o app não abre até ser reinstalado ou renovado pelo iLoader | Até 3 apps "sideloaded" ativos por aparelho e 10 App IDs a cada 7 dias. A ferramenta pode alterar o bundle ID |
| Apple Developer Program (pago) | 1 ano (perfil de desenvolvimento ou ad hoc) | Aparelho cadastrado no perfil |

Ao renovar com o **mesmo Apple ID**, o token salvo no Keychain é mantido. Com outro Apple ID, será
preciso entrar de novo.

## IPA assinado no CI (opcional, conta paga)

Se estes segredos existirem no repositório (Settings → Secrets and variables → Actions), o workflow
também gera `MonitorIOS-signed.ipa`:

| Segredo | Conteúdo |
|---|---|
| `IOS_P12_BASE64` | Certificado de assinatura `.p12` em base64 (`base64 -i cert.p12`) |
| `IOS_P12_PASSWORD` | Senha do `.p12` |
| `IOS_PROFILE_BASE64` | Perfil `.mobileprovision` em base64 |

O bundle ID, o time e o método de exportação (development, ad hoc ou App Store) são lidos do próprio
perfil. Sem esses segredos, essa etapa é simplesmente pulada. **Nunca** coloque certificados ou
senhas em arquivos do repositório.

## Uso do app

- **Câmeras**: grade com o vídeo ao vivo de todas as câmeras (o botão no topo troca para miniaturas),
  estado online/offline e gravação. Toque numa câmera para abrir **AO VIVO / GRAVAÇÕES**; o botão
  de relógio no canto de cada câmera abre direto as gravações dela.
- **Ao vivo**: abre na hora (o servidor fica conectado às câmeras o tempo todo), com decodificação
  por hardware e atraso mínimo. Pinça ou toque duplo para ampliar, áudio (desligado por padrão),
  qualidade alta ou econômica, captura de imagem e tela cheia (gire o iPhone). Se a rede cair, a
  última imagem fica na tela com o aviso "Reconectando…" e o vídeo volta sozinho.
- **Gravações**: calendário do mês (dias com gravação destacados pela quantidade de horas) e linha do
  tempo de 24 h:
  - arrastar a régua escolhe o horário;
  - a pinça ou os botões aproximam e afastam;
  - um toque salta ao ponto;
  - ±10 s e ±1 min, velocidade de 1× a 16×;
  - numa lacuna, o app mostra "Sem gravação às HH:MM" com o botão "Ir para" a próxima gravação;
  - ao fim do dia, continua no dia seguinte.
- **Exportar**: escolha início e fim. O servidor monta o MP4 sem recodificar, o app baixa e abre a
  folha de compartilhamento (Salvar vídeo / Arquivos / AirDrop). "Marcar como importante" impede que
  a limpeza automática apague o trecho.
- **Ajustes**: endereço do servidor, estado do armazenamento, tema (escuro por padrão) e sair.
