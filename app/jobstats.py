"""Estatisticas de jobs do TOTVS RM lidas do SQL Server (GJOBXEXECUCAO).

Conta sucesso vs falha nas ultimas N horas/minutos pela DATAFIMEXEC. Config SQL vem
do .env (RMON_SQL_*). Inerte (retorna None) se o SQL nao estiver configurado.
"""
from __future__ import annotations

import logging
import os
import re
import struct
import time
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
                    "fila fica indisponível", ", ".join(_COLS_INICIO))


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
            "col": None, "items": [], "error": erro}


# Quantas execucoes presas vao para a tela por leitura. A lista fica gravada com
# cada coleta (checks.jobs), entao tem teto: no pior dia do historico foram 8 de
# uma vez no mesmo executor.
ITENS_MAX = 10


def _viva(cur, prefixo: str = "") -> str:
    """Execucao que o RM ainda considera em andamento: sem cancelamento pedido e
    sem status terminal. Sem as colunas (outra versao do RM), tudo conta como
    vivo e o veredito fica so com a idade.

    Fora isso, a execucao que o RM mandou cancelar antes de comecar e nunca
    fechou: STATUS 3 (esperando), sem inicio e com o texto da rotina de
    cancelamento. Nao vai rodar nunca - o job 2450548 (20/10) ficou 6h na lista
    de presos assim, e foram 5 iguais em 90 dias. Os COALESCE seguram o NOT:
    sem eles, STATUS ou TEXTOSTATUS nulo daria UNKNOWN e tiraria da fila
    execucao viva de verdade.
    """
    if not _tem(cur, "EMCANCELAMENTO", "STATUS"):
        return "1 = 1"
    terminais = ", ".join(str(s) for s in _STATUS_TERMINAIS)
    viva = (f"{prefixo}EMCANCELAMENTO IS NULL "
            f"AND ({prefixo}STATUS IS NULL OR {prefixo}STATUS NOT IN ({terminais}))")
    if _tem(cur, "DATAINIEXEC", "TEXTOSTATUS"):
        # %% vira % quando a consulta leva parametro; sem parametro fica %%, que
        # no LIKE e o mesmo curinga - vale dos dois jeitos
        viva += (f" AND NOT (COALESCE({prefixo}STATUS, -1) = 3 AND {prefixo}DATAINIEXEC IS NULL "
                 f"AND COALESCE({prefixo}TEXTOSTATUS, '') LIKE '%%rotina de cancelamento%%')")
    return viva


def _host(servidor: Any) -> str:
    """"srv11:8052" -> "srv11": o RM grava o executor como host:porta."""
    return str(servidor or "").split(":", 1)[0].strip()


def _itens(cur, col: str | None, viva: str, w: int, z: int, servidor: str | None) -> list[dict[str, Any]]:
    """QUAIS execucoes estao presas, para a tela dizer o que parar e como.

    So roda quando a contagem ja achou alguma - o ciclo normal nao ganha
    consulta. Mesmos dois criterios da contagem: em andamento ha mais de `w`
    minutos (e menos de `z`), ou com a hora programada vencida ha mais de `w`
    sem ninguem comecar. Coluna que esta versao do RM nao tem vira NULL.
    """
    def c(nome: str) -> str:
        return f"E.{nome}" if _tem(cur, nome) else "NULL"

    params: list[Any] = []
    srv = ""
    limpo = re.sub(r"[^A-Za-z0-9_.-]", "", servidor or "")
    conds = []
    if col:
        conds.append(f"(E.{col} IS NOT NULL AND E.{col} >= DATEADD(day, -7, GETDATE()) "
                     f"AND DATEDIFF(minute, E.{col}, GETDATE()) > {w} "
                     f"AND DATEDIFF(minute, E.{col}, GETDATE()) <= {z}"
                     + (" AND E.SERVIDOR LIKE %s)" if limpo else ")"))
        if limpo:
            params.append(limpo + ":%")
    if _tem(cur, "DATAINIEXEC", "DATAPROGRAMADA"):
        conds.append(f"(E.DATAINIEXEC IS NULL "
                     f"AND E.DATAPROGRAMADA <= DATEADD(minute, -{w}, GETDATE()) "
                     f"AND E.DATAPROGRAMADA >= DATEADD(minute, -{z}, GETDATE())"
                     + (" AND (E.SERVIDOR IS NULL OR E.SERVIDOR = '' OR E.SERVIDOR LIKE %s))"
                        if limpo else ")"))
        if limpo:
            params.append(limpo + ":%")
    if not conds:
        return []
    ini = f"E.{col}" if col else "NULL"
    cur.execute(
        f"""SELECT TOP {ITENS_MAX} E.IDJOB, {c('IDEXECUCAO')}, {c('SERVIDOR')}, {c('PROCESSID')},
                   CONVERT(varchar(19), {ini}, 120), DATEDIFF(minute, {ini}, GETDATE()),
                   CONVERT(varchar(16), {c('DATAPROGRAMADA')}, 120),
                   DATEDIFF(minute, {c('DATAPROGRAMADA')}, GETDATE()),
                   {c('PERCPROGRESSO')}, {c('STATUS')}, LEFT({c('TEXTOSTATUS')}, 160),
                   J.CODUSUARIO, J.NOME, J.CLASSEPROCESSO, {c('RECCREATEDBY')}
              FROM dbo.GJOBXEXECUCAO E LEFT JOIN dbo.GJOBX J ON J.IDJOB = E.IDJOB
             WHERE E.DATAFIMEXEC IS NULL AND {viva} AND ({' OR '.join(conds)})
             ORDER BY COALESCE({ini}, {c('DATAPROGRAMADA')})""",
        tuple(params))
    saida = []
    for r in cur.fetchall():
        rodando = r[4] is not None
        pid = str(r[3]).strip() if r[3] is not None else ""
        saida.append({
            "idjob": r[0], "idexec": r[1], "servidor": r[2], "host": _host(r[2]),
            "pid": int(pid) if pid.isdigit() else None,
            "inicio": r[4], "idade_min": r[5] if rodando else None,
            "programada": r[6], "espera_min": None if rodando else r[7],
            "progresso": r[8], "status": r[9], "texto": (r[10] or "").strip() or None,
            "usuario": (r[11] or r[14] or "").strip() or None,
            "nome": (r[12] or r[13] or "").strip() or None, "classe": r[13],
            "tipo": "presa" if rodando else "aguardando",
        })
    return saida


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
        # "Viva" e a execucao que o RM ainda considera em andamento (ver _viva).
        # Das 87 execucoes sem DATAFIMEXEC dos ultimos 90 dias, 76 estao em
        # status 4 ou 7 (abortada, falha critica) - o RM marca assim quando o
        # servico volta, mas nunca preenche a data de fim. Contar isso como fila
        # era contar defunto.
        viva, viva_e = _viva(cur), _viva(cur, "E.")

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

        # A contagem diz que ha algo preso; a lista diz o que e. Falhar aqui nao
        # derruba os numeros acima, que sao o que decide o alerta.
        itens: list[dict[str, Any]] = []
        if presas or aguardando:
            try:
                itens = _itens(cur, col, viva_e, w, z, servidor)
            except Exception as exc:  # noqa: BLE001
                log.warning("jobstats.queue (itens): %s", exc)
        conn.close()
        return {"pending": em_curso + presas + aguardando, "running": em_curso,
                "stuck": presas, "waiting": aguardando, "orphans": orfas,
                "oldest_min": idade, "waiting_min": espera,
                "done_recent": concluidas, "since_last_min": desde,
                "window_min": w, "residue_min": z, "col": col, "items": itens, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.queue: %s", exc)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return _fila_vazia(parada_min, residuo_min, f"{type(exc).__name__}: {exc}"[:150])


def execucao_aberta(idjob: Any, idexec: Any = None) -> dict[str, Any] | None:
    """A execucao ainda esta aberta, e em que processo? Lida na hora, no banco.

    O RM.Host.JobRunner e reaproveitado: o mesmo PID roda um job atras do
    outro (o 5420 do SRV11 estava no ar desde a vespera quando pegou o
    relatorio preso de 20/10). Encerrar pelo PID que a coleta anotou minutos
    atras pode matar o job SEGUINTE - por isso, antes de encerrar pelo job, a
    rota pergunta de novo se aquela execucao segue sem fim e em qual PID.

    Devolve {"pid", "servidor", "host", "inicio", "aberta", "nome", "usuario"};
    None se o SQL nao estiver configurado ou a consulta falhar (quem chama
    recusa a acao).
    """
    try:
        idjob = int(idjob)
        idexec = int(idexec) if idexec not in (None, "") else None
    except (TypeError, ValueError):
        return None
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.execucao_aberta: %s", exc)
        return None
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        col = _coluna_inicio(cur) or "RECCREATEDON"
        if not _tem(cur, "PROCESSID", "SERVIDOR"):
            conn.close()
            return None
        filtro, params = "E.IDJOB = %s", [idjob]
        if idexec is not None and _tem(cur, "IDEXECUCAO"):
            filtro += " AND E.IDEXECUCAO = %s"
            params.append(idexec)
        cur.execute(
            f"""SELECT TOP 1 E.PROCESSID, E.SERVIDOR, CONVERT(varchar(19), E.{col}, 120),
                       CASE WHEN E.DATAFIMEXEC IS NULL THEN 1 ELSE 0 END, J.NOME, J.CODUSUARIO
                  FROM dbo.GJOBXEXECUCAO E LEFT JOIN dbo.GJOBX J ON J.IDJOB = E.IDJOB
                 WHERE {filtro}
                 ORDER BY E.{col} DESC""", tuple(params))
        r = cur.fetchone()
        conn.close()
        if not r:
            return {"pid": None, "servidor": None, "host": "", "inicio": None, "aberta": False,
                    "nome": None, "usuario": None}
        pid = str(r[0] or "").strip()
        return {"pid": int(pid) if pid.isdigit() else None, "servidor": r[1],
                "host": _host(r[1]), "inicio": r[2], "aberta": bool(r[3]),
                "nome": (r[4] or "").strip() or None, "usuario": (r[5] or "").strip() or None}
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.execucao_aberta: %s", exc)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return None


_FILTRO_RE = re.compile(r"\(\((.{1,400}?)\)\)", re.S)


def executions_by_pid(computador: str | None, pids) -> dict[str, dict[str, Any]] | None:
    """Que job esta rodando em cada JobRunner deste host: {pid: execucao}.

    A GJOBXEXECUCAO guarda o PID do processo que executa o job (PROCESSID) e o
    executor (SERVIDOR = "host:porta"), entao o processo que o coletor ve
    crescendo no Windows aponta direto para o job, o usuario que o pediu e o
    filtro usado. Foi assim que os estouros de 06/10 e 13/10 foram explicados:
    o mesmo relatorio gerado so com `STURMADISC.CODCOLIGADA = '3'`, sem turma.

    O filtro sai do VALORPARAMETROS (objeto .NET serializado) recortado no
    proprio SQL: o trecho fica alem dos primeiros 4 KB, que e o que o TEXTSIZE
    padrao devolveria. PID e reaproveitado pelo Windows, por isso a busca fica
    nas execucoes sem fim dos ultimos 3 dias e a mais recente de cada PID vence.
    """
    alvo = sorted({str(int(p)) for p in (pids or []) if str(p).strip().isdigit()})
    if not alvo:
        return {}
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.executions_by_pid: %s", exc)
        return None
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        col = _coluna_inicio(cur) or "RECCREATEDON"
        if not _tem(cur, "PROCESSID", "SERVIDOR"):
            conn.close()
            return None
        params: list[Any] = list(alvo)
        srv_filter = ""
        limpo = re.sub(r"[^A-Za-z0-9_.-]", "", computador or "")
        if limpo:
            srv_filter = " AND E.SERVIDOR LIKE %s"
            params.append(limpo + ":%")
        marcas = ", ".join(["%s"] * len(alvo))
        texto = "CAST(CAST(J.VALORPARAMETROS AS varbinary(max)) AS varchar(max))"
        cur.execute(
            f"""SELECT E.PROCESSID, E.IDJOB, E.SERVIDOR,
                       CONVERT(varchar(16), E.{col}, 120), E.PERCPROGRESSO, E.STATUS,
                       J.CODUSUARIO, J.NOME, J.CLASSEPROCESSO,
                       CASE WHEN CHARINDEX('((', {texto}) > 0
                            THEN SUBSTRING({texto}, CHARINDEX('((', {texto}), 420) END
                  FROM dbo.GJOBXEXECUCAO E JOIN dbo.GJOBX J ON J.IDJOB = E.IDJOB
                 WHERE E.PROCESSID IN ({marcas}) AND E.DATAFIMEXEC IS NULL
                   AND E.{col} >= DATEADD(day, -3, GETDATE()){srv_filter}
                 ORDER BY E.{col} DESC""",
            tuple(params))
        saida: dict[str, dict[str, Any]] = {}
        for r in cur.fetchall():
            pid = str(r[0]).strip()
            if pid in saida:
                continue
            filtro = None
            if r[9]:
                m = _FILTRO_RE.search(str(r[9]))
                filtro = m.group(1).strip() if m else None
            saida[pid] = {"idjob": r[1], "servidor": r[2], "inicio": r[3],
                          "progresso": r[4], "status": r[5], "usuario": r[6],
                          "nome": (r[7] or "").strip(), "classe": r[8], "filtro": filtro}
        conn.close()
        return saida
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.executions_by_pid: %s", exc)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return None


# ---------------------------------------------------------------- encerrados

_STATUS_TXT = {2: "concluído", 4: "abortado", 5: "terminou com erro", 6: "concluído com ressalva",
               7: "falha crítica"}
# o int32 que vem logo depois deste, no VALORPARAMETROS de um "Gerar Relatório",
# e o RRPTREPORT.ID (BinaryFormatter do .NET: marcador 08 08 + int32 LE)
_MARCA_RELATORIO = 11529
# {idjob: (validade, leitura)}; validade 0 = nao muda mais (encerrado ha mais de um dia)
_cache_enc: dict[int, tuple[float, dict[str, Any]]] = {}


def parametros_do_job(blob: Any) -> dict[str, Any]:
    """Do VALORPARAMETROS: {"relatorio": RRPTREPORT.ID ou None, "sem_filtro", "filtro"}.

    Sem `((` no blob o pedido nao teve filtro - foi assim que os relatorios que
    derrubaram o parque em 19 e 20/10 foram achados. O filtro vem cortado se
    passar de 300 caracteres (e longo quando sao muitas turmas).
    """
    if blob is None:
        return {"relatorio": None, "sem_filtro": None, "filtro": None}
    if isinstance(blob, str):
        blob = blob.encode("utf-8", "replace")
    blob = bytes(blob)
    ints, i = [], 0
    while True:
        i = blob.find(b"\x08\x08", i)
        if i < 0 or i + 6 > len(blob):
            break
        ints.append(struct.unpack_from("<i", blob, i + 2)[0])
        i += 2
    rid = next((b for a, b in zip(ints, ints[1:]) if a == _MARCA_RELATORIO), None)
    texto = blob.decode("utf-8", "replace")
    ini = texto.find("((")
    if ini < 0:
        return {"relatorio": rid, "sem_filtro": True, "filtro": None}
    fim = texto.find("))", ini)
    trecho = texto[ini + 2:fim] if 0 < fim - ini <= 302 else texto[ini + 2:ini + 300] + "…"
    trecho = " ".join("".join(ch if ch.isprintable() else " " for ch in trecho).split())
    return {"relatorio": rid, "sem_filtro": False, "filtro": trecho}


def _status(v: Any) -> tuple[int | None, str]:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None, "sem conclusão"
    return n, _STATUS_TXT.get(n, "em andamento")


def detalhes_encerrados(eventos: list[dict[str, Any]]) -> dict[int, dict[str, Any]] | None:
    """O que o banco do RM sabe de cada job cujo processo o RMon encerrou.

    `eventos`: [{"idjob", "seg"}], `seg` = segundos desde o encerramento no
    relogio do RMon. As contas de tempo sao feitas no relogio do proprio SQL
    (GETDATE() - seg): o dominio anda ~40 s a frente da VM, e em 23/10 o job
    "comecou" 38 s DEPOIS de o RMon o encerrar. Assim, `dur_s` (de quando o
    job comecou ate o encerramento) nao depende de relogio acertado.

    Devolve {idjob: {...}}: execucao (executor, inicio, status, mensagem,
    progresso), quem pediu (codigo e nome), o relatorio e o filtro, e os pedidos
    do MESMO relatorio pela mesma pessoa nas 24 h seguintes (`depois`). None se
    o SQL nao estiver configurado ou a consulta falhar.
    """
    agora = time.time()
    pedidos: dict[int, float] = {}
    for e in eventos:
        try:
            pedidos[int(e["idjob"])] = max(0.0, float(e.get("seg") or 0))
        except (KeyError, TypeError, ValueError):
            continue
    saida: dict[int, dict[str, Any]] = {}
    faltam: dict[int, float] = {}
    for idjob, seg in pedidos.items():
        c = _cache_enc.get(idjob)
        if c and (c[0] == 0 or c[0] > agora):
            saida[idjob] = c[1]
        else:
            faltam[idjob] = seg
    if not faltam:
        return saida
    try:
        conn = _connect(15)
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.detalhes_encerrados: %s", exc)
        return None
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        col = _coluna_inicio(cur) or "RECCREATEDON"

        def c(nome: str) -> str:
            return f"E.{nome}" if _tem(cur, nome) else "NULL"

        # o VALORPARAMETROS passa de 16 KB: sem isso volta cortado em 4 KB
        cur.execute("SET TEXTSIZE 2147483647")
        marcas = ", ".join(["%s"] * len(faltam))
        cur.execute(
            f"""SELECT J.IDJOB, {c('IDEXECUCAO')}, {c('SERVIDOR')}, {c('PROCESSID')},
                       CONVERT(varchar(19), E.{col}, 120), DATEDIFF(second, E.{col}, GETDATE()),
                       {c('STATUS')}, {c('MENSAGEMSTATUS')}, {c('PERCPROGRESSO')},
                       J.NOME, J.CLASSEPROCESSO, J.CODUSUARIO, U.NOME, J.VALORPARAMETROS
                  FROM dbo.GJOBX J LEFT JOIN dbo.GJOBXEXECUCAO E ON E.IDJOB = J.IDJOB
                  LEFT JOIN dbo.GUSUARIO U ON U.CODUSUARIO = J.CODUSUARIO
                 WHERE J.IDJOB IN ({marcas})
                 ORDER BY J.IDJOB, E.{col} DESC""", tuple(faltam))
        novos: dict[int, dict[str, Any]] = {}
        for r in cur.fetchall():
            idjob = int(r[0])
            if idjob in novos:
                continue
            status, status_txt = _status(r[6])
            pid = str(r[3] or "").strip()
            par = parametros_do_job(r[13])
            novos[idjob] = {
                "idexec": r[1], "executor": r[2], "pid": int(pid) if pid.isdigit() else None,
                "inicio": r[4],
                "dur_s": max(0, int(r[5]) - int(faltam[idjob])) if r[5] is not None else None,
                "status": status, "status_txt": status_txt,
                "mensagem": (r[7] or "").strip() or None, "progresso": r[8],
                "nome": (r[9] or "").strip() or None, "classe": r[10],
                "usuario": (r[11] or "").strip() or None, "usuario_nome": (r[12] or "").strip() or None,
                "relatorio_id": par["relatorio"], "sem_filtro": par["sem_filtro"], "filtro": par["filtro"],
                "relatorio": None, "depois": None,
            }
        rids = sorted({d["relatorio_id"] for d in novos.values() if d["relatorio_id"]})
        if rids:
            try:
                cur.execute(f"""SELECT ID, CODIGO, DESCRICAO, FILTERREQUIRED FROM dbo.RRPTREPORT
                                 WHERE ID IN ({", ".join(["%s"] * len(rids))})""", tuple(rids))
                rel = {int(r[0]): {"id": int(r[0]), "codigo": (r[1] or "").strip(),
                                   "descricao": (r[2] or "").strip(), "filtro_obrigatorio": bool(r[3])}
                       for r in cur.fetchall()}
            except Exception as exc:  # noqa: BLE001 - sem o nome do relatorio, segue
                log.warning("jobstats.detalhes_encerrados (relatório): %s", exc)
                rel = {}
            for d in novos.values():
                d["relatorio"] = rel.get(d["relatorio_id"])
        for idjob, d in novos.items():
            try:
                d["depois"] = _pedidos_depois(cur, col, idjob, d, faltam[idjob])
            except Exception as exc:  # noqa: BLE001 - o resto da leitura vale sem isso
                log.warning("jobstats.detalhes_encerrados (pedidos depois): %s", exc)
        conn.close()
        for idjob, d in novos.items():
            # depois de 24 h nao aparece pedido novo e o status ja e o final
            fixo = faltam[idjob] > 86400 and d["status"] is not None
            _cache_enc[idjob] = (0 if fixo else agora + 120, d)
            saida[idjob] = d
        return saida
    except Exception as exc:  # noqa: BLE001
        log.warning("jobstats.detalhes_encerrados: %s", exc)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return None


def _pedidos_depois(cur, col: str, idjob: int, d: dict[str, Any], seg: float) -> list[dict[str, Any]] | None:
    """A mesma pessoa pediu o mesmo job de novo nas 24 h seguintes? E deu certo?

    "Gerar Relatório" e o nome de TODO relatorio: sem o RRPTREPORT.ID nao da
    para dizer que e o mesmo, entao fica sem resposta (None). O IDJOB e
    sequencial (~3.500 por dia em 10/2026): a busca fica nos 20 mil seguintes,
    pela chave, sem varrer o GJOBX inteiro.
    """
    if not d.get("usuario") or not d.get("classe"):
        return None
    relatorio = d["classe"] == "RptProcGenerateReport"
    if relatorio and not d.get("relatorio_id"):
        return None
    cur.execute(
        f"""SELECT TOP 20 J.IDJOB, E.STATUS, CONVERT(varchar(19), E.{col}, 120),
                   DATEDIFF(second, E.{col}, E.DATAFIMEXEC), J.NOME, J.VALORPARAMETROS
              FROM dbo.GJOBX J LEFT JOIN dbo.GJOBXEXECUCAO E ON E.IDJOB = J.IDJOB
             WHERE J.IDJOB > %s AND J.IDJOB <= %s AND J.CODUSUARIO = %s AND J.CLASSEPROCESSO = %s
               AND J.RECCREATEDON >= DATEADD(second, -%s, GETDATE())
               AND J.RECCREATEDON < DATEADD(second, 86400 - %s, GETDATE())
             ORDER BY J.IDJOB""",
        (idjob, idjob + 20000, d["usuario"], d["classe"], int(seg), int(seg)))
    vistos, saida = set(), []
    for r in cur.fetchall():
        if int(r[0]) in vistos:
            continue
        vistos.add(int(r[0]))
        par = parametros_do_job(r[5]) if relatorio else {}
        if relatorio and par.get("relatorio") != d["relatorio_id"]:
            continue
        if not relatorio and (r[4] or "").strip() != (d.get("nome") or ""):
            continue
        status, status_txt = _status(r[1])
        saida.append({"idjob": int(r[0]), "status": status, "status_txt": status_txt, "inicio": r[2],
                      "dur_s": r[3], "sem_filtro": par.get("sem_filtro"), "filtro": par.get("filtro")})
    return saida


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
