"""Coleta de dados dos servidores Windows via WinRM (PowerShell remoto) + HTTP health."""
from __future__ import annotations

import json
import logging
import socket
import struct
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
# Exclusoes (__EXCLUDE__): casam no curinga mas nao interessam ao monitor
# (ex.: RM.Host.Cleanner). Somem da descoberta e do broker; fixo nao e afetado.
$svcExclude = @(__EXCLUDE__)
# Agentes de coleta (ex.: Elastic Agent): descobertos a parte, para
# nao entrarem na busca de pastas do broker, que usa os curingas do RM.
$agentPatterns = @(__AGENTS__)
function Test-Excluido($s) {
    foreach ($x in $svcExclude) {
        if ($s.Name -like $x -or $s.DisplayName -like $x) { return $true }
    }
    return $false
}
$allSvc = @(Get-CimInstance Win32_Service | Select-Object Name, DisplayName, State, StartMode, PathName)
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
        if (Test-Excluido $s) { continue }
        $seen[$s.Name] = $true
        [void]$services.Add([pscustomobject]@{
            name = $s.Name; status = $s.State; start = $s.StartMode
            display = $s.DisplayName; src = 'auto'; pattern = $p })
    }
}
foreach ($p in $agentPatterns) {
    foreach ($s in @($allSvc | Where-Object { $_.Name -like $p -or $_.DisplayName -like $p } | Sort-Object Name)) {
        if ($seen[$s.Name]) { continue }
        $seen[$s.Name] = $true
        [void]$services.Add([pscustomobject]@{
            name = $s.Name; status = $s.State; start = $s.StartMode
            display = $s.DisplayName; src = 'agente'; pattern = $p })
    }
}
$services = @($services)
# Broker de customizacoes: o RM so gera _BrokerCustom.dat quando ele NAO existe.
# Se a geracao aborta no meio (falta de commit/pagefile), fica um arquivo curto
# no lugar - e o RM passa a reusar esse cache truncado para sempre, subindo
# "com sucesso" e sem as customizacoes. Aqui so medimos: tamanho e idade.
$brokerFiles = @(__BROKER_FILES__)
# Quem, dentro da pasta, realmente gera o broker. Um servico auxiliar que more
# na mesma instalacao do RM.Host.Service e nao gere nada, no ar sozinho, nao
# torna a ausencia do arquivo uma anomalia. Pasta onde nenhum servico
# casa com a lista cai no criterio antigo (qualquer servico dela vale) - assim
# uma instalacao com nome fora do padrao nao fica sem vigilancia.
$brokerGen = @(__BROKER_GENERATORS__)
$broker = New-Object System.Collections.ArrayList
if ($brokerFiles.Count -gt 0) {
    # Cada pasta guarda QUEM mora nela: o broker daquela instalacao so tinha de
    # ter sido gerado se um servico DESSA pasta subiu. Servico de outra pasta -
    # ou um vizinho que nem gera broker - nao serve de prova.
    $brokerDirs = @{}
    foreach ($s in $allSvc) {
        $casa = $false
        foreach ($p in $svcPatterns) {
            if ($s.Name -like $p -or $s.DisplayName -like $p) { $casa = $true; break }
        }
        if (-not $casa -or (Test-Excluido $s)) { continue }
        $cmd = "$($s.PathName)".Trim()
        if ($cmd -match '^\s*"([^"]+)"') { $exe = $matches[1] } else { $exe = ($cmd -split '\s+')[0] }
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { continue }
        $dir = [System.IO.Path]::GetDirectoryName($exe)
        if (-not $brokerDirs.ContainsKey($dir)) {
            $brokerDirs[$dir] = New-Object System.Collections.ArrayList
        }
        [void]$brokerDirs[$dir].Add([pscustomobject]@{ name = $s.Name; state = $s.State })
    }
    foreach ($d in @($brokerDirs.Keys)) {
        $donos = @($brokerDirs[$d])
        $geradores = New-Object System.Collections.ArrayList
        foreach ($o in $donos) {
            foreach ($g in $brokerGen) {
                if ($o.name -like $g) { [void]$geradores.Add($o); break }
            }
        }
        if ($geradores.Count -eq 0) { $geradores = $donos }
        $nomes = @($geradores | ForEach-Object { $_.name } | Sort-Object)
        $noAr = @($geradores | Where-Object { $_.state -eq 'Running' } | ForEach-Object { $_.name } | Sort-Object)
        foreach ($bf in $brokerFiles) {
            $alvo = Join-Path $d $bf
            $i = Get-Item -LiteralPath $alvo -ErrorAction SilentlyContinue
            if ($i) {
                [void]$broker.Add([pscustomobject]@{
                    name = $i.Name; path = $i.FullName; size = [int64]$i.Length
                    mtime = $i.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
                    age_min = [math]::Round(((Get-Date) - $i.LastWriteTime).TotalMinutes, 0)
                    services = $nomes; running = $noAr
                })
            } else {
                [void]$broker.Add([pscustomobject]@{
                    name = $bf; path = $alvo; size = $null; mtime = $null; age_min = $null
                    services = $nomes; running = $noAr })
            }
        }
    }
}
$broker = @($broker)
# Commit charge: TotalVirtualMemorySize e o limite de commit (RAM + pagefile).
# E esse limite - nao a RAM livre - que estoura com ERROR_COMMITMENT_LIMIT
# (0x800705AF) e derruba a geracao do broker no meio.
$commitPct = if ($os.TotalVirtualMemorySize -gt 0) {
    [math]::Round(((([double]$os.TotalVirtualMemorySize - [double]$os.FreeVirtualMemory) / [double]$os.TotalVirtualMemorySize) * 100), 1)
} else { $null }
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
# JobRunner do RM: cada job roda num processo proprio. Relatorio sem filtro cresce
# devagar por horas e depois explode para dezenas de GB (13/10: 80 GB no .34),
# esgotando o commit do host inteiro. Mede-se a memoria PRIVADA - e ela que conta
# no commit - e so dos maiores: o host tem dezenas de JobRunners ociosos.
$jrName = __JR_NAME__
$jobrunners = $null
if ($jrName) {
    $jrAll = @(Get-Process -Name $jrName -ErrorAction SilentlyContinue)
    $jrTop = @($jrAll | Sort-Object PrivateMemorySize64 -Descending | Select-Object -First __JR_TOP__ | ForEach-Object {
        $ini = $null
        try { $ini = $_.StartTime.ToString('yyyy-MM-dd HH:mm:ss') } catch { }
        [pscustomobject]@{
            pid = $_.Id; private_mb = [math]::Round($_.PrivateMemorySize64 / 1MB, 0)
            ws_mb = [math]::Round($_.WorkingSet64 / 1MB, 0); start = $ini }
    })
    $jrSum = ($jrAll | Measure-Object -Property PrivateMemorySize64 -Sum).Sum
    $jobrunners = [pscustomobject]@{
        count = $jrAll.Count; total_mb = [math]::Round([double]$jrSum / 1MB, 0)
        top = $jrTop; computer = $env:COMPUTERNAME }
}
# NLB + RDP (so nos nos declarados com `nlb: true`): estado do proprio no no
# cluster e quantos logons RDP autenticaram (1149) x viraram sessao (21/25)
# na janela. E a diferenca entre os dois que denuncia o no que aceita o
# usuario mas nao consegue abrir a sessao - coisa que o NLB nao enxerga.
$nlb = $null
if (__NLB__) {
    $nlb = [ordered]@{ status = $null; cluster = $null; host_id = $null; auth = $null; sess = $null
                       window_min = __RDP_WIN__; error = $null }
    try {
        $me = @(Get-CimInstance -Namespace root/MicrosoftNLB -ClassName MicrosoftNLB_Node -ErrorAction Stop |
            Where-Object { $_.ComputerName -eq $env:COMPUTERNAME -or $_.ComputerName -like "$($env:COMPUTERNAME).*" })
        if ($me.Count -gt 0) {
            $nlb.status = [int]$me[0].StatusCode
            $partes = "$($me[0].Name)".Split(':')
            $nlb.cluster = $partes[0]
            if ($partes.Count -gt 1) { $nlb.host_id = [int]$partes[1] }
        } else { $nlb.error = 'no local nao encontrado em MicrosoftNLB_Node' }
    } catch { $nlb.error = "$($_.Exception.Message)".Trim() }
    $rdpSince = (Get-Date).AddMinutes(-__RDP_WIN__)
    $nlb.auth = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational'; Id = 1149; StartTime = $rdpSince } -ErrorAction SilentlyContinue).Count
    $nlb.sess = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational'; Id = 21, 25; StartTime = $rdpSince } -ErrorAction SilentlyContinue).Count
    # Padronizacao entre os nos (scheduler.padrao_cluster): a camada de seguranca
    # do RDP decide se o 1149 e gravado - com SecurityLayer=0 (RDP nativo) o
    # Windows nao grava, e o no fica sem o sinal "autentica sem abrir sessao".
    $nlb.security_layer = $null; $nlb.nla = $null; $nlb.min_encryption = $null; $nlb.ultimo_1149_min = $null
    try {
        $rdpTcp = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -ErrorAction Stop
        $nlb.security_layer = $rdpTcp.SecurityLayer; $nlb.nla = $rdpTcp.UserAuthentication
        # Criptografia 1 (Baixa) faz o RDP so oferecer o modo nativo, sem TLS,
        # mesmo com SecurityLayer=1: era isso no .188 (16/10)
        $nlb.min_encryption = $rdpTcp.MinEncryptionLevel
    } catch { }
    $u1149 = Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-TerminalServices-RemoteConnectionManager/Operational'; Id = 1149 } -MaxEvents 1 -ErrorAction SilentlyContinue
    if ($u1149) { $nlb.ultimo_1149_min = [int]((Get-Date) - $u1149.TimeCreated).TotalMinutes }
    $nlb = [pscustomobject]$nlb
}
[pscustomobject]@{
    cpu = $cpu; mem_pct = $memPct; mem_total_mb = [math]::Round($memTotal / 1024, 0)
    uptime_sec = [math]::Round($uptime, 0); disks = $disks; services = $services; events = $events
    users_count = $ucount; broker = $broker; commit_pct = $commitPct; jobrunners = $jobrunners
    nlb = $nlb
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


# Arquivos de cache do broker verificados na pasta de instalacao do RM.
# `_BrokerCustom.dat` e o cache das customizacoes do cliente (pasta Custom);
# `_Broker.dat` e o do produto. Sao os dois que o RM manda apagar e recriar.
BROKER_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "files": ["_BrokerCustom.dat", "_Broker.dat"],
    # Servicos que geram o broker ao subir. Serve para nao cobrar o arquivo de
    # uma pasta onde so um servico auxiliar (que nao gera broker) esta no ar.
    "generators": ["RM.Host.Service*"],
}


def broker_settings(defaults: dict[str, Any] | None) -> dict[str, Any]:
    """Config efetiva da verificacao do broker (defaults.broker sobre os nossos)."""
    return {**BROKER_DEFAULTS, **((defaults or {}).get("broker") or {})}


# Medicao dos processos que executam os jobs do RM. `top` limita quantos vao no
# payload: o veredito so precisa dos maiores, e o host tem dezenas ociosos.
JOBRUNNER_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "process": "RM.Host.JobRunner",
    "top": 5,
}


def jobrunner_settings(defaults: dict[str, Any] | None) -> dict[str, Any]:
    """Config efetiva da medicao do JobRunner (defaults.jobrunner sobre os nossos)."""
    return {**JOBRUNNER_DEFAULTS, **((defaults or {}).get("jobrunner") or {})}


# Agentes de coleta vigiados em todo servidor que os tiver instalados. O RMon
# sobe sozinho o que estiver parado (scheduler.iniciar_automatico).
AGENT_PATTERNS_DEFAULT = ["Elastic Agent"]


def agent_patterns(defaults: dict[str, Any] | None) -> list[str]:
    """`defaults.agent_patterns` do inventario; sem a chave, os nossos ([] desliga)."""
    d = defaults or {}
    if "agent_patterns" in d:
        return [str(x) for x in (d.get("agent_patterns") or [])]
    return list(AGENT_PATTERNS_DEFAULT)


def _build_script(server: ServerConfig, defaults: dict[str, Any]) -> str:
    services = server.services or list(defaults.get("services") or [])
    patterns = server.service_patterns or list(defaults.get("service_patterns") or [])
    exclude = list(server.service_exclude or [])
    el = defaults.get("eventlog") or {}
    logs = list(el.get("logs") or ["System", "Application"])
    lookback = int(el.get("lookback_hours", 24))
    noise = el.get("noise_ids") or [10016, 1058, 1030, 1502, 1500, 7000, 7009]
    bk = broker_settings(defaults)
    broker_files = list(bk.get("files") or []) if bk.get("enabled", True) else []
    broker_gen = list(bk.get("generators") or [])
    jr = jobrunner_settings(defaults)
    jr_name = _ps_str(jr["process"]) if jr.get("enabled", True) and jr.get("process") else "$null"
    try:
        jr_top = max(1, int(jr.get("top") or 5))
    except (TypeError, ValueError):
        jr_top = 5
    prov_re = el.get("providers_regex") or (
        r"\b(RM|TOTVS|SGE|MSSQL|SQL|IIS|W3SVC|WAS|DBAccess|WER)"
        r"|\.NET Runtime|ASP\.NET|Application Error|Application Hang|Windows Error Reporting"
    )
    return (
        _PS_TEMPLATE
        .replace("__SERVICES__", ",".join(_ps_str(s) for s in services))
        .replace("__PATTERNS__", ",".join(_ps_str(p) for p in patterns))
        .replace("__EXCLUDE__", ",".join(_ps_str(x) for x in exclude))
        .replace("__AGENTS__", ",".join(_ps_str(a) for a in agent_patterns(defaults)))
        .replace("__LOGS__", ",".join(f"'{l}'" for l in logs))
        .replace("__LOOKBACK_H__", str(lookback))
        .replace("__NOISE_IDS__", ",".join(str(int(n)) for n in noise))
        .replace("__PROV_RE__", prov_re)
        .replace("__BROKER_FILES__", ",".join(_ps_str(f) for f in broker_files))
        .replace("__BROKER_GENERATORS__", ",".join(_ps_str(g) for g in broker_gen))
        .replace("__JR_NAME__", jr_name)
        .replace("__JR_TOP__", str(jr_top))
        .replace("__NLB__", "$true" if getattr(server, "nlb", False) else "$false")
        .replace("__RDP_WIN__", str(nlb_window(defaults)))
    )


def nlb_window(defaults: dict[str, Any] | None) -> int:
    """Janela (min) da contagem de eventos RDP - a mesma que o nlb.py julga."""
    try:
        return max(1, int(((defaults or {}).get("nlb") or {}).get("window_min", 15)))
    except (TypeError, ValueError):
        return 15


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
# ProgressPreference desligado: os registros de progresso saem como CLIXML no
# stderr, fazem o pywinrm reclamar de XML mal formado e, quando ha erro de
# verdade, ocupam o comeco da mensagem que aparece no painel.
_BOOTSTRAP = b64encode(
    ("$ProgressPreference = 'SilentlyContinue'; "
     "$s = [Console]::In.ReadToEnd(); Invoke-Expression $s").encode("utf_16_le")
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
# O quser devolve TEMPO OCIOSO e TEMPO DE LOGON grudados numa unica sobra de
# linha. Mandar essa sobra crua para a tela e o que produzia a coluna com "."
# e numeros soltos antes da data: o "." e o proprio quser dizendo "sem ocioso"
# e o numero e o ocioso em minutos. Aqui a linha e separada no host, e o ocioso
# vira minuto (idle_min) para a tela poder formatar e ordenar.
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
            # copiar antes de qualquer outro -match: $matches e sobrescrito
            $u = $matches[1]; $sess = $matches[2]; $sid = [int]$matches[3]
            $st = $matches[4]; $info = $matches[5].Trim()
            $idle = ''; $logon = $info
            if ($info -match '^(\S+)\s+(\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}.*)$') {
                $idle = $matches[1]; $logon = $matches[2].Trim()
            }
            $idleMin = $null
            if ($idle -eq '.' -or $idle -match '^(?i)(none|nenhum)') {
                $idleMin = 0
            } elseif ($idle -match '^(\d+)\+(\d+):(\d+)$') {
                $idleMin = ([int]$matches[1] * 1440) + ([int]$matches[2] * 60) + [int]$matches[3]
            } elseif ($idle -match '^(\d+):(\d+)$') {
                $idleMin = ([int]$matches[1] * 60) + [int]$matches[2]
            } elseif ($idle -match '^(\d+)$') {
                $idleMin = [int]$matches[1]
            }
            [void]$result.Add([pscustomobject]@{
                user = $u; session = $sess; id = $sid
                state = $st; info = $info; idle = $idle; idle_min = $idleMin; logon = $logon
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


# --- Encerrar um RM.Host.JobRunner que esta estourando a memoria ---
# O PID vem do formulario, entao o script confere tudo no host antes de matar:
# o processo existe, ainda e um JobRunner e e o MESMO que a coleta viu (hora de
# inicio igual) - PID reaproveitado por outro processo nao morre por engano.
_PS_KILL_JR = r"""
$p = Get-Process -Id __PID__ -ErrorAction SilentlyContinue
if (-not $p) { 'GONE' }
elseif ($p.ProcessName -ne __NAME__) { 'NOT_JR ' + $p.ProcessName }
else {
    $ini = $null
    try { $ini = $p.StartTime.ToString('yyyy-MM-dd HH:mm:ss') } catch { }
    if (__START__ -and $ini -ne __START__) { 'REUSED ' + $ini }
    else {
        $mb = [math]::Round($p.PrivateMemorySize64 / 1MB, 0)
        Stop-Process -Id __PID__ -Force -ErrorAction Stop
        'OK ' + $mb
    }
}
"""
_INICIO_RE = _re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def jobrunner_alvo(coleta: dict | None, pid) -> dict | None:
    """O processo que o painel pode encerrar: um dos JobRunners da ultima coleta.

    Fora dessa lista o pedido e recusado - nao existe "matar PID qualquer".
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    jr = (coleta or {}).get("jobrunners")
    top = jr.get("top") if isinstance(jr, dict) else None
    for proc in (top if isinstance(top, list) else [top] if isinstance(top, dict) else []):
        if isinstance(proc, dict) and proc.get("pid") is not None and int(proc["pid"]) == pid:
            return proc
    return None


def script_encerrar_jobrunner(pid: int, inicio: str | None, processo: str) -> str:
    pid = int(pid)
    if pid <= 0:
        raise ValueError("PID invalido")
    if not _re.fullmatch(r"[A-Za-z0-9._-]+", processo or ""):
        raise ValueError("nome de processo invalido")
    inicio = inicio if inicio and _INICIO_RE.fullmatch(str(inicio)) else ""
    return (_PS_KILL_JR.replace("__PID__", str(pid))
            .replace("__NAME__", _ps_str(processo)).replace("__START__", _ps_str(inicio)))


def interpretar_encerramento(saida: str, pid: int) -> tuple[bool, str]:
    linha = (saida or "").strip().splitlines()[-1:] or [""]
    cod, _, resto = linha[0].strip().partition(" ")
    if cod == "OK":
        return True, f"PID {pid} encerrado ({resto or '?'} MB de memoria privada liberados)"
    if cod == "GONE":
        return False, f"o PID {pid} ja tinha terminado: nada a fazer"
    if cod == "NOT_JR":
        return False, f"o PID {pid} agora e {resto or 'outro processo'}, nao um JobRunner: nada feito"
    if cod == "REUSED":
        return False, (f"o PID {pid} e de um processo iniciado em {resto or '?'}, nao o que a "
                       "coleta viu (PID reaproveitado): nada feito")
    return False, (saida or "sem resposta do host").strip()[:200]


def kill_jobrunner(server: ServerConfig, wc: WinRMConfig, pid: int, inicio: str | None,
                   processo: str) -> tuple[bool, str]:
    """Encerra um RM.Host.JobRunner pelo PID (Stop-Process -Force), com as conferencias acima."""
    try:
        script = script_encerrar_jobrunner(pid, inicio, processo)
    except (TypeError, ValueError) as exc:
        return False, str(exc)
    user, pw = credential_for(server.cred)
    if not user or not pw:
        return False, f"sem credencial (perfil '{server.cred}')"
    try:
        session = _winrm_session(server.host, wc, user, pw)
        r = _run_ps(session, script, _deadline(wc))
        if r.status_code != 0:
            return False, _decode(r.std_err)[:200] or "Stop-Process falhou"
        return interpretar_encerramento(_decode(r.std_out), int(pid))
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


# --- NLB: drenar / reingressar o proprio no ---
# Comandos FIXOS (nada vindo de formulario entra no script). nlb.exe atua no no
# local pelo driver, sem WMI remoto nem "controle remoto" do NLB - por isso roda
# pela mesma sessao WinRM da coleta. O estado antes/depois vem do WMI local.
_PS_NLB_TEMPLATE = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
function Get-NlbLocalStatus {
    $me = @(Get-CimInstance -Namespace root/MicrosoftNLB -ClassName MicrosoftNLB_Node -ErrorAction Stop |
        Where-Object { $_.ComputerName -eq $env:COMPUTERNAME -or $_.ComputerName -like "$($env:COMPUTERNAME).*" })
    if ($me.Count -gt 0) { [int]$me[0].StatusCode } else { $null }
}
$antes = Get-NlbLocalStatus
$out = @()
__ACTION__
Start-Sleep -Seconds 5
$depois = Get-NlbLocalStatus
[pscustomobject]@{ before = $antes; after = $depois; output = (($out -join ' ') -replace '\s+', ' ').Trim() } | ConvertTo-Json -Compress
"""
_PS_NLB_ACTION = {
    "drain": "$out += (& nlb.exe drainstop 2>&1 | Out-String)",
    # suspenso precisa de resume antes do start
    "rejoin": ("if ($antes -eq 1013) { $out += (& nlb.exe resume 2>&1 | Out-String) }\n"
               "$out += (& nlb.exe start 2>&1 | Out-String)"),
}
NLB_OK_AFTER = {"drain": {1005, 1009}, "rejoin": {1006, 1007, 1008}}


def nlb_action(server: ServerConfig, wc: WinRMConfig, action: str) -> tuple[bool, str]:
    """drainstop (action="drain") ou start (action="rejoin") do NLB no proprio host.

    Sucesso e o estado DEPOIS do comando, lido do WMI: drenando/parado para
    drain, convergindo/convergido para rejoin. A saida do nlb.exe vem junto so
    para a auditoria.
    """
    if action not in _PS_NLB_ACTION:
        return False, "acao invalida"
    user, pw = credential_for(server.cred)
    if not user or not pw:
        return False, f"sem credencial (perfil '{server.cred}')"
    script = _PS_NLB_TEMPLATE.replace("__ACTION__", _PS_NLB_ACTION[action])
    try:
        r = _run_ps_resilient(server.host, wc, user, pw, script, attempts=2, budget_sec=90)
        if r.status_code != 0:
            return False, _decode(r.std_err)[:300] or "WinRM status != 0"
        data = json.loads(_decode(r.std_out) or "{}")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:300]
    depois = data.get("after")
    ok = depois in NLB_OK_AFTER[action]
    return ok, f"estado {data.get('before')} -> {depois}; nlb.exe: {str(data.get('output') or '')[:200]}"


# Negociacao do RDP (X.224 Connection Request + RDP_NEG_REQ pedindo TLS): o que
# o servico de RDP do no aceita DE VERDADE, nao o que o registro diz. O .188
# ficou com SecurityLayer=1 desde 15/10 e continuou recusando TLS (nem o
# reboot resolveu: era a criptografia em 1, Baixa) - e o 1149 nao vinha.
_RDP_NEG_TLS = struct.pack(">BBH", 3, 0, 19) + bytes([14, 0xE0, 0, 0, 0, 0, 0]) + struct.pack("<BBHI", 1, 0, 8, 1)
# Cada teste e uma conexao no listener (evento 261 num log circular de 1 MB):
# basta de hora em hora, ou logo que o registro mudar.
RDP_TLS_TTL_S = 3600
_rdp_tls_cache: dict[str, tuple[float, Any, bool | None]] = {}


def rdp_aceita_tls(host: str, port: int = 3389, timeout: float = 5.0) -> bool | None:
    """True: o RDP negocia TLS; False: recusa (so RDP nativo); None: sem resposta."""
    try:
        with socket.create_connection((host, port), timeout) as c:
            c.settimeout(timeout)
            c.sendall(_RDP_NEG_TLS)
            r = c.recv(64)
    except OSError:
        return None
    # TPKT(4) + X.224 CC(7) + tipo do RDP_NEG: 2 = resposta, 3 = falha
    if len(r) < 19 or r[0] != 3:
        return None
    if r[11] == 2:
        return struct.unpack("<I", r[15:19])[0] in (1, 2, 8)  # TLS, CredSSP, CredSSP+EX
    if r[11] == 3:
        return False
    return None


def rdp_tls_em_vigor(host: str, registro: Any, agora: float | None = None) -> bool | None:
    """rdp_aceita_tls com cache por host (TTL), refeito na hora se o registro mudou."""
    agora = time.time() if agora is None else agora
    velho = _rdp_tls_cache.get(host)
    if velho and velho[1] == registro and agora - velho[0] < RDP_TLS_TTL_S:
        return velho[2]
    tls = rdp_aceita_tls(host)
    _rdp_tls_cache[host] = (agora, registro, tls)
    return tls


def collect_server(
    server: ServerConfig, wc: WinRMConfig, defaults: dict[str, Any]
) -> dict[str, Any]:
    """Coleta de um servidor. Nunca levanta excecao: encapsula erro no resultado."""
    result: dict[str, Any] = {
        "reachable": False, "cpu": None, "mem_pct": None, "uptime_sec": None,
        "disks": [], "services": [], "events": [], "app_ok": None, "app_ms": None,
        "broker": [], "commit_pct": None, "jobrunners": None, "error": None,
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
            "broker": _as_list(data.get("broker")),
            "commit_pct": data.get("commit_pct"),
            "nlb": data.get("nlb") if isinstance(data.get("nlb"), dict) else None,
        })
        jr = data.get("jobrunners")
        if isinstance(jr, dict):
            jr["top"] = _as_list(jr.get("top"))
            result["jobrunners"] = jr
        n = result.get("nlb")
        if n is not None and server.nlb:
            n["rdp_tls"] = rdp_tls_em_vigor(server.host, n.get("security_layer"))
    except Exception as exc:  # noqa: BLE001 - queremos registrar qualquer falha
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return result
