#!/usr/bin/env bash
# Publica o painel em HTTPS (porta 443) com certificado proprio. Rode como root:
#   sudo deploy/definir-https.sh [ORIGEM ...]    # instala/atualiza tudo
#   sudo deploy/definir-https.sh --renovar        # so reemite o certificado se preciso
#
# Por que assim:
#   - nginx na frente do uvicorn termina o TLS e repassa para 127.0.0.1:8080.
#     A porta 8080 CONTINUA em HTTP: e por ela que os servidores Windows baixam
#     os pacotes (execution.base_url) e ali nao da para depender de cadeia de CA.
#   - Em vez de um certificado auto-assinado solto, cria uma CA local ("RMonitor
#     CA") e assina com ela o certificado do servidor. Quem instalar a CA uma vez
#     (GPO ou certutil) para de ver o aviso - inclusive depois das renovacoes,
#     porque a CA e mantida e so o certificado do servidor e reemitido.
#   - A CA fica publicada em http(s)://<host>/rmon-ca.crt para instalar nas TVs.
#   - SEM HSTS de proposito: com certificado de CA propria, HSTS tornaria o aviso
#     do navegador impossivel de contornar em quem ainda nao instalou a CA.
#
# ORIGEM: redes/IPs liberados no ufw para 80/443. Sem argumentos, repete as
# origens ja liberadas na 8080, exceto as regras de entrega de pacote aos hosts
# (comentario com "pacote"), que so falam HTTP com a 8080.
#
# SANs extras (alem do hostname e dos IPs da VM): RMON_TLS_SAN="DNS:rmon.exemplo,IP:10.0.0.5"
# Ficam gravados em /etc/rmon/tls/san-extra para a renovacao automatica nao perde-los;
# RMON_TLS_SAN="" (vazio, mas definido) apaga os extras.
set -euo pipefail

TLS_DIR=/etc/rmon/tls
CA_KEY="$TLS_DIR/ca/rmon-ca.key"
CA_CRT="$TLS_DIR/rmon-ca.crt"
SRV_KEY="$TLS_DIR/rmon.key"
SRV_CRT="$TLS_DIR/rmon.crt"
SAN_EXTRA="$TLS_DIR/san-extra"
SITE=/etc/nginx/sites-available/rmon
CA_DIAS=3650
SRV_DIAS=825          # teto aceito por macOS/iOS para CA instalada pelo usuario
RENOVAR_ANTES=60      # dias antes do vencimento em que o certificado e reemitido
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Rode como root (sudo)." >&2
  exit 1
fi

SO_RENOVAR=0
if [ "${1:-}" = "--renovar" ]; then SO_RENOVAR=1; shift; fi

install -d -m 755 "$TLS_DIR"
if [ -n "${RMON_TLS_SAN+x}" ]; then
  printf '%s
' "$RMON_TLS_SAN" > "$SAN_EXTRA"
elif [ -f "$SAN_EXTRA" ]; then
  RMON_TLS_SAN="$(cat "$SAN_EXTRA")"
fi

# --- SANs: hostname + IPs globais da VM + localhost + extras ------------------
sans() {
  local s h f
  h="$(hostname -s)"; f="$(hostname -f 2>/dev/null || true)"
  s="DNS:$h,DNS:localhost,IP:127.0.0.1"
  [ -n "$f" ] && [ "$f" != "$h" ] && s="$s,DNS:$f"
  for ip in $(hostname -I); do
    case "$ip" in *:*) continue ;; esac   # so IPv4 (link-local v6 nao interessa)
    s="$s,IP:$ip"
  done
  [ -n "${RMON_TLS_SAN:-}" ] && s="$s,$RMON_TLS_SAN"
  echo "$s"
}

# SANs do certificado atual, normalizadas no mesmo formato de sans() e ordenadas
sans_atuais() {
  openssl x509 -in "$SRV_CRT" -noout -ext subjectAltName 2>/dev/null \
    | tail -n +2 | tr ',' '\n' | sed -e 's/^ *//' -e 's/IP Address:/IP:/' | grep . | sort
}

emitir_ca() {
  [ -f "$CA_KEY" ] && [ -f "$CA_CRT" ] && return 0
  echo "    -> criando a CA local (valida por $CA_DIAS dias)"
  install -d -m 700 "$TLS_DIR/ca"
  openssl req -x509 -new -newkey rsa:3072 -nodes -sha256 -days "$CA_DIAS" \
    -keyout "$CA_KEY" -out "$CA_CRT" \
    -subj "/CN=RMonitor CA ($(hostname -s))" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" -quiet
  chmod 600 "$CA_KEY"; chmod 644 "$CA_CRT"
}

# 0 = precisa reemitir
precisa_emitir() {
  [ -f "$SRV_CRT" ] && [ -f "$SRV_KEY" ] || return 0
  openssl x509 -in "$SRV_CRT" -noout -checkend $((RENOVAR_ANTES * 86400)) >/dev/null || return 0
  openssl verify -CAfile "$CA_CRT" "$SRV_CRT" >/dev/null 2>&1 || return 0
  [ "$(sans_atuais)" = "$(sans | tr ',' '\n' | sort)" ] || return 0
  return 1
}

emitir_servidor() {
  local tmp; tmp="$(mktemp -d)"
  printf '%s\n' \
    "basicConstraints=critical,CA:FALSE" \
    "keyUsage=critical,digitalSignature,keyEncipherment" \
    "extendedKeyUsage=serverAuth" \
    "subjectKeyIdentifier=hash" \
    "authorityKeyIdentifier=keyid" \
    "subjectAltName=$(sans)" > "$tmp/ext.cnf"
  openssl req -new -newkey rsa:2048 -nodes -sha256 \
    -keyout "$tmp/rmon.key" -out "$tmp/rmon.csr" -subj "/CN=$(hostname -s)" -quiet
  openssl x509 -req -sha256 -days "$SRV_DIAS" -in "$tmp/rmon.csr" \
    -CA "$CA_CRT" -CAkey "$CA_KEY" -set_serial "0x$(openssl rand -hex 16)" \
    -extfile "$tmp/ext.cnf" -out "$tmp/rmon.crt"
  install -m 600 "$tmp/rmon.key" "$SRV_KEY"
  install -m 644 "$tmp/rmon.crt" "$SRV_CRT"
  rm -rf "$tmp"
}

# Nunca chamar dentro de if/||: ali o bash desliga o set -e e uma falha do
# openssl passaria em silencio. O resultado vai em EMITIU.
EMITIU=0
certificado() {
  emitir_ca
  if precisa_emitir; then
    echo "    -> emitindo certificado do servidor (valido por $SRV_DIAS dias)"
    emitir_servidor
    EMITIU=1
  else
    echo "    -> certificado atual ainda serve; mantido."
  fi
}

if [ "$SO_RENOVAR" = 1 ]; then
  certificado
  [ "$EMITIU" = 1 ] && systemctl reload nginx
  exit 0
fi

echo "==> [1/5] nginx"
export DEBIAN_FRONTEND=noninteractive
command -v nginx >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y -qq nginx; }

echo "==> [2/5] Certificado (CA local + servidor)"
certificado

echo "==> [3/5] Site do nginx"
install -m 644 "$SRC_DIR/deploy/nginx-rmon.conf" "$SITE"
ln -sf "$SITE" /etc/nginx/sites-enabled/rmon
# o site 'default' do pacote tambem quer ser default_server na 80
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx >/dev/null 2>&1 || true
systemctl reload nginx 2>/dev/null || systemctl restart nginx

echo "==> [4/5] Renovacao automatica (timer mensal)"
install -m 644 "$SRC_DIR/deploy/rmon-tls-renovar.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/deploy/rmon-tls-renovar.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rmon-tls-renovar.timer >/dev/null

echo "==> [5/5] Firewall (80/443)"
ORIGENS=("$@")
if [ "${#ORIGENS[@]}" -eq 0 ] && command -v ufw >/dev/null 2>&1; then
  # linhas "8080/tcp  ALLOW [IN]  <origem>  # comentario" (so IPv4; o "IN" so
  # aparece no modo verbose/numbered, mas nao custa aceitar os dois)
  mapfile -t ORIGENS < <(ufw status | awk '$1=="8080/tcp" && $2=="ALLOW" && !/\(v6\)/ && !/pacote/ {i=3; if ($i=="IN") i++; print $i}' | sort -u)
fi
if ! command -v ufw >/dev/null 2>&1 || ! ufw status | grep -q "Status: active"; then
  echo "    -> ufw inativo; nada a liberar."
elif [ "${#ORIGENS[@]}" -eq 0 ]; then
  echo "    -> nenhuma origem informada/encontrada. Libere depois, ex.:"
  echo "       sudo ufw allow from 10.0.0.0/24 to any port 80,443 proto tcp"
else
  for o in "${ORIGENS[@]}"; do
    ufw allow from "$o" to any port 80,443 proto tcp comment "RMon HTTPS - $o" >/dev/null
    echo "    -> 80,443 liberadas para $o"
  done
fi

echo "----------------------------------------"
openssl x509 -in "$SRV_CRT" -noout -subject -enddate -ext subjectAltName
echo "Impressao digital SHA-256 da CA:"
openssl x509 -in "$CA_CRT" -noout -fingerprint -sha256
echo "----------------------------------------"
if curl -fsS --cacert "$CA_CRT" https://127.0.0.1/healthz; then
  echo
  echo "OK: painel em https://$(hostname -I | awk '{print $1}')/"
  echo "CA para instalar nos clientes: http://$(hostname -I | awk '{print $1}')/rmon-ca.crt"
else
  echo "ATENCAO: https://127.0.0.1/healthz nao respondeu. Veja: journalctl -u nginx -n 50" >&2
  exit 1
fi
