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
  services: []                     # lista padrão de serviços (se um servidor não definir a sua)
  eventlog:
    logs: [System, Application]
    lookback_hours: 24             # janela de ocorrências do Event Log
    noise_ids: [10016, 1058, 1030, 1502, 1500, 7000, 7009]   # IDs ignorados (ruído)
    providers_regex: '\b(RM|TOTVS|SGE|MSSQL|SQL|IIS|W3SVC|WAS|DBAccess|WER)|\.NET Runtime|ASP\.NET|Application Error|Application Hang'

servers:
  - name: rm-app-01                # rótulo único no painel
    host: 10.0.0.50                # IP/hostname do alvo
    cred: fin                      # perfil de credencial WinRM (ver .env). Default: 'default'
    services:                      # serviços Windows monitorados neste host
      - RM.Host.Service
      - RM.Host.Service1
      - W3SVC
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
| `services` | — | Serviços Windows a checar. Se omitido, usa `defaults.services` |
| `app_health` | — | `url`, `expect_status`, `timeout_sec` — checagem HTTP independente do WinRM |
| `jobs` | — | `window_min`, `servidor`, `success_status`, `failed_status` — requer login SQL configurado |

> A regra `providers_regex` usa `\b` (fronteira de palavra) para evitar falsos
> positivos (ex.: `FilterManager` casando `rm`). Ajuste conforme seu ambiente.

---

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
