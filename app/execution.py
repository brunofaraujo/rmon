"""Execucao de instalacoes e atualizacoes nos hosts - desarmada por padrao.

A maioria dos hosts monitorados esta em producao. Este modulo foi escrito com
isso como premissa, nao como ressalva:

* **Trava mestra.** Sem `execution.enabled: true` no inventario, nada roda: o
  painel so faz o **pre-voo** (checagens somente-leitura e o comando exato que
  seria executado). Instalar de verdade exige, alem da trava, o host numa lista
  explicita - `hosts: []` (o padrao) nao libera ninguem.
* **Nada de comando livre.** O que roda sai de um catalogo de acoes declarado no
  YAML: o executavel e sempre o pacote que o RMon mesmo colocou no host, e os
  argumentos sao texto fixo da configuracao. Nada digitado na tela entra numa
  linha de comando.
* **Pre-voo obrigatorio.** Toda tarefa - inclusive a real - roda antes as
  checagens somente-leitura. Uma checagem dura reprovada bloqueia a tarefa; ela
  nao "tenta assim mesmo".
* **So instalador, e so o que se desinstala.** O RMon nao copia arquivo solto
  para dentro da instalacao do RM: o que ele roda e um `.exe`/`.msi` que se
  registra no Windows. Depois de executar, ele confere nas chaves de
  desinstalacao que o pacote apareceu la e guarda o `UninstallString`. Instalou
  e nao se registrou? A tarefa e dada como **falha**, porque o resultado nao
  seria reversivel pelo Painel de Controle.
* **Uma tarefa por host de cada vez**, e o resultado inteiro (comando, codigo de
  saida, saida) fica gravado e auditado.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from pathlib import Path
from typing import Any

from .collector import _as_list, _decode, _ps_str, _run_ps_resilient
from .config import ServerConfig, Settings, WinRMConfig, credential_for
from .inventory import compare_versions

log = logging.getLogger("rmon.execution")

DEFAULTS: dict[str, Any] = {
    # Trava mestra. Falso = so pre-voo, nunca instala.
    "enabled": False,
    # Lista explicita de hosts onde a execucao real e permitida. Vazia = nenhum.
    "hosts": [],
    # Janela de manutencao "HH:MM-HH:MM" (pode virar o dia). Vazia = qualquer hora.
    "window": "",
    # Recusa se houver mais sessoes RDP ativas que isto
    "max_sessions": 0,
    # Espaco livre minimo no destino, em GB
    "min_free_gb": 2,
    # Pasta usada para deixar o pacote no host antes de instalar
    "temp_dir": "C:\\totvs\\rmon-stage",
    # URL do RMon como o HOST o enxerga (ex.: http://10.10.10.110:8080). E por
    # aqui que o pacote viaja da VM para o servidor Windows.
    "base_url": "",
    # Acoes permitidas. Sem isto, nao ha o que executar.
    "actions": [],
}

# So instalador: e o que deixa rastro nas chaves de desinstalacao do Windows e,
# portanto, o que da para desfazer pelo Painel de Controle depois.
EXTENSOES_INSTALADOR = (".exe", ".msi")

# Um id de acao entra em nome de arquivo e em log: mantenha-o sem surpresas.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
# Padrao de nome de servico a parar antes do instalador ("RM.Host*")
_SVC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._* -]{0,79}$")
# Quanto esperar, depois de parar os servicos, os processos sairem de fato
STOP_WAIT_PADRAO = 180

# Checagens que, reprovadas, impedem a tarefa. As demais sao avisos.
DURAS = {"habilitado", "host_liberado", "acao", "pacote", "janela", "sessoes",
         "disco", "destino", "servicos", "instalador", "extensao"}


def settings_for(defaults: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULTS, **((defaults or {}).get("execution") or {})}


def actions_for(defaults: dict[str, Any] | None) -> dict[str, dict]:
    """Catalogo de acoes permitidas, ja validado.

    Uma acao malformada e descartada com aviso no log em vez de virar comando
    inesperado num servidor de producao.
    """
    saida: dict[str, dict] = {}
    for raw in settings_for(defaults).get("actions") or []:
        if not isinstance(raw, dict):
            continue
        ident = str(raw.get("id") or "").strip().lower()
        if not _ID_RE.match(ident):
            log.warning("ação de execução ignorada: id inválido (%r)", raw.get("id"))
            continue
        if raw.get("dest"):
            # Copiar DLL para dentro da instalacao nao passa pelo registro do
            # Windows: some do "Programas e Recursos" e nao ha como desinstalar
            # nem saber que versao esta la. Se o fornecedor so entrega o
            # arquivo, empacote antes - nao e o RMon que vai contornar isso.
            log.warning("ação %s ignorada: copia de arquivo não e reversível pelo "
                        "Painel de Controle; use um instalador (.exe/.msi)", ident)
            continue
        args = str(raw.get("args") or "")
        # O executavel e sempre o pacote que nos mesmos colocamos la; os
        # argumentos sao texto fixo do YAML. Encadear outro comando nao passa.
        if any(c in args for c in ";&|<>`$\n\r"):
            log.warning("ação %s ignorada: caracteres proibidos em 'args'", ident)
            continue
        extensoes = [str(e).lower() for e in (raw.get("extensions") or [])]
        invalidas = [e for e in extensoes if e not in EXTENSOES_INSTALADOR]
        if invalidas:
            log.warning("ação %s ignorada: só instalador (%s), e não %s", ident,
                        ", ".join(EXTENSOES_INSTALADOR), ", ".join(invalidas))
            continue
        # Servicos a parar antes do instalador. O instalador da TOTVS ate para o
        # RM.Host sozinho, mas nao espera os processos sairem: no .223 em
        # 17/11 a copia comecou 3 s depois de parar o Cleanner, achou a DLL de
        # customizacao em uso e, sem janela para "Repetir", abortou tudo.
        parar = [str(p).strip() for p in _as_list(raw.get("stop_services")) if str(p).strip()]
        ruins = [p for p in parar if not _SVC_RE.match(p)]
        if ruins:
            log.warning("ação %s ignorada: nome de serviço inválido em stop_services (%r)",
                        ident, ruins[0])
            continue
        saida[ident] = {
            "id": ident,
            "label": str(raw.get("label") or ident)[:120],
            "kind": "run",
            "args": args[:200],
            "extensions": extensoes or list(EXTENSOES_INSTALADOR),
            "timeout_sec": max(30, min(int(raw.get("timeout_sec", 900) or 900), 7200)),
            "stop_services": parar,
            "stop_wait_sec": max(10, min(int(raw.get("stop_wait_sec", STOP_WAIT_PADRAO)
                                             or STOP_WAIT_PADRAO), 900)),
        }
    return saida


def prazo_total(acao: dict) -> int:
    """Tempo da chamada inteira: instalador + parar/esperar + religar."""
    extra = (acao["stop_wait_sec"] + 180) if acao.get("stop_services") else 0
    return int(acao["timeout_sec"]) + extra


def na_janela(janela: str, agora: time.struct_time | None = None) -> bool:
    """A hora atual esta dentro da janela de manutencao 'HH:MM-HH:MM'?"""
    if not janela:
        return True
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", janela)
    if not m:
        log.warning("janela de manutenção invalida (%r): tratando como sempre aberta", janela)
        return True
    ini = int(m.group(1)) * 60 + int(m.group(2))
    fim = int(m.group(3)) * 60 + int(m.group(4))
    t = agora or time.localtime()
    atual = t.tm_hour * 60 + t.tm_min
    if ini <= fim:
        return ini <= atual <= fim
    return atual >= ini or atual <= fim  # janela que atravessa a meia-noite


def pacote_local(settings: Settings, arquivo: str) -> Path | None:
    """Resolve um arquivo dentro do repositorio de pacotes.

    Resolve e confere que o caminho continua **dentro** da pasta: um
    `../../etc/shadow` vindo do banco nao vira arquivo servido nem instalado.
    """
    if not arquivo:
        return None
    raiz = Path(settings.packages_dir).resolve()
    try:
        alvo = (raiz / arquivo).resolve()
        alvo.relative_to(raiz)
    except (OSError, ValueError):
        return None
    return alvo if alvo.is_file() else None


def sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as fh:
        for bloco in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


# ---------- token de download do pacote (staging) ----------
def stage_token(secret: str, task_id: int, expira: int) -> str:
    msg = f"{task_id}:{expira}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def stage_token_ok(secret: str, task_id: int, expira: int, assinatura: str) -> bool:
    if not secret or expira < int(time.time()):
        return False
    return hmac.compare_digest(stage_token(secret, task_id, expira), assinatura or "")


# ---------- pre-voo ----------
_PS_CHECAGENS = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$destino = '__DESTINO__'
$temp = '__TEMP__'
$svcPatterns = @(__SERVICOS__)
$pararPatterns = @(__PARAR__)
$url = '__URL__'
__PASTAS_FN__

$sessoes = 0
try { $qu = @(quser 2>$null); if ($qu.Count -gt 1) { $sessoes = $qu.Count - 1 } } catch { }

$drive = $null
$alvo = if ($destino) { $destino } else { $temp }
try { $drive = (Split-Path -Qualifier $alvo) } catch { }
$livreGb = $null
if ($drive) {
    $d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$drive'"
    if ($d) { $livreGb = [math]::Round($d.FreeSpace / 1GB, 1) }
}

$servicos = @()
foreach ($p in $svcPatterns) {
    foreach ($s in @(Get-CimInstance Win32_Service | Where-Object { $_.Name -like $p -or $_.DisplayName -like $p })) {
        $servicos += [pscustomobject]@{ nome = $s.Name; estado = $s.State }
    }
}

# O que a execucao pararia, e quantos processos hoje rodam das pastas deles
$parar = @()
$pastasParar = @()
if ($pararPatterns.Count) {
    $alvos = Get-ServicosParar $pararPatterns
    foreach ($s in $alvos) { $parar += [pscustomobject]@{ nome = $s.Name; estado = $s.State } }
    $pastasParar = @(Get-PastasServicos $alvos)
}
$procsParar = @(Get-ProcessosDe $pastasParar).Count

# O host consegue baixar o pacote do proprio RMon? Descobrir isso agora evita
# uma tarefa que falharia so na hora de instalar.
$baixa = $null
if ($url) {
    try {
        # GET, nao HEAD: o FastAPI nao registra HEAD para rota GET e devolve 405,
        # o que faria esta checagem acusar "host nao alcanca" com a rede boa.
        # O /healthz e minusculo, entao buscar o corpo nao custa nada.
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15
        $baixa = [int]$r.StatusCode
    } catch { $baixa = -1 }
}

[pscustomobject]@{
    sessoes = $sessoes; livre_gb = $livreGb; drive = $drive
    destino_existe = [bool]($destino -and (Test-Path -LiteralPath $destino))
    temp_pai_existe = [bool](Test-Path -LiteralPath (Split-Path -Parent $temp))
    servicos = @($servicos); http = $baixa
    parar = @($parar); pastas_parar = @($pastasParar); processos_parar = $procsParar
} | ConvertTo-Json -Depth 4 -Compress
"""

# Usado pelo pre-voo e pela execucao: quais servicos casam com stop_services,
# de que pasta rodam e que processos ainda estao vivos nessas pastas. A espera
# e por PASTA, nao por nome: o RM.Host.Service sai, mas os JobRunner e o
# Cleanner, que rodam da mesma RM.Net, podem seguir segurando as DLLs.
_PS_PASTAS = r"""
function Get-ServicosParar($padroes) {
    @(Get-CimInstance Win32_Service | Where-Object {
        $n = $_.Name; @($padroes | Where-Object { $n -like $_ }).Count -gt 0 })
}
function Get-PastasServicos($svcs) {
    $pastas = @()
    foreach ($s in $svcs) {
        $exe = "$($s.PathName)".Trim()
        if ($exe.StartsWith('"')) { $exe = $exe.Substring(1, $exe.IndexOf('"', 1) - 1) }
        else { $exe = ($exe -split ' ')[0] }
        $pasta = Split-Path -Parent $exe
        # servico hospedado no svchost nao tem pasta "dele": esperar o
        # System32 esvaziar seria esperar para sempre
        if (-not $pasta -or $pasta.StartsWith($env:windir, [StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($pastas -notcontains $pasta) { $pastas += $pasta }
    }
    return $pastas
}
function Get-ProcessosDe($pastas) {
    if (-not $pastas -or -not @($pastas).Count) { return @() }
    @(Get-CimInstance Win32_Process | Where-Object {
        $p = "$($_.ExecutablePath)"
        $p -and @($pastas | Where-Object {
            $p.StartsWith($_ + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0 })
}
"""


def _check(nome: str, ok: bool, texto: str, dura: bool | None = None) -> dict:
    return {"nome": nome, "ok": bool(ok),
            "dura": (nome in DURAS) if dura is None else bool(dura), "texto": texto}


def _remoto(server: ServerConfig, wc: WinRMConfig, destino: str, temp: str,
            servicos: list[str], url: str, parar: list[str] | None = None) -> dict[str, Any]:
    """Roda as checagens no host. Tudo somente-leitura."""
    user, pw = credential_for(server.cred)
    if not (user and pw):
        return {"erro": f"sem credencial para o perfil '{server.cred}'"}
    script = (_PS_CHECAGENS
              .replace("__PASTAS_FN__", _PS_PASTAS)
              .replace("__DESTINO__", destino.replace("'", "''"))
              .replace("__TEMP__", temp.replace("'", "''"))
              .replace("__SERVICOS__", ",".join(_ps_str(s) for s in servicos))
              .replace("__PARAR__", ",".join(_ps_str(s) for s in (parar or [])))
              .replace("__URL__", url.replace("'", "''")))
    try:
        r = _run_ps_resilient(server.host, wc, user, pw, script, attempts=2, budget_sec=120)
        if r.status_code != 0:
            return {"erro": _decode(r.std_err)[:300] or "WinRM status != 0"}
        import json
        return json.loads(_decode(r.std_out) or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"erro": f"{type(exc).__name__}: {exc}"[:300]}


def preflight(task: dict, server: ServerConfig | None, wc: WinRMConfig,
              defaults: dict[str, Any], settings: Settings,
              instalado: str | None = None, url_check: str = "") -> dict[str, Any]:
    """Checagens somente-leitura + o comando exato que seria executado.

    Roda igual para tarefa em seco e para tarefa real; a diferenca e so o que
    acontece depois.
    """
    cfg = settings_for(defaults)
    acoes = actions_for(defaults)
    checagens: list[dict] = []

    acao = acoes.get(task.get("action") or "")
    checagens.append(_check("acao", bool(acao),
                            acao["label"] if acao else
                            f"ação '{task.get('action')}' não existe no catálogo"))
    checagens.append(_check(
        "habilitado", bool(cfg.get("enabled")),
        "execução real habilitada no inventário" if cfg.get("enabled")
        else "execução real DESLIGADA (execution.enabled = false): só pré-voo"))
    liberado = task["server"] in (cfg.get("hosts") or [])
    checagens.append(_check(
        "host_liberado", liberado,
        f"{task['server']} esta na lista de hosts liberados" if liberado
        else f"{task['server']} NÃO esta em execution.hosts"))
    dentro = na_janela(str(cfg.get("window") or ""))
    checagens.append(_check(
        "janela", dentro,
        "dentro da janela de manutenção" if dentro
        else f"fora da janela de manutenção ({cfg.get('window')})"))

    caminho = pacote_local(settings, task.get("arquivo") or "")
    if caminho:
        tamanho = caminho.stat().st_size
        checagens.append(_check("pacote", True,
                                f"{caminho.name} ({tamanho / 1048576:.1f} MB), sha256 "
                                f"{sha256(caminho)[:16]}..."))
    else:
        checagens.append(_check("pacote", False,
                                f"pacote não encontrado no repositório: {task.get('arquivo')}"))
        tamanho = 0

    if caminho:
        instalador = caminho.suffix.lower() in EXTENSOES_INSTALADOR
        checagens.append(_check(
            "instalador", instalador,
            f"{caminho.suffix} se registra no Windows e da para desinstalar depois"
            if instalador else
            f"{caminho.suffix} não e instalador: só {', '.join(EXTENSOES_INSTALADOR)}, "
            "porque a instalação precisa ser reversível pelo Painel de Controle"))
        if acao and acao["extensions"] and caminho.suffix.lower() not in acao["extensions"]:
            checagens.append(_check("extensao", False,
                                    f"{caminho.suffix} não e aceito por esta ação "
                                    f"(esperado: {', '.join(acao['extensions'])})"))

    if instalado and task.get("version"):
        cmp = compare_versions(task["version"], instalado)
        checagens.append(_check(
            "versao", cmp > 0,
            f"instalado {instalado} -> pacote {task['version']}" if cmp > 0
            else f"o host já esta em {instalado}; o pacote e {task['version']}"))

    comando = ""
    dados: dict[str, Any] = {}
    if server is None:
        checagens.append(_check("host", False, "servidor não esta no inventário"))
    else:
        destino = ""
        temp = str(cfg.get("temp_dir") or DEFAULTS["temp_dir"])
        dados = _remoto(server, wc, destino, temp,
                        list(cfg.get("require_services_stopped") or []), url_check,
                        (acao or {}).get("stop_services") or [])
        if dados.get("erro"):
            checagens.append(_check("host", False, f"host inacessível: {dados['erro']}"))
        else:
            sessoes = int(dados.get("sessoes") or 0)
            limite = int(cfg.get("max_sessions", 0) or 0)
            checagens.append(_check(
                "sessoes", sessoes <= limite,
                f"{sessoes} sessão(ões) RDP ativa(s) (limite {limite})"))
            livre = dados.get("livre_gb")
            minimo = float(cfg.get("min_free_gb", 2) or 0)
            precisa = max(minimo, (tamanho / 1073741824) * 2)
            checagens.append(_check(
                "disco", livre is not None and float(livre) >= precisa,
                f"{livre} GB livres em {dados.get('drive')} (precisa de {precisa:.1f} GB)"))
            checagens.append(_check("destino", bool(dados.get("temp_pai_existe")),
                                    f"pasta de preparo: {temp}"))
            parados = [s for s in (dados.get("servicos") or [])
                       if str(s.get("estado")) != "Stopped"]
            exigidos = list(cfg.get("require_services_stopped") or [])
            checagens.append(_check(
                "servicos", not parados,
                "nenhum serviço exigido parado pendente" if not exigidos
                else (f"{len(parados)} serviço(s) ainda rodando: "
                      + ", ".join(s["nome"] for s in parados[:5]) if parados
                      else "serviços exigidos estão parados")))
            if acao and acao.get("stop_services"):
                checagens.append(checagem_parada(acao, dados))
            # O pacote viaja da VM para o host por HTTP, entao "o host alcanca o
            # RMon?" decide se a instalacao e possivel. Em seco isso e so um
            # aviso; para valer, e impedimento - melhor barrar aqui do que
            # falhar no meio da instalacao.
            real = task.get("mode") == "real"
            if url_check:
                http = dados.get("http")
                checagens.append(_check(
                    "alcance", http == 200,
                    f"o host alcanca o RMon em {url_check} (HTTP {http})" if http == 200
                    else f"o host NÃO alcanca o RMon em {url_check} (HTTP {http}) - "
                         "sem isso o pacote não chega la", dura=real))
            elif real:
                checagens.append(_check(
                    "alcance", False,
                    "execution.base_url não configurada: o host não teria de onde "
                    "baixar o pacote", dura=True))

        if acao and caminho:
            comando = comando_previsto(acao, temp, caminho.name)

    bloqueios = [c for c in checagens if c["dura"] and not c["ok"]]
    return {"checks": checagens, "comando": comando, "ok": not bloqueios,
            "bloqueios": [c["nome"] for c in bloqueios]}


def checagem_parada(acao: dict, dados: dict) -> dict:
    """O que a execucao vai parar no host, e quanto tera de esperar.

    Aviso, nao impedimento: padrao que nao casa com nada so quer dizer que nada
    sera parado - o instalador roda como rodaria sem a opcao.
    """
    alvos = [s for s in _as_list(dados.get("parar")) if isinstance(s, dict)]
    pastas = [str(p) for p in _as_list(dados.get("pastas_parar"))]
    procs = int(dados.get("processos_parar") or 0)
    padroes = ", ".join(acao["stop_services"])
    if not alvos:
        return _check("parada", False,
                      f"nenhum serviço casa com {padroes}: nada será parado antes do instalador",
                      dura=False)
    rodando = [s["nome"] for s in alvos if str(s.get("estado")) == "Running"]
    texto = (f"para {', '.join(rodando) or 'nada (já parados)'} e espera até "
             f"{acao['stop_wait_sec']} s não restar processo de {', '.join(pastas) or '?'} "
             f"(agora: {procs}); no fim, religa o que estava rodando")
    return _check("parada", True, texto, dura=False)


def comando_previsto(acao: dict, temp: str, nome_arquivo: str) -> str:
    """O que rodaria no host, exatamente como rodaria."""
    origem = temp.rstrip(chr(92)) + chr(92) + nome_arquivo
    args = f" {acao['args']}" if acao["args"] else ""
    if nome_arquivo.lower().endswith(".msi"):
        cmd = f"msiexec.exe /i '{origem}'{args}"
    else:
        cmd = f"'{origem}'{args}"
    if acao.get("stop_services"):
        return (f"parar {', '.join(acao['stop_services'])} e esperar os processos saírem "
                f"(até {acao['stop_wait_sec']} s) → {cmd} → religar o que estava rodando")
    return cmd


# ---------- execucao ----------
_PS_EXECUTA = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$temp = __TEMP__
$nome = __NOME__
$url = __URL__
$sha = __SHA__
$passos = New-Object System.Collections.ArrayList
$registro = New-Object System.Collections.ArrayList
$saida = ''
$codigo = $null
$destino = $null
$antes = $null
$parar = @(__PARAR__)
$espera = __ESPERA__
$religar = New-Object System.Collections.ArrayList
__PASTAS_FN__

# Foto das chaves de desinstalacao. Tirada antes e depois, ela responde a
# pergunta que importa: o que foi instalado aparece em "Programas e Recursos"
# e tem como ser desinstalado?
function Get-Instalados {
    $mapa = @{}
    foreach ($p in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
                     'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')) {
        foreach ($k in (Get-ItemProperty $p -ErrorAction SilentlyContinue)) {
            $n = "$($k.DisplayName)".Trim()
            if (-not $n) { continue }
            $mapa[$n] = [pscustomobject]@{
                v = "$($k.DisplayVersion)".Trim(); u = "$($k.UninstallString)".Trim() }
        }
    }
    return $mapa
}

try {
    $antes = Get-Instalados
    if (-not (Test-Path -LiteralPath $temp)) { [void](New-Item -ItemType Directory -Path $temp -Force) }
    $destino = Join-Path $temp $nome
    [void]$passos.Add("preparo: $temp")

    Invoke-WebRequest -Uri $url -OutFile $destino -UseBasicParsing -TimeoutSec 600
    [void]$passos.Add("baixado: $([math]::Round((Get-Item -LiteralPath $destino).Length / 1MB, 1)) MB")

    # Confere o pacote NO HOST antes de executar: download truncado ou trocado
    # no caminho nao vira instalacao.
    $hash = (Get-FileHash -LiteralPath $destino -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $sha) { throw "sha256 nao confere: esperado $sha, veio $hash" }
    [void]$passos.Add('sha256 conferido')

__PARADA__

__COMANDO__

    [void]$passos.Add('instalador encerrado')
} catch {
    $saida = "$saida`n$($_.Exception.Message)"
    if ($null -eq $codigo) { $codigo = -1 }
} finally {
    # Religa o que o RMon parou - deu certo ou nao. Instalacao abortada que
    # deixa o servidor sem o RM.Host e pior que a instalacao nao feita.
    if ($religar.Count) {
        foreach ($n in $religar) {
            try {
                $svc = Get-Service -Name $n
                if ($svc.Status -eq 'Stopped') { $svc.Start() }
            } catch { $saida = "$saida`nnão consegui religar ${n}: $($_.Exception.Message)" }
        }
        $limite = (Get-Date).AddSeconds(120)
        foreach ($n in $religar) {
            $resta = $limite - (Get-Date)
            if ($resta.TotalSeconds -gt 0) {
                try { (Get-Service -Name $n).WaitForStatus('Running', $resta) } catch { }
            }
        }
        [void]$passos.Add('religado: ' + (($religar | ForEach-Object {
            "$_ (" + (Get-Service -Name $_ -ErrorAction SilentlyContinue).Status + ")" }) -join ', '))
    }
    # Mesmo se deu erro: o que ficou registrado no Windows tem de ser dito.
    if ($antes) {
        $depois = Get-Instalados
        foreach ($n in $depois.Keys) {
            if (-not $antes.ContainsKey($n)) {
                [void]$registro.Add("instalado: $n $($depois[$n].v) | desinstalar com: $($depois[$n].u)")
            } elseif ($antes[$n].v -ne $depois[$n].v) {
                [void]$registro.Add("atualizado: $n $($antes[$n].v) -> $($depois[$n].v) | desinstalar com: $($depois[$n].u)")
            }
        }
    }
    if ($destino -and (Test-Path -LiteralPath $destino)) {
        Remove-Item -LiteralPath $destino -Force -ErrorAction SilentlyContinue
        [void]$passos.Add('pacote temporario removido')
    }
}
[pscustomobject]@{
    passos = @($passos); saida = "$saida"; codigo = $codigo; registro = @($registro)
} | ConvertTo-Json -Depth 3 -Compress
"""

# O unico trecho que age. O executavel e sempre o pacote que nos mesmos
# baixamos; os argumentos vem do YAML. Nao ha caminho em que texto digitado na
# tela vire comando.
_PS_RODA = r"""    $argumentos = __ARGS__
    if ([System.IO.Path]::GetExtension($destino).ToLower() -eq '.msi') {
        # .msi sempre pelo msiexec: e o que instala de fato e deixa o pacote
        # registrado para desinstalacao.
        $proc = Start-Process -FilePath 'msiexec.exe' -ArgumentList (@('/i', $destino) + $argumentos) -Wait -PassThru
    } elseif ($argumentos.Count -gt 0) {
        $proc = Start-Process -FilePath $destino -ArgumentList $argumentos -Wait -PassThru
    } else {
        # -ArgumentList vazio e erro de parametro no PowerShell; sem argumentos,
        # a chamada simplesmente nao leva o parametro.
        $proc = Start-Process -FilePath $destino -Wait -PassThru
    }
    $codigo = $proc.ExitCode
    $saida = 'instalador encerrado com codigo ' + $codigo"""


# Para os servicos e espera as pastas deles esvaziarem. So entra no script
# quando a acao declara stop_services. Vem DEPOIS do download e do sha256:
# nada e parado por causa de um pacote que nem chegou inteiro.
_PS_PARA = r"""    $alvos = Get-ServicosParar $parar
    $pastas = @(Get-PastasServicos $alvos)
    foreach ($s in $alvos) {
        if ($s.State -eq 'Running') {
            [void]$religar.Add($s.Name)
            (Get-Service -Name $s.Name).Stop()
        }
    }
    foreach ($n in $religar) {
        try { (Get-Service -Name $n).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60)) } catch { }
    }
    if ($religar.Count) { [void]$passos.Add('parado: ' + ($religar -join ', ')) }
    $inicio = Get-Date
    $limite = $inicio.AddSeconds($espera)
    do {
        $vivos = @(Get-ProcessosDe $pastas)
        if (-not $vivos.Count) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $limite)
    if ($vivos.Count) {
        $quem = ($vivos | Group-Object Name | ForEach-Object { "$($_.Name) x$($_.Count)" }) -join ', '
        throw "depois de $espera s ainda havia processo rodando de $($pastas -join ', '): $quem. O instalador NAO foi executado."
    }
    [void]$passos.Add('sem processo de ' + ($pastas -join ', ') + ' após ' +
                      [int]((Get-Date) - $inicio).TotalSeconds + ' s')"""


def _args_ps(args: str) -> str:
    """Argumentos do YAML como arranjo do PowerShell."""
    partes = [p for p in (args or "").split() if p]
    if not partes:
        return "@()"
    return "@(" + ",".join(_ps_str(p) for p in partes) + ")"


def _script_execucao(acao: dict, temp: str, nome: str, url: str, sha: str) -> str:
    parar = list(acao.get("stop_services") or [])
    return (_PS_EXECUTA
            .replace("__PASTAS_FN__", _PS_PASTAS)
            .replace("__PARADA__", _PS_PARA if parar else "")
            .replace("__PARAR__", ",".join(_ps_str(p) for p in parar))
            .replace("__ESPERA__", str(int(acao.get("stop_wait_sec") or STOP_WAIT_PADRAO)))
            .replace("__TEMP__", _ps_str(temp))
            .replace("__NOME__", _ps_str(nome))
            .replace("__URL__", _ps_str(url))
            .replace("__SHA__", _ps_str(sha))
            .replace("__COMANDO__", _PS_RODA.replace("__ARGS__", _args_ps(acao["args"]))))


def execute(task: dict, server: ServerConfig, wc: WinRMConfig,
            defaults: dict[str, Any], settings: Settings,
            url_stage: str) -> dict[str, Any]:
    """Executa a tarefa no host. So chega aqui quem passou pelo pre-voo.

    A trava mestra e conferida DE NOVO aqui: entre criar a tarefa e executa-la
    alguem pode ter desligado a execucao no inventario, e a ultima palavra e a
    configuracao no momento de agir.
    """
    cfg = settings_for(defaults)
    if not cfg.get("enabled"):
        return {"ok": False, "error": "execução real desligada (execution.enabled = false)"}
    if task["server"] not in (cfg.get("hosts") or []):
        return {"ok": False, "error": f"{task['server']} não esta em execution.hosts"}
    acao = actions_for(defaults).get(task.get("action") or "")
    if not acao:
        return {"ok": False, "error": "ação desconhecida"}
    caminho = pacote_local(settings, task.get("arquivo") or "")
    if not caminho:
        return {"ok": False, "error": "pacote não encontrado no repositório"}
    if caminho.suffix.lower() not in EXTENSOES_INSTALADOR:
        return {"ok": False,
                "error": f"{caminho.suffix} não e instalador: a instalação precisa ser "
                         "reversível pelo Painel de Controle"}
    user, pw = credential_for(server.cred)
    if not (user and pw):
        return {"ok": False, "error": f"sem credencial para o perfil '{server.cred}'"}

    temp = str(cfg.get("temp_dir") or DEFAULTS["temp_dir"])
    script = _script_execucao(acao, temp, caminho.name, url_stage, sha256(caminho))
    try:
        r = _run_ps_resilient(server.host, wc, user, pw, script, attempts=1,
                              budget_sec=prazo_total(acao))
        bruto = _decode(r.std_out).strip()
        import json
        dados = json.loads(bruto) if bruto else {}
        codigo = dados.get("codigo")
        passos = [str(x) for x in _as_list(dados.get("passos"))]
        registro = [str(x) for x in _as_list(dados.get("registro"))]
        texto = "\n".join(passos)
        if registro:
            texto += "\n" + "\n".join(registro)
        if dados.get("saida"):
            texto = f"{texto}\n{dados['saida']}".strip()
        erro = _decode(r.std_err)[:500] or None

        if codigo != 0:
            return {"ok": False, "exit_code": codigo, "output": texto[:4000],
                    "error": erro or "o instalador retornou código != 0"}
        if not registro:
            # Rodou sem erro mas nao deixou rastro em "Programas e Recursos":
            # nao da para desinstalar nem para saber que versao ficou. Pela
            # regra da casa, isso nao e uma instalacao bem-sucedida.
            return {"ok": False, "exit_code": codigo, "output": texto[:4000],
                    "error": "o instalador terminou com sucesso mas nada apareceu em "
                             "Programas e Recursos: o resultado não seria reversível "
                             "pelo Painel de Controle"}
        return {"ok": True, "exit_code": codigo, "output": texto[:4000], "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
