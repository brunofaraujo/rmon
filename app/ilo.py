"""Reset de servidor fisico HPE pela iLO (Redfish): o canal fora do Windows.

Servidor fisico que trava sem contato (o .222 em 19/10: um JobRunner esgotou a
memoria e o WinRM, o RPC e o agente do Elastic pararam juntos) so volta pelo
botao ou pela iLO. A iLO 4/5 fala Redfish: um POST no
`ComputerSystem.Reset` com `ForceRestart` reinicia o servidor sem passar pelo
Windows.

Configuracao, por servidor no servers.yaml:

  ilo: 10.10.x.y        # IP (ou nome) da iLO
  ilo_cred: ilo         # perfil: RMON_CRED_ILO_USER / RMON_CRED_ILO_PASSWORD no .env

A credencial e so do perfil: ao contrario do WinRM, NAO cai no par legado
RMON_WINRM_* - a senha do dominio nunca vai para a iLO. Use um usuario local da
iLO com o privilegio "Virtual Power and Reset" e nada mais.

O certificado da iLO costuma ser o autoassinado de fabrica. Com
/opt/rmon/config/ilo-ca.crt presente, ele e conferido contra esse arquivo; sem,
a conexao e cifrada mas o certificado nao e conferido (rede de gerencia).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import requests

# IP ou nome DNS: nada alem disso chega a URL
HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
CA_PADRAO = "/opt/rmon/config/ilo-ca.crt"
SISTEMA = "/redfish/v1/Systems/1/"

# o canal e conferido de hora em hora; falhou, de novo em 10 min (como o Incus)
CHECAR_OK_S = 3600
CHECAR_FALHA_S = 600


def configurado(server) -> bool:
    return bool(HOST.match(getattr(server, "ilo", "") or ""))


def _credencial(perfil: str) -> tuple[str, str]:
    prof = (perfil or "ilo").strip().upper()
    return (os.environ.get(f"RMON_CRED_{prof}_USER", ""),
            os.environ.get(f"RMON_CRED_{prof}_PASSWORD", ""))


def _verify() -> Any:
    ca = os.environ.get("RMON_ILO_CA", CA_PADRAO)
    if ca and Path(ca).is_file():
        return ca
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return False


def _pedir(server, metodo: str, caminho: str, timeout: int, **kw) -> tuple[bool, Any]:
    if not configurado(server):
        return False, f"iLO inválida: {getattr(server, 'ilo', '')!r}"
    user, pw = _credencial(getattr(server, "ilo_cred", "ilo"))
    if not user or not pw:
        prof = (getattr(server, "ilo_cred", "") or "ilo").upper()
        return False, f"sem credencial da iLO (RMON_CRED_{prof}_USER/_PASSWORD no .env)"
    url = f"https://{server.ilo}{caminho}"
    try:
        r = requests.request(metodo, url, auth=(user, pw), verify=_verify(), timeout=timeout,
                             headers={"Accept": "application/json"}, **kw)
    except requests.RequestException as exc:
        return False, f"iLO {server.ilo} sem resposta: {type(exc).__name__}"
    if r.status_code in (401, 403):
        return False, f"iLO {server.ilo} recusou a credencial ({r.status_code})"
    if r.status_code >= 300:
        return False, f"iLO {server.ilo} respondeu {r.status_code}: {(r.text or '')[:200]}"
    return True, r


def reset(server) -> tuple[bool, str]:
    """ForceRestart pela iLO (o equivalente a segurar o botao e ligar de novo)."""
    ok, r = _pedir(server, "POST", SISTEMA + "Actions/ComputerSystem.Reset/", 30,
                   json={"ResetType": "ForceRestart"})
    if not ok:
        return False, r
    return True, f"reiniciado pela iLO {server.ilo} (ForceRestart)"


def checar(server) -> dict[str, Any]:
    """O canal funciona? So leitura: o estado de energia do servidor."""
    ok, r = _pedir(server, "GET", SISTEMA, 15)
    if not ok:
        return {"ok": False, "detail": r}
    try:
        d = r.json()
    except ValueError:
        return {"ok": False, "detail": f"iLO {server.ilo} não respondeu Redfish"}
    return {"ok": True, "detail": f"iLO {server.ilo}: {d.get('Model') or 'servidor'} "
                                  f"{d.get('PowerState') or '?'}"}


def devido(estado: dict | None, agora: float) -> bool:
    if not estado or not estado.get("ts"):
        return True
    passo = CHECAR_OK_S if estado.get("ok") else CHECAR_FALHA_S
    return agora - float(estado["ts"]) >= passo
