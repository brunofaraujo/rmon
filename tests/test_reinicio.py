"""Servidor fora do NLB travado sem contato (app/reinicio.py) e o canal da iLO (app/ilo.py).

O caso real e o .222 em 19/10: JobRunner esgotou a memoria as 10:31, o WinRM, o
RPC e o agente do Elastic pararam juntos e o servidor ficou esperando alguem.
"""
from __future__ import annotations

from types import SimpleNamespace

from app import db, ilo, incus, notify, reinicio

CFG = reinicio.settings_for({})
T0 = 1_760_000_000.0
ILO = ("ilo", "o servidor pela iLO 10.9.9.22")


def es_mudo(ultimo=T0 - 30):
    return {"ok": True, "ts": None} if ultimo is None else {"ok": True, "ts": ultimo}


def rodar(st, ciclos, es, via=ILO, manut=False, inicio=T0, passo=60):
    """`ciclos` coletas sem contato seguidas; devolve as decisoes."""
    out = []
    for i in range(ciclos):
        d = reinicio.avaliar("jobs-222", False, es if i == 0 else {"ok": True, "ts": None}, st,
                             CFG, inicio + i * passo, via, manut)
        out.append(d)
    return out


def test_222_travado_e_reiniciado_na_terceira_coleta():
    st = {}
    d = rodar(st, 3, es_mudo(T0 - 30), passo=120)
    assert d[0] is None and d[1] is None
    assert d[2]["action"] and d[2]["kind"] == "reset_unreachable"
    assert "iLO 10.9.9.22" in d[2]["msg"] and "o Windows travou" in d[2]["msg"]


def test_so_o_winrm_travado_nao_reinicia():
    st = {}
    for i in range(3):
        d = reinicio.avaliar("x", False, {"ok": True, "ts": T0 + i * 60 - 10}, st, CFG, T0 + i * 60, ILO, False)
    assert d["kind"] == "unreachable_alive" and not d["action"]


def test_sem_elastic_nao_faz_nada_alem_do_down():
    st = {}
    for i in range(5):
        d = reinicio.avaliar("x", False, None, st, CFG, T0 + i * 60, ILO, False)
    assert d is None


def test_sem_canal_avisa_uma_vez_por_episodio():
    st = {}
    d = rodar(st, 6, es_mudo(T0 - 30), via=("", ""), passo=120)
    crit = [x for x in d if x]
    assert len(crit) == 1 and crit[0]["kind"] == "unreachable_crit"
    assert "não tem como reiniciá-lo" in crit[0]["msg"]
    # voltou e travou de novo: novo episodio, novo aviso
    reinicio.avaliar("x", True, None, st, CFG, T0 + 3600, ("", ""), False)
    d = rodar(st, 3, es_mudo(T0 + 3600), via=("", ""), inicio=T0 + 3660, passo=120)
    assert [x["kind"] for x in d if x] == ["unreachable_crit"]


def test_manutencao_e_modo_off_nao_reiniciam():
    assert rodar({}, 4, es_mudo(), manut=True, passo=120)[-1] is None
    cfg = reinicio.settings_for({"reinicio": {"mode": "off"}})
    st = {}
    for i in range(4):
        d = reinicio.avaliar("x", False, es_mudo(), st, cfg, T0 + i * 120, ILO, False)
    assert d is None


def test_modo_alerta_so_diz_o_que_faria():
    cfg = reinicio.settings_for({"reinicio": {"mode": "alert"}})
    st = {}
    for i in range(3):
        d = reinicio.avaliar("x", False, es_mudo() if i == 0 else {"ok": True}, st, cfg, T0 + i * 120, ILO, False)
    assert d["kind"] == "would_reset" and not d["action"] and "Reiniciaria" in d["msg"]


def test_espera_o_boot_e_para_no_limite_do_dia():
    st = {"resets": [T0 - 60]}
    # reiniciado ha 1 min: espera o boot
    assert rodar(st, 3, es_mudo(T0 - 300), passo=60)[-1] is None
    st = {"resets": [T0 - 7200, T0 - 5400]}
    d = rodar(st, 3, es_mudo(T0 - 300), passo=60)[-1]
    assert d["kind"] == "unreachable_crit" and "2x em 24h" in d["msg"]


def test_canal_preferido():
    nlb_cfg = {"incus_ssh": "rmon-reset@10.10.10.161"}
    vm = SimpleNamespace(name="a", incus_vm="win2016", ilo="")
    fisico = SimpleNamespace(name="b", incus_vm="", ilo="10.9.9.22")
    nada = SimpleNamespace(name="c", incus_vm="", ilo="")
    assert reinicio.canal(vm, nlb_cfg)[0] == "incus"
    assert reinicio.canal(vm, {})[0] == ""
    assert reinicio.canal(fisico, {})[0] == "ilo"
    assert reinicio.canal(nada, nlb_cfg) == ("", "")


def test_run_cycle_reinicia_pela_ilo_e_registra(monkeypatch):
    estado, alertas, enviadas, resets = {}, [], [], []
    monkeypatch.setattr(db, "get_config", lambda k, d=None: estado.get(k, d))
    monkeypatch.setattr(db, "set_config", lambda k, v: estado.__setitem__(k, v))
    monkeypatch.setattr(db, "record_alert", lambda *a: alertas.append(a))
    monkeypatch.setattr(db, "audit", lambda *a, **k: None)
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(notify, "send", lambda t: enviadas.append(t))
    monkeypatch.setattr(ilo, "reset", lambda s: resets.append(s.name) or (True, "reiniciado pela iLO"))
    monkeypatch.setattr(ilo, "checar", lambda s: {"ok": True, "detail": "iLO ok"})
    srv = SimpleNamespace(name="fin-sge-jobs-222", host="10.10.10.222", nlb=False, incus_vm="",
                          ilo="10.9.9.22", ilo_cred="ilo")
    no = SimpleNamespace(name="fin-sge-app-188", host="10.10.10.188", nlb=True, incus_vm="win2016", ilo="")
    inv = SimpleNamespace(servers=[srv, no], defaults={}, winrm=None)
    for i in range(3):
        reinicio.run_cycle(inv, {s.name: {"reachable": False} for s in inv.servers},
                           {srv.name: {"ok": True, "ts": T0 - 30 if i == 0 else None}},
                           now=T0 + i * 120)
    assert resets == ["fin-sge-jobs-222"]          # o no do NLB nao e com este modulo
    assert [a[2] for a in alertas] == ["reset_unreachable"] and alertas[0][1] == "reinicio"
    assert "Reiniciado:" in alertas[0][3]
    assert any("Reiniciado:" in t for t in enviadas)


def test_ilo_nunca_usa_a_senha_do_winrm(monkeypatch):
    monkeypatch.setenv("RMON_WINRM_USER", "dominio\\admin")
    monkeypatch.setenv("RMON_WINRM_PASSWORD", "segredo")
    monkeypatch.delenv("RMON_CRED_ILO_USER", raising=False)
    chamadas = []
    monkeypatch.setattr(ilo.requests, "request", lambda *a, **k: chamadas.append(k))
    ok, msg = ilo.reset(SimpleNamespace(ilo="10.9.9.22", ilo_cred="ilo"))
    assert not ok and "RMON_CRED_ILO_USER" in msg and chamadas == []


def test_ilo_manda_force_restart(monkeypatch):
    monkeypatch.setenv("RMON_CRED_ILO_USER", "rmon")
    monkeypatch.setenv("RMON_CRED_ILO_PASSWORD", "x")
    pedidos = []

    def falso(metodo, url, **kw):
        pedidos.append((metodo, url, kw.get("json")))
        return SimpleNamespace(status_code=200, text="")
    monkeypatch.setattr(ilo.requests, "request", falso)
    ok, _ = ilo.reset(SimpleNamespace(ilo="10.9.9.22", ilo_cred="ilo"))
    assert ok and pedidos == [("POST", "https://10.9.9.22/redfish/v1/Systems/1/Actions/ComputerSystem.Reset/",
                               {"ResetType": "ForceRestart"})]
    assert not ilo.configurado(SimpleNamespace(ilo="10.9.9.22/../x"))


def test_incus_e_o_mesmo_canal_do_nlb(monkeypatch):
    feitos = []
    monkeypatch.setattr(incus, "reset", lambda cfg, vm: feitos.append(vm) or (True, "ok"))
    srv = SimpleNamespace(name="x", incus_vm="vm-x", ilo="")
    assert reinicio._executar(srv, "incus", {"incus_ssh": "a@b"}) == (True, "ok") and feitos == ["vm-x"]
