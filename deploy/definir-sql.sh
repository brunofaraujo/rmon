#!/usr/bin/env bash
# Define o login SQL (somente-leitura) usado para consultar o GJOBXEXECUCAO. Rode como root:
#   sudo bash /opt/rmon/deploy/definir-sql.sh
# Grava RMON_SQL_USER e RMON_SQL_PASSWORD no /opt/rmon/.env (perm 600) e reinicia.
set -euo pipefail
APP_DIR=/opt/rmon
ENV_FILE="$APP_DIR/.env"
[ "$(id -u)" -eq 0 ] || { echo "Rode como root (sudo)." >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "Nao encontrei $ENV_FILE." >&2; exit 1; }

read -r -p "Usuario SQL (login somente-leitura): " SU
SU="$(printf '%s' "$SU" | tr -d '[:space:]')"
[ -n "$SU" ] || { echo "Usuario vazio." >&2; exit 1; }
read -r -s -p "Senha SQL: " SP1; echo
read -r -s -p "Confirme a senha: " SP2; echo
[ "$SP1" = "$SP2" ] || { echo "Nao conferem." >&2; exit 1; }
[ -n "$SP1" ] || { echo "Senha vazia." >&2; exit 1; }

cd "$APP_DIR"
RMON_U="$SU" RMON_P="$SP1" .venv/bin/python - "$ENV_FILE" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); lines = p.read_text(encoding="utf-8").splitlines()
kv = {"RMON_SQL_USER": os.environ["RMON_U"], "RMON_SQL_PASSWORD": os.environ["RMON_P"]}
seen = set(); out = []
for ln in lines:
    k = ln.split("=", 1)[0].strip() if "=" in ln else ""
    if k in kv: out.append(f"{k}={kv[k]}"); seen.add(k)
    else: out.append(ln)
for k, v in kv.items():
    if k not in seen: out.append(f"{k}={v}")
p.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
unset SP1 SP2
chown rmon:rmon "$ENV_FILE"; chmod 600 "$ENV_FILE"
systemctl restart rmon
echo "OK: login SQL gravado e servico reiniciado."
