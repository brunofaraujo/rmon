#!/usr/bin/env bash
# Acesso RESTRITO do RMon ao Incus para reiniciar VM de no do NLB travado.
# Rode como root NO HYPERVISOR (.161, fiepbsrv032):
#
#   sudo bash instalar-no-hypervisor.sh 'ssh-ed25519 AAAA... rmon@rmonsrv'
#
# A chave publica sai do `sudo bash /opt/rmon/deploy/definir-incus-reset.sh` na
# VM do RMon. Por que existe: docs/NLB.md, "No sem contato".
#
# O que cria (e so isso):
#  - usuario de sistema `rmon-reset`, sem senha (so a chave acima, so do IP do RMon,
#    sem shell, sem tunel nem pty: `restrict,from=...,command=...`);
#  - /usr/local/sbin/rmon-incus-reset, o UNICO comando que essa chave roda:
#      listar          as VMs liberadas, com estado e MAC
#      reset <vm>      incus restart --force de uma VM liberada, no maximo 1x a
#                      cada 30 min por VM; todo pedido vai para o syslog
#                      (journalctl -t rmon-incus-reset)
#  - /etc/rmon-incus-reset/permitidas: as VMs liberadas, achadas pelo MAC da placa
#    (o padrao sao as do .188 e do .190);
#  - /etc/sudoers.d/rmon-incus-reset: o usuario so pode rodar aquele comando como root.
#
# Variaveis opcionais:
#   RMON_IP=10.10.10.110                                  de onde o RMon conecta
#   RMON_MACS="10:66:6a:cc:09:90 10:66:6a:e7:40:b1"       VMs liberadas (.188 .190)
#
# Desfazer: userdel -r rmon-reset; rm -rf /usr/local/sbin/rmon-incus-reset \
#   /etc/rmon-incus-reset /etc/sudoers.d/rmon-incus-reset /var/lib/rmon-incus-reset
set -euo pipefail

USUARIO=rmon-reset
RMON_IP="${RMON_IP:-10.10.10.110}"
RMON_MACS="${RMON_MACS:-10:66:6a:cc:09:90 10:66:6a:e7:40:b1}"
COMANDO=/usr/local/sbin/rmon-incus-reset
LISTA_DIR=/etc/rmon-incus-reset
SUDOERS=/etc/sudoers.d/rmon-incus-reset
CHAVE="${1:-}"

[ "$(id -u)" -eq 0 ] || { echo "Rode como root (sudo)." >&2; exit 1; }
if ! [[ "$CHAVE" =~ ^ssh-ed25519\ [A-Za-z0-9+/]+=*(\ [A-Za-z0-9@._-]+)?$ ]]; then
  echo "Uso: $0 'ssh-ed25519 AAAA... rmon@rmonsrv'  (a linha que o definir-incus-reset.sh mostrou)" >&2
  exit 1
fi
INCUS="$(command -v incus || true)"
[ -n "$INCUS" ] || { echo "Nao achei o comando incus neste host." >&2; exit 1; }
command -v python3 >/dev/null || { echo "Falta python3 (para ler o 'incus list')." >&2; exit 1; }

echo "== 1/5 VMs liberadas (pelo MAC da placa)"
LISTA="$("$INCUS" list --all-projects --format json | RMON_MACS="$RMON_MACS" python3 -c '
import json, os, sys
macs = [m.lower() for m in os.environ["RMON_MACS"].split()]
achadas = {}
for i in json.load(sys.stdin):
    cfg = i.get("expanded_config") or i.get("config") or {}
    for k, v in cfg.items():
        if k.startswith("volatile.") and k.endswith(".hwaddr") and str(v).lower() in macs:
            achadas[str(v).lower()] = (i["name"], i.get("project") or "default", i.get("type") or "?")
faltam = [m for m in macs if m not in achadas]
if faltam:
    sys.exit("nenhuma instancia do Incus com o MAC " + ", ".join(faltam))
for m in macs:
    nome, proj, tipo = achadas[m]
    if tipo != "virtual-machine":
        sys.exit(f"{nome} ({m}) e {tipo}, nao virtual-machine")
    print(nome, proj, m)
')"
echo "$LISTA" | sed 's/^/   /'

echo "== 2/5 comando forcado $COMANDO"
install -d -m 755 "$LISTA_DIR"
printf '%s\n' "$LISTA" > "$LISTA_DIR/permitidas"
chmod 644 "$LISTA_DIR/permitidas"
install -d -m 700 /var/lib/rmon-incus-reset
cat > "$COMANDO" <<'SCRIPT'
#!/bin/bash
# Comando forcado da chave do RMon (instalado por deploy/incus/instalar-no-hypervisor.sh
# do repositorio brunofaraujo/rmon). Aceita SO:  listar | reset <vm liberada>
set -uo pipefail
LISTA=/etc/rmon-incus-reset/permitidas
ESTADO=/var/lib/rmon-incus-reset
INTERVALO=1800
INCUS=__INCUS__

if [ "$(id -u)" -ne 0 ]; then
  # chegou pelo SSH: registra o pedido e sobe para root so com o texto pedido
  logger -t rmon-incus-reset "pedido '${SSH_ORIGINAL_COMMAND:-}' de ${SSH_CLIENT%% *}"
  exec sudo -n "$0" "${SSH_ORIGINAL_COMMAND:-}"
fi

read -r ACAO ALVO RESTO <<< "${1:-}"
nega() { logger -t rmon-incus-reset "recusado: $1"; echo "recusado: $1" >&2; exit "${2:-2}"; }
[ -z "${RESTO:-}" ] || nega "argumentos demais"

case "${ACAO:-}" in
  listar)
    [ -z "${ALVO:-}" ] || nega "listar nao leva argumento"
    while read -r nome proj mac; do
      [ -n "$nome" ] || continue
      st="$("$INCUS" list --project "$proj" --format csv -c s "^${nome}\$" 2>/dev/null | head -1)"
      echo "$nome ${st:-?} $mac"
    done < "$LISTA"
    ;;
  reset)
    [[ "${ALVO:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || nega "nome de VM invalido"
    linha="$(awk -v n="$ALVO" '$1 == n' "$LISTA")"
    [ -n "$linha" ] || nega "$ALVO nao esta liberada" 3
    proj="$(awk '{print $2}' <<< "$linha")"
    marca="$ESTADO/$ALVO"
    if [ -f "$marca" ]; then
      idade=$(( $(date +%s) - $(stat -c %Y "$marca") ))
      [ "$idade" -ge "$INTERVALO" ] || nega "$ALVO reiniciada ha ${idade}s (minimo ${INTERVALO}s)" 4
    fi
    touch "$marca"
    logger -t rmon-incus-reset "reset --force de $ALVO (projeto $proj)"
    if timeout 120 "$INCUS" restart --force --project "$proj" "$ALVO"; then
      echo "$ALVO reiniciada (incus restart --force)"
    else
      rc=$?
      logger -t rmon-incus-reset "reset de $ALVO FALHOU (rc $rc)"
      echo "incus restart --force $ALVO falhou (rc $rc)" >&2
      exit 5
    fi
    ;;
  *)
    nega "comando desconhecido: '${ACAO:-}'"
    ;;
esac
SCRIPT
sed -i "s#__INCUS__#$INCUS#" "$COMANDO"
chown root:root "$COMANDO"
chmod 755 "$COMANDO"

echo "== 3/5 usuario $USUARIO (sem senha, so a chave do RMon a partir de $RMON_IP)"
if ! id "$USUARIO" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/var/lib/$USUARIO" --shell /bin/sh \
          --comment "RMon - reset de VM do NLB" "$USUARIO"
fi
passwd -l "$USUARIO" >/dev/null
HOME_U="$(getent passwd "$USUARIO" | cut -d: -f6)"
install -d -m 700 -o "$USUARIO" -g "$USUARIO" "$HOME_U/.ssh"
printf 'restrict,from="%s",command="%s" %s\n' "$RMON_IP" "$COMANDO" "$CHAVE" > "$HOME_U/.ssh/authorized_keys"
chown "$USUARIO:$USUARIO" "$HOME_U/.ssh/authorized_keys"
chmod 600 "$HOME_U/.ssh/authorized_keys"

echo "== 4/5 sudo restrito ($SUDOERS)"
TMP="$(mktemp)"
printf 'Defaults:%s !requiretty\n%s ALL=(root) NOPASSWD: %s *\n' "$USUARIO" "$USUARIO" "$COMANDO" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -m 440 -o root -g root "$TMP" "$SUDOERS"
rm -f "$TMP"

echo "== 5/5 conferencia"
sudo -u "$USUARIO" env SSH_ORIGINAL_COMMAND=listar SSH_CLIENT="local 0 0" "$COMANDO" | sed 's/^/   /'
if sudo -u "$USUARIO" env SSH_ORIGINAL_COMMAND="reset nao-existe" SSH_CLIENT="local 0 0" "$COMANDO" 2>/dev/null; then
  echo "ERRO: o comando aceitou uma VM nao liberada" >&2; exit 1
fi
echo "   VM nao liberada: recusada (ok)"
RESTR="$(sshd -T -C "user=$USUARIO,host=rmonsrv,addr=$RMON_IP" 2>/dev/null \
         | grep -iE '^(allowusers|allowgroups|denyusers|denygroups) ' || true)"
if [ -n "$RESTR" ]; then
  echo "   ATENCAO: o sshd tem restricao de usuario; confira se $USUARIO entra:"
  echo "$RESTR" | sed 's/^/     /'
fi
echo
echo "Pronto. Impressao digital deste host (confira com a que o definir-incus-reset.sh mostrou):"
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
echo "Pedidos do RMon: journalctl -t rmon-incus-reset"
