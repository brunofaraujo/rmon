"""Inventario de software instalado nos hosts Windows (via WinRM).

Le tres fontes em uma unica ida ao host:

* **registro** - chaves de desinstalacao (HKLM 64 e 32 bits). E a fonte
  canonica de "programas instalados". NAO usamos `Win32_Product`: aquela classe
  dispara a auto-reparacao do MSI em cada pacote, leva minutos e e conhecida
  por quebrar instalacao em producao.
* **binarios** - FileVersion dos executaveis dos servicos descobertos pelos
  `service_patterns` (RM.Host*). No TOTVS RM a versao que importa e a dos
  binarios: o update troca os arquivos e o registro continua na versao antiga.
* **hotfixes** - `Get-HotFix`, para comparar nivel de patch entre servidores.

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
    # Arquivos procurados na pasta de cada servico descoberto (alem do proprio exe)
    "binaries": ["RM.exe", "RM.Host.exe", "RM.Host.Service.exe"],
    "hotfixes": True,
    # Ruido do registro: pacote que nao diz nada sobre o estado do servidor
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

function Add-Pkg($name, $version, $publisher, $date, $arch, $source, $detail) {
    if (-not $name) { return }
    [void]$items.Add([pscustomobject]@{
        name = "$name".Trim(); version = $version; publisher = $publisher
        install_date = $date; arch = $arch; source = $source; detail = $detail
    })
}

# --- 1. Registro: chaves de desinstalacao (64 e 32 bits) ---
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
        # InstallDate vem como yyyyMMdd (quando vem); muitos pacotes deixam vazio
        $dt = $null
        if ("$($k.InstallDate)" -match '^(\d{4})(\d{2})(\d{2})$') {
            $dt = "$($matches[1])-$($matches[2])-$($matches[3])"
        }
        Add-Pkg $nome (Get-Nz $k.DisplayVersion) (Get-Nz $k.Publisher) $dt $entry.a 'registry' (Get-Nz $k.InstallLocation)
    }
}

# --- 2. Binarios dos servicos monitorados (a versao real do RM) ---
$svcPatterns = @(__PATTERNS__)
$extras = @(__BINARIES__)
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
        $vi = (Get-Item -LiteralPath $exe).VersionInfo
        Add-Pkg ([System.IO.Path]::GetFileName($exe)) (Get-Nz $vi.FileVersion) (Get-Nz $vi.CompanyName) $null $null 'binary' $exe
    }
}
foreach ($d in @($dirs.Keys)) {
    foreach ($nome in $extras) {
        $f = Join-Path $d $nome
        if (-not (Test-Path -LiteralPath $f)) { continue }
        $vi = (Get-Item -LiteralPath $f).VersionInfo
        Add-Pkg $nome (Get-Nz $vi.FileVersion) (Get-Nz $vi.CompanyName) $null $null 'binary' $f
    }
}

# --- 3. Hotfixes do Windows ---
if (__HOTFIXES__) {
    foreach ($h in (Get-HotFix -ErrorAction SilentlyContinue)) {
        $dt = $null
        if ($h.InstalledOn) { $dt = $h.InstalledOn.ToString('yyyy-MM-dd') }
        Add-Pkg $h.HotFixID $null 'Microsoft' $dt $null 'hotfix' (Get-Nz $h.Description)
    }
}

[pscustomobject]@{
    items = @($items)
    computer = $env:COMPUTERNAME
    os = (Get-CimInstance Win32_OperatingSystem).Caption
} | ConvertTo-Json -Depth 4 -Compress
"""


def settings_for(defaults: dict[str, Any] | None) -> dict[str, Any]:
    """Config efetiva do inventario (defaults.inventory do YAML sobre os nossos)."""
    return {**DEFAULTS, **((defaults or {}).get("inventory") or {})}


def _build_script(server: ServerConfig, defaults: dict[str, Any]) -> str:
    cfg = settings_for(defaults)
    patterns = server.service_patterns or list((defaults or {}).get("service_patterns") or [])
    return (
        _PS_HELPER
        + _PS_TEMPLATE
        .replace("__PATTERNS__", ",".join(_ps_str(p) for p in patterns))
        .replace("__BINARIES__", ",".join(_ps_str(b) for b in (cfg.get("binaries") or [])))
        .replace("__IGNORE__", ",".join(_ps_str(i) for i in (cfg.get("ignore") or [])))
        .replace("__HOTFIXES__", "$true" if cfg.get("hotfixes", True) else "$false")
    )


# Muitos pacotes carregam a versao no proprio nome ("... Redistributable - 14.34.31931").
# Sem tirar isso, cada atualizacao viraria "removido + instalado" em vez de
# "atualizado", e a comparacao entre hosts nunca casaria as linhas.
_VERSAO_NO_NOME = re.compile(r"\s+-\s+\d+(?:\.\d+)+\s*$")


# FileVersion do Windows costuma vir com um sufixo entre parenteses
# ("10.0.26100.8875 (WinBuild.160101.0800)"). Ele nao identifica versao nenhuma
# e, se um host o traz e outro nao, a comparacao acusaria diferenca inexistente.
_SUFIXO_VERSAO = re.compile(r"\s*\([^)]*\)\s*$")


def clean_version(version: str | None) -> str | None:
    if not version:
        return None
    limpa = _SUFIXO_VERSAO.sub("", version).strip()
    return limpa or None


def normalize_name(name: str) -> str:
    limpo = _VERSAO_NO_NOME.sub("", (name or "").strip())
    return re.sub(r"\s+", " ", limpo).strip().lower()


def package_key(item: dict[str, Any]) -> str:
    """Identidade estavel de um pacote: casa o mesmo software entre hosts e
    entre coletas."""
    source = item.get("source") or "registry"
    nome = normalize_name(item.get("name") or "")
    if source == "hotfix":
        return f"kb:{nome}"
    if source == "binary":
        return f"bin:{nome}"
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


def _clean(items: list[dict]) -> list[dict]:
    """Normaliza e deduplica os itens de um host.

    Dois servicos RM.Host em pastas diferentes podem apontar para binarios de
    versoes diferentes; a chave e a mesma, entao fica a maior versao - que e a
    que interessa saber se ja chegou naquele servidor.
    """
    por_chave: dict[str, dict] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        nome = _txt(raw.get("name"), 250)
        if not nome:
            continue
        item = {
            "name": nome,
            "version": clean_version(_txt(raw.get("version"), 80)),
            "publisher": _txt(raw.get("publisher"), 120),
            "install_date": _txt(raw.get("install_date"), 10),
            "arch": _txt(raw.get("arch"), 8),
            "source": _txt(raw.get("source"), 16) or "registry",
            "detail": _txt(raw.get("detail"), 400),
        }
        chave = package_key(item)
        item["pkg_key"] = chave
        anterior = por_chave.get(chave)
        if anterior is None or compare_versions(item["version"], anterior["version"]) > 0:
            por_chave[chave] = item
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
        # Orcamento generoso: varrer o registro e os hotfixes de um terminal
        # server sobrecarregado passa bem dos 30s do ciclo de metricas.
        r = _run_ps_resilient(server.host, wc, user, pw, script, attempts=2, budget_sec=180)
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
def diff_packages(atuais: dict[str, dict], novos: list[dict]) -> list[dict]:
    """Eventos entre o estado gravado e o que o host acabou de reportar.

    O `install_date` do registro vem vazio na maior parte dos pacotes, e quando
    vem nao tem hora. Sao estes eventos - e nao o registro - que dao a linha do
    tempo confiavel de "o que mudou neste servidor".
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
        eventos.append({"pkg_key": chave, "name": antigo["name"], "kind": "removed",
                        "old_version": antigo.get("version"), "new_version": None,
                        "source": antigo.get("source")})
    return eventos


EVENT_LABEL = {"installed": "instalado", "upgraded": "atualizado",
               "downgraded": "REGREDIU", "removed": "removido"}


# ---------- comparacao entre hosts ----------
FONTES = {
    "prog": ("registry", "binary"),
    "registry": ("registry",),
    "binary": ("binary",),
    "hotfix": ("hotfix",),
    "todos": ("registry", "binary", "hotfix"),
}


def build_matrix(rows: list[dict], servers: list[str], *, fonte: str = "prog",
                 busca: str = "", filtro: str = "todos",
                 servidor: str = "") -> list[dict]:
    """Matriz pacote x host.

    A referencia de cada pacote e a **maior versao encontrada no parque**: sem
    catalogo publico do TOTVS RM, "esta atualizado" so pode significar "esta no
    mesmo nivel do host mais novo".
    """
    fontes = FONTES.get(fonte, FONTES["prog"])
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
        for nome_srv, cell in p["cells"].items():
            cell["status"] = ("ok" if not p["latest"] or
                              compare_versions(cell["version"], p["latest"]) == 0 else "behind")
        if filtro == "drift" and not p["drift"]:
            continue
        if filtro == "ausentes" and not p["parcial"]:
            continue
        if filtro == "problemas" and not (p["drift"] or p["parcial"]):
            continue
        saida.append(p)

    saida.sort(key=lambda p: (not p["drift"], not p["parcial"], p["source"] != "binary",
                              (p["name"] or "").lower()))
    return saida
