"""Por que o job esta preso: o provavel motivo, com as pistas que o sustentam.

A lista de presos dizia QUAL job e COMO parar. Faltava o porque - e quase
sempre ele esta no proprio pedido. Em 23/10 os dois presos do win2016-2 eram o
SENAI007 (Lista de Presença) filtrado so por `STURMADISC.CODCOLIGADA = '3'`: a
instituicao inteira. O relatorio foi pedido 4 vezes assim em 30 dias e nenhuma
concluiu, enquanto os processos ficavam horas com um nucleo a 93% e a memoria
subindo. E nem todo "preso" esta preso: a Exportação de Dados das 12:30 leva
20 a 45 min todo dia.

As pistas vem de tres lugares:

* o pedido (jobstats._itens, lido junto com a fila): o relatorio e o filtro;
* o historico no banco do RM: os outros pedidos do mesmo relatorio (com que
  campos filtram os que concluem, e em quanto tempo) ou da mesma rotina;
* o processo no Elasticsearch: trabalhando (CPU) ou parado.

Historico e Elastic sao lidos em paralelo, com cache e prazo curto: a tela nao
espera mais que PRAZO_S; o que nao chegou aparece na atualizacao seguinte.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any, Callable

from . import elastic, jobstats
from .collector import jobrunner_settings

log = logging.getLogger("rmon.diagnostico")

PRAZO_S = 4.0
CACHE_HIST_S = 3600     # historico de relatorio/rotina muda devagar
CACHE_PROC_S = 60       # o processo, a cada atualizacao da tela
# "dentro do normal" = ate tanto x o p90 das execucoes que concluiram
FOLGA_NORMAL = 1.5

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rmon-diag")
_cache: dict[tuple, tuple[float, Future]] = {}
_lock = threading.Lock()

_ROTULO_CAMPO = {"CODCOLIGADA": "coligada", "CODFILIAL": "filial", "CODTIPOCURSO": "tipo de curso",
                 "CODTURMA": "turma", "IDPERLET": "período letivo", "RA": "RA", "CODDISC": "disciplina",
                 "IDTURMADISC": "turma/disciplina", "CODCURSO": "curso", "IDHABILITACAOFILIAL": "habilitação"}


def _rotulo(campos) -> str:
    nomes = [_ROTULO_CAMPO.get(c, c) for c in campos]
    if len(nomes) <= 1:
        return "".join(nomes)
    return ", ".join(nomes[:-1]) + " e " + nomes[-1]


def tempo(seg: Any) -> str:
    try:
        s = int(seg)
    except (TypeError, ValueError):
        return "?"
    if s < 90:
        return f"{s} s"
    m = round(s / 60)
    return f"{m} min" if m < 90 else f"{m // 60}h{m % 60:02d}"


def _cacheado(chave: tuple, validade: float, fn: Callable[[], Any]) -> Future:
    """A leitura em curso ou guardada; leitura que falhou (None) tenta de novo em 1 min."""
    agora = time.time()
    with _lock:
        c = _cache.get(chave)
        if c and not c[1].done():
            return c[1]
        if c and c[0] > agora and (_resultado(c[1]) is not None or c[0] - validade + 60 > agora):
            return c[1]
        fut = _pool.submit(fn)
        _cache[chave] = (agora + validade, fut)
        for k in [k for k, (exp, f) in _cache.items() if exp < agora - 3600 and f.done()]:
            del _cache[k]
        return fut


def _resultado(fut: Future | None) -> Any:
    if fut is None or not fut.done():
        return None
    try:
        return fut.result()
    except Exception as exc:  # noqa: BLE001
        log.warning("diagnostico: %s", exc)
        return None


def diagnosticar(it: dict, rel: dict | None, rot: dict | None, proc: dict | None) -> dict[str, Any]:
    """{"motivo", "tipo", "pistas"} para um job preso. Funcao pura.

    tipo: filtro (o pedido e o problema), normal (a rotina e demorada mesmo),
    parado (processo sem CPU), trabalhando (processando sem parar), ou
    desconhecido. A primeira regra que se aplica da o motivo; as pistas juntam
    tudo o que se sabe.
    """
    pistas: list[str] = []
    idade_s = (it.get("idade_min") or 0) * 60
    eh_rel = it.get("classe") == jobstats.RELATORIO
    campos = it.get("campos") or []
    nome_rel = (it.get("relatorio") or {}).get("codigo") or "este relatório"

    # o que os pedidos que concluem deste relatorio usam como filtro
    usual = None
    if rel and rel.get("n"):
        if rel.get("concluidas"):
            pistas.append(f"{nome_rel} em 30 dias: {rel['n']} pedido(s), {rel['concluidas']} concluído(s), "
                          f"em {tempo(rel['mediana_s'])} na mediana")
            usual = next((g for g in rel.get("grupos") or [] if g.get("concluidas") and g.get("campos")), None)
            if usual and usual["campos"] != campos:
                pistas.append(f"Os que concluem filtram por {_rotulo(usual['campos'])} "
                              f"({usual['concluidas']} pedido(s), {tempo(usual['mediana_s'])} na mediana)")
        else:
            pistas.append(f"{nome_rel} em 30 dias: {rel['n']} pedido(s), nenhum concluído")
    if eh_rel and it.get("filtro"):
        pistas.append(f"Filtro do pedido: {it['filtro']}")

    rotina = None
    if rot:
        rotina = rot.get("mesmo_job") or rot.get("classe")
        if rot.get("mesmo_job"):
            m = rot["mesmo_job"]
            pistas.append(f"Este job já rodou {m['concluidas']} vez(es): de {tempo(m['min_s'])} "
                          f"a {tempo(m['max_s'])}, {tempo(m['mediana_s'])} na mediana")
        elif rot.get("classe"):
            m = rot["classe"]
            pistas.append(f"Esta rotina em 14 dias: {m['concluidas']} execução(ões) concluída(s), "
                          f"{tempo(m['mediana_s'])} na mediana e até {tempo(m['p90_s'])} em 9 de cada 10")

    if proc:
        cpu = proc.get("cpu") or 0
        cresc = (proc["mb_fim"] - proc["mb_ini"]) / max(1, proc["seg"] / 60) if proc.get("seg") else 0
        estado = (f"um núcleo a {round(cpu * 100)}%" if 0.5 <= cpu <= 1.2 else
                  f"{round(cpu * 100)}% de CPU (100% = um núcleo)")
        mem = (f"memória de {proc['mb_ini']} para {proc['mb_fim']} MB em {tempo(proc['seg'])}"
               if proc.get("seg") else f"{proc['mb_fim']} MB de memória")
        pistas.append(f"Processo agora: {estado}, {mem}" + (f" (+{round(cresc)} MB/min)" if cresc >= 5 else ""))

    if eh_rel and it.get("sem_filtro"):
        motivo, tipo = ("Relatório gerado sem filtro: processa todos os registros da base.", "filtro")
    elif eh_rel and campos and set(campos) <= jobstats.CAMPOS_LARGOS:
        motivo, tipo = (f"Filtro só por {_rotulo(campos)}: na prática o relatório da instituição inteira, "
                        "sem turma, período ou aluno.", "filtro")
    elif eh_rel and usual and not set(usual["campos"]) <= set(campos):
        motivo, tipo = (f"Filtro diferente do usual: os pedidos que concluem filtram por "
                        f"{_rotulo(usual['campos'])}.", "filtro")
    elif rotina and rotina.get("p90_s") and idade_s <= rotina["p90_s"] * FOLGA_NORMAL:
        motivo, tipo = (f"Dentro do tempo normal desta rotina: costuma levar até {tempo(rotina['p90_s'])}.",
                        "normal")
    elif proc and (proc.get("cpu") or 0) < 0.05 and abs(proc["mb_fim"] - proc["mb_ini"]) < 20:
        motivo, tipo = ("O processo está parado, sem uso de CPU: provavelmente esperando o banco de dados "
                        "(bloqueio) ou travado.", "parado")
    elif proc and (proc.get("cpu") or 0) >= 0.5:
        motivo, tipo = ("O processo está trabalhando sem parar: volume de dados grande para o pedido.",
                        "trabalhando")
    else:
        motivo, tipo = ("Sem pista clara no pedido nem no processo: confirme com quem pediu.", "desconhecido")
    return {"motivo": motivo, "tipo": tipo, "pistas": pistas}


def anotar(itens: list[dict], inv, servidores: dict | None = None) -> None:
    """Poe `diag` em cada execucao presa da lista (in place), sem segurar a tela."""
    presos = [it for it in itens if it.get("tipo") == "presa" and it.get("idjob") is not None]
    if not presos:
        return
    cfg = elastic.settings_for(inv.defaults)
    processo = jobrunner_settings(inv.defaults).get("process") or "RM.Host.JobRunner"
    servidores = servidores or {s.name: s for s in inv.servers}
    pedidos = []
    for it in presos:
        rid = (it.get("relatorio") or {}).get("id")
        idjob = int(it["idjob"])
        if it.get("classe") == jobstats.RELATORIO:
            rel = (_cacheado(("rel", rid), CACHE_HIST_S, lambda r=rid, j=idjob: jobstats.historico_relatorio(r, j))
                   if rid else None)
            rot = None
        else:
            rel = None
            rot = _cacheado(("rot", idjob), CACHE_HIST_S,
                            lambda j=idjob, c=it.get("classe"): jobstats.historico_rotina(j, c))
        proc = None
        if it.get("pid") and elastic.ativo(cfg):
            nome = elastic.nome_es(servidores.get(it.get("server")), it.get("host"))
            proc = _cacheado(("proc", nome, it["pid"]), CACHE_PROC_S,
                             lambda n=nome, p=it["pid"]: elastic.processo_agora(cfg, n, processo, p))
        pedidos.append((it, rel, rot, proc))
    wait([f for _, *fs in pedidos for f in fs if f is not None], timeout=PRAZO_S)
    for it, rel, rot, proc in pedidos:
        it["diag"] = diagnosticar(it, _resultado(rel), _resultado(rot), _resultado(proc))
        if any(f is not None and not f.done() for f in (rel, rot, proc)):
            it["diag"]["pistas"].append("Ainda lendo o histórico e o processo: a tela completa na próxima atualização")
