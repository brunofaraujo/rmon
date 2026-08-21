"""Inventario do software TOTVS instalado nos hosts Windows (via WinRM).

O alvo e o parque **TOTVS RM**: versao do produto, bibliotecas (`RM.Lib.*`),
customizacoes (`RM.Cst.*` e a pasta `Custom`) e executaveis. Programas de
terceiros - navegador, antivirus, runtime - ficam de fora de proposito: nao e
disso que o parque RM precisa dar noticia.

Fontes lidas numa unica ida ao host:

* **`rm`** - versao base do produto: a versao mais frequente entre os assemblies
  da TOTVS na pasta de instalacao. E o numero que responde "em que versao esse
  servidor esta". Quando sobram arquivos **abaixo** dela (residuo de uma
  atualizacao que nao trocou tudo), isso vira um item proprio.
* **`assembly`** - arquivos rastreados um a um (`watch`): bibliotecas
  `RM.Lib.*`, interfaces de customizacao e os executaveis do RM. Cada um tem seu
  proprio nivel de patch, aplicado pelo RM.Atualizador.
* **`custom`** - a pasta `Custom` da instalacao, onde ficam as customizacoes do
  cliente (`RM.Cst.*`). Existe em uns hosts e nao em outros, e isso importa.
* **`registry`** - chaves de desinstalacao (HKLM 64 e 32 bits), filtradas pela
  TOTVS. NAO usamos `Win32_Product`: aquela classe dispara a auto-reparacao do
  MSI em cada pacote, leva minutos e e conhecida por quebrar instalacao em
  producao.
* **`hotfix`** - `Get-HotFix`. Desligado por padrao: e Windows, nao TOTVS.

A coleta tem cadencia propria (horas), separada do ciclo de metricas.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .collector import _as_list, _decode, _ps_str, _run_ps_resilient
from .config import ServerConfig, WinRMConfig, credential_for

log = logging.getLogger("rmon.inventory")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "interval_hours": 6,
    # So software da TOTVS. O que identifica: fabricante ou nome do pacote.
    "only_totvs": True,
    "totvs_regex": "TOTVS|RM Sistemas|Microsiga|Corpore",
    # Arquivos da pasta de instalacao rastreados um a um (curinga sobre o nome).
    # A pasta e descoberta pelo caminho dos servicos casados por service_patterns.
    "watch": ["RM.Cst.*", "RM.Lib.*", "*Customizacao*", "*Customizada*", "RM*.exe"],
    # Pastas extras varridas alem das descobertas pelos servicos
    "paths": [],
    # Subpastas varridas por inteiro: e onde moram as customizacoes do cliente
    "custom_folders": ["Custom", "Scripts Especificos"],
    # Nas subpastas acima so interessam os arquivos da TOTVS/RM - o resto sao
    # dependencias de terceiros que a customizacao carrega junto
    "custom_prefix": "RM.*",
    # Windows, nao TOTVS: fora por padrao
    "hotfixes": False,
    # Ruido do registro, aplicado antes do filtro TOTVS
    "ignore": [
        "Update for Microsoft*", "Security Update for*", "Hotfix for*",
        "Definition Update for*",
    ],
}

# `Get-Nz` ("nao-zero"): string vazia vira $null, para nao gravar '' no banco.
# Fica fora do template so para nao repetir o mesmo if uma duzia de vezes.
_PS_HELPER = r"""
function Get-Nz($v) { $s = "$v".Trim(); if ($s) { return $s } else { return $null } }
"""

_PS_TEMPLATE = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

$items = New-Object System.Collections.ArrayList
$ignore = @(__IGNORE__)
$totvsRe = '__TOTVS_RE__'
$soTotvs = __SO_TOTVS__

function Add-Pkg($name, $version, $publisher, $date, $arch, $source, $detail, $dist) {
    if (-not $name) { return }
    [void]$items.Add([pscustomobject]@{
        name = "$name".Trim(); version = $version; publisher = $publisher
        install_date = $date; arch = $arch; source = $source; detail = $detail; dist = $dist
    })
}

# --- 1. Registro: chaves de desinstalacao (64 e 32 bits), so TOTVS ---
$paths = @(
    @{ p = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*';             a = 'x64' },
    @{ p = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'; a = 'x86' }
)
foreach ($entry in $paths) {
    foreach ($k in (Get-ItemProperty $entry.p -ErrorAction SilentlyContinue)) {
        $nome = "$($k.DisplayName)".Trim()
        if (-not $nome) { continue }
        # SystemComponent/Parent*: patches e componentes internos, nao "programas"
        if ($k.SystemComponent -eq 1) { continue }
        if ($k.ParentKeyName -or $k.ParentDisplayName) { continue }
        $pular = $false
        foreach ($ig in $ignore) { if ($nome -like $ig) { $pular = $true; break } }
        if ($pular) { continue }
        # Filtro TOTVS: casa pelo fabricante ou pelo nome do pacote
        if ($soTotvs -and -not ("$($k.Publisher)" -match $totvsRe -or $nome -match $totvsRe)) { continue }
        # InstallDate vem como yyyyMMdd (quando vem); muitos pacotes deixam vazio
        $dt = $null
        if ("$($k.InstallDate)" -match '^(\d{4})(\d{2})(\d{2})$') {
            $dt = "$($matches[1])-$($matches[2])-$($matches[3])"
        }
        Add-Pkg $nome (Get-Nz $k.DisplayVersion) (Get-Nz $k.Publisher) $dt $entry.a 'registry' (Get-Nz $k.InstallLocation) $null
    }
}

# --- 2. Pasta de instalacao do RM (descoberta pelo caminho dos servicos) ---
$svcPatterns = @(__PATTERNS__)
$watch = @(__WATCH__)
$subpastas = @(__CUSTOM_FOLDERS__)
$prefixoCustom = '__CUSTOM_PREFIX__'
$dirs = @{}
if ($svcPatterns.Count -gt 0) {
    foreach ($s in (Get-CimInstance Win32_Service -ErrorAction SilentlyContinue)) {
        $casa = $false
        foreach ($p in $svcPatterns) {
            if ($s.Name -like $p -or $s.DisplayName -like $p) { $casa = $true; break }
        }
        if (-not $casa) { continue }
        $cmd = "$($s.PathName)".Trim()
        if ($cmd -match '^\s*"([^"]+)"') { $exe = $matches[1] } else { $exe = ($cmd -split '\s+')[0] }
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { continue }
        # [IO.Path] em vez de Split-Path: no PowerShell 5.1, -LiteralPath com
        # -Parent/-Leaf cai em conjunto de parametros ambiguo e a chamada falha.
        $dirs[[System.IO.Path]::GetDirectoryName($exe)] = $true
    }
}
foreach ($extra in @(__PATHS__)) { if (Test-Path -LiteralPath $extra) { $dirs[$extra] = $true } }

foreach ($d in @($dirs.Keys)) {
    $arq = @(Get-ChildItem -LiteralPath $d -File | Where-Object { $_.Extension -eq '.dll' -or $_.Extension -eq '.exe' })
    if ($arq.Count -eq 0) { continue }

    # Versao base = a mais frequente entre os assemblies da TOTVS. Numa
    # instalacao do RM sao milhares de arquivos; a moda e o numero da versao do
    # produto, e o que diverge dela e patch aplicado (ou residuo antigo).
    $tv = @($arq | Where-Object { "$($_.VersionInfo.CompanyName)" -match $totvsRe })
    if ($tv.Count -gt 0) {
        $grupos = @($tv | Group-Object { "$($_.VersionInfo.FileVersion)" } | Sort-Object Count -Descending)
        $dist = @($grupos | ForEach-Object { "$($_.Name)=$($_.Count)" })
        Add-Pkg 'RM - versao base' $grupos[0].Name 'TOTVS' $null $null 'rm' $d $dist
    }

    foreach ($f in $arq) {
        foreach ($w in $watch) {
            if ($f.Name -like $w) {
                Add-Pkg $f.Name (Get-Nz $f.VersionInfo.FileVersion) (Get-Nz $f.VersionInfo.CompanyName) $f.LastWriteTime.ToString('yyyy-MM-dd') $null 'assembly' $f.FullName $null
                break
            }
        }
    }

    # Customizacoes do cliente: as subpastas configuradas, so os arquivos RM.*
    foreach ($sp in $subpastas) {
        $alvo = Join-Path $d $sp
        if (-not (Test-Path -LiteralPath $alvo)) { continue }
        foreach ($f in @(Get-ChildItem -LiteralPath $alvo -File -Recurse)) {
            if ($f.Name -notlike $prefixoCustom) { continue }
            Add-Pkg $f.Name (Get-Nz $f.VersionInfo.FileVersion) (Get-Nz $f.VersionInfo.CompanyName) $f.LastWriteTime.ToString('yyyy-MM-dd') $null 'custom' $f.FullName $null
        }
    }
}

# --- 3. Hotfixes do Windows (desligado por padrao) ---
if (__HOTFIXES__) {
    foreach ($h in (Get-HotFix -ErrorAction SilentlyContinue)) {
        $dt = $null
        if ($h.InstalledOn) { $dt = $h.InstalledOn.ToString('yyyy-MM-dd') }
        Add-Pkg $h.HotFixID $null 'Microsoft' $dt $null 'hotfix' (Get-Nz $h.Description) $null
    }
}

[pscustomobject]@{
    items = @($items)
    computer = $env:COMPUTERNAME
    os = (Get-CimInstance Win32_OperatingSystem).Caption
} | ConvertTo-Json -Depth 4 -Compress
"""

NOME_BASE = "RM - versao base"
NOME_RESIDUO = "RM - assemblies abaixo da versao base"


def settings_for(defaults: dict[str, Any] | None) -> dict[str, Any]:
    """Config efetiva do inventario (defaults.inventory do YAML sobre os nossos)."""
    return {**DEFAULTS, **((defaults or {}).get("inventory") or {})}


def fontes_ativas(defaults: dict[str, Any] | None) -> set[str]:
    """Fontes que a coleta ainda produz com a configuracao atual."""
    ativas = {"rm", "assembly", "custom", "registry"}
    if settings_for(defaults).get("hotfixes"):
        ativas.add("hotfix")
    return ativas


def _lista(cfg: dict[str, Any], chave: str) -> list[str]:
    return [str(x) for x in (cfg.get(chave) or [])]


def _build_script(server: ServerConfig, defaults: dict[str, Any]) -> str:
    cfg = settings_for(defaults)
    patterns = server.service_patterns or list((defaults or {}).get("service_patterns") or [])
    return (
        _PS_HELPER
        + _PS_TEMPLATE
        .replace("__PATTERNS__", ",".join(_ps_str(p) for p in patterns))
        .replace("__WATCH__", ",".join(_ps_str(w) for w in _lista(cfg, "watch")))
        .replace("__PATHS__", ",".join(_ps_str(p) for p in _lista(cfg, "paths")))
        .replace("__CUSTOM_FOLDERS__", ",".join(_ps_str(c) for c in _lista(cfg, "custom_folders")))
        .replace("__CUSTOM_PREFIX__", str(cfg.get("custom_prefix") or "RM.*"))
        .replace("__IGNORE__", ",".join(_ps_str(i) for i in _lista(cfg, "ignore")))
        .replace("__TOTVS_RE__", str(cfg.get("totvs_regex") or "TOTVS"))
        .replace("__SO_TOTVS__", "$true" if cfg.get("only_totvs", True) else "$false")
        .replace("__HOTFIXES__", "$true" if cfg.get("hotfixes") else "$false")
    )


# FileVersion do Windows costuma vir com um sufixo entre parenteses
# ("10.0.26100.8875 (WinBuild.160101.0800)"). Ele nao identifica versao nenhuma
# e, se um host o traz e outro nao, a comparacao acusaria diferenca inexistente.
_SUFIXO_VERSAO = re.compile(r"\s*\([^)]*\)\s*$")


def clean_version(version: str | None) -> str | None:
    if not version:
        return None
    limpa = _SUFIXO_VERSAO.sub("", version).strip()
    return limpa or None


# Muitos pacotes carregam a versao no proprio nome ("... Redistributable - 14.34.31931").
# Sem tirar isso, cada atualizacao viraria "removido + instalado" em vez de
# "atualizado", e a comparacao entre hosts nunca casaria as linhas.
_VERSAO_NO_NOME = re.compile(r"\s+-\s+\d+(?:\.\d+)+\s*$")


def display_name(name: str) -> str:
    """Nome sem a versao embutida.

    Os pacotes de customizacao da TOTVS se chamam, por exemplo,
    "TOTVS_CES_RM_Office365_CNI - 12.1.2602.002": com a versao dentro do nome,
    a mesma customizacao apareceria com rotulo diferente em cada host e a linha
    da matriz mudaria de nome a cada atualizacao. A versao ja tem coluna."""
    return re.sub(r"\s+", " ", _VERSAO_NO_NOME.sub("", (name or "").strip())).strip()


def normalize_name(name: str) -> str:
    return display_name(name).lower()


_PREFIXO_CHAVE = {"hotfix": "kb", "rm": "rm", "assembly": "asm", "custom": "cst"}


def package_key(item: dict[str, Any]) -> str:
    """Identidade estavel de um pacote: casa o mesmo software entre hosts e
    entre coletas."""
    source = item.get("source") or "registry"
    nome = normalize_name(item.get("name") or "")
    prefixo = _PREFIXO_CHAVE.get(source)
    if prefixo:
        return f"{prefixo}:{nome}"
    return f"reg:{nome}:{(item.get('arch') or '').lower()}"


def version_tuple(version: str | None) -> tuple:
    """Versao comparavel. Vazia quando nao ha numero nenhum na string."""
    if not version:
        return ()
    partes = re.findall(r"\d+", version)
    return tuple(int(p) for p in partes[:6])


def compare_versions(a: str | None, b: str | None) -> int:
    """-1 se a < b, 0 se iguais, 1 se a > b. Sem numeros, compara texto."""
    ta, tb = version_tuple(a), version_tuple(b)
    if ta and tb:
        # completa com zeros: '1.2' e '1.2.0.0' sao a mesma versao
        n = max(len(ta), len(tb))
        ta = ta + (0,) * (n - len(ta))
        tb = tb + (0,) * (n - len(tb))
        return (ta > tb) - (ta < tb)
    sa, sb = (a or "").strip(), (b or "").strip()
    return (sa > sb) - (sa < sb)


def _txt(value: Any, limite: int) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s[:limite] if s else None


def _parse_dist(dist: Any) -> list[tuple[str, int]]:
    """['12.1.2602.1=2920', ...] -> [('12.1.2602.1', 2920), ...]."""
    saida: list[tuple[str, int]] = []
    for entrada in _as_list(dist):
        versao, _, quantidade = str(entrada).rpartition("=")
        if not versao or not quantidade.isdigit():
            continue
        saida.append((clean_version(versao) or versao, int(quantidade)))
    return saida


def _mesma_linha(a: str | None, b: str | None) -> bool:
    """As duas versoes pertencem a mesma linha de release (mesmos dois primeiros
    componentes, ex.: 12.1)?

    A pasta do RM mistura assemblies da TOTVS que seguem a versao do produto
    (12.1.xxxx) com bibliotecas que a TOTVS assina mas versiona por conta
    propria (1.0.0.0, 6.0.290.0). Sem separar as duas coisas, "assembly atras
    da versao base" apontaria sempre para um 1.0.0.0 que nunca fez parte da
    linha do produto.
    """
    ta, tb = version_tuple(a)[:2], version_tuple(b)[:2]
    return bool(ta) and ta == tb


def _resumo_instalacao(base: str | None,
                       dist: list[tuple[str, int]]) -> tuple[str, dict | None]:
    """Descreve a instalacao e, se houver, destaca o residuo antigo.

    Numa instalacao do RM a maioria dos assemblies fica na versao do produto e
    um punhado sobe de patch pelo RM.Atualizador - isso e normal. O que chama
    atencao e o contrario: arquivo da mesma linha de release que ficou **para
    tras** da base, resto de uma atualizacao que nao trocou tudo. Esse caso vira
    um item proprio, comparavel entre hosts.
    """
    total = sum(q for _, q in dist)
    na_base = sum(q for v, q in dist if compare_versions(v, base) == 0)
    linha = [(v, q) for v, q in dist if _mesma_linha(v, base)]
    acima = sum(q for v, q in linha if compare_versions(v, base) > 0)
    atrasados = [(v, q) for v, q in linha if compare_versions(v, base) < 0]
    proprios = total - sum(q for _, q in linha)
    resumo = (f"{total} assemblies TOTVS: {na_base} na versao base, "
              f"{acima} em patch mais novo, {sum(q for _, q in atrasados)} atras da base, "
              f"{proprios} com versionamento proprio")
    if not atrasados:
        return resumo, None
    atrasados.sort(key=lambda x: version_tuple(x[0]))
    mais_antiga = atrasados[0][0]
    quantos = sum(q for _, q in atrasados)
    residuo = {
        "name": NOME_RESIDUO, "version": mais_antiga, "publisher": "TOTVS",
        "install_date": None, "arch": None, "source": "rm",
        "detail": (f"{quantos} arquivo(s) em {len(atrasados)} versao(oes) anteriores "
                   f"a base (mais antiga: {mais_antiga})"),
    }
    return resumo, residuo


def _clean(items: list[dict]) -> list[dict]:
    """Normaliza e deduplica os itens de um host.

    Dois servicos RM.Host em pastas diferentes podem apontar para instalacoes de
    versoes diferentes; a chave e a mesma, entao fica a maior versao - que e a
    que interessa saber se ja chegou naquele servidor.
    """
    por_chave: dict[str, dict] = {}

    def guardar(item: dict) -> None:
        chave = package_key(item)
        item["pkg_key"] = chave
        anterior = por_chave.get(chave)
        if anterior is None or compare_versions(item["version"], anterior["version"]) > 0:
            por_chave[chave] = item

    for raw in items:
        if not isinstance(raw, dict):
            continue
        nome = _txt(raw.get("name"), 250)
        if not nome:
            continue
        item = {
            "name": display_name(nome),
            "version": clean_version(_txt(raw.get("version"), 80)),
            "publisher": _txt(raw.get("publisher"), 120),
            "install_date": _txt(raw.get("install_date"), 10),
            "arch": _txt(raw.get("arch"), 8),
            "source": _txt(raw.get("source"), 16) or "registry",
            "detail": _txt(raw.get("detail"), 400),
        }
        if item["source"] == "rm" and raw.get("dist"):
            resumo, residuo = _resumo_instalacao(item["version"], _parse_dist(raw["dist"]))
            item["detail"] = f"{item['detail']} - {resumo}" if item["detail"] else resumo
            if residuo:
                guardar(residuo)
        guardar(item)

    return sorted(por_chave.values(), key=lambda i: (i["source"], i["name"].lower()))


def collect_inventory(server: ServerConfig, wc: WinRMConfig,
                      defaults: dict[str, Any]) -> dict[str, Any]:
    """Coleta o inventario de um host. Nunca levanta excecao."""
    out: dict[str, Any] = {"ok": False, "items": [], "error": None, "computer": None,
                           "os": None, "ms": 0}
    user, pw = credential_for(server.cred)
    if not (user and pw):
        out["error"] = f"sem credencial para o perfil '{server.cred}'"
        return out
    # cronometrado aqui dentro: quem chama roda em pool, e la fora o relogio
    # mediria o tempo na fila, nao o tempo do host.
    inicio = time.monotonic()
    try:
        script = _build_script(server, defaults)
        # Orcamento generoso: a pasta do RM tem milhares de assemblies e ler o
        # FileVersion de cada um passa bem dos 30s do ciclo de metricas.
        r = _run_ps_resilient(server.host, wc, user, pw, script, attempts=2, budget_sec=300)
        if r.status_code != 0:
            out["error"] = _decode(r.std_err)[:500] or "WinRM status != 0"
            return out
        data = json.loads(_decode(r.std_out))
        out.update(ok=True, items=_clean(_as_list(data.get("items"))),
                   computer=data.get("computer"), os=data.get("os"))
    except Exception as exc:  # noqa: BLE001 - coleta nunca derruba o ciclo
        out["error"] = f"{type(exc).__name__}: {exc}"[:500]
    out["ms"] = int((time.monotonic() - inicio) * 1000)
    return out


# ---------- diferenca entre coletas ----------
def diff_packages(atuais: dict[str, dict], novos: list[dict],
                  ativas: set[str] | None = None) -> list[dict]:
    """Eventos entre o estado gravado e o que o host acabou de reportar.

    O `install_date` do registro vem vazio na maior parte dos pacotes, e quando
    vem nao tem hora. Sao estes eventos - e nao o registro - que dao a linha do
    tempo confiavel de "o que mudou neste servidor".

    `ativas` sao as fontes que a coleta ainda produz. Desligar uma fonte na
    configuracao faz sumir tudo o que veio dela, e isso nao e desinstalacao:
    sem essa ressalva, tirar os hotfixes do inventario geraria uma enxurrada de
    eventos "removido" que nunca aconteceram no servidor.
    """
    eventos: list[dict] = []
    vistos: set[str] = set()
    for item in novos:
        chave = item["pkg_key"]
        vistos.add(chave)
        antigo = atuais.get(chave)
        if antigo is None:
            eventos.append({"pkg_key": chave, "name": item["name"], "kind": "installed",
                            "old_version": None, "new_version": item.get("version"),
                            "source": item.get("source")})
            continue
        cmp = compare_versions(item.get("version"), antigo.get("version"))
        if cmp > 0:
            kind = "upgraded"
        elif cmp < 0:
            kind = "downgraded"
        else:
            continue
        eventos.append({"pkg_key": chave, "name": item["name"], "kind": kind,
                        "old_version": antigo.get("version"),
                        "new_version": item.get("version"), "source": item.get("source")})
    for chave, antigo in atuais.items():
        if chave in vistos:
            continue
        if ativas is not None and (antigo.get("source") or "") not in ativas:
            continue
        eventos.append({"pkg_key": chave, "name": antigo["name"], "kind": "removed",
                        "old_version": antigo.get("version"), "new_version": None,
                        "source": antigo.get("source")})
    return eventos


EVENT_LABEL = {"installed": "instalado", "upgraded": "atualizado",
               "downgraded": "REGREDIU", "removed": "removido"}

FONTE_LABEL = {"rm": "produto", "assembly": "biblioteca", "custom": "customizacao",
               "registry": "instalador", "hotfix": "KB"}


# ---------- comparacao entre hosts ----------
FONTES = {
    "totvs": ("rm", "assembly", "custom"),
    "custom": ("custom",),
    "assembly": ("assembly",),
    "rm": ("rm",),
    "registry": ("registry",),
    "hotfix": ("hotfix",),
    "todos": ("rm", "assembly", "custom", "registry", "hotfix"),
}


def build_matrix(rows: list[dict], servers: list[str], *, fonte: str = "totvs",
                 busca: str = "", filtro: str = "todos",
                 servidor: str = "") -> list[dict]:
    """Matriz pacote x host.

    A referencia de cada pacote e a **maior versao encontrada no parque**: sem
    catalogo publico do TOTVS RM, "esta atualizado" so pode significar "esta no
    mesmo nivel do host mais novo".
    """
    fontes = FONTES.get(fonte, FONTES["totvs"])
    busca_l = (busca or "").strip().lower()
    por_pacote: dict[str, dict] = {}

    for r in rows:
        if r["source"] not in fontes:
            continue
        if busca_l and busca_l not in (r["name"] or "").lower() \
                and busca_l not in (r["version"] or "").lower():
            continue
        p = por_pacote.get(r["pkg_key"])
        if p is None:
            p = por_pacote[r["pkg_key"]] = {
                "key": r["pkg_key"], "name": r["name"], "source": r["source"],
                "publisher": r["publisher"], "cells": {}, "latest": None,
            }
        p["cells"][r["server"]] = {
            "version": r["version"],
            "date": r["install_date"] or (r["first_seen"].date() if r.get("first_seen") else None),
            "estimada": not r["install_date"],
            "detail": r["detail"],
        }
        if compare_versions(r["version"], p["latest"]) > 0:
            p["latest"] = r["version"]

    saida: list[dict] = []
    total_hosts = len(servers)
    for p in por_pacote.values():
        if servidor and servidor not in p["cells"]:
            continue
        versoes = {c["version"] for c in p["cells"].values()}
        p["hosts"] = len(p["cells"])
        p["ausentes"] = [s for s in servers if s not in p["cells"]]
        # divergencia de versao so faz sentido entre quem tem o pacote
        p["drift"] = len(versoes) > 1
        p["parcial"] = bool(p["ausentes"]) and p["hosts"] < total_hosts
        for cell in p["cells"].values():
            cell["status"] = ("ok" if not p["latest"] or
                              compare_versions(cell["version"], p["latest"]) == 0 else "behind")
        if filtro == "drift" and not p["drift"]:
            continue
        if filtro == "ausentes" and not p["parcial"]:
            continue
        if filtro == "problemas" and not (p["drift"] or p["parcial"]):
            continue
        saida.append(p)

    # produto primeiro, depois customizacoes, e dentro de cada grupo o que
    # diverge sobe: e a leitura que se quer fazer ao abrir a tela.
    ordem_fonte = {"rm": 0, "custom": 1, "assembly": 2, "registry": 3, "hotfix": 4}
    saida.sort(key=lambda p: (ordem_fonte.get(p["source"], 9), not p["drift"],
                              not p["parcial"], (p["name"] or "").lower()))
    return saida
