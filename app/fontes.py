"""De onde veio cada dado do ultimo ciclo: WinRM ou Elasticsearch.

O RMon le dos dois lados (ver app/elastic.py), e a tela precisa dizer qual dos
dois respondeu - senao ninguem sabe se o grafico, a contagem do NLB ou o
JobRunner sao leitura do host agora ou do Elastic. O scheduler registra aqui,
a cada ciclo, o que usou para cada servidor; o cabecalho, a pagina /fontes e
as etiquetas das telas so leem.

Vive na memoria do processo (o scheduler e o painel sao o mesmo processo):
depois de um restart fica vazio ate a primeira coleta, e a tela diz isso.
"""
from __future__ import annotations

import time
from typing import Any

_estado: dict[str, Any] = {"ts": None, "servidores": {}}

ROTULO = {"winrm": "WinRM", "elastic": "Elastic"}


def registrar(coletas: list[tuple], nomes_es: dict[str, str] | None = None,
              vistos_es: dict[str, float] | None = None, es_ativo: bool = False,
              agora: float | None = None) -> None:
    """Anota o que o ciclo usou. `vistos_es` = ultima metrica de cada host no Elastic."""
    agora = time.time() if agora is None else agora
    nomes_es = nomes_es or {}
    vistos_es = vistos_es or {}
    servidores: dict[str, dict] = {}
    for server, r in coletas:
        nome_es = nomes_es.get(server.name) or ""
        jr = r.get("jobrunners") if isinstance(r.get("jobrunners"), dict) else None
        n = r.get("nlb") if isinstance(r.get("nlb"), dict) else None
        visto = vistos_es.get(nome_es) if nome_es else None
        servidores[server.name] = {
            "winrm_ok": bool(r.get("reachable")),
            "winrm_erro": None if r.get("reachable") else (r.get("error") or "sem resposta")[:160],
            "elastic_host": nome_es,
            "elastic_visto": visto,
            # o que de fato alimentou cada dado neste ciclo
            "metricas": "winrm" if r.get("reachable") else None,
            "jobrunner": (jr.get("fonte") or "winrm") if jr else None,
            "nlb_auth": ((n.get("fonte") or {}).get("auth") or ("winrm" if n.get("auth") is not None else None))
            if n else None,
            "nlb_sess": ((n.get("fonte") or {}).get("sess") or ("winrm" if n.get("sess") is not None else None))
            if n else None,
            "nlb": bool(getattr(server, "nlb", False)),
        }
    _estado.update(ts=agora, servidores=servidores, es_ativo=bool(es_ativo))


def estado() -> dict[str, Any]:
    return _estado


def do_servidor(nome: str) -> dict[str, Any]:
    return (_estado.get("servidores") or {}).get(nome) or {}


def resumo(es: dict[str, Any], agora: float | None = None) -> dict[str, Any]:
    """O que o cabecalho mostra: WinRM N/M e o estado do Elastic.

    `es` = {"ativo", "ok_ts", "erro"} do app/elastic.py, lido na hora (a ultima
    consulta pode ser mais nova que o ultimo ciclo).
    """
    agora = time.time() if agora is None else agora
    srv = _estado.get("servidores") or {}
    total = len(srv)
    ok = sum(1 for s in srv.values() if s.get("winrm_ok"))
    if not es.get("ativo"):
        es_estado, es_texto = "off", "desligado"
    elif es.get("erro"):
        es_estado, es_texto = "erro", "fora do ar"
    elif es.get("ok_ts"):
        es_estado = "ok"
        es_texto = f"ha {max(0, int(agora - es['ok_ts']))}s"
    else:
        es_estado, es_texto = "espera", "aguardando"
    return {
        "coletado": _estado.get("ts") is not None,
        "winrm_ok": ok, "total": total,
        "winrm_estado": "ok" if total and ok == total else ("erro" if total and ok == 0 else "parcial"),
        "es_estado": es_estado, "es_texto": es_texto,
        "ciclo_ha": int(agora - _estado["ts"]) if _estado.get("ts") else None,
    }
