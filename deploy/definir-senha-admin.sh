#!/usr/bin/env bash
# Define/atualiza a senha do admin do painel RMonitor. Rode como root:
#   sudo deploy/definir-senha-admin.sh
# A senha e digitada interativamente, transformada em hash pbkdf2 e gravada
# apenas no /opt/rmon/.env (nunca em texto). Reinicia o servico ao final.
set -euo pipefail

APP_DIR=/opt/rmon
ENV_FILE="$APP_DIR/.env"

[ "$(id -u)" -eq 0 ] || { echo "Rode como root (sudo)." >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "Nao encontrei $ENV_FILE. Rode o instalador antes." >&2; exit 1; }

read -r -p "Usuario admin [admin]: " ADMIN_USER; ADMIN_USER="${ADMIN_USER:-admin}"
read -r -s -p "Nova senha do admin do painel: " PW1; echo
read -r -s -p "Confirme a senha: " PW2; echo
[ "$PW1" = "$PW2" ] || { echo "As senhas nao conferem." >&2; exit 1; }
[ -n "$PW1" ] || { echo "Senha vazia nao permitida." >&2; exit 1; }

HASH=$(cd "$APP_DIR" && RMON_PW="$PW1" .venv/bin/python -c \
  "import os;from app.security import hash_password;print(hash_password(os.environ['RMON_PW']))")
unset PW1 PW2

# Atualiza as linhas no .env de forma segura (sem sed, para nao mexer nos '$' do hash)
cd "$APP_DIR"
RMON_NEW_USER="$ADMIN_USER" RMON_NEW_HASH="$HASH" .venv/bin/python - "$ENV_FILE" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1])
lines = p.read_text(encoding="utf-8").splitlines()
kv = {"RMON_ADMIN_USER": os.environ["RMON_NEW_USER"],
      "RMON_ADMIN_PASSWORD_HASH": os.environ["RMON_NEW_HASH"]}
seen = set()
out = []
for ln in lines:
    k = ln.split("=", 1)[0].strip() if "=" in ln else ""
    if k in kv:
        out.append(f"{k}={kv[k]}"); seen.add(k)
    else:
        out.append(ln)
for k, v in kv.items():
    if k not in seen:
        out.append(f"{k}={v}")
p.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

chown rmon:rmon "$ENV_FILE"; chmod 600 "$ENV_FILE"
systemctl restart rmon
echo "OK: senha do admin atualizada e servico 'rmon' reiniciado."
