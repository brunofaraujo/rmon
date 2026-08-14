#!/usr/bin/env bash
# Define/atualiza a credencial WinRM de um PERFIL usado pelo RMonitor.
# Uso (como root):
#   sudo bash /opt/rmon/deploy/definir-winrm.sh fin
#   sudo bash /opt/rmon/deploy/definir-winrm.sh rh
# A senha e digitada interativamente (read -s: nao ecoa, nao vai para o historico),
# gravada apenas no /opt/rmon/.env (perm 600) nas chaves RMON_CRED_<PERFIL>_USER/PASSWORD.
set -euo pipefail

APP_DIR=/opt/rmon
ENV_FILE="$APP_DIR/.env"

[ "$(id -u)" -eq 0 ] || { echo "Rode como root (sudo)." >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "Nao encontrei $ENV_FILE. Rode o instalador antes." >&2; exit 1; }

PROFILE="${1:-}"
if [ -z "$PROFILE" ]; then
  read -r -p "Perfil de credencial (ex.: fin, rh): " PROFILE
fi
[ -n "$PROFILE" ] || { echo "Perfil vazio nao permitido." >&2; exit 1; }
# normaliza para MAIUSCULAS e valida (apenas letras/numeros/_)
PROFILE_UP=$(printf '%s' "$PROFILE" | tr '[:lower:]' '[:upper:]')
case "$PROFILE_UP" in
  *[!A-Z0-9_]*) echo "Perfil invalido: use apenas letras, numeros e _." >&2; exit 1;;
esac

read -r -p "Usuario WinRM do perfil '$PROFILE' (ex.: DOMINIO\\usuario): " WU
[ -n "$WU" ] || { echo "Usuario vazio nao permitido." >&2; exit 1; }
read -r -s -p "Senha WinRM: " WP1; echo
read -r -s -p "Confirme a senha WinRM: " WP2; echo
[ "$WP1" = "$WP2" ] || { echo "As senhas nao conferem." >&2; exit 1; }
[ -n "$WP1" ] || { echo "Senha vazia nao permitida." >&2; exit 1; }

cd "$APP_DIR"
RMON_KEY_USER="RMON_CRED_${PROFILE_UP}_USER" \
RMON_KEY_PW="RMON_CRED_${PROFILE_UP}_PASSWORD" \
RMON_NEW_USER="$WU" RMON_NEW_PW="$WP1" .venv/bin/python - "$ENV_FILE" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1])
lines = p.read_text(encoding="utf-8").splitlines()
kv = {os.environ["RMON_KEY_USER"]: os.environ["RMON_NEW_USER"],
      os.environ["RMON_KEY_PW"]: os.environ["RMON_NEW_PW"]}
seen = set(); out = []
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
unset WP1 WP2

chown rmon:rmon "$ENV_FILE"; chmod 600 "$ENV_FILE"
systemctl restart rmon
echo "OK: credencial do perfil '$PROFILE' (RMON_CRED_${PROFILE_UP}_*) gravada e servico reiniciado."
