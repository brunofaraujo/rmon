#!/usr/bin/env python3
"""Preparo e conferencia do cluster NLB para a regra "no sem contato" (docs/NLB.md).

Nos com `nlb: true` do inventario:

  subir-parado  ClusterModeOnStart = 0 no registro: o no sobe FORA do NLB depois
                de um reboot e o RMon o devolve quando estiver apto. Lido so no
                boot - nao recarrega o NLB, pode rodar a qualquer hora.
                (Aplicado nos 3 nos do 10.10.10.228 em 16/10/2026.)
  checar        So leitura: estado de cada no, convergencia e ClusterModeOnStart.

Exemplos (na VM):
  sudo -u rmon /opt/rmon/.venv/bin/python /opt/rmon/deploy/nlb_controle_remoto.py checar
  sudo -u rmon /opt/rmon/.venv/bin/python /opt/rmon/deploy/nlb_controle_remoto.py subir-parado

O antigo `ligar` (controle remoto do NLB com senha, para um vizinho drenar o no
mudo) foi removido: no Server 2016 o `SetPassword` nao existe mais (0x8004100c,
falhou no job de 16/10/2026 12:00) e o WMI do NLB nao age em no remoto. O no
mudo sai pelo hypervisor (app/incus.py).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.environ.get("RMON_HOME", "/opt/rmon"))  # RMON_HOME: testar uma copia
from app.config import credential_for, load_dotenv, load_inventory  # noqa: E402

load_dotenv("/opt/rmon/.env")
from app import collector, db, nlb  # noqa: E402

ATIVOS = {1006, 1007, 1008}

_PS_COMUM = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
function Get-Eu {
    @(Get-CimInstance -Namespace root/MicrosoftNLB -ClassName MicrosoftNLB_Node |
        Where-Object { $_.ComputerName -eq $env:COMPUTERNAME -or $_.ComputerName -like "$($env:COMPUTERNAME).*" })
}
# @(...): funcao que devolve UM objeto devolve o objeto (sem .Count no PowerShell 5.1)
function Get-Status { $e = @(Get-Eu); if ($e.Count -gt 0) { [int]$e[0].StatusCode } else { $null } }
function Get-Membros {
    $t = (& nlb.exe query 2>&1 | Out-String)
    $m = [regex]::Matches($t, '(?m)^[ \t]*\d+([ \t]*,[ \t]*\d+)*[ \t]*\r?$')
    if ($m.Count -gt 0) { @($m[$m.Count - 1].Value -split ',' | ForEach-Object { [int]$_.Trim() }) } else { @() }
}
function Get-Chave {
    $cs = Get-CimInstance -Namespace root/MicrosoftNLB -ClassName MicrosoftNLB_ClusterSetting
    Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Services\WLBS\Parameters\Interface' |
        Where-Object { (Get-ItemProperty $_.PSPath).ClusterIPAddress -eq $cs.ClusterIPAddress } |
        Select-Object -First 1
}
function Get-Config {
    $cs = Get-CimInstance -Namespace root/MicrosoftNLB -ClassName MicrosoftNLB_ClusterSetting
    $ns = Get-CimInstance -Namespace root/MicrosoftNLB -ClassName MicrosoftNLB_NodeSetting
    $k = Get-Chave
    [ordered]@{
        computer = $env:COMPUTERNAME; cluster = "$($cs.ClusterIPAddress)"; name = "$($cs.Name)"
        status = (Get-Status); members = @(Get-Membros)
        mode_on_start = (Get-ItemProperty $k.PSPath).ClusterModeOnStart
        mode_on_start_wmi = [bool]$ns.ClusterModeOnStart
    }
}
"""

_PS_CHECAR = _PS_COMUM + r"""
$r = Get-Config
[pscustomobject]$r | ConvertTo-Json -Compress -Depth 4
"""

_PS_SUBIR_PARADO = _PS_COMUM + r"""
$k = Get-Chave
$antes = (Get-ItemProperty $k.PSPath).ClusterModeOnStart
Set-ItemProperty -Path $k.PSPath -Name ClusterModeOnStart -Value 0 -Type DWord
$r = Get-Config
$r.mode_on_start_before = $antes
[pscustomobject]$r | ConvertTo-Json -Compress -Depth 4
"""


def rodar(srv, inv, script, budget: int = 120) -> dict:
    user, pw = credential_for(srv.cred)
    r = collector._run_ps_resilient(srv.host, inv.winrm, user, pw, script, attempts=1,
                                    budget_sec=budget)
    if r.status_code != 0:
        raise RuntimeError(collector._decode(r.std_err)[:400] or "WinRM status != 0")
    return json.loads(collector._decode(r.std_out) or "{}")


def nos(inv) -> list:
    # o no padrao do cluster por ultimo: e o que segura o trafego sem regra
    return sorted(nlb.nlb_servers(inv), key=lambda s: s.name.endswith("-34"))


def resumo(nome: str, r: dict) -> str:
    return (f"{nome} ({r.get('computer')}): estado {r.get('status')}, convergidos "
            f"{r.get('members')}, ClusterModeOnStart {r.get('mode_on_start')}")


def checar(inv) -> tuple[bool, list[str]]:
    linhas, ok = [], True
    membros_esperados = None
    for s in nos(inv):
        try:
            r = rodar(s, inv, _PS_CHECAR)
        except Exception as exc:  # noqa: BLE001
            linhas.append(f"{s.name}: SEM CONTATO ({type(exc).__name__}: {exc})")
            ok = False
            continue
        linhas.append(resumo(s.name, r))
        if r.get("status") not in ATIVOS:
            ok = False
        m = sorted(r.get("members") or [])
        membros_esperados = membros_esperados or m
        if m != membros_esperados or len(m) != len(nos(inv)):
            ok = False
    return ok, linhas


def subir_parado(inv) -> list[str]:
    linhas = []
    for s in nos(inv):
        r = rodar(s, inv, _PS_SUBIR_PARADO)
        linhas.append(f"{s.name}: ClusterModeOnStart {r.get('mode_on_start_before')} -> "
                      f"{r.get('mode_on_start')} (WMI: {r.get('mode_on_start_wmi')})")
        db.audit("rmon-nlb", "nlb_subir_parado", linhas[-1])
    return linhas


def main() -> None:
    ap = argparse.ArgumentParser(description="Subida parada e conferencia do NLB")
    ap.add_argument("acao", choices=["checar", "subir-parado"])
    args = ap.parse_args()
    db.init(os.environ["RMON_DB_DSN"])
    inv = load_inventory(os.environ.get("RMON_CONFIG", "/opt/rmon/config/servers.yaml"))
    if args.acao == "checar":
        ok, linhas = checar(inv)
    else:
        ok, linhas = True, subir_parado(inv)
    print("\n".join(linhas))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
