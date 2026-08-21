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
  inventory:                       # inventário do software TOTVS (tela /pacotes)
    enabled: true
    interval_hours: 6              # cadência da coleta (horas)
    only_totvs: true               # ignora software que não é da TOTVS
    totvs_regex: "TOTVS|RM Sistemas|Microsiga|Corpore"
    watch: ["RM.Cst.*", "RM.Lib.*", "*Customizacao*", "*Customizada*", "RM*.exe"]
    custom_folders: ["Custom", "Scripts Especificos"]   # customizações do cliente
    custom_prefix: "RM.*"          # nessas pastas, só os arquivos da TOTVS/RM
    hotfixes: false                # KBs do Windows: é SO, não TOTVS
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

## Inventário do software TOTVS (`defaults.inventory`)

A tela `/pacotes` compara o software **TOTVS** instalado entre os hosts. O que
não é da TOTVS fica de fora de propósito: navegador, antivírus e runtime não
dizem nada sobre o estado do parque RM. Para inventariar tudo, use
`only_totvs: false`.

A coleta é separada do ciclo de métricas (padrão: a cada 6 h, ~10 s por host) e
produz cinco fontes:

| Fonte | O que é | Exemplo real |
|---|---|---|
| `rm` | **Versão base do produto**: a versão mais frequente entre os assemblies da TOTVS na pasta de instalação | `12.1.2602.1` (2.950 dos 4.200 assemblies) |
| `assembly` | Arquivos rastreados um a um (`watch`): bibliotecas `RM.Lib.*`, interfaces de customização e executáveis, cada um com seu nível de patch | `RM.exe` em `12.1.2602.198` |
| `custom` | A pasta `Custom` da instalação — onde ficam as customizações do cliente | `RM.Cst.CNI_DN.PortalSESI.Form.dll` |
| `registry` | Chaves de desinstalação, filtradas pela TOTVS | `BibliotecaRM`, `TOTVS_CES_RM_Office365_CNI` |
| `hotfix` | `Get-HotFix` (KBs do Windows) — **desligado** por padrão | `KB5034441` |

A pasta de instalação não é configurada: sai do caminho dos serviços já
descobertos por `service_patterns` (`RM.Host*`). Use `paths` só se houver uma
instalação sem serviço associado.

> **`Win32_Product` não é usado.** Consultar aquela classe WMI dispara a
> auto-reparação do MSI em cada pacote, leva minutos e é conhecida por quebrar
> instalação em produção. As chaves de desinstalação dão a mesma informação em
> ~1 s.

**Versão base e resíduo.** Numa instalação do RM a maioria dos assemblies fica
na versão do produto e um punhado sobe de patch pelo RM.Atualizador — isso é
normal e esperado. O que chama atenção é o contrário: arquivo **da mesma linha
de release** que ficou para trás da base, resto de uma atualização que não
trocou tudo. Esse caso vira uma linha própria (`RM - assemblies abaixo da versao
base`), comparável entre hosts.

A ressalva da "mesma linha" importa: a pasta do RM mistura assemblies que seguem
a versão do produto (`12.1.xxxx`) com bibliotecas que a TOTVS assina mas
versiona por conta própria (`1.0.0.0`, `6.0.290.0`). Sem separar as duas coisas,
o resíduo apontaria sempre para um `1.0.0.0` que nunca fez parte da linha do
produto.

**Referência de comparação:** a maior versão encontrada no parque. Não existe
catálogo público consultável do TOTVS RM, então "estar atualizado" só pode
significar "estar no mesmo nível do host mais novo".

**Data de instalação:** para arquivos é a data do próprio arquivo; para o
registro, o `InstallDate` — que vem vazio na maior parte dos pacotes. Quando
falta, o painel mostra a data da primeira coleta que viu aquele item, marcada
com `*`. A linha do tempo confiável é a de `/pacotes/mudancas`, montada pelo
diff entre coletas: instalação, atualização, **regressão** de versão e remoção,
com data e hora.

Mudança de item gera registro em `alerts_log` e notificação (Telegram/Slack),
quando configurada. Não é alerta de falha: é rastreabilidade — software que muda
sem ninguém ter mexido é a primeira coisa que se procura quando o servidor passa
a se comportar diferente.

> Mudar o formato do inventário (fontes, chaves) faz o RMon **recolher tudo do
> zero** na próxima coleta, via `db.INVENTORY_SCHEMA`. Isso evita que as linhas
> antigas sejam lidas como desinstalação e encham a linha do tempo de remoções
> que nunca aconteceram.

## Versões disponíveis (repositório de pacotes)

A tela `/pacotes` mostra uma coluna **Disponível** ao lado dos hosts: a maior
versão de cada item que **já foi baixada do TDN**.

> **O RMon não acessa o TDN.** Automatizar login no portal com a credencial de
> alguém seria frágil (o Confluence muda, o SSO expira, um bloqueio derruba a
> coleta em silêncio) e exigiria guardar essa senha no servidor. O caminho aqui
> é outro: **baixar o pacote já é a declaração de que aquela versão existe**.

Duas formas de alimentar o catálogo:

**1. Repositório de pacotes** (`RMON_PACKAGES_DIR`, padrão `./pacotes` — na VM,
`/opt/rmon/pacotes`). Coloque ali os arquivos baixados do TDN. O RMon só **lê**
a pasta: nunca escreve, nunca apaga, nunca executa nada. A cada coleta de
inventário (e no botão *Ler repositório*) ele varre a pasta e tira produto e
versão do nome do arquivo:

```
pacotes/
├── TOTVS_CES_RM_Office365_CNI_12.1.2602.002.exe   → produto ...CNI, versão 12.1.2602.002
├── BibliotecaRM_12.1.2606.120.exe                 → BibliotecaRM 12.1.2606.120
└── customizacoes/RM.Cst.CNI.Lib.Api_12.1.2602.003.dll
```

A versão é o último número de 3 ou 4 componentes no nome; o que vem antes é o
produto. Separador não importa (`_`, `-`, espaço). Extensões reconhecidas:
`.exe`, `.msi`, `.zip`, `.rar`, `.7z`, `.cab`, `.dll`. Subpastas viram rótulo de
agrupamento (até 3 níveis, no máximo 2.000 arquivos).

**2. Registro manual** (`/pacotes/catalogo`, admin). Para o que o nome do arquivo
não revela, ou para marcar uma versão como alvo antes de baixar: produto,
versão, link do TDN e observação.

### Como o pacote encontra o item instalado

O nome do arquivo é comparado com o nome do item do inventário descartando tudo
que não é letra ou número — `TOTVS-CES-RM-Office365-CNI` casa com
`TOTVS_CES_RM_Office365_CNI` do registro. Pacotes do próprio produto (`RM`,
`CorporeRM`) são ligados por apelido ao item `RM - versao base`.

Sem casamento automático, a entrada aparece em `/pacotes/catalogo` esperando que
alguém diga a que ela pertence — melhor do que adivinhar errado e anunciar
"atualização disponível" para o item errado. O vínculo é **por produto**, não
por arquivo: sobrevive à próxima varredura e vale para a próxima versão do mesmo
pacote.

### Última atualização aplicada

O `RM.Atualizador` deixa um log por execução na pasta `Atualizador` da
instalação. O RMon lê a data do mais recente e mostra no cabeçalho de cada host
(`↻ dd/mm/aaaa`) — dá para ver, por exemplo, que um pacote foi baixado em agosto
e o host ainda está com a atualização de julho.

## Instalar e atualizar nos hosts (`defaults.execution`)

A tela `/pacotes/tarefas` enfileira tarefas de instalação. Ela nasce
**desarmada** e a maior parte do valor está no modo que não instala nada.

### Pré-voo (o padrão)

Com `execution.enabled: false`, toda tarefa é um **pré-voo**: o RMon faz
checagens **somente-leitura** no host e mostra o comando exato que seria
executado. Nada é baixado, copiado ou iniciado.

O que ele confere:

| Checagem | Pergunta |
|---|---|
| `acao` | a ação existe no catálogo permitido? |
| `habilitado` / `host_liberado` | a execução real está armada para este host? |
| `janela` | estamos na janela de manutenção? |
| `sessoes` | quantas sessões RDP ativas há agora? |
| `disco` | há espaço livre suficiente (mínimo, ou o dobro do pacote)? |
| `destino` | a pasta de destino existe? |
| `servicos` | os serviços exigidos estão parados? |
| `pacote` | o arquivo existe no repositório? qual o SHA-256? |
| `versao` | o host está mesmo atrás da versão do pacote? |
| `alcance` | o host consegue chegar no RMon para baixar o pacote? |

Uma checagem **dura** reprovada bloqueia a tarefa: ela não "tenta assim mesmo".

### Armar a execução real

Três coisas, todas obrigatórias, nenhuma delas por acidente:

1. `execution.enabled: true` no `servers.yaml`;
2. o host listado em `execution.hosts` (a lista vazia, que é o padrão, não
   libera ninguém);
3. na tela, digitar o nome do host exatamente como ele aparece no painel.

Além disso o pré-voo roda de novo antes de agir, e a trava mestra é conferida
mais uma vez no momento da execução — entre enfileirar e executar, alguém pode
ter desarmado, e quem manda é a configuração na hora de agir.

### Ações: não existe comando livre

O que roda sai de `execution.actions`. O executável é **sempre** o pacote que o
próprio RMon colocou no host, e os argumentos são texto fixo do YAML. Nada
digitado na tela entra numa linha de comando. Uma ação é descartada (com aviso
no log) se o `id` tiver formato estranho, se `dest` não for uma subpasta
simples, ou se `args` contiver `;`, `&`, `|`, `<`, `>`, `` ` `` ou `$`.

Dois tipos:

- **`dest`** → copia o arquivo para aquela subpasta da instalação do RM
  (a pasta da instalação vem do inventário, não de configuração nova);
- **`args`** → executa o pacote com aqueles argumentos e espera terminar.

### Como o pacote chega ao host

O RMon serve o arquivo em `/pacotes/stage/{id}`, com assinatura HMAC e prazo,
válido só enquanto aquela tarefa está viva e só com a execução habilitada. O
host baixa, **confere o SHA-256 antes de executar** e apaga o arquivo
temporário no fim.

Isso exige que o host alcance o RMon na porta do painel — é o que a checagem
`alcance` responde. Se o firewall da VM não liberar a faixa dos servidores RM, a
execução real fica bloqueada (o pré-voo continua funcionando normalmente).

### Depois de um restart

Tarefa que ficou `running` quando o processo morreu é marcada como falha e
**não** é repetida: repetir uma instalação sozinho, sem ninguém pedir, é pior do
que exigir um clique novo.

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

### Broker do RM (`defaults.broker`)

O `RM.Host` só gera `_BrokerCustom.dat` — o cache das customizações da pasta `Custom` —
quando o arquivo **não existe**. Se a geração aborta no meio (falta de *commit*, ver
abaixo), sobra um arquivo curto, e a partir daí todo start reaproveita esse cache: o
serviço sobe "com sucesso" e as customizações não carregam.

A coleta lê o tamanho e a data desses arquivos na pasta de instalação (descoberta pelo
caminho dos serviços de `service_patterns`) e compara **host a host**: o tamanho certo
depende de quantas customizações o cliente tem, então a referência é o maior arquivo do
parque — o mesmo que a operação copia à mão quando um host sobe truncado.

| Chave | Padrão | Para que serve |
|---|---|---|
| `broker.enabled` | `true` | Liga a verificação |
| `broker.files` | `["_BrokerCustom.dat", "_Broker.dat"]` | Arquivos verificados na pasta do RM |
| `alerts.broker_min_pct` | `60` | Alerta abaixo desse % do maior arquivo do parque |
| `alerts.broker_min_kb` | `0` | Piso absoluto em KB (0 = só a regra relativa) |
| `alerts.broker_settle_min` | `10` | Não julga arquivo gerado há menos que isso |
| `alerts.commit_pct` | `90` | Alerta de *commit charge* (RAM + pagefile reservados) |

O `commit_pct` é a causa raiz, não um extra: é o limite de *commit* — e não a RAM livre —
que estoura com `ERROR_COMMITMENT_LIMIT (0x800705AF)` e derruba a geração do broker.
Procedimento de recuperação em [OPERACAO.md](OPERACAO.md#broker-truncado-customizações-não-carregam).

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
