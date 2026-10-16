#!/usr/bin/env bash
# Canal do RMon com o hypervisor Incus: reset da VM de no do NLB travado sem
# contato (docs/NLB.md, "No sem contato"). Rode como root na VM do RMon:
#
#   sudo bash /opt/rmon/deploy/definir-incus-reset.sh [rmon-reset@10.10.10.161]
#       gera a chave do RMon (se ainda nao existe), grava a chave do host no
#       known_hosts e mostra a linha para colar no hypervisor, com
#       deploy/incus/instalar-no-hypervisor.sh
#
#   sudo bash /opt/rmon/deploy/definir-incus-reset.sh --testar [rmon-reset@10.10.10.161]
#       roda o `listar` como o RMon roda (so leitura): mostra as VMs liberadas
#
# Depois, no servers.yaml: defaults.nlb.incus_ssh: rmon-reset@10.10.10.161 e
# `incus_vm: <nome>` em cada no do NLB que e VM; e reinicie o rmon.
set -euo pipefail
APP_DIR=/opt/rmon
DIR="$APP_DIR/config/incus-reset"
CHAVE="$DIR/id_ed25519"
KNOWN="$DIR/known_hosts"
[ "$(id -u)" -eq 0 ] || { echo "Rode como root (sudo)." >&2; exit 1; }

TESTAR=0
if [ "${1:-}" = "--testar" ]; then TESTAR=1; shift; fi
DESTINO="${1:-rmon-reset@10.10.10.161}"
HOST="${DESTINO#*@}"

if [ "$TESTAR" -eq 1 ]; then
  [ -f "$CHAVE" ] && [ -s "$KNOWN" ] || { echo "Rode antes sem --testar." >&2; exit 1; }
  echo "listar em $DESTINO, como o RMon:"
  sudo -u rmon ssh -T -i "$CHAVE" -o BatchMode=yes -o IdentitiesOnly=yes \
       -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KNOWN" -o ConnectTimeout=10 \
       "$DESTINO" -- listar
  exit $?
fi

install -d -m 700 -o rmon -g rmon "$DIR"
if [ ! -f "$CHAVE" ]; then
  sudo -u rmon ssh-keygen -q -t ed25519 -N '' -C "rmon@$(hostname -s)" -f "$CHAVE"
  echo "Chave nova gerada em $CHAVE"
fi

TMP="$(mktemp)"
ssh-keyscan -T 10 -t ed25519 "$HOST" 2>/dev/null > "$TMP"
[ -s "$TMP" ] || { rm -f "$TMP"; echo "O host $HOST nao respondeu na porta 22." >&2; exit 1; }
install -m 600 -o rmon -g rmon "$TMP" "$KNOWN"
rm -f "$TMP"

echo
echo "Chave do host $HOST gravada. Confira que e a mesma que o instalador mostra no fim:"
ssh-keygen -lf "$KNOWN"
echo
echo "No hypervisor ($HOST), como root:"
echo "  curl -fsSLO https://raw.githubusercontent.com/brunofaraujo/rmon/main/deploy/incus/instalar-no-hypervisor.sh"
echo "  sudo bash instalar-no-hypervisor.sh '$(cat "$CHAVE.pub")'"
echo
echo "Depois, aqui: sudo bash $0 --testar $DESTINO"
