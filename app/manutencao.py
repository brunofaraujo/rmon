"""Janela de manutencao por host: o RMon segue coletando e registrando, mas cala.

Reboot planejado, atualizacao do RM, troca de disco: durante a janela o host
cai, os servicos param, o broker e regerado - e cada uma dessas coisas virava
mensagem no celular de quem justamente estava fazendo a manutencao. Agora:

* os problemas continuam no alerts_log e na tela (o historico nao perde nada);
* nenhuma transicao do host notifica, nem os lembretes de critico aberto;
* a automacao do NLB trata o no como em hold (nao drena nem reingressa);
* no fim da janela sai UMA mensagem: o que seguiu quebrado, ou que voltou limpo.

O estado vive no app_config (chave `manutencao`), entao sobrevive a restart e
vale igual para o scheduler e para o painel. Encerrar antes da hora so adianta
o `ate`: quem fecha a janela (e manda o resumo) e sempre o ciclo de coleta.
"""
from __future__ import annotations

import time
from typing import Any

from . import db

CHAVE = "manutencao"
# Teto da janela: manutencao esquecida ligada por dias e monitor desligado.
MAX_MIN = 24 * 60


def todas() -> dict[str, dict[str, Any]]:
    """Janelas registradas, inclusive as vencidas que o ciclo ainda nao fechou."""
    v = db.get_config(CHAVE, {}) or {}
    return v if isinstance(v, dict) else {}


def ativas(agora: float | None = None, janelas: dict | None = None) -> dict[str, dict[str, Any]]:
    agora = time.time() if agora is None else agora
    janelas = todas() if janelas is None else janelas
    return {nome: j for nome, j in janelas.items() if float(j.get("ate") or 0) > agora}


def vencidas(agora: float | None = None, janelas: dict | None = None) -> dict[str, dict[str, Any]]:
    agora = time.time() if agora is None else agora
    janelas = todas() if janelas is None else janelas
    return {nome: j for nome, j in janelas.items() if float(j.get("ate") or 0) <= agora}


def iniciar(server: str, minutos: int, motivo: str, por: str,
            agora: float | None = None) -> dict[str, Any]:
    agora = time.time() if agora is None else agora
    minutos = max(1, min(MAX_MIN, int(minutos)))
    janelas = todas()
    atual = janelas.get(server) or {}
    j = {
        # estender uma janela aberta mantem o inicio original
        "desde": atual.get("desde") if float(atual.get("ate") or 0) > agora else agora,
        "ate": agora + minutos * 60,
        "motivo": (motivo or "").strip()[:200],
        "por": por,
    }
    janelas[server] = j
    db.set_config(CHAVE, janelas)
    return j


def encerrar(server: str, agora: float | None = None) -> bool:
    """Adianta o fim da janela para agora; o proximo ciclo fecha e resume."""
    agora = time.time() if agora is None else agora
    janelas = todas()
    if server not in janelas:
        return False
    janelas[server]["ate"] = min(float(janelas[server].get("ate") or 0), agora)
    db.set_config(CHAVE, janelas)
    return True


def remover(nomes: list[str], agora: float | None = None) -> None:
    """Tira as janelas ja fechadas pelo ciclo. Rele antes de gravar para nao
    apagar uma janela que alguem abriu (ou estendeu) no painel durante o ciclo."""
    agora = time.time() if agora is None else agora
    janelas = todas()
    for nome in nomes:
        j = janelas.get(nome)
        if j is not None and float(j.get("ate") or 0) <= agora:
            janelas.pop(nome)
    db.set_config(CHAVE, janelas)


def hora(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%d/%m %H:%M", time.localtime(float(ts)))
