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
                  "VALUES (now() - make_interval(days => %s), %s, %s, %s, %s)",
                  (dias, server, kind, problem, msg))


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
