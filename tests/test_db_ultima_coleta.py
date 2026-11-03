"""Ultima coleta de cada servidor (db.latest_per_server) contra PostgreSQL de verdade.

Roda so com RMON_TEST_DSN apontando para um banco DESCARTAVEL (o CI sobe um).
"""
from __future__ import annotations

import os

import pytest

from app import db

DSN = os.environ.get("RMON_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="RMON_TEST_DSN não definido")


@pytest.fixture
def banco():
    db.init(DSN)
    db.init_db()
    with db._conn() as c:
        c.execute("TRUNCATE checks")
    return db


def _ts(banco, server, iso):
    with banco._conn() as c:
        c.execute("UPDATE checks SET ts = %s WHERE id = (SELECT max(id) FROM checks WHERE server = %s)",
                  (iso, server))


def test_uma_linha_por_servidor_a_mais_recente(banco):
    banco.insert_check("app-34", {"reachable": True, "cpu": 10})
    _ts(banco, "app-34", "2026-11-03 09:00-03")
    banco.insert_check("app-34", {"reachable": True, "cpu": 20})
    _ts(banco, "app-34", "2026-11-03 10:00-03")
    # gravada depois, mas mais antiga: vale o ts, nao a ordem de insercao
    banco.insert_check("app-34", {"reachable": False, "cpu": 30})
    _ts(banco, "app-34", "2026-11-03 08:00-03")
    banco.insert_check("rh-248", {"reachable": False, "error": "WinRM"})

    rows = banco.latest_per_server()

    assert [r["server"] for r in rows] == ["app-34", "rh-248"]
    assert rows[0]["cpu"] == pytest.approx(20) and rows[0]["reachable"] is True
    assert rows[1]["error"] == "WinRM"
    assert isinstance(rows[0]["ts"], float)


def test_sem_coletas(banco):
    assert banco.latest_per_server() == []


def test_usa_o_indice_e_nao_ordena_a_tabela(banco):
    """Em 03/11 o DISTINCT ON ordenava a tabela inteira em disco (~600 MB por
    chamada) e o painel parava de abrir depois do login. A consulta tem de ir
    pelo indice (server, ts DESC), uma descida por servidor."""
    with banco._conn() as c:
        c.execute("SET enable_seqscan = off")
        plano = "\n".join(r["QUERY PLAN"] for r in c.execute("EXPLAIN " + banco._ULTIMA_COLETA))
    assert "idx_checks_server_ts" in plano
    assert "Unique" not in plano  # o DISTINCT ON que ordenava tudo
