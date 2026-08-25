# Operação

Rotina do dia a dia do RMonitor já instalado em `/opt/rmon`.

## Serviço systemd

```bash
systemctl status rmon            # estado atual
sudo systemctl restart rmon      # reiniciar (após editar .env ou servers.yaml)
sudo systemctl stop rmon         # parar
sudo systemctl start rmon        # iniciar
sudo systemctl disable rmon      # não subir no boot
journalctl -u rmon -f            # logs ao vivo
journalctl -u rmon -n 100 --no-pager   # últimas 100 linhas
```

Health check:

```bash
curl -fsS http://127.0.0.1:8080/healthz
```

## Telas do painel

| Rota | Descrição | Acesso |
|---|---|---|
| `/` | Dashboard: resumo (online/offline, serviços parados, alertas) e cartão por servidor | login |
| `/server/{name}` | Detalhe e histórico de um servidor | login |
| `/jobs` | Estatísticas de jobs do RM (pool, por servidor, por solicitante, falhas) | login |
| `/ocorrencias` | Central de ocorrências: problemas abertos agora, linha do tempo do que abriu/resolveu (`alerts_log`) e os erros/críticos do Event Log | login |
| `/pacotes` | Inventário TOTVS por ambiente, em seções (produto, customizações, bibliotecas, instaladores): **Resumo** (uma linha por item, hosts sob demanda) ou **Matriz** (item × host) | login (coletar: admin) |
| `/pacotes/mudancas` | Linha do tempo de instalações, atualizações, regressões e remoções | login |
| `/pacotes/catalogo` | Versões disponíveis: repositório de pacotes baixados do TDN, vínculos e registro manual | admin |
| `/pacotes/tarefas` | Instalações: pré-voo (somente leitura) e, se armada, execução de pacotes nos hosts | admin |
| `/sessions` | Sessões RDP ao vivo (ocioso e logon em colunas separadas); encerrar sessões selecionadas, com barra de progresso host a host. Aceita `?server=` e `?user=` para abrir já filtrada | admin p/ logoff |
| `/logs` | Auditoria de ações | admin |
| `/admin` | Limiares de alerta, tema/refresh da UI e resumo de usuários | admin |
| `/admin/usuarios` | Cadastro de usuários: criar, editar, papel, ativar/desativar, senha, excluir | admin |
| `/profile` | Nome, e-mail e troca da própria senha | login |
| `/tv` | Mural em tela cheia para TV (sem rolagem, atualiza sozinho) | login |

Ações sensíveis (restart de serviço, logoff de sessão, mudanças de config) exigem
perfil **admin** e são registradas na auditoria.

## Mural de TV (perfil `viewer`)

Quem entra com papel **`viewer`** cai direto no mural (`/`) e **não** acessa mais
nenhuma tela: as demais rotas redirecionam de volta para o mural e os POSTs/APIs
respondem `403`. É o modo quiosque, pensado para uma TV pendurada na sala.

O que o mural faz:

- **Cabe sempre na tela.** A grade escolhe o arranjo de colunas × linhas que rende a
  **maior letra possível** para a quantidade de servidores, e a densidade do cartão cai
  sozinha (detalhes → só CPU/memória) quando há muitos servidores. Nunca há rolagem.
- **Atualiza sem recarregar a página**: busca `/api/tv` no intervalo configurado e só
  redesenha quando os dados mudam — sem piscar, sem perder posição.
- Semáforo no topo (**TUDO OPERACIONAL** / servidores em atenção / em falha), cinco KPIs,
  cartão por servidor (vermelho pulsante quando crítico) e rodapé que **alterna** entre as
  ocorrências abertas, as mais graves primeiro.
- Conveniências de TV: relógio, barra do próximo refresh, botão de tela cheia, cursor some
  sozinho, `wake lock` para a tela não apagar, leve deslocamento periódico contra *burn-in*
  e aviso visível se o servidor ficar inacessível (com recuo exponencial na retentativa).

Ajustes em **Admin → Interface**: *Mural de TV — atualização (segundos)* (padrão **15 s**,
faixa 5–600). Não adianta ficar abaixo do `poll_interval_seconds` do inventário — o dado só
muda a cada coleta. O tema (escuro/claro) do mural segue o tema padrão do painel.

Para pendurar na TV: crie um usuário `viewer` (ex.: `tv`) em **Usuários**, abra o navegador
da TV em `http://<host>:8080/`, faça o login uma vez (a sessão é um cookie assinado) e
clique em **Tela cheia**. Um admin vê o mesmo mural em `/tv` sem perder o painel completo.

## Usuários do painel

### Pela interface (recomendado)

Logado como **admin**, acesse **Usuários** no menu (ou `/admin/usuarios`). Ali é possível:

- criar contas (login, nome, e-mail, papel e senha inicial);
- editar nome, e-mail, papel e ativar/desativar a conta;
- redefinir a senha de qualquer usuário;
- excluir contas.

Regras aplicadas pelo painel: senha de no mínimo 8 caracteres, login com 3 a 32
caracteres (letras, números, `.`, `-`, `_`), e é proibido rebaixar, desativar ou
excluir o **último administrador ativo**. Toda alteração vai para a auditoria (`/logs`).

Cada usuário troca a própria senha em **Meu perfil** (`/profile`), informando a senha atual.

### Pela CLI (roda dentro do venv, lê o `.env` para o DSN)

Útil para recuperação de acesso, quando não há admin disponível para entrar no painel:

```bash
sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py list
sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py add joao --role admin
sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py passwd joao
sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py role joao viewer
sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py disable joao
sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/usuarios.py enable joao
```

Papéis: **`admin`** (tudo, inclusive ações remotas e config) e **`viewer`** (somente leitura).

## Atualizar segredos

Sempre pelos helpers (gravam no `.env` com perm `600` e reiniciam o serviço):

```bash
sudo deploy/definir-senha-admin.sh     # troca a senha do admin
sudo deploy/definir-winrm.sh <perfil>  # rotaciona a credencial WinRM de um perfil
sudo deploy/definir-sql.sh             # troca o login SQL
sudo deploy/definir-telegram.sh        # (re)configura Telegram
sudo deploy/definir-slack.sh           # (re)configura Slack
```

## Atualizar a aplicação

Com o novo código presente (ex.: `git pull` no diretório de origem):

```bash
sudo deploy/instalar.sh                # idempotente: re-sincroniza app, deps e serviço
```

O instalador **preserva** o `.env` e o `servers.yaml` existentes — não sobrescreve
segredos nem inventário.

## Banco de dados

O histórico fica no PostgreSQL local (database `rmon`). O `scheduler` roda uma
**limpeza automática** a cada 6h: `checks` acima de 30 dias e `audit_log`/`alerts_log`
acima de 180 dias.

Backup manual (recomendado antes de mudanças grandes):

```bash
sudo runuser -u postgres -- pg_dump rmon | gzip > rmon-$(date +%F).sql.gz
```

Restaurar:

```bash
gunzip -c rmon-AAAA-MM-DD.sql.gz | sudo runuser -u postgres -- psql rmon
```

## Diagnóstico rápido

| Sintoma | Onde olhar |
|---|---|
| Painel não abre | `systemctl status rmon`, `journalctl -u rmon -n 50` |
| Servidor "sem contato" | credencial do perfil (`definir-winrm.sh`), rede/porta WinRM, `descobrir.py` |
| "Sem contato" intermitente (alterna com "resolvido") | host engasgando no WinRM, não queda: ver *Coleta instável* abaixo |
| Jobs vazios | `RMON_SQL_*` configurado? bloco `jobs:` no servidor? login com acesso à base RM? |
| Alerta não chega | canal configurado? `journalctl` mostra "falha Telegram/Slack"? |
| Serviço não sobe | `RMON_DB_DSN` válido? PostgreSQL ativo (`systemctl status postgresql`)? |

Descoberta de serviços nos alvos (útil ao ajustar o inventário):

```bash
sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/descobrir.py
```

---

## Coleta instável (alertas DOWN intermitentes)

Sintoma: um servidor alterna "sem contato" e "resolvido" várias vezes por hora,
sempre com `WinRMOperationTimeoutError`, enquanto o serviço segue atendendo os
usuários normalmente.

Não é queda do host. Medindo as fases da chamada WinRM, um servidor saudável
abre shell e devolve a saída em ~0,2s de forma constante; um servidor sobrecarregado
trava esporadicamente de 12s a 115s em qualquer fase (abrir shell, iniciar o comando
ou receber a saída) — e a tentativa imediatamente seguinte responde em menos de 1s.
Estourado o `operation_timeout_sec`, a coleta era marcada como falha e virava alerta.

O RMonitor já absorve isso (repetição da coleta + `down_after`, ver
`docs/CONFIGURACAO.md`). Se ainda assim houver alertas, a causa é do lado do Windows.
Para confirmar e agir:

```bash
# taxa de falha de coleta por servidor nas ultimas 24h
sudo -u postgres psql rmon -c "SELECT server, count(*) total,   count(*) FILTER (WHERE NOT reachable) falhas FROM checks   WHERE ts > now() - interval '24 hours' GROUP BY 1 ORDER BY 2 DESC;"
```

Nos hosts afetados, verificar na aba **Ocorrências** se há `Application Error`/
`Application Hang` do `RM.exe` e quantas sessões RDP estão abertas: travamentos de
WinRM costumam acompanhar sobrecarga do servidor de aplicação. Encerrar sessões
presas (tela **Sessões**) ou reiniciar o host resolve o sintoma.

---

## Broker truncado (customizações não carregam)

Sintoma: depois de instalar um pacote TOTVS, apagar o `_BrokerCustom.dat` e reiniciar o
RM Host, o serviço sobe **com sucesso**, mas o arquivo é recriado com uma fração do
tamanho normal (ex.: 50 KB no lugar de 560 KB) e as customizações não carregam para os
usuários daquele servidor. Paliativo conhecido: copiar o arquivo de um host que gerou
certo.

### Por que acontece

`_BrokerCustom.dat` é o cache de reflexão sobre as DLLs de customização
(`RM.Net\Custom`, ~65 MB / 237 assemblies neste parque). O `RM.Host` só o gera quando o
arquivo **não existe** — e é isso que torna qualquer geração ruim permanente: como o
arquivo passou a existir, todo start seguinte reusa o cache curto, o serviço sobe "com
sucesso" e as customizações não carregam.

O que o histórico do RMon mostra, com o tamanho de cada geração (parque SGE, onde o
arquivo íntegro tem 571.588 bytes):

| host | 1ª geração | 2ª | 3ª |
|---|---|---|---|
| `.190` 21/08 | 17:26 → **55.061** | 17:28 → **55.061** | 17:30 → 571.588 ✅ |
| `.188` 21/08 | 17:30 → **553.582** | 17:31 → 571.588 ✅ | |
| `.223` 21/08 | 17:36 → **553.582** | 17:37 → 571.588 ✅ | 17:42 → **553.582**, 17:43 → ✅ |
| `.222` 21/08 | 17:31 → 571.588 ✅ | | |
| `.34` 24/08 | 15:53 → **55.061** | 16:00 → **55.061** | (arquivo copiado à mão) |

Três coisas se leem daí:

1. **A primeira geração sai incompleta com frequência, em qualquer host** — não é defeito
   do `.34` nem do `.188`. O `.190`, que servia de "servidor bom" para copiar o arquivo,
   também gerou 55.061 na primeira tentativa.
2. **O tamanho errado é determinístico** (55.061 exato em gerações diferentes, em hosts
   diferentes; 553.582 idem). Não é corte aleatório por falta de recurso: é o broker de um
   **subconjunto estável** de assemblies — o que aquele processo enxergou naquele start.
3. **Uma geração posterior completa o arquivo.** Onde alguém repetiu o ciclo, o tamanho
   subiu para 571.588 e ficou.

Por que exatamente o segundo start enxerga mais que o primeiro ainda **não está
confirmado** — a suspeita é qual processo gera (o serviço `RM.Host` sozinho versus o
conjunto que sobe depois, com `RM.exe` e `JobRunner` já carregando customização). Isso
exige um teste controlado em homologação para afirmar.

**O que já foi descartado com dados:** falta de memória. Em 24/08 no `.34` a geração
truncada aconteceu com a máquina recém-reiniciada, *commit charge* entre 3% e 10% e
87 GB de limite — e sem nenhum erro no Visualizador de Eventos.

#### O erro de commit é outro problema, real, e já corrigido

Em 10/08 e 18/08 o `.34` registrou falhas de start com o pagefile no modo automático:

```
RM.Host.Service — System.IO.FileLoadException: ... O arquivo de paginação é muito
  pequeno para que esta operação seja concluída. (HRESULT: 0x800705AF)
  em RM.Lib.RMSBroker.InternalStartHost()

RM.Host.Service — Erro ao Registrar Servers iniciais (2) - BrokerServer: Erro ao ler
  arquivo ...\_Broker.dat: 'System.OutOfMemoryException'
```

`0x800705AF` é `ERROR_COMMITMENT_LIMIT` — nem RAM livre, nem disco: o limite de commit
(RAM + pagefile). Isso derrubava o start do RM.Host e **pode** deixar um broker parcial
para trás, mas não é o que explica as gerações truncadas acima. O pagefile fixo (passo 1)
resolve essa falha; não resolve o broker incompleto.

### Correção definitiva

1. **Tirar o arquivo de paginação do modo automático** nos hosts do RM. O tamanho fixo
   (inicial = máximo) sai do maior entre: **16 GB**, **1,5× o pico já usado do pagefile**
   e **metade da RAM** — sem passar de 1/4 do espaço livre em `C:`. Hosts de mesmo papel
   ficam iguais, para não virar caso a caso na hora do incidente.

   Aplicado em 21/08/2026 (**efetivo no próximo reboot** de cada host):

   | Host | RAM | pagefile antes (aloc / pico) | limite de commit antes | pagefile fixo | limite depois |
   |---|---|---|---|---|---|
   | `.34` SRV06N | 55,5 GB | 8 / 0,1 GB | 63,5 GB | 32 GB | ~87 GB |
   | `.188` WIN2016 | 32 GB | 25 / **21,3** GB | 56,6 GB | 32 GB | ~64 GB |
   | `.190` WIN2016-2 | 32 GB | 4,9 / 1,2 GB | 36,7 GB | 32 GB | ~64 GB |
   | `.222` SRV11 (jobs) | 15,9 GB | 49 / **48,4** GB | 63,8 GB | 80 GB | ~96 GB |
   | `.223` SRV12 (web) | 4,7 GB | 1,7 / **1,7** GB | **6,4 GB** | 16 GB | ~21 GB |
   | `.218` FIEPSRV-008 (homolog) | 16 GB | 2,4 / 0,5 GB | 18,4 GB | 16 GB | ~32 GB |

   Os três em negrito já vinham batendo no teto: o pico de uso do pagefile encostou no
   que estava alocado, que é o retrato do `ERROR_COMMITMENT_LIMIT` acontecendo.

   ```powershell
   $cs = Get-WmiObject Win32_ComputerSystem
   if ($cs.AutomaticManagedPagefile) { $cs.AutomaticManagedPagefile = $false; [void]$cs.Put() }
   # Sem -Filter: escapar barra invertida em WQL e fonte de "consulta invalida"
   $pf = @(Get-WmiObject Win32_PageFileSetting | Where-Object { $_.Name -like 'C:*' })[0]
   if (-not $pf) { $pf = ([WmiClass]'Win32_PageFileSetting').CreateInstance(); $pf.Name = 'C:\pagefile.sys' }
   $pf.InitialSize = 32768; $pf.MaximumSize = 32768; [void]$pf.Put()
   ```

   Quem manda no próximo boot é o registro — confira ali, não no WMI:

   ```powershell
   (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management').PagingFiles
   ```

   Se a criação da instância falhar depois de desligar o automático, **volte
   `AutomaticManagedPagefile` para `$true`**: ficar sem pagefile nenhum é pior que o
   problema original. E confira que `C:` tem o espaço da diferença, porque o arquivo só
   cresce até o tamanho novo no boot.

   Depois do reboot, `AllocatedBaseSize` tem de vir com o valor fixado:

   ```powershell
   Get-WmiObject Win32_PageFileUsage | Select-Object Name, AllocatedBaseSize, PeakUsage
   ```

2. **Gerar o broker e não aceitar o primeiro resultado.** Este é o passo que resolve o
   sintoma hoje, enquanto o mecanismo não está confirmado. Pare **todos** os `RM.Host*`
   do host, apague o `_BrokerCustom.dat`, suba **um** serviço só, espere o arquivo parar
   de crescer e **confira o tamanho**:

   ```powershell
   (Get-Item 'C:	otvs\CorporeRM\RM.Net\_BrokerCustom.dat').Length
   ```

   Enquanto não bater o tamanho de referência do host (571.588 no SGE), **apague o
   arquivo e repita** — não adianta reiniciar por cima, porque com o arquivo existindo o
   RM não gera de novo. Foi repetindo o ciclo que `.188`, `.190`, `.222` e `.223`
   chegaram ao arquivo íntegro em 21/08.

3. **Só então liberar.** Copiar o arquivo de outro host do mesmo parque continua sendo
   saída válida e imediata — os hosts do SGE têm o arquivo byte a byte igual. Mas copie
   sabendo que é paliativo: o RMon avisa se o host voltar a subir com o broker curto.

### O que o RMonitor faz

A coleta lê tamanho e data de `_BrokerCustom.dat`/`_Broker.dat` em cada host e compara
com o maior tamanho que aquele host já teve; abaixo de `broker_min_pct` (padrão 60%) sai
alerta `broker:_BrokerCustom.dat` no Telegram/Slack e uma tarja no card do servidor.

O veredito só é dado quando o arquivo **parou de crescer** — duas coletas seguidas com o
mesmo tamanho e a mesma data. Isso vale um ciclo de espera (~1 min), não uma janela fixa:
no incidente de 24/08 o broker do `.34` ficou truncado por 7 minutos e a janela de 10 que
existia antes engoliu o evento inteiro, sem alertar ninguém.

O `commit charge` também é coletado e alerta em `commit_pct` (padrão 90%) — é o indicador
da falha de start descrita acima, não do broker incompleto. Ver
[CONFIGURACAO.md](CONFIGURACAO.md#broker-do-rm-defaultsbroker).

### O que falta confirmar

Por que o segundo start enxerga mais assemblies que o primeiro. O teste que decide é em
homologação (`.218`), com o arquivo salvo antes: parar os `RM.Host`, apagar o broker,
subir **só o serviço** e medir; depois abrir um `RM.exe` e medir de novo. Se o tamanho só
completar com o cliente no ar, a correção definitiva passa a ser gerar o broker com o
cliente — e não com o serviço — antes de liberar o host.
