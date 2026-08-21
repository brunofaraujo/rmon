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
            log.warning("acao de execucao ignorada: id invalido (%r)", raw.get("id"))
            continue
        if raw.get("dest"):
            # Copiar DLL para dentro da instalacao nao passa pelo registro do
            # Windows: some do "Programas e Recursos" e nao ha como desinstalar
            # nem saber que versao esta la. Se o fornecedor so entrega o
            # arquivo, empacote antes - nao e o RMon que vai contornar isso.
            log.warning("acao %s ignorada: copia de arquivo nao e reversivel pelo "
                        "Painel de Controle; use um instalador (.exe/.msi)", ident)
            continue
        args = str(raw.get("args") or "")
        # O executavel e sempre o pacote que nos mesmos colocamos la; os
        # argumentos sao texto fixo do YAML. Encadear outro comando nao passa.
        if any(c in args for c in ";&|<>`$\n\r"):
            log.warning("acao %s ignorada: caracteres proibidos em 'args'", ident)
            continue
        extensoes = [str(e).lower() for e in (raw.get("extensions") or [])]
        invalidas = [e for e in extensoes if e not in EXTENSOES_INSTALADOR]
        if invalidas:
            log.warning("acao %s ignorada: so instalador (%s), e nao %s", ident,
                        ", ".join(EXTENSOES_INSTALADOR), ", ".join(invalidas))
            continue
        saida[ident] = {
            "id": ident,
            "label": str(raw.get("label") or ident)[:120],
            "kind": "run",
            "args": args[:200],
            "extensions": extensoes or list(EXTENSOES_INSTALADOR),
            "timeout_sec": max(30, min(int(raw.get("timeout_sec", 900) or 900), 7200)),
        }
    return saida


def na_janela(janela: str, agora: time.struct_time | None = None) -> bool:
    """A hora atual esta dentro da janela de manutencao 'HH:MM-HH:MM'?"""
    if not janela:
        return True
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", janela)
    if not m:
        log.warning("janela de manutencao invalida (%r): tratando como sempre aberta", janela)
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
$url = '__URL__'

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
} | ConvertTo-Json -Depth 4 -Compress
"""


def _check(nome: str, ok: bool, texto: str, dura: bool | None = None) -> dict:
    return {"nome": nome, "ok": bool(ok),
            "dura": (nome in DURAS) if dura is None else bool(dura), "texto": texto}


def _remoto(server: ServerConfig, wc: WinRMConfig, destino: str, temp: str,
            servicos: list[str], url: str) -> dict[str, Any]:
    """Roda as checagens no host. Tudo somente-leitura."""
    user, pw = credential_for(server.cred)
    if not (user and pw):
        return {"erro": f"sem credencial para o perfil '{server.cred}'"}
    script = (_PS_CHECAGENS
              .replace("__DESTINO__", destino.replace("'", "''"))
              .replace("__TEMP__", temp.replace("'", "''"))
              .replace("__SERVICOS__", ",".join(_ps_str(s) for s in servicos))
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
                            f"acao '{task.get('action')}' nao existe no catalogo"))
    checagens.append(_check(
        "habilitado", bool(cfg.get("enabled")),
        "execucao real habilitada no inventario" if cfg.get("enabled")
        else "execucao real DESLIGADA (execution.enabled = false): so pre-voo"))
    liberado = task["server"] in (cfg.get("hosts") or [])
    checagens.append(_check(
        "host_liberado", liberado,
        f"{task['server']} esta na lista de hosts liberados" if liberado
        else f"{task['server']} NAO esta em execution.hosts"))
    dentro = na_janela(str(cfg.get("window") or ""))
    checagens.append(_check(
        "janela", dentro,
        "dentro da janela de manutencao" if dentro
        else f"fora da janela de manutencao ({cfg.get('window')})"))

    caminho = pacote_local(settings, task.get("arquivo") or "")
    if caminho:
        tamanho = caminho.stat().st_size
        checagens.append(_check("pacote", True,
                                f"{caminho.name} ({tamanho / 1048576:.1f} MB), sha256 "
                                f"{sha256(caminho)[:16]}..."))
    else:
        checagens.append(_check("pacote", False,
                                f"pacote nao encontrado no repositorio: {task.get('arquivo')}"))
        tamanho = 0

    if caminho:
        instalador = caminho.suffix.lower() in EXTENSOES_INSTALADOR
        checagens.append(_check(
            "instalador", instalador,
            f"{caminho.suffix} se registra no Windows e da para desinstalar depois"
            if instalador else
            f"{caminho.suffix} nao e instalador: so {', '.join(EXTENSOES_INSTALADOR)}, "
            "porque a instalacao precisa ser reversivel pelo Painel de Controle"))
        if acao and acao["extensions"] and caminho.suffix.lower() not in acao["extensions"]:
            checagens.append(_check("extensao", False,
                                    f"{caminho.suffix} nao e aceito por esta acao "
                                    f"(esperado: {', '.join(acao['extensions'])})"))

    if instalado and task.get("version"):
        cmp = compare_versions(task["version"], instalado)
        checagens.append(_check(
            "versao", cmp > 0,
            f"instalado {instalado} -> pacote {task['version']}" if cmp > 0
            else f"o host ja esta em {instalado}; o pacote e {task['version']}"))

    comando = ""
    dados: dict[str, Any] = {}
    if server is None:
        checagens.append(_check("host", False, "servidor nao esta no inventario"))
    else:
        destino = ""
        temp = str(cfg.get("temp_dir") or DEFAULTS["temp_dir"])
        dados = _remoto(server, wc, destino, temp,
                        list(cfg.get("require_services_stopped") or []), url_check)
        if dados.get("erro"):
            checagens.append(_check("host", False, f"host inacessivel: {dados['erro']}"))
        else:
            sessoes = int(dados.get("sessoes") or 0)
            limite = int(cfg.get("max_sessions", 0) or 0)
            checagens.append(_check(
                "sessoes", sessoes <= limite,
                f"{sessoes} sessao(oes) RDP ativa(s) (limite {limite})"))
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
                "nenhum servico exigido parado pendente" if not exigidos
                else (f"{len(parados)} servico(s) ainda rodando: "
                      + ", ".join(s["nome"] for s in parados[:5]) if parados
                      else "servicos exigidos estao parados")))
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
                    else f"o host NAO alcanca o RMon em {url_check} (HTTP {http}) - "
                         "sem isso o pacote nao chega la", dura=real))
            elif real:
                checagens.append(_check(
                    "alcance", False,
                    "execution.base_url nao configurada: o host nao teria de onde "
                    "baixar o pacote", dura=True))

        if acao and caminho:
            comando = comando_previsto(acao, temp, caminho.name)

    bloqueios = [c for c in checagens if c["dura"] and not c["ok"]]
    return {"checks": checagens, "comando": comando, "ok": not bloqueios,
            "bloqueios": [c["nome"] for c in bloqueios]}


def comando_previsto(acao: dict, temp: str, nome_arquivo: str) -> str:
    """O que rodaria no host, exatamente como rodaria."""
    origem = temp.rstrip(chr(92)) + chr(92) + nome_arquivo
    args = f" {acao['args']}" if acao["args"] else ""
    if nome_arquivo.lower().endswith(".msi"):
        return f"msiexec.exe /i '{origem}'{args}"
    return f"'{origem}'{args}"


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

__COMANDO__

    [void]$passos.Add('instalador encerrado')
} catch {
    $saida = "$saida`n$($_.Exception.Message)"
    if ($null -eq $codigo) { $codigo = -1 }
} finally {
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


def _args_ps(args: str) -> str:
    """Argumentos do YAML como arranjo do PowerShell."""
    partes = [p for p in (args or "").split() if p]
    if not partes:
        return "@()"
    return "@(" + ",".join(_ps_str(p) for p in partes) + ")"


def _script_execucao(acao: dict, temp: str, nome: str, url: str, sha: str) -> str:
    return (_PS_EXECUTA
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
        return {"ok": False, "error": "execucao real desligada (execution.enabled = false)"}
    if task["server"] not in (cfg.get("hosts") or []):
        return {"ok": False, "error": f"{task['server']} nao esta em execution.hosts"}
    acao = actions_for(defaults).get(task.get("action") or "")
    if not acao:
        return {"ok": False, "error": "acao desconhecida"}
    caminho = pacote_local(settings, task.get("arquivo") or "")
    if not caminho:
        return {"ok": False, "error": "pacote nao encontrado no repositorio"}
    if caminho.suffix.lower() not in EXTENSOES_INSTALADOR:
        return {"ok": False,
                "error": f"{caminho.suffix} nao e instalador: a instalacao precisa ser "
                         "reversivel pelo Painel de Controle"}
    user, pw = credential_for(server.cred)
    if not (user and pw):
        return {"ok": False, "error": f"sem credencial para o perfil '{server.cred}'"}

    temp = str(cfg.get("temp_dir") or DEFAULTS["temp_dir"])
    script = _script_execucao(acao, temp, caminho.name, url_stage, sha256(caminho))
    try:
        r = _run_ps_resilient(server.host, wc, user, pw, script, attempts=1,
                              budget_sec=acao["timeout_sec"])
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
                    "error": erro or "o instalador retornou codigo != 0"}
        if not registro:
            # Rodou sem erro mas nao deixou rastro em "Programas e Recursos":
            # nao da para desinstalar nem para saber que versao ficou. Pela
            # regra da casa, isso nao e uma instalacao bem-sucedida.
            return {"ok": False, "exit_code": codigo, "output": texto[:4000],
                    "error": "o instalador terminou com sucesso mas nada apareceu em "
                             "Programas e Recursos: o resultado nao seria reversivel "
                             "pelo Painel de Controle"}
        return {"ok": True, "exit_code": codigo, "output": texto[:4000], "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
