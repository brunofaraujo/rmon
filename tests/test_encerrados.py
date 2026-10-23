"""Historico dos jobs encerrados pelo RMon (aba Jobs).

Em 23/10 o canal recebeu "RMon encerrou o processo de job PID 5956 (job 2460362
'Gerar Relatório' de maria)" e nao havia na tela onde rever o evento. Agora cada
encerramento fica registrado com o motivo e a memoria, e a aba Jobs junta o que
o banco do RM (relatorio, filtro, nome, tempo de execucao, pedido de novo), as
coletas e o Elastic dizem dele.
"""
from __future__ import annotations

import struct
import time

import pytest

from app import db, elastic, encerrados, jobstats
from tests.test_estado_alertas import ambiente, encerrar, jr_de, ok  # noqa: F401  (fixtures)
from tests.test_jobs_presos import aberta, kill_com_antes  # noqa: F401  (fixture)
from tests.test_rotas import app_falso  # noqa: F401  (fixture)

# as linhas da auditoria de 20 e 23/10, como estao no banco de producao
AUTO_RITMO = ("fin-sge-jobs-222 PID 5956 (job 2460362 'Gerar Relatório' de maria; cresceu de 64 para 777 MB "
              "em 1 min (891 MB/min; limite: 400 MB/min acima de 768 MB)) -> True: PID 5956 encerrado "
              "(777 MB de memória privada liberados)")
AUTO_GB = ("fin-sge-app-190 PID 18564 (job 2450016 'Gerar Relatório' de Marianemelo; 5.2 GB de memória "
           "privada (limite: 4 GB)) -> True: PID 18564 encerrado (5328 MB de memória privada liberados)")
MANUAL = ("fin-sge-jobs-222 PID 5420 (0.7 GB, job 2450147 'Gerar Relatório' de kellygomes preso) -> True: "
          "PID 5420 encerrado (674 MB de memória privada liberados)")


# ------------------------------------------------------------ carga da auditoria

def test_auditoria_vira_registro():
    k = db.kill_da_auditoria("jobrunner_kill_auto", "rmon", AUTO_RITMO)
    assert k["server"] == "fin-sge-jobs-222" and k["pid"] == 5956 and k["origem"] == "auto" and k["ok"]
    assert k["dados"].items() >= {"idjob": 2460362, "usuario": "maria", "regra": "ritmo", "antes_mb": 64,
                                  "mb": 777, "mb_min": 891, "liberado_mb": 777}.items()
    assert k["dados"]["motivo"].startswith("cresceu de 64 para 777 MB")

    k = db.kill_da_auditoria("jobrunner_kill_auto", "rmon", AUTO_GB)
    assert k["dados"]["regra"] == "memoria" and k["dados"]["liberado_mb"] == 5328

    k = db.kill_da_auditoria("jobrunner_kill", "admin", MANUAL)
    assert k["origem"] == "manual" and k["por"] == "admin"
    assert k["dados"].items() >= {"idjob": 2450147, "usuario": "kellygomes", "preso": True, "mb": 717}.items()


def test_auditoria_sem_encerramento_fica_de_fora():
    ja_tinha = "app-34 PID 9 (job 1 'X' de y; 5.0 GB de memória privada (limite: 4 GB)) -> False: o PID 9 já tinha terminado"
    assert db.kill_da_auditoria("jobrunner_kill_auto", "rmon", ja_tinha) is None
    negado = "app-34 PID 9 (job não identificado; 5.0 GB de memória privada (limite: 4 GB)) -> False: Acesso negado"
    k = db.kill_da_auditoria("jobrunner_kill_auto", "rmon", negado)
    assert k["ok"] is False and k["dados"]["retorno"] == "Acesso negado" and "idjob" not in k["dados"]
    assert db.kill_da_auditoria("jobrunner_kill", "admin", "texto qualquer") is None


# ------------------------------------------------------------ registro na hora

def test_encerramento_automatico_fica_registrado_com_os_numeros(ambiente, encerrar):
    ambiente.ciclo(**ok(jobrunners=jr_de(340, pid=2172)))
    ambiente.relogio[0] += 60
    ambiente.ciclo(**ok(jobrunners=jr_de(854, pid=2172)))
    [k] = ambiente.banco.kills
    assert k["server"] == "app-34" and k["pid"] == 2172 and k["origem"] == "auto" and k["ok"]
    d = k["dados"]
    assert d["regra"] == "ritmo" and d["antes_mb"] == 340 and d["mb"] == 854 and d["mb_min"] == 514
    assert d["idjob"] == 2447428 and d["usuario"] == "DJACY.MARIANO" and d["liberado_mb"] == 4280
    # as leituras que levaram ao encerramento vao junto: a curva da tela
    assert [mb for _, mb in d["amostras"]] == [340, 854]
    assert d["computer"] == "WIN2016" and k["ts"] == ambiente.relogio[0]


def test_falha_ao_encerrar_tambem_fica_registrada(ambiente, encerrar, monkeypatch):
    from app import scheduler
    monkeypatch.setattr(scheduler, "_jr_falhou", set())
    monkeypatch.setattr(scheduler, "kill_jobrunner", lambda *a: (False, "Acesso negado"))
    ambiente.ciclo(**ok(jobrunners=jr_de(4280)))
    [k] = ambiente.banco.kills
    assert k["ok"] is False and k["dados"]["retorno"] == "Acesso negado" and k["dados"]["regra"] == "memoria"


def test_encerramento_pelo_botao_fica_registrado(app_falso, aberta, monkeypatch):  # noqa: F811
    kill_com_antes(app_falso, monkeypatch)
    app_falso.entrar()
    r = app_falso.c.post("/jobrunner/encerrar", data={"server": "app-34", "pid": "9001", "confirm": "9001",
                                                      "idjob": "2450147", "idexec": "1"})
    assert r.json()["ok"]
    [(args, _)] = app_falso.estado.kills
    server, pid, origem, por, ok_, dados = args
    assert (server, pid, origem, por, ok_) == ("app-34", 9001, "manual", "admin", True)
    assert dados["preso"] and dados["idjob"] == 2450147 and dados["usuario"] == "kellygomes"
    assert dados["executor"] == "SRV06N:8052" and dados["regra"] == "manual"


# ------------------------------------------------------------ banco do RM

def blob(rid=None, filtro=None):
    b = b"\x00\x01cabecalho"
    if rid is not None:
        b += b"\x08\x08" + struct.pack("<i", 7) + b"..\x08\x08" + struct.pack("<i", 11529) + b"\x00\x08\x08" + struct.pack("<i", rid)
    if filtro is not None:
        b += f"xx(({filtro}))\x06\r<ParametersSet>".encode()
    return b


def test_relatorio_e_filtro_saem_do_valorparametros():
    p = jobstats.parametros_do_job(blob(747, "SMATRICPL.CODCOLIGADA = '3' AND SMATRICPL.CODTURMA = 'QUA.040.311'"))
    assert p == {"relatorio": 747, "sem_filtro": False,
                 "filtro": "SMATRICPL.CODCOLIGADA = '3' AND SMATRICPL.CODTURMA = 'QUA.040.311'"}
    assert jobstats.parametros_do_job(blob(111)) == {"relatorio": 111, "sem_filtro": True, "filtro": None}
    longo = jobstats.parametros_do_job(blob(406, " OR ".join(["(STURMA.CODTURMA = 'X')"] * 40)))
    assert longo["sem_filtro"] is False and longo["filtro"].endswith("…") and len(longo["filtro"]) <= 301
    assert jobstats.parametros_do_job(None)["sem_filtro"] is None


class Cursor:
    def __init__(self, b):
        self.b = b
        self._r = []

    def execute(self, sql, params=()):
        self.b.sqls.append((sql, params))
        if "INFORMATION_SCHEMA" in sql:
            self._r = [(c,) for c in ("IDEXECUCAO", "SERVIDOR", "PROCESSID", "DATAINIEXEC", "STATUS",
                                      "MENSAGEMSTATUS", "PERCPROGRESSO")]
        elif sql.startswith("SET TEXTSIZE"):
            self._r = []
        elif "LEFT JOIN dbo.GUSUARIO" in sql:
            self._r = self.b.execucoes
        elif "FROM dbo.RRPTREPORT" in sql:
            self._r = [(747, "SENAI-EP.002", "[SENAI] - Histórico Escolar (Novo Modelo)", 0)]
        elif "J.IDJOB > %s" in sql:
            self._r = self.b.depois
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return list(self._r)


class BancoRM:
    def __init__(self):
        self.sqls = []
        # 23/10: o job "comecou" 38 s depois do encerramento no relogio da VM;
        # no do SQL, 18 s antes. idade no SQL 1218 s, encerrado ha 1200 s
        self.execucoes = [(2460362, 1, "srv11:8052", "5956", "2026-10-23 14:10:21", 1218, 7,
                           "Ocorreu uma falha crítica no processo.", None, "Gerar Relatório",
                           "RptProcGenerateReport", "maria", "MARIA DE FÁTIMA DE SOUZA", blob(747, "A = '1'"))]
        self.depois = [(2460400, 2, "2026-10-23 14:20:02", 12, "Gerar Relatório", blob(747, "A = '1'")),
                       (2460401, 5, "2026-10-23 14:25:00", 3, "Gerar Relatório", blob(12, "B = '2'")),
                       (2460402, 7, "2026-10-23 14:30:00", None, "Gerar Relatório", blob(747))]

    def cursor(self):
        return Cursor(self)

    def close(self):
        pass


@pytest.fixture
def rm(monkeypatch):
    b = BancoRM()
    monkeypatch.setattr(jobstats, "_connect", lambda timeout=10: b)
    monkeypatch.setattr(jobstats, "_colunas", None)
    monkeypatch.setattr(jobstats, "_col_inicio", None)
    monkeypatch.setattr(jobstats, "_cache_enc", {})
    return b


def test_detalhes_do_job_encerrado(rm):
    d = jobstats.detalhes_encerrados([{"idjob": 2460362, "seg": 1200}])[2460362]
    assert d["dur_s"] == 18 and d["status_txt"] == "falha crítica" and d["executor"] == "srv11:8052"
    assert d["usuario_nome"] == "MARIA DE FÁTIMA DE SOUZA" and d["usuario"] == "maria"
    assert d["relatorio"]["codigo"] == "SENAI-EP.002" and d["relatorio"]["filtro_obrigatorio"] is False
    assert d["sem_filtro"] is False and d["filtro"] == "A = '1'"
    # so o MESMO relatorio conta como pedido de novo
    assert [(x["idjob"], x["status_txt"], x["sem_filtro"]) for x in d["depois"]] == [
        (2460400, "concluído", False), (2460402, "falha crítica", True)]
    # a busca de "depois" vai pela chave, a partir do job encerrado
    sql, params = [s for s in rm.sqls if "J.IDJOB > %s" in s[0]][0]
    assert params[:4] == (2460362, 2480362, "maria", "RptProcGenerateReport")


def test_detalhes_ficam_em_memoria_quando_nao_mudam_mais(rm):
    jobstats.detalhes_encerrados([{"idjob": 2460362, "seg": 2 * 86400}])
    n = len(rm.sqls)
    jobstats.detalhes_encerrados([{"idjob": 2460362, "seg": 2 * 86400 + 600}])
    assert len(rm.sqls) == n


def test_sem_sql_configurado_nao_quebra(monkeypatch):
    monkeypatch.setattr(jobstats, "_connect", lambda timeout=10: None)
    monkeypatch.setattr(jobstats, "_cache_enc", {})
    assert jobstats.detalhes_encerrados([{"idjob": 1, "seg": 10}]) is None


# ------------------------------------------------------------ tela

def test_duracao():
    assert [encerrados.duracao(s) for s in (18, 90, 600, 13500, 2 * 86400 + 3600)] == [
        "18 s", "1 min 30 s", "10 min", "3h45", "2d 1h"]


def test_curva_junta_rmon_coletas_e_elastic():
    kill = 10_000.0
    pontos = encerrados._pontos({"amostras": [[9940, 64]], "mb": 777},
                                [{"t": 9880, "mb": 63}, {"t": 9942, "mb": 70}, {"t": 9990, "mb": None}],
                                {"pontos": [{"t": 9970, "mb": 332}]}, kill - 600, kill)
    assert pontos == [(9880, 63), (9940, 70), (9970, 332), (kill, 777)]
    g = encerrados.grafico(pontos)
    assert g["pico_mb"] == 777 and g["n"] == 4 and g["fim_x"] == 237 and g["linha"].startswith("3.0,")
    assert encerrados.grafico(pontos[:1]) is None


def kill_de_23_10(agora):
    return {"id": 8, "ts": agora - 1200, "server": "app-34", "pid": 5956, "origem": "auto", "por": "rmon",
            "ok": True, "idjob": 2460362, "usuario": "maria",
            "dados": {"regra": "ritmo", "motivo": "cresceu de 64 para 777 MB em 1 min (891 MB/min; limite: "
                                                  "400 MB/min acima de 768 MB)",
                      "mb": 777, "antes_mb": 64, "mb_min": 891, "liberado_mb": 777,
                      "amostras": [[agora - 1260, 64]], "computer": "SRV11"}}


def test_aba_jobs_mostra_o_encerramento_detalhado(app_falso, rm, monkeypatch):  # noqa: F811
    agora = time.time()
    monkeypatch.setattr(jobstats, "pool_summary", lambda w: {
        "ok": 1, "failed": 0, "total": 1, "requesters": 1, "by_server": [], "top_requesters": [],
        "recent_failures": [], "window_min": w, "error": None})
    monkeypatch.setattr(jobstats, "queue", lambda *a, **k: {"pending": 0, "stuck": 0, "waiting": 0, "items": []})
    monkeypatch.setattr(db, "jobrunner_nas_coletas", lambda *a: [
        {"t": agora - 1200 + 3, "mb": 777, "mem_pct": 40.1, "commit_pct": 7.5}])
    guardado = []
    monkeypatch.setattr(db, "job_kill_guardar", lambda i, extra: guardado.append((i, extra)))
    # o de 20/10, preso, sem coleta perto do encerramento e sem o job no RM
    preso = {**kill_de_23_10(agora), "id": 3, "ts": agora - 3 * 86400, "origem": "manual", "por": "admin",
             "idjob": 999, "dados": {"regra": "manual", "preso": True, "mb": 717, "liberado_mb": 674}}
    app_falso.estado.encerrados[:] = [kill_de_23_10(agora), preso]
    app_falso.entrar()
    r = app_falso.c.get("/jobs")
    assert r.status_code == 200
    html = r.text
    assert "Job preso, encerrado por admin" in html and "<b>674 MB</b> liberados" in html
    assert 'id="encerrados"' in html and 'href="#encerrados"' in html
    assert "[SENAI] - Histórico Escolar (Novo Modelo)" in html and "SENAI-EP.002" in html
    assert "MARIA DE FÁTIMA DE SOUZA</b></a> (maria)" in html
    assert "rodou <b>18 s</b> até ser encerrado" in html
    assert "Crescimento rápido de memória" in html and "891 MB/min" in html
    assert "<b>777 MB</b> liberados" in html and "servidor com <b>40%</b> da memória em uso" in html
    assert "falha crítica" in html and "Pediu de novo 2" in html and "concluído em 12 s" in html
    assert 'class="enc-curva"' in html and 'data-copiar="2460362"' in html
    # leitura assentada (mais de 3 min) e com o RM: guardada no registro, nao rele
    assert guardado and guardado[0][0] == 8 and "coletas" in guardado[0][1]


def test_aba_jobs_sem_encerramentos(app_falso, monkeypatch):  # noqa: F811
    monkeypatch.setattr(jobstats, "pool_summary", lambda w: {
        "ok": 1, "failed": 0, "total": 1, "requesters": 1, "by_server": [], "top_requesters": [],
        "recent_failures": [], "window_min": w, "error": None})
    monkeypatch.setattr(jobstats, "queue", lambda *a, **k: {"pending": 0, "stuck": 0, "waiting": 0, "items": []})
    app_falso.entrar()
    html = app_falso.c.get("/jobs").text
    assert "Nenhum processo de job encerrado nos últimos 30 dias." in html


def test_historico_com_falha_e_sem_rm(monkeypatch):
    agora = time.time()
    monkeypatch.setattr(db, "recent_job_kills", lambda d, l: [
        {**kill_de_23_10(agora), "ok": False, "dados": {"retorno": "Acesso negado", "regra": "memoria"}}])
    monkeypatch.setattr(db, "jobrunner_nas_coletas", lambda *a: [])
    monkeypatch.setattr(db, "job_kill_guardar", lambda *a: None)
    monkeypatch.setattr(jobstats, "detalhes_encerrados", lambda ev: None)
    monkeypatch.setattr(elastic, "ativo", lambda cfg: False)
    from types import SimpleNamespace
    h = encerrados.historico(SimpleNamespace(servers=[], defaults={}), agora=agora)
    assert h["rm_erro"] and h["falhas"] == 1 and h["auto"] == 0
    [e] = h["itens"]
    assert e["retorno"] == "Acesso negado" and e["dur_s"] is None and e["usuario"] == "maria"
    assert e["mem_pct"] is None and e["grafico"] is None


def test_elastic_fora_nao_atrasa_a_tela(monkeypatch):
    agora = time.time()
    monkeypatch.setattr(db, "recent_job_kills", lambda d, l: [
        {**kill_de_23_10(agora), "id": i, "ts": agora - 600 * i} for i in range(1, 6)])
    monkeypatch.setattr(db, "jobrunner_nas_coletas", lambda *a: [])
    monkeypatch.setattr(db, "job_kill_guardar", lambda *a: None)
    monkeypatch.setattr(jobstats, "detalhes_encerrados", lambda ev: {})
    monkeypatch.setattr(elastic, "ativo", lambda cfg: True)
    chamadas = []
    monkeypatch.setattr(elastic, "jobrunner_pid", lambda *a: chamadas.append(a) and None)
    from types import SimpleNamespace
    h = encerrados.historico(SimpleNamespace(servers=[], defaults={}), agora=agora)
    assert len(h["itens"]) == 5 and len(chamadas) == 1
