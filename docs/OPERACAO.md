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
| `/sessions` | Sessões RDP ao vivo; encerrar sessões selecionadas | admin p/ logoff |
| `/logs` | Auditoria de ações | admin |
| `/admin` | Limiares de alerta, tema/refresh da UI e resumo de usuários | admin |
| `/admin/usuarios` | Cadastro de usuários: criar, editar, papel, ativar/desativar, senha, excluir | admin |
| `/profile` | Nome, e-mail e troca da própria senha | login |

Ações sensíveis (restart de serviço, logoff de sessão, mudanças de config) exigem
perfil **admin** e são registradas na auditoria.

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
| Jobs vazios | `RMON_SQL_*` configurado? bloco `jobs:` no servidor? login com acesso à base RM? |
| Alerta não chega | canal configurado? `journalctl` mostra "falha Telegram/Slack"? |
| Serviço não sobe | `RMON_DB_DSN` válido? PostgreSQL ativo (`systemctl status postgresql`)? |

Descoberta de serviços nos alvos (útil ao ajustar o inventário):

```bash
sudo /opt/rmon/.venv/bin/python /opt/rmon/deploy/descobrir.py
```
