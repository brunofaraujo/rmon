"""Coleta de dados dos servidores Windows via WinRM (PowerShell remoto) + HTTP health."""
from __future__ import annotations

import json
import logging
import time
from base64 import b64encode
from typing import Any, Iterable

import httpx
import requests
import winrm
from winrm.exceptions import WinRMOperationTimeoutError

from .config import ServerConfig, WinRMConfig, credential_for

log = logging.getLogger("rmon.collector")

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
# Servicos: os fixos vem do inventario; os padroes (__PATTERNS__) sao expandidos
# aqui, no proprio host, para o monitor enxergar o que esta REALMENTE instalado
# (ex.: quantos RM.Host existem hoje) em vez de uma lista que envelhece.
$svcNames = @(__SERVICES__)
$svcPatterns = @(__PATTERNS__)
$allSvc = @(Get-CimInstance Win32_Service | Select-Object Name, DisplayName, State, StartMode)
$services = New-Object System.Collections.ArrayList
$seen = @{}
foreach ($n in $svcNames) {
    $s = @($allSvc | Where-Object { $_.Name -eq $n -or $_.DisplayName -eq $n })[0]
    if ($s) {
        $seen[$s.Name] = $true
        [void]$services.Add([pscustomobject]@{
            name = $s.Name; status = $s.State; start = $s.StartMode
            display = $s.DisplayName; src = 'fixed' })
    } else {
        [void]$services.Add([pscustomobject]@{
            name = $n; status = 'NOT_FOUND'; start = $null; display = $null; src = 'fixed' })
    }
}
foreach ($p in $svcPatterns) {
    foreach ($s in @($allSvc | Where-Object { $_.Name -like $p -or $_.DisplayName -like $p } | Sort-Object Name)) {
        if ($seen[$s.Name]) { continue }
        $seen[$s.Name] = $true
        [void]$services.Add([pscustomobject]@{
            name = $s.Name; status = $s.State; start = $s.StartMode
            display = $s.DisplayName; src = 'auto'; pattern = $p })
    }
}
$services = @($services)
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


def _decode(raw: bytes | None) -> str:
    """Texto vindo do host. O stdout tem encoding forcado para UTF-8 pelo
    proprio script; ja o stderr sai no code page do console do Windows
    (cp850 em pt-BR), entao tentamos UTF-8 e caimos nele antes de desistir -
    senao a mensagem de erro chega ilegivel no painel.
    """
    if not raw:
        return ""
    for enc in ("utf-8", "cp850", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _ps_str(value: str) -> str:
    """Literal PowerShell entre aspas simples (aspa interna dobrada)."""
    return "'" + str(value).replace("'", "''") + "'"


def _build_script(server: ServerConfig, defaults: dict[str, Any]) -> str:
    services = server.services or list(defaults.get("services") or [])
    patterns = server.service_patterns or list(defaults.get("service_patterns") or [])
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
        .replace("__SERVICES__", ",".join(_ps_str(s) for s in services))
        .replace("__PATTERNS__", ",".join(_ps_str(p) for p in patterns))
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


# Bootstrap: le o stdin inteiro e executa de uma vez. Nao da para mandar o
# script direto em "powershell -Command -", porque nesse modo o PowerShell le
# linha a linha como se fosse digitado e o primeiro bloco multilinha (foreach,
# ForEach-Object...) engole silenciosamente todo o resto - saida vazia, rc 0.
_BOOTSTRAP = b64encode(
    "$s = [Console]::In.ReadToEnd(); Invoke-Expression $s".encode("utf_16_le")
).decode("ascii")


def _deadline(wc: WinRMConfig) -> float:
    """Prazo maximo de uma execucao remota, para nenhuma chamada ficar presa."""
    return time.monotonic() + max(30, wc.read_timeout_sec * 2)


def _receive(proto, shell_id: str, command_id: str,
             deadline: float | None) -> tuple[bytes, bytes, int]:
    """Le a saida do comando respeitando um prazo.

    O get_command_output do pywinrm engole WinRMOperationTimeoutError e repete
    "silently retry indefinitely": se o host parar de responder no meio da
    entrega da saida, a coleta fica presa sem limite e segura o ciclo inteiro do
    scheduler. Aqui o mesmo timeout e tolerado, porem so ate o prazo.
    """
    out: list[bytes] = []
    err: list[bytes] = []
    while True:
        try:
            o, e, status, done = proto.get_command_output_raw(shell_id, command_id)
            out.append(o)
            err.append(e)
            if done:
                return b"".join(out), b"".join(err), status
        except WinRMOperationTimeoutError:
            pass  # normal enquanto o comando ainda roda
        if deadline is not None and time.monotonic() >= deadline:
            raise WinRMOperationTimeoutError()


def _run_ps(session: winrm.Session, script: str, deadline: float | None = None) -> winrm.Response:
    """Executa PowerShell remoto SEMPRE fechando o shell no Windows.

    O Session.run_cmd do pywinrm nao tem try/finally: se o operation timeout
    estourar em run_command, o close_shell nunca e chamado e o shell fica orfao
    no host ate o IdleTimeout do WinRM (PT7200S = 2h por padrao). Como cada
    coleta abre um shell novo e os timeouts aqui sao rotineiros (ver
    _run_ps_resilient), vale garantir a limpeza para nao deixar shells presos
    justamente nos hosts que ja estao sobrecarregados.

    O script vai por STDIN, e nao inteiro em -EncodedCommand: encodado ele vira
    base64 de UTF-16LE (~2,7 caracteres por caractere de script) na linha de
    comando do cmd.exe, que corta em ~8k - a coleta inteira morria com "Linha
    de comando muito longa" assim que o script cresceu. Por stdin o tamanho
    deixa de ser um limite; o que vai na linha de comando e so o bootstrap.
    """
    proto = session.protocol
    shell_id = proto.open_shell()
    command_id = None
    try:
        command_id = proto.run_command(
            shell_id, f"powershell -NoProfile -NonInteractive -EncodedCommand {_BOOTSTRAP}",
            console_mode_stdin=False)
        proto.send_command_input(shell_id, command_id, script.encode("utf-8"), end=True)
        std_out, std_err, status = _receive(proto, shell_id, command_id, deadline)
        if std_err:
            std_err = session._clean_error_msg(std_err)
        return winrm.Response((std_out, std_err, status))
    finally:
        if command_id is not None:
            try:
                proto.cleanup_command(shell_id, command_id)
            except Exception as exc:  # noqa: BLE001 - limpeza e best-effort
                log.debug("cleanup_command falhou: %s", exc)
        try:
            proto.close_shell(shell_id)
        except Exception as exc:  # noqa: BLE001 - limpeza e best-effort
            log.debug("close_shell falhou: %s", exc)


# Falhas transitorias que valem uma segunda tentativa: sao timeout, nao "host fora".
_TRANSIENT = (WinRMOperationTimeoutError, requests.exceptions.Timeout,
              requests.exceptions.ConnectionError)


def _run_ps_resilient(host: str, wc: WinRMConfig, user: str, pw: str, script: str,
                      attempts: int = 2, budget_sec: float | None = None) -> winrm.Response:
    """Roda o script tentando de novo quando a falha foi timeout.

    Servidores de aplicacao com muitas sessoes travam esporadicamente por
    dezenas de segundos em qualquer fase do WinRM (medido: 12-115s), enquanto a
    tentativa seguinte costuma responder em menos de 1s. Sem essa repeticao, um
    engasgo isolado do host virava alerta de "servidor fora do ar".

    `budget_sec` limita o tempo TOTAL gasto com o host (todas as tentativas),
    para que um servidor travado nao atrase a coleta dos demais.
    """
    deadline = (time.monotonic() + budget_sec) if budget_sec is not None else _deadline(wc)
    last: Exception | None = None
    for i in range(max(1, attempts)):
        session = _winrm_session(host, wc, user, pw)
        try:
            return _run_ps(session, script, deadline)
        except _TRANSIENT as exc:
            last = exc
            log.info("WinRM %s: %s na tentativa %d/%d", host, type(exc).__name__, i + 1, attempts)
            if time.monotonic() >= deadline:
                break
    raise last  # type: ignore[misc]


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
        r = _run_ps(session, _PS_SESSIONS, _deadline(wc))
        if r.status_code != 0:
            return {"error": _decode(r.std_err)[:300] or "erro WinRM", "sessions": []}
        out = _decode(r.std_out).strip()
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
        r = _run_ps(session, f"logoff {sid}", _deadline(wc))
        if r.status_code == 0:
            return True, "ok"
        return False, _decode(r.std_err)[:200] or "logoff falhou"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


import re as _re


def service_action(server: ServerConfig, wc: WinRMConfig, service_name: str, action: str = "restart",
                   allowed: Iterable[str] | None = None) -> tuple[bool, str]:
    """start/stop/restart de um servico. So permite servicos monitorados do proprio servidor.

    `allowed` cobre os servicos descobertos por padrao (service_patterns), que
    nao estao na lista fixa do inventario: quem chama passa os nomes vindos da
    ultima coleta daquele servidor.
    """
    permitido = set(server.services or []) | set(allowed or ())
    if service_name not in permitido:
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
        r = _run_ps(session, cmds[action], _deadline(wc))
        if r.status_code == 0:
            return True, action
        return False, _decode(r.std_err)[:200] or f"{action} falhou"
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
        script = _build_script(server, defaults)
        r = _run_ps_resilient(server.host, wc, user, pw, script)
        if r.status_code != 0:
            result["error"] = _decode(r.std_err)[:500] or "WinRM status != 0"
            return result
        data = json.loads(_decode(r.std_out))
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
