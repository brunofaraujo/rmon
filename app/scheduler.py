"""Polling periodico dos servidores e escrita no banco."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Iterable

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, execution, inventory, jobstats, notify, packages
from .collector import collect_server
from .config import Inventory, Settings
from .inventory import collect_inventory

log = logging.getLogger("rmon.scheduler")

# Estado do ultimo alerta por servidor (chave-problema -> texto), para notificar so em transicoes.
_last: dict[str, dict] = {}


DEFAULT_ALERTS = {"disk_pct": 90, "mem_pct": 90, "app_ms": 3000, "jobs_failed": 3,
                  "down_after": 3, "commit_pct": 90, "broker_min_pct": 60,
                  "broker_min_kb": 0, "broker_settle_min": 10,
                  "broker_history_days": 30}


def broker_reference(dias: int | None = None) -> dict[str, dict[str, int]]:
    """Tamanho de referencia do broker de cada host: o maior que ele ja teve.

    O tamanho "certo" do broker depende de quantas customizacoes a instalacao
    tem - o mesmo painel enxerga um host de 558KB e outro de 33KB, os dois
    corretos -, entao comparar host contra host acusaria diferenca onde nao ha.
    O que caracteriza a falha e a queda: o arquivo era grande e voltou pequeno,
    e assim fica, porque o RM so gera o broker quando ele nao existe.
    """
    return db.broker_max(int(dias or 30))


def _kb(n: float) -> str:
    return f"{n / 1024:.0f}KB"


def broker_problems(r: dict, th: dict, ref: dict[str, int] | None = None) -> dict[str, str]:
    """Broker de customizacao truncado ou ausente.

    O RM so gera `_BrokerCustom.dat` quando o arquivo NAO existe. Se a geracao
    aborta no meio (tipicamente por estouro do limite de commit), sobra um
    arquivo curto - e a partir dai todo start reusa esse cache: o servico sobe
    "com sucesso" e as customizacoes simplesmente nao carregam.

    `ref` e o historico DESTE host ({arquivo: maior tamanho ja visto}), vindo de
    broker_reference().
    """
    p: dict[str, str] = {}
    brokers = r.get("broker") or []
    if not brokers:
        return p
    ref = ref or {}
    settle = int(th.get("broker_settle_min", 10) or 0)
    min_pct = float(th.get("broker_min_pct", 60) or 0)
    min_kb = float(th.get("broker_min_kb", 0) or 0)
    # Ausencia so e anomalia se ha RM.Host no ar: com o host parado, ninguem
    # tinha mesmo de ter gerado o arquivo.
    host_no_ar = any(
        (s.get("status") or "") == "Running" and str(s.get("name") or "").startswith("RM.Host")
        for s in (r.get("services") or [])
    )
    for b in brokers:
        nome = b.get("name") or "broker"
        chave = f"broker:{nome}"
        tamanho = b.get("size")
        if tamanho is None:
            if host_no_ar:
                p[chave] = f"{nome} nao existe em {b.get('path')} com o RM.Host no ar"
            continue
        idade = b.get("age_min")
        if idade is not None and settle and idade < settle:
            continue  # recem-gerado: pode ainda estar sendo escrito
        maior = int(ref.get(nome) or 0)
        if min_kb and tamanho < min_kb * 1024:
            p[chave] = (f"{nome} com {_kb(tamanho)} (minimo esperado {_kb(min_kb * 1024)}): "
                        "cache truncado, customizacoes nao carregam")
        elif maior and min_pct and tamanho < maior * min_pct / 100:
            p[chave] = (f"{nome} com {_kb(tamanho)}, contra {_kb(maior)} que este host ja teve: "
                        "geracao abortada, customizacoes nao carregam")
    return p


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


def problems(r: dict, th: dict, fail_streak: int | None = None,
             broker_ref: dict[str, int] | None = None) -> dict[str, str]:
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
    commit = r.get("commit_pct")
    if commit is not None and commit >= th.get("commit_pct", 90):
        p["COMMIT"] = (f"commit charge em {commit}% do limite (RAM + pagefile): "
                       "nesse ponto o RM.Host falha ao gerar o broker (0x800705AF)")
    p.update(broker_problems(r, th, broker_ref))
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
        coletas = []
        for fut, server in list(futures.items()):
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                result = {"reachable": False, "error": f"poll: {exc}"}
            if server.jobs:
                jb = server.jobs
                result["jobs"] = jobstats.query(jb.get("window_min", 15), jb.get("success_status", [2]), jb.get("failed_status", [5, 7]), jb.get("servidor"))
            coletas.append((server, result))

        # Referencia do broker: o historico de cada host, lido uma vez por ciclo.
        ref = broker_reference(th.get("broker_history_days"))
        for server, result in coletas:
            db.insert_check(server.name, result)
            state = "OK" if result.get("reachable") else f"FALHA ({result.get('error')})"
            log.info("coleta %s -> %s", server.name, state)

            streak = 0 if result.get("reachable") else db.fail_streak(server.name)
            probs = problems(result, th, streak, ref.get(server.name))
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


def poll_inventory(inv: Inventory, settings: Settings) -> None:
    """Coleta o inventario de software de todos os hosts e grava as mudancas.

    Roda em cadencia de horas (nao no ciclo de metricas): varrer o registro e os
    hotfixes custa segundos por host e o resultado muda em dias, nao em minutos.
    """
    cfg = inventory.settings_for(inv.defaults)
    if not cfg.get("enabled", True) or not inv.servers:
        return
    with ThreadPoolExecutor(max_workers=min(4, len(inv.servers))) as pool:
        futures = {pool.submit(collect_inventory, s, inv.winrm, inv.defaults): s
                   for s in inv.servers}
        for fut, server in list(futures.items()):
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "items": [], "error": f"inventario: {exc}"}
            gasto = int(res.get("ms") or 0)
            if not res.get("ok"):
                db.record_inventory_run(server.name, False, 0, res.get("error"), gasto)
                log.warning("inventario %s -> FALHA (%s)", server.name, res.get("error"))
                continue

            itens = res["items"]
            atuais = db.packages_of(server.name)
            # Primeira coleta do host e semeadura: gerar 200 eventos "instalado"
            # so encheria a linha do tempo de ruido no dia em que o servidor entrou.
            eventos = (inventory.diff_packages(atuais, itens, inventory.fontes_ativas(inv.defaults))
                       if atuais else [])
            db.replace_packages(server.name, itens)
            db.insert_package_events(server.name, eventos)
            db.record_inventory_run(server.name, True, len(itens), None, gasto,
                                    res.get("computer"), res.get("os"))
            if res.get("last_upgrade"):
                db.set_inventory_upgrade(server.name, res["last_upgrade"])
            log.info("inventario %s -> %d pacotes, %d mudanca(s) em %dms",
                     server.name, len(itens), len(eventos), gasto)
            _notify_package_changes(server.name, server.host, eventos)
    scan_packages(settings)


def scan_packages(settings: Settings) -> int:
    """Le o repositorio de pacotes baixados e atualiza o catalogo.

    Roda junto com a coleta de inventario porque o casamento entre "arquivo
    baixado" e "item instalado" depende do inventario recem-gravado: o nome do
    pacote do TDN e o mesmo que o item usa no registro do Windows.
    """
    entradas = packages.scan(settings.packages_dir)
    if not entradas:
        db.replace_repo_catalog([])
        return 0
    indice = packages.build_index(db.all_packages())
    vinculos = db.get_config("catalog_bind", {}) or {}
    for e in entradas:
        e["pkg_key"] = packages.resolve(e["produto"], indice, vinculos)
    db.replace_repo_catalog(entradas)
    sem_casar = sum(1 for e in entradas if not e["pkg_key"])
    log.info("repositorio de pacotes: %d arquivo(s), %d sem item correspondente",
             len(entradas), sem_casar)
    return len(entradas)


def _notify_package_changes(name: str, host: str, eventos: list[dict]) -> None:
    """Avisa que o software de um servidor mudou.

    Nao e um alerta de falha: e rastreabilidade. Mudanca de pacote e a primeira
    coisa que se procura quando o servidor comeca a se comportar diferente sem
    ninguem ter mexido nele.
    """
    if not eventos:
        return
    for e in eventos:
        db.record_alert(name, "package", e["pkg_key"],
                        f"{inventory.EVENT_LABEL.get(e['kind'], e['kind'])}: {e['name']} "
                        f"{e.get('old_version') or ''} -> {e.get('new_version') or ''}".strip())
    if not notify.enabled():
        return
    linhas = [f"\u2022 {inventory.EVENT_LABEL.get(e['kind'], e['kind'])}: {e['name']}"
              + (f" {e.get('old_version')} -> {e.get('new_version')}"
                 if e["kind"] in ("upgraded", "downgraded") else
                 (f" {e.get('new_version')}" if e.get("new_version") else ""))
              for e in eventos[:12]]
    if len(eventos) > 12:
        linhas.append(f"... e mais {len(eventos) - 12} mudanca(s)")
    notify.send(f"\U0001F4E6 RMonitor \u2014 software alterado em "
                f"{name} ({host})\n" + "\n".join(linhas))


def _url_stage(settings: Settings, inv: Inventory, task_id: int, segundos: int) -> str:
    """Endereco temporario e assinado de onde o host baixa o pacote."""
    base = str(execution.settings_for(inv.defaults).get("base_url") or "").rstrip("/")
    if not base:
        return ""
    expira = int(time.time()) + max(120, segundos)
    assinatura = execution.stage_token(settings.secret_key, task_id, expira)
    return f"{base}/pacotes/stage/{task_id}?exp={expira}&sig={assinatura}"


def run_pending_tasks(inv: Inventory, settings: Settings) -> None:
    """Processa UMA tarefa da fila por vez.

    Toda tarefa passa pelo pre-voo, inclusive a real: as checagens sao
    somente-leitura e uma reprovacao dura bloqueia em vez de "tentar assim
    mesmo". Tarefa em seco para no pre-voo e nao encosta no host alem disso.
    """
    task = db.next_pending_task()
    if task is None:
        return
    servidores = {s.name: s for s in inv.servers}
    server = servidores.get(task["server"])
    linhas = list(db.packages_of(task["server"]).values())
    task = dict(task)
    task["install_dir"] = execution.install_dir_from(linhas)
    instalado = next((r["version"] for r in linhas if r["pkg_key"] == task.get("pkg_key")), None)

    acao = execution.actions_for(inv.defaults).get(task.get("action") or "")
    prazo = acao["timeout_sec"] if acao else 900
    base = str(execution.settings_for(inv.defaults).get("base_url") or "").rstrip("/")
    plano = execution.preflight(task, server, inv.winrm, inv.defaults, settings,
                                instalado=instalado,
                                url_check=f"{base}/healthz" if base else "")

    if task["mode"] != "real":
        db.finish_task(task["id"], "ok" if plano["ok"] else "blocked",
                       checks=plano["checks"], comando=plano["comando"],
                       output="pre-voo: nada foi executado no host",
                       error=None if plano["ok"] else
                       "bloqueado por: " + ", ".join(plano["bloqueios"]))
        log.info("tarefa %d (%s, em seco) -> %s", task["id"], task["server"],
                 "ok" if plano["ok"] else "bloqueada")
        return

    if not plano["ok"]:
        db.finish_task(task["id"], "blocked", checks=plano["checks"],
                       comando=plano["comando"], output="nada foi executado",
                       error="bloqueado por: " + ", ".join(plano["bloqueios"]))
        log.warning("tarefa %d (%s) BLOQUEADA: %s", task["id"], task["server"],
                    ", ".join(plano["bloqueios"]))
        return

    destino = ""
    if acao and acao["kind"] == "copy" and task["install_dir"]:
        destino = task["install_dir"].rstrip(chr(92)) + chr(92) + acao["dest"]
    log.warning("tarefa %d: EXECUTANDO em %s -> %s", task["id"], task["server"],
                plano["comando"])
    url = _url_stage(settings, inv, task["id"], prazo + 600)
    res = execution.execute(task, server, inv.winrm, inv.defaults, settings, url, destino)
    db.finish_task(task["id"], "ok" if res.get("ok") else "failed",
                   checks=plano["checks"], comando=plano["comando"],
                   exit_code=res.get("exit_code"), output=res.get("output"),
                   error=res.get("error"))
    db.audit(task.get("created_by"), "install_task",
             f"{task['server']} {task.get('produto')} {task.get('version')} -> "
             f"{'ok' if res.get('ok') else 'falhou'}")
    if notify.enabled():
        marca = "\u2705" if res.get("ok") else "\u274C"
        notify.send(f"{marca} RMonitor \u2014 {task['server']}: {task.get('produto')} "
                    f"{task.get('version')} ({task['action']}) "
                    f"{'concluido' if res.get('ok') else 'FALHOU: ' + str(res.get('error'))[:200]}")


def build_scheduler(inv: Inventory, settings: Settings) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="America/Recife")
    sched.add_job(
        poll_all, "interval", seconds=inv.poll_interval_seconds, args=[inv, settings],
        id="poll_all", max_instances=1, coalesce=True,
    )
    sched.add_job(db.prune, "interval", hours=6, id="prune", max_instances=1, coalesce=True)
    # A fila e curta e barata de olhar; o intervalo curto e so latencia da tela.
    sched.add_job(run_pending_tasks, "interval", seconds=20, args=[inv, settings],
                  id="run_pending_tasks", max_instances=1, coalesce=True)
    cfg = inventory.settings_for(inv.defaults)
    if cfg.get("enabled", True):
        # Primeira execucao logo apos o start (o APScheduler so dispararia depois
        # do intervalo inteiro, e um restart nao pode significar horas sem dados).
        sched.add_job(
            poll_inventory, "interval", hours=max(1, int(cfg.get("interval_hours", 6))),
            args=[inv, settings], id="poll_inventory", max_instances=1, coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
        )
    return sched
