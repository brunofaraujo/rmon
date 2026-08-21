"""Polling periodico dos servidores e escrita no banco."""
from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, jobstats, notify
from .collector import collect_server
from .config import Inventory, Settings

log = logging.getLogger("rmon.scheduler")

# Estado do ultimo alerta por servidor (chave-problema -> texto), para notificar so em transicoes.
_last: dict[str, dict] = {}


DEFAULT_ALERTS = {"disk_pct": 90, "mem_pct": 90, "app_ms": 3000, "jobs_failed": 3,
                  "down_after": 3}


def service_down(s: dict) -> bool:
    """O servico conta como falha?

    Fixo (nomeado no inventario): qualquer estado != Running, inclusive
    NOT_FOUND - se foi declarado, e para estar la.
    Descoberto por padrao (service_patterns): so falha se estiver instalado com
    inicio automatico. Um RM.Host desinstalado nem aparece na coleta, e um
    deixado em Manual/Disabled foi parado de proposito - nenhum dos dois e
    motivo de alerta vermelho.
    """
    if (s.get("status") or "") == "Running":
        return False
    if s.get("src") == "auto":
        return str(s.get("start") or "").strip().lower().startswith("auto")
    return True


def service_groups(services: list[dict] | None) -> list[dict]:
    """Resumo dos servicos descobertos por padrao: quantos instalados x rodando."""
    grupos: dict[str, dict] = {}
    for s in services or []:
        if s.get("src") != "auto":
            continue
        g = grupos.setdefault(s.get("pattern") or "*", {"pattern": s.get("pattern") or "*",
                                                        "installed": 0, "running": 0})
        g["installed"] += 1
        if (s.get("status") or "") == "Running":
            g["running"] += 1
    return list(grupos.values())


def problems(r: dict, th: dict, fail_streak: int | None = None) -> dict[str, str]:
    """Problemas ativos de uma coleta. `fail_streak` = coletas consecutivas sem
    contato (db.fail_streak); enquanto ficar abaixo de `down_after`, a falha e
    tratada como instabilidade e nao vira DOWN - e o que evita a enxurrada de
    alertas quando o WinRM do host demora mais que o timeout de vez em quando.
    Sem esse argumento, mantem o comportamento antigo (alerta na primeira falha).
    """
    p: dict[str, str] = {}
    if not r.get("reachable"):
        need = max(1, int(th.get("down_after", 3) or 1))
        streak = need if fail_streak is None else fail_streak
        if streak < need:
            return p
        p["DOWN"] = f"sem contato (WinRM) ha {streak} coletas: {(r.get('error') or '')[:100]}"
        return p
    for s in r.get("services") or []:
        if service_down(s):
            p[f"svc:{s['name']}"] = f"servico {s['name']} = {s.get('status')}"
    if r.get("app_ok") is False:
        p["APP"] = "app_health (HTTP) falhou"
    mem = r.get("mem_pct")
    if mem is not None and mem >= th["mem_pct"]:
        p["MEM"] = f"memoria em {mem}%"
    disks = r.get("disks") or []
    main = next((d for d in disks if str(d.get("drive", "")).upper().startswith("C")), disks[0] if disks else None)
    if main and (main.get("used_pct") or 0) >= th["disk_pct"]:
        p["DISK"] = f"disco {main.get('drive')} em {main.get('used_pct')}%"
    if r.get("app_ok") is True and r.get("app_ms") is not None and r["app_ms"] > th["app_ms"]:
        p["APPSLOW"] = f"app_health lento: {r['app_ms']}ms"
    jb = r.get("jobs")
    if isinstance(jb, dict) and jb.get("failed") is not None and jb["failed"] >= th.get("jobs_failed", 3):
        p["JOBS"] = (f"{jb['failed']} execucoes de job com erro em {jb.get('window_min')}min "
                     "(validacao/regra de negocio, nao falha do servidor)")
    return p


def poll_all(inv: Inventory, settings: Settings) -> None:
    if not inv.servers:
        return
    th = {**DEFAULT_ALERTS, **(inv.defaults.get("alerts") or {}), **(db.get_config("alerts", {}) or {})}
    with ThreadPoolExecutor(max_workers=min(8, len(inv.servers))) as pool:
        futures = {
            pool.submit(collect_server, s, inv.winrm, inv.defaults): s
            for s in inv.servers
        }
        for fut, server in list(futures.items()):
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                result = {"reachable": False, "error": f"poll: {exc}"}
            if server.jobs:
                jb = server.jobs
                result["jobs"] = jobstats.query(jb.get("window_min", 15), jb.get("success_status", [2]), jb.get("failed_status", [5, 7]), jb.get("servidor"))
            db.insert_check(server.name, result)
            state = "OK" if result.get("reachable") else f"FALHA ({result.get('error')})"
            log.info("coleta %s -> %s", server.name, state)

            streak = 0 if result.get("reachable") else db.fail_streak(server.name)
            probs = problems(result, th, streak)
            prev = _last.get(server.name, {})
            new_keys = [k for k in probs if k not in prev]
            gone_keys = [k for k in prev if k not in probs]
            for k in new_keys:
                db.record_alert(server.name, "raised", k, probs[k])
            for k in gone_keys:
                db.record_alert(server.name, "resolved", k, prev[k])
            msgs = ["\U0001F534 " + probs[k] for k in new_keys]
            msgs += ["\U0001F7E2 resolvido: " + prev[k] for k in gone_keys]
            if msgs and notify.enabled():
                notify.send(f"RMonitor — {server.name} ({server.host})\n" + "\n".join(msgs))
            _last[server.name] = probs


def build_scheduler(inv: Inventory, settings: Settings) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="America/Recife")
    sched.add_job(
        poll_all, "interval", seconds=inv.poll_interval_seconds, args=[inv, settings],
        id="poll_all", max_instances=1, coalesce=True,
    )
    sched.add_job(db.prune, "interval", hours=6, id="prune", max_instances=1, coalesce=True)
    return sched
