# Configuração

A configuração do RMonitor está em **dois lugares**:

- **`config/servers.yaml`** — inventário (o *que* monitorar). Versionável apenas como `.example`.
- **`.env`** — segredos (chaves, senhas, credenciais). **Nunca** versionado.

---

## Inventário (`config/servers.yaml`)

Copiado de `config/servers.example.yaml`. Estrutura completa:

```yaml
poll_interval_seconds: 60          # intervalo do polling (segundos)

winrm:
  scheme: https                    # https (5986, recomendado) ou http (5985)
  port: 5986
  transport: ntlm                  # ntlm | kerberos | credssp
  server_cert_validation: ignore   # ignore (self-signed) | validate
  read_timeout_sec: 30
  operation_timeout_sec: 25

defaults:                          # aplicados a todos os servidores (sobrescrevíveis)
  alerts:
    disk_pct: 90                   # alerta se disco principal (C:) >= 90%
    mem_pct: 90                    # alerta se memória >= 90%
    app_ms: 3000                   # app_health respondendo, porém > 3000ms = "lento"
    jobs_failed: 3                 # alerta se >= N jobs falharam na janela
    down_after: 3                  # coletas seguidas sem contato antes de alertar DOWN
  services: []                     # lista padrão de serviços fixos (se um servidor não definir a sua)
  service_patterns: ["RM.Host*"]   # descoberta automática (curinga) — ver abaixo
  eventlog:
    logs: [System, Application]
    lookback_hours: 24             # janela de ocorrências do Event Log
    noise_ids: [10016, 1058, 1030, 1502, 1500, 7000, 7009]   # IDs ignorados (ruído)
    providers_regex: '\b(RM|TOTVS|SGE|MSSQL|SQL|IIS|W3SVC|WAS|DBAccess|WER)|\.NET Runtime|ASP\.NET|Application Error|Application Hang'
  inventory:                       # inventário de software (tela /pacotes)
    enabled: true
    interval_hours: 6              # cadência da coleta de inventário (horas)
    hotfixes: true                 # incluir os KBs do Windows (Get-HotFix)
    binaries: ["RM.exe", "RM.Host.exe", "RM.Host.Service.exe"]   # versão real do RM
    ignore: ["Update for Microsoft*", "Security Update for*"]     # ruído do registro

servers:
  - name: rm-app-01                # rótulo único no painel
    host: 10.0.0.50                # IP/hostname do alvo
    cred: fin                      # perfil de credencial WinRM (ver .env). Default: 'default'
    services:                      # serviços Windows fixos monitorados neste host
      - W3SVC
    service_patterns:              # descobertos no host a cada coleta
      - "RM.Host*"
    app_health:                    # (opcional) checagem HTTP da aplicação
      url: "http://10.0.0.50:8051/"
      expect_status: 200           # status HTTP esperado
      timeout_sec: 8
    jobs:                          # (opcional) estatísticas de jobs do RM (requer RMON_SQL_*)
      window_min: 15               # janela em minutos
      servidor: SRV06N             # executor deste host (coluna SERVIDOR = "SRV06N:porta")
      success_status: [2]          # STATUS de sucesso na GJOBXEXECUCAO
      failed_status: [5, 7]        # STATUS de falha

  - name: rm-db-01
    host: 10.0.0.51
    cred: rh
    services: [MSSQLSERVER, SQLSERVERAGENT]
    # sem app_health / jobs (servidor de banco)
```

### Campos por servidor

| Campo | Obrigatório | Descrição |
|---|---|---|
| `name` | ✅ | Identificador único exibido no painel |
| `host` | ✅ | IP ou hostname do alvo (destino do WinRM) |
| `cred` | — | Perfil de credencial (mapeia para `RMON_CRED_<PERFIL>_*` no `.env`). Padrão `default` |
| `services` | — | Serviços Windows fixos a checar. Se omitido, usa `defaults.services` |
| `service_patterns` | — | Curingas descobertos no próprio host a cada coleta. Se omitido, usa `defaults.service_patterns` |
| `app_health` | — | `url`, `expect_status`, `timeout_sec` — checagem HTTP independente do WinRM |
| `jobs` | — | `window_min`, `servidor`, `success_status`, `failed_status` — requer login SQL configurado |

### Serviços fixos x descobertos (`service_patterns`)

Instâncias de RM.Host nascem e morrem: o time instala uma nova ou desinstala
duas, e o inventário fica desatualizado — foi o que aconteceu no
`fin-sge-app-34`, onde dois RM.Host desinstalados continuavam listados e o
monitor acusava dois serviços em falha.

Com `service_patterns`, o monitor pergunta ao próprio host, a cada coleta,
quais serviços casam com o curinga (compara **nome** e **nome de exibição**,
ex.: `RM.Host*`). O que sobra vira a lista real de RM.Hosts instalados, e o
painel mostra `RM.Host* · 4/4 em execução`.

A diferença de tratamento entre os dois:

| Situação | Fixo (`services`) | Descoberto (`service_patterns`) |
|---|---|---|
| Serviço não existe no host | 🔴 falha (`NOT_FOUND`) | não aparece — desinstalar não gera alerta |
| Instalado, início `Auto`, parado | 🔴 falha | 🔴 falha |
| Instalado, início `Manual`/`Disabled`, parado | 🔴 falha | 🟡 âmbar (parado de propósito) |

Use `services` para o que **tem** que existir (`W3SVC`, `MSSQLSERVER`) e
`service_patterns` para o que varia com a instalação (os RM.Host). Os dois
podem conviver no mesmo servidor; um serviço que casa nos dois é contado uma
vez só, como fixo. Ações `start`/`stop`/`restart` funcionam também nos
descobertos (limitadas ao que apareceu na última coleta daquele servidor).

> A regra `providers_regex` usa `\b` (fronteira de palavra) para evitar falsos
> positivos (ex.: `FilterManager` casando `rm`). Ajuste conforme seu ambiente.

---

## Inventário de software (`defaults.inventory`)

A tela `/pacotes` compara o software instalado entre os hosts. A coleta é
separada do ciclo de métricas (padrão: a cada 6 h) e lê **três** fontes numa só
ida ao servidor:

| Fonte | O que traz | Por quê |
|---|---|---|
| `registry` | Chaves de desinstalação (HKLM 64 e 32 bits): nome, versão, fabricante, data | Fonte canônica de "programas instalados" |
| `binary` | `FileVersion` dos executáveis dos serviços casados por `service_patterns` | No RM a versão que vale é a do binário — o registro fica defasado após um update |
| `hotfix` | `Get-HotFix` (KBs do Windows) | Comparar nível de patch entre servidores |

> **`Win32_Product` não é usado.** Consultar aquela classe WMI dispara a
> auto-reparação do MSI em cada pacote, leva minutos e é conhecida por quebrar
> instalação em produção. As chaves de desinstalação dão a mesma informação em
> ~1 s.

**Referência de comparação:** a maior versão encontrada no parque. Não existe
catálogo público consultável do TOTVS RM, então "estar atualizado" só pode
significar "estar no mesmo nível do host mais novo".

**Data de instalação:** o `InstallDate` do registro vem vazio na maior parte dos
pacotes. Quando falta, o painel mostra a data da primeira coleta que viu aquele
pacote, marcada com `*`. A linha do tempo confiável é a de `/pacotes/mudancas`,
montada pelo diff entre coletas — ela registra instalação, atualização,
**regressão** de versão e remoção, com data e hora.

Mudança de pacote gera registro em `alerts_log` e notificação (Telegram/Slack),
quando configurada. Não é alerta de falha: é rastreabilidade — software que muda
sem ninguém ter mexido é a primeira coisa que se procura quando o servidor passa
a se comportar diferente.

## Variáveis de ambiente (`.env`)

Modelo completo em [`.env.example`](../.env.example). A aplicação lê o `.env` com um
parser Python próprio (o `systemd` **não** usa `EnvironmentFile` — ver
[ARQUITETURA.md](ARQUITETURA.md#por-que-não-environmentfile-no-systemd)).

| Variável | Uso |
|---|---|
| `RMON_SECRET_KEY` | Chave para assinar o cookie de sessão. Gere com `openssl rand -hex 32` |
| `RMON_ADMIN_USER` | Login do admin semeado no 1º start |
| `RMON_ADMIN_PASSWORD_HASH` | Hash pbkdf2 da senha do admin (gerado pelos helpers) |
| `RMON_DB_DSN` | **Obrigatório.** DSN do PostgreSQL: `postgresql://user:senha@host:porta/banco` |
| `RMON_CRED_<PERFIL>_USER` / `_PASSWORD` | Credencial WinRM do perfil (ex.: `RMON_CRED_FIN_USER`). Usuário no formato `DOMINIO\usuario` |
| `RMON_WINRM_USER` / `_PASSWORD` | Par legado, usado por servidores **sem** `cred` |
| `RMON_SQL_HOST`/`_PORT`/`_DB`/`_USER`/`_PASSWORD` | Login (somente-leitura) do SQL Server p/ estatísticas de jobs |
| `RMON_TELEGRAM_TOKEN` / `RMON_TELEGRAM_CHAT_ID` | Alerta via Telegram |
| `RMON_SLACK_WEBHOOK` | Alerta via Slack (Incoming Webhook) |
| `RMON_CONFIG` | Caminho do `servers.yaml` (padrão `/opt/rmon/config/servers.yaml`) |
| `RMON_HOST` / `RMON_PORT` | Bind do servidor (em prod o systemd fixa `0.0.0.0:8080`) |
| `RMON_TV_TOKEN` | Token do mural em quiosque: libera `/tv?token=...` sem login (ver abaixo) |
| `RMON_COOKIE_SAMESITE` | `lax` (padrão), `strict` ou `none`. Use `none` só com HTTPS |
| `RMON_COOKIE_SECURE` | `1` marca o cookie de sessão como `Secure` (exige HTTPS) |

Prefira sempre os **helpers** de `deploy/` para gravar segredos — eles preservam a
permissão `600` e não deixam a senha no histórico:

```bash
sudo deploy/definir-senha-admin.sh
sudo deploy/definir-winrm.sh <perfil>
sudo deploy/definir-sql.sh
sudo deploy/definir-telegram.sh
sudo deploy/definir-slack.sh
```

### Credenciais multidomínio (perfis)

Cada servidor referencia um perfil via `cred:`. A resolução procura
`RMON_CRED_<PERFIL>_USER/PASSWORD`; se não existir, cai no par legado
`RMON_WINRM_USER/PASSWORD`. Assim você mantém uma credencial por domínio/conta sem
repeti-la em cada host. Ações remotas (restart/logoff) exigem que a conta seja
**administradora** no alvo.

---

## WinRM nos servidores Windows

Os alvos precisam ter o WinRM habilitado e acessível pela VM. Numa sessão
PowerShell **como administrador** no servidor Windows:

```powershell
# Habilita o WinRM (cria listener HTTP 5985 e regra de firewall)
Enable-PSRemoting -Force

# (Recomendado) listener HTTPS 5986 com certificado — troque o thumbprint
New-Item -Path WSMan:\localhost\Listener -Transport HTTPS -Address * `
  -CertificateThumbPrint <THUMBPRINT> -Force
New-NetFirewallRule -DisplayName "WinRM HTTPS 5986" -Protocol TCP -LocalPort 5986 -Action Allow

# Garanta que o listener aceita NTLM (padrão em domínio)
```

- **HTTP (5985)**: o transporte NTLM cifra a *mensagem*, mas não o canal. Aceitável em rede confiável.
- **HTTPS (5986)**: cifra o transporte. Preferível; ajuste `scheme: https` e `port: 5986` no `servers.yaml`.
- **Conta**: use uma conta de serviço do domínio. Para **restart de serviços** e **logoff de sessões**, ela precisa ser **administradora local** no alvo.

Teste rápido a partir da VM (dentro do venv):

```bash
sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/descobrir.py
```

Isso lista, por servidor, o SO, serviços RM/SQL/IIS, serviços auto-start parados e
portas em escuta — útil para montar a lista `services:` do inventário.

---

## Estatísticas de jobs (SQL Server)

Se você preencher `RMON_SQL_*` e adicionar o bloco `jobs:` nos servidores, o RMonitor
consulta a tabela `dbo.GJOBXEXECUCAO` da base RM e mostra, na tela **Jobs**, sucesso ×
falha por job server e por solicitante, além das falhas recentes.

- Use um login **somente-leitura** dedicado.
- `servidor:` filtra pelo executor (coluna `SERVIDOR`, formato `NOME:porta`) — assim cada host contabiliza só os jobs que ele processou.
- `success_status` / `failed_status` mapeiam os códigos de `STATUS` (padrão: `2` = sucesso; `5`/`7` = falha).

---

## Alertas

Disparados pelo `scheduler` **apenas nas transições** de estado (problema levantou ou
resolveu), evitando repetição. Condições avaliadas a cada ciclo: servidor sem contato,
serviço fora de `Running`, `app_health` falhando ou lento, disco/memória acima do
limiar e jobs com falha. Configure os limiares em `defaults.alerts` (inventário) ou na
tela **Admin**; os canais (Telegram/Slack) via helpers. Se nenhum canal estiver
configurado, o envio é inerte (sem erro).

### Antiflapping do alerta DOWN

Uma coleta isolada que estoura o timeout do WinRM **não** é servidor fora do ar:
servidores de aplicação com muitas sessões travam esporadicamente por dezenas de
segundos, e a tentativa seguinte responde em menos de um segundo. Por isso:

- o coletor **repete a coleta uma vez** quando a falha é de timeout;
- o alerta `DOWN` só sai depois de `down_after` coletas seguidas sem contato
  (padrão 3, ajustável em `defaults.alerts` ou na tela **Admin**);
- enquanto não confirma, o mural mostra o card em **âmbar** ("coleta instável"),
  não em vermelho.

Para voltar ao comportamento antigo (alertar na primeira falha), use `down_after: 1`.

---

## Embutir o mural (`/tv`) em outro painel via iframe

Numa central de monitoramento que carrega o RMonitor dentro de um `<iframe>` de
**outro domínio/porta**, o login pela tela normal *não funciona*: o cookie de
sessão é `SameSite=Lax` e o navegador simplesmente o descarta em contexto de
terceiros — a página volta para o login sem mensagem de erro nenhuma.

**Solução recomendada (funciona em HTTP puro): token de quiosque.**

O mural é somente leitura, então ele pode ser liberado por um token fixo, sem
sessão e sem cookie:

```bash
# na VM: gera o token e grava no .env
TOKEN=$(openssl rand -hex 24)
printf 'RMON_TV_TOKEN=%s
' "$TOKEN" | sudo tee -a /opt/rmon/.env >/dev/null
sudo systemctl restart rmon
echo "$TOKEN"
```

Na central, aponte o iframe para:

```
http://<host-do-rmon>:8080/tv?token=<TOKEN>
```

O token vale **apenas** para `/tv` e `/api/tv` (leitura). Todo o resto continua
exigindo login. Ele também é aceito no cabeçalho `X-RMon-Token`. Sem
`RMON_TV_TOKEN` no `.env`, o modo quiosque fica desligado.

> Quem tiver o link tem o mural. Trate a URL como segredo e troque o token
> (novo valor no `.env` + `systemctl restart rmon`) se ela vazar.

**Alternativa (exige HTTPS): cookie cross-site.** Se o RMonitor estiver atrás de
um reverse-proxy TLS, dá para manter o login normal dentro do iframe:

```
RMON_COOKIE_SAMESITE=none
```

`SameSite=None` implica `Secure`, então o RMonitor liga o `Secure` sozinho — e o
cookie *não* será aceito em HTTP puro. Não adianta usar essa opção sem TLS.
