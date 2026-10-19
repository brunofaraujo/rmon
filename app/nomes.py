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
    if _INV is None:
        return chave
    comp = _computadores().get(chave)
    return comp.upper() if comp else chave


def descricao(chave: str | None, curta: bool = False) -> str:
    """Papel e ambiente por extenso: "Servidor de aplicacao - SGE/Financeiro"."""
    cfg = _cfg(chave or "")
    if cfg is None:
        return ""
    papeis = PAPEIS_CURTO if curta else PAPEIS
    partes = [papeis.get(cfg.role, cfg.role) if cfg.role else "",
              (_INV.environments.get(cfg.env, cfg.env) if cfg.env else "")]
    return " · ".join(p for p in partes if p)


def ip(chave: str | None) -> str:
    cfg = _cfg(chave or "")
    return cfg.host if cfg is not None else ""


def com_ip(chave: str | None) -> str:
    """Para mensagens fora da tela (Slack/Telegram): "SRV06N (10.10.10.34)"."""
    cfg = _cfg(chave or "")
    nome = titulo(chave)
    return f"{nome} ({cfg.host})" if cfg is not None else nome


def confere(digitado: str, chave: str) -> bool:
    """Confirmacao digitada: vale o nome mostrado na tela ou a chave interna."""
    d = (digitado or "").strip().lower()
    return bool(d) and d in {chave.lower(), titulo(chave).lower()}
