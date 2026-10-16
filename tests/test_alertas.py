"""Motor de alertas (app/scheduler.py): o que vira problema, de que cor e quando.

Funcoes puras, sem rede nem banco. Os cenarios sao os que ja deram alarme falso
(ou deixaram de dar alarme) em producao - cada um cita o PR que o corrigiu.
"""
from __future__ import annotations

from app import scheduler
from app.scheduler import DEFAULT_ALERTS

TH = dict(DEFAULT_ALERTS)


def coleta(**kw):
    r = {
        "reachable": True,
        "mem_pct": 40, "commit_pct": 30,
        "disks": [{"drive": "C:", "used_pct": 50}],
        "services": [{"name": "RM.Host.Service", "status": "Running", "src": "auto", "start": "Auto"}],
        "app_ok": True, "app_ms": 200,
    }
    r.update(kw)
    return r


def fila(**kw):
    f = {"done_recent": 0, "stuck": 0, "waiting": 0, "window_min": 30,
         "oldest_min": None, "waiting_min": None}
    f.update(kw)
    return f


# ------------------------------------------------------------ cores

def test_avisos_nao_notificam():
    assert not scheduler.notifica("JOBS")
    assert not scheduler.notifica("JOBSQL")
    assert scheduler.notifica("JOBQUEUE")
    assert scheduler.notifica("svc:RM.Host.Service")


def test_criticos_e_avisos():
    for k in ("DOWN", "APP", "JOBQUEUE", "JOBFAIL", "svc:W3SVC", "jobrunner-crit:42"):
        assert scheduler.critico(k), k
    for k in ("MEM", "DISK", "APPSLOW", "COMMIT", "JOBS", "jobrunner:42", "broker:_Broker.dat"):
        assert not scheduler.critico(k), k


# ------------------------------------------------------------ servicos

def test_servico_fixo_falha_em_qualquer_estado_fora_de_running():
    assert scheduler.service_down({"name": "W3SVC", "status": "Stopped"})
    assert scheduler.service_down({"name": "W3SVC", "status": "NOT_FOUND"})
    assert not scheduler.service_down({"name": "W3SVC", "status": "Running"})


def test_servico_descoberto_so_falha_se_automatico():
    base = {"name": "RM.Host.Service1", "status": "Stopped", "src": "auto"}
    assert scheduler.service_down({**base, "start": "Auto"})
    assert not scheduler.service_down({**base, "start": "Manual"})
    assert not scheduler.service_down({**base, "start": "Disabled"})


def test_familia_tira_so_o_sufixo_numerico():
    assert scheduler.service_family("RM.Host.Service02") == "RM.Host.Service"
    assert scheduler.service_family("RM.Host.Service_2") == "RM.Host.Service"
    assert scheduler.service_family("RM.Host.Service (2)") == "RM.Host.Service"
    assert scheduler.service_family("RM.Host.Service") == "RM.Host.Service"


def test_cleanner_nao_e_instancia_do_service_pr25():
    svcs = [{"name": n, "status": "Running", "src": "auto", "pattern": "RM.Host.*"}
            for n in ("RM.Host.Service", "RM.Host.Service1", "RM.Host.Cleanner")]
    grupos = scheduler.service_groups(svcs)
    assert [g["family"] for g in grupos] == ["RM.Host.Service"]
    assert grupos[0]["installed"] == 2 and grupos[0]["running"] == 2


# ------------------------------------------------------------ host

def test_coleta_saudavel_nao_tem_problema():
    assert scheduler.problems(coleta(), TH) == {}


def test_engasgo_do_winrm_nao_vira_down_pr5():
    r = coleta(reachable=False, error="WinRMOperationTimeoutError")
    assert scheduler.problems(r, TH, fail_streak=1) == {}
    assert scheduler.problems(r, TH, fail_streak=2) == {}
    p = scheduler.problems(r, TH, fail_streak=3)
    assert list(p) == ["DOWN"] and "3 coletas" in p["DOWN"]


def test_sem_contato_nao_avalia_mais_nada():
    """Sem contato os outros campos sao lixo da coleta: so o DOWN conta."""
    r = coleta(reachable=False, mem_pct=99, app_ok=False)
    assert list(scheduler.problems(r, TH, fail_streak=5)) == ["DOWN"]


def test_limiares_de_host():
    r = coleta(mem_pct=95, commit_pct=92, app_ms=4000,
               disks=[{"drive": "D:", "used_pct": 99}, {"drive": "C:", "used_pct": 91}],
               services=[{"name": "W3SVC", "status": "Stopped"}])
    p = scheduler.problems(r, TH)
    assert set(p) == {"MEM", "COMMIT", "APPSLOW", "DISK", "svc:W3SVC"}
    assert "C:" in p["DISK"]


def test_app_fora_nao_e_tambem_app_lenta():
    p = scheduler.problems(coleta(app_ok=False, app_ms=9000), TH)
    assert "APP" in p and "APPSLOW" not in p


# ------------------------------------------------------------ broker

def broker(**kw):
    b = {"name": "_BrokerCustom.dat", "path": r"C:\RM\_BrokerCustom.dat",
         "size": 558 * 1024, "age_min": 600}
    b.update(kw)
    return b


def test_broker_comparado_com_o_proprio_host_pr19():
    """33KB e correto num host que nunca teve mais que isso."""
    r = coleta(broker=[broker(size=33 * 1024)])
    assert scheduler.broker_problems(r, TH, {"_BrokerCustom.dat": 33 * 1024}) == {}
    p = scheduler.broker_problems(r, TH, {"_BrokerCustom.dat": 558 * 1024})
    assert "558KB que este host ja teve" in p["broker:_BrokerCustom.dat"]


def test_broker_ainda_crescendo_nao_tem_veredito_pr23():
    r = coleta(broker=[broker(size=10 * 1024, age_min=2)])
    ref = {"_BrokerCustom.dat": 558 * 1024}
    assert scheduler.broker_problems(r, TH, ref, {"_BrokerCustom.dat": False}) == {}
    # parou de crescer (duas coletas iguais): vale mesmo recem-gerado
    assert scheduler.broker_problems(r, TH, ref, {"_BrokerCustom.dat": True})


def test_broker_ausente_so_cobra_quem_tinha_de_gera_lo_pr26():
    sem = broker(size=None)
    assert scheduler.broker_problems(coleta(broker=[{**sem, "running": []}]), TH) == {}
    p = scheduler.broker_problems(coleta(broker=[{**sem, "running": ["RM.Host.Service"]}]), TH)
    assert "RM.Host.Service no ar" in p["broker:_BrokerCustom.dat"]


def test_broker_minimo_absoluto():
    th = {**TH, "broker_min_kb": 100}
    p = scheduler.broker_problems(coleta(broker=[broker(size=50 * 1024)]), th)
    assert "minimo esperado" in p["broker:_BrokerCustom.dat"]


# ------------------------------------------------------------ JobRunner (#32)

def jr(gb, pid=4242, jobs=None):
    d = {"top": [{"pid": pid, "private_mb": gb * 1024}, {"pid": 7, "private_mb": 80}]}
    if jobs is not None:
        d["jobs"] = jobs
    return d


def test_jobrunner_normal_nao_alerta():
    assert scheduler.jobrunner_problems(jr(1.5), TH) == {}


def test_jobrunner_aviso_e_critico_tem_chaves_proprias():
    assert set(scheduler.jobrunner_problems(jr(5), TH)) == {"jobrunner:4242"}
    assert set(scheduler.jobrunner_problems(jr(9), TH)) == {"jobrunner:4242", "jobrunner-crit:4242"}


def test_jobrunner_zero_desliga_o_nivel():
    th = {**TH, "jobrunner_warn_gb": 0}
    assert set(scheduler.jobrunner_problems(jr(9), th)) == {"jobrunner-crit:4242"}
    assert scheduler.jobrunner_limiar_minimo(th) == 8


def test_jobrunner_diz_qual_job_e_de_quem():
    ex = {"4242": {"idjob": 2431028, "nome": "Relatorio de turma", "usuario": "fulano",
                   "inicio": "13/10 08:00", "progresso": 40, "filtro": "TURMA = *"}}
    texto = scheduler.jobrunner_problems(jr(5, jobs=ex), TH)["jobrunner:4242"]
    assert "2431028" in texto and "fulano" in texto and "(40%)" in texto and "TURMA" in texto


# ------------------------------------------------------------ fila de jobs (#27)

def test_fila_que_anda_nao_esta_travada():
    assert scheduler.fila_parada(fila(done_recent=1, stuck=9), TH) == (False, "")


def test_fila_vazia_fora_do_expediente_nao_esta_travada():
    assert scheduler.fila_parada(fila(), TH) == (False, "")


def test_execucao_presa_isolada_e_ruido():
    assert not scheduler.fila_parada(fila(stuck=1), TH)[0]


def test_fila_travada_por_presas_ou_por_pickup():
    assert scheduler.fila_parada(fila(stuck=8, oldest_min=55), TH)[0]
    assert scheduler.fila_parada(fila(waiting=3, waiting_min=40), TH)[0]


def test_fila_com_erro_de_leitura_nao_decide():
    assert scheduler.fila_parada({"error": "timeout"}, TH) == (False, "")


def test_fila_travada_precisa_se_confirmar():
    travada = fila(stuck=8)
    jb = {"ok": 0, "failed": 0, "window_min": 15, "queue": travada}
    assert "JOBQUEUE" not in scheduler.job_problems(jb, TH, [travada, travada])
    assert "JOBQUEUE" not in scheduler.job_problems(jb, TH, [travada, fila(done_recent=2), travada])
    assert "JOBQUEUE" in scheduler.job_problems(jb, TH, [travada] * 3)


def test_fila_travada_nao_duplica_rm_host_parado():
    travada = fila(stuck=8)
    jb = {"ok": 0, "failed": 0, "window_min": 15, "queue": travada}
    ja = {"svc:RM.Host.Service": "parado"}
    assert "JOBQUEUE" not in scheduler.job_problems(jb, TH, [travada] * 3, ja)


def test_job_com_erro_e_aviso_e_falha_em_bloco_e_critico():
    p = scheduler.job_problems({"ok": 40, "failed": 5, "window_min": 15}, TH)
    assert set(p) == {"JOBS"}
    p = scheduler.job_problems({"ok": 0, "failed": 12, "window_min": 15}, TH)
    assert set(p) == {"JOBS", "JOBFAIL"}


def test_sql_fora_e_so_aviso():
    p = scheduler.job_problems({"error": "Login failed"}, TH)
    assert set(p) == {"JOBSQL"} and not scheduler.notifica("JOBSQL")


# ------------------------------------------------------------ transicoes

def test_transicao_abre_e_resolve():
    prev = {"MEM": "memoria em 95%", "DISK": "disco C: em 92%"}
    atual, novas, resolvidas = scheduler.transicoes(prev, {"MEM": "memoria em 96%", "APP": "x"},
                                                    coleta())
    assert novas == ["APP"] and resolvidas == ["DISK"]
    assert atual == {"MEM": "memoria em 96%", "APP": "x"}


def test_engasgo_do_winrm_nao_resolve_o_que_estava_aberto():
    """Antes: cada timeout dava "resolvido" para tudo e reabria no ciclo seguinte."""
    prev = {"svc:RM.Host.Service": "servico RM.Host.Service = Stopped", "MEM": "memoria em 95%"}
    sem_contato = coleta(reachable=False)
    atual, novas, resolvidas = scheduler.transicoes(prev, {}, sem_contato)
    assert novas == [] and resolvidas == [] and atual == prev
    # quando o DOWN confirma, ele abre e o resto segue aberto
    atual, novas, resolvidas = scheduler.transicoes(prev, {"DOWN": "sem contato"}, sem_contato)
    assert novas == ["DOWN"] and resolvidas == [] and set(atual) == set(prev) | {"DOWN"}


def test_down_resolve_quando_o_contato_volta():
    prev = {"DOWN": "sem contato", "MEM": "memoria em 95%"}
    atual, novas, resolvidas = scheduler.transicoes(prev, {}, coleta())
    assert sorted(resolvidas) == ["DOWN", "MEM"] and atual == {}


def test_servico_tirado_do_inventario_sai_do_monitoramento():
    r = coleta()
    assert scheduler.fora_do_monitoramento("svc:RM.Host.Service3", r)
    assert not scheduler.fora_do_monitoramento("svc:RM.Host.Service", r)
    assert not scheduler.fora_do_monitoramento("MEM", r)
    # sem contato a lista de servicos vem vazia: nao prova nada
    assert not scheduler.fora_do_monitoramento("svc:RM.Host.Service3", coleta(reachable=False))


# ------------------------------------------------------------ servico que nao subiu no boot
# rh-web-248, 15/10/2026: o Windows Update reiniciou o host e, no boot, o
# RM.Host.Service estourou os 30 s do SCM e ficou parado. A recuperacao do
# Windows (reiniciar) nao age em falha de partida, e o alerta dizia so
# "servico RM.Host.Service = Stopped" - nada apontava para o reboot.

AGORA = 1_760_570_000.0


def rm_parado(**kw):
    s = {"name": "RM.Host.Service", "status": "Stopped", "src": "auto", "start": "Auto"}
    s.update(kw)
    return s


def test_servico_automatico_parado_logo_apos_o_boot_e_marcado():
    r = coleta(uptime_sec=228, services=[rm_parado()])
    scheduler.marcar_pos_boot(r, {}, AGORA, 30 * 60)
    assert r["services"][0]["nao_subiu"] == int(AGORA - 228)
    texto = scheduler.problems(r, TH)["svc:RM.Host.Service"]
    assert texto.startswith("servico RM.Host.Service = Stopped")
    assert "nao subiu apos o reboot de " + scheduler.manutencao.hora(AGORA - 228) in texto


def test_marca_segue_enquanto_o_alerta_aberto_no_boot_continua():
    # 12h depois o host ja tem uptime alto, mas o alerta abriu logo apos o boot
    r = coleta(uptime_sec=12 * 3600, services=[rm_parado()])
    desde = {"svc:RM.Host.Service": AGORA - 12 * 3600 + 228}
    scheduler.marcar_pos_boot(r, desde, AGORA, 30 * 60)
    assert r["services"][0]["nao_subiu"] == int(AGORA - 12 * 3600)


def test_servico_que_caiu_horas_depois_do_boot_nao_e_marcado():
    r = coleta(uptime_sec=5 * 3600, services=[rm_parado()])
    scheduler.marcar_pos_boot(r, {}, AGORA, 30 * 60)
    assert "nao_subiu" not in r["services"][0]
    assert "reboot" not in scheduler.problems(r, TH)["svc:RM.Host.Service"]


def test_so_servico_automatico_parado_e_marcado():
    servicos = [rm_parado(start="Manual", name="A"), rm_parado(status="Running", name="B"),
                {"name": "W3SVC", "status": "NOT_FOUND", "src": "fixed"}]
    r = coleta(uptime_sec=120, services=servicos)
    scheduler.marcar_pos_boot(r, {}, AGORA, 30 * 60)
    assert all("nao_subiu" not in s for s in r["services"])


def test_marcar_nao_altera_a_lista_de_quem_chamou():
    servicos = [rm_parado()]
    r = coleta(uptime_sec=120, services=servicos)
    scheduler.marcar_pos_boot(r, {}, AGORA, 30 * 60)
    assert "nao_subiu" not in servicos[0]


def test_sem_uptime_ou_sem_contato_nada_e_marcado():
    for r in (coleta(services=[rm_parado()]),
              {"reachable": False, "uptime_sec": 60, "services": [rm_parado()]}):
        scheduler.marcar_pos_boot(r, {}, AGORA, 30 * 60)
        assert "nao_subiu" not in r["services"][0]


def test_hora_do_boot_nao_oscila_entre_ciclos():
    r = coleta(uptime_sec=300, services=[rm_parado()])
    boot = scheduler.marcar_pos_boot(r, {}, AGORA, 30 * 60)
    r2 = coleta(uptime_sec=360, services=[rm_parado()])
    # o ciclo seguinte terminou 40 s mais tarde em relacao a leitura do uptime
    assert scheduler.marcar_pos_boot(r2, {}, AGORA + 100, 30 * 60, boot) == boot
    assert r2["services"][0]["nao_subiu"] == int(boot)
    r3 = coleta(uptime_sec=60, services=[rm_parado()])   # reiniciou de novo
    assert scheduler.marcar_pos_boot(r3, {}, AGORA + 3600, 30 * 60, boot) == AGORA + 3540
