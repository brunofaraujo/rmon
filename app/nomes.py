"""Nome de cada servidor como a tela o mostra.

O `name` do inventario ("fin-sge-app-34") e a chave interna: URL, banco,
historico de alertas e estado do NLB dependem dele, entao nao muda. Na tela vai
o nome que a equipe reconhece: o `label` do YAML, se houver, senao o nome do
computador (COMPUTERNAME) que o inventario coletou ("SRV06N"). Ao lado, o papel
e o ambiente por extenso ("Servidor de aplicacao - SGE/Financeiro").

Sem inventario configurado (testes) ou antes da primeira coleta, cai na chave.
"""
from __future__ import annotations

import time

from . import db
from .config import PAPEIS, PAPEIS_CURTO

_INV = None
_CACHE: dict = {"ts": 0.0, "comp": {}}
_TTL = 300.0


def configurar(inv) -> None:
    global _INV
    _INV = inv
    _CACHE.update(ts=0.0, comp={})


def _computadores() -> dict[str, str]:
    agora = time.time()
    if agora - _CACHE["ts"] > _TTL:
        _CACHE["ts"] = agora
        try:
            _CACHE["comp"] = db.computadores()
        except Exception:  # banco fora: segue com o que ja tinha
            pass
    return _CACHE["comp"]


def _cfg(chave: str):
    for s in (_INV.servers if _INV else []):
        if s.name == chave:
            return s
    return None


def titulo(chave: str | None) -> str:
    """Nome curto para a tela: label do YAML, senao o nome do computador."""
    if not chave:
        return ""
    cfg = _cfg(chave)
    if cfg is not None and getattr(cfg, "label", ""):
        return cfg.label
    return computador(chave) or chave


def computador(chave: str | None) -> str:
    """COMPUTERNAME coletado pelo inventario ("SRV06N"); vazio antes da coleta."""
    if not chave or _INV is None:
        return ""
    return (_computadores().get(chave) or "").upper()


def descricao(chave: str | None, curta: bool = False) -> str:
    """O que acompanha o titulo: "Servidor de aplicacao - SGE/Financeiro".

    Com `label` no YAML o titulo ja diz sistema e papel ("SGE - Aplicacao 1"):
    o hostname entra na frente, porque e por ele que se acha a maquina no RDP,
    no vCenter e no AD. Na versao curta (mural), so o hostname.
    """
    cfg = _cfg(chave or "")
    if cfg is None:
        return ""
    comp = computador(chave) if getattr(cfg, "label", "") else ""
    if comp and curta:
        return comp
    papeis = PAPEIS_CURTO if curta else PAPEIS
    partes = [comp, papeis.get(cfg.role, cfg.role) if cfg.role else "",
              (_INV.environments.get(cfg.env, cfg.env) if cfg.env else "")]
    return " \u00b7 ".join(p for p in partes if p)


def ip(chave: str | None) -> str:
    cfg = _cfg(chave or "")
    return cfg.host if cfg is not None else ""


def com_ip(chave: str | None) -> str:
    """Para mensagens fora da tela (Slack/Telegram): "SRV06N (10.10.10.34)"."""
    cfg = _cfg(chave or "")
    nome = titulo(chave)
    return f"{nome} ({cfg.host})" if cfg is not None else nome


def _normal(texto: str) -> str:
    # "SGE – Aplicacao 1" digitado com hifen comum, sem acento ou com espaco
    # sobrando ainda confere: ninguem acha o travessao no teclado.
    import unicodedata
    t = unicodedata.normalize("NFKD", (texto or "").replace("\u2013", "-").replace("\u2014", "-"))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.lower().split())


def confere(digitado: str, chave: str) -> bool:
    """Confirmacao digitada: vale o nome da tela, o hostname ou a chave interna."""
    d = _normal(digitado)
    return bool(d) and d in {_normal(x) for x in (chave, titulo(chave), computador(chave)) if x}
