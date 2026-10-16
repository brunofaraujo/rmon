#!/usr/bin/env python3
"""Prepara o cluster NLB para a regra "no sem contato" (docs/NLB.md).

Duas mudancas nos nos com `nlb: true` do inventario, uma de cada vez:

  subir-parado  ClusterModeOnStart = 0 no registro: o no sobe FORA do NLB depois
                de um reboot e o RMon o devolve quando estiver apto. Lido so no
                boot - nao recarrega o NLB, pode rodar a qualquer hora.
  ligar         Controle remoto do NLB ligado, com a senha RMON_NLB_RC_PASSWORD
                do .env. Recarrega o NLB do no (reconvergencia de segundos): rodar
                FORA DO HORARIO. Um no por vez; para no primeiro que nao voltar a
                convergir com os outros.
  checar        So leitura: estado de cada no, convergencia, ClusterModeOnStart,
                controle remoto e se um vizinho enxerga/consulta os outros.

Exemplos (na VM):
  sudo -u rmon /opt/rmon/.venv/bin/python /opt/rmon/deploy/nlb_controle_remoto.py checar
  sudo -u rmon /opt/rmon/.venv/bin/python /opt/rmon/deploy/nlb_controle_remoto.py subir-parado
  sudo -u rmon /opt/rmon/.venv/bin/python /opt/rmon/deploy/nlb_controle_remoto.py ligar

A senha chega aos nos por variavel de ambiente do shell WinRM, nunca no texto do
script (que fica no log de script do PowerShell). Depois de ligar, o `checar` mostra se cada no le o estado dos vizinhos pelo
WMI - e o que a drenagem do no sem contato usa (app/collector.py). Tudo vai para o audit_log
(usuario rmon-nlb) e, no `ligar`, o resumo vai para o Slack/Telegram.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.environ.get("RMON_HOME", "/opt/rmon"))  # RMON_HOME: testar uma copia
from app.config import credential_for, load_dotenv, load_inventory  # noqa: E402

load_dotenv("/opt/rmon/.env")
from app import collector, db, nlb, notify  # noqa: E402

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
        remote_control = [bool]$cs.RemoteControlEnabled
        mode_on_start = (Get-ItemProperty $k.PSPath).ClusterModeOnStart
        mode_on_start_wmi = [bool]$ns.ClusterModeOnStart
    }
}
"""

_PS_CHECAR = _PS_COMUM + r"""
$r = Get-Config
# o que este no enxerga dos outros pelo WMI (StatusCode 0 = sem controle remoto)
$r.peers_wmi = @(Get-CimInstance -Namespace root/MicrosoftNLB -ClassName MicrosoftNLB_Node |
    ForEach-Object { "$($_.Name)=$([int]$_.StatusCode)" })
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

_PS_LIGAR = _PS_COMUM + r"""
if (-not $env:RMON_NLB_RC) { throw 'sem senha (RMON_NLB_RC)' }
$passos = @()
$antes = Get-Status
$cs = Get-CimInstance -Namespace root/MicrosoftNLB -ClassName MicrosoftNLB_ClusterSetting
try {
    Set-CimInstance -InputObject $cs -Property @{ RemoteControlEnabled = $true }
    $passos += 'RemoteControlEnabled=wmi'
} catch {
    Set-ItemProperty -Path (Get-Chave).PSPath -Name RemoteControlEnabled -Value 1 -Type DWord
    $passos += "RemoteControlEnabled=registro ($($_.Exception.Message.Trim()))"
}
$cs = Get-CimInstance -Namespace root/MicrosoftNLB -ClassName MicrosoftNLB_ClusterSetting
$x = Invoke-CimMethod -InputObject $cs -MethodName SetPassword -Arguments @{ Password = $env:RMON_NLB_RC }
$passos += "SetPassword=$($x.ReturnValue)"
$x = Invoke-CimMethod -InputObject $cs -MethodName LoadAllSettings
$passos += "LoadAllSettings=$($x.ReturnValue)"
$fim = (Get-Date).AddSeconds(60)
do { Start-Sleep -Seconds 3; $s = Get-Status } while ((Get-Date) -lt $fim -and $s -notin 1007, 1008)
if ($s -eq 1005) {
    # recarregar nao deveria parar o no; se parou, volta na hora
    $passos += 'parado depois do reload: nlb start'
    & nlb.exe start 2>&1 | Out-Null
    $fim = (Get-Date).AddSeconds(45)
    do { Start-Sleep -Seconds 3; $s = Get-Status } while ((Get-Date) -lt $fim -and $s -notin 1007, 1008)
}
$r = Get-Config
$r.status_before = $antes
$r.passos = $passos
[pscustomobject]$r | ConvertTo-Json -Compress -Depth 4
"""


def rodar(srv, inv, script, segredo: str = "", budget: int = 120) -> dict:
    user, pw = credential_for(srv.cred)
    env = {"RMON_NLB_RC": segredo} if segredo else None
    r = collector._run_ps_resilient(srv.host, inv.winrm, user, pw, script, attempts=1,
                                    budget_sec=budget, env=env)
    if r.status_code != 0:
        raise RuntimeError(collector._decode(r.std_err)[:400] or "WinRM status != 0")
    return json.loads(collector._decode(r.std_out) or "{}")


def nos(inv) -> list:
    # o no padrao do cluster por ultimo: e o que segura o trafego sem regra
    return sorted(nlb.nlb_servers(inv), key=lambda s: s.name.endswith("-34"))


def resumo(nome: str, r: dict) -> str:
    return (f"{nome} ({r.get('computer')}): estado {r.get('status')}, convergidos "
            f"{r.get('members')}, controle remoto {'ligado' if r.get('remote_control') else 'desligado'}, "
            f"ClusterModeOnStart {r.get('mode_on_start')}")


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
        vistos = r.get("peers_wmi") or []
        linhas.append(f"   WMI enxerga: {', '.join(vistos)}")
        # com o controle remoto ligado, o vizinho le o estado dos OUTROS nos
        if r.get("remote_control") and any(x.endswith("=0") for x in vistos):
            linhas.append("   ATENCAO: controle remoto ligado, mas este no nao le o estado de algum vizinho")
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


def ligar(inv, segredo: str) -> tuple[bool, list[str]]:
    ok, linhas = checar(inv)
    if not ok:
        return False, ["ABORTADO antes de mexer: o cluster nao esta inteiro e convergido."] + linhas
    total = len(nos(inv))
    for s in nos(inv):
        try:
            r = rodar(s, inv, _PS_LIGAR, segredo, budget=180)
        except Exception as exc:  # noqa: BLE001
            linhas.append(f"{s.name}: FALHOU ({type(exc).__name__}: {exc}). Parei aqui.")
            return False, linhas
        linhas.append(resumo(s.name, r) + f"; passos: {'; '.join(r.get('passos') or [])}")
        db.audit("rmon-nlb", "nlb_controle_remoto", linhas[-1][:900])
        if r.get("status") not in ATIVOS or len(r.get("members") or []) != total \
                or not r.get("remote_control"):
            linhas.append(f"{s.name}: nao voltou inteiro (estado/convergencia/controle remoto). Parei aqui.")
            return False, linhas
        time.sleep(20)
    ok, depois = checar(inv)
    # o objetivo e o vizinho enxergar (e poder drenar) os outros pelo WMI
    if ok and any("ATENCAO" in x for x in depois):
        ok = False
    return ok, linhas + ["--- depois:"] + depois


def main() -> None:
    ap = argparse.ArgumentParser(description="Controle remoto e subida parada do NLB")
    ap.add_argument("acao", choices=["checar", "subir-parado", "ligar"])
    args = ap.parse_args()
    db.init(os.environ["RMON_DB_DSN"])
    inv = load_inventory(os.environ.get("RMON_CONFIG", "/opt/rmon/config/servers.yaml"))
    segredo = os.environ.get("RMON_NLB_RC_PASSWORD", "")
    if args.acao == "checar":
        ok, linhas = checar(inv)
    elif args.acao == "subir-parado":
        ok, linhas = True, subir_parado(inv)
    else:
        if not segredo:
            sys.exit("RMON_NLB_RC_PASSWORD vazio no /opt/rmon/.env")
        ok, linhas = ligar(inv, segredo)
        texto = ("✅ NLB: controle remoto ligado nos nos" if ok else
                 "❌ NLB: ligar o controle remoto NAO terminou") + "\n" + "\n".join(linhas)
        db.audit("rmon-nlb", "nlb_controle_remoto_fim", ("ok " if ok else "FALHOU ") + texto[:850])
        if notify.enabled():
            notify.send(texto[:3500])
    print("\n".join(linhas))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
