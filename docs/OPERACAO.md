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
| `/ocorrencias` | Erros/críticos recentes do Event Log, consolidados | login |
| `/pacotes` | Inventário TOTVS: matriz item × host (produto, customizações, bibliotecas), divergências e ausências | login (coletar: admin) |
| `/pacotes/mudancas` | Linha do tempo de instalações, atualizações, regressões e remoções | login |
| `/pacotes/catalogo` | Versões disponíveis: repositório de pacotes baixados do TDN, vínculos e registro manual | admin |
| `/sessions` | Sessões RDP ao vivo; encerrar sessões selecionadas | admin p/ logoff |
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
