# Instalação (produção)

Guia de instalação do RMonitor numa VM Linux dedicada. A aplicação roda como
serviço `systemd` e monitora servidores Windows remotos — **nada é instalado nos
alvos**; eles só precisam ter o WinRM habilitado (ver [CONFIGURACAO.md](CONFIGURACAO.md#winrm-nos-servidores-windows)).

## 1. Pré-requisitos

| Item | Requisito |
|---|---|
| SO da VM | Ubuntu 22.04+ / Debian 12+ (com `systemd` e `apt`) |
| Acesso | usuário com `sudo` na VM |
| Rede | alcance aos alvos em **WinRM** (5985 ou 5986) e, se usar jobs, ao **SQL Server** (1433) |
| Banco | PostgreSQL — **provisionado automaticamente** pelo instalador (local) |

O instalador cuida do Python, do virtualenv, do PostgreSQL e das bibliotecas do
`pymssql` (FreeTDS). Você não precisa instalá-los à mão.

## 2. Obter o código na VM

Via `git`:

```bash
git clone https://github.com/<seu-usuario>/rmon.git
cd rmon
```

Ou copie da sua estação com `scp`/`rsync` para um diretório qualquer (ex.: `~/rmon-src`).

## 3. Rodar o instalador

```bash
sudo deploy/instalar.sh
```

Ele é **idempotente** (pode rodar de novo sem estragar nada) e executa, em ordem:

1. **Pacotes de SO** — `python3-venv`, `postgresql`, `freetds-dev`, `build-essential`, etc.
2. **Usuário de serviço** `rmon` (system user, sem shell).
3. **Cópia** da aplicação para `/opt/rmon`.
4. **Virtualenv** em `/opt/rmon/.venv` + `pip install -r requirements.txt`.
5. **PostgreSQL** — cria a role `rmon` e o database `rmon`, gera uma senha aleatória e monta o `RMON_DB_DSN`.
6. **Inventário** — cria `config/servers.yaml` a partir do exemplo (se ainda não existir).
7. **Segredos** — gera `/opt/rmon/.env` (perm `600`), perguntando **interativamente** a senha do admin do painel e (opcional) a credencial WinRM.
8. **Serviço systemd** `rmon` — habilita, sobe e valida `http://127.0.0.1:8080/healthz`.

### Modo não-interativo

Para automação (sem perguntar senhas), exporte `RMON_NONINTERACTIVE=1`. O `.env` é
criado **sem** a senha do admin — defina-a depois com o helper (passo 5 abaixo).

```bash
sudo RMON_NONINTERACTIVE=1 deploy/instalar.sh
```

## 4. Ajustar o inventário

Edite os servidores monitorados:

```bash
sudoedit /opt/rmon/config/servers.yaml
sudo systemctl restart rmon
```

Estrutura e campos: veja [CONFIGURACAO.md](CONFIGURACAO.md#inventário-configserversyaml).

## 5. Definir segredos (helpers)

Todos gravam **apenas** em `/opt/rmon/.env` (perm `600`) e reiniciam o serviço.
Senhas são digitadas sem eco e não vão para o histórico do shell.

```bash
sudo deploy/definir-senha-admin.sh          # senha do admin do painel (hash pbkdf2)
sudo deploy/definir-winrm.sh fin            # credencial WinRM do perfil 'fin'
sudo deploy/definir-winrm.sh rh             # ... e de outros perfis
sudo deploy/definir-sql.sh                  # login SQL (somente-leitura) p/ estatísticas de jobs
sudo deploy/definir-telegram.sh             # alerta via Telegram (+ mensagem de teste)
sudo deploy/definir-slack.sh                # alerta via Slack (+ mensagem de teste)
```

## 6. Primeiro acesso

Abra `http://<ip-da-vm>:8080` e faça login com o usuário admin definido no passo 3/5.
O admin inicial é semeado no banco a partir do `.env` no primeiro start.

Para criar mais usuários, use a CLI (ver [OPERACAO.md](OPERACAO.md#usuários-do-painel)).

## 7. Firewall (recomendado)

Restrinja a porta do painel à sua rede de gestão. Exemplo com `ufw`:

```bash
sudo ufw allow from 10.0.0.0/24 to any port 8080 proto tcp
sudo ufw allow from 10.0.0.0/24 to any port 22 proto tcp
sudo ufw default deny incoming && sudo ufw enable
```

Para expor com TLS, coloque um reverse-proxy (nginx/caddy) na frente do `:8080`.

## 8. Verificar

```bash
systemctl status rmon
curl -fsS http://127.0.0.1:8080/healthz    # {"status":"ok","version":"..."}
journalctl -u rmon -f                       # logs ao vivo
```

## Desinstalação

```bash
sudo systemctl disable --now rmon
sudo rm -f /etc/systemd/system/rmon.service
sudo systemctl daemon-reload
sudo deluser --remove-home rmon 2>/dev/null || true
sudo rm -rf /opt/rmon
# Banco (opcional — apaga o histórico):
sudo runuser -u postgres -- dropdb rmon
sudo runuser -u postgres -- dropuser rmon
```
