# Desenvolvimento local

Como rodar o RMonitor na sua estação para desenvolver.

## Pré-requisitos

- Python 3.11+
- Um **PostgreSQL** acessível (local ou container)
- Acesso a algum servidor Windows com WinRM para testar coleta real (opcional — o painel abre mesmo sem alvos)

### PostgreSQL rápido via Docker

```bash
docker run -d --name rmon-pg -p 5432:5432 \
  -e POSTGRES_USER=rmon -e POSTGRES_PASSWORD=rmon -e POSTGRES_DB=rmon \
  postgres:16
```

DSN correspondente: `postgresql://rmon:rmon@127.0.0.1:5432/rmon`.

## Ambiente

```bash
python -m venv .venv
. .venv/Scripts/activate            # Windows (PowerShell: .venv\Scripts\Activate.ps1)
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

> `pymssql` (estatísticas de jobs) precisa do FreeTDS. No Linux há wheels prontas; se
> falhar, instale `freetds-dev`. É opcional para o desenvolvimento do painel.

## Configuração

```bash
cp .env.example .env
cp config/servers.example.yaml config/servers.yaml
```

No `.env`, preencha ao menos:

```dotenv
RMON_SECRET_KEY=<openssl rand -hex 32>
RMON_DB_DSN=postgresql://rmon:rmon@127.0.0.1:5432/rmon
RMON_ADMIN_USER=admin
RMON_ADMIN_PASSWORD_HASH=<gere abaixo>
```

Gerar o hash da senha do admin:

```bash
python -c "from app.security import hash_password; print(hash_password('minhasenha'))"
```

## Rodar

```bash
uvicorn app.main:app --reload
```

Abra `http://127.0.0.1:8080`. O schema do banco é criado automaticamente no start e o
admin é semeado a partir do `.env` no primeiro boot.

## Estrutura do código

```
app/
  __init__.py     __version__
  main.py         Rotas HTTP, sessão, montagem do app e lifespan (start/stop)
  config.py       load_settings() (.env) e load_inventory() (YAML); credential_for()
  collector.py    collect_server(), list_sessions(), logoff_session(), service_action()
  scheduler.py    poll_all() + build_scheduler(); detecção de problemas e alertas
  db.py           psycopg3: schema, checks, users, audit, alerts, app_config
  jobstats.py     query()/pool_summary() sobre GJOBXEXECUCAO (pymssql)
  notify.py       send() -> Telegram/Slack (inerte se não configurado)
  security.py     hash_password()/verify_password() (pbkdf2, stdlib)
  templates/      Jinja2 (base, dashboard, tv, server, jobs, sessions, admin, logs…)
  static/         style.css; tv.css + tv.js (mural de TV, sem dependencias externas)
```

## Convenções

- **Comentários e mensagens em português** (segue o código existente).
- Funções de coleta/ação **não levantam exceção** — encapsulam o erro no retorno (`{"error": ...}` ou `(False, msg)`), para nunca derrubar o ciclo de polling.
- Segredos **nunca** são logados. Ao adicionar credenciais, siga o padrão de `credential_for` / helpers `definir-*.sh`.
- Alterações de schema são **aditivas e idempotentes** (`IF NOT EXISTS`) — o `init_db` roda em todo start.
- Novos segredos: adicione a chave no `.env.example` e, se fizer sentido, um helper `deploy/definir-*.sh` que grave preservando a permissão `600`.

## Antes de publicar / commitar

O `.gitignore` já protege o essencial, mas confira:

- **Nunca** commite `.env`, `config/servers.yaml` real ou o diretório `data/`.
- Mantenha os arquivos `*.example` atualizados quando mudar a configuração.
- Dados internos de infraestrutura (IPs, hostnames, notas de VM) ficam fora do repositório público.
