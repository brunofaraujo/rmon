#!/usr/bin/env python3
"""Descoberta de servicos nos servidores RM via WinRM.

Le as credenciais do /opt/rmon/.env (nunca as imprime) e o inventario de
/opt/rmon/config/servers.yaml. Para cada servidor, lista SO, servicos
relacionados a RM/SQL/IIS, servicos auto-start parados e portas em escuta.

Uso (na VM, como root para ler o .env):
    sudo -n /opt/rmon/.venv/bin/python /opt/rmon/deploy/descobrir.py
"""
from __future__ import annotations

import json
import os
import pathlib

import winrm
import yaml

APP_DIR = pathlib.Path("/opt/rmon")

# Carrega o .env (apenas para uso; nao ecoa nada)
for _line in (APP_DIR / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in _line and not _line.lstrip().startswith("#"):
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip())

def _cred_for(profile: str) -> tuple[str, str]:
    prof = (profile or "default").strip().upper()
    u = os.environ.get(f"RMON_CRED_{prof}_USER")
    pw = os.environ.get(f"RMON_CRED_{prof}_PASSWORD")
    if u and pw:
        return u, pw
    return os.environ.get("RMON_WINRM_USER", ""), os.environ.get("RMON_WINRM_PASSWORD", "")


inv = yaml.safe_load((APP_DIR / "config" / "servers.yaml").read_text(encoding="utf-8"))
w = inv["winrm"]
SCHEME, PORT, TRANSPORT = w["scheme"], w["port"], w["transport"]

PS = r"""
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$pat = 'RM|TOTVS|SGE|SQL|MSSQL|Report|IIS|W3SVC|DBAccess|Job|\.Host|Smart|Protheus|Fluig'
$svc = Get-CimInstance Win32_Service | ForEach-Object {
    [pscustomobject]@{ name=$_.Name; display=$_.DisplayName; status=$_.State; start=$_.StartMode }
}
$rm = @($svc | Where-Object { $_.name -match $pat -or $_.display -match $pat })
$autostopped = @($svc | Where-Object { $_.start -eq 'Auto' -and $_.status -ne 'Running' } |
    Select-Object name, display)
$ports = @()
try {
    $ports = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Select-Object -ExpandProperty LocalPort -Unique | Where-Object { $_ -lt 10000 } | Sort-Object -Unique)
} catch { }
[pscustomobject]@{ host=$os.CSName; os=$os.Caption; rm_services=$rm; auto_stopped=$autostopped; ports=$ports } |
    ConvertTo-Json -Depth 5 -Compress
"""


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def main() -> None:
    for srv in inv["servers"]:
        name, host = srv["name"], srv["host"]
        profile = srv.get("cred", "default")
        print("=" * 64)
        print(f"{name}  ({host})  [perfil: {profile}]")
        user, pw = _cred_for(profile)
        if not user or not pw:
            print(f"  SEM CREDENCIAL para o perfil '{profile}' "
                  f"(defina RMON_CRED_{profile.upper()}_USER/PASSWORD com definir-winrm.sh)")
            continue
        try:
            s = winrm.Session(f"{SCHEME}://{host}:{PORT}/wsman", auth=(user, pw), transport=TRANSPORT)
            r = s.run_ps(PS)
            if r.status_code != 0:
                print("  ERRO WinRM:", (r.std_err or b"").decode("utf-8", "replace")[:400])
                continue
            d = json.loads((r.std_out or b"").decode("utf-8", "replace"))
            print("  OS:", d.get("os"))
            print("  Portas ouvindo:", d.get("ports"))
            rm = _as_list(d.get("rm_services"))
            print(f"  Servicos RM/SQL/IIS ({len(rm)}):")
            for x in rm:
                print(f"    - {x['name']:32} [{x['status']}/{x['start']}]  {x['display']}")
            auto = _as_list(d.get("auto_stopped"))
            if auto:
                print(f"  ATENCAO - auto-start PARADOS ({len(auto)}):")
                for x in auto:
                    print(f"    - {x['name']}  ({x['display']})")
        except Exception as exc:  # noqa: BLE001
            print("  EXCECAO:", type(exc).__name__, str(exc)[:300])


if __name__ == "__main__":
    main()
