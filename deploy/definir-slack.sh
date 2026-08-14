#!/usr/bin/env bash
# Configura alerta via Slack (Incoming Webhook do canal #rm_alerts). Rode como root:
#   sudo bash /opt/rmon/deploy/definir-slack.sh
# Grava RMON_SLACK_WEBHOOK no /opt/rmon/.env (perm 600), reinicia e envia teste.
set -euo pipefail
APP_DIR=/opt/rmon
ENV_FILE="$APP_DIR/.env"
[ "$(id -u)" -eq 0 ] || { echo "Rode como root (sudo)." >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "Nao encontrei $ENV_FILE." >&2; exit 1; }

read -r -s -p "Webhook URL do Slack (#rm_alerts): " HOOK; echo
HOOK="$(printf '%s' "$HOOK" | tr -d '[:space:]')"
case "$HOOK" in
  https://hooks.slack.com/*) : ;;
  *) echo "URL nao parece um Incoming Webhook do Slack (https://hooks.slack.com/...)." >&2; exit 1 ;;
esac

cd "$APP_DIR"
RMON_HOOK="$HOOK" .venv/bin/python - "$ENV_FILE" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); lines = p.read_text(encoding="utf-8").splitlines()
kv = {"RMON_SLACK_WEBHOOK": os.environ["RMON_HOOK"]}
seen = set(); out = []
for ln in lines:
    k = ln.split("=", 1)[0].strip() if "=" in ln else ""
    if k in kv: out.append(f"{k}={kv[k]}"); seen.add(k)
    else: out.append(ln)
for k, v in kv.items():
    if k not in seen: out.append(f"{k}={v}")
p.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
chown rmon:rmon "$ENV_FILE"; chmod 600 "$ENV_FILE"
systemctl restart rmon
echo "Enviando mensagem de teste ao Slack..."
RMON_HOOK="$HOOK" .venv/bin/python -c "import os,httpx;r=httpx.post(os.environ['RMON_HOOK'],json={'text':'RMonitor: teste de alerta no #rm_alerts OK.'},timeout=10);print('HTTP',r.status_code,'' if r.status_code==200 else r.text[:200])"
echo "Concluido."
