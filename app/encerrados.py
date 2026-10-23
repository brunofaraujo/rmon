"""Historico dos processos de job que o RMon encerrou (aba Jobs).

O alerta do canal diz o essencial na hora: PID, job, quem pediu e o motivo.
Depois a pergunta muda - que relatorio era, quanto tempo rodou, valeu a pena
encerrar, a pessoa pediu de novo? Cada encerramento fica em `job_kills` com o
que se sabia na hora (regra, memoria, as leituras dos ultimos minutos), e a
tela junta tres leituras feitas depois:

* banco do RM: nome de quem pediu, o relatorio e o filtro, quanto tempo o job
  rodou ate ser encerrado, como o RM fechou a execucao e se a pessoa pediu o
  mesmo relatorio de novo nas 24 h seguintes;
* coletas do RMon (checks): a memoria do processo e a do servidor no minuto
  do encerramento;
* Elasticsearch: a curva do processo antes do encerramento (o crescimento
  lento aparece inteiro) e desde quando o processo estava no ar.

Coletas e Elastic sao lidos uma vez so e guardados no proprio registro: a
tabela checks guarda 30 dias, o historico fica.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from . import db, elastic, jobstats, nomes
from .collector import jobrunner_settings

log = logging.getLogger("rmon.encerrados")

DIAS = 30
LIMITE = 60
# coleta e Elastic so ficam guardados depois de assentar (o Elastic chega com
# ate 11 s de atraso; a coleta do minuto seguinte ainda mostra o processo)
ASSENTAR_S = 180
# curva de um job preso por horas: as ultimas 6 bastam para o desenho
JANELA_MAX_S = 6 * 3600


def duracao(seg: Any) -> str:
    """18 s, 1 min 30 s, 3h45, 2d 4h."""
    try:
        s = int(round(float(seg)))
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{max(s, 0)} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s} s" if s and m < 10 else f"{m} min"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def _pontos(dados: dict, coletas: list[dict] | None, es: dict | None,
            desde: float, kill: float) -> list[tuple[float, float]]:
    """A curva do processo: leituras do RMon (na hora), coletas e Elastic juntos."""
    brutos: list[tuple[float, float]] = []
    for a in dados.get("amostras") or []:
        if isinstance(a, (list, tuple)) and len(a) == 2 and a[1] is not None:
            brutos.append((float(a[0]), float(a[1])))
    brutos += [(p["t"], float(p["mb"])) for p in (coletas or []) if p.get("mb") is not None]
    brutos += [(p["t"], float(p["mb"])) for p in ((es or {}).get("pontos") or [])]
    if dados.get("mb") is not None:
        brutos.append((kill, float(dados["mb"])))
    saida: list[tuple[float, float]] = []
    for t, mb in sorted(brutos):
        if not desde <= t <= kill + 5:
            continue
        if saida and t - saida[-1][0] < 5:
            # a mesma leitura vinda de duas fontes: fica a maior
            saida[-1] = (saida[-1][0], max(saida[-1][1], mb))
        else:
            saida.append((t, mb))
    return saida


def grafico(pontos: list[tuple[float, float]], largura: int = 240, altura: int = 54) -> dict | None:
    """Geometria da curva (SVG feito no template). Comeca do zero: o salto e o assunto."""
    if len(pontos) < 2:
        return None
    t0, t1 = pontos[0][0], pontos[-1][0]
    dt = (t1 - t0) or 1
    topo = max(mb for _, mb in pontos) * 1.12 or 1
    m = 3

    def x(t):
        return round(m + (t - t0) / dt * (largura - 2 * m), 1)

    def y(mb):
        return round(altura - m - mb / topo * (altura - 2 * m), 1)

    linha = " ".join(f"{x(t)},{y(mb)}" for t, mb in pontos)
    return {"w": largura, "h": altura, "linha": linha,
            "area": f"{x(t0)},{altura} {linha} {x(t1)},{altura}",
            "fim_x": x(t1), "fim_y": y(pontos[-1][1]), "n": len(pontos),
            "pico_mb": round(max(mb for _, mb in pontos)), "t0": t0, "t1": t1}


def _servidor_no_kill(coletas: list[dict] | None, kill: float) -> dict:
    """Memoria do servidor na coleta mais perto do encerramento."""
    perto = [p for p in coletas or [] if abs(p["t"] - kill) <= 90 and p.get("mem_pct") is not None]
    if not perto:
        return {}
    p = min(perto, key=lambda p: abs(p["t"] - kill))
    return {"mem_pct": p.get("mem_pct"), "commit_pct": p.get("commit_pct")}


def _leituras(r: dict, dados: dict, server, cfg: dict, processo: str, desde: float,
              agora: float, guardar: bool) -> tuple[list[dict] | None, dict | None]:
    """Coletas e Elastic do processo: do registro, ou lidos agora (e guardados)."""
    kill = r["ts"]
    extra: dict[str, Any] = {}
    coletas = dados.get("coletas")
    if coletas is None and r.get("pid") is not None:
        try:
            coletas = db.jobrunner_nas_coletas(r["server"], r["pid"], desde, kill + 90)
            if guardar:
                extra["coletas"] = coletas
        except Exception as exc:  # noqa: BLE001
            log.warning("encerrados: coletas de %s: %s", r["server"], exc)
    es = dados.get("elastic")
    if es is None and r.get("pid") is not None and elastic.ativo(cfg):
        nome = elastic.nome_es(server, dados.get("computer") or nomes.computador(r["server"]))
        es = elastic.jobrunner_pid(cfg, nome, processo, r["pid"], desde, kill + 60)
        if es is not None and guardar:
            extra["elastic"] = es
    if extra and r.get("id") is not None:
        db.job_kill_guardar(r["id"], extra)
    return coletas, es


def _regra(r: dict, dados: dict) -> str:
    if r.get("origem") == "manual":
        return (f"Job preso, encerrado por {r.get('por') or '?'}" if dados.get("preso")
                else f"Encerrado por {r.get('por') or '?'}")
    return {"memoria": "Memória acima do limite",
            "ritmo": "Crescimento rápido de memória"}.get(dados.get("regra"), "Encerrado pelo RMon")


def _depois_txt(depois: list | None, seg: float) -> str | None:
    if depois is None:
        return None
    if not depois:
        return ("Não pediu de novo nas 24 h seguintes" if seg >= 86400
                else "Ainda não pediu de novo")
    return f"Pediu de novo {len(depois)}×"


def _item(r: dict, ex: dict | None, server, cfg: dict, processo: str, agora: float) -> dict[str, Any]:
    dados = dict(r.get("dados") or {})
    kill = r["ts"]
    seg = agora - kill
    ex = ex or {}
    # so do SQL: o `inicio` gravado na hora e do relogio do dominio, nao do RMon
    dur = ex.get("dur_s")
    desde = kill - min(JANELA_MAX_S, (dur or 600) + 120)
    # guarda a leitura so depois de assentar, e so com a janela certa (com o RM)
    guardar = seg > ASSENTAR_S and (bool(ex) or seg > 86400)
    coletas, es = _leituras(r, dados, server, cfg, processo, desde, agora, guardar)
    pontos = _pontos(dados, coletas, es, desde, kill)
    rel = ex.get("relatorio") or {}
    titulo = (rel.get("descricao") or ex.get("nome") or dados.get("nome") or "Job sem nome")
    processo_desde = (es or {}).get("processo_desde")
    return {
        "id": r.get("id"), "ts": kill, "seg": seg, "server": r["server"],
        "label": nomes.titulo(r["server"]) or r["server"], "pid": r.get("pid"),
        "origem": r.get("origem"), "por": r.get("por"), "ok": bool(r.get("ok")),
        "regra_txt": _regra(r, dados), "motivo": dados.get("motivo"), "retorno": dados.get("retorno"),
        "preso": bool(dados.get("preso")),
        "titulo": titulo, "relatorio": rel or None,
        "idjob": r.get("idjob") or dados.get("idjob"), "idexec": ex.get("idexec"),
        "usuario": ex.get("usuario") or r.get("usuario") or dados.get("usuario"),
        "usuario_nome": ex.get("usuario_nome"),
        "executor": ex.get("executor") or dados.get("executor"),
        "dur_s": dur, "inicio_ts": kill - dur if dur is not None else None,
        "rm": ({"status": ex.get("status"), "status_txt": ex.get("status_txt"),
                "mensagem": ex.get("mensagem"), "progresso": ex.get("progresso")} if ex else None),
        "mb": dados.get("mb"), "antes_mb": dados.get("antes_mb"), "mb_min": dados.get("mb_min"),
        "liberado_mb": dados.get("liberado_mb"), "regra": dados.get("regra"),
        "grafico": grafico(pontos), "mem_pct": None, "commit_pct": None,
        **_servidor_no_kill(coletas, kill),
        "processo_desde": processo_desde,
        "processo_reaproveitado": bool(processo_desde and dur is not None
                                       and kill - dur - processo_desde > 300),
        "sem_filtro": ex.get("sem_filtro"), "filtro": ex.get("filtro") or dados.get("filtro"),
        "filtro_obrigatorio": rel.get("filtro_obrigatorio"),
        "depois": ex.get("depois") or [], "depois_txt": _depois_txt(ex.get("depois"), seg),
    }


def historico(inv, dias: int = DIAS, limite: int = LIMITE, agora: float | None = None) -> dict[str, Any]:
    """{"itens": [...], "dias", "auto", "manual", "falhas", "erro", "rm_erro"} para a aba Jobs."""
    agora = time.time() if agora is None else agora
    base = {"itens": [], "dias": dias, "auto": 0, "manual": 0, "falhas": 0, "erro": None, "rm_erro": False}
    try:
        rows = db.recent_job_kills(dias, limite)
    except Exception as exc:  # noqa: BLE001
        log.warning("encerrados: %s", exc)
        return {**base, "erro": f"{type(exc).__name__}: {exc}"[:150]}
    eventos = [{"idjob": r["idjob"], "seg": agora - r["ts"]} for r in rows if r.get("idjob")]
    rm = jobstats.detalhes_encerrados(eventos) if eventos else {}
    cfg = elastic.settings_for(inv.defaults)
    processo = jobrunner_settings(inv.defaults).get("process") or "RM.Host.JobRunner"
    servidores = {s.name: s for s in inv.servers}
    itens = []
    for r in rows:
        try:
            itens.append(_item(r, (rm or {}).get(r.get("idjob")), servidores.get(r["server"]),
                               cfg, processo, agora))
        except Exception as exc:  # noqa: BLE001 - um registro torto nao some com os outros
            log.warning("encerrados: registro %s: %s", r.get("id"), exc)
    return {**base, "itens": itens, "rm_erro": rm is None,
            "auto": sum(1 for i in itens if i["ok"] and i["origem"] == "auto"),
            "manual": sum(1 for i in itens if i["ok"] and i["origem"] == "manual"),
            "falhas": sum(1 for i in itens if not i["ok"])}


def quando(ts: float) -> dict[str, str]:
    """{"hora": "14:09", "dia": "hoje" | "ontem" | "20/10"} para a coluna da esquerda."""
    d = datetime.fromtimestamp(ts)
    hoje = datetime.now().date()
    dias = (hoje - d.date()).days
    return {"hora": d.strftime("%H:%M"),
            "dia": "hoje" if dias == 0 else ("ontem" if dias == 1 else d.strftime("%d/%m"))}
