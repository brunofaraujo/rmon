"""Niveis de log do RMonitor: sucesso silencioso, problema barulhento.

Sem isto o processo escreve ~30 mil linhas por dia, das quais 29 mil sao
avisos de que tudo deu certo. Espaco nao e o problema (o journal inteiro da
VM cabe em algumas dezenas de MB); legibilidade e: um WARNING real fica
enterrado no meio de milhares de `-> OK` e ninguem le.

O corte inteiro e reversivel por `RMON_LOG_LEVEL=DEBUG`, que devolve tudo o
que se cala aqui - e o que se quer ao depurar uma coleta especifica.
"""
from __future__ import annotations

import logging
import os

from .config import load_dotenv

FORMATO = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Bibliotecas que anunciam cada sucesso em INFO. O APScheduler emite duas
# linhas por disparo ("Running job" + "executed successfully") e o httpx uma
# por requisicao do coletor - juntos, metade do volume diario, sem nada que a
# propria aplicacao ja nao registre: o scheduler anota o resultado da coleta e
# o notify.py anota as falhas de Telegram/Slack.
BIBLIOTECAS_TAGARELAS = ("apscheduler", "httpx", "httpcore")

# Rotas de polling: as TVs batem em /api/tv a cada 15s e o healthz responde ao
# nginx. Registrar cada acerto afoga as navegacoes de pagina, que sao as unicas
# linhas do access log com valor de auditoria.
ROTAS_SILENCIOSAS = ("/api/tv", "/healthz", "/favicon.ico", "/static/")


class FiltroPolling(logging.Filter):
    """Descarta do access log do uvicorn os acertos nas rotas de polling.

    So cala 2xx e 3xx de proposito: um 401 ou um 500 em /api/tv e exatamente o
    que se quer ver, e e raro o bastante para nao pesar.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        args = record.args
        # Formato do uvicorn: ('%s - "%s %s HTTP/%s" %d', cliente, metodo,
        # caminho, versao, status). Qualquer outra forma passa intacta.
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        caminho, status = args[2], args[4]
        if not isinstance(caminho, str) or not isinstance(status, int):
            return True
        if status >= 400:
            return True
        caminho = caminho.split("?", 1)[0]
        return not caminho.startswith(ROTAS_SILENCIOSAS)


def nivel_configurado() -> int:
    """Le RMON_LOG_LEVEL; qualquer coisa que nao seja um nivel vira INFO."""
    nome = os.environ.get("RMON_LOG_LEVEL", "INFO").strip().upper()
    nivel = logging.getLevelName(nome or "INFO")
    return nivel if isinstance(nivel, int) else logging.INFO


def setup_logging() -> int:
    """Monta o log do processo e devolve o nivel aplicado.

    Chamado na importacao de `app.main`, ou seja depois de o uvicorn ter feito
    a propria configuracao - por isso o filtro em `uvicorn.access` sobrevive.
    """
    # O nivel pode vir do .env, que so e lido em load_settings() - tarde demais
    # para o log da importacao. Ler aqui e barato e idempotente (nao sobrescreve
    # o que ja estiver no ambiente).
    load_dotenv(os.environ.get("RMON_DOTENV", ".env"))
    nivel = nivel_configurado()
    logging.basicConfig(level=nivel, format=FORMATO)
    # basicConfig nao mexe no nivel se a raiz ja tiver handler (reload, testes).
    logging.getLogger().setLevel(nivel)

    if nivel <= logging.DEBUG:
        return nivel   # depurando: ninguem quer o log filtrado

    for nome in BIBLIOTECAS_TAGARELAS:
        logging.getLogger(nome).setLevel(logging.WARNING)

    acesso = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, FiltroPolling) for f in acesso.filters):
        acesso.addFilter(FiltroPolling())
    return nivel
