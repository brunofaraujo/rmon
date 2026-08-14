"""Coleta de dados dos servidores Windows via WinRM (PowerShell remoto) + HTTP health."""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
import winrm

from .config import ServerConfig, WinRMConfig, credential_for

# Script PowerShell executado remotamente. Retorna JSON compacto.
# Placeholders __SERVICES__, __LOGS__, __LEVEL__, __MAXEV__ sao substituidos em Python.
_PS_TEMPLATE = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$memTotal = [double]$os.TotalVisibleMemorySize
$memFree  = [double]$os.FreePhysicalMemory
$memPct = if ($memTotal -gt 0) { [math]::Round((($memTotal - $memFree) / $memTotal) * 100, 1) } else { 0 }
$uptime = ((Get-Date) - $os.LastBootUpTime).TotalSeconds
$disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    [pscustomobject]@{
        drive    = $_.DeviceID
        size_gb  = [math]::Round($_.Size / 1GB, 1)
        free_gb  = [math]::Round($_.FreeSpace / 1GB, 1)
        used_pct = if ($_.Size) { [math]::Round((($_.Size - $_.FreeSpace) / $_.Size) * 100, 1) } else { 0 }
    }
}
$svcNames = @(__SERVICES__)
$services = foreach ($n in $svcNames) {
    $s = Get-Service -Name $n -ErrorAction SilentlyContinue
    [pscustomobject]@{ name = $n; status = if ($s) { $s.Status.ToString() } else { 'NOT_FOUND' } }
}
# Ocorrencias: erros/criticos das ultimas N horas, sem ruido, AGRUPADOS por origem+ID
$since = (Get-Date).AddHours(-__LOOKBACK_H__)
$noise = @(__NOISE_IDS__)
$provRe = '__PROV_RE__'
$evAll = @()
foreach ($log in @(__LOGS__)) {
    try {
        $e = Get-WinEvent -FilterHashtable @{ LogName = $log; Level = @(1, 2); StartTime = $since } -MaxEvents 300 -ErrorAction SilentlyContinue
        if ($e) { $evAll += $e }
    } catch { }
}
$events = @($evAll | Where-Object { $noise -notcontains $_.Id -and $_.ProviderName -match $provRe } |
    Group-Object ProviderName, Id | ForEach-Object {
        $last = ($_.Group | Sort-Object TimeCreated -Descending)[0]
        $msg = if ($last.Message) { ($last.Message -replace '\s+', ' ').Trim() } else { '' }
        if ($msg.Length -gt 180) { $msg = $msg.Substring(0, 180) }
        [pscustomobject]@{
            provider = $last.ProviderName; id = $last.Id; count = $_.Count
            last = $last.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'); level = $last.LevelDisplayName; message = $msg
        }
    } | Sort-Object last -Descending | Select-Object -First 12)
$ucount = 0
try { $qu = @(quser 2>$null); if ($qu.Count -gt 1) { $ucount = $qu.Count - 1 } } catch { }
[pscustomobject]@{
    cpu = $cpu; mem_pct = $memPct; mem_total_mb = [math]::Round($memTotal / 1024, 0)
    uptime_sec = [math]::Round($uptime, 0); disks = $disks; services = $services; events = $events
    users_count = $ucount
} | ConvertTo-Json -Depth 5 -Compress
"""


def _as_list(v: Any) -> list:
    """Coerce para lista: o ConvertTo-Json do PowerShell 5.1 serializa colecao
    de 1 elemento como objeto unico (dict), nao array."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _build_script(server: ServerConfig, defaults: dict[str, Any]) -> str:
    services = server.services or list(defaults.get("services") or [])
    el = defaults.get("eventlog") or {}
    logs = list(el.get("logs") or ["System", "Application"])
    lookback = int(el.get("lookback_hours", 24))
    noise = el.get("noise_ids") or [10016, 1058, 1030, 1502, 1500, 7000, 7009]
    prov_re = el.get("providers_regex") or (
        r"\b(RM|TOTVS|SGE|MSSQL|SQL|IIS|W3SVC|WAS|DBAccess|WER)"
        r"|\.NET Runtime|ASP\.NET|Application Error|Application Hang|Windows Error Reporting"
    )
    return (
        _PS_TEMPLATE
        .replace("__SERVICES__", ",".join(f"'{s}'" for s in services))
        .replace("__LOGS__", ",".join(f"'{l}'" for l in logs))
        .replace("__LOOKBACK_H__", str(lookback))
        .replace("__NOISE_IDS__", ",".join(str(int(n)) for n in noise))
        .replace("__PROV_RE__", prov_re)
    )


def _winrm_session(host: str, wc: WinRMConfig, user: str, pw: str) -> winrm.Session:
    endpoint = f"{wc.scheme}://{host}:{wc.port}/wsman"
    return winrm.Session(
        endpoint,
        auth=(user, pw),
        transport=wc.transport,
        server_cert_validation=wc.server_cert_validation,
        read_timeout_sec=wc.read_timeout_sec,
        operation_timeout_sec=wc.operation_timeout_sec,
    )


def _check_app_health(app_health: dict[str, Any] | None) -> tuple[bool | None, int | None]:
    if not app_health or not app_health.get("url"):
        return None, None
    url = app_health["url"]
    expect = int(app_health.get("expect_status", 200))
    timeout = float(app_health.get("timeout_sec", 8))
    start = time.perf_counter()
    try:
        resp = httpx.get(url, timeout=timeout, verify=False, follow_redirects=True)
        ms = int((time.perf_counter() - start) * 1000)
        return (resp.status_code == expect), ms
    except Exception:
        ms = int((time.perf_counter() - start) * 1000)
        return False, ms


# --- Sessoes RDP: listar e encerrar (quser / logoff) ---
_PS_SESSIONS = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$raw = @(quser 2>$null)
$mem = @{}
foreach ($p in (Get-Process -ErrorAction SilentlyContinue)) { $mem[$p.SessionId] = ($mem[$p.SessionId] + $p.WorkingSet64) }
$result = New-Object System.Collections.ArrayList
if ($raw -and $raw.Count -gt 1) {
    foreach ($ln in $raw[1..($raw.Count - 1)]) {
        if ($ln -match '^>?\s*(\S+)\s+(?:(\S+)\s+)?(\d+)\s+(\S+)\s+(.*)$') {
            $sid = [int]$matches[3]
            [void]$result.Add([pscustomobject]@{
                user = $matches[1]; session = $matches[2]; id = $sid
                state = $matches[4]; info = $matches[5].Trim()
                mem_mb = [math]::Round(($mem[$sid]) / 1MB, 0)
            })
        }
    }
}
ConvertTo-Json -InputObject @($result) -Depth 4 -Compress
"""


def list_sessions(server: ServerConfig, wc: WinRMConfig) -> dict[str, Any]:
    """Lista sessoes (quser) de um servidor. Nunca levanta excecao."""
    user, pw = credential_for(server.cred)
    if not user or not pw:
        return {"error": f"sem credencial (perfil '{server.cred}')", "sessions": []}
    try:
        session = _winrm_session(server.host, wc, user, pw)
        r = session.run_ps(_PS_SESSIONS)
        if r.status_code != 0:
            return {"error": (r.std_err or b"").decode("utf-8", "replace")[:300] or "erro WinRM", "sessions": []}
        out = (r.std_out or b"").decode("utf-8", "replace").strip()
        data = json.loads(out) if out else []
        return {"error": None, "sessions": _as_list(data)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300], "sessions": []}


def logoff_session(server: ServerConfig, wc: WinRMConfig, session_id: int) -> tuple[bool, str]:
    """Encerra (logoff) uma sessao pelo ID. session_id e validado como inteiro."""
    user, pw = credential_for(server.cred)
    if not user or not pw:
        return False, f"sem credencial (perfil '{server.cred}')"
    try:
        sid = int(session_id)  # impede injecao no comando
    except (TypeError, ValueError):
        return False, "id de sessao invalido"
    try:
        session = _winrm_session(server.host, wc, user, pw)
        r = session.run_ps(f"logoff {sid}")
        if r.status_code == 0:
            return True, "ok"
        return False, (r.std_err or b"").decode("utf-8", "replace")[:200] or "logoff falhou"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


import re as _re


def service_action(server: ServerConfig, wc: WinRMConfig, service_name: str, action: str = "restart") -> tuple[bool, str]:
    """start/stop/restart de um servico. So permite servicos monitorados do proprio servidor."""
    if service_name not in (server.services or []):
        return False, "servico nao monitorado neste servidor"
    if not _re.fullmatch(r"[A-Za-z0-9 ._#$-]+", service_name or ""):
        return False, "nome de servico invalido"
    cmds = {
        "start": f"Start-Service -Name '{service_name}' -ErrorAction Stop; 'OK'",
        "stop": f"Stop-Service -Name '{service_name}' -Force -ErrorAction Stop; 'OK'",
        "restart": f"Restart-Service -Name '{service_name}' -Force -ErrorAction Stop; 'OK'",
    }
    if action not in cmds:
        return False, "acao invalida"
    user, pw = credential_for(server.cred)
    if not user or not pw:
        return False, f"sem credencial (perfil '{server.cred}')"
    try:
        session = _winrm_session(server.host, wc, user, pw)
        r = session.run_ps(cmds[action])
        if r.status_code == 0:
            return True, action
        return False, (r.std_err or b"").decode("utf-8", "replace")[:200] or f"{action} falhou"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


def collect_server(
    server: ServerConfig, wc: WinRMConfig, defaults: dict[str, Any]
) -> dict[str, Any]:
    """Coleta de um servidor. Nunca levanta excecao: encapsula erro no resultado."""
    result: dict[str, Any] = {
        "reachable": False, "cpu": None, "mem_pct": None, "uptime_sec": None,
        "disks": [], "services": [], "events": [], "app_ok": None, "app_ms": None,
        "error": None,
    }
    # Saude HTTP (independe do WinRM)
    result["app_ok"], result["app_ms"] = _check_app_health(server.app_health)

    user, pw = credential_for(server.cred)
    if not user or not pw:
        prof = (server.cred or "default").upper()
        result["error"] = f"sem credencial para o perfil '{server.cred}' (defina RMON_CRED_{prof}_USER/PASSWORD)"
        return result

    try:
        session = _winrm_session(server.host, wc, user, pw)
        script = _build_script(server, defaults)
        r = session.run_ps(script)
        if r.status_code != 0:
            result["error"] = (r.std_err or b"").decode("utf-8", "replace")[:500] or "WinRM status != 0"
            return result
        data = json.loads((r.std_out or b"").decode("utf-8", "replace"))
        result.update({
            "reachable": True,
            "cpu": data.get("cpu"),
            "mem_pct": data.get("mem_pct"),
            "uptime_sec": data.get("uptime_sec"),
            "disks": _as_list(data.get("disks")),
            "services": _as_list(data.get("services")),
            "events": _as_list(data.get("events")),
            "users_count": data.get("users_count"),
        })
    except Exception as exc:  # noqa: BLE001 - queremos registrar qualquer falha
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return result
