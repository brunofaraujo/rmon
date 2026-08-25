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

# Colunas de inicio candidatas na GJOBXEXECUCAO: o nome varia entre versoes do
# RM, entao a coluna usada e descoberta no proprio banco (INFORMATION_SCHEMA) em
# vez de chutada - chutar errado derrubaria a consulta inteira da fila.
_COLS_INICIO = ("DATAINIEXEC", "DATAINICIOEXEC", "DATAINICIO", "RECCREATEDON")


def _connect(timeout: int = 10):
    """Conexao com a base do RM, ou None se o SQL nao estiver configurado."""
    host = os.environ.get("RMON_SQL_HOST")
    user = os.environ.get("RMON_SQL_USER")
    pw = os.environ.get("RMON_SQL_PASSWORD")
    if not (host and user and pw):
        return None
    import pymssql
    return pymssql.connect(
        server=host, port=int(os.environ.get("RMON_SQL_PORT", 1433)),
        user=user, password=pw, database=os.environ.get("RMON_SQL_DB"),
        timeout=timeout, login_timeout=8,
    )


# A coluna nao muda enquanto o processo vive: descobrir uma vez evita uma ida ao
# INFORMATION_SCHEMA por servidor a cada ciclo de coleta.
_col_inicio: str | None = None
_col_inicio_lida = False


def _coluna_inicio(cur) -> str | None:
    global _col_inicio, _col_inicio_lida
    if _col_inicio_lida:
        return _col_inicio
    cur.execute("""SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME = 'GJOBXEXECUCAO'""")
    existentes = {str(r[0]).upper() for r in cur.fetchall()}
    _col_inicio = next((c for c in _COLS_INICIO if c in existentes), None)
    _col_inicio_lida = True
    if _col_inicio is None:
        log.warning("GJOBXEXECUCAO sem coluna de inicio conhecida (%s): a idade da "
                    "fila fica indisponivel", ", ".join(_COLS_INICIO))
    return _col_inicio


def queue(servidor: str | None = None) -> dict[str, Any] | None:
    """Saude da FILA, nao das validacoes: o que entrou e nao saiu.

    Job que termina em erro e validacao/regra de negocio da aplicacao - nao e
    problema do servidor. O que caracteriza problema de verdade e a fila parar
    de andar: execucao sem DATAFIMEXEC envelhecendo e nenhuma conclusao nova
    saindo do pool. Sao esses dois numeros que esta consulta traz.
    """
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.queue: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"[:150]}
    if conn is None:
        return None
    srv_filter, params = "", []
    if servidor:
        limpo = re.sub(r"[^A-Za-z0-9_.-]", "", servidor)
        if limpo:
            srv_filter = " AND SERVIDOR LIKE %s"
            params.append(limpo + ":%")
    try:
        cur = conn.cursor()
        col = _coluna_inicio(cur)
        idade = (f"DATEDIFF(minute, MIN({col}), GETDATE())" if col else "NULL")
        # So o que entrou nas ultimas 24h conta como fila. Execucao antiga que
        # ficou com DATAFIMEXEC nulo para sempre (job abortado, restart do
        # RM.Host no meio) e lixo historico: incluida, ela abriria um alerta de
        # "fila travada" no primeiro ciclo e nunca mais fecharia - e um alerta
        # que nunca fecha e o mesmo que alerta nenhum, porque mascara o proximo.
        recorte = (f" AND {col} >= DATEADD(hour, -24, GETDATE())" if col else "")
        cur.execute(
            f"""SELECT COUNT(*), {idade} FROM dbo.GJOBXEXECUCAO
                WHERE DATAFIMEXEC IS NULL{recorte}{srv_filter}""", tuple(params))
        row = cur.fetchone()
        # A ultima conclusao e procurada so nas ultimas 24h: sem o recorte, o MAX
        # varreria a GJOBXEXECUCAO inteira (anos de historico) a cada ciclo. Se
        # nada concluiu em 24h, o resultado nulo ja diz o que precisava dizer.
        cur.execute(
            f"""SELECT DATEDIFF(minute, MAX(DATAFIMEXEC), GETDATE()) FROM dbo.GJOBXEXECUCAO
                WHERE DATAFIMEXEC >= DATEADD(hour, -24, GETDATE()){srv_filter}""", tuple(params))
        parada = cur.fetchone()
        conn.close()
        pendentes = int(row[0] or 0)
        return {"pending": pendentes,
                "oldest_min": (int(row[1]) if row[1] is not None else None),
                "since_last_min": (int(parada[0]) if parada and parada[0] is not None else None),
                "col": col, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.queue: %s", exc)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return {"pending": None, "oldest_min": None, "since_last_min": None,
                "col": None, "error": f"{type(exc).__name__}: {exc}"[:150]}


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
