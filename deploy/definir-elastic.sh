#!/usr/bin/env bash
# Liga o Elasticsearch como fonte opcional (so leitura) do RMon. Rode como root:
#   sudo bash /opt/rmon/deploy/definir-elastic.sh
#
# Antes, crie no Kibana (Dev Tools) uma API key que SO LE os indices que o RMon
# consulta - o comando esta em docs/CONFIGURACAO.md (secao Elasticsearch) - e
# tenha em maos o campo "encoded" da resposta.
#
# O script: grava a CA do Elastic que o proprio servidor apresenta (mostra a
# impressao digital para voce conferir), testa a chave contra o Elastic, grava
# RMON_ES_URL/RMON_ES_API_KEY/RMON_ES_CA no /opt/rmon/.env (perm 600) e reinicia.
set -euo pipefail
APP_DIR=/opt/rmon
ENV_FILE="$APP_DIR/.env"
CA_FILE="$APP_DIR/config/elastic-ca.crt"
[ "$(id -u)" -eq 0 ] || { echo "Rode como root (sudo)." >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "Nao encontrei $ENV_FILE." >&2; exit 1; }

read -r -p "URL do Elasticsearch (ex.: https://elastic.exemplo.local:9200): " URL
URL="$(printf '%s' "$URL" | tr -d '[:space:]')"; URL="${URL%/}"
case "$URL" in https://*) : ;; *) echo "Use https://host:porta." >&2; exit 1 ;; esac
HOSTPORT="${URL#https://}"; HOSTPORT="${HOSTPORT%%/*}"
case "$HOSTPORT" in *:*) : ;; *) HOSTPORT="$HOSTPORT:443" ;; esac

# CA: o Elastic 8+ assina o HTTP com uma CA propria que ele mesmo serve no fim
# da cadeia. Grava-se essa CA (confiar na primeira vez), com a impressao digital
# na tela para conferir com a do servidor:
#   openssl x509 -in /etc/elasticsearch/certs/http_ca.crt -noout -fingerprint -sha256
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
echo | openssl s_client -showcerts -connect "$HOSTPORT" 2>/dev/null \
  | awk '/BEGIN CERTIFICATE/{c=""} {c=c $0 "\n"} /END CERTIFICATE/{ultimo=c} END{printf "%s", ultimo}' > "$TMP"
[ -s "$TMP" ] || { echo "Nao consegui ler o certificado de $HOSTPORT." >&2; exit 1; }
echo "CA apresentada pelo Elasticsearch:"
openssl x509 -in "$TMP" -noout -subject -issuer -enddate -fingerprint -sha256 | sed 's/^/  /'
read -r -p "Confere com a CA do seu Elastic? [s/N] " OK
case "$OK" in s|S|sim|SIM) : ;; *) echo "Nada foi gravado." >&2; exit 1 ;; esac

read -r -s -p "API key (campo \"encoded\"): " KEY; echo
KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
[ -n "$KEY" ] || { echo "API key vazia." >&2; exit 1; }

echo "Testando a chave (uma busca de leitura)..."
CODE="$(curl -sS -o /dev/null -w '%{http_code}' --cacert "$TMP" -H "Authorization: ApiKey $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"size":0,"query":{"range":{"@timestamp":{"gte":"now-5m"}}}}' \
  "$URL/metrics-system.cpu-*/_search" || true)"
[ "$CODE" = "200" ] || { echo "O Elasticsearch respondeu HTTP $CODE: confira a URL, a chave e os privilegios. Nada foi gravado." >&2; exit 1; }

install -o root -g rmon -m 0644 "$TMP" "$CA_FILE"
cd "$APP_DIR"
RMON_ES_URL_NOVO="$URL" RMON_ES_KEY_NOVA="$KEY" RMON_ES_CA_NOVA="$CA_FILE" .venv/bin/python - "$ENV_FILE" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); lines = p.read_text(encoding="utf-8").splitlines()
kv = {"RMON_ES_URL": os.environ["RMON_ES_URL_NOVO"],
      "RMON_ES_API_KEY": os.environ["RMON_ES_KEY_NOVA"],
      "RMON_ES_CA": os.environ["RMON_ES_CA_NOVA"]}
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
echo "Concluido. Na tela de um servidor do RM deve aparecer o painel 'Memoria do maior JobRunner'."
