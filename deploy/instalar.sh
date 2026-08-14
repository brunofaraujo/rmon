#!/usr/bin/env bash
# Instalador do RMonitor (RMon) para Ubuntu/Debian. Rode como root:
#   sudo deploy/instalar.sh
#
# O que faz (idempotente - pode rodar de novo com seguranca):
#   1. Instala pacotes de SO (Python, PostgreSQL, libs do pymssql).
#   2. Cria o usuario de servico 'rmon'.
#   3. Copia a aplicacao para /opt/rmon.
#   4. Cria o virtualenv e instala as dependencias Python.
#   5. Provisiona o banco PostgreSQL local (role + database) e monta o DSN.
#   6. Cria o inventario config/servers.yaml a partir do exemplo (se faltar).
#   7. Gera /opt/rmon/.env (perm 600) com segredos - pede senha do admin/WinRM.
#   8. Instala e sobe o servico systemd 'rmon' e valida /healthz.
set -euo pipefail

APP_DIR=/opt/rmon
SERVICE_USER=rmon
DB_NAME=rmon
DB_USER=rmon
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Rode como root (sudo)." >&2
  exit 1
fi

echo "==> [1/8] Pacotes de SO"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# python + venv; postgresql (banco); build deps do pymssql (freetds); utilitarios.
apt-get install -y -qq \
  python3 python3-venv python3-pip python3-dev build-essential \
  postgresql postgresql-client \
  freetds-dev freetds-bin libssl-dev \
  rsync openssl curl

echo "==> [2/8] Usuario de servico ($SERVICE_USER)"
id "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> [3/8] Copiando aplicacao para $APP_DIR"
mkdir -p "$APP_DIR/config" "$APP_DIR/data"
rsync -a --delete "$SRC_DIR/app/" "$APP_DIR/app/"
rsync -a "$SRC_DIR/deploy/" "$APP_DIR/deploy/"
cp "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
cp "$SRC_DIR/config/servers.example.yaml" "$APP_DIR/config/servers.example.yaml"

echo "==> [4/8] Ambiente virtual + dependencias Python"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> [5/8] Banco de dados PostgreSQL"
systemctl enable --now postgresql >/dev/null 2>&1 || true
ENV_FILE="$APP_DIR/.env"
DB_DSN=""
if [ -f "$ENV_FILE" ] && grep -q '^RMON_DB_DSN=' "$ENV_FILE"; then
  echo "    -> RMON_DB_DSN ja definido no .env; mantido."
  DB_DSN="$(grep '^RMON_DB_DSN=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
else
  DB_PASS="$(openssl rand -hex 24)"
  # role (cria ou reseta a senha) - senha hex e URL-safe
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 <<SQL
DO \$do\$
BEGIN
   IF EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
      ALTER ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
   ELSE
      CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
   END IF;
END
\$do\$;
SQL
  # database (cria se faltar)
  if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
    runuser -u postgres -- createdb -O "${DB_USER}" "${DB_NAME}"
    echo "    -> database '${DB_NAME}' criado (owner ${DB_USER})."
  else
    echo "    -> database '${DB_NAME}' ja existe."
  fi
  DB_DSN="postgresql://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}"
fi

echo "==> [6/8] Inventario (config/servers.yaml)"
if [ ! -f "$APP_DIR/config/servers.yaml" ]; then
  cp "$APP_DIR/config/servers.example.yaml" "$APP_DIR/config/servers.yaml"
  echo "    -> criado a partir do exemplo. EDITE depois com seus servidores."
else
  echo "    -> ja existe, mantido."
fi

echo "==> [7/8] Segredos (.env)"
if [ ! -f "$ENV_FILE" ]; then
  SECRET=$(openssl rand -hex 32)
  ADMIN_USER="${RMON_ADMIN_USER:-admin}"
  ADMIN_HASH=""
  WINRM_USER="${RMON_WINRM_USER:-}"
  WINRM_PW="${RMON_WINRM_PASSWORD:-}"
  if [ -z "${RMON_NONINTERACTIVE:-}" ]; then
    read -r -p "    Usuario admin do painel [admin]: " _u; ADMIN_USER="${_u:-$ADMIN_USER}"
    read -r -s -p "    Senha do admin do painel: " ADMIN_PW; echo
    read -r -p "    Usuario WinRM (DOMINIO\\usuario) [Enter p/ depois]: " _wu; WINRM_USER="${_wu:-$WINRM_USER}"
    read -r -s -p "    Senha WinRM [Enter p/ depois]: " WINRM_PW; echo
    if [ -n "${ADMIN_PW:-}" ]; then
      ADMIN_HASH=$(cd "$APP_DIR" && RMON_PW="$ADMIN_PW" .venv/bin/python -c \
        "import os;from app.security import hash_password;print(hash_password(os.environ['RMON_PW']))")
    fi
    unset ADMIN_PW
  else
    echo "    -> modo nao-interativo: .env sem senha de admin."
    echo "       Defina depois com: sudo deploy/definir-senha-admin.sh"
  fi
  umask 077
  cat > "$ENV_FILE" <<EOF
RMON_SECRET_KEY=$SECRET
RMON_ADMIN_USER=$ADMIN_USER
RMON_ADMIN_PASSWORD_HASH=$ADMIN_HASH
RMON_DB_DSN=$DB_DSN
RMON_WINRM_USER=$WINRM_USER
RMON_WINRM_PASSWORD=$WINRM_PW
RMON_CONFIG=/opt/rmon/config/servers.yaml
RMON_HOST=0.0.0.0
RMON_PORT=8080
EOF
  unset WINRM_PW
  echo "    -> .env criado (permissao 600)."
else
  # garante que o DSN esteja presente mesmo em .env pre-existente
  grep -q '^RMON_DB_DSN=' "$ENV_FILE" || echo "RMON_DB_DSN=$DB_DSN" >> "$ENV_FILE"
  echo "    -> .env ja existe, mantido (nao sobrescrevo segredos)."
fi

echo "==> [8/8] Servico systemd"
install -m 644 "$SRC_DIR/deploy/rmon.service" /etc/systemd/system/rmon.service
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 600 "$ENV_FILE"
chmod 700 "$APP_DIR/data"
systemctl daemon-reload
systemctl enable rmon >/dev/null 2>&1 || true
systemctl restart rmon
sleep 2

echo "----------------------------------------"
systemctl --no-pager --lines=0 status rmon || true
echo "----------------------------------------"
if curl -fsS "http://127.0.0.1:8080/healthz"; then
  echo
  echo "OK: RMonitor respondendo em http://127.0.0.1:8080/healthz"
  echo "Proximos passos:"
  echo "  - Credenciais WinRM:  sudo deploy/definir-winrm.sh <perfil>"
  echo "  - Inventario:         sudoedit /opt/rmon/config/servers.yaml && sudo systemctl restart rmon"
else
  echo "ATENCAO: /healthz nao respondeu. Veja: journalctl -u rmon -n 50 --no-pager" >&2
fi
