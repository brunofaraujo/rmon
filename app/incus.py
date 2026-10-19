"""Reset de VM pelo Incus: tira do NLB um no que travou sem contato.

No Windows Server 2016 um vizinho do cluster NAO consegue drenar outro no: o
provedor WMI root/MicrosoftNLB so age no proprio no (instancia remota tem
StatusCode 0 e todo metodo falha), e o `SetPassword` do controle remoto foi
removido no Server 2008. Quem chega no no travado sem depender do Windows dele
e o hypervisor: reiniciar a VM derruba o driver do NLB na hora (os outros nos
reconvergem em segundos) e ela volta FORA do NLB (ClusterModeOnStart=0), para o
reingresso normal do RMon devolve-la quando estiver apta.

O RMon nao tem acesso ao Incus. Fala por SSH com um usuario do hypervisor preso
a um comando forcado (deploy/incus/instalar-no-hypervisor.sh), que so aceita:

  listar            as instancias liberadas: "<nome> <estado> <mac>" por linha
  reset <nome>      `incus restart --force` de uma instancia liberada, no maximo
                    uma vez a cada 30 min por instancia

A chave e o known_hosts ficam em /opt/rmon/config/incus-reset/
(deploy/definir-incus-reset.sh). Ver docs/NLB.md, "No sem contato".
"""
from __future__ import annotations

import re
import subprocess
from typing import Any

# nome de instancia do Incus; tambem impede que algo alem de um nome chegue ao
# comando remoto (o comando forcado valida de novo contra a lista liberada)
NOME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

# o canal e conferido de hora em hora; falhou, de novo em 10 min
CHECAR_OK_S = 3600
CHECAR_FALHA_S = 600


def configurado(cfg: dict) -> bool:
    return bool((cfg.get("incus_ssh") or "").strip())


def _ssh(cfg: dict, args: list[str], timeout: int) -> tuple[bool, str]:
    destino = (cfg.get("incus_ssh") or "").strip()
    if not destino:
        return False, "defaults.nlb.incus_ssh vazio"
    cmd = ["ssh", "-T", "-i", str(cfg.get("incus_key") or ""),
           "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
           "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={cfg.get('incus_known_hosts') or ''}",
           "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
           "-o", "ServerAliveCountMax=3", "-o", "LogLevel=ERROR",
           destino, "--", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "cliente ssh ausente na VM do RMon"
    except subprocess.TimeoutExpired:
        return False, f"o hypervisor não respondeu em {timeout}s"
    saida, erro = (r.stdout or "").strip(), (r.stderr or "").strip()
    if r.returncode != 0:
        return False, (erro or saida or f"ssh saiu com {r.returncode}")[-300:]
    return True, saida[-1500:]


def reset(cfg: dict, vm: str) -> tuple[bool, str]:
    """`incus restart --force` da instancia `vm`, pelo comando forcado."""
    if not NOME.match(vm or ""):
        return False, f"instância inválida: {vm!r}"
    return _ssh(cfg, ["reset", vm], timeout=120)


def ler_lista(saida: str) -> dict[str, dict[str, str]]:
    """{"nome": {"estado", "mac"}} da saida do `listar`."""
    vms: dict[str, dict[str, str]] = {}
    for linha in (saida or "").splitlines():
        partes = linha.split()
        if len(partes) >= 2 and NOME.match(partes[0]):
            vms[partes[0]] = {"estado": partes[1], "mac": partes[2] if len(partes) > 2 else ""}
    return vms


def checar(cfg: dict, esperadas: dict[str, str]) -> dict[str, Any]:
    """O canal funciona e libera as VMs do inventario? `esperadas`: {servidor: vm}.

    So leitura (`listar`). O resultado vai para a tela /nlb.
    """
    ok, saida = _ssh(cfg, ["listar"], timeout=30)
    if not ok:
        return {"ok": False, "detail": saida, "vms": {}}
    vms = ler_lista(saida)
    faltam = [f"{srv} ({vm})" for srv, vm in sorted(esperadas.items()) if vm not in vms]
    if faltam:
        return {"ok": False, "vms": vms,
                "detail": "instância(s) não liberada(s) no hypervisor: " + ", ".join(faltam)}
    return {"ok": True, "vms": vms, "detail": f"{len(vms)} instância(s) liberada(s)"}


def devido(estado: dict | None, agora: float) -> bool:
    """Hora de conferir o canal de novo?"""
    if not estado or not estado.get("ts"):
        return True
    passo = CHECAR_OK_S if estado.get("ok") else CHECAR_FALHA_S
    return agora - float(estado["ts"]) >= passo
