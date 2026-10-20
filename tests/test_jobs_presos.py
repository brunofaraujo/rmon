"""Jobs presos: a tela diz QUAIS sao e COMO parar cada um.

Em 20/10 o mural mostrava "fila parada: 1 job(" (texto cortado) e nao havia
como descobrir que eram tres "Gerar Relatório" da mesma usuaria, presos ha 3h
em tres executores. A consulta agora traz a lista, as telas mostram o job e
quem pediu, e o encerramento pelo job confere no banco antes de matar - o
JobRunner e reaproveitado entre jobs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import collector, jobstats, main, scheduler
from tests.test_rotas import app_falso, coleta  # noqa: F401  (fixture)

COLUNAS = ["IDJOB", "IDEXECUCAO", "DATAPROGRAMADA", "SERVIDOR", "DATAINIEXEC", "DATAFIMEXEC",
           "PERCPROGRESSO", "STATUS", "EMCANCELAMENTO", "TEXTOSTATUS", "RECCREATEDBY",
           "PROCESSID"]

# Como o banco do RM devolveu em 20/10: um preso (relatorio) e um aguardando
LINHAS = [
    (2450147, 1, "srv11:8052", "5420", "2026-10-20 08:32:10", 194, "2026-10-20 08:32", 194,
     None, 1, None, "kellygomes", "Gerar Relatório", "RptProcGenerateReport", "kellygomes"),
    (2450548, 1, "win2016:8050", None, None, None, "2026-10-20 10:19", 87,
     None, 3, "O Processo entrou na rotina de Cancelamento de Jobs", "alane.alves",
     "Executar Fórmula Visual", "GlbWorkflowExecProc", "alane.alves"),
]


class Cursor:
    def __init__(self, banco):
        self.b = banco
        self._r = []

    def execute(self, sql, params=()):
        self.b.sqls.append(sql)
        if "INFORMATION_SCHEMA" in sql:
            self._r = [(c,) for c in COLUNAS]
        elif "TOP 1 E.PROCESSID" in sql:
            if self.b.falha_itens:
                raise RuntimeError("deadlock")
            self._r = [self.b.aberta] if self.b.aberta else []
        elif "LEFT JOIN dbo.GJOBX J" in sql:
            if self.b.falha_itens:
                raise RuntimeError("deadlock")
            self._r = self.b.itens
        elif "MAX(CASE WHEN viva = 1" in sql:
            self._r = [self.b.contagem]
        elif "DATEADD(hour, -24" in sql:
            self._r = [(0, 48)]
        elif "COUNT(*), MAX(DATEDIFF(minute, DATAPROGRAMADA" in sql:
            self._r = [self.b.espera]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self._r[0] if self._r else None

    def fetchall(self):
        return list(self._r)


class Banco:
    def __init__(self, contagem=(0, 1, 0, 194), espera=(1, 87), itens=LINHAS):
        self.contagem, self.espera, self.itens = contagem, espera, list(itens)
        self.aberta = None
        self.falha_itens = False
        self.sqls = []

    def cursor(self):
        return Cursor(self)

    def close(self):
        pass


@pytest.fixture
def banco(monkeypatch):
    b = Banco()
    monkeypatch.setattr(jobstats, "_connect", lambda timeout=10: b)
    monkeypatch.setattr(jobstats, "_colunas", None)
    monkeypatch.setattr(jobstats, "_col_inicio", None)
    return b


# ------------------------------------------------------------ consulta

def test_fila_traz_quais_jobs_estao_presos(banco):
    f = jobstats.queue("SRV11")
    assert (f["stuck"], f["waiting"]) == (1, 1)
    preso, aguardando = f["items"]
    assert preso == {**preso, "idjob": 2450147, "host": "srv11", "pid": 5420, "usuario": "kellygomes",
                     "nome": "Gerar Relatório", "tipo": "presa", "idade_min": 194,
                     "inicio": "2026-10-20 08:32:10", "espera_min": None}
    assert aguardando["tipo"] == "aguardando" and aguardando["pid"] is None
    assert aguardando["espera_min"] == 87 and "Cancelamento" in aguardando["texto"]
    sql = banco.sqls[-1]
    assert f"TOP {jobstats.ITENS_MAX}" in sql and "E.EMCANCELAMENTO IS NULL" in sql


def test_cancelado_antes_de_comecar_e_residuo(banco):
    """Job 2450548 (20/10): STATUS 3, sem inicio, "entrou na rotina de
    Cancelamento" 4 s depois de criado - ficou 6h como aguardando. As tres
    consultas (contagem, espera e lista) tiram esse caso da fila; a contagem o
    poe nos residuos (viva = 0)."""
    jobstats.queue("SRV11")
    regra = ("NOT (COALESCE({p}STATUS, -1) = 3 AND {p}DATAINIEXEC IS NULL "
             "AND COALESCE({p}TEXTOSTATUS, '') LIKE '%%rotina de cancelamento%%')")
    contagem, _, espera, lista = banco.sqls[1:]
    assert regra.format(p="") in contagem and "THEN 1 ELSE 0 END viva" in contagem
    assert regra.format(p="") in espera
    assert regra.format(p="E.") in lista


def test_rm_sem_texto_de_status_nao_perde_a_fila(banco, monkeypatch):
    monkeypatch.setattr(jobstats, "_colunas", {c for c in COLUNAS if c != "TEXTOSTATUS"})
    monkeypatch.setattr(jobstats, "_col_inicio", "DATAINIEXEC")
    jobstats.queue("SRV11")
    assert not any("rotina de cancelamento" in s for s in banco.sqls)
    assert "EMCANCELAMENTO IS NULL" in banco.sqls[0]


def test_sem_preso_nao_ha_consulta_a_mais(banco):
    banco.contagem, banco.espera = (3, 0, 0, 5), (0, None)
    f = jobstats.queue("SRV11")
    assert f["items"] == [] and not any("LEFT JOIN" in s for s in banco.sqls)


def test_lista_que_falha_nao_derruba_a_contagem(banco):
    banco.falha_itens = True
    f = jobstats.queue("SRV11")
    assert f["error"] is None and f["stuck"] == 1 and f["items"] == []


def test_execucao_aberta_lida_na_hora(banco):
    banco.aberta = ("5420", "srv11:8052", "2026-10-20 08:32:10", 1, "Gerar Relatório", "kellygomes")
    r = jobstats.execucao_aberta(2450147, 1)
    assert r == {"pid": 5420, "servidor": "srv11:8052", "host": "srv11", "inicio": "2026-10-20 08:32:10",
                 "aberta": True, "nome": "Gerar Relatório", "usuario": "kellygomes"}
    banco.aberta = None
    assert jobstats.execucao_aberta(2450147, 1)["aberta"] is False
    assert jobstats.execucao_aberta("x") is None


# ------------------------------------------------------------ alerta e script

def test_alerta_de_fila_travada_diz_qual_job():
    fila = {"done_recent": 0, "stuck": 3, "waiting": 0, "oldest_min": 194, "window_min": 30,
            "items": [{"idjob": 2450147, "nome": "Gerar Relatório", "usuario": "kellygomes", "pid": 5420},
                      {"idjob": 2450153}, {"idjob": 2450157}]}
    jb = {"ok": 0, "failed": 0, "window_min": 15, "queue": fila}
    texto = scheduler.job_problems(jb, {**scheduler.DEFAULT_ALERTS})["JOBQUEUE"]
    assert "'Gerar Relatório' de kellygomes (job 2450147, PID 5420) e mais 2" in texto


def test_script_pelo_job_so_mata_processo_mais_velho_que_o_job():
    s = collector.script_encerrar_jobrunner(5420, None, "RM.Host.JobRunner", "2026-10-20 08:34:10")
    assert "'2026-10-20 08:34:10'" in s and "-gt '2026-10-20 08:34:10'" in s
    # entrada torta nao entra no script
    s = collector.script_encerrar_jobrunner(5420, None, "RM.Host.JobRunner", "'; Remove-Item C:\\ ;'")
    assert "Remove-Item" not in s


# ------------------------------------------------------------ telas

def presos_na_coleta(**kw):
    item = {"idjob": 2450147, "idexec": 1, "servidor": "srv06n:8052", "host": "srv06n", "pid": 4242,
            "inicio": "2026-10-20 08:32:10", "idade_min": 194, "programada": "2026-10-20 08:32",
            "espera_min": None, "progresso": None, "status": 1, "texto": None,
            "usuario": "kellygomes", "nome": "Gerar Relatório", "classe": "RptProcGenerateReport",
            "tipo": "presa"}
    item.update(kw)
    fila = {"pending": 1, "running": 0, "stuck": 1, "waiting": 0, "orphans": 0, "oldest_min": 194,
            "done_recent": 0, "window_min": 30, "items": [item]}
    return coleta(jobs={"ok": 0, "failed": 0, "total": 0, "window_min": 15, "error": None,
                        "queue": fila})


def test_tela_do_servidor_lista_o_job_preso_com_botao(app_falso):
    app_falso.entrar()
    app_falso.estado.coletas[:] = [presos_na_coleta()]
    html = app_falso.c.get("/server/app-34").text
    assert 'id="presos"' in html and "Gerar Relatório" in html and "kellygomes" in html
    assert "3h14" in html and 'data-idjob="2450147"' in html and 'data-pid="4242"' in html
    assert "Encerrar job" in html and 'data-copiar="2450147"' in html
    assert "Cancelar pelo RM" in html


def test_aguardando_nao_tem_botao_e_diz_o_que_fazer(app_falso):
    app_falso.entrar()
    app_falso.estado.coletas[:] = [presos_na_coleta(tipo="aguardando", pid=None, idade_min=None,
                                                     espera_min=87, inicio=None)]
    html = app_falso.c.get("/server/app-34").text
    assert "nenhum executor começou" in html and "Encerrar job" not in html
    assert "1h27" in html


def test_pedido_repetido_e_destacado(app_falso):
    app_falso.entrar()
    c = presos_na_coleta()
    fila = c["jobs"]["queue"]
    fila["items"] = [fila["items"][0], {**fila["items"][0], "idjob": 2450153, "pid": 77}]
    fila["stuck"] = 2
    app_falso.estado.coletas[:] = [c]
    assert "pediu este mesmo job 2 vezes" in app_falso.c.get("/server/app-34").text


def test_link_do_mural_so_para_admin(app_falso):
    """O viewer fica preso ao mural (_kiosk_guard): link para /server o
    devolveria ao mural. A linha do job aparece para todos; o link, so ao admin."""
    app_falso.estado.coletas[:] = [presos_na_coleta()]
    app_falso.entrar()
    assert 'data-admin="1"' in app_falso.c.get("/tv").text
    app_falso.entrar("tv")
    assert 'data-admin=""' in app_falso.c.get("/").text
    js = (Path(main.__file__).parent / "static" / "tv.js").read_text(encoding="utf-8")
    assert "if (TOKEN || !ADMIN)" in js


def test_painel_diz_o_job_e_leva_a_lista(app_falso):
    app_falso.entrar()
    app_falso.estado.coletas[:] = [presos_na_coleta()]
    html = app_falso.c.get("/").text
    assert 'data-href="/server/app-34#presos"' in html and "ver e parar" in html
    assert "<b>Gerar Relatório</b> de kellygomes" in html


def test_tela_de_jobs_abre_pela_lista(app_falso, monkeypatch):
    monkeypatch.setattr(jobstats, "pool_summary", lambda w: {
        "ok": 1, "failed": 0, "total": 1, "requesters": 1, "by_server": [], "top_requesters": [],
        "recent_failures": [], "window_min": w, "error": None})
    fila = presos_na_coleta()["jobs"]["queue"]
    monkeypatch.setattr(jobstats, "queue", lambda *a, **k: fila)
    app_falso.entrar()
    html = app_falso.c.get("/jobs").text
    assert html.index('id="presos"') < html.index('class="kpis"')
    assert 'href="/server/app-34"' in html and 'data-idjob="2450147"' in html


def test_mural_mostra_o_job_pelo_nome(app_falso):
    app_falso.entrar()
    app_falso.estado.coletas[:] = [presos_na_coleta()]
    d = app_falso.c.get("/api/tv").json()
    assert d["servers"][0]["jobs"]["qj"] == [{"n": "Gerar Relatório", "u": "kellygomes", "j": 2450147,
                                             "t": "presa", "m": 194}]
    assert any("job preso há 3h14: Gerar Relatório de kellygomes (nº 2450147)" in i["text"]
               for i in d["issues"])


def test_mural_tem_linha_propria_para_o_job_preso():
    js = (Path(main.__file__).parent / "static" / "tv.js").read_text(encoding="utf-8")
    assert "function presoHTML(s)" in js and re.search(r"tagsHTML\(s, th\).*\n\s*presoHTML\(s\)", js)
    assert '<span class="jb warn">fila parada' not in js     # o texto que saia cortado no rodape
    assert '"atenção"' in js


# ------------------------------------------------------------ encerrar pelo job

@pytest.fixture
def aberta(monkeypatch):
    estado = {"r": {"pid": 9001, "servidor": "SRV06N:8052", "host": "SRV06N",
                    "inicio": "2026-10-20 08:32:10", "aberta": True,
                    "nome": "Gerar Relatório", "usuario": "kellygomes"}}
    monkeypatch.setattr(jobstats, "execucao_aberta", lambda idjob, idexec=None: estado["r"])
    return estado


def kill_com_antes(app_falso, monkeypatch):
    monkeypatch.setattr(main, "kill_jobrunner",
                        lambda srv, wc, pid, inicio, proc, antes=None:
                        app_falso.estado.mortes.append((srv.name, pid, inicio, antes))
                        or (True, f"PID {pid} encerrado"))


def test_encerrar_pelo_job_fora_dos_maiores(app_falso, aberta, monkeypatch):
    kill_com_antes(app_falso, monkeypatch)
    app_falso.entrar()
    r = app_falso.c.post("/jobrunner/encerrar", data={"server": "app-34", "pid": "9001", "confirm": "9001",
                                                      "idjob": "2450147", "idexec": "1"})
    assert r.json()["ok"]
    # PID fora da coleta: so encerra processo que ja existia quando o job comecou
    assert app_falso.estado.mortes == [("app-34", 9001, None, "2026-10-20 08:34:10")]
    assert "job 2450147 'Gerar Relatório' de kellygomes preso" in app_falso.estado.enviadas[-1]


def test_encerrar_pelo_job_dentro_dos_maiores_confere_a_hora_exata(app_falso, aberta, monkeypatch):
    kill_com_antes(app_falso, monkeypatch)
    aberta["r"] = {**aberta["r"], "pid": 4242}
    app_falso.entrar()
    r = app_falso.c.post("/jobrunner/encerrar", data={"server": "app-34", "pid": "4242", "confirm": "4242",
                                                      "idjob": "2450147"})
    assert r.json()["ok"]
    assert app_falso.estado.mortes == [("app-34", 4242, "2026-10-15 08:00:00", None)]


@pytest.mark.parametrize("mudanca, trecho", [
    ({"aberta": False}, "já terminou"),
    ({"pid": 1234}, "agora está no PID 1234"),
    ({"host": "SRV11"}, "roda em SRV11"),
])
def test_encerrar_pelo_job_recusa_quando_o_banco_discorda(app_falso, aberta, monkeypatch, mudanca, trecho):
    kill_com_antes(app_falso, monkeypatch)
    aberta["r"] = {**aberta["r"], **mudanca}
    app_falso.entrar()
    r = app_falso.c.post("/jobrunner/encerrar", data={"server": "app-34", "pid": "9001", "confirm": "9001",
                                                      "idjob": "2450147"})
    assert r.status_code == 409 and trecho in r.json()["msg"] and app_falso.estado.mortes == []


def test_encerrar_pelo_job_sem_banco_nao_faz_nada(app_falso, aberta, monkeypatch):
    kill_com_antes(app_falso, monkeypatch)
    aberta["r"] = None
    app_falso.entrar()
    r = app_falso.c.post("/jobrunner/encerrar", data={"server": "app-34", "pid": "9001", "confirm": "9001",
                                                      "idjob": "2450147"})
    assert r.status_code == 409 and app_falso.estado.mortes == []


def test_linha_do_job_so_fica_vermelha_com_a_fila_travada():
    """Job preso com o executor concluindo outros e aviso (cartao verde ou
    ambar): a linha acompanha, em ambar. Vermelho so no cartao em falha."""
    js = (Path(main.__file__).parent / "static" / "tv.js").read_text(encoding="utf-8")
    assert '(aguarda || s.sev < 2 ? " aguarda" : "")' in js
