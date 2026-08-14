#!/usr/bin/env bash
# Configura o alerta via Telegram. Rode como root:
#   sudo bash /opt/rmon/deploy/definir-telegram.sh
# Grava RMON_TELEGRAM_TOKEN e RMON_TELEGRAM_CHAT_ID no /opt/rmon/.env (perm 600),
# reinicia o servico e envia uma mensagem de teste.
set -euo pipefail
APP_DIR=/opt/rmon
ENV_FILE="$APP_DIR/.env"
[ "$(id -u)" -eq 0 ] || { echo "Rode como root (sudo)." >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "Nao encontrei $ENV_FILE." >&2; exit 1; }

read -r -s -p "Token do bot (BotFather): " TOKEN; echo
# remove espacos/CR/LF/tabs que costumam vir colados do Windows
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
[ -n "$TOKEN" ] || { echo "Token vazio." >&2; exit 1; }
if ! printf '%s' "$TOKEN" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]+$'; then
  echo "Token nao parece valido. Esperado: <numeros>:<letras/numeros/_->" >&2
  echo "(len=${#TOKEN}) - verifique se copiou completo, sem espacos." >&2
  exit 1
fi

read -r -p "Chat ID (destino do alerta): " CHAT
CHAT="$(printf '%s' "$CHAT" | tr -d '[:space:]')"
[ -n "$CHAT" ] || { echo "Chat ID vazio." >&2; exit 1; }

cd "$APP_DIR"
RMON_T="$TOKEN" RMON_C="$CHAT" .venv/bin/python - "$ENV_FILE" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); lines = p.read_text(encoding="utf-8").splitlines()
kv = {"RMON_TELEGRAM_TOKEN": os.environ["RMON_T"], "RMON_TELEGRAM_CHAT_ID": os.environ["RMON_C"]}
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

echo "Validando token e enviando teste..."
RMON_T="$TOKEN" RMON_C="$CHAT" .venv/bin/python - <<'PY'
import os, httpx
tok, chat = os.environ["RMON_T"], os.environ["RMON_C"]
me = httpx.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
if not me.get("ok"):
    print("TOKEN INVALIDO:", me.get("description")); raise SystemExit(1)
print("Bot OK: @%s" % me["result"].get("username"))
r = httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage",
               json={"chat_id": chat, "text": "RMonitor: teste de alerta OK."}, timeout=10).json()
if r.get("ok"):
    print("Mensagem de teste ENVIADA para o chat", chat)
else:
    print("FALHA no envio:", r.get("description"))
    print(">> Dica: fale com o bot (botao Iniciar) ou adicione-o ao grupo;")
    print(">> e confirme o Chat ID (usuario = numero positivo; grupo = negativo).")
PY
echo "Concluido."
