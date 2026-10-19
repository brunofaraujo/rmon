"""Servidor FORA do NLB travado sem contato: reinicio fora do Windows.

Em 19/10 o .222 (SRV11, jobs) travou as 10:31: um JobRunner esgotou a memoria
e o WinRM, o RPC e o agente do Elastic pararam juntos. O .188, no do NLB que
travou do mesmo jeito minutos depois, voltou sozinho - o RMon reiniciou a VM
pelo Incus (app/nlb.py, #52). O .222 ficou parado esperando alguem, porque essa
regra so valia para os nos do NLB.

Aqui vale a mesma regra para os demais servidores:

* sem contato (WinRM) ha `ciclos` coletas seguidas;
* o Elastic confirma que o Windows parou, nao so o WinRM
  (nlb.elastic_confirma: consulta respondida, o host mandava dados quando o
  contato caiu e nada chega ha `elastic_mudo_sec`);
* fora de janela de manutencao; no maximo `max_dia` reinicios em 24h e um a
  cada `intervalo_min`.

O unico canal fora do Windows e o Incus (`incus_vm`, o mesmo comando forcado do
NLB). Servidor fisico (o .222 e um HPE sem iLO acessivel) nao tem como ser
reiniciado pelo RMon: a protecao dele e nao deixar travar (o encerramento
automatico do JobRunner, scheduler.encerrar_jobrunners). Travado mesmo assim,
sai um alerta critico, uma vez por episodio, dizendo que o Windows parou (nao
so o WinRM) e que o reset e no console. Passado o limite de reinicios, idem.
Elastic ainda recebendo dados: so o WinRM travou - aviso, sem reset.
Elastic desligado ou sem leitura: nada aqui (o alerta "sem contato" ja diz tudo).
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Any, Iterable

from . import incus
from .nlb import elastic_confirma

log = logging.getLogger("rmon.reinicio")

DEFAULTS: dict[str, Any] = {
    # auto: reinicia; alert: so diz o que faria; off: nada
    "mode": "auto",
    "ciclos": 3,
    "elastic_mudo_sec": 180,
    "max_dia": 2,
    "intervalo_min": 30,
}
MODOS = ("auto", "alert", "off")
STATE_KEY = "reinicio_state"
_LOCK = threading.Lock()

ICON = {"reset_unreachable": "\U0001F504", "would_reset": "\U0001F7E0",
        "unreachable_crit": "\U0001F534", "unreachable_alive": "⚠️",
        "failed": "❌"}


def settings_for(defaults: dict | None) -> dict[str, Any]:
    cfg = {**DEFAULTS, **(((defaults or {}).get("reinicio")) or {})}
    if cfg.get("mode") not in MODOS:
        cfg["mode"] = "alert"
    cfg["ciclos"] = max(1, int(cfg.get("ciclos") or 1))
    cfg["elastic_mudo_sec"] = max(60, int(cfg.get("elastic_mudo_sec") or 60))
    cfg["max_dia"] = max(0, int(cfg.get("max_dia") or 0))
    cfg["intervalo_min"] = max(5, int(cfg.get("intervalo_min") or 5))
    return cfg


def canal(server, nlb_cfg: dict) -> tuple[str, str]:
    """("incus"|"", descricao) - por onde este servidor pode ser reiniciado."""
    vm = getattr(server, "incus_vm", "") or ""
    if vm and incus.configurado(nlb_cfg):
        return "incus", f"a VM {vm} pelo Incus"
    return "", ""


def _nota(st: dict, valor: str) -> bool:
    """Registra a situacao do episodio; True se mudou (merece mensagem)."""
    if st.get("nota") == valor:
        return False
    st["nota"] = valor
    return True


def avaliar(nome: str, reachable: bool, es: dict | None, st: dict, cfg: dict, now: float,
            via: tuple[str, str], em_manut: bool) -> dict | None:
    """Decide o que fazer com UM servidor neste ciclo. Funcao pura (mexe so em `st`).

    Devolve {"kind", "msg", "action": bool} ou None. `es`: o batimento do host
    no Elastic neste ciclo ({"ok", "ts"}), None com o Elastic desligado.
    """
    es = es or {}
    if es.get("ts"):
        st["es_last"] = max(float(es["ts"]), float(st.get("es_last") or 0))
    st["resets"] = [t for t in (st.get("resets") or []) if now - t < 86400]
    if reachable:
        st["unreach"] = 0
        st.pop("mudo_desde", None)
        st.pop("nota", None)
        return None
    st["unreach"] = int(st.get("unreach") or 0) + 1
    if st["unreach"] == 1:
        st["mudo_desde"] = now
    if st["unreach"] < cfg["ciclos"] or em_manut or cfg["mode"] == "off":
        return None
    resumo = f"sem contato (WinRM) há {st['unreach']} coletas seguidas"
    estado, es_txt = elastic_confirma({"es": es}, st,
                                      {"reset_elastic_mudo_sec": cfg["elastic_mudo_sec"]}, now)
    if estado == "vivo":
        if _nota(st, "vivo"):
            return {"kind": "unreachable_alive", "action": False,
                    "msg": f"{resumo}, mas o Elastic ainda recebe dados dele ({es_txt}): o Windows "
                           "está de pé e só o WinRM travou. Não reiniciado"}
        return None
    if estado != "mudo":
        # sem a confirmacao do Elastic nao ha o que acrescentar ao "sem contato"
        # que o alerta DOWN ja deu: nada de reset nem de segunda mensagem
        return None
    tipo, desc = via
    impede = None
    if not tipo:
        impede = ("sem_canal", f"o Elastic também está sem dados ({es_txt}): o Windows travou. "
                               "O RMon não tem como reiniciá-lo (servidor físico)")
    elif len(st["resets"]) >= cfg["max_dia"]:
        impede = ("limite", f"Elastic sem dados ({es_txt}), mas o RMon já o reiniciou "
                            f"{len(st['resets'])}x em 24h: só aviso")
    if impede:
        if _nota(st, "crit:" + impede[0]):
            return {"kind": "unreachable_crit", "action": False,
                    "msg": f"{resumo}: {impede[1]}. Reinicie pelo console"}
        return None
    if st["resets"] and now - st["resets"][-1] < cfg["intervalo_min"] * 60:
        return None          # reiniciado ha pouco: o boot ainda esta em curso
    msg = f"{resumo}; Elastic sem dados ({es_txt}): o Windows travou. Reinicia {desc}"
    if cfg["mode"] == "auto":
        if st.get("falhou") and now - st["falhou"] < 5 * 60:
            return None
        return {"kind": "reset_unreachable", "action": True, "msg": msg}
    if _nota(st, "would"):
        return {"kind": "would_reset", "action": False,
                "msg": "[modo alerta] " + msg.replace("Reinicia", "Reiniciaria")}
    return None


def _executar(server, tipo: str, nlb_cfg: dict) -> tuple[bool, str]:
    if tipo == "incus":
        return incus.reset(nlb_cfg, server.incus_vm)
    return False, "sem canal"


def _registrar(server, kind: str, msg: str, avisar: bool = True) -> None:
    from . import db, nomes, notify
    db.record_alert(server.name, "reinicio", kind, msg)
    db.audit("rmon-reinicio", f"reinicio_{kind}", f"{server.name}: {msg}"[:900])
    if avisar and notify.enabled():
        notify.send(f"{ICON.get(kind, '')} RMonitor — {nomes.com_ip(server.name)}: {msg}")


def servidores(inv) -> list:
    """Quem entra aqui: todo servidor fora do NLB (os do NLB tem a regra deles)."""
    return [s for s in inv.servers if not getattr(s, "nlb", False)]


def run_cycle(inv, coletas: dict[str, dict], es: dict[str, dict] | None,
              hold: Iterable[str] = (), now: float | None = None) -> list[dict]:
    """Chamado ao fim de cada poll_all, com as coletas que acabaram de chegar."""
    from . import db
    from .nlb import settings_for as nlb_settings
    cfg = settings_for(inv.defaults)
    alvo = servidores(inv)
    if cfg["mode"] == "off" or not alvo:
        return []
    now = time.time() if now is None else now
    nlb_cfg = nlb_settings(inv.defaults)
    hold = set(hold)
    feitas = []
    with _LOCK:
        state = copy.deepcopy(db.get_config(STATE_KEY, {}) or {})
        nos = state.setdefault("servidores", {})
        for s in alvo:
            r = coletas.get(s.name)
            if r is None:
                continue
            st = nos.setdefault(s.name, {})
            via = canal(s, nlb_cfg)
            d = avaliar(s.name, bool(r.get("reachable")), (es or {}).get(s.name), st, cfg, now,
                        via, s.name in hold)
            if not d:
                continue
            if d["action"]:
                ok, detalhe = _executar(s, via[0], nlb_cfg)
                log.warning("reinicio %s pelo %s -> %s (%s)", s.name, via[0], ok, detalhe[:200])
                if ok:
                    st["resets"] = (st.get("resets") or []) + [now]
                    st.pop("falhou", None)
                    _nota(st, "reset")
                    d = {**d, "msg": d["msg"].replace("Reinicia ", "Reiniciado: ") + f" ({detalhe[:120]})"}
                else:
                    nova = "falhou" not in st
                    st["falhou"] = now
                    d = {**d, "kind": "failed",
                         "msg": f"reinício fora do Windows FALHOU ({via[1]}): {detalhe[:200]}"}
                    if not nova:
                        _registrar(s, d["kind"], d["msg"], avisar=False)
                        feitas.append({**d, "server": s.name})
                        continue
            _registrar(s, d["kind"], d["msg"])
            feitas.append({**d, "server": s.name})
        db.set_config(STATE_KEY, state)
    return feitas
