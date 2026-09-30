"""Polling periodico dos servidores e escrita no banco."""
from __future__ import annotations

import logging
import re
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
                  "broker_history_days": 30,
                  # fila de jobs: janela sem nenhuma conclusao E idade a partir da
                  # qual uma execucao em andamento passa a contar como presa
                  "jobs_stuck_min": 30,
                  # quantas execucoes presas (ou esperando pickup) para alertar
                  "jobs_stuck_jobs": 2,
                  # coletas seguidas vendo a fila parada antes de alertar
                  "jobs_stuck_checks": 3,
                  # acima disso a execucao sem conclusao e residuo (host reiniciado
                  # no meio), nao fila: nao conta para alerta nenhum
                  "jobs_orphan_min": 360,
                  # falhas na janela SEM nenhum sucesso = falha em bloco
                  "jobs_all_failed": 10}

# Problemas que existem para serem VISTOS no painel, nao empurrados no celular.
# Job que termina com erro e validacao/regra de negocio da aplicacao (dado
# errado no cadastro, competencia fechada...), nao falha do servidor: quem
# resolve e o usuario que pediu o job, no horario dele. O que continua acordando
# alguem e a fila travada (JOBQUEUE) e a falha em bloco (JOBFAIL), que sao
# problema de infraestrutura.
AVISOS = frozenset({"JOBS", "JOBSQL"})

# Problemas que pintam o cartao de vermelho (mural e painel).
CRITICOS = frozenset({"DOWN", "APP", "JOBQUEUE", "JOBFAIL"})


def notifica(chave: str) -> bool:
    """Este problema merece Slack/Telegram?"""
    return chave not in AVISOS


def critico(chave: str) -> bool:
    """Este problema e vermelho (vs. ambar/aviso)?"""
    return chave.startswith("svc:") or chave in CRITICOS


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


def _lista(v) -> list[str]:
    """Campo que o PowerShell manda como lista - ou como string, quando tem um
    item so (ConvertTo-Json do 5.1 nao empacota colecao unitaria em array)."""
    if not v:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def broker_problems(r: dict, th: dict, ref: dict[str, int] | None = None,
                    estavel: dict[str, bool] | None = None) -> dict[str, str]:
    """Broker de customizacao truncado ou ausente.

    O RM so gera `_BrokerCustom.dat` quando o arquivo NAO existe, e a primeira
    geracao costuma sair incompleta (ver docs/OPERACAO.md). Como o arquivo
    passou a existir, todo start seguinte reusa esse cache curto: o servico sobe
    "com sucesso" e as customizacoes simplesmente nao carregam.

    `ref` e o historico DESTE host ({arquivo: maior tamanho ja visto}), vindo de
    broker_reference(). `estavel` (db.broker_estado) diz se o arquivo parou de
    crescer entre as duas ultimas coletas - so ai o tamanho vale como veredito.
    """
    p: dict[str, str] = {}
    brokers = r.get("broker") or []
    if not brokers:
        return p
    ref = ref or {}
    estavel = estavel or {}
    settle = int(th.get("broker_settle_min", 10) or 0)
    min_pct = float(th.get("broker_min_pct", 60) or 0)
    min_kb = float(th.get("broker_min_kb", 0) or 0)
    # Ausencia so e anomalia se um servico DAQUELA pasta subiu: e ele que teria
    # de ter gerado o arquivo. Com a instalacao parada, ninguem tinha mesmo de
    # gerar - e o vizinho de outra pasta (ou um auxiliar, que nem gera broker)
    # nao serve de prova. Coleta antiga, sem os donos da pasta, cai no
    # comportamento anterior: qualquer servico descoberto no ar vale.
    legado_no_ar = any(
        (s.get("status") or "") == "Running" and s.get("src") == "auto"
        for s in (r.get("services") or [])
    )
    for b in brokers:
        nome = b.get("name") or "broker"
        chave = f"broker:{nome}"
        tamanho = b.get("size")
        if tamanho is None:
            donos = _lista(b.get("running"))
            if "running" not in b:
                if legado_no_ar:
                    p[chave] = f"{nome} nao existe em {b.get('path')} com o servico no ar"
            elif donos:
                quem = ", ".join(donos[:3]) + ("..." if len(donos) > 3 else "")
                p[chave] = f"{nome} nao existe em {b.get('path')} com {quem} no ar"
            continue
        # Recem-gerado pode ainda estar sendo escrito. A prova de que parou de
        # crescer sao duas coletas iguais; a idade fica so como rede para quem
        # ainda nao tem coleta anterior (host novo, RMon recem-subido).
        idade = b.get("age_min")
        if not estavel.get(nome):
            if idade is None or (settle and idade < settle):
                continue
        maior = int(ref.get(nome) or 0)
        if min_kb and tamanho < min_kb * 1024:
            p[chave] = (f"{nome} com {_kb(tamanho)} (minimo esperado {_kb(min_kb * 1024)}): "
                        "cache truncado, customizacoes nao carregam")
        elif maior and min_pct and tamanho < maior * min_pct / 100:
            p[chave] = (f"{nome} com {_kb(tamanho)}, contra {_kb(maior)} que este host ja teve: "
                        "broker incompleto, customizacoes nao carregam")
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


# Sufixo de instancia: separador opcional + numero no fim do nome
# ("RM.Host.Service02", "RM.Host.Service_2", "RM.Host.Service (2)").
_SUFIXO_INSTANCIA = re.compile(r"^(?P<base>.*[^\W\d_])[ ._\-#]?\(?(?P<n>\d{1,3})\)?$")


def service_family(nome: str | None) -> str:
    """Familia de um servico: o proprio nome, sem o sufixo de instancia.

    Dois servicos que so diferem por um numero no fim ("RM.Host.Service" e
    "RM.Host.Service02") sao a mesma aplicacao instalada duas vezes e contam
    juntos. Nomes diferentes - "RM.Host.Service" e "RM.Host.Cleanner" - sao
    servicos diferentes, com funcoes diferentes, ainda que casem no mesmo
    curinga de descoberta: o prefixo comum nao faz deles um so.
    """
    nome = (nome or "").strip()
    m = _SUFIXO_INSTANCIA.match(nome)
    return m.group("base") if m else nome


def service_groups(services: list[dict] | None) -> list[dict]:
    """Resumo por familia de servico: quantas instancias instaladas x rodando.

    Agrupa pelo nome real do servico (ver service_family), nao pelo curinga que
    o descobriu - o curinga junta coisas que nada tem a ver uma com a outra.
    Familia com uma instancia so nao vira resumo: a pilula dela ja esta na
    lista de servicos, e repetir "1 instalado, 1 em execucao" nao diz nada.
    """
    grupos: dict[str, dict] = {}
    for s in services or []:
        if s.get("src") != "auto":
            continue
        familia = service_family(s.get("name"))
        g = grupos.setdefault(familia, {"family": familia, "patterns": [], "names": [],
                                        "installed": 0, "running": 0})
        padrao = s.get("pattern") or "*"
        if padrao not in g["patterns"]:
            g["patterns"].append(padrao)
        g["names"].append(s.get("name") or "")
        g["installed"] += 1
        if (s.get("status") or "") == "Running":
            g["running"] += 1
    saida = [g for g in grupos.values() if g["installed"] > 1]
    for g in saida:
        g["pattern"] = ", ".join(g["patterns"])
        g["names"].sort()
    return sorted(saida, key=lambda g: g["family"].lower())


def problems(r: dict, th: dict, fail_streak: int | None = None,
             broker_ref: dict[str, int] | None = None,
             broker_estavel: dict[str, bool] | None = None,
             fila_hist: list[dict] | None = None) -> dict[str, str]:
    """Problemas ativos de uma coleta. `fail_streak` = coletas consecutivas sem
    contato (db.fail_streak); enquanto ficar abaixo de `down_after`, a falha e
    tratada como instabilidade e nao vira DOWN - e o que evita a enxurrada de
    alertas quando o WinRM do host demora mais que o timeout de vez em quando.
    Sem esse argumento, mantem o comportamento antigo (alerta na primeira falha).

    `fila_hist` = as ultimas leituras da fila deste host (db.fila_hist), da mais
    nova para a mais velha, para que a fila travada precise se confirmar em
    coletas seguidas antes de virar alerta.
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
    p.update(broker_problems(r, th, broker_ref, broker_estavel))
    p.update(job_problems(r.get("jobs"), th, fila_hist, p))
    return p


def fila_parada(fila: dict | None, th: dict) -> tuple[bool, str]:
    """Esta leitura da fila mostra a fila TRAVADA? (veredito instantaneo)

    Duas perguntas, nesta ordem:

    1. a fila andou? `done_recent` > 0 e o fim da conversa - executor que
       concluiu alguma coisa na janela nao esta travado, por mais velha que seja
       a execucao mais antiga que ele carrega. Foi por nao perguntar isso que a
       versao anterior alertava a noite inteira: "nada concluido ha 30min" e o
       normal de um executor fora do horario comercial (nos ultimos 7 dias houve
       de 24 a 71 intervalos assim por executor, o maior com 2,4 dias);
    2. tem trabalho preso? ou execucao em andamento ha mais de `jobs_stuck_min`
       (`stuck` - ja sem o residuo de host reiniciado, que a consulta separa em
       `orphans`), ou execucao cuja hora programada venceu e ninguem comecou
       (`waiting`). Sem uma coisa nem outra, a fila esta vazia, nao travada.

    O minimo de execucoes (`jobs_stuck_jobs`) existe porque uma sozinha e ruido
    conhecido: aparecem cerca de 11 execucoes assim a cada 90 dias no parque,
    quase sempre isoladas. No incidente real de 18/08 foram 8 de uma vez, no
    mesmo executor, com zero conclusoes - e isso o criterio pega 55min antes de
    o servico cair e virar alerta de servico parado.
    """
    if not isinstance(fila, dict) or fila.get("error"):
        return False, ""
    minimo = max(1, int(th.get("jobs_stuck_jobs", 2) or 1))
    concluidas = fila.get("done_recent")
    if concluidas is None or concluidas > 0:
        return False, ""
    janela = fila.get("window_min") or th.get("jobs_stuck_min", 30)
    presas = fila.get("stuck") or 0
    aguardando = fila.get("waiting") or 0
    if presas >= minimo:
        idade = fila.get("oldest_min")
        return True, (f"{presas} execucao(oes) em andamento ha mais de {janela}min"
                      + (f" (a mais antiga ha {idade}min)" if idade is not None else "")
                      + f" e nenhuma conclusao nesses {janela}min")
    if aguardando >= minimo:
        espera = fila.get("waiting_min")
        return True, (f"{aguardando} execucao(oes) com a hora programada vencida sem ninguem "
                      "iniciar" + (f" (a mais velha ha {espera}min)" if espera is not None else "")
                      + f" e nenhuma conclusao em {janela}min")
    return False, ""


def job_problems(jb: dict | None, th: dict, fila_hist: list[dict] | None = None,
                 ja_detectado: dict[str, str] | None = None) -> dict[str, str]:
    """Problemas vindos dos jobs do RM, separados por natureza.

    JOBS e aviso: execucao que termina em erro quase sempre e validacao ou regra
    de negocio da aplicacao. Ja JOBQUEUE (a fila parou de andar) e JOBFAIL (a
    janela inteira falhou, nenhum sucesso) sao infraestrutura - e so esses dois
    viram notificacao.
    """
    p: dict[str, str] = {}
    if not isinstance(jb, dict):
        return p
    if jb.get("error"):
        p["JOBSQL"] = f"nao deu para ler os jobs no SQL: {str(jb['error'])[:120]}"
    falhas, ok = jb.get("failed"), jb.get("ok")
    janela = jb.get("window_min")
    if falhas is not None and falhas >= th.get("jobs_failed", 3):
        p["JOBS"] = (f"{falhas} execucoes de job com erro em {janela}min "
                     "(validacao/regra de negocio, nao falha do servidor)")
    minimo = int(th.get("jobs_all_failed", 10) or 0)
    if minimo and falhas is not None and falhas >= minimo and not ok:
        p["JOBFAIL"] = (f"{falhas} execucoes de job em {janela}min e NENHUMA concluida com "
                        "sucesso: falha em bloco, nao validacao pontual")

    travada, motivo = fila_parada(jb.get("queue"), th)
    # Uma coleta so nao decide: a fila tem de aparecer travada em `jobs_stuck_checks`
    # leituras seguidas. Sem o historico (chamada solta, coleta antiga sem o
    # campo), vale o instantaneo - o mesmo criterio de `fail_streak` no DOWN.
    if travada and fila_hist is not None:
        precisa = max(1, int(th.get("jobs_stuck_checks", 3) or 1))
        travada = (len(fila_hist) >= precisa
                   and all(fila_parada(f, th)[0] for f in fila_hist[:precisa]))
    # RM.Host parado ou host sem contato ja e alerta por si: repetir o mesmo
    # incidente como "fila travada" so duplica a mensagem no celular.
    if travada and any(k == "DOWN" or k.startswith("svc:RM.Host")
                       for k in (ja_detectado or {})):
        travada = False
    if travada:
        p["JOBQUEUE"] = f"fila de jobs travada: {motivo}"
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
                # A fila fica no mesmo bloco `jobs` de proposito: e a mesma
                # coluna jsonb do banco, sem migracao de schema so para isso.
                if isinstance(result.get("jobs"), dict):
                    result["jobs"]["queue"] = jobstats.queue(
                        jb.get("servidor"), th.get("jobs_stuck_min", 30),
                        th.get("jobs_orphan_min", 360))
            coletas.append((server, result))

        for server, result in coletas:
            db.insert_check(server.name, result)
            if result.get("reachable"):
                # O caso comum: uma linha por servidor por ciclo, ~11 mil por
                # dia so de sucesso. Em DEBUG, para nao afogar o resto.
                log.debug("coleta %s -> OK", server.name)
            else:
                log.warning("coleta %s -> FALHA (%s)", server.name, result.get("error"))

        # Veredito do broker depois de gravar: a referencia e o historico de cada
        # host e a estabilidade compara esta coleta com a anterior - as duas
        # perguntas sao para o banco, uma vez por ciclo, nao por servidor.
        ref = broker_reference(th.get("broker_history_days"))
        estado = db.broker_estado()
        filas = db.fila_hist(max(1, int(th.get("jobs_stuck_checks", 3) or 1)))
        for server, result in coletas:
            streak = 0 if result.get("reachable") else db.fail_streak(server.name)
            probs = problems(result, th, streak, ref.get(server.name),
                             estado.get(server.name), filas.get(server.name))
            prev = _last.get(server.name, {})
            new_keys = [k for k in probs if k not in prev]
            gone_keys = [k for k in prev if k not in probs]
            for k in new_keys:
                db.record_alert(server.name, "raised", k, probs[k])
            for k in gone_keys:
                db.record_alert(server.name, "resolved", k, prev[k])
            # Aviso (job com erro, SQL fora do ar) fica registrado e aparece na
            # tela, mas nao vira mensagem: alerta que nao exige acao imediata
            # so ensina o time a ignorar o canal.
            msgs = ["\U0001F534 " + probs[k] for k in new_keys if notifica(k)]
            msgs += ["\U0001F7E2 resolvido: " + prev[k] for k in gone_keys if notifica(k)]
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

    log.warning("tarefa %d: EXECUTANDO em %s -> %s", task["id"], task["server"],
                plano["comando"])
    url = _url_stage(settings, inv, task["id"], prazo + 600)
    res = execution.execute(task, server, inv.winrm, inv.defaults, settings, url)
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
