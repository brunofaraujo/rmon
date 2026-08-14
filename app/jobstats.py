"""Estatisticas de jobs do TOTVS RM lidas do SQL Server (GJOBXEXECUCAO).

Conta sucesso vs falha nas ultimas N horas/minutos pela DATAFIMEXEC. Config SQL vem
do .env (RMON_SQL_*). Inerte (retorna None) se o SQL nao estiver configurado.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger("rmon.jobstats")


def query(window_min: int = 15, success_status=(2,), failed_status=(5, 7), servidor: str | None = None) -> dict[str, Any] | None:
    host = os.environ.get("RMON_SQL_HOST")
    user = os.environ.get("RMON_SQL_USER")
    pw = os.environ.get("RMON_SQL_PASSWORD")
    if not (host and user and pw):
        return None
    succ = ",".join(str(int(s)) for s in (success_status or [])) or "NULL"
    fail = ",".join(str(int(s)) for s in (failed_status or [])) or "NULL"
    w = int(window_min)
    # filtra pelo executor (host do RM.Host, ex.: SRV11) -> so os jobs processados por ESTA maquina
    srv_filter, params = "", []
    if servidor:
        clean = re.sub(r"[^A-Za-z0-9_.-]", "", servidor)
        if clean:
            srv_filter = " AND SERVIDOR LIKE %s"
            params.append(clean + ":%")
    where = f"DATAFIMEXEC >= DATEADD(minute, -{w}, GETDATE()){srv_filter}"
    try:
        import pymssql
        conn = pymssql.connect(
            server=host, port=int(os.environ.get("RMON_SQL_PORT", 1433)),
            user=user, password=pw, database=os.environ.get("RMON_SQL_DB"),
            timeout=10, login_timeout=8,
        )
        cur = conn.cursor()
        cur.execute(
            f"""SELECT SUM(CASE WHEN STATUS IN ({succ}) THEN 1 ELSE 0 END),
                       SUM(CASE WHEN STATUS IN ({fail}) THEN 1 ELSE 0 END),
                       COUNT(*), COUNT(DISTINCT RECCREATEDBY)
                FROM dbo.GJOBXEXECUCAO WHERE {where}""", tuple(params))
        row = cur.fetchone()
        cur.execute(
            f"""SELECT TOP 6 RECCREATEDBY, COUNT(*) c FROM dbo.GJOBXEXECUCAO
                WHERE {where} AND RECCREATEDBY IS NOT NULL
                GROUP BY RECCREATEDBY ORDER BY c DESC""", tuple(params))
        top = [{"user": r[0], "c": int(r[1])} for r in cur.fetchall()]
        conn.close()
        return {"ok": int(row[0] or 0), "failed": int(row[1] or 0), "total": int(row[2] or 0),
                "requesters": int(row[3] or 0), "top_requesters": top, "window_min": w, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats: %s", exc)
        return {"ok": None, "failed": None, "total": None, "requesters": None, "top_requesters": [],
                "window_min": w, "error": f"{type(exc).__name__}: {exc}"[:150]}


def pool_summary(window_min: int = 60, success_status=(2,), failed_status=(5, 7)) -> dict[str, Any] | None:
    """Agregado do POOL de job servers (todos os executores) na janela."""
    host = os.environ.get("RMON_SQL_HOST")
    user = os.environ.get("RMON_SQL_USER")
    pw = os.environ.get("RMON_SQL_PASSWORD")
    if not (host and user and pw):
        return None
    succ = ",".join(str(int(s)) for s in (success_status or [])) or "NULL"
    fail = ",".join(str(int(s)) for s in (failed_status or [])) or "NULL"
    w = int(window_min)
    win = f"DATAFIMEXEC >= DATEADD(minute, -{w}, GETDATE())"
    hostexpr = "LEFT(SERVIDOR, CHARINDEX(':', SERVIDOR + ':') - 1)"
    try:
        import pymssql
        conn = pymssql.connect(
            server=host, port=int(os.environ.get("RMON_SQL_PORT", 1433)),
            user=user, password=pw, database=os.environ.get("RMON_SQL_DB"), timeout=15, login_timeout=8)
        cur = conn.cursor()
        cur.execute(f"""SELECT SUM(CASE WHEN STATUS IN ({succ}) THEN 1 ELSE 0 END),
                               SUM(CASE WHEN STATUS IN ({fail}) THEN 1 ELSE 0 END),
                               COUNT(*), COUNT(DISTINCT RECCREATEDBY) FROM dbo.GJOBXEXECUCAO WHERE {win}""")
        t = cur.fetchone()
        cur.execute(f"""SELECT {hostexpr} h, SUM(CASE WHEN STATUS IN ({succ}) THEN 1 ELSE 0 END),
                        SUM(CASE WHEN STATUS IN ({fail}) THEN 1 ELSE 0 END), COUNT(*)
                        FROM dbo.GJOBXEXECUCAO WHERE {win} GROUP BY {hostexpr} ORDER BY COUNT(*) DESC""")
        by_server = [{"host": r[0], "ok": int(r[1] or 0), "failed": int(r[2] or 0), "total": int(r[3] or 0)} for r in cur.fetchall()]
        cur.execute(f"""SELECT TOP 15 RECCREATEDBY, COUNT(*) c FROM dbo.GJOBXEXECUCAO
                        WHERE {win} AND RECCREATEDBY IS NOT NULL GROUP BY RECCREATEDBY ORDER BY c DESC""")
        top = [{"user": r[0], "c": int(r[1])} for r in cur.fetchall()]
        cur.execute(f"""SELECT TOP 25 IDJOB, SERVIDOR, RECCREATEDBY, MENSAGEMSTATUS, DATAFIMEXEC
                        FROM dbo.GJOBXEXECUCAO WHERE {win} AND STATUS IN ({fail}) ORDER BY DATAFIMEXEC DESC""")
        fails = [{"idjob": r[0], "servidor": r[1], "user": r[2], "msg": (r[3] or "")[:160],
                  "when": str(r[4])[:19]} for r in cur.fetchall()]
        conn.close()
        return {"ok": int(t[0] or 0), "failed": int(t[1] or 0), "total": int(t[2] or 0),
                "requesters": int(t[3] or 0), "by_server": by_server, "top_requesters": top,
                "recent_failures": fails, "window_min": w, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.pool: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"[:150], "window_min": w}
