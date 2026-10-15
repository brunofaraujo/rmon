"""Polling periodico dos servidores e escrita no banco."""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Iterable

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, elastic, execution, inventory, jobstats, manutencao, nlb, notify, packages
from .collector import collect_server
from .collector import jobrunner_settings as collector_jobrunner_settings
from .collector import nlb_window as collector_nlb_window
from .config import Inventory, Settings
from .inventory import collect_inventory

log = logging.getLogger("rmon.scheduler")

# Estado do ultimo alerta por servidor (chave-problema -> texto), para notificar so em transicoes.
_last: dict[str, dict] = {}
# _last nasce vazio a cada start; antes da primeira avaliacao ele e recarregado
# do alerts_log (ver _restaurar_estado).
_restaurado = False
# Por servidor e problema aberto: quando abriu e quando foi avisado pela ultima
# vez (o 'raised' ou o lembrete). Restaurados junto com _last.
_desde: dict[str, dict[str, float]] = {}
_avisado: dict[str, dict[str, float]] = {}


def _agora() -> float:
    """Relogio do scheduler (os testes trocam para simular horas passando)."""
    return time.time()


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
                  "jobs_all_failed": 10,
                  # memoria privada de UM RM.Host.JobRunner, em GB: aviso
                  # precoce e critico (ver jobrunner_problems)
                  "jobrunner_warn_gb": 4, "jobrunner_gb": 8,
                  # critico ainda aberto e lembrado a cada tantas horas (0 desliga)
                  "lembrete_h": 4,
                  # hora do resumo diario dos alertas (-1 desliga)
                  "resumo_hora": 8}

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
    return chave.startswith(("svc:", "jobrunner-crit:")) or chave in CRITICOS


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
        if streak >= need:
            p["DOWN"] = f"sem contato (WinRM) ha {streak} coletas: {(r.get('error') or '')[:100]}"
        # Sem WinRM o resto da coleta e lixo - menos o JobRunner lido do
        # Elasticsearch (app/elastic.py), que chega por outro caminho. E
        # justamente quando o JobRunner estoura o commit que o WinRM do host
        # para de responder: e a hora em que o alerta dele mais importa.
        jr = r.get("jobrunners")
        if isinstance(jr, dict) and jr.get("fonte") == "elastic":
            p.update(jobrunner_problems(jr, th))
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
    p.update(jobrunner_problems(r.get("jobrunners"), th))
    p.update(job_problems(r.get("jobs"), th, fila_hist, p))
    return p


def _gb_limiar(th: dict, chave: str) -> float:
    try:
        return max(0.0, float(th.get(chave) or 0))
    except (TypeError, ValueError):
        return float(DEFAULT_ALERTS[chave])


def jobrunner_limiar_minimo(th: dict) -> float:
    """Menor limiar ligado: a partir dele vale perguntar ao SQL qual e o job."""
    ligados = [g for g in (_gb_limiar(th, "jobrunner_warn_gb"), _gb_limiar(th, "jobrunner_gb")) if g > 0]
    return min(ligados) if ligados else 0.0


def jobrunner_problems(jr: dict | None, th: dict) -> dict[str, str]:
    """RM.Host.JobRunner com memoria privada fora do normal.

    Cada job do RM roda num JobRunner proprio, e no parque nenhum passou de 1 GB
    fora de incidente nos 14 dias medidos. Os quatro estouros conhecidos (06/10
    no srv11 e no .188, 13/10 no .34 e no .190) foram todos o mesmo relatorio
    gerado sem filtro de turma: horas crescendo devagar ate ~7 GB e, de repente,
    dezenas de GB em minutos - o host esgota o commit, os RM.Host caem e o RDP
    para de criar sessao.

    Por isso dois niveis, cada um com sua chave (a escalada notifica de novo, e o
    aviso nao "resolve" quando o critico acende):

    * `jobrunner:<pid>` (aviso, `jobrunner_warn_gb`, 4 GB) - cruzado de 3,5 a 6h
      ANTES da queda nos quatro casos: da tempo de cancelar o job;
    * `jobrunner-crit:<pid>` (vermelho, `jobrunner_gb`, 8 GB) - nos quatro casos
      cruzado a 1min da explosao final: o host cai em 10-30min.

    Zero desliga o nivel. O texto traz o job, quem pediu e o filtro quando o SQL
    do RM responde (scheduler.poll_all grava isso em `jr["jobs"]`).
    """
    p: dict[str, str] = {}
    if not isinstance(jr, dict):
        return p
    aviso = _gb_limiar(th, "jobrunner_warn_gb")
    limite = _gb_limiar(th, "jobrunner_gb")
    jobs = jr.get("jobs") if isinstance(jr.get("jobs"), dict) else {}
    top = jr.get("top")
    for proc in (top if isinstance(top, list) else [top] if isinstance(top, dict) else []):
        if not isinstance(proc, dict) or proc.get("pid") is None or proc.get("private_mb") is None:
            continue
        try:
            gb = float(proc["private_mb"]) / 1024
        except (TypeError, ValueError):
            continue
        pid = proc["pid"]
        alto = bool(aviso) and gb >= aviso
        estouro = bool(limite) and gb >= limite
        if not (alto or estouro):
            continue
        texto = f"RM.Host.JobRunner PID {pid} com {gb:.1f} GB de memoria privada"
        ex = jobs.get(str(pid))
        if isinstance(ex, dict):
            texto += (f": job {ex.get('idjob')} '{ex.get('nome') or ex.get('classe') or '?'}'"
                      f" de {ex.get('usuario') or '?'}, desde {ex.get('inicio') or '?'}")
            if ex.get("progresso") is not None:
                texto += f" ({ex['progresso']}%)"
            if ex.get("filtro"):
                texto += f", filtro {ex['filtro'][:160]}"
        if alto:
            p[f"jobrunner:{pid}"] = (texto + f" - acima de {aviso:g} GB; cancelar o job "
                                     "antes que ele exploda e derrube o host")
        if estouro:
            p[f"jobrunner-crit:{pid}"] = (texto + f" - acima de {limite:g} GB: o commit do "
                                          "host esgota em minutos, cancelar o job AGORA")
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


def _identificar_jobrunners(jr: dict | None, th: dict) -> None:
    """Anota em `jr["jobs"]` qual job roda em cada JobRunner acima do limiar.

    So pergunta ao SQL quando ha processo grande - o caso raro -, entao o ciclo
    normal de coleta nao ganha consulta nenhuma. O resultado fica gravado com a
    coleta, para o painel mostrar o mesmo texto que foi notificado.
    """
    if not isinstance(jr, dict):
        return
    minimo = jobrunner_limiar_minimo(th)
    if not minimo:
        return
    grandes = [p.get("pid") for p in (jr.get("top") or [])
               if isinstance(p, dict) and p.get("pid") is not None
               and (p.get("private_mb") or 0) / 1024 >= minimo]
    if grandes:
        jr["jobs"] = jobstats.executions_by_pid(jr.get("computer"), grandes) or {}


def aplicar_rdp(r: dict, es: dict | None) -> None:
    """Combina as contagens 1149/21/25 do Elasticsearch com as do WinRM.

    O que so uma fonte sabe, vale ela: canal nao coletado no Elastic (o 1149
    de um host que nem grava o evento) ou agente parado ficam com o WinRM;
    host cujo Get-WinEvent nao respondeu fica com o Elastic.

    Quando as duas sabem, vale a leitura que deixa o no MAIS SAUDAVEL: menos
    autenticacoes (`auth`) e mais sessoes (`sess`). E essa contagem que, no
    modo auto, drena o no do NLB - e um atraso de ingestao (5-11 s normais,
    mais com o cluster carregado) pode mostrar o 1149 antes do 21/25 e fazer um
    no saudavel parecer "autentica sem abrir sessao". Fonte secundaria nao
    drena no que a leitura local nao drenaria.

    As duas leituras ficam gravadas (`auth_winrm`/`sess_winrm` e o Elastic em
    `fonte`) para a tela do NLB e para comparar depois.
    """
    n = r.get("nlb") if isinstance(r.get("nlb"), dict) else None
    if n is None:
        if not es or all(es.get(c) is None for c in ("auth", "sess")):
            return
        # host sem WinRM: so as contagens; status do NLB segue desconhecido
        n = r["nlb"] = {"status": None, "cluster": None, "host_id": None,
                        "auth": None, "sess": None, "error": None}
    n["auth_winrm"], n["sess_winrm"] = n.get("auth"), n.get("sess")
    n["auth_elastic"], n["sess_elastic"] = (es or {}).get("auth"), (es or {}).get("sess")
    fonte: dict[str, str] = {}
    for campo, saudavel in (("auth", min), ("sess", max)):
        do_es, do_winrm = (es or {}).get(campo), n.get(campo)
        if do_es is None and do_winrm is None:
            continue
        if do_winrm is None or (do_es is not None and saudavel(do_es, do_winrm) == do_es):
            n[campo] = do_es
            fonte[campo] = "elastic"
        else:
            fonte[campo] = "winrm"
    n["fonte"] = fonte


def _enriquecer_elastic(inv: Inventory, coletas: list[tuple]) -> None:
    """Completa as coletas do ciclo com o Elasticsearch (so leitura, opcional).

    * contagens de RDP dos nos do NLB: Elastic primeiro, WinRM no que faltar;
    * JobRunners de host sem WinRM: a leitura do Elastic substitui a que nao
      veio, e os alertas de 4/8 GB continuam valendo (ver problems()).

    Elastic desligado, fora do ar ou sem dado fresco do host: nada muda, a
    coleta segue so com o WinRM. Nunca levanta excecao.
    """
    cfg = elastic.settings_for(inv.defaults)
    if not elastic.ativo(cfg):
        return
    try:
        comp = db.computadores()
    except Exception:  # noqa: BLE001
        comp = {}
    nomes = {}
    for server, r in coletas:
        jr = r.get("jobrunners") if isinstance(r.get("jobrunners"), dict) else {}
        nomes[server.name] = elastic.nome_es(server, comp.get(server.name) or jr.get("computer"))
    rdp_de = [s for s, _ in coletas if cfg.get("rdp") and getattr(s, "nlb", False)
              and nomes.get(s.name)]
    jr_de = [(s, r) for s, r in coletas if cfg.get("jobrunner") and not r.get("reachable")
             and nomes.get(s.name)]
    if not (rdp_de or jr_de):
        return
    vivos = elastic.frescos(cfg, sorted({nomes[s.name] for s in rdp_de}
                                        | {nomes[s.name] for s, _ in jr_de}))
    if rdp_de:
        contagens = elastic.rdp(cfg, [nomes[s.name] for s in rdp_de if nomes[s.name] in vivos],
                                collector_nlb_window(inv.defaults))
        for server, r in coletas:
            if server in rdp_de:
                aplicar_rdp(r, contagens.get(nomes[server.name]))
    processo = collector_jobrunner_settings(inv.defaults).get("process") or "RM.Host.JobRunner"
    for server, r in jr_de:
        if nomes[server.name] not in vivos:
            continue
        jr = elastic.jobrunners_agora(cfg, nomes[server.name], processo, comp.get(server.name))
        if jr:
            r["jobrunners"] = jr


def transicoes(prev: dict[str, str], probs: dict[str, str],
               r: dict) -> tuple[dict[str, str], list[str], list[str]]:
    """Compara o estado anterior com os problemas desta coleta.

    Devolve (novo estado, chaves que abriram, chaves que resolveram).

    Coleta sem contato nao ve nada alem do proprio DOWN: o servico parado, a
    memoria alta e o broker curto continuam desconhecidos, nao resolvidos. Por
    isso eles seguem abertos ate uma coleta que enxergue o host de novo. Antes,
    cada engasgo do WinRM dava "resolvido" para tudo e, um ciclo depois,
    levantava tudo outra vez.
    """
    atual = dict(probs)
    if not r.get("reachable"):
        for k, texto in prev.items():
            if k != "DOWN":
                atual.setdefault(k, texto)
    novas = [k for k in atual if k not in prev]
    resolvidas = [k for k in prev if k not in atual]
    return atual, novas, resolvidas


def fora_do_monitoramento(chave: str, r: dict) -> bool:
    """O problema sumiu porque o servico saiu da coleta, nao porque voltou?

    Servico tirado do servers.yaml (ou desinstalado, quando era descoberto)
    nao aparece mais na coleta. O alerta dele tem de fechar, mas "resolvido"
    no celular seria mentira: ninguem consertou nada.
    """
    if not chave.startswith("svc:") or not r.get("reachable"):
        return False
    return chave[4:] not in {s.get("name") for s in (r.get("services") or [])}


def _restaurar_estado(servidores: Iterable[str]) -> None:
    """Recarrega do banco os problemas que estavam abertos antes do start.

    Sem isso, o primeiro ciclo depois de um restart comparava contra um estado
    vazio: tudo o que seguia quebrado virava "novo" (notificado de novo, e um
    segundo 'raised' sem 'resolved' na linha do tempo), e o que se resolveu com
    o RMon parado nunca era dado por resolvido. Com o estado restaurado, o
    restart e invisivel: so transicao de verdade notifica.

    Servidor que saiu do inventario nunca mais e avaliado: os problemas dele
    fecham aqui, calados como o servico que sai da coleta.

    Se o banco nao responder, a excecao sobe como a do insert_check e o ciclo
    seguinte tenta de novo - nunca avalia com o estado vazio.
    """
    global _restaurado
    if _restaurado:
        return
    abertos = db.open_alerts()
    tempos = db.open_alerts_tempos()
    ativos = set(servidores)
    for nome, probs in abertos.items():
        if nome not in ativos:
            for k, texto in probs.items():
                db.record_alert(nome, "resolved", k, texto + " (saiu do monitoramento)")
            continue
        _last.setdefault(nome, dict(probs))
        for k, t in (tempos.get(nome) or {}).items():
            _desde.setdefault(nome, {}).setdefault(k, t["desde"])
            _avisado.setdefault(nome, {}).setdefault(k, t["aviso"])
    _restaurado = True
    log.info("estado dos alertas restaurado: %d problema(s) aberto(s) em %d servidor(es)",
             sum(len(p) for p in abertos.values()), len(abertos))


def _duracao(seg: float) -> str:
    seg = max(0, int(seg))
    d, r = divmod(seg, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d{h:02d}h"
    return f"{h}h{m:02d}" if h else f"{m}min"


def lembretes_devidos(atual: dict[str, str], avisado: dict[str, float],
                      agora: float, intervalo_s: float) -> list[str]:
    """Criticos abertos cujo ultimo aviso ja tem `intervalo_s` ou mais.

    So o vermelho que notifica merece lembrete: e o que exige alguem agindo, e
    um servico parado ha 6h sem ninguem olhar e pior do que o alerta das 6h
    atras faz parecer. Aviso (ambar) fica para o resumo diario.
    """
    if intervalo_s <= 0:
        return []
    return [k for k in atual
            if critico(k) and notifica(k) and k in avisado
            and agora - avisado[k] >= intervalo_s]


def _fechar_manutencao(nome: str, host: str, janela: dict, agora: float) -> None:
    """Fim da janela: uma mensagem so, com o que seguiu aberto."""
    abertos = {k: v for k, v in (_last.get(nome) or {}).items() if notifica(k)}
    db.record_alert(nome, "manutencao", "MANUT",
                    f"fim da manutencao aberta em {manutencao.hora(janela.get('desde'))}: "
                    + (f"{len(abertos)} problema(s) seguem abertos" if abertos else "sem pendencias"))
    for k in abertos:
        # a mensagem abaixo e o aviso: o relogio do lembrete recomeca aqui
        _avisado.setdefault(nome, {})[k] = agora
    if not notify.enabled():
        return
    cab = (f"\U0001F527 RMonitor — {nome} ({host}): fim da manutencao aberta por "
           f"{janela.get('por') or '?'} em {manutencao.hora(janela.get('desde'))}")
    if abertos:
        notify.send(cab + "\nSeguem abertos:\n" + "\n".join(
            ("\U0001F534 " if critico(k) else "⚠ ") + v for k, v in abertos.items()))
    else:
        notify.send(cab + "\nNenhum problema aberto: voltou limpo.")


def poll_all(inv: Inventory, settings: Settings) -> None:
    if not inv.servers:
        return
    _restaurar_estado(s.name for s in inv.servers)
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

        # Elasticsearch (opcional): contagens de RDP do NLB e JobRunner de host
        # sem WinRM. Antes de gravar, para o painel e o alerta verem o mesmo dado.
        try:
            _enriquecer_elastic(inv, coletas)
        except Exception:  # noqa: BLE001
            log.exception("Elasticsearch: falha ao completar a coleta (seguindo pelo WinRM)")
        for server, result in coletas:
            _identificar_jobrunners(result.get("jobrunners"), th)

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
        agora = _agora()
        janelas = manutencao.todas()
        # Janela vencida ainda cala neste ciclo: quem fala por ela e a mensagem
        # de fim de manutencao, logo abaixo. Senao o "resolvido" de algo que
        # abriu calado durante a janela chegaria solto, sem o alerta de antes.
        calar = set(janelas)
        try:
            intervalo = max(0.0, float(th.get("lembrete_h") or 0)) * 3600
        except (TypeError, ValueError):
            intervalo = 0.0
        for server, result in coletas:
            nome = server.name
            streak = 0 if result.get("reachable") else db.fail_streak(nome)
            probs = problems(result, th, streak, ref.get(nome),
                             estado.get(nome), filas.get(nome))
            prev = _last.get(nome, {})
            atual, new_keys, gone_keys = transicoes(prev, probs, result)
            calados = {k for k in gone_keys if fora_do_monitoramento(k, result)}
            desde = _desde.setdefault(nome, {})
            avisado = _avisado.setdefault(nome, {})
            for k in new_keys:
                db.record_alert(nome, "raised", k, atual[k])
                desde[k] = avisado[k] = agora
            for k in gone_keys:
                db.record_alert(nome, "resolved", k,
                                prev[k] + (" (saiu do monitoramento)" if k in calados else ""))
                desde.pop(k, None)
                avisado.pop(k, None)
            _last[nome] = atual
            if nome in calar:
                continue
            # Aviso (job com erro, SQL fora do ar) fica registrado e aparece na
            # tela, mas nao vira mensagem: alerta que nao exige acao imediata
            # so ensina o time a ignorar o canal.
            msgs = ["\U0001F534 " + atual[k] for k in new_keys if notifica(k)]
            msgs += ["\U0001F7E2 resolvido: " + prev[k] for k in gone_keys
                     if notifica(k) and k not in calados]
            for k in lembretes_devidos(atual, avisado, agora, intervalo):
                aberto = _duracao(agora - desde.get(k, avisado[k]))
                db.record_alert(nome, "reminder", k, f"segue aberto ha {aberto}: {atual[k]}")
                avisado[k] = agora
                msgs.append(f"⏰ segue aberto ha {aberto}: {atual[k]}")
            if msgs and notify.enabled():
                notify.send(f"RMonitor — {nome} ({server.host})\n" + "\n".join(msgs))

        fechadas = manutencao.vencidas(agora, janelas)
        hosts = {s.name: s.host for s in inv.servers}
        for nome, janela in fechadas.items():
            if nome in hosts:
                _fechar_manutencao(nome, hosts[nome], janela, agora)
        if fechadas:
            manutencao.remover(list(fechadas), agora)

        # Drenagem/reingresso do NLB: depois dos alertas, com as coletas deste
        # ciclo. Nunca derruba a coleta - erro aqui vira log. Host em
        # manutencao fica em hold: a automacao nao mexe em quem esta sendo mexido.
        try:
            nlb.run_cycle(inv, {s.name: r for s, r in coletas},
                          hold=manutencao.ativas(agora, janelas))
        except Exception:  # noqa: BLE001
            log.exception("NLB: falha no ciclo de avaliacao")


def texto_resumo(abertos: dict[str, dict[str, str]], tempos: dict[str, dict[str, dict]],
                 janelas: dict[str, dict], ultimas24: list[dict], servidores: list[str],
                 agora: float, elastic_mudos: list[str] | None = None) -> str:
    """Resumo diario em texto: o que esta aberto (vermelho e ambar), quem esta em
    manutencao e o movimento das ultimas 24h.

    E o lugar dos avisos que nao notificam (job com erro, SQL fora, memoria
    alta que nunca vira incidente): eles ficam visiveis uma vez por dia, sem
    ensinar o time a ignorar o canal. E tambem o sinal de vida do monitor - se
    o resumo da manha nao chegou, o RMon parou.
    """
    def linhas(criticos: bool) -> list[str]:
        out = []
        for nome in servidores:
            for k, texto in sorted((abertos.get(nome) or {}).items()):
                if critico(k) != criticos:
                    continue
                t = (tempos.get(nome) or {}).get(k) or {}
                ha = f" (ha {_duracao(agora - t['desde'])})" if t.get("desde") else ""
                out.append(f"• {nome}: {texto[:160]}{ha}")
        return out

    def bloco(titulo: str, itens: list[str], limite: int = 15) -> list[str]:
        if not itens:
            return []
        extra = [f"... e mais {len(itens) - limite}"] if len(itens) > limite else []
        return [f"{titulo} ({len(itens)}):"] + itens[:limite] + extra

    partes = [f"\U0001F4CB RMonitor — resumo diario {time.strftime('%d/%m', time.localtime(agora))}"]
    crit, avisos = linhas(True), linhas(False)
    partes += bloco("\U0001F534 Criticos abertos", crit)
    partes += bloco("⚠ Avisos abertos", avisos)
    if not crit and not avisos:
        partes.append("✅ Nenhum problema aberto.")
    ativas = manutencao.ativas(agora, janelas)
    if ativas:
        partes.append("\U0001F527 Em manutencao: " + ", ".join(
            f"{n} ate {manutencao.hora(j.get('ate'))}" for n, j in sorted(ativas.items())))
    if elastic_mudos:
        partes.append("📡 Elastic sem dados de: " + ", ".join(elastic_mudos)
                      + " (agente parado: serie e RDP desses hosts so pelo WinRM)")
    abriram = sum(1 for a in ultimas24 if a.get("kind") == "raised")
    resolveram = sum(1 for a in ultimas24 if a.get("kind") == "resolved")
    partes.append(f"Ultimas 24h: {abriram} alerta(s) aberto(s), {resolveram} resolvido(s).")
    return "\n".join(partes)


def _elastic_mudos(inv: Inventory, agora: float) -> list[str]:
    """Servidores cujo agente do Elastic parou de mandar dado (ou nunca mandou
    em 7 dias). Agente morto passa despercebido - foi assim no srv06n em 14/10 -
    e com ele somem a serie do JobRunner e as contagens de RDP do Elastic."""
    cfg = elastic.settings_for(inv.defaults)
    if not elastic.ativo(cfg):
        return []
    try:
        comp = db.computadores()
    except Exception:  # noqa: BLE001
        return []
    nomes = {s.name: elastic.nome_es(s, comp.get(s.name)) for s in inv.servers}
    nomes = {k: v for k, v in nomes.items() if v}
    if not nomes:
        return []
    vistos = elastic.vivos(cfg, sorted(set(nomes.values())), "now-7d")
    if not vistos and elastic.erro_atual():
        return []  # o Elastic e que esta fora: nao acusa os agentes
    limite = float(cfg.get("max_age_sec") or 180)
    mudos = []
    for srv, nome in sorted(nomes.items()):
        ts = vistos.get(nome)
        if ts is None:
            mudos.append(f"{srv} (nada em 7 dias)")
        elif agora - ts > limite:
            mudos.append(f"{srv} (desde {manutencao.hora(ts)})")
    return mudos


def resumo_diario(inv: Inventory, forcar: bool = False) -> bool:
    """Roda de hora em hora e manda o resumo na hora configurada, uma vez por dia.

    A hora vem dos limiares (painel > YAML > padrao), entao muda sem restart.
    A marca do ultimo envio fica no banco: restart dentro da hora nao repete.
    """
    th = {**DEFAULT_ALERTS, **(inv.defaults.get("alerts") or {}), **(db.get_config("alerts", {}) or {})}
    agora = _agora()
    try:
        hora = int(th.get("resumo_hora", 8))
    except (TypeError, ValueError):
        hora = 8
    hoje = time.strftime("%Y-%m-%d", time.localtime(agora))
    if not forcar:
        if hora < 0 or time.localtime(agora).tm_hour != hora:
            return False
        if db.get_config("resumo_ultimo") == hoje:
            return False
    if not notify.enabled():
        return False
    nomes = [s.name for s in inv.servers]
    texto = texto_resumo(db.open_alerts(), db.open_alerts_tempos(), manutencao.todas(),
                         [a for a in db.recent_alerts(24, 5000) if a.get("server") in nomes],
                         nomes, agora, _elastic_mudos(inv, agora))
    notify.send(texto)
    db.set_config("resumo_ultimo", hoje)
    log.info("resumo diario enviado")
    return True


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
    # De hora em hora: resumo_diario decide se e a hora configurada.
    sched.add_job(resumo_diario, "cron", minute=0, args=[inv], id="resumo_diario",
                  max_instances=1, coalesce=True)
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
