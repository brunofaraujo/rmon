# NLB: drenagem e reingresso automáticos dos terminal servers

O RemoteApp do RM roda em terminal servers atrás de um cluster **NLB** (Network Load
Balancing do Windows). O NLB só enxerga a placa de rede: um nó que **autentica o RDP mas
não consegue criar a sessão** (commit esgotado, RM.Host caído...) continua "convergido"
e segue recebendo usuários, que tomam o erro do RemoteApp (0x108) de forma intermitente.

O RMon olha a saúde de **aplicação** de cada nó e tira do balanceamento o nó doente
(`nlb.exe drainstop`), devolvendo-o (`nlb.exe start`) quando ele volta a ficar apto.

**Por padrão nada é executado:** o modo é `alert` (avisa, registra e audita o que *faria*).

## O incidente que motivou

| Quando | O que houve | O que o RMon via |
|---|---|---|
| tarde, nó A | um `RM.Host.JobRunner` passou de 7,5 a 60 GB em 20 min; commit 100%, RM.Host caiu, 27 logons RDP autenticados para 8 sessões | WinRM recusando com "arquivo de paginação muito pequeno" por ~25 min |
| noite, nó B | o mesmo relatório explodiu no outro nó | idem, por ~40 min |
| manhã seguinte, nó B | o job já tinha sido cancelado e o commit estava normal, **mas o nó não criava sessão nenhuma**: os outros dois chegaram a 17 usuários cada, ele ficou com 0 por 2 h | WinRM respondendo; `users_count` = 0; no log do host, 7 a 18 logons autenticados (1149) por meia hora e nenhuma sessão (21/25) |

Duas lições moldaram o desenho:

1. **Na fase aguda o WinRM do nó doente não responde** — nada consegue drená-lo
   remotamente. O que chega a tempo ali é o **aviso precoce** (o JobRunner cresce devagar
   por horas antes de explodir: alertas `jobrunner:<pid>` e `jobrunner-crit:<pid>`, ver
   CONFIGURACAO.md).
2. **A falha longa é a que dá para automatizar**: o nó "vivo" que não cria sessão.
   Com a regra abaixo ele teria sido drenado ~30 min depois de os usuários começarem a
   chegar, em vez de 2 h e um reboot depois.

## Fonte dos dados

Tudo vem da **coleta normal do RMon** (o mesmo WinRM, a cada minuto), só nos servidores
marcados com `nlb: true` no inventário:

- **estado do nó no NLB**: classe WMI `root/MicrosoftNLB:MicrosoftNLB_Node`, lida no
  próprio host (`StatusCode`: 1007/1008 convergido, 1006 convergindo, 1009 drenando,
  1005 parado, 1013 suspenso). Os cmdlets `Get-NlbClusterNode` dão *acesso negado* por
  WinRM (fazem WMI remoto para os outros nós — salto duplo); o WMI local e o `nlb.exe` não.
- **logons RDP na janela** (`window_min`, 15 min): eventos **1149** do
  `TerminalServices-RemoteConnectionManager/Operational` (autenticou) e **21/25** do
  `TerminalServices-LocalSessionManager/Operational` (sessão criada/reconectada).
- **usuários** (`quser`), **commit charge**, **serviços RM.Host.Service\*** e **uptime**
  — já coletados.
- **maior RM.Host.JobRunner** (memória privada, a mesma medição `jobrunners` dos
  alertas `jobrunner:*`) — só para a drenagem preventiva opcional.

O ELK foi considerado e **não** é usado como gatilho: nesses hosts o agente é justamente o
que morre sob pressão de memória, e há nó que não envia o 1149. Ler direto do host é a
mesma informação, sem depender de outro sistema estar de pé.

## Quando um nó é considerado doente

Só se julga nó **alcançável e ativo no NLB** (nó fora do NLB naturalmente não cria sessão).
Qualquer um destes sinais, **sem interrupção por `bad_after_min` (15 min)**:

| Sinal | Regra (padrões) | Pega |
|---|---|---|
| autentica sem criar sessão | ≥ `auth_min` (5) logons 1149 e sessões 21/25 < `session_ratio` (0,4) × logons, na janela | o nó da manhã (7–18 : 0) e a criação parcial (27 : 8) |
| ninguém entra aqui | 0 usuários e 0 sessões novas enquanto os pares têm média ≥ `peer_users_min` (3) usuários **e** criaram ≥ `peer_sess_min` (8) sessões na janela | o nó que não registra o 1149 |
| RM.Host fora do ar | algum `RM.Host.Service*` (geradores do broker) diferente de Running | serviço caído |
| commit no teto | `commit_pct` ≥ `commit_drain_pct` (95) | — |
| JobRunner (opcional) | maior JobRunner ≥ `drain_jobrunner_gb` (0 = desligado) | drenagem preventiva |

O sinal "ninguém entra aqui" fica desligado por `grace_min` (30 min) depois de o nó
reiniciar ou reingressar — recém-chegado ainda não recebeu gente e seria drenado de volta.

## Travas de segurança da drenagem

Na ordem em que são checadas:

1. **Manutenção (hold)** — nó em `hold:` no inventário ou marcado na tela: nunca é tocado.
2. **Flapping** — mais de `max_drains_day` (2) drenagens do nó em 24 h: só avisa.
3. **Piso do cluster** — depois de drenar, têm de sobrar pelo menos `min_active_remaining`
   (2) nós ativos **e** `min_healthy_remaining` (1, não configurável abaixo de 1) nós
   ativos e saudáveis. Nó sem contato não conta como ativo. Com 3 nós e 2 doentes ao
   mesmo tempo (o caso de 13/10), o primeiro é drenado e o segundo **fica**, com aviso.
4. **Cooldown** — uma drenagem automática por cluster a cada `cooldown_min` (30 min).
5. **Uma ação por cluster por ciclo**, o doente mais antigo primeiro.

Nó doente que perdeu o WinRM gera o aviso "doente e sem contato: drene pelo console" —
e a contagem de tempo doente continua; se o WinRM voltar com o nó ainda doente, a drenagem
sai no mesmo ciclo.

## Reingresso

O NLB **grava o estado parado no registro** (evento 75) e o mantém depois do reboot: nó
drenado e reiniciado volta *parado*. Por isso o reingresso automático é parte do desenho.

Um nó fora do NLB (parado, drenando ou suspenso) é reingressado quando, **sem interrupção
por `rejoin_after_min` (10 min)**:

- está alcançável, com todos os `RM.Host.Service*` Running;
- commit < `commit_ok_pct` (85%) e Windows no ar há ≥ `min_uptime_min` (10 min);
- **se foi o RMon que o drenou**: reiniciou depois da drenagem **ou** criou sessão RDP de
  novo (um teste por RDP direto no IP do nó). Sem uma das duas provas, o nó que não criava
  sessão voltaria ao balanceamento igual.

Nó fora do NLB que o RMon **não** drenou (manutenção manual, atualização do Windows...)
gera o aviso `out_unowned` e só é reingressado sozinho com `rejoin_unowned: true`.
Suspenso (1013) recebe `nlb.exe resume` antes do `start`.

## Modos

| `mode` | Avalia | Notifica | Executa no host |
|---|---|---|---|
| `off` | não | — | não |
| `alert` (padrão) | sim | "drenaria" / "reingressaria" / bloqueios | **não** |
| `auto` | sim | o que fez | sim (`drainstop` / `start`) |

Toda decisão vai para o `alerts_log` (`kind = 'nlb'`, aparece em Ocorrências) e para o
`audit_log` (usuário `rmon-nlb`, ações `nlb_*`). Mensagem no Slack/Telegram só nas
**transições** — a mesma situação não se repete a cada minuto; falha de execução avisa
na primeira vez e é tentada de novo a cada `retry_min` (5 min).

## Tela `/nlb` (admin)

Estado de cada nó no NLB, sinais de saúde de agora, contagem de logons/sessões, usuários,
commit, maior JobRunner, se está em manutenção / drenado pelo RMon, última ação e o
histórico de 7 dias. Botões:

- **Manutenção / Liberar** — liga e desliga o hold (só no RMon, não mexe no NLB).
- **Drenar** — `nlb.exe drainstop` agora, com o **mesmo piso** da automática; liga a
  manutenção (o RMon não devolve sozinho um nó drenado à mão).
- **Reingressar** — `nlb.exe start` agora (`resume` antes, se suspenso); desliga a manutenção.

Drenar e reingressar pedem o nome do servidor digitado. Funcionam em qualquer modo.

## Como ativar o modo automático

1. Deixe rodando em `alert` e confira em `/nlb` e em Ocorrências o que ele *teria feito*.
2. No `servers.yaml`, em `defaults.nlb`, troque `mode: alert` por `mode: auto`.
3. `sudo systemctl restart rmon`.

Para voltar atrás: `mode: alert` (ou `off`) e restart. Nós que o RMon drenou continuam
fora até alguém reingressá-los (tela `/nlb` ou `nlb.exe start` no nó).

## Configuração

```yaml
defaults:
  nlb:
    mode: alert            # off | alert | auto
    window_min: 15
    auth_min: 5
    session_ratio: 0.4
    peer_users_min: 3
    peer_sess_min: 8
    commit_drain_pct: 95
    drain_jobrunner_gb: 0  # 0 = nao drena por JobRunner (ficam so os alertas jobrunner:*)
    bad_after_min: 15
    rejoin_after_min: 10
    grace_min: 30
    commit_ok_pct: 85
    min_uptime_min: 10
    min_healthy_remaining: 1
    min_active_remaining: 2
    cooldown_min: 30
    max_drains_day: 2
    retry_min: 5
    rejoin_unowned: false
    hold: []

servers:
  - name: rm-app-01
    host: 10.10.10.50
    nlb: true              # no de cluster NLB: entra na avaliacao
```

O estado da automação (desde quando cada nó está doente/apto, quem drenou, histórico de
drenagens) fica em `app_config` (`key = 'nlb_state'`) e sobrevive a restart do RMon.

## Limitações conhecidas

- **Nó sem WinRM não é drenado.** No pico da falha (commit esgotado) é exatamente o que
  acontece. Alternativa avaliada e *não* adotada: habilitar o "controle remoto" do NLB
  (`nlb.exe drainstop <cluster>:<host>` a partir de outro nó), que abre UDP 2504 com
  senha fraca e é desaconselhado pela Microsoft.
- `drainstop` mantém as conexões existentes até elas fecharem; o nó só fica *parado*
  quando a última sessão RDP cai. Para o objetivo (parar de mandar gente nova), drenando
  já basta.
- A comparação com os pares supõe que o NLB distribui de forma parecida; afinidade por
  IP de origem pode concentrar uma unidade inteira num nó. Por isso ela exige movimento
  real nos pares (`peer_sess_min`) e não vale na carência.
