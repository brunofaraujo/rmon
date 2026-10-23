"""Elasticsearch como fonte opcional, SO DE LEITURA, ao lado do WinRM.

O parque ja manda para o Elastic (Elastic Agent/Fleet) duas coisas que o RMon
coleta por WinRM a cada minuto:

* `metrics-system.process-*`: os processos que estao no topo de memoria/CPU de
  cada host. Um RM.Host.JobRunner que dispara entra nesse topo - no incidente
  de 13/10 o Elastic tem a escalada inteira, ponto a ponto, ate 80 GB. E a
  unica fonte com HISTORICO: o WinRM so ve o instante;
* `logs-winlog.rdp_*`: os eventos 1149 (RDP autenticado) e 21/25 (sessao
  criada/reconectada) que o NLB usa para achar o no que autentica e nao abre
  sessao. Chegam ao Elastic de 5 a 11 s depois de acontecer.

Regras do jogo:

* nada aqui escreve no Elasticsearch: so `_search`;
* sem `RMON_ES_URL` + `RMON_ES_API_KEY` o modulo fica inerte e o RMon segue
  100% pelo WinRM, como sempre;
* toda chamada tem timeout curto e nunca levanta excecao: erro vira `None`, e
  quem chamou cai no WinRM. O Elastic fora do ar nao pode atrasar a coleta;
* dado so vale se for fresco: o host tem de ter mandado metrica ha menos de
  `max_age_sec`. Agente parado (acontece: Elastic Agent morto no host) nao
  pode virar "zero logons" nem "nenhum JobRunner grande".
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Iterable

import httpx

log = logging.getLogger("rmon.elastic")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "timeout_sec": 5,
    # host sem metrica no Elastic ha mais que isso = agente parado: nao vale
    "max_age_sec": 180,
    "process_index": "metrics-system.process-*",
    "rdp_index": "logs-winlog.rdp_*",
    # serie de batimento do agente (uma metrica a cada 10 s por host)
    "alive_index": "metrics-system.cpu-*",
    "rdp": True,
    "jobrunner": True,
}

# Saude da integracao, para a tela: ultimo sucesso, ultimo erro.
_saude: dict[str, Any] = {"ok_ts": None, "erro": None, "erro_ts": None}
_ULTIMO_LOG: dict[str, float] = {}


def settings_for(defaults: dict | None) -> dict[str, Any]:
    cfg = {**DEFAULTS, **(((defaults or {}).get("elastic")) or {})}
    cfg["url"] = os.environ.get("RMON_ES_URL", "").strip().rstrip("/")
    cfg["api_key"] = os.environ.get("RMON_ES_API_KEY", "").strip()
    ca = os.environ.get("RMON_ES_CA", "").strip()
    # caminho do PEM da CA do Elastic; "off" desliga a verificacao (nao recomendado)
    cfg["verify"] = False if ca.lower() in {"off", "false", "0", "no"} else (ca or True)
    return cfg


def ativo(cfg: dict) -> bool:
    return bool(cfg.get("enabled", True) and cfg.get("url") and cfg.get("api_key"))


def saude() -> dict[str, Any]:
    return dict(_saude)


def erro_atual() -> str | None:
    """O erro da ultima consulta, se ela falhou (erro mais novo que o ultimo sucesso)."""
    if (_saude.get("erro_ts") or 0) > (_saude.get("ok_ts") or 0):
        return _saude.get("erro")
    return None


def _avisar(chave: str, msg: str, *args) -> None:
    """Log de falha no maximo a cada 5 min por tipo: Elastic fora do ar nao
    pode encher o journal com uma linha por coleta."""
    agora = time.time()
    if agora - _ULTIMO_LOG.get(chave, 0) >= 300:
        _ULTIMO_LOG[chave] = agora
        log.warning(msg, *args)


def _buscar(cfg: dict, indice: str, corpo: dict) -> dict | None:
    if not ativo(cfg):
        return None
    try:
        r = httpx.post(f"{cfg['url']}/{indice}/_search", json=corpo,
                       headers={"Authorization": f"ApiKey {cfg['api_key']}"},
                       timeout=float(cfg.get("timeout_sec") or 5), verify=cfg["verify"])
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
        dados = r.json()
        _saude.update(ok_ts=time.time())
        return dados
    except Exception as exc:  # noqa: BLE001
        _saude.update(erro=f"{type(exc).__name__}: {exc}"[:200], erro_ts=time.time())
        _avisar("busca", "Elasticsearch: consulta em %s falhou (%s) - seguindo pelo WinRM",
                indice, _saude["erro"])
        return None


# ------------------------------------------------------------ nomes

def nome_es(server, computador: str | None) -> str:
    """host.name do servidor no Elastic: o configurado, ou o COMPUTERNAME."""
    return (getattr(server, "elastic_host", "") or computador or "").strip().lower()


def _filtro_hosts(nomes: Iterable[str]) -> dict:
    """host.name exato ou com dominio: "srv06n" casa "srv06n.dominio.local"."""
    should = []
    for n in sorted(set(x for x in nomes if x)):
        should += [{"term": {"host.name": n}}, {"prefix": {"host.name": n + "."}}]
    return {"bool": {"should": should, "minimum_should_match": 1}}


def _curto(host_name: str, nomes: Iterable[str]) -> str | None:
    """De volta do host.name do Elastic para o nome que procuramos."""
    h = (host_name or "").lower()
    for n in nomes:
        if h == n or h.startswith(n + "."):
            return n
    return None


def _epoch(valor) -> float | None:
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor) / 1000
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------ batimento

def vivos(cfg: dict, nomes: list[str], janela: str = "now-15m") -> dict[str, float]:
    """Ultima metrica de cada host no Elastic (epoch). Host ausente = sem dado
    na janela: agente parado, ou host que nem tem agente."""
    nomes = [n for n in nomes if n]
    if not nomes:
        return {}
    d = _buscar(cfg, cfg["alive_index"], {
        "size": 0,
        "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": janela}}},
                                      _filtro_hosts(nomes)]}},
        "aggs": {"h": {"terms": {"field": "host.name", "size": 500},
                       "aggs": {"ultimo": {"max": {"field": "@timestamp"}}}}}})
    if d is None:
        return {}
    saida: dict[str, float] = {}
    for b in d.get("aggregations", {}).get("h", {}).get("buckets", []):
        curto = _curto(b.get("key"), nomes)
        ts = _epoch((b.get("ultimo") or {}).get("value"))
        if curto and ts:
            saida[curto] = max(ts, saida.get(curto, 0))
    return saida


def frescos(cfg: dict, nomes: list[str], agora: float | None = None) -> set[str]:
    agora = time.time() if agora is None else agora
    limite = float(cfg.get("max_age_sec") or 180)
    # relogio do host pode estar adiantado (o dominio sistemafiep anda ~45 s a
    # frente do Elastic): "do futuro" conta como fresco
    return {n for n, ts in vivos(cfg, nomes).items() if agora - ts <= limite}


# ------------------------------------------------------------ RDP (NLB)

def rdp(cfg: dict, nomes: list[str], janela_min: int) -> dict[str, dict[str, int | None]]:
    """Logons RDP autenticados (1149) e sessoes criadas (21/25) por host na janela.

    `auth`/`sess` so vem preenchido se aquele canal do host chegou ao Elastic
    nas ultimas 24h: canal que nao e coletado (o 1149 de um host que nem grava
    o evento, um agente sem a integracao) e "nao sei", nunca zero.
    """
    nomes = [n for n in nomes if n]
    if not nomes:
        return {}
    janela = max(1, int(janela_min))
    d = _buscar(cfg, cfg["rdp_index"], {
        "size": 0,
        "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "now-24h"}}},
                                      _filtro_hosts(nomes)]}},
        "aggs": {"h": {"terms": {"field": "host.name", "size": 500}, "aggs": {
            "auth_canal": {"filter": {"term": {"event.code": "1149"}}},
            "sess_canal": {"filter": {"terms": {"event.code": ["21", "25"]}}},
            "agora": {"filter": {"range": {"@timestamp": {"gte": f"now-{janela}m"}}}, "aggs": {
                "auth": {"filter": {"term": {"event.code": "1149"}}},
                "sess": {"filter": {"terms": {"event.code": ["21", "25"]}}}}}}}}})
    if d is None:
        return {}
    saida: dict[str, dict[str, int | None]] = {}
    for b in d.get("aggregations", {}).get("h", {}).get("buckets", []):
        curto = _curto(b.get("key"), nomes)
        if not curto:
            continue
        agora = b.get("agora") or {}
        saida[curto] = {
            "auth": int(agora["auth"]["doc_count"]) if b["auth_canal"]["doc_count"] else None,
            "sess": int(agora["sess"]["doc_count"]) if b["sess_canal"]["doc_count"] else None,
        }
    return saida


# ------------------------------------------------------------ JobRunner

def _filtro_processo(cfg: dict, processo: str) -> dict:
    # o Elastic grava o nome com ".exe" (RM.Host.JobRunner.exe)
    return {"prefix": {"process.name": processo}}


def jobrunners_agora(cfg: dict, nome: str, processo: str, computador: str | None = None,
                     top: int = 5) -> dict | None:
    """Os maiores JobRunners do host segundo o Elastic, nos ultimos `max_age_sec`.

    E o substituto da leitura do WinRM quando o host para de responder - que e
    exatamente o que acontece quando um JobRunner estoura o commit. O formato e
    o mesmo da coleta (`top` com pid/private_mb), marcado com `fonte`.
    """
    if not nome:
        return None
    idade = int(float(cfg.get("max_age_sec") or 180))
    d = _buscar(cfg, cfg["process_index"], {
        "size": 0,
        "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": f"now-{idade}s"}}},
                                      _filtro_hosts([nome]), _filtro_processo(cfg, processo)]}},
        "aggs": {"pid": {"terms": {"field": "process.pid", "size": max(1, top),
                                   "order": {"mem": "desc"}},
                         "aggs": {"mem": {"max": {"field": "system.process.memory.size"}},
                                  "ts": {"max": {"field": "@timestamp"}}}}}})
    if d is None:
        return None
    procs = []
    ultimo = None
    for b in d.get("aggregations", {}).get("pid", {}).get("buckets", []):
        mem = (b.get("mem") or {}).get("value")
        if mem is None:
            continue
        ts = _epoch((b.get("ts") or {}).get("value"))
        if ts and (ultimo is None or ts > ultimo):
            ultimo = ts
        procs.append({"pid": int(b["key"]), "private_mb": round(float(mem) / 2**20)})
    if not procs:
        return None
    return {"count": None, "total_mb": None, "top": procs,
            "computer": (computador or nome).upper(), "fonte": "elastic", "ts": ultimo}


def jobrunner_serie(cfg: dict, nome: str, processo: str, horas: int = 24) -> list[dict]:
    """Memoria privada do MAIOR JobRunner do host ao longo do tempo.

    Um ponto por intervalo (5 min em 24h, 30 min em 7 dias), com o PID dono do
    pico. Intervalo sem JobRunner no topo do host fica de fora: o Elastic so
    guarda os processos que estao entre os maiores - quieto e bom sinal.
    """
    if not nome:
        return []
    horas = max(1, min(24 * 30, int(horas)))
    passo = "5m" if horas <= 24 else ("30m" if horas <= 24 * 7 else "2h")
    d = _buscar(cfg, cfg["process_index"], {
        "size": 0,
        "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": f"now-{horas}h"}}},
                                      _filtro_hosts([nome]), _filtro_processo(cfg, processo)]}},
        "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": passo,
                                          "min_doc_count": 1},
                       "aggs": {"mem": {"max": {"field": "system.process.memory.size"}},
                                "dono": {"top_metrics": {
                                    "metrics": {"field": "process.pid"},
                                    "sort": {"system.process.memory.size": "desc"}}}}}}})
    if d is None:
        return []
    pontos = []
    for b in d.get("aggregations", {}).get("t", {}).get("buckets", []):
        mem = (b.get("mem") or {}).get("value")
        if mem is None:
            continue
        top = ((b.get("dono") or {}).get("top") or [{}])[0].get("metrics") or {}
        pid = top.get("process.pid")
        pontos.append({"t": float(b["key"]) / 1000, "mb": round(float(mem) / 2**20),
                       "pid": int(pid) if pid is not None else None})
    return pontos


def jobrunner_pid(cfg: dict, nome: str, processo: str, pid: int, desde: float,
                  ate: float) -> dict | None:
    """Um JobRunner especifico entre dois instantes (epoch): para o historico de
    encerrados. {"pontos": [{"t", "mb"}], "processo_desde": epoch | None}.

    O Elastic so guarda os processos do topo do host, uma vez por minuto (no
    SRV11, 10/2026): um estouro de 1 minuto tem um ponto ou nenhum, mas o
    crescimento lento (as horas de 06 e 13/10) aparece inteiro. None = Elastic
    fora do ar ou desligado; {} sem pontos = o processo nao chegou ao topo.
    """
    if not nome or pid is None:
        return None
    d = _buscar(cfg, cfg["process_index"], {
        "size": 0,
        "query": {"bool": {"filter": [
            {"range": {"@timestamp": {"gte": int(desde * 1000), "lte": int(ate * 1000),
                                      "format": "epoch_millis"}}},
            _filtro_hosts([nome]), _filtro_processo(cfg, processo), {"term": {"process.pid": int(pid)}}]}},
        "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": "30s",
                                          "min_doc_count": 1},
                       "aggs": {"mem": {"max": {"field": "system.process.memory.size"}}}},
                 "inicio": {"min": {"field": "process.cpu.start_time"}}}})
    if d is None:
        return None
    ag = d.get("aggregations", {})
    pontos = [{"t": float(b["key"]) / 1000, "mb": round(float(b["mem"]["value"]) / 2**20)}
              for b in ag.get("t", {}).get("buckets", []) if (b.get("mem") or {}).get("value") is not None]
    return {"pontos": pontos, "processo_desde": _epoch((ag.get("inicio") or {}).get("value"))}


def processo_agora(cfg: dict, nome: str, processo: str, pid: int, minutos: int = 15) -> dict | None:
    """O que um JobRunner preso esta fazendo: CPU e memoria nos ultimos minutos.

    `cpu` e a media de system.process.cpu.total.pct: 1.0 = um nucleo inteiro
    (o relatorio do RM e de uma thread so: em 23/10 os dois presos ficaram em
    0,93 por horas, com a memoria subindo ~35 MB/min). {"cpu", "mb_ini",
    "mb_fim", "seg"}; None = Elastic fora ou o processo fora do topo do host.
    """
    if not nome or pid is None:
        return None
    d = _buscar(cfg, cfg["process_index"], {
        "size": 0,
        "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": f"now-{int(minutos)}m"}}},
                                      _filtro_hosts([nome]), _filtro_processo(cfg, processo),
                                      {"term": {"process.pid": int(pid)}}]}},
        "aggs": {"cpu": {"avg": {"field": "system.process.cpu.total.pct"}},
                 "t": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1m", "min_doc_count": 1},
                       "aggs": {"m": {"max": {"field": "system.process.memory.size"}}}}}})
    if d is None:
        return None
    ag = d.get("aggregations", {})
    pts = [(float(b["key"]) / 1000, b["m"]["value"]) for b in ag.get("t", {}).get("buckets", [])
           if (b.get("m") or {}).get("value") is not None]
    cpu = (ag.get("cpu") or {}).get("value")
    if not pts or cpu is None:
        return None
    return {"cpu": round(float(cpu), 3), "mb_ini": round(pts[0][1] / 2**20), "mb_fim": round(pts[-1][1] / 2**20),
            "seg": round(pts[-1][0] - pts[0][0])}


# ------------------------------------------------------------ desenho

def grafico(pontos: list[dict], aviso_gb: float = 0, critico_gb: float = 0,
            largura: int = 640, altura: int = 150) -> dict[str, Any]:
    """Geometria do grafico da serie (SVG feito no template, sem biblioteca).

    Escala vertical ate o maior entre o pico e o limiar critico, para as linhas
    de aviso/critico estarem sempre na tela - e a distancia ate elas que conta.
    """
    if not pontos:
        return {}
    t0, t1 = pontos[0]["t"], pontos[-1]["t"]
    dt = (t1 - t0) or 1
    pico = max(pontos, key=lambda p: p["mb"])
    topo_gb = max(pico["mb"] / 1024, float(critico_gb or 0), float(aviso_gb or 0), 1) * 1.1
    m_esq, m_inf = 4, 4
    w, h = largura - m_esq, altura - m_inf

    def x(t):
        return round(m_esq + (t - t0) / dt * (w - 4), 1)

    def y(gb):
        return round(h - gb / topo_gb * (h - 6), 1)

    linha = " ".join(f"{x(p['t'])},{y(p['mb'] / 1024)}" for p in pontos)
    return {
        "w": largura, "h": altura, "linha": linha,
        "area": f"{x(t0)},{h} {linha} {x(t1)},{h}",
        "aviso_y": y(float(aviso_gb)) if aviso_gb else None,
        "critico_y": y(float(critico_gb)) if critico_gb else None,
        "pico_gb": round(pico["mb"] / 1024, 1), "pico_pid": pico.get("pid"),
        "pico_t": pico["t"], "pico_x": x(pico["t"]), "pico_y": y(pico["mb"] / 1024),
        "t0": t0, "t1": t1, "pontos": len(pontos),
    }
