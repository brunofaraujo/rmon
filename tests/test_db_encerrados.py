"""SQL do historico de encerrados (app/db.py) contra um PostgreSQL de verdade.

Roda so com RMON_TEST_DSN apontando para um banco DESCARTAVEL (o CI sobe um).
"""
from __future__ import annotations

import os
import time

import pytest

from app import db
from tests.test_encerrados import AUTO_RITMO, MANUAL

DSN = os.environ.get("RMON_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="RMON_TEST_DSN não definido")


@pytest.fixture
def banco():
    db.init(DSN)
    db.init_db()
    with db._conn() as c:
        c.execute("TRUNCATE job_kills, audit_log, checks")
        c.execute("DELETE FROM app_config WHERE key = 'job_kills_carga'")
    return db


def test_grava_le_e_completa(banco):
    agora = time.time()
    banco.record_job_kill("app-34", 5956, "auto", "rmon", True,
                          {"idjob": 2460362, "usuario": "maria", "mb": 777, "amostras": [[agora - 60, 64]]},
                          ts=agora - 30)
    banco.record_job_kill("app-34", 1, "manual", "admin", False, {"retorno": "Acesso negado"})
    a, b = banco.recent_job_kills()
    assert a["origem"] == "manual" and a["ok"] is False and a["idjob"] is None
    assert b["idjob"] == 2460362 and b["usuario"] == "maria" and abs(b["ts"] - (agora - 30)) < 1
    banco.job_kill_guardar(b["id"], {"coletas": [{"t": agora, "mb": 777}]})
    b = banco.recent_job_kills()[1]
    assert b["dados"]["mb"] == 777 and b["dados"]["coletas"][0]["mb"] == 777


def test_jobrunner_nas_coletas(banco):
    banco.insert_check("app-34", {"reachable": True, "mem_pct": 40.1, "commit_pct": 7.5,
                                  "jobrunners": {"top": [{"pid": 5956, "private_mb": 777},
                                                         {"pid": 200, "private_mb": 65}]}})
    banco.insert_check("app-34", {"reachable": True, "mem_pct": 35.6, "jobrunners": {"count": 0}})
    banco.insert_check("outro", {"reachable": True, "mem_pct": 99,
                                 "jobrunners": {"top": [{"pid": 5956, "private_mb": 9}]}})
    agora = time.time()
    pontos = banco.jobrunner_nas_coletas("app-34", 5956, agora - 60, agora + 60)
    assert [(p["mb"], p["mem_pct"]) for p in pontos] == [(777, pytest.approx(40.1)), (None, pytest.approx(35.6))]


def test_carga_dos_encerramentos_da_auditoria_uma_vez(banco):
    banco.audit("rmon", "jobrunner_kill_auto", AUTO_RITMO)
    banco.audit("admin", "jobrunner_kill", MANUAL)
    banco.audit("admin", "login", "nada a ver")
    banco._carregar_job_kills_antigos()
    banco._carregar_job_kills_antigos()
    kills = banco.recent_job_kills()
    assert sorted((k["pid"], k["origem"], k["por"]) for k in kills) == [
        (5420, "manual", "admin"), (5956, "auto", "rmon")]
