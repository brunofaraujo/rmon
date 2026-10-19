#!/usr/bin/env python3
"""Teto de memoria do RM.Host.JobRunner nos servidores do RM, a partir da VM do RMon.

Leva o deploy/windows/ ao servidor pela mesma sessao WinRM da coleta (nada e
copiado a mao) e roda la o instalador ou o desinstalador. Ver docs/OPERACAO.md,
"Teto de memoria do JobRunner".

  checar [SERVIDOR ...]               so leitura: o componente em cada servidor
  instalar SERVIDOR ... [--modo registrar|aplicar] [--teto-mb N]
                                      instala ou atualiza (padrao: registrar, teto
                                      automatico = 6144 MB sem passar de 40% da RAM)
  modo SERVIDOR ... registrar|aplicar troca o modo sem reinstalar (vale em ~2 s)
  desinstalar SERVIDOR ...            tira o teto dos JobRunners e remove o servico

instalar, modo e desinstalar exigem o nome do servidor (a chave do servers.yaml,
ex. fin-sge-jobs-222): nao existe "todos". Cada acao vai para a auditoria.

Exemplos (na VM):
  sudo -u rmon /opt/rmon/.venv/bin/python /opt/rmon/deploy/limite_jobrunner_remoto.py checar
  sudo -u rmon /opt/rmon/.venv/bin/python /opt/rmon/deploy/limite_jobrunner_remoto.py instalar homolog-218
"""
import argparse
import base64
import html
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("RMON_HOME", "/opt/rmon"))  # RMON_HOME: testar uma copia
from app.config import credential_for, load_dotenv, load_inventory  # noqa: E402

load_dotenv("/opt/rmon/.env")
from app import collector, db, limite_jr  # noqa: E402

WINDOWS = Path(__file__).resolve().parent / "windows"

_PS_CHECAR = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
    "$ErrorActionPreference = 'Stop'\n"
    f"$allSvc = @(Get-CimInstance Win32_Service -Filter \"Name='{limite_jr.SERVICO}'\")\n"
    + collector._PS_LIMITE_JR.replace("__LJ_SVC__", collector._ps_str(limite_jr.SERVICO))
    + "\nConvertTo-Json -InputObject $limiteJr -Depth 5 -Compress\n"
)

_PS_MODO = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$k = 'HKLM:\SOFTWARE\RMon\LimiteJobRunner'
if (-not (Test-Path $k)) { 'NAO_INSTALADO'; exit 2 }
$antes = (Get-ItemProperty $k).Modo
Set-ItemProperty -Path $k -Name Modo -Value __MODO__ -Type String
$ms = [int](Get-ItemProperty $k).IntervaloMs
Start-Sleep -Milliseconds ([math]::Max(2000, $ms) + 3000)
$e = Get-Content -LiteralPath (Join-Path $env:ProgramData 'RMon\LimiteJobRunner\estado.json') -Raw -Encoding UTF8 | ConvertFrom-Json
[pscustomobject]@{ antes = $antes; depois = $e.modo; servico = "$((Get-Service RMonLimiteJobRunner).Status)"
                   processos = $e.processos; sob_teto = $e.sob_teto; teto_mb = $e.teto_mb } | ConvertTo-Json -Compress
"""


def _b64(texto: str) -> str:
    return base64.b64encode(texto.encode("utf-8")).decode("ascii")


def _embutido(arquivo: str, argumentos: str) -> str:
    """Roda um .ps1 de deploy/windows no host sem grava-lo: scriptblock nao passa
    pela ExecutionPolicy, e o texto chega em base64 (aspas e acentos intactos)."""
    texto = (WINDOWS / arquivo).read_text(encoding="utf-8")
    return ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            f"$s = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(texto)}'))\n"
            f"& ([scriptblock]::Create($s)) {argumentos}\n")


def script_instalar(modo: str, teto_mb: int) -> str:
    if modo not in ("registrar", "aplicar"):
        raise ValueError("modo inválido")
    teto_mb = int(teto_mb)
    if teto_mb != 0 and teto_mb < 256:
        raise ValueError("teto mínimo: 256 MB")
    fonte = (WINDOWS / "RMonLimiteJobRunner.cs").read_text(encoding="utf-8")
    return (f"$fonte = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(fonte)}'))\n"
            + _embutido("instalar-limite-jobrunner.ps1", f"-Modo {modo} -TetoMB {teto_mb} -FonteTexto $fonte"))


def script_desinstalar() -> str:
    return _embutido("desinstalar-limite-jobrunner.ps1", "")


def script_modo(modo: str) -> str:
    if modo not in ("registrar", "aplicar"):
        raise ValueError("modo inválido")
    return _PS_MODO.replace("__MODO__", collector._ps_str(modo))


def so_erros(err: str) -> str:
    """O stderr sem o eco do Write-Host. Pelo WinRM, o PowerShell repete cada
    Write-Host no stderr como registro de informacao em CLIXML (o pywinrm so
    limpa quando ha erro), e o texto do instalador saia duas vezes."""
    if not err.startswith("#< CLIXML"):
        return err
    erros = re.findall(r'<S S="Error">(.*?)</S>', err, re.S)
    return "".join(html.unescape(e).replace("_x000D__x000A_", "\n") for e in erros).strip()


def rodar(srv, inv, script: str, budget: int) -> tuple[int, str, str]:
    user, pw = credential_for(srv.cred)
    r = collector._run_ps_resilient(srv.host, inv.winrm, user, pw, script, attempts=1, budget_sec=budget)
    return r.status_code, collector._decode(r.std_out).strip(), so_erros(collector._decode(r.std_err).strip())


def escolher(inv, nomes: list[str], todos_se_vazio: bool) -> list:
    por_nome = {s.name: s for s in inv.servers}
    if not nomes:
        if todos_se_vazio:
            return list(inv.servers)
        sys.exit("diga o(s) servidor(es): " + ", ".join(por_nome))
    faltam = [n for n in nomes if n not in por_nome]
    if faltam:
        sys.exit(f"servidor desconhecido: {', '.join(faltam)} (há: {', '.join(por_nome)})")
    return [por_nome[n] for n in nomes]


def checar(inv, servidores) -> bool:
    ok = True
    for s in servidores:
        try:
            rc, out, err = rodar(s, inv, _PS_CHECAR, 60)
            lj = json.loads(out or "null") if rc == 0 else None
            if rc != 0:
                print(f"{s.name}: ERRO {err[:300]}")
                ok = False
                continue
        except Exception as exc:  # noqa: BLE001
            print(f"{s.name}: SEM CONTATO ({type(exc).__name__}: {exc})")
            ok = False
            continue
        r = limite_jr.resumo(lj)
        print(f"{s.name}: {r['texto']}")
        for t in r["detalhes"]:
            print(f"    {t}")
        for e in r["estouros"]:
            print(f"    passou do teto: {e['texto']}")
    return ok


def agir(inv, servidores, acao: str, script: str, budget: int, detalhe: str) -> bool:
    ok = True
    for s in servidores:
        print(f"== {s.name} ({s.host}): {acao} {detalhe}".rstrip())
        try:
            rc, out, err = rodar(s, inv, script, budget)
        except Exception as exc:  # noqa: BLE001
            rc, out, err = -1, "", f"{type(exc).__name__}: {exc}"
        if out:
            print(out)
        if err:
            print(err[:1500])
        print(f"   -> {'ok' if rc == 0 else f'FALHOU (código {rc})'}")
        db.audit("rmon-limite-jr", f"limite_jr_{acao}",
                 f"{s.name} {detalhe} -> {'ok' if rc == 0 else f'falhou rc={rc}'}: {(out or err)[-300:]}"[:900])
        ok = ok and rc == 0
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description="Teto de memória do RM.Host.JobRunner (Job Object)")
    sub = ap.add_subparsers(dest="acao", required=True)
    p = sub.add_parser("checar", help="só leitura")
    p.add_argument("servidores", nargs="*")
    p = sub.add_parser("instalar")
    p.add_argument("servidores", nargs="+")
    p.add_argument("--modo", choices=["registrar", "aplicar"], default="registrar")
    p.add_argument("--teto-mb", type=int, default=0, help="0 = automático (6144 MB, até 40%% da RAM)")
    p = sub.add_parser("modo")
    p.add_argument("servidores", nargs="+")
    p.add_argument("modo", choices=["registrar", "aplicar"])
    p = sub.add_parser("desinstalar")
    p.add_argument("servidores", nargs="+")
    args = ap.parse_args()
    inv = load_inventory(os.environ.get("RMON_CONFIG", "/opt/rmon/config/servers.yaml"))
    if args.acao == "checar":
        sys.exit(0 if checar(inv, escolher(inv, args.servidores, True)) else 1)
    db.init(os.environ["RMON_DB_DSN"])
    servidores = escolher(inv, args.servidores, False)
    if args.acao == "instalar":
        ok = agir(inv, servidores, "instalar", script_instalar(args.modo, args.teto_mb), 240,
                  f"modo={args.modo} teto_mb={args.teto_mb or 'auto'}")
    elif args.acao == "modo":
        ok = agir(inv, servidores, "modo", script_modo(args.modo), 60, f"modo={args.modo}")
    else:
        ok = agir(inv, servidores, "desinstalar", script_desinstalar(), 120, "")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
