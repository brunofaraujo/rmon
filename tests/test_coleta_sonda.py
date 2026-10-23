"""Coleta independente por servidor e sonda dos JobRunners (scheduler, 19/10).

Com o .222 travado, o ciclo de todos os servidores foi de 60 s para ~2 min,
justo enquanto o .188 estourava. A coleta de um host travado nao pode segurar
as dos outros, e entre as coletas a sonda de 15 s le so os JobRunners.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app import collector, db, jobstats, manutencao, notify, scheduler


@pytest.fixture
def limpo(monkeypatch):
    monkeypatch.setattr(scheduler, "_em_curso", {})
    monkeypatch.setattr(scheduler, "_sondas", {})
    monkeypatch.setattr(scheduler, "sonda_ultima", {})
    monkeypatch.setattr(scheduler, "_com_jobrunner", set())
    monkeypatch.setattr(scheduler, "_jr_amostra", {})
    monkeypatch.setattr(scheduler, "_jr_encerrados", {})
    monkeypatch.setattr(scheduler, "_jr_matando", set())
    monkeypatch.setattr(scheduler, "_jr_falhou", set())
    monkeypatch.setattr(db, "audit", lambda *a, **k: None)
    monkeypatch.setattr(db, "record_alert", lambda *a: None)
    monkeypatch.setattr(db, "record_job_kill", lambda *a, **k: None)
    monkeypatch.setattr(db, "get_config", lambda k, d=None: d)
    monkeypatch.setattr(manutencao, "ativas", lambda agora=None, janelas=None: {})
    monkeypatch.setattr(notify, "enabled", lambda: False)
    monkeypatch.setattr(jobstats, "executions_by_pid", lambda comp, pids: {})


def srv(nome):
    return SimpleNamespace(name=nome, host=nome, cred="x", jobs=None, nlb=False)


def test_host_travado_nao_atrasa_os_outros(limpo, monkeypatch):
    solta = threading.Event()
    chamadas = []

    def coleta(s, winrm, defaults):
        chamadas.append(s.name)
        if s.name == "jobs-222":
            solta.wait(10)
            return {"reachable": True, "tarde": True}
        return {"reachable": True}
    monkeypatch.setattr(scheduler, "collect_server", coleta)
    inv = SimpleNamespace(servers=[srv("app-188"), srv("jobs-222")], winrm=None,
                          defaults={"coleta_espera_s": 0.3})
    r = dict((s.name, x) for s, x in scheduler.coletar_todos(inv, {}, set()))
    assert r["app-188"] == {"reachable": True}
    assert r["jobs-222"]["reachable"] is False and r["jobs-222"]["pendente"]
    assert "sem atrasar os outros" in r["jobs-222"]["error"]
    # ciclo seguinte com a coleta ainda presa: nao abre outra para o mesmo host
    scheduler.coletar_todos(inv, {}, set())
    assert chamadas.count("jobs-222") == 1 and chamadas.count("app-188") == 2
    # ficou pronta: o resultado atrasado entra no ciclo seguinte
    solta.set()
    scheduler._em_curso["jobs-222"][0].result(5)
    r = dict((s.name, x) for s, x in scheduler.coletar_todos(inv, {}, set()))
    assert r["jobs-222"] == {"reachable": True, "tarde": True}
    assert chamadas.count("jobs-222") == 1


def test_espera_padrao_e_o_intervalo_menos_15s():
    assert scheduler.espera_coleta(SimpleNamespace(defaults={}, poll_interval_seconds=60)) == 45
    assert scheduler.espera_coleta(SimpleNamespace(defaults={}, poll_interval_seconds=20)) == 15
    assert scheduler.espera_coleta(SimpleNamespace(defaults={"coleta_espera_s": 30}, poll_interval_seconds=60)) == 30


def test_so_e_sondado_quem_respondeu_e_tem_jobrunner(limpo):
    scheduler._marcar_sondaveis([
        (srv("a"), {"reachable": True, "jobrunners": {"count": 3, "top": [{"pid": 1, "private_mb": 80}]}}),
        (srv("b"), {"reachable": False, "jobrunners": {"fonte": "elastic", "top": [{"pid": 1}]}}),
        (srv("c"), {"reachable": True, "jobrunners": None}),
        (srv("d"), {"reachable": True, "jobrunners": {"count": 0, "top": []}}),
    ])
    assert scheduler._com_jobrunner == {"a"}


def jr(mb, pid=7760):
    return {"count": 30, "computer": "WIN2016",
            "top": [{"pid": pid, "private_mb": mb, "start": "2026-10-19 09:24:38"}]}


def test_sonda_encerra_entre_as_coletas_e_a_coleta_nao_repete(limpo, monkeypatch):
    mortos = []
    monkeypatch.setattr(scheduler, "kill_jobrunner",
                        lambda s, wc, pid, ini, proc: mortos.append(pid) or (True, f"PID {pid} encerrado (1 MB)"))
    monkeypatch.setattr(scheduler, "sonda_jobrunners", lambda s, wc, proc, top: jr(4400))
    inv = SimpleNamespace(servers=[srv("app-188")], winrm=None, defaults={})
    scheduler._com_jobrunner = {"app-188"}
    scheduler.sondar_jobrunners(inv)
    scheduler._sondas["app-188"].result(5)
    assert mortos == [7760] and scheduler.sonda_ultima["app-188"][1] is True
    # a coleta seguinte ainda ve o processo (leitura de antes do fim): marca, nao mata de novo
    r = {"reachable": True, "jobrunners": jr(4400)}
    assert scheduler.encerrar_jobrunners(srv("app-188"), inv, r, scheduler.DEFAULT_ALERTS, scheduler._agora() + 60) == []
    assert mortos == [7760] and r["jobrunners"]["top"][0]["encerrado"]
    assert scheduler.jobrunner_problems(r["jobrunners"], scheduler.DEFAULT_ALERTS) == {}


def test_sonda_nao_empilha_em_host_engasgado(limpo, monkeypatch):
    solta = threading.Event()
    chamadas = []

    def lenta(s, wc, proc, top):
        chamadas.append(s.name)
        solta.wait(5)
        return None
    monkeypatch.setattr(scheduler, "sonda_jobrunners", lenta)
    inv = SimpleNamespace(servers=[srv("jobs-222")], winrm=None, defaults={})
    scheduler._com_jobrunner = {"jobs-222"}
    scheduler.sondar_jobrunners(inv)
    scheduler.sondar_jobrunners(inv)
    solta.set()
    scheduler._sondas["jobs-222"].result(5)
    assert chamadas == ["jobs-222"]
    assert scheduler.sonda_ultima["jobs-222"][1] is False


def test_sonda_desligada_sem_regra_ou_em_manutencao(limpo, monkeypatch):
    chamadas = []
    monkeypatch.setattr(scheduler, "sonda_jobrunners", lambda *a: chamadas.append(1))
    inv = SimpleNamespace(servers=[srv("a")], winrm=None,
                          defaults={"alerts": {"jobrunner_kill_gb": 0, "jobrunner_kill_mb_min": 0}})
    scheduler._com_jobrunner = {"a"}
    scheduler.sondar_jobrunners(inv)
    monkeypatch.setattr(manutencao, "ativas", lambda agora=None, janelas=None: {"a": {}})
    scheduler.sondar_jobrunners(SimpleNamespace(servers=[srv("a")], winrm=None, defaults={}))
    assert chamadas == [] and scheduler._sondas == {}


def test_ler_sonda_aceita_um_processo_so():
    d = collector.ler_sonda('{"count":1,"top":{"pid":5,"private_mb":90,"start":null},"computer":"SRV11"}')
    assert d["top"] == [{"pid": 5, "private_mb": 90, "start": None}] and d["computer"] == "SRV11"
    assert collector.ler_sonda("") is None and collector.ler_sonda("lixo") is None
    assert "Get-Process -Name 'RM.Host.JobRunner'" in collector.script_sonda_jobrunners("RM.Host.JobRunner", 5)
    with pytest.raises(ValueError):
        collector.script_sonda_jobrunners("x'; Stop-Computer", 5)
