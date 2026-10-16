"""SQL do estado dos alertas (app/db.py) contra um PostgreSQL de verdade.

Roda so com RMON_TEST_DSN apontando para um banco DESCARTAVEL (o CI sobe um):
o teste apaga as tabelas de alerta e de coleta. Sem a variavel, e pulado.
"""
from __future__ import annotations

import os

import pytest

from app import db

DSN = os.environ.get("RMON_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="RMON_TEST_DSN nao definido")


@pytest.fixture
def banco():
    db.init(DSN)
    db.init_db()
    with db._conn() as c:
        c.execute("TRUNCATE alerts_log, checks, audit_log, package_events")
    return db


def gravar(server, kind, problem, msg="", dias=0):
    with db._conn() as c:
        c.execute("INSERT INTO alerts_log (ts, server, kind, problem, message) "
                  "VALUES (now() - %s * interval '1 day', %s, %s, %s, %s)",
                  (float(dias), server, kind, problem, msg))


def test_aberto_e_o_que_terminou_em_raised(banco):
    gravar("a", "raised", "MEM", "memoria em 95%", dias=3)
    gravar("a", "resolved", "MEM", "memoria em 95%", dias=2)
    gravar("a", "raised", "DISK", "disco em 92%", dias=1)
    gravar("b", "raised", "svc:W3SVC", "parado", dias=2)
    gravar("b", "resolved", "svc:W3SVC", "parado", dias=1)
    gravar("b", "raised", "svc:W3SVC", "parado de novo")
    gravar("b", "package", "rm:versao", "atualizado")
    gravar("b", "nlb", "drain", "drenado")
    assert banco.open_alerts() == {"a": {"DISK": "disco em 92%"},
                                   "b": {"svc:W3SVC": "parado de novo"}}


def test_mesmo_instante_desempata_pelo_id(banco):
    with db._conn() as c:
        c.execute("INSERT INTO alerts_log (ts, server, kind, problem, message) VALUES "
                  "('2026-10-01 10:00', 'a', 'raised', 'APP', 'x'), "
                  "('2026-10-01 10:00', 'a', 'resolved', 'APP', 'x')")
    assert banco.open_alerts() == {}


def test_prune_guarda_o_raised_de_problema_ainda_aberto(banco):
    gravar("a", "raised", "DISK", "disco em 91%", dias=200)
    gravar("a", "raised", "MEM", "memoria", dias=200)
    gravar("a", "resolved", "MEM", "memoria", dias=199)
    gravar("a", "package", "rm:versao", "velho", dias=400)
    banco.prune()
    with db._conn() as c:
        restam = c.execute("SELECT kind, problem FROM alerts_log ORDER BY id").fetchall()
    assert [(r["kind"], r["problem"]) for r in restam] == [("raised", "DISK")]
    assert banco.open_alerts() == {"a": {"DISK": "disco em 91%"}}


def test_tempos_do_aberto_contam_o_ultimo_lembrete(banco):
    gravar("a", "raised", "svc:X", "parado", dias=3)
    gravar("a", "reminder", "svc:X", "lembrete velho", dias=2.5)
    gravar("a", "resolved", "svc:X", "parado", dias=2)
    gravar("a", "raised", "svc:X", "parado de novo", dias=1)
    gravar("a", "raised", "DOWN", "sem contato", dias=0.5)
    gravar("a", "reminder", "DOWN", "segue", dias=0.25)
    t = banco.open_alerts_tempos()["a"]
    assert set(t) == {"svc:X", "DOWN"}
    # lembrete do episodio anterior nao conta
    assert t["svc:X"]["aviso"] == t["svc:X"]["desde"]
    assert t["DOWN"]["aviso"] - t["DOWN"]["desde"] == pytest.approx(0.25 * 86400, abs=5)


def test_ultima_acao_e_a_mais_recente_do_mesmo_problema(banco):
    assert banco.ultima_acao("a", "svc:RM.Host.Service") is None
    gravar("a", "acao", "svc:RM.Host.Service", "tentativa automatica", dias=2)
    gravar("a", "acao", "svc:RM.Host.Service", "tentativa automatica", dias=1)
    gravar("a", "raised", "svc:RM.Host.Service", "parado")      # outro tipo nao conta
    gravar("a", "acao", "svc:W3SVC", "outro servico")           # outro problema nao conta
    gravar("b", "acao", "svc:RM.Host.Service", "outro host")    # outro host nao conta
    with db._conn() as c:
        esperado = c.execute("SELECT extract(epoch FROM now() - interval '1 day') AS t").fetchone()["t"]
    assert banco.ultima_acao("a", "svc:RM.Host.Service") == pytest.approx(float(esperado), abs=5)
