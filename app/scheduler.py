"""Polling periodico dos servidores e escrita no banco."""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Iterable

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, elastic, execution, fontes, inventory, jobstats, manutencao, nlb, notify, packages
from . import reinicio
from . import nomes as exibicao
from .collector import SEM_RESPOSTA, collect_server, kill_jobrunner, subir_servicos
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
# Partidas automaticas pos-boot de cada (servidor, problema), epoch de cada uma
_tentado: dict[tuple[str, str], list[float]] = {}
# Boot de cada servidor visto no ciclo anterior (epoch), para a hora nao oscilar
_boot: dict[str, float] = {}
# Ultima leitura de cada JobRunner por servidor ("pid|inicio" -> (epoch, MB)),
# para medir quanto ele cresceu desde a coleta anterior (jobrunners_a_encerrar)
_jr_amostra: dict[str, dict[str, tuple[float, float]]] = {}
# Processos que o RMon tentou e NAO conseguiu encerrar ("servidor|pid|inicio"):
# a falha vai ao canal uma vez; as repeticoes ficam so no log e na auditoria
_jr_falhou: set[str] = set()


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
                  # encerramento automatico do JobRunner (jobrunners_a_encerrar):
                  # a partir de tantos GB, ou crescendo tantos MB/min ja acima de
                  # tantos MB. 0 desliga cada regra.
                  "jobrunner_kill_gb": 4, "jobrunner_kill_mb_min": 400,
                  "jobrunner_kill_min_mb": 768,
                  # critico ainda aberto e lembrado a cada tantas horas (0 desliga)
                  "lembrete_h": 4,
                  # hora do resumo diario dos alertas (-1 desliga)
                  "resumo_hora": 8,
                  # servico automatico parado cujo alerta abriu ate tantos min
                  # depois do boot "nao subiu apos o reboot" (ver marcar_pos_boot)
                  "boot_janela_min": 30,
                  # guardiao (iniciar_automatico): servico monitorado parado volta
                  # sozinho, a qualquer hora, sem limite de tentativas. Nada antes
                  # de boot_iniciar_min de uptime (0 desliga: so avisa); entre as
                  # tentativas, 2, 5, 10, 15 min e dai de teto em teto.
                  "boot_iniciar_min": 10, "autocorrecao_teto_min": 30}

# Problemas que existem para serem VISTOS no painel, nao empurrados no celular.
# Job que termina com erro e validacao/regra de negocio da aplicacao (dado
# errado no cadastro, competencia fechada...), nao falha do servidor: quem
# resolve e o usuario que pediu o job, no horario dele. O que continua acordando
# alguem e a fila travada (JOBQUEUE) e a falha em bloco (JOBFAIL), que sao
# problema de infraestrutura.
AVISOS = frozenset({"JOBS", "JOBSQL"})

# Uptime abaixo do qual agente de coleta parado nao e problema (partida atrasada)
AGENTE_CARENCIA_S = 10 * 60

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
                    p[chave] = f"{nome} não existe em {b.get('path')} com o serviço no ar"
            elif donos:
                quem = ", ".join(donos[:3]) + ("..." if len(donos) > 3 else "")
                p[chave] = f"{nome} não existe em {b.get('path')} com {quem} no ar"
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
            p[chave] = (f"{nome} com {_kb(tamanho)} (mínimo esperado {_kb(min_kb * 1024)}): "
                        "cache truncado, customizações não carregam")
        elif maior and min_pct and tamanho < maior * min_pct / 100:
            p[chave] = (f"{nome} com {_kb(tamanho)}, contra {_kb(maior)} que este host já teve: "
                        "broker incompleto, customizações não carregam")
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
    if s.get("src") in ("auto", "agente"):
        return str(s.get("start") or "").strip().lower().startswith("auto")
    return True


def chave_servico(s: dict) -> str:
    """Chave do problema de um servico fora do ar. Agente de coleta (Elastic
    Agent) tem chave propria: e ambar - o RM segue funcionando sem ele, so o
    historico do Elastic fica cego - e o RMon o sobe sozinho a qualquer hora."""
    return ("agente:" if s.get("src") == "agente" else "svc:") + str(s.get("name"))


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


def marcar_pos_boot(r: dict, desde: dict[str, float], agora: float, janela_s: float,
                    boot_anterior: float | None = None) -> float | None:
    """Marca na coleta (`nao_subiu` = epoch do boot) o servico automatico que
    nao subiu depois do reboot.

    rh-web-248, 15/10/2026: o Windows Update reiniciou o host e, no boot, o
    RM.Host.Service estourou os 30 s que o Windows da para um servico partir.
    A recuperacao do servico (reiniciar) so age quando ele trava depois de
    rodar, nunca em falha de partida - entao ficou parado a noite toda, e o
    alerta dizia so "= Stopped".

    Conta como "nao subiu" o servico com inicio automatico, fora do ar, cujo
    alerta abriu antes do boot ou ate `janela_s` depois dele (`desde`, o
    'raised' de cada problema; quem nao tem alerta ainda abre agora). O que
    caiu horas depois do boot e outra historia e fica sem a marca. A marca vai
    gravada com a coleta para painel, mural e alerta dizerem a mesma coisa.

    O uptime e lido no inicio da coleta e `agora` vem no fim do ciclo, entao a
    conta oscila alguns segundos. Com `boot_anterior` a menos de 5 min, vale o
    anterior: senao o "18:24" do alerta viraria "18:25" no ciclo seguinte.
    Devolve o boot usado (None sem contato ou sem uptime).
    """
    up = r.get("uptime_sec")
    if not r.get("reachable") or up is None:
        return None
    try:
        boot = agora - float(up)
    except (TypeError, ValueError):
        return None
    if boot_anterior is not None and abs(boot - boot_anterior) < 300:
        boot = boot_anterior
    servicos = []
    for s in r.get("services") or []:
        automatico = str(s.get("start") or "").strip().lower().startswith("auto")
        if (automatico and service_down(s)
                and desde.get(chave_servico(s), agora) <= boot + janela_s):
            s = {**s, "nao_subiu": int(boot)}
        servicos.append(s)
    r["services"] = servicos
    return boot


# Estado do servico (Win32_Service) em portugues, para a tela e as mensagens
STATUS_SERVICO = {"Running": "em execução", "Stopped": "parado", "StartPending": "iniciando",
                  "StopPending": "parando", "Paused": "pausado", "PausePending": "pausando",
                  "ContinuePending": "retomando", "NOT_FOUND": "não encontrado no servidor"}


def status_servico(st) -> str:
    return STATUS_SERVICO.get(str(st), str(st or "desconhecido"))


def texto_servico(s: dict) -> str:
    tipo = "agente de coleta" if s.get("src") == "agente" else "serviço"
    texto = f"{tipo} {s['name']} {status_servico(s.get('status'))}"
    if s.get("nao_subiu"):
        texto += f" (início automático: não subiu após o reboot de {manutencao.hora(s['nao_subiu'])})"
    return texto


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
            p["DOWN"] = f"sem contato (WinRM) há {streak} coletas: {(r.get('error') or '')[:100]}"
        # Sem WinRM o resto da coleta e lixo - menos o JobRunner lido do
        # Elasticsearch (app/elastic.py), que chega por outro caminho. E
        # justamente quando o JobRunner estoura o commit que o WinRM do host
        # para de responder: e a hora em que o alerta dele mais importa.
        jr = r.get("jobrunners")
        if isinstance(jr, dict) and jr.get("fonte") == "elastic":
            p.update(jobrunner_problems(jr, th))
        return p
    up = r.get("uptime_sec")
    for s in r.get("services") or []:
        if service_down(s):
            # Agente com partida atrasada so sobe ~2 min depois do boot: parado
            # antes disso e o Windows ainda nao chegou nele, nao defeito.
            if s.get("src") == "agente" and up is not None and float(up) < AGENTE_CARENCIA_S:
                continue
            p[chave_servico(s)] = texto_servico(s)
    if r.get("app_ok") is False:
        p["APP"] = "a aplicação RM não respondeu ao teste HTTP"
    mem = r.get("mem_pct")
    if mem is not None and mem >= th["mem_pct"]:
        p["MEM"] = f"memória em {mem}%"
    disks = r.get("disks") or []
    main = next((d for d in disks if str(d.get("drive", "")).upper().startswith("C")), disks[0] if disks else None)
    if main and (main.get("used_pct") or 0) >= th["disk_pct"]:
        p["DISK"] = f"disco {main.get('drive')} em {main.get('used_pct')}%"
    if r.get("app_ok") is True and r.get("app_ms") is not None and r["app_ms"] > th["app_ms"]:
        p["APPSLOW"] = f"aplicação RM lenta: respondeu em {r['app_ms']} ms"
    commit = r.get("commit_pct")
    if commit is not None and commit >= th.get("commit_pct", 90):
        p["COMMIT"] = (f"memória reservada em {commit}% do limite (RAM + arquivo de paginação): "
                       "nesse ponto o RM.Host falha ao gerar o broker (0x800705AF)")
    p.update(broker_problems(r, th, broker_ref, broker_estavel))
    p.update(jobrunner_problems(r.get("jobrunners"), th))
    p.update(job_problems(r.get("jobs"), th, fila_hist, p))
    # divergencias do padrao dos outros servidores (padrao_cluster, gravado na coleta)
    if isinstance(r.get("padrao"), dict):
        p.update({k: str(v) for k, v in r["padrao"].items() if str(k).startswith("PADRAO:")})
    return p


def _minutos(th: dict, chave: str) -> float:
    try:
        return max(0.0, float(th.get(chave) or 0))
    except (TypeError, ValueError):
        return float(DEFAULT_ALERTS[chave])


def _tentativas(nome: str, chave: str, boot: float) -> list[float]:
    """Partidas automaticas deste servico feitas depois deste boot."""
    feitas = _tentado.get((nome, chave))
    if feitas is None:
        feitas = db.acoes_desde(nome, chave, boot)
        _tentado[(nome, chave)] = feitas
    return [t for t in feitas if t >= boot]


# Tentativas que vao para o canal: as primeiras, as que deram certo e uma a
# cada tantas depois (as outras ficam so na linha do tempo, sem afogar ninguem).
_AVISAR_ATE = 3
_AVISAR_CADA = 6
# Espera depois da n-esima tentativa que falhou (min); a ultima se repete, limitada
# pelo teto (autocorrecao_teto_min).
_ESCADA_MIN = (2, 5, 10, 15, 30)
# Servico em transicao (StopPending/StartPending) e deixado assentar; preso nisso
# por mais que tanto, e reiniciado (o restart mata o processo travado).
_PENDENTE_S = 5 * 60


def _espera_s(n: int, teto_s: float) -> float:
    """Espera antes da proxima tentativa, depois de `n` tentativas feitas."""
    return min(_ESCADA_MIN[min(max(n, 1), len(_ESCADA_MIN)) - 1] * 60, teto_s)


def iniciar_automatico(server, inv: Inventory, r: dict, apos_s: float, agora: float,
                       teto_s: float = 1800, desde: dict[str, float] | None = None,
                       atual: dict[str, str] | None = None) -> list[str]:
    """Guardiao: todo servico monitorado fora do ar volta sozinho.

    Vale para qualquer servico que o RMon vigia no host (fixos do inventario,
    RM.Host* descobertos, Elastic Agent), a QUALQUER hora - depois de reboot,
    de queda, de alguem ter parado sem janela de manutencao. Ninguem depende do
    Windows para isso: a recuperacao do servico ("reiniciar") nao age quando
    ele estoura os 30 s na partida (.248 em 15/10, .188 em 16/10).

    - parado (inicio automatico ou servico fixo do inventario): start;
    - agente no ar mas mudo para o Elastic (PADRAO:elastic): restart.

    Fica de fora so o que foi desligado de proposito (Desabilitado), o que nem
    existe no host e o host em manutencao (quem chama ja pula). Nada antes de
    `apos_s` de uptime, para o boot assentar. Servico em transicao (parando ou
    subindo) e deixado assentar; preso nisso por 5 min, e reiniciado. NAO
    desiste: a 1a tentativa e na hora e as seguintes esperam 2, 5, 10, 15 min e
    dai `teto_s` (30) - o que travou por uma hora no boot (o .188 em 16/10) volta
    logo que o host liberar. Host que nao respondeu ao comando nao conta
    tentativa: repete no ciclo seguinte.

    Todos os alvos do host vao numa chamada so (collector.subir_servicos), que
    dispara e confere sem prender a coleta. Cada tentativa vai para a linha do
    tempo ('acao'); para o canal, as 3 primeiras, as que deram certo e uma a
    cada 6.
    """
    if apos_s <= 0 or not r.get("reachable") or float(r.get("uptime_sec") or 0) < apos_s:
        return []
    desde = desde or {}
    atual = atual or {}
    servicos = [s for s in r.get("services") or [] if s.get("name")]
    vistos = [s["name"] for s in servicos]
    alvos = []   # (servico, chave, acao, referencia do episodio, motivo)
    for s in servicos:
        chave = chave_servico(s)
        inicio = str(s.get("start") or "").strip().lower()
        if s.get("src") == "agente" and s.get("status") == "Running" and "PADRAO:elastic" in atual:
            ref = desde.get("PADRAO:elastic", agora)
            alvos.append((s, "PADRAO:elastic", "restart", ref,
                          f"no ar, mas sem mandar dados ao Elastic desde {manutencao.hora(ref)}"))
        elif (service_down(s) and s.get("status") not in ("NOT_FOUND", None)
              and not inicio.startswith("disab")):
            st = str(s.get("status"))
            acao = "start"
            if "Pending" in st:
                if agora - desde.get(chave, agora) < _PENDENTE_S:
                    continue         # o Windows ainda esta mexendo nele
                acao = "restart"
            if s.get("nao_subiu"):
                ref = float(s["nao_subiu"])
                motivo = f"não subiu após o reboot de {manutencao.hora(s['nao_subiu'])}"
            else:
                ref = desde.get(chave, agora)
                motivo = (f"preso em {st} desde {manutencao.hora(ref)}" if acao == "restart"
                          else f"parado desde {manutencao.hora(ref)}")
            alvos.append((s, chave, acao, ref, motivo))
    agora_vai = []
    for s, chave, acao, ref, motivo in alvos:
        try:
            feitas = _tentativas(server.name, chave, ref)
        except Exception:  # noqa: BLE001 - sem saber o que ja foi feito, nao tenta
            log.exception("guardiao: sem como ler as tentativas de %s/%s", server.name, chave)
            continue
        if feitas and agora - feitas[-1] < _espera_s(len(feitas), teto_s):
            continue
        _tentado[(server.name, chave)].append(agora)
        agora_vai.append((s, chave, acao, motivo, len(feitas) + 1))
    if not agora_vai:
        return []
    resultado = subir_servicos(server, inv.winrm, [(acao, s["name"]) for s, _, acao, _, _ in agora_vai],
                               allowed=vistos)
    msgs = []
    for s, chave, acao, motivo, n in agora_vai:
        ok, retorno = resultado.get(s["name"], (False, SEM_RESPOSTA + "?"))
        if retorno.startswith(SEM_RESPOSTA):
            # o host nao respondeu: nada foi tentado, repete no proximo ciclo
            _tentado[(server.name, chave)].pop()
            log.info("guardiao %s/%s: %s (repete no próximo ciclo)", server.name, s["name"], retorno)
            continue
        verbo = "reiniciando" if acao == "restart" else "iniciando"
        texto = f"RMon {verbo} {s['name']} automaticamente ({n}ª tentativa; {motivo}): {retorno}"
        if not ok:
            texto += f" - nova tentativa em {int(_espera_s(n, teto_s) // 60)} min, sem desistir"
        db.record_alert(server.name, "acao", chave, texto)
        log.info("guardiao %s/%s %s #%d -> %s (%s)", server.name, s["name"], acao, n, ok, retorno)
        if ok or n <= _AVISAR_ATE or n % _AVISAR_CADA == 0:
            msgs.append("🔧 " + texto)
    return msgs


def _limiar(th: dict, chave: str) -> float:
    """Limiar numerico de `th` (GB, MB...), nunca negativo; invalido = o padrao."""
    try:
        return max(0.0, float(th.get(chave) or 0))
    except (TypeError, ValueError):
        return float(DEFAULT_ALERTS[chave])


def _procs(jr) -> list:
    """Os processos de `jobrunners.top`: lista, ou um so (o PowerShell achata)."""
    top = jr.get("top") if isinstance(jr, dict) else None
    return top if isinstance(top, list) else [top] if isinstance(top, dict) else []


def jobrunner_limiar_minimo(th: dict) -> float:
    """Menor limiar ligado: a partir dele vale perguntar ao SQL qual e o job."""
    ligados = [g for g in (_limiar(th, "jobrunner_warn_gb"), _limiar(th, "jobrunner_gb")) if g > 0]
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
    aviso = _limiar(th, "jobrunner_warn_gb")
    limite = _limiar(th, "jobrunner_gb")
    jobs = jr.get("jobs") if isinstance(jr.get("jobs"), dict) else {}
    for proc in _procs(jr):
        if not isinstance(proc, dict) or proc.get("pid") is None or proc.get("private_mb") is None:
            continue
        if proc.get("encerrado"):
            continue         # o RMon ja o encerrou neste ciclo (encerrar_jobrunners)
        try:
            gb = float(proc["private_mb"]) / 1024
        except (TypeError, ValueError):
            continue
        pid = proc["pid"]
        alto = bool(aviso) and gb >= aviso
        estouro = bool(limite) and gb >= limite
        if not (alto or estouro):
            continue
        texto = f"RM.Host.JobRunner PID {pid} com {gb:.1f} GB de memória privada"
        ex = jobs.get(str(pid))
        if isinstance(ex, dict):
            texto += (f": job {ex.get('idjob')} '{ex.get('nome') or ex.get('classe') or '?'}'"
                      f" de {ex.get('usuario') or '?'}, desde {ex.get('inicio') or '?'}")
            if ex.get("progresso") is not None:
                texto += f" ({ex['progresso']}%)"
            if ex.get("filtro"):
                texto += f", filtro {ex['filtro'][:160]}"
        if alto:
            p[f"jobrunner:{pid}"] = (texto + f" - acima de {aviso:g} GB: cancele o job "
                                     "antes que ele derrube o servidor")
        if estouro:
            p[f"jobrunner-crit:{pid}"] = (texto + f" - acima de {limite:g} GB: a memória do "
                                          "servidor esgota em minutos, cancele o job AGORA")

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
        return True, (f"{presas} execução(ões) em andamento há mais de {janela}min"
                      + (f" (a mais antiga há {idade}min)" if idade is not None else "")
                      + f" e nenhuma conclusão nesses {janela}min")
    if aguardando >= minimo:
        espera = fila.get("waiting_min")
        return True, (f"{aguardando} execução(ões) com a hora programada vencida sem ninguém "
                      "iniciar" + (f" (a mais velha há {espera}min)" if espera is not None else "")
                      + f" e nenhuma conclusão em {janela}min")
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
        p["JOBSQL"] = f"não deu para ler os jobs no SQL: {str(jb['error'])[:120]}"
    falhas, ok = jb.get("failed"), jb.get("ok")
    janela = jb.get("window_min")
    if falhas is not None and falhas >= th.get("jobs_failed", 3):
        p["JOBS"] = (f"{falhas} execuções de job com erro em {janela}min "
                     "(validação/regra de negócio, não falha do servidor)")
    minimo = int(th.get("jobs_all_failed", 10) or 0)
    if minimo and falhas is not None and falhas >= minimo and not ok:
        p["JOBFAIL"] = (f"{falhas} execuções de job em {janela}min e NENHUMA concluída com "
                        "sucesso: falha em bloco, não validação pontual")

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
    ja = jr.get("jobs") or {}
    grandes = [p.get("pid") for p in _procs(jr)
               if isinstance(p, dict) and p.get("pid") is not None
               and str(p.get("pid")) not in ja
               and (p.get("private_mb") or 0) / 1024 >= minimo]
    if grandes:
        # merge: o job de quem o RMon acabou de encerrar ja veio identificado
        jr["jobs"] = {**(jobstats.executions_by_pid(jr.get("computer"), grandes) or {}),
                      **(jr.get("jobs") or {})}


def _jr_chave(proc: dict) -> str:
    return f"{proc.get('pid')}|{proc.get('start') or ''}"


def jobrunners_a_encerrar(jr: dict | None, anterior: dict[str, tuple[float, float]] | None,
                          th: dict, agora: float) -> list[tuple[dict, str]]:
    """JobRunners que o RMon encerra sozinho: [(processo, motivo)]. Funcao pura.

    Em 19/10 o mesmo relatorio derrubou o .222 e o .188 em minutos, sem as
    horas de crescimento lento dos casos de 06 e 13/10: o PID 7760 foi de 373 MB
    a 4,2 GB entre duas coletas e o host parou de responder na seguinte; o 2172
    passou de 340 a 854 MB em um minuto e o .222 travou logo depois. O aviso
    chegou, mas nao deu tempo de ninguem agir - e sem WinRM o botao some.

    Duas regras, cada uma desligada com 0:

    * `jobrunner_kill_gb` (4): memoria privada a partir disso;
    * `jobrunner_kill_mb_min` (400) com `jobrunner_kill_min_mb` (768): cresceu
      pelo menos tanto por minuto desde a coleta anterior (o mesmo processo:
      PID e hora de inicio) e ja passa do minimo. Em 5 dias de historico do
      parque, os saltos rapidos normais (ate 441 MB/min) sao carga inicial e
      param abaixo de 500 MB; so os dois de 19/10 cresceram assim acima de
      800 MB.

    Leitura do Elastic (host sem WinRM) nao entra: sem WinRM nao ha como encerrar.
    """
    if not isinstance(jr, dict) or jr.get("fonte") == "elastic":
        return []
    limite_gb = _limiar(th, "jobrunner_kill_gb")
    ritmo = _limiar(th, "jobrunner_kill_mb_min")
    piso_mb = _limiar(th, "jobrunner_kill_min_mb")
    anterior = anterior or {}
    alvos = []
    for proc in _procs(jr):
        if not isinstance(proc, dict) or proc.get("pid") is None or proc.get("private_mb") is None:
            continue
        try:
            mb = float(proc["private_mb"])
        except (TypeError, ValueError):
            continue
        if limite_gb and mb / 1024 >= limite_gb:
            alvos.append((proc, f"{mb / 1024:.1f} GB de memória privada (limite: {limite_gb:g} GB)"))
            continue
        antes = anterior.get(_jr_chave(proc))
        if not (ritmo and antes and mb >= piso_mb):
            continue
        minutos = (agora - antes[0]) / 60
        if not 0 < minutos <= 5:
            continue
        por_min = (mb - antes[1]) / max(minutos, 0.5)
        if por_min >= ritmo:
            alvos.append((proc, f"cresceu de {antes[1]:.0f} para {mb:.0f} MB em "
                                f"{max(1, round(minutos))} min ({por_min:.0f} MB/min; limite: "
                                f"{ritmo:g} MB/min acima de {piso_mb:g} MB)"))
    return alvos


def encerrar_jobrunners(server, inv: Inventory, r: dict, th: dict, agora: float) -> list[str]:
    """Encerra no host, logo depois da coleta, o JobRunner que esta estourando.

    Roda no mesmo worker da coleta do servidor, antes de esperar os outros: no
    .222 o WinRM durou um minuto depois da ultima leitura boa. Usa o mesmo
    caminho do botao da tela (collector.kill_jobrunner: confere PID, nome e hora
    de inicio antes de matar). O processo encerrado fica marcado na coleta
    (`encerrado`), sai dos alertas de 4/8 GB e vai para a linha do tempo, a
    auditoria e o canal com o job e quem o pediu. O RM perde aquela execucao.
    """
    jr = r.get("jobrunners") if isinstance(r.get("jobrunners"), dict) else None
    if not r.get("reachable") or not jr:
        return []
    anterior = _jr_amostra.get(server.name) or {}
    procs = [p for p in _procs(jr) if isinstance(p, dict) and p.get("pid") is not None and p.get("private_mb") is not None]
    _jr_amostra[server.name] = {_jr_chave(p): (agora, float(p["private_mb"])) for p in procs}
    alvos = jobrunners_a_encerrar(jr, anterior, th, agora)
    if not alvos:
        return []
    try:
        jobs = jobstats.executions_by_pid(jr.get("computer"), [p["pid"] for p, _ in alvos]) or {}
    except Exception:  # noqa: BLE001 - sem o nome do job, encerra do mesmo jeito
        jobs = {}
    if jobs:
        jr["jobs"] = {**(jr.get("jobs") or {}), **jobs}
    processo = collector_jobrunner_settings(inv.defaults).get("process") or "RM.Host.JobRunner"
    msgs = []
    for proc, motivo in alvos:
        pid = int(proc["pid"])
        ex = jobs.get(str(pid)) or {}
        quem = (f"job {ex.get('idjob')} '{ex.get('nome') or ex.get('classe') or '?'}' de "
                f"{ex.get('usuario') or '?'}" if ex else "job não identificado")
        ok, retorno = kill_jobrunner(server, inv.winrm, pid, proc.get("start"), processo)
        log.warning("JobRunner %s PID %s encerrado automaticamente (%s) -> %s (%s)",
                    server.name, pid, motivo, ok, retorno)
        db.audit("rmon", "jobrunner_kill_auto",
                 f"{server.name} PID {pid} ({quem}; {motivo}) -> {ok}: {retorno}"[:900])
        if ok:
            proc["encerrado"] = agora
            texto = (f"RMon encerrou o processo de job PID {pid} ({quem}): {motivo}. "
                     f"{retorno[:1].upper() + retorno[1:]}. O RM perde esta execução: avise quem pediu")
            db.record_alert(server.name, "acao", f"jobrunner-kill:{pid}", texto)
            msgs.append("\U0001F6D1 " + texto)
        elif not retorno.startswith("o PID") and f"{server.name}|{_jr_chave(proc)}" not in _jr_falhou:
            # "o PID ... ja tinha terminado / e outro processo" nao e falha
            _jr_falhou.add(f"{server.name}|{_jr_chave(proc)}")
            msgs.append(f"\u26A0\uFE0F RMon NÃO conseguiu encerrar o processo de job PID {pid} "
                        f"({quem}; {motivo}): {retorno}")
    return msgs


def _coletar(server, inv: Inventory, th: dict, manut: set[str]) -> dict:
    """Coleta um servidor e, na mesma hora, encerra o JobRunner que estourou."""
    r = collect_server(server, inv.winrm, inv.defaults)
    if server.name in manut:
        return r
    try:
        msgs = encerrar_jobrunners(server, inv, r, th, _agora())
    except Exception:  # noqa: BLE001 - nunca derruba a coleta
        log.exception("encerramento automatico do JobRunner em %s", server.name)
        msgs = []
    if msgs and notify.enabled():
        notify.send(f"RMonitor — {exibicao.com_ip(server.name)}\n" + "\n".join(msgs))
    return r


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


def _enriquecer_elastic(inv: Inventory, coletas: list[tuple]) -> tuple[dict, dict]:
    """Completa as coletas do ciclo com o Elasticsearch (so leitura, opcional).

    * contagens de RDP dos nos do NLB: Elastic primeiro, WinRM no que faltar;
    * JobRunners de host sem WinRM: a leitura do Elastic substitui a que nao
      veio, e os alertas de 4/8 GB continuam valendo (ver problems()).

    Elastic desligado, fora do ar ou sem dado fresco do host: nada muda, a
    coleta segue so com o WinRM. Nunca levanta excecao.

    Devolve ({servidor: host.name no Elastic}, {host.name: ultima metrica}),
    para o registro de fontes (app/fontes.py) mostrar na tela de onde veio cada
    dado - uma consulta de batimento por ciclo cobre todos os servidores.
    """
    cfg = elastic.settings_for(inv.defaults)
    if not elastic.ativo(cfg):
        return {}, {}
    try:
        comp = db.computadores()
    except Exception:  # noqa: BLE001
        comp = {}
    nomes = {}
    for server, r in coletas:
        jr = r.get("jobrunners") if isinstance(r.get("jobrunners"), dict) else {}
        nomes[server.name] = elastic.nome_es(server, comp.get(server.name) or jr.get("computer"))
    vistos = elastic.vivos(cfg, sorted({n for n in nomes.values() if n}))
    limite = float(cfg.get("max_age_sec") or 180)
    agora = time.time()
    vivos = {n for n, ts in vistos.items() if agora - ts <= limite}
    rdp_de = [s for s, _ in coletas if cfg.get("rdp") and getattr(s, "nlb", False)
              and nomes.get(s.name)]
    jr_de = [(s, r) for s, r in coletas if cfg.get("jobrunner") and not r.get("reachable")
             and nomes.get(s.name)]
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
    return nomes, vistos


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
    if not chave.startswith(("svc:", "agente:")) or not r.get("reachable"):
        return False
    return chave.split(":", 1)[1] not in {s.get("name") for s in (r.get("services") or [])}


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
                    f"fim da manutenção aberta em {manutencao.hora(janela.get('desde'))}: "
                    + (f"{len(abertos)} problema(s) seguem abertos" if abertos else "sem pendências"))
    for k in abertos:
        # a mensagem abaixo e o aviso: o relogio do lembrete recomeca aqui
        _avisado.setdefault(nome, {})[k] = agora
    if not notify.enabled():
        return
    cab = (f"\U0001F527 RMonitor — {exibicao.com_ip(nome)}: fim da manutenção aberta por "
           f"{janela.get('por') or '?'} em {manutencao.hora(janela.get('desde'))}")
    if abertos:
        notify.send(cab + "\nSeguem abertos:\n" + "\n".join(
            ("\U0001F534 " if critico(k) else "⚠ ") + v for k, v in abertos.items()))
    else:
        notify.send(cab + "\nNenhum problema aberto: voltou limpo.")


# Camada de seguranca do RDP (RDP-Tcp\SecurityLayer) e NLA, para o texto.
_SECURITY_LAYER = {0: "RDP nativo, sem TLS", 1: "Negociar", 2: "TLS"}
_NLA = {0: "desligada", 1: "ligada"}
_CRIPTO = {1: "Baixo", 2: "Compatível com o cliente", 3: "Alto", 4: "FIPS"}
# Agente do Elastic calado ha mais que isso (a consulta de batimento olha 15 min)
_ELASTIC_MUDO_S = 15 * 60


def _texto_tls_recusado(n: dict) -> str:
    sl, cripto = n.get("security_layer"), n.get("min_encryption")
    texto = (f"a segurança do RDP (SecurityLayer) já é {sl} ({_SECURITY_LAYER.get(sl, '?')}), mas o RDP "
             "deste servidor recusa TLS (testado na porta 3389)")
    if cripto == 1:
        return texto + (": o nível de criptografia do RDP é 1 (Baixo), e com ele o Windows só oferece o "
                        "RDP nativo. Subir para 2 (Compatível com o cliente), como nos outros servidores: WMI "
                        "Win32_TSGeneralSetting, SetEncryptionLevel(2) - vale na hora, sem derrubar sessão")
    return texto + ": conferir o nível de criptografia e o certificado do RDP deste servidor"


def padrao_cluster(coletas: list[tuple], vistos_es: dict[str, float] | None = None,
                   nomes_es: dict[str, str] | None = None, es_ativo: bool = False,
                   agora: float | None = None) -> dict[str, dict[str, str]]:
    """O que deixa um servidor FORA DO PADRAO dos outros: {servidor: {chave: texto}}.

    Coleta que vale para uns servidores e nao para outros nao protege nada - o
    .188 passou meses sem gravar o 1149 (SecurityLayer=0) e o NLB nunca
    conseguiu julga-lo pelo sinal "autentica sem abrir sessao". Aqui cada no e
    comparado com os pares e a divergencia vira aviso (`PADRAO:*`, ambar, que
    notifica uma vez e fica na tela ate ser corrigida):

    * nos do NLB, por cluster: SecurityLayer e NLA do RDP diferentes da maioria;
      criptografia do RDP diferente; SecurityLayer certo mas o RDP recusando TLS
      na porta 3389 (o .188 em 15-16/10: criptografia 1, Baixa);
      no ativo no NLB que nao grava o 1149 ha 7 dias enquanto um par gravou nas
      ultimas 24h (fim de semana sem logon em nenhum no nao acusa ninguem);
    * Elastic ligado: agente de um servidor calado enquanto os outros mandam
      (Elastic inteiro fora do ar nao acusa ninguem).
    """
    agora = time.time() if agora is None else agora
    out: dict[str, dict[str, str]] = {}

    def marcar(nome: str, chave: str, texto: str) -> None:
        out.setdefault(nome, {})[chave] = texto

    por_cluster: dict[str, list[tuple[str, dict]]] = {}
    for s, r in coletas:
        n = r.get("nlb") if isinstance(r.get("nlb"), dict) else None
        if getattr(s, "nlb", False) and r.get("reachable") and n and n.get("cluster"):
            por_cluster.setdefault(str(n["cluster"]), []).append((s.name, n))
    for cluster, nos in por_cluster.items():
        if len(nos) < 2:
            continue
        for campo, rotulo, explica in (("security_layer", "SecurityLayer do RDP", _SECURITY_LAYER),
                                       ("nla", "NLA do RDP", _NLA),
                                       ("min_encryption", "Criptografia do RDP", _CRIPTO)):
            valores = [n.get(campo) for _, n in nos if n.get(campo) is not None]
            if len(valores) < 2:
                continue
            contagem: dict = {}
            for v in valores:
                contagem[v] = contagem.get(v, 0) + 1
            maioria, qtd = max(contagem.items(), key=lambda kv: kv[1])
            if qtd * 2 <= len(valores):
                continue  # sem maioria, nao ha "padrao" para cobrar
            for nome, n in nos:
                v = n.get(campo)
                if v is not None and v != maioria:
                    marcar(nome, f"PADRAO:{campo}",
                           f"{rotulo} = {v} ({explica.get(v, '?')}), diferente dos outros servidores do "
                           f"balanceamento {cluster} ({maioria}, {explica.get(maioria, '?')})")
        # SecurityLayer certo, mas o RDP recusando TLS de verdade na porta: conta
        # como fora (a criptografia diferente vai junto, num aviso so).
        for nome, n in nos:
            if n.get("security_layer") in (1, 2) and n.get("rdp_tls") is False:
                (out.get(nome) or {}).pop("PADRAO:min_encryption", None)
                marcar(nome, "PADRAO:rdp_tls", _texto_tls_recusado(n))
        ativos = [(nome, n) for nome, n in nos if n.get("status") in nlb.ATIVOS]
        gravam = [nome for nome, n in ativos
                  if n.get("ultimo_1149_min") is not None and n["ultimo_1149_min"] <= 24 * 60]
        if not gravam:
            continue
        for nome, n in ativos:
            ult = n.get("ultimo_1149_min")
            if "ultimo_1149_min" in n and (ult is None or ult > 7 * 24 * 60):
                texto = (f"não registra o login do RDP (evento 1149), que "
                         f"{', '.join(exibicao.titulo(g) for g in gravam)} registra: o balanceamento não "
                         "consegue ver se este servidor aceita o login sem abrir a sessão")
                sl = n.get("security_layer")
                tls = (out.get(nome) or {}).pop("PADRAO:rdp_tls", None)
                causa = (out.get(nome) or {}).pop("PADRAO:security_layer", None)
                if tls is not None:
                    texto += ". Causa: " + tls
                elif causa is not None:
                    texto += (f". Causa: SecurityLayer do RDP = {sl} "
                              f"({_SECURITY_LAYER.get(sl, '?')}); com 1 (Negociar), "
                              "como nos outros servidores, o Windows passa a registrar")
                elif sl in (1, 2) and n.get("rdp_tls") is True:
                    # corrigido e em vigor: o 1149 so aparece no proximo logon
                    texto += (f". O SecurityLayer do RDP já é {sl} ({_SECURITY_LAYER[sl]}) e o RDP já "
                              "aceita TLS (testado na porta 3389): o aviso some sozinho no primeiro "
                              "login novo neste servidor")
                elif sl in (1, 2):
                    texto += (f". O SecurityLayer do RDP no registro já é {sl} ({_SECURITY_LAYER[sl]}), "
                              "igual aos outros servidores, mas o RMon ainda não conseguiu testar se o RDP "
                              "aceita TLS (porta 3389)")
                marcar(nome, "PADRAO:1149", texto)

    if es_ativo and vistos_es:
        frescos = {h for h, ts in vistos_es.items() if agora - ts <= _ELASTIC_MUDO_S}
        if frescos:
            for s, r in coletas:
                h = (nomes_es or {}).get(s.name)
                if not h or h in frescos:
                    continue
                ts = vistos_es.get(h)
                marcar(s.name, "PADRAO:elastic",
                       f"agente do Elastic ({h}) sem mandar dados "
                       + (f"desde {manutencao.hora(ts)}" if ts else "nos últimos 15 min")
                       + ", enquanto os outros servidores mandam: histórico do JobRunner e RDP deste "
                         "host ficam só com o WinRM")
    return {k: v for k, v in out.items() if v}


def batimento_nlb(inv: Inventory, nomes_es: dict, vistos_es: dict,
                  es_ativo: bool) -> dict[str, dict] | None:
    """Ultima metrica de cada no do NLB no Elastic, para o reset do no sem contato.

    `ok` so quando a consulta deste ciclo respondeu: Elastic fora do ar nao pode
    parecer "host mudo" e autorizar um reset. None com o Elastic desligado.
    """
    return batimento(nomes_es, vistos_es, es_ativo,
                     [s for s in inv.servers if getattr(s, "nlb", False)])


def batimento(nomes_es: dict, vistos_es: dict, es_ativo: bool,
              servidores: Iterable) -> dict[str, dict] | None:
    """Ultima metrica de cada servidor no Elastic (ver batimento_nlb)."""
    if not es_ativo:
        return None
    # respondeu AGORA: sucesso recente e nenhum erro depois dele
    ok = (elastic.erro_atual() is None
          and time.time() - float(elastic.saude().get("ok_ts") or 0) < 120)
    return {s.name: {"ok": ok, "ts": vistos_es.get(nomes_es.get(s.name) or "")}
            for s in servidores}


def poll_all(inv: Inventory, settings: Settings) -> None:
    if not inv.servers:
        return
    _restaurar_estado(s.name for s in inv.servers)
    th = {**DEFAULT_ALERTS, **(inv.defaults.get("alerts") or {}), **(db.get_config("alerts", {}) or {})}
    # host em manutencao: quem esta mexendo nele decide (o encerramento
    # automatico do JobRunner tambem fica de fora)
    try:
        manut = set(manutencao.ativas(_agora()))
    except Exception:  # noqa: BLE001
        manut = set()
    with ThreadPoolExecutor(max_workers=min(8, len(inv.servers))) as pool:
        futures = {
            pool.submit(_coletar, s, inv, th, manut): s
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
        nomes_es, vistos_es = {}, {}
        try:
            nomes_es, vistos_es = _enriquecer_elastic(inv, coletas)
        except Exception:  # noqa: BLE001
            log.exception("Elasticsearch: falha ao completar a coleta (seguindo pelo WinRM)")
        # De onde veio cada dado deste ciclo, para a tela dizer (app/fontes.py)
        es_ativo = elastic.ativo(elastic.settings_for(inv.defaults))
        fontes.registrar(coletas, nomes_es, vistos_es, es_ativo)
        # Padronizacao: gravada com a coleta, para painel, mural e alerta verem o mesmo
        padrao = padrao_cluster(coletas, vistos_es, nomes_es, es_ativo)
        for server, result in coletas:
            result["padrao"] = dict(padrao.get(server.name) or {})
            # Elastic sem resposta neste ciclo nao "resolve" o agente parado
            # que ja estava acusado: sem leitura, o estado anterior vale.
            if es_ativo and not vistos_es:
                velho = (_last.get(server.name) or {}).get("PADRAO:elastic")
                if velho:
                    result["padrao"]["PADRAO:elastic"] = velho
        fontes.registrar_padrao({s.name: r["padrao"] for s, r in coletas})
        for server, result in coletas:
            _identificar_jobrunners(result.get("jobrunners"), th)
        janela_boot = _minutos(th, "boot_janela_min") * 60
        for server, result in coletas:
            boot = marcar_pos_boot(result, _desde.get(server.name) or {}, _agora(), janela_boot,
                                   _boot.get(server.name))
            if boot is not None:
                _boot[server.name] = boot

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
                db.record_alert(nome, "reminder", k, f"segue aberto há {aberto}: {atual[k]}")
                avisado[k] = agora
                msgs.append(f"⏰ segue aberto há {aberto}: {atual[k]}")
            msgs += iniciar_automatico(server, inv, result, _minutos(th, "boot_iniciar_min") * 60, agora,
                                       _minutos(th, "autocorrecao_teto_min") * 60, desde, atual)
            if msgs and notify.enabled():
                notify.send(f"RMonitor — {exibicao.com_ip(nome)}\n" + "\n".join(msgs))

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
                          hold=manutencao.ativas(agora, janelas),
                          es=batimento_nlb(inv, nomes_es, vistos_es, es_ativo))
        except Exception:  # noqa: BLE001
            log.exception("NLB: falha no ciclo de avaliação")
        # Servidor fora do NLB travado sem contato: reinicio pela iLO/hypervisor
        # (o .222 em 19/10). Mesma confirmacao do Elastic que o NLB exige.
        try:
            reinicio.run_cycle(inv, {s.name: r for s, r in coletas},
                               batimento(nomes_es, vistos_es, es_ativo, reinicio.servidores(inv)),
                               hold=manutencao.ativas(agora, janelas))
        except Exception:  # noqa: BLE001
            log.exception("reinício: falha no ciclo de avaliação")


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
                ha = f" (há {_duracao(agora - t['desde'])})" if t.get("desde") else ""
                out.append(f"• {exibicao.titulo(nome)}: {texto[:160]}{ha}")
        return out

    def bloco(titulo: str, itens: list[str], limite: int = 15) -> list[str]:
        if not itens:
            return []
        extra = [f"... e mais {len(itens) - limite}"] if len(itens) > limite else []
        return [f"{titulo} ({len(itens)}):"] + itens[:limite] + extra

    partes = [f"\U0001F4CB RMonitor — resumo diário {time.strftime('%d/%m', time.localtime(agora))}"]
    crit, avisos = linhas(True), linhas(False)
    partes += bloco("\U0001F534 Críticos abertos", crit)
    partes += bloco("⚠ Avisos abertos", avisos)
    if not crit and not avisos:
        partes.append("✅ Nenhum problema aberto.")
    ativas = manutencao.ativas(agora, janelas)
    if ativas:
        partes.append("\U0001F527 Em manutenção: " + ", ".join(
            f"{exibicao.titulo(n)} até {manutencao.hora(j.get('ate'))}" for n, j in sorted(ativas.items())))
    if elastic_mudos:
        partes.append("📡 Elastic sem dados de: " + ", ".join(elastic_mudos)
                      + " (agente parado: histórico do JobRunner e acessos desses servidores só pelo WinRM)")
    abriram = sum(1 for a in ultimas24 if a.get("kind") == "raised")
    resolveram = sum(1 for a in ultimas24 if a.get("kind") == "resolved")
    partes.append(f"Últimas 24h: {abriram} alerta(s) aberto(s), {resolveram} resolvido(s).")
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
            mudos.append(f"{exibicao.titulo(srv)} (nada em 7 dias)")
        elif agora - ts > limite:
            mudos.append(f"{exibicao.titulo(srv)} (desde {manutencao.hora(ts)})")
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
    log.info("resumo diário enviado")
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
                res = {"ok": False, "items": [], "error": f"inventário: {exc}"}
            gasto = int(res.get("ms") or 0)
            if not res.get("ok"):
                db.record_inventory_run(server.name, False, 0, res.get("error"), gasto)
                log.warning("inventário %s -> FALHA (%s)", server.name, res.get("error"))
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
            log.info("inventário %s -> %d pacotes, %d mudanca(s) em %dms",
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
    log.info("repositório de pacotes: %d arquivo(s), %d sem item correspondente",
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
        linhas.append(f"... e mais {len(eventos) - 12} mudança(s)")
    notify.send(f"\U0001F4E6 RMonitor \u2014 software alterado em "
                f"{exibicao.com_ip(name)}\n" + "\n".join(linhas))


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
                       output="pré-voo: nada foi executado no host",
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
        notify.send(f"{marca} RMonitor \u2014 {exibicao.titulo(task['server'])}: {task.get('produto')} "
                    f"{task.get('version')} ({task['action']}) "
                    f"{'concluído' if res.get('ok') else 'FALHOU: ' + str(res.get('error'))[:200]}")


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
