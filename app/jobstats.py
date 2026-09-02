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

# STATUS que o RM ja considera encerrados (2 = sucesso, 4 = abortada, 5 = erro,
# 6 = concluida com ressalva, 7 = falha critica). A pergunta e feita pelo
# negativo de proposito: um codigo desconhecido, de outra versao do RM, conta
# como execucao viva - errar para o lado de "ainda rodando" so adia um alerta,
# enquanto errar para o lado de "terminou" esconderia fila travada de verdade.
_STATUS_TERMINAIS = (2, 4, 5, 6, 7)


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


# As colunas nao mudam enquanto o processo vive: descobrir uma vez evita uma ida
# ao INFORMATION_SCHEMA por servidor a cada ciclo de coleta.
_colunas: set[str] | None = None
_col_inicio: str | None = None


def _descobrir(cur) -> None:
    global _colunas, _col_inicio
    if _colunas is not None:
        return
    cur.execute("""SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_NAME = 'GJOBXEXECUCAO'""")
    _colunas = {str(r[0]).upper() for r in cur.fetchall()}
    _col_inicio = next((c for c in _COLS_INICIO if c in _colunas), None)
    if _col_inicio is None:
        log.warning("GJOBXEXECUCAO sem coluna de inicio conhecida (%s): a idade da "
                    "fila fica indisponivel", ", ".join(_COLS_INICIO))


def _coluna_inicio(cur) -> str | None:
    _descobrir(cur)
    return _col_inicio


def _tem(cur, *cols: str) -> bool:
    """Esta versao do RM tem todas essas colunas? (o que falta vira consulta a menos)"""
    _descobrir(cur)
    return bool(_colunas) and all(c in _colunas for c in cols)


def _minutos(v: Any, dflt: int) -> int:
    """Limiar em minutos vindo do YAML/painel, sem deixar valor torto derrubar a
    coleta inteira - a fila e so um dos itens do ciclo."""
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return dflt


def _fila_vazia(parada_min: int, residuo_min: int, erro: str | None) -> dict[str, Any]:
    """Payload de fila indisponivel: nenhum numero, so o motivo."""
    return {"pending": None, "running": None, "stuck": None, "waiting": None,
            "orphans": None, "oldest_min": None, "waiting_min": None,
            "done_recent": None, "since_last_min": None,
            "window_min": _minutos(parada_min, 30), "residue_min": _minutos(residuo_min, 360),
            "col": None, "error": erro}


def queue(servidor: str | None = None, parada_min: int = 30,
          residuo_min: int = 360) -> dict[str, Any] | None:
    """Saude da FILA, nao das validacoes: o que entrou e nao esta saindo.

    Job que termina em erro e validacao/regra de negocio da aplicacao - nao e
    problema do servidor. O que caracteriza problema de verdade e a fila parar de
    andar, e "parar de andar" nao e a mesma coisa que "estar quieta": das 17.723
    execucoes de uma semana do parque, a media termina em 4s e NENHUMA esperou
    mais de 5min entre a hora programada e o inicio; fora do horario comercial,
    por outro lado, e normal um executor passar horas sem concluir nada.

    Por isso a consulta separa tres coisas que a versao anterior somava numa
    contagem unica de "pendentes":

    * `running` - em andamento ha menos de `parada_min`: fila normal;
    * `stuck`   - em andamento ha mais de `parada_min` (e menos de `residuo_min`):
                  candidata real a travamento;
    * `orphans` - sem conclusao ha mais de `residuo_min`, ou ja marcada como
                  abortada/cancelada: residuo historico, tipicamente execucao que
                  estava no ar quando o RM.Host reiniciou. Sao 87 delas em 90
                  dias no parque, e cada uma ficava 24h contando como "pendente"
                  - a origem do falso positivo de fila travada.

    E acrescenta as duas medidas que faltavam para distinguir fila travada de
    fila ociosa: `done_recent` (quantas concluiram na janela - se e maior que
    zero, a fila andou, ponto final) e `waiting` (execucoes cuja hora programada
    ja passou e que ninguem comecou, o sintoma direto de pool que parou de puxar
    trabalho; em 90 dias de historico nunca passou de 2 num mesmo dia).
    """
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.queue: %s", exc)
        return _fila_vazia(parada_min, residuo_min, f"{type(exc).__name__}: {exc}"[:150])
    if conn is None:
        return None
    srv_filter, params = "", []
    if servidor:
        limpo = re.sub(r"[^A-Za-z0-9_.-]", "", servidor)
        if limpo:
            srv_filter = " AND SERVIDOR LIKE %s"
            params.append(limpo + ":%")
    w = _minutos(parada_min, 30)
    z = max(w + 1, _minutos(residuo_min, 360))
    try:
        cur = conn.cursor()
        col = _coluna_inicio(cur)
        # "Viva" e a execucao que o RM ainda considera em andamento: sem
        # cancelamento pedido e sem status terminal. Das 87 execucoes sem
        # DATAFIMEXEC dos ultimos 90 dias, 76 estao em status 4 ou 7 (abortada,
        # falha critica) - o RM marca assim quando o servico volta, mas nunca
        # preenche a data de fim. Contar isso como fila era contar defunto.
        # Sem as colunas (outra versao do RM), tudo conta como vivo e o veredito
        # fica so com a idade.
        terminais = ", ".join(str(s) for s in _STATUS_TERMINAIS)
        viva = ("EMCANCELAMENTO IS NULL "
                f"AND (STATUS IS NULL OR STATUS NOT IN ({terminais}))")
        if not _tem(cur, "EMCANCELAMENTO", "STATUS"):
            viva = "1 = 1"

        em_curso = presas = orfas = 0
        idade = None
        if col:
            cur.execute(
                f"""SELECT SUM(CASE WHEN viva = 1 AND idade <= {w} THEN 1 ELSE 0 END),
                           SUM(CASE WHEN viva = 1 AND idade > {w} AND idade <= {z} THEN 1 ELSE 0 END),
                           SUM(CASE WHEN viva = 0 OR idade > {z} THEN 1 ELSE 0 END),
                           MAX(CASE WHEN viva = 1 AND idade <= {z} THEN idade END)
                      FROM (SELECT DATEDIFF(minute, {col}, GETDATE()) idade,
                                   CASE WHEN {viva} THEN 1 ELSE 0 END viva
                              FROM dbo.GJOBXEXECUCAO
                             WHERE DATAFIMEXEC IS NULL
                               AND {col} >= DATEADD(day, -7, GETDATE()){srv_filter}) x""",
                tuple(params))
            row = cur.fetchone()
            if row:
                em_curso, presas, orfas = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
                idade = int(row[3]) if row[3] is not None else None

        # Conclusoes: quantas sairam na janela (a fila andou?) e ha quanto tempo
        # foi a ultima. As duas recortadas em 24h - sem o recorte o MAX varreria
        # anos de GJOBXEXECUCAO a cada ciclo de coleta.
        cur.execute(
            f"""SELECT SUM(CASE WHEN DATAFIMEXEC >= DATEADD(minute, -{w}, GETDATE()) THEN 1 ELSE 0 END),
                       DATEDIFF(minute, MAX(DATAFIMEXEC), GETDATE())
                  FROM dbo.GJOBXEXECUCAO
                 WHERE DATAFIMEXEC >= DATEADD(hour, -24, GETDATE()){srv_filter}""",
            tuple(params))
        row = cur.fetchone()
        concluidas = int(row[0] or 0) if row else 0
        desde = int(row[1]) if row and row[1] is not None else None

        # Programadas que ja venceram e ninguem comecou. Agendamento com hora no
        # futuro nao entra: nao esta esperando, esta marcado. O executor pode nem
        # estar definido (o RM so grava SERVIDOR quando alguem pega o job), entao
        # a espera sem dono conta para todos os executores.
        aguardando, espera = 0, None
        if _tem(cur, "DATAINIEXEC", "DATAPROGRAMADA"):
            dono = " AND (SERVIDOR IS NULL OR SERVIDOR = '' OR SERVIDOR LIKE %s)" if srv_filter else ""
            cur.execute(
                f"""SELECT COUNT(*), MAX(DATEDIFF(minute, DATAPROGRAMADA, GETDATE()))
                      FROM dbo.GJOBXEXECUCAO
                     WHERE DATAINIEXEC IS NULL AND DATAFIMEXEC IS NULL AND {viva}
                       AND DATAPROGRAMADA <= DATEADD(minute, -{w}, GETDATE())
                       AND DATAPROGRAMADA >= DATEADD(minute, -{z}, GETDATE()){dono}""",
                tuple(params) if dono else ())
            row = cur.fetchone()
            if row:
                aguardando = int(row[0] or 0)
                espera = int(row[1]) if row[1] is not None else None
        conn.close()
        return {"pending": em_curso + presas + aguardando, "running": em_curso,
                "stuck": presas, "waiting": aguardando, "orphans": orfas,
                "oldest_min": idade, "waiting_min": espera,
                "done_recent": concluidas, "since_last_min": desde,
                "window_min": w, "residue_min": z, "col": col, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.queue: %s", exc)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return _fila_vazia(parada_min, residuo_min, f"{type(exc).__name__}: {exc}"[:150])


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
