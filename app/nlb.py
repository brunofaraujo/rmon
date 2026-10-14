"""Retirada e reingresso automaticos de nos do NLB (Network Load Balancing).

O NLB do Windows so enxerga a placa de rede: um terminal server que autentica o
RDP mas nao consegue criar a sessao (commit esgotado, RM.Host caido...) continua
"convergido" e segue recebendo usuarios, que tomam o erro do RemoteApp. Este
modulo olha a saude de APLICACAO de cada no, a partir da coleta normal do RMon,
e drena (`nlb.exe drainstop`) o no doente / reingressa (`nlb.exe start`) o no
recuperado.

Separacao de responsabilidades, para que a decisao seja testavel sem rede:

- `view()` resume uma coleta no que interessa ao NLB;
- `evaluate()` e PURA: recebe as vistas, o estado anterior e a configuracao e
  devolve as decisoes e o estado novo;
- `run_cycle()` e o adaptador: le/grava o estado no banco, executa a acao no host
  (so no modo `auto`), audita e notifica.

Ver docs/NLB.md.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Any, Iterable

log = logging.getLogger("rmon.nlb")

# StatusCode da classe WMI MicrosoftNLB_Node (wlbsctrl.h).
STATUS = {
    1005: "parado",
    1006: "convergindo",
    1007: "convergido",
    1008: "convergido (padrao)",
    1009: "drenando",
    1013: "suspenso",
}
# Recebendo (ou prestes a receber) trafego novo.
ATIVOS = frozenset({1006, 1007, 1008})
# Fora do balanceamento: nao recebe conexao nova.
FORA = frozenset({1005, 1009, 1013})

MODOS = ("off", "alert", "auto")

DEFAULTS: dict[str, Any] = {
    # off = nem avalia; alert = avalia, avisa e audita o que FARIA (padrao);
    # auto = drena e reingressa de verdade.
    "mode": "alert",
    # janela dos eventos de RDP contados no proprio host (1149 x 21/25)
    "window_min": 15,
    # Sinal 1 - autentica mas nao cria sessao: pelo menos `auth_min` logons RDP
    # autenticados (1149) e sessoes criadas/reconectadas (21/25) abaixo de
    # `session_ratio` x autenticacoes. Saudavel fica perto de 1:1.
    "auth_min": 5,
    "session_ratio": 0.4,
    # Sinal 2 - ninguem entra aqui enquanto os outros recebem gente: zero
    # sessoes no no, os pares com media >= `peer_users_min` usuarios e pelo
    # menos `peer_sess_min` sessoes novas na janela. Serve para o no que nao
    # registra o 1149.
    "peer_users_min": 3,
    "peer_sess_min": 8,
    # Sinal 3 - commit charge (RAM + pagefile) no teto.
    "commit_drain_pct": 95,
    # Sinal 4 (opcional, 0 = desligado) - drenar preventivamente quando um
    # RM.Host.JobRunner passar deste tamanho (GB de memoria privada).
    "drain_jobrunner_gb": 0,
    # Histerese: o no tem de estar doente sem interrupcao por este tempo antes
    # de ser drenado...
    "bad_after_min": 15,
    # ...e apto sem interrupcao por este tempo antes de reingressar.
    "rejoin_after_min": 10,
    # Depois de reiniciar ou reingressar, o no leva um tempo ate receber gente:
    # o sinal 2 (comparacao com os pares) fica desligado nesse intervalo.
    "grace_min": 30,
    # Reingresso: commit abaixo disto e Windows no ar ha pelo menos `min_uptime_min`.
    "commit_ok_pct": 85,
    "min_uptime_min": 10,
    # Seguranca. Restam, DEPOIS da drenagem, pelo menos `min_healthy_remaining`
    # nos ativos e saudaveis (piso absoluto 1) e `min_active_remaining` nos
    # ativos, saudaveis ou nao.
    "min_healthy_remaining": 1,
    "min_active_remaining": 2,
    # Uma drenagem automatica por cluster a cada `cooldown_min`.
    "cooldown_min": 30,
    # Mais que isso por no em 24 h e flapping: passa a so avisar.
    "max_drains_day": 2,
    # Acao que falhou (WinRM engasgado) so e repetida depois deste intervalo.
    "retry_min": 5,
    # No fora do NLB que o RMon NAO drenou (manutencao manual, reboot com
    # atualizacao...): reingressar sozinho quando estiver apto?
    "rejoin_unowned": False,
    # Nos que o RMon nunca toca (manutencao). Tambem pode ser ligado na tela.
    "hold": [],
}

# Uma avaliacao por vez: o ciclo de coleta e os botoes da tela mexem no mesmo estado.
_LOCK = threading.Lock()
STATE_KEY = "nlb_state"


def settings_for(defaults: dict[str, Any] | None) -> dict[str, Any]:
    """Config efetiva (defaults.nlb do servers.yaml sobre DEFAULTS), saneada."""
    cfg = {**DEFAULTS, **(((defaults or {}).get("nlb")) or {})}
    if cfg.get("mode") not in MODOS:
        cfg["mode"] = "alert"
    # nunca deixar o cluster sem nenhum no saudavel, nem por configuracao
    cfg["min_healthy_remaining"] = max(1, int(cfg.get("min_healthy_remaining") or 1))
    cfg["min_active_remaining"] = max(1, int(cfg.get("min_active_remaining") or 1))
    cfg["hold"] = [str(x) for x in (cfg.get("hold") or [])]
    return cfg


def status_label(code: Any) -> str:
    if code is None:
        return "desconhecido"
    return STATUS.get(int(code), f"codigo {code}")


# ---------------------------------------------------------------- vistas

def _rm_ok(services: Iterable[dict] | None, generators: Iterable[str]) -> tuple[bool, list[str]]:
    """Todos os RM.Host.Service* (os que atendem o RM.exe) estao Running?"""
    import fnmatch
    gens = list(generators) or ["RM.Host.Service*"]
    alvo = [s for s in (services or [])
            if any(fnmatch.fnmatch(str(s.get("name") or ""), g) for g in gens)]
    parados = [str(s.get("name")) for s in alvo if (s.get("status") or "") != "Running"]
    return (bool(alvo) and not parados), parados


def view(name: str, host: str, r: dict, now: float,
         generators: Iterable[str] = ("RM.Host.Service*",)) -> dict[str, Any]:
    """O que uma coleta diz sobre o no, do ponto de vista do NLB."""
    n = r.get("nlb") if isinstance(r.get("nlb"), dict) else {}
    jr = r.get("jobrunner") if isinstance(r.get("jobrunner"), dict) else {}
    ok, parados = _rm_ok(r.get("services"), generators)
    uptime = r.get("uptime_sec")
    return {
        "name": name,
        "host": host,
        "reachable": bool(r.get("reachable")),
        "error": (r.get("error") or "")[:160],
        "status": n.get("status"),
        "cluster": n.get("cluster"),
        "host_id": n.get("host_id"),
        "nlb_error": n.get("error"),
        "auth": n.get("auth"),
        "sess": n.get("sess"),
        "users": r.get("users_count"),
        "commit": r.get("commit_pct"),
        "rm_ok": ok,
        "rm_down": parados,
        "uptime_min": (float(uptime) / 60) if uptime is not None else None,
        "boot_ts": (now - float(uptime)) if uptime is not None else None,
        "jobrunner_gb": jr.get("top_gb"),
        "jobrunner_pid": jr.get("top_pid"),
    }


# ---------------------------------------------------------------- sinais

def _peers(v: dict, vistas: list[dict]) -> list[dict]:
    """Os outros nos do mesmo cluster, alcancaveis e ativos no NLB."""
    return [p for p in vistas if p is not v and p["name"] != v["name"]
            and p.get("cluster") == v.get("cluster")
            and p["reachable"] and p.get("status") in ATIVOS]


def sick_reasons(v: dict, vistas: list[dict], cfg: dict, st: dict | None = None,
                 now: float | None = None) -> list[str]:
    """Motivos pelos quais o no, ativo no NLB, nao deveria receber usuario AGORA.

    So julga no alcancavel e ativo: no fora do NLB naturalmente nao cria sessao,
    e no sem WinRM nao tem leitura nenhuma para julgar.
    """
    if not v["reachable"] or v.get("status") not in ATIVOS:
        return []
    motivos: list[str] = []
    jan = cfg["window_min"]
    auth, sess = v.get("auth"), v.get("sess")
    if auth is not None and sess is not None and auth >= cfg["auth_min"] \
            and sess < auth * float(cfg["session_ratio"]):
        motivos.append(f"RDP autentica sem criar sessao: {auth} logon(s) autenticado(s) (1149) "
                       f"e {sess} sessao(oes) criada(s) (21/25) em {jan}min")

    # Sinal 2: comparacao com os pares, fora da caréncia de quem acabou de entrar.
    carencia = cfg["grace_min"] * 60
    recem = False
    if v.get("uptime_min") is not None and v["uptime_min"] * 60 < carencia:
        recem = True
    if st and st.get("joined_at") and now is not None and now - st["joined_at"] < carencia:
        recem = True
    pares = _peers(v, vistas)
    if not recem and pares and v.get("users") == 0 and (sess or 0) == 0:
        media = sum((p.get("users") or 0) for p in pares) / len(pares)
        novas = sum((p.get("sess") or 0) for p in pares)
        if media >= cfg["peer_users_min"] and novas >= cfg["peer_sess_min"]:
            motivos.append(f"nenhuma sessao aqui enquanto os outros nos tem em media {media:.0f} "
                           f"usuario(s) e criaram {novas} sessao(oes) em {jan}min")

    if not v["rm_ok"]:
        motivos.append("RM.Host fora do ar: " + (", ".join(v["rm_down"]) or "nenhum encontrado"))
    commit = v.get("commit")
    if commit is not None and commit >= cfg["commit_drain_pct"]:
        motivos.append(f"commit charge em {commit}% do limite")
    gb = float(cfg.get("drain_jobrunner_gb") or 0)
    if gb and (v.get("jobrunner_gb") or 0) >= gb:
        motivos.append(f"RM.Host.JobRunner (PID {v.get('jobrunner_pid')}) com "
                       f"{v['jobrunner_gb']} GB")
    return motivos


def rejoin_gaps(v: dict, st: dict, cfg: dict) -> list[str]:
    """O que ainda falta para o no fora do NLB voltar (lista vazia = apto)."""
    falta: list[str] = []
    if not v["reachable"]:
        return ["sem contato (WinRM)"]
    if not v["rm_ok"]:
        falta.append("RM.Host fora do ar: " + (", ".join(v["rm_down"]) or "nenhum encontrado"))
    commit = v.get("commit")
    if commit is None or commit >= cfg["commit_ok_pct"]:
        falta.append(f"commit em {commit}% (precisa < {cfg['commit_ok_pct']}%)")
    up = v.get("uptime_min")
    if up is None or up < cfg["min_uptime_min"]:
        falta.append(f"Windows no ar ha menos de {cfg['min_uptime_min']}min")
    # Quem o RMon drenou tem de PROVAR que mudou: ou reiniciou depois da
    # drenagem, ou criou sessao de novo (RDP direto pelo IP, de teste). Sem
    # isso, o no que nao criava sessao voltaria ao balanceamento igual.
    if st.get("owned") and st.get("drained_at"):
        reiniciou = v.get("boot_ts") is not None and v["boot_ts"] > st["drained_at"]
        if not reiniciou and not (v.get("sess") or 0):
            falta.append("sem reboot nem sessao RDP nova desde a drenagem")
    return falta


# ---------------------------------------------------------------- decisao

def _note(st: dict, cat: str, val: str) -> bool:
    """Marca a situacao corrente do no numa categoria; True se ela e NOVA.

    As categorias sao independentes ("sick", "out", "rejoin", "nlb", "fail")
    para que duas situacoes simultaneas nao se revezem na mesma marca - o que
    faria cada uma parecer nova a cada ciclo e repetir a mensagem.
    """
    notas = st.setdefault("notes", {})
    if notas.get(cat) == val:
        return False
    notas[cat] = val
    return True


def _clear(st: dict, *cats: str) -> None:
    notas = st.setdefault("notes", {})
    for c in cats:
        notas.pop(c, None)


def _decision(v: dict, kind: str, msg: str, action: str | None = None,
              new: bool = True) -> dict:
    return {"server": v["name"], "host": v["host"], "cluster": v.get("cluster"),
            "kind": kind, "action": action, "msg": msg, "notify": new}


def _floor(v: dict, vistas: list[dict], saude: dict[str, bool],
           retirados: set[str]) -> tuple[list[dict], list[dict]]:
    """(ativos, saudaveis) que SOBRAM no cluster de `v` se ele sair."""
    cl = v.get("cluster")
    ativos = [p for p in vistas if p["name"] != v["name"] and p.get("cluster") == cl
              and p["name"] not in retirados and p["reachable"] and p.get("status") in ATIVOS]
    return ativos, [p for p in ativos if saude.get(p["name"])]


def evaluate(vistas: list[dict], state: dict, cfg: dict, now: float,
             hold: Iterable[str] = ()) -> tuple[list[dict], dict]:
    """Decide o que fazer com cada no. Funcao pura.

    Devolve (decisoes, estado_novo). Decisao com `action` ("drain"/"rejoin") e
    para executar - so aparece no modo auto; no modo alert a mesma situacao vira
    `kind` "would_drain"/"would_rejoin", sem acao. `notify` diz se a situacao e
    nova (transicao) e merece mensagem; as repetidas so atualizam o estado.
    """
    st_all = copy.deepcopy(state or {})
    nodes: dict = st_all.setdefault("nodes", {})
    clusters: dict = st_all.setdefault("clusters", {})
    if cfg.get("mode") == "off":
        return [], st_all
    hold_cfg = set(hold) | set(cfg.get("hold") or [])
    auto = cfg.get("mode") == "auto"
    bad_after = cfg["bad_after_min"] * 60
    rejoin_after = cfg["rejoin_after_min"] * 60
    retry = cfg["retry_min"] * 60
    decisoes: list[dict] = []

    doentes: list[tuple[dict, list[str]]] = []
    aptos: list[dict] = []
    saude: dict[str, bool] = {}

    def em_hold(nome: str) -> bool:
        return bool(nodes[nome].get("hold")) or nome in hold_cfg

    for v in vistas:
        st = nodes.setdefault(v["name"], {})
        st["drains"] = [t for t in (st.get("drains") or []) if now - t < 86400]
        if v.get("status") is not None:
            st["last_status"] = v["status"]
        if v.get("cluster"):
            st["cluster"] = v["cluster"]

        if not v["reachable"]:
            st["good_since"] = None
            if st.get("bad_since") and st.get("last_status") in ATIVOS:
                # Doente e agora sem WinRM: e o pior caso (commit esgotado) e o
                # RMon nao tem como drenar. Mantem bad_since: quando o WinRM
                # voltar e ele ainda estiver doente, a drenagem sai na hora.
                if _note(st, "sick", "unreachable"):
                    decisoes.append(_decision(
                        v, "unreachable_sick",
                        "no doente e sem contato por WinRM: o RMon nao consegue drena-lo. "
                        "Drene pelo console (nlb.exe drainstop) ou reinicie o servidor."))
            continue

        status = v.get("status")
        if status is None:
            if v.get("nlb_error") and _note(st, "nlb", "error"):
                decisoes.append(_decision(v, "nlb_error",
                                          f"nao deu para ler o estado do NLB: {v['nlb_error']}"))
            continue
        _clear(st, "nlb")

        if status in ATIVOS:
            st["good_since"] = None
            st["rejoin_gaps"] = []
            _clear(st, "out", "rejoin")
            if st.get("owned"):
                # voltou ao NLB por fora (alguem deu start na mao)
                st["owned"] = False
                st["drained_at"] = None
            motivos = sick_reasons(v, vistas, cfg, st, now)
            saude[v["name"]] = not motivos
            if motivos:
                st["bad_since"] = st.get("bad_since") or now
                st["bad_reasons"] = motivos
                if now - st["bad_since"] >= bad_after:
                    doentes.append((v, motivos))
            else:
                if (st.get("notes") or {}).get("sick") not in (None, "drained"):
                    decisoes.append(_decision(v, "recovered",
                                              "voltou a ficar saudavel sem sair do NLB"))
                st["bad_since"] = None
                st["bad_reasons"] = []
                _clear(st, "sick")
            continue

        # fora do balanceamento (parado, drenando, suspenso)
        st["bad_since"] = None
        st["bad_reasons"] = []
        saude[v["name"]] = False
        if em_hold(v["name"]):
            st["good_since"] = None
            continue
        falta = rejoin_gaps(v, st, cfg)
        st["rejoin_gaps"] = falta
        dono = bool(st.get("owned"))
        if not dono and not cfg.get("rejoin_unowned") and _note(st, "out", "unowned"):
            decisoes.append(_decision(
                v, "out_unowned",
                f"fora do NLB ({status_label(status)}) sem ter sido drenado pelo RMon: "
                "reingresse manualmente (nlb.exe start) ou pela tela /nlb"
                + ("" if falta else ". Ele ja esta apto")))
        if falta:
            st["good_since"] = None
            _clear(st, "rejoin")
            continue
        st["good_since"] = st.get("good_since") or now
        if now - st["good_since"] >= rejoin_after and (dono or cfg.get("rejoin_unowned")):
            aptos.append(v)

    # ---- drenagem: o doente mais antigo primeiro. No modo auto sai no maximo
    # uma por cluster por ciclo; no modo alerta a simulacao continua, contando
    # os ja "drenados" para mostrar o que o piso faria com o segundo doente.
    doentes.sort(key=lambda x: nodes[x[0]["name"]]["bad_since"])
    retirados: set[str] = set()
    agiu: set[str] = set()
    for v, motivos in doentes:
        st = nodes[v["name"]]
        cl = v.get("cluster") or "-"
        cst = clusters.setdefault(cl, {})
        resumo = "; ".join(motivos)
        if em_hold(v["name"]):
            if _note(st, "sick", "hold"):
                decisoes.append(_decision(v, "blocked", f"doente, mas em manutencao (hold): {resumo}"))
            continue
        if auto and cl in agiu:
            continue
        if len(st["drains"]) >= cfg["max_drains_day"]:
            if _note(st, "sick", "flapping"):
                decisoes.append(_decision(
                    v, "blocked",
                    f"doente, mas ja foi drenado {len(st['drains'])}x em 24h (flapping): "
                    f"so aviso. Motivo: {resumo}"))
            continue
        ativos, saudaveis = _floor(v, vistas, saude, retirados)
        if len(saudaveis) < cfg["min_healthy_remaining"] or len(ativos) < cfg["min_active_remaining"]:
            if _note(st, "sick", f"floor:{len(ativos)}:{len(saudaveis)}"):
                decisoes.append(_decision(
                    v, "blocked",
                    f"doente, mas drenar deixaria {len(ativos)} no(s) ativo(s) e "
                    f"{len(saudaveis)} saudavel(is) (minimos {cfg['min_active_remaining']} e "
                    f"{cfg['min_healthy_remaining']}): fica no NLB. Motivo: {resumo}"))
            continue
        if auto:
            ultimo = cst.get("last_auto") or 0
            if now - ultimo < cfg["cooldown_min"] * 60:
                if _note(st, "sick", "cooldown"):
                    decisoes.append(_decision(
                        v, "blocked",
                        f"doente, mas houve drenagem automatica neste cluster ha "
                        f"{int((now - ultimo) / 60)}min (cooldown {cfg['cooldown_min']}min). "
                        f"Motivo: {resumo}"))
                continue
            agiu.add(cl)
            if st.get("last_try") and now - st["last_try"] < retry:
                continue
            retirados.add(v["name"])
            decisoes.append(_decision(v, "drain", resumo, action="drain",
                                      new=_note(st, "sick", "draining")))
        else:
            retirados.add(v["name"])
            if _note(st, "sick", "would_drain"):
                decisoes.append(_decision(
                    v, "would_drain",
                    f"[modo alerta] drenaria agora (sobram {len(ativos)} ativo(s), "
                    f"{len(saudaveis)} saudavel(is)): {resumo}"))

    # ---- reingresso: um por cluster por ciclo
    voltando: set[str] = set()
    for v in aptos:
        st = nodes[v["name"]]
        cl = v.get("cluster") or "-"
        if cl in voltando:
            continue
        voltando.add(cl)
        if auto:
            if st.get("last_try") and now - st["last_try"] < retry:
                continue
            decisoes.append(_decision(v, "rejoin", "RM.Host no ar e commit normal",
                                      action="rejoin", new=_note(st, "rejoin", "rejoining")))
        elif _note(st, "rejoin", "would"):
            decisoes.append(_decision(
                v, "would_rejoin",
                "[modo alerta] reingressaria agora: RM.Host no ar, commit normal, apto ha "
                f"{int((now - st['good_since']) / 60)}min"))
    return decisoes, st_all


def apply_result(state: dict, d: dict, ok: bool, now: float,
                 detail: str = "") -> tuple[dict, bool]:
    """Registra no estado o resultado de uma acao executada.

    Devolve (estado, falha_nova): a falha so e notificada na primeira vez - a
    acao e repetida a cada `retry_min` enquanto o no seguir doente, e o canal
    nao precisa de uma mensagem igual a cada tentativa.
    """
    st_all = copy.deepcopy(state)
    st = st_all.setdefault("nodes", {}).setdefault(d["server"], {})
    st["last_action"] = {"action": d["action"], "ok": ok, "ts": now, "detail": detail[:300],
                         "by": d.get("by") or "rmon"}
    if not ok:
        st["last_try"] = now
        return st_all, _note(st, "fail", d["action"])
    st["last_try"] = None
    _clear(st, "fail")
    if d["action"] == "drain":
        st["owned"] = True
        st["drained_at"] = now
        st["drains"] = (st.get("drains") or []) + [now]
        _note(st, "sick", "drained")
        if d.get("cluster") and not d.get("by"):
            st_all.setdefault("clusters", {}).setdefault(d["cluster"], {})["last_auto"] = now
    elif d["action"] == "rejoin":
        st["owned"] = False
        st["drained_at"] = None
        st["good_since"] = None
        st["joined_at"] = now
        _clear(st, "out", "rejoin", "sick")
    return st_all, False


def floor_ok(vistas: list[dict], alvo: str, cfg: dict) -> tuple[bool, str]:
    """A drenagem MANUAL de `alvo` respeita o piso? (mesma regra da automatica)"""
    v = next((x for x in vistas if x["name"] == alvo), None)
    if v is None:
        return False, "no desconhecido"
    cl = v.get("cluster")
    ativos = [p for p in vistas if p["name"] != alvo and p.get("cluster") == cl
              and p["reachable"] and p.get("status") in ATIVOS]
    saudaveis = [p for p in ativos if not sick_reasons(p, vistas, cfg)]
    if len(saudaveis) < cfg["min_healthy_remaining"] or len(ativos) < cfg["min_active_remaining"]:
        return False, (f"restariam {len(ativos)} no(s) ativo(s) e {len(saudaveis)} saudavel(is) "
                       f"(minimos {cfg['min_active_remaining']} e {cfg['min_healthy_remaining']})")
    return True, ""


# ---------------------------------------------------------------- adaptador

ICON = {"drain": "\U0001F500", "rejoin": "✅", "would_drain": "\U0001F7E0",
        "would_rejoin": "\U0001F7E2", "blocked": "⛔", "unreachable_sick": "\U0001F534",
        "out_unowned": "⚠️", "recovered": "\U0001F7E2", "nlb_error": "⚠️",
        "failed": "❌"}


def _message(d: dict) -> str:
    cl = f" [{d['cluster']}]" if d.get("cluster") else ""
    return f"{ICON.get(d['kind'], '')} NLB{cl} — {d['server']} ({d['host']}): {d['msg']}"


def load_state() -> dict:
    from . import db
    return db.get_config(STATE_KEY, {}) or {}


def save_state(state: dict) -> None:
    from . import db
    db.set_config(STATE_KEY, state)


def nlb_servers(inv) -> list:
    return [s for s in inv.servers if getattr(s, "nlb", False)]


def views_from(inv, coletas: dict[str, dict], now: float) -> list[dict]:
    from .collector import broker_settings
    gens = broker_settings(inv.defaults).get("generators") or ["RM.Host.Service*"]
    return [view(s.name, s.host, coletas.get(s.name) or {"reachable": False}, now, gens)
            for s in nlb_servers(inv)]


def _record(d: dict, ok: bool | None = None) -> None:
    """Alerta + auditoria + mensagem de uma decisao."""
    from . import db, notify
    kind = d["kind"] if ok is not False else "failed"
    db.record_alert(d["server"], "nlb", kind, d["msg"])
    db.audit(d.get("by") or "rmon-nlb", f"nlb_{kind}", f"{d['server']}: {d['msg']}"[:900])
    if d.get("notify") and notify.enabled():
        notify.send(_message({**d, "kind": kind}))


def execute(server, wc, action: str) -> tuple[bool, str]:
    from .collector import nlb_action
    return nlb_action(server, wc, action)


def run_cycle(inv, coletas: dict[str, dict], now: float | None = None) -> list[dict]:
    """Chamado ao fim de cada poll_all, com as coletas que acabaram de chegar."""
    cfg = settings_for(inv.defaults)
    servidores = {s.name: s for s in nlb_servers(inv)}
    if cfg["mode"] == "off" or not servidores:
        return []
    now = time.time() if now is None else now
    with _LOCK:
        state = load_state()
        vistas = views_from(inv, coletas, now)
        decisoes, state = evaluate(vistas, state, cfg, now)
        for d in decisoes:
            if d.get("action"):
                ok, detalhe = execute(servidores[d["server"]], inv.winrm, d["action"])
                state, falha_nova = apply_result(state, d, ok, now, detalhe)
                if not ok:
                    d = {**d, "msg": f"{d['action']} FALHOU: {detalhe[:200]}"}
                elif d["action"] == "drain":
                    d = {**d, "msg": "drenado (nlb.exe drainstop): " + d["msg"].split(": ", 1)[-1]}
                else:
                    d = {**d, "msg": "reingressado (nlb.exe start)"}
                d["notify"] = ok or falha_nova
                _record(d, ok)
                log.warning("NLB %s %s -> %s (%s)", d["server"], d["action"], ok, detalhe[:200])
            else:
                _record(d)
                log.info("NLB %s %s: %s", d["server"], d["kind"], d["msg"])
        save_state(state)
    return decisoes


def manual(inv, coletas: dict[str, dict], nome: str, acao: str, user: str) -> tuple[bool, str]:
    """Acao da tela (/nlb), sempre auditada. Drenar respeita o mesmo piso da automatica.

    `hold`/`release` so mexem no estado do RMon (manutencao); `drain`/`rejoin`
    executam no host. Drenagem manual liga o hold - quem drenou na mao esta
    fazendo manutencao e nao quer o RMon devolvendo o no ao NLB.
    """
    from . import db
    cfg = settings_for(inv.defaults)
    servidores = {s.name: s for s in nlb_servers(inv)}
    if nome not in servidores:
        return False, "servidor fora do NLB monitorado"
    now = time.time()
    with _LOCK:
        state = load_state()
        st = state.setdefault("nodes", {}).setdefault(nome, {})
        if acao in ("hold", "release"):
            st["hold"] = acao == "hold"
            _clear(st, "sick", "out", "rejoin")
            save_state(state)
            db.audit(user, f"nlb_{acao}", nome)
            return True, ("em manutencao: o RMon nao drena nem reingressa este no"
                          if acao == "hold" else "liberado para a automacao")
        if acao not in ("drain", "rejoin"):
            return False, "acao invalida"
        vistas = views_from(inv, coletas, now)
        if acao == "drain":
            ok, motivo = floor_ok(vistas, nome, cfg)
            if not ok:
                db.audit(user, "nlb_drain_recusado", f"{nome}: {motivo}")
                return False, f"recusado: {motivo}"
        ok, detalhe = execute(servidores[nome], inv.winrm, acao)
        d = {"server": nome, "host": servidores[nome].host, "cluster": st.get("cluster"),
             "action": acao, "kind": acao, "by": user, "notify": True,
             "msg": f"{'drenado' if acao == 'drain' else 'reingressado'} manualmente por {user}"}
        state, _ = apply_result(state, d, ok, now, detalhe)
        st = state["nodes"][nome]
        st["hold"] = acao == "drain" if ok else st.get("hold")
        if ok and acao == "drain":
            st["owned"] = False      # manutencao: nao e drenagem do RMon
        save_state(state)
        if not ok:
            d["msg"] = f"{acao} manual por {user} FALHOU: {detalhe[:200]}"
        _record(d, ok)
        return ok, detalhe or ("ok" if ok else "falhou")
