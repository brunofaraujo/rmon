"""Teto de memoria do RM.Host.JobRunner no Windows: como a tela mostra.

O componente roda no proprio servidor (servico RMonLimiteJobRunner, em
deploy/windows) e poe cada JobRunner num Job Object com teto de memoria. Ele
grava o estado num arquivo que a coleta WinRM le (bloco `limite_jr` do
collector._PS_TEMPLATE). Aqui so se traduz esse estado para a tela: o RMon nao
liga, desliga nem configura nada no servidor.
"""
from __future__ import annotations

from typing import Any

SERVICO = "RMonLimiteJobRunner"
# o servico regrava o estado a cada 30 s: tres vezes isso sem mudar e sinal perdido
SEM_SINAL_S = 180

_STATUS = {"Stopped": "parado", "StartPending": "iniciando", "StopPending": "parando",
           "Paused": "pausado"}


def _gb(mb: Any) -> str:
    try:
        v = float(mb) / 1024
    except (TypeError, ValueError):
        return "?"
    return f"{v:.1f}".replace(".", ",").removesuffix(",0")


def _lista(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _n(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _plural(n: int, um: str, varios: str) -> str:
    return f"{n} {um if n == 1 else varios}"


def _estouro(e: dict) -> dict:
    texto = f"PID {e.get('pid')}: {_gb(e.get('mb'))} GB em {e.get('quando') or '?'}"
    if e.get("pico_mb") not in (None, -1):
        texto += f", pico de {_gb(e.get('pico_mb'))} GB"
    if e.get("modo") == "aplicar":
        texto += " (memória negada)"
    else:
        texto += " (só registrado)"
    return {**e, "texto": texto}


def resumo(lj: dict | None) -> dict[str, Any]:
    """{'estado', 'cor', 'texto', 'detalhes': [...], 'estouros': [...]} para a tela do servidor.

    estado: ausente | parado | sem_sinal | registrando | aplicando. A cor segue as
    pilulas do painel: verde = protegendo, azul = calibrando, ambar = precisa de
    atencao (o servidor nao caiu por isso, mas esta sem a protecao).
    """
    if not isinstance(lj, dict):
        return {"estado": "ausente", "cor": "", "texto": "Não instalado neste servidor",
                "detalhes": [], "estouros": []}
    estouros = [_estouro(e) for e in _lista(lj.get("estouros")) if isinstance(e, dict)]
    base = {"detalhes": [], "estouros": list(reversed(estouros))}
    svc = str(lj.get("servico") or "")
    if svc != "Running":
        return {**base, "estado": "parado", "cor": "amber",
                "texto": f"Instalado, mas o serviço está {_STATUS.get(svc, svc or 'desconhecido')}: "
                         "os processos de job estão sem teto"}
    if lj.get("erro"):
        return {**base, "estado": "sem_sinal", "cor": "amber",
                "texto": f"Serviço no ar, mas o estado não pôde ser lido: {lj['erro']}"}
    if lj.get("ativo") is False:
        return {**base, "estado": "sem_sinal", "cor": "amber",
                "texto": "Serviço no ar, mas o estado gravado é de quando ele estava parado"}
    idade = lj.get("idade_s")
    if idade is not None and _n(idade) > SEM_SINAL_S:
        return {**base, "estado": "sem_sinal", "cor": "amber",
                "texto": f"Serviço no ar, mas sem atualizar o estado há {_n(idade) // 60} min"}

    teto = _gb(lj.get("teto_mb"))
    if lj.get("modo") == "aplicar":
        r = {**base, "estado": "aplicando", "cor": "green",
             "texto": f"Ligado: o Windows nega memória acima de {teto} GB por processo de job"}
    else:
        r = {**base, "estado": "registrando", "cor": "blue",
             "texto": f"Só registra (calibração): teto de {teto} GB, nada é bloqueado"}
    processos, sob, falhas = _n(lj.get("processos")), _n(lj.get("sob_teto")), _n(lj.get("falhas"))
    r["detalhes"].append(f"{sob} de {processos} processos de job sob o teto")
    if falhas:
        r["cor"] = "amber"
        r["detalhes"].append(f"{_plural(falhas, 'não pôde ser posto', 'não puderam ser postos')} sob o teto "
                             "(evento 1090 no log Application do servidor)")
    if _n(lj.get("ignorados")):
        r["detalhes"].append(f"{_n(lj['ignorados'])} com outro processo pai, fora do teto")
    if lj.get("maior_mb") is not None:
        r["detalhes"].append(f"maior agora: {_gb(lj['maior_mb'])} GB (PID {lj.get('maior_pid')}) de {teto} GB")
    n24 = _n(lj.get("estouros_24h"))
    if n24:
        if r["estado"] == "aplicando":
            r["detalhes"].append(f"{_plural(n24, 'processo de job bateu', 'processos de job bateram')} no teto "
                                 "nas últimas 24 horas: o job falhou por falta de memória e o servidor seguiu de pé")
        else:
            r["detalhes"].append(f"{_plural(n24, 'processo de job teria passado', 'processos de job teriam passado')} "
                                 "do teto nas últimas 24 horas")
    if lj.get("ultimo_erro"):
        r["detalhes"].append(f"último erro do serviço: {lj['ultimo_erro']}")
    return r
