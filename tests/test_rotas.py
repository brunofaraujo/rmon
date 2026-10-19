"""Rotas da Fase 2 (manutencao e encerrar JobRunner) pelo FastAPI, com banco falso.

Tambem renderiza as telas que mudaram (painel, servidor, mural, ocorrencias e
admin): erro de template so apareceria em producao, na frente do usuario.
"""
from __future__ import annotations

import json
import re
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import collector, db, main, manutencao, notify
from app.config import ServerConfig, WinRMConfig
from app.security import hash_password

SENHA = "senha-de-teste-123"
AGORA = time.time()


def coleta(**kw):
    r = {"server": "app-34", "ts": AGORA - 30, "reachable": True, "cpu": 20, "mem_pct": 40,
         "uptime_sec": 86400, "disks": [{"drive": "C:", "used_pct": 50, "free_gb": 50, "size_gb": 100}],
         "services": [{"name": "RM.Host.Service", "status": "Stopped", "src": "auto", "start": "Auto"}],
         "events": [], "app_ok": True, "app_ms": 120, "users_count": 3, "error": None, "jobs": None,
         "broker": None, "commit_pct": 40,
         "jobrunners": {"count": 12, "total_mb": 9000, "computer": "SRV06N",
                        "top": [{"pid": 4242, "private_mb": 5 * 1024, "start": "2026-10-15 08:00:00"},
                                {"pid": 77, "private_mb": 300, "start": "2026-10-15 09:00:00"}],
                        "jobs": {"4242": {"idjob": 2431028, "nome": "Relatório de turma",
                                          "usuario": "fulano"}}}}
    r.update(kw)
    return r


@pytest.fixture
def app_falso(monkeypatch):
    usuarios = {"admin": {"username": "admin", "role": "admin", "active": True,
                          "password_hash": hash_password(SENHA)},
                "tv": {"username": "tv", "role": "viewer", "active": True,
                       "password_hash": hash_password(SENHA)}}
    estado = SimpleNamespace(config={}, alertas=[], auditoria=[], enviadas=[], mortes=[],
                             coletas=[coleta()])

    def falha():
        raise AssertionError("o teste tentou abrir conexão com o PostgreSQL")

    monkeypatch.setattr(db, "_conn", falha)
    monkeypatch.setattr(db, "get_user", lambda u: usuarios.get(u))
    monkeypatch.setattr(db, "touch_login", lambda u: None)
    monkeypatch.setattr(db, "audit", lambda *a, **k: estado.auditoria.append(a))
    monkeypatch.setattr(db, "get_config", lambda k, d=None: estado.config.get(k, d))
    monkeypatch.setattr(db, "set_config", lambda k, v: estado.config.__setitem__(k, v))
    monkeypatch.setattr(db, "record_alert", lambda *a: estado.alertas.append(a))
    monkeypatch.setattr(db, "latest_per_server", lambda: [dict(c) for c in estado.coletas])
    monkeypatch.setattr(db, "history", lambda name, limit=120: [dict(c) for c in estado.coletas])
    monkeypatch.setattr(db, "broker_max", lambda dias=30: {})
    monkeypatch.setattr(db, "broker_estado", lambda dias=1: {})
    monkeypatch.setattr(db, "fila_hist", lambda n=3: {})
    monkeypatch.setattr(db, "fail_streak", lambda s, limit=20: 0)
    monkeypatch.setattr(db, "list_users", lambda: list(usuarios.values()))
    monkeypatch.setattr(db, "recent_alerts", lambda h=48, lim=500, server=None: [
        {"ts": AGORA - 600, "server": "app-34", "kind": "reminder", "problem": "svc:RM.Host.Service",
         "message": "segue aberto há 4h00: serviço RM.Host.Service parado"},
        {"ts": AGORA - 300, "server": "app-34", "kind": "manutencao", "problem": "MANUT",
         "message": "manutenção aberta por admin"},
        {"ts": AGORA - 60, "server": "app-34", "kind": "acao", "problem": "jobrunner-kill:4242",
         "message": "JobRunner PID 4242 encerrado por admin"}])
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(notify, "send", lambda t: estado.enviadas.append(t) or True)
    monkeypatch.setattr(main, "kill_jobrunner",
                        lambda srv, wc, pid, inicio, proc: estado.mortes.append((srv.name, pid, inicio, proc))
                        or (True, f"PID {pid} encerrado (5120 MB de memória privada liberados)"))
    servidor = ServerConfig(name="app-34", host="10.0.0.34")
    inv = SimpleNamespace(servers=[servidor], winrm=WinRMConfig(), defaults={},
                          poll_interval_seconds=60, environments={})
    monkeypatch.setitem(main.STATE, "inv", inv)
    monkeypatch.setitem(main.STATE, "settings", SimpleNamespace(tv_token=""))
    main._TV_CACHE.update(ts=0.0, data=None)
    cliente = TestClient(main.app)

    def entrar(usuario="admin"):
        r = cliente.post("/login", data={"username": usuario, "password": SENHA},
                         follow_redirects=False)
        assert r.status_code == 302

    return SimpleNamespace(c=cliente, estado=estado, entrar=entrar)


# ------------------------------------------------------------ telas

@pytest.mark.parametrize("rota", ["/", "/server/app-34", "/tv", "/ocorrencias", "/admin"])
def test_telas_renderizam(app_falso, rota):
    app_falso.entrar()
    r = app_falso.c.get(rota)
    assert r.status_code == 200, r.text[:500]


def test_linha_do_tempo_mostra_os_tipos_novos(app_falso):
    app_falso.entrar()
    html = app_falso.c.get("/ocorrencias").text
    assert "lembrete &middot;" in html and "manutenção &middot; Manutenção" in html
    assert "Processo de job encerrado (PID 4242)" in html


def test_servico_que_nao_subiu_apos_o_reboot_aparece_na_tela(app_falso):
    # rh-web-248, 15/10/2026: RM.Host.Service parado desde o boot do Windows Update
    boot = int(AGORA - 3600)
    app_falso.estado.coletas = [coleta(uptime_sec=3600, services=[
        {"name": "RM.Host.Service", "status": "Stopped", "src": "auto", "start": "Auto",
         "nao_subiu": boot}])]
    app_falso.entrar()
    marca = "não subiu após o reboot de " + manutencao.hora(boot)
    assert marca in app_falso.c.get("/server/app-34").text
    assert "não subiu após o reboot" in app_falso.c.get("/").text
    main._TV_CACHE.update(ts=0.0, data=None)
    # no mural o texto vai dentro do JSON (tojson escapa o que nao e ASCII)
    assert json.dumps(marca)[1:-1] in app_falso.c.get("/tv").text


def test_agente_parado_aparece_ambar_nas_telas(app_falso):
    app_falso.estado.coletas = [coleta(services=[
        {"name": "Elastic Agent", "status": "Stopped", "src": "agente", "start": "Auto"}])]
    app_falso.entrar()
    for rota in ("/server/app-34", "/"):
        html = app_falso.c.get(rota).text
        assert re.search(r'class="pill amber" title="Elastic Agent[^"]*agente de coleta', html), rota


def test_admin_guarda_a_partida_pos_boot(app_falso):
    app_falso.entrar()
    assert 'name="boot_iniciar_min"' in app_falso.c.get("/admin").text
    assert 'name="autocorrecao_teto_min"' in app_falso.c.get("/admin").text
    app_falso.c.post("/admin/config", data={"boot_iniciar_min": "0", "boot_janela_min": "45"},
                     follow_redirects=False)
    alerts = app_falso.estado.config["alerts"]
    assert alerts["boot_iniciar_min"] == 0 and alerts["boot_janela_min"] == 45
    assert alerts["autocorrecao_teto_min"] == 30   # campo ausente fica no padrao


def test_admin_tem_os_limiares_novos(app_falso):
    app_falso.entrar()
    html = app_falso.c.get("/admin").text
    assert 'name="lembrete_h"' in html and 'name="resumo_hora"' in html
    app_falso.c.post("/admin/config", data={"lembrete_h": "6", "resumo_hora": "-1"})
    alerts = app_falso.estado.config["alerts"]
    assert alerts["lembrete_h"] == 6 and alerts["resumo_hora"] == -1


# ------------------------------------------------------------ manutencao

def test_abrir_manutencao(app_falso):
    app_falso.entrar()
    r = app_falso.c.post("/manutencao/iniciar",
                         data={"server": "app-34", "minutos": "120", "motivo": "atualização do RM"},
                         follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/server/app-34"
    j = app_falso.estado.config["manutencao"]["app-34"]
    assert j["por"] == "admin" and j["motivo"] == "atualização do RM"
    assert 119 * 60 < j["ate"] - time.time() <= 120 * 60
    assert app_falso.estado.alertas[-1][1:3] == ("manutencao", "MANUT")
    assert "silenciados" in app_falso.estado.enviadas[-1]
    html = app_falso.c.get("/server/app-34").text
    assert "Em manutenção até" in html and "Encerrar agora" in html


def test_manutencao_tem_teto(app_falso):
    app_falso.entrar()
    app_falso.c.post("/manutencao/iniciar", data={"server": "app-34", "minutos": "99999"})
    j = app_falso.estado.config["manutencao"]["app-34"]
    assert j["ate"] - time.time() <= manutencao.MAX_MIN * 60


def test_encerrar_manutencao_adianta_o_fim(app_falso):
    app_falso.entrar()
    app_falso.c.post("/manutencao/iniciar", data={"server": "app-34", "minutos": "60"})
    app_falso.c.post("/manutencao/encerrar", data={"server": "app-34"})
    assert app_falso.estado.config["manutencao"]["app-34"]["ate"] <= time.time()
    assert manutencao.ativas() == {}


def test_manutencao_some_do_mural_vermelho(app_falso):
    app_falso.entrar()
    tv = app_falso.c.get("/api/tv").json()
    assert tv["servers"][0]["sev"] == 2 and tv["issues"]
    app_falso.c.post("/manutencao/iniciar", data={"server": "app-34", "minutos": "60"})
    main._TV_CACHE.update(ts=0.0, data=None)
    tv = app_falso.c.get("/api/tv").json()
    s = tv["servers"][0]
    assert s["sev"] == 0 and s["manut"] and tv["issues"] == []
    assert tv["summary"]["maint"] == 1 and tv["summary"]["services_down"] == 0


def test_viewer_nao_abre_manutencao(app_falso):
    app_falso.entrar("tv")
    r = app_falso.c.post("/manutencao/iniciar", data={"server": "app-34", "minutos": "60"})
    assert r.status_code == 403
    assert "manutencao" not in app_falso.estado.config


def test_servidor_desconhecido(app_falso):
    app_falso.entrar()
    r = app_falso.c.post("/manutencao/iniciar", data={"server": "outro", "minutos": "60"})
    assert r.status_code == 404


# ------------------------------------------------------------ encerrar JobRunner

def test_encerrar_jobrunner(app_falso):
    app_falso.entrar()
    r = app_falso.c.post("/jobrunner/encerrar", data={"server": "app-34", "pid": "4242", "confirm": "4242"})
    assert r.json()["ok"]
    assert app_falso.estado.mortes == [("app-34", 4242, "2026-10-15 08:00:00", "RM.Host.JobRunner")]
    assert app_falso.estado.alertas[-1][1:3] == ("acao", "jobrunner-kill:4242")
    assert "2431028" in app_falso.estado.enviadas[-1] and "por admin" in app_falso.estado.enviadas[-1]
    assert app_falso.estado.auditoria[-1][1] == "jobrunner_kill"


def test_encerrar_exige_o_pid_digitado(app_falso):
    app_falso.entrar()
    r = app_falso.c.post("/jobrunner/encerrar", data={"server": "app-34", "pid": "4242", "confirm": "424"})
    assert r.status_code == 400 and app_falso.estado.mortes == []


def test_so_encerra_jobrunner_da_ultima_coleta(app_falso):
    app_falso.entrar()
    r = app_falso.c.post("/jobrunner/encerrar", data={"server": "app-34", "pid": "4", "confirm": "4"})
    assert r.status_code == 409 and app_falso.estado.mortes == []


def test_viewer_nao_encerra(app_falso):
    app_falso.entrar("tv")
    r = app_falso.c.post("/jobrunner/encerrar", data={"server": "app-34", "pid": "4242", "confirm": "4242"})
    assert r.status_code == 403 and app_falso.estado.mortes == []


def test_botao_de_encerrar_aparece_na_tela(app_falso):
    app_falso.entrar()
    html = app_falso.c.get("/server/app-34").text
    assert 'class="svc-btn jr-kill"' in html and 'data-pid="4242"' in html


# ------------------------------------------------------------ script no host

def test_script_confere_nome_e_inicio_antes_de_matar():
    s = collector.script_encerrar_jobrunner(4242, "2026-10-15 08:00:00", "RM.Host.JobRunner")
    assert "Get-Process -Id 4242" in s and "Stop-Process -Id 4242 -Force" in s
    assert "'RM.Host.JobRunner'" in s and "'2026-10-15 08:00:00'" in s
    # inicio fora do formato nao entra no script (e desliga so essa conferencia)
    s = collector.script_encerrar_jobrunner(4242, "'; Remove-Item C:\\ -Recurse; '", "RM.Host.JobRunner")
    assert "Remove-Item" not in s


@pytest.mark.parametrize("pid,proc", [(0, "RM.Host.JobRunner"), (-1, "RM.Host.JobRunner"),
                                      (10, "x'; rm"), ("abc", "RM.Host.JobRunner")])
def test_script_recusa_entrada_invalida(pid, proc):
    with pytest.raises((ValueError, TypeError)):
        collector.script_encerrar_jobrunner(pid, None, proc)


def test_interpretar_respostas_do_host():
    assert collector.interpretar_encerramento("OK 5120", 9) == (
        True, "PID 9 encerrado (5120 MB de memória privada liberados)")
    assert not collector.interpretar_encerramento("GONE", 9)[0]
    assert "notepad" in collector.interpretar_encerramento("NOT_JR notepad", 9)[1]
    assert "reaproveitado" in collector.interpretar_encerramento("REUSED 2026-10-15 10:00:00", 9)[1]
    assert not collector.interpretar_encerramento("", 9)[0]


def test_alvo_so_da_lista_da_coleta():
    c = coleta()
    assert collector.jobrunner_alvo(c, "4242")["pid"] == 4242
    assert collector.jobrunner_alvo(c, 77)["pid"] == 77
    assert collector.jobrunner_alvo(c, 5) is None
    assert collector.jobrunner_alvo(c, "x") is None
    assert collector.jobrunner_alvo({}, 4242) is None


# ------------------------------------------------------------ Elasticsearch

@pytest.fixture
def com_elastic(app_falso, monkeypatch):
    monkeypatch.setenv("RMON_ES_URL", "https://elastic.exemplo:9200")
    monkeypatch.setenv("RMON_ES_API_KEY", "chave-de-teste")
    monkeypatch.setattr(db, "computadores", lambda: {"app-34": "HOSTA"})
    pedidos = []
    serie = [{"t": AGORA - 7200, "mb": 2048, "pid": 4242}, {"t": AGORA - 600, "mb": 9 * 1024, "pid": 4242}]

    def jobrunner_serie(cfg, nome, processo, horas=24):
        pedidos.append((nome, processo, horas))
        return serie

    monkeypatch.setattr(main.elastic, "jobrunner_serie", jobrunner_serie)
    monkeypatch.setattr(main.elastic, "vivos", lambda cfg, nomes, janela="now-15m": {"hosta": time.time() - 20})
    return SimpleNamespace(app=app_falso, pedidos=pedidos)


def test_tela_mostra_a_serie_do_elastic(com_elastic):
    com_elastic.app.entrar()
    html = com_elastic.app.c.get("/server/app-34?jr_h=168").text
    assert "Memória do maior processo de job (JobRunner)" in html and 'class="es-graf"' in html
    assert "pico de <b class=\"txt-red\">9.0 GB</b>" in html
    assert re.search(r"último dado há 2\d s", html)
    assert com_elastic.pedidos == [("hosta", "RM.Host.JobRunner", 168)]


def test_tela_sem_elastic_nao_tem_o_painel(app_falso):
    app_falso.entrar()
    assert "Memória do maior processo de job" not in app_falso.c.get("/server/app-34").text


def test_host_sem_winrm_mostra_o_jobrunner_do_elastic_sem_botao(com_elastic):
    est = com_elastic.app.estado
    est.coletas = [coleta(reachable=False, error="WinRMOperationTimeoutError",
                          jobrunners={"top": [{"pid": 4242, "private_mb": 9 * 1024}],
                                      "fonte": "elastic", "computer": "HOSTA"})]
    com_elastic.app.entrar()
    html = com_elastic.app.c.get("/server/app-34").text
    assert "leitura do Elasticsearch" in html and "PID 4242" in html
    assert "Processos de job do RM (JobRunner) <span class=\"src-tag elastic\">Elastic</span>" in html
    assert 'class="svc-btn jr-kill"' not in html and "acima de 8 GB" in html
    r = com_elastic.app.c.post("/jobrunner/encerrar", data={"server": "app-34", "pid": "4242", "confirm": "4242"})
    assert r.status_code == 409 and "Elasticsearch" in r.json()["msg"] and est.mortes == []


def test_nlb_mostra_de_onde_veio_cada_contagem(app_falso):
    main.STATE["inv"].servers[0].nlb = True
    app_falso.estado.coletas = [coleta(nlb={
        "status": 1008, "cluster": "vip", "host_id": 1, "auth": 7, "sess": 0,
        "auth_winrm": 7, "sess_winrm": 0, "auth_elastic": 9, "sess_elastic": 0,
        "fonte": {"auth": "winrm", "sess": "elastic"}})]
    app_falso.entrar()
    html = app_falso.c.get("/nlb").text
    # cada numero com a sua etiqueta, a vista - nao num tooltip
    assert '7 login(s) <span class="src-tag winrm">WinRM</span>' in html
    assert '0 sessão(ões) aberta(s) <span class="src-tag elastic">Elastic</span>' in html
    assert "Elastic 9/0 &middot; WinRM 7/0" in html


def test_nlb_mostra_a_saida_do_no_sem_contato(app_falso):
    """Reset pelo Incus visivel na tela: a etiqueta por no e o estado do canal."""
    srv = main.STATE["inv"].servers[0]
    srv.nlb, srv.incus_vm = True, "WIN2016"
    main.STATE["inv"].defaults = {"nlb": {"incus_ssh": "rmon-reset@10.10.10.161"}}
    app_falso.estado.config["nlb_state"] = {"incus": {
        "ok": True, "ts": AGORA - 60, "destino": "rmon-reset@10.10.10.161",
        "detail": "2 instância(s) liberada(s)",
        "vms": {"WIN2016": {"estado": "RUNNING", "mac": "10:66:6a:cc:09:90"}}}}
    app_falso.estado.coletas = [coleta(nlb={"status": 1007, "cluster": "vip", "host_id": 5})]
    app_falso.entrar()
    html = app_falso.c.get("/nlb").text
    assert "se travar: reinicia a VM WIN2016" in html
    assert '<span class="pill green">funcionando</span>' in html and "WIN2016</span> (RUNNING)" in html
    # sem VM no inventario: servidor fisico, so alerta
    srv.incus_vm = ""
    html = app_falso.c.get("/nlb").text
    assert "se travar: só alerta (servidor físico)" in html


def test_nlb_sem_canal_do_incus_diz_o_que_falta(app_falso):
    main.STATE["inv"].servers[0].nlb = True
    app_falso.entrar()
    html = app_falso.c.get("/nlb").text
    assert "não configurado" in html and "instalar-no-hypervisor.sh" in html


def test_tela_mostra_encerramento_automatico(app_falso):
    app_falso.entrar()
    html = app_falso.c.get("/server/app-34").text
    assert "Encerramento automático:" in html and "<b>ligado</b>" in html and "a partir de 4 GB" in html
    jr = coleta()["jobrunners"]
    jr["top"][0]["encerrado"] = AGORA - 20
    app_falso.estado.coletas[:] = [coleta(jobrunners=jr)]
    html = app_falso.c.get("/server/app-34").text
    assert "encerrado pelo RMon" in html
    app_falso.estado.config["alerts"] = {"jobrunner_kill_gb": 0, "jobrunner_kill_mb_min": 0}
    assert "<b>desligado</b>" in app_falso.c.get("/server/app-34").text


def test_tela_mostra_a_sonda_dos_jobrunners(app_falso, monkeypatch):
    from app import scheduler
    app_falso.entrar()
    monkeypatch.setattr(scheduler, "sonda_ultima", {"app-34": (time.time() - 7, True)})
    html = app_falso.c.get("/server/app-34").text
    assert "a sonda lê os processos de job a cada 15 s" in html and "última leitura há 7 s" in html
    monkeypatch.setattr(scheduler, "sonda_ultima", {"app-34": (time.time() - 3, False)})
    assert "sem resposta" in app_falso.c.get("/server/app-34").text
