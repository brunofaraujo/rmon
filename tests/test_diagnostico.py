"""Por que o job esta preso (app/diagnostico.py).

Os casos sao os do parque: o SENAI007 filtrado so pela coligada (23/10, dois
presos por horas, nenhum pedido assim concluiu em 30 dias), o TOTVS-074 sem
filtro (20/10), a Exportação de Dados que leva 40 min todo dia e nao esta presa.
"""
from __future__ import annotations

import threading

import pytest

from app import diagnostico, elastic, jobstats, scheduler
from tests.test_jobs_presos import presos_na_coleta
from tests.test_rotas import app_falso  # noqa: F401  (fixture)

SENAI007 = {"idjob": 2460450, "classe": "RptProcGenerateReport", "nome": "Gerar Relatório", "tipo": "presa",
            "idade_min": 117, "pid": 36320, "host": "win2016-2", "usuario": "elaine.florencio",
            "relatorio": {"id": 419, "codigo": "SENAI007",
                          "descricao": "Alunos por Disciplina - Para Assinatura(Lista de Presença)"},
            "sem_filtro": False, "filtro": "STURMADISC.CODCOLIGADA = '3'", "campos": ["CODCOLIGADA"]}
# o Elastic em 23/10, 16:10 a 16:30: um nucleo a 93%, memoria subindo
PROC_SENAI007 = {"cpu": 0.93, "mb_ini": 4154, "mb_fim": 4503, "seg": 1200}
EXPORTACAO = {"idjob": 2347303, "classe": "CstExportaDadosProcess", "nome": "Exportação de Dados",
              "tipo": "presa", "idade_min": 41, "pid": 21940, "host": "win2016"}
# as 8 execucoes anteriores: 1217 a 2677 s
ROTINA_EXPORTACAO = {"mesmo_job": {"concluidas": 8, "mediana_s": 2338, "p90_s": 2677, "min_s": 1217,
                                   "max_s": 2677}, "classe": None}


# ------------------------------------------------------------ filtro

def test_campos_do_filtro():
    assert jobstats.campos_do_filtro("STURMADISC.CODCOLIGADA = '3'") == ["CODCOLIGADA"]
    f = ("SMATRICPL.CODCOLIGADA = '3' AND SMATRICPL.IDPERLET = '471' AND SMATRICPL.CODFILIAL = '20001' "
         "AND SMATRICPL.CODTURMA = 'QUA.040.311'")
    assert jobstats.campos_do_filtro(f) == ["CODCOLIGADA", "CODFILIAL", "CODTURMA", "IDPERLET"]
    assert jobstats.campos_do_filtro("SALUNO.RA IN ('1', '2') OR (X.Y LIKE 'a%')") == ["RA", "Y"]
    assert jobstats.campos_do_filtro(None) == []


def test_corta_filtro_no_fechamento():
    assert jobstats.corta_filtro("A.B = '1'))\x06\r<ParametersSet>") == "A.B = '1'"
    assert jobstats.corta_filtro("x" * 400).endswith("…")
    assert jobstats.corta_filtro(None) is None


# ------------------------------------------------------------ o motivo

def test_senai007_so_pela_coligada():
    rel = {"n": 4, "concluidas": 0, "mediana_s": None, "p90_s": None,
           "grupos": [{"campos": ["CODCOLIGADA"], "n": 4, "concluidas": 0}]}
    d = diagnosticar_senai007(rel)
    assert d["tipo"] == "filtro" and d["motivo"].startswith("Filtro só por coligada")
    assert "SENAI007 em 30 dias: 4 pedido(s), nenhum concluído" in d["pistas"]
    assert "Filtro do pedido: STURMADISC.CODCOLIGADA = '3'" in d["pistas"]
    assert any("um núcleo a 93%" in p and "4154 para 4503 MB" in p and "+17 MB/min" in p for p in d["pistas"])


def diagnosticar_senai007(rel):
    return diagnostico.diagnosticar(SENAI007, rel, None, PROC_SENAI007)


def test_relatorio_sem_filtro():
    it = {**SENAI007, "sem_filtro": True, "filtro": None, "campos": [], "relatorio": {"id": 111, "codigo": "TOTVS-074"}}
    d = diagnostico.diagnosticar(it, None, None, None)
    assert d["tipo"] == "filtro" and d["motivo"].startswith("Relatório gerado sem filtro")


def test_filtro_diferente_do_que_conclui():
    # SENAI-EP.002: 41 de 42 pedidos filtram por turma e periodo e concluem em 16 s
    rel = {"n": 42, "concluidas": 41, "mediana_s": 16, "p90_s": 22,
           "grupos": [{"campos": ["CODCOLIGADA", "CODFILIAL", "CODTURMA", "IDPERLET"], "n": 41,
                       "concluidas": 41, "mediana_s": 16}]}
    it = {**SENAI007, "relatorio": {"id": 747, "codigo": "SENAI-EP.002"},
          "filtro": "SMATRICPL.CODCOLIGADA = '3' AND SMATRICPL.IDPERLET = '471'", "campos": ["CODCOLIGADA", "IDPERLET"]}
    d = diagnostico.diagnosticar(it, rel, None, None)
    assert d["tipo"] == "filtro"
    assert d["motivo"] == "Filtro diferente do usual: os pedidos que concluem filtram por coligada, filial, turma e período letivo."
    assert "SENAI-EP.002 em 30 dias: 42 pedido(s), 41 concluído(s), em 16 s na mediana" in d["pistas"]


def test_filtro_usual_nao_acusa_nada():
    rel = {"n": 42, "concluidas": 41, "mediana_s": 16, "p90_s": 22,
           "grupos": [{"campos": ["CODCOLIGADA", "CODTURMA"], "n": 41, "concluidas": 41, "mediana_s": 16}]}
    it = {**SENAI007, "campos": ["CODCOLIGADA", "CODTURMA"], "filtro": "A.CODCOLIGADA = '3' AND A.CODTURMA = 'X'"}
    d = diagnostico.diagnosticar(it, rel, None, PROC_SENAI007)
    assert d["tipo"] == "trabalhando"


def test_exportacao_diaria_esta_no_tempo_normal():
    d = diagnostico.diagnosticar(EXPORTACAO, None, ROTINA_EXPORTACAO, None)
    assert d["tipo"] == "normal" and "costuma levar até 45 min" in d["motivo"]
    assert "Este job já rodou 8 vez(es): de 20 min a 45 min, 39 min na mediana" in d["pistas"]
    # passou de 1,5x o normal: deixa de ser "normal"
    d = diagnostico.diagnosticar({**EXPORTACAO, "idade_min": 90}, None, ROTINA_EXPORTACAO, None)
    assert d["tipo"] == "desconhecido"


def test_processo_parado_ou_sem_pista():
    parado = {"cpu": 0.0, "mb_ini": 670, "mb_fim": 672, "seg": 900}
    assert diagnostico.diagnosticar(EXPORTACAO, None, None, parado)["tipo"] == "parado"
    d = diagnostico.diagnosticar(EXPORTACAO, None, None, None)
    assert d["tipo"] == "desconhecido" and d["pistas"] == []


# ------------------------------------------------------------ alerta e mural

def test_alerta_diz_o_relatorio_e_o_filtro():
    fila = {"done_recent": 0, "stuck": 2, "waiting": 0, "oldest_min": 117, "window_min": 30,
            "items": [{**SENAI007, "pid": 36320}]}
    texto = scheduler.job_problems({"ok": 0, "failed": 0, "window_min": 15, "queue": fila},
                                   {**scheduler.DEFAULT_ALERTS})["JOBQUEUE"]
    assert ("'Gerar Relatório' SENAI007 (filtro só por coligada) de elaine.florencio (job 2460450, PID 36320)"
            in texto)
    assert scheduler.filtro_curto({**SENAI007, "sem_filtro": True}) == "sem filtro"
    assert scheduler.filtro_curto({**SENAI007, "campos": ["CODCOLIGADA", "CODTURMA"]}) is None
    assert scheduler.filtro_curto(EXPORTACAO) is None


def test_mural_mostra_o_relatorio_e_o_filtro(app_falso):  # noqa: F811
    app_falso.entrar()
    app_falso.estado.coletas[:] = [presos_na_coleta(**{k: v for k, v in SENAI007.items() if k != "idade_min"})]
    qj = app_falso.c.get("/api/tv").json()["servers"][0]["jobs"]["qj"][0]
    assert qj["n"] == "SENAI007" and qj["f"] == "filtro só por coligada"


# ------------------------------------------------------------ leitura em paralelo

@pytest.fixture
def limpo(monkeypatch):
    monkeypatch.setattr(diagnostico, "_cache", {})
    monkeypatch.setattr(elastic, "ativo", lambda cfg: True)


def test_anotar_junta_historico_e_processo(limpo, monkeypatch):
    chamadas = []
    monkeypatch.setattr(jobstats, "historico_relatorio",
                        lambda rid, j: chamadas.append(("rel", rid)) or {"n": 4, "concluidas": 0, "grupos": []})
    monkeypatch.setattr(elastic, "processo_agora", lambda cfg, n, p, pid: chamadas.append(("proc", n, pid))
                        or PROC_SENAI007)
    from types import SimpleNamespace
    inv = SimpleNamespace(defaults={}, servers=[])
    itens = [dict(SENAI007), dict(SENAI007, idjob=2460545, pid=33316), {"tipo": "aguardando", "idjob": 1}]
    diagnostico.anotar(itens, inv)
    assert itens[0]["diag"]["tipo"] == "filtro" and "diag" not in itens[2]
    # o mesmo relatorio e lido uma vez so
    assert sorted(chamadas) == [("proc", "win2016-2", 33316), ("proc", "win2016-2", 36320), ("rel", 419)]
    diagnostico.anotar([dict(SENAI007)], inv)
    assert len(chamadas) == 3


def test_leitura_lenta_nao_segura_a_tela(limpo, monkeypatch):
    solta = threading.Event()
    monkeypatch.setattr(diagnostico, "PRAZO_S", 0.2)
    monkeypatch.setattr(jobstats, "historico_relatorio", lambda rid, j: solta.wait(5) and None)
    monkeypatch.setattr(elastic, "processo_agora", lambda *a: None)
    from types import SimpleNamespace
    itens = [dict(SENAI007)]
    diagnostico.anotar(itens, SimpleNamespace(defaults={}, servers=[]))
    solta.set()
    d = itens[0]["diag"]
    # o filtro vem da propria fila: o motivo sai mesmo sem o historico
    assert d["tipo"] == "filtro" and d["pistas"][-1].startswith("Ainda lendo o histórico")


def test_tela_mostra_o_provavel_motivo(app_falso, limpo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(jobstats, "historico_relatorio", lambda rid, j: {"n": 4, "concluidas": 0, "grupos": []})
    monkeypatch.setattr(elastic, "processo_agora", lambda *a: PROC_SENAI007)
    # com o Elastic ligado a tela do servidor tambem le a serie dele
    from app import db
    monkeypatch.setattr(db, "computadores", lambda: {"app-34": "WIN2016-2"})
    monkeypatch.setattr(elastic, "jobrunner_serie", lambda *a, **k: [])
    monkeypatch.setattr(elastic, "jobrunners_agora", lambda *a, **k: None)
    app_falso.entrar()
    app_falso.estado.coletas[:] = [presos_na_coleta(**{k: v for k, v in SENAI007.items() if k != "idade_min"})]
    html = app_falso.c.get("/server/app-34").text
    assert "Alunos por Disciplina - Para Assinatura(Lista de Presença)" in html and ">SENAI007<" in html
    assert 'class="preso-motivo filtro"' in html and "Filtro só por coligada" in html
    assert "SENAI007 em 30 dias: 4 pedido(s), nenhum concluído" in html


def test_job_com_pouco_historico_usa_a_rotina():
    # 2 execucoes do mesmo job nao bastam (a fila exige 3): vale a classe
    rot = {"mesmo_job": {"concluidas": 2, "mediana_s": 3000, "p90_s": 3000, "min_s": 2900, "max_s": 3000},
           "classe": {"concluidas": 202, "mediana_s": 1, "p90_s": 4, "min_s": 0, "max_s": 1724}}
    d = diagnostico.diagnosticar(EXPORTACAO, None, rot, None)
    assert d["tipo"] == "desconhecido"
