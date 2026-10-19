"""Servidor fora do NLB travado sem contato (app/reinicio.py).

O caso real e o .222 em 19/10: JobRunner esgotou a memoria as 10:31, o WinRM, o
RPC e o agente do Elastic pararam juntos e o servidor ficou esperando alguem.
"""
from __future__ import annotations

from types import SimpleNamespace

from app import db, incus, notify, reinicio

CFG = reinicio.settings_for({})
T0 = 1_760_000_000.0
VM = ("incus", "a VM vm-x pelo Incus")
FISICO = ("", "")


def es_mudo(ultimo=T0 - 30):
    return {"ok": True, "ts": ultimo}


def rodar(st, ciclos, es, via=VM, manut=False, inicio=T0, passo=60):
    """`ciclos` coletas sem contato seguidas; devolve as decisoes."""
    return [reinicio.avaliar("x", False, es if i == 0 else {"ok": True, "ts": None}, st,
                             CFG, inicio + i * passo, via, manut)
            for i in range(ciclos)]


def test_vm_travada_e_reiniciada_na_terceira_coleta():
    d = rodar({}, 3, es_mudo(), passo=120)
    assert d[0] is None and d[1] is None
    assert d[2]["action"] and d[2]["kind"] == "reset_unreachable"
    assert "vm-x pelo Incus" in d[2]["msg"] and "o Windows travou" in d[2]["msg"]


def test_fisico_travado_avisa_uma_vez_por_episodio():
    # o .222: sem canal fora do Windows
    st = {}
    crit = [x for x in rodar(st, 6, es_mudo(), via=FISICO, passo=120) if x]
    assert len(crit) == 1 and crit[0]["kind"] == "unreachable_crit" and not crit[0]["action"]
    assert "servidor físico" in crit[0]["msg"] and "console" in crit[0]["msg"]
    # voltou e travou de novo: novo episodio, novo aviso
    reinicio.avaliar("x", True, None, st, CFG, T0 + 3600, FISICO, False)
    d = rodar(st, 3, es_mudo(T0 + 3600), via=FISICO, inicio=T0 + 3660, passo=120)
    assert [x["kind"] for x in d if x] == ["unreachable_crit"]


def test_so_o_winrm_travado_nao_reinicia():
    st = {}
    for i in range(3):
        d = reinicio.avaliar("x", False, {"ok": True, "ts": T0 + i * 60 - 10}, st, CFG, T0 + i * 60, VM, False)
    assert d["kind"] == "unreachable_alive" and not d["action"]


def test_sem_elastic_nao_faz_nada_alem_do_down():
    st = {}
    for i in range(5):
        d = reinicio.avaliar("x", False, None, st, CFG, T0 + i * 60, VM, False)
    assert d is None


def test_manutencao_e_modo_off_nao_reiniciam():
    assert rodar({}, 4, es_mudo(), manut=True, passo=120)[-1] is None
    cfg = reinicio.settings_for({"reinicio": {"mode": "off"}})
    st = {}
    for i in range(4):
        d = reinicio.avaliar("x", False, es_mudo(), st, cfg, T0 + i * 120, VM, False)
    assert d is None


def test_modo_alerta_so_diz_o_que_faria():
    cfg = reinicio.settings_for({"reinicio": {"mode": "alert"}})
    st = {}
    for i in range(3):
        d = reinicio.avaliar("x", False, es_mudo() if i == 0 else {"ok": True}, st, cfg, T0 + i * 120, VM, False)
    assert d["kind"] == "would_reset" and not d["action"] and "Reiniciaria" in d["msg"]


def test_espera_o_boot_e_para_no_limite_do_dia():
    # reiniciado ha 1 min: espera o boot
    assert rodar({"resets": [T0 - 60]}, 3, es_mudo(T0 - 300))[-1] is None
    d = rodar({"resets": [T0 - 7200, T0 - 5400]}, 3, es_mudo(T0 - 300))[-1]
    assert d["kind"] == "unreachable_crit" and "2x em 24h" in d["msg"]


def test_canal():
    vm = SimpleNamespace(name="a", incus_vm="win2016")
    fisico = SimpleNamespace(name="b", incus_vm="")
    assert reinicio.canal(vm, {"incus_ssh": "rmon-reset@10.10.10.161"})[0] == "incus"
    assert reinicio.canal(vm, {}) == ("", "")
    assert reinicio.canal(fisico, {"incus_ssh": "x@y"}) == ("", "")


def test_run_cycle_reinicia_vm_fora_do_nlb_e_registra(monkeypatch):
    estado, alertas, enviadas, resets = {}, [], [], []
    monkeypatch.setattr(db, "get_config", lambda k, d=None: estado.get(k, d))
    monkeypatch.setattr(db, "set_config", lambda k, v: estado.__setitem__(k, v))
    monkeypatch.setattr(db, "record_alert", lambda *a: alertas.append(a))
    monkeypatch.setattr(db, "audit", lambda *a, **k: None)
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(notify, "send", lambda t: enviadas.append(t))
    monkeypatch.setattr(incus, "reset", lambda cfg, vm: resets.append(vm) or (True, "ok"))
    vm = SimpleNamespace(name="jobs-vm", host="10.0.0.9", nlb=False, incus_vm="vm-jobs")
    fisico = SimpleNamespace(name="fin-sge-jobs-222", host="10.10.10.222", nlb=False, incus_vm="")
    no = SimpleNamespace(name="fin-sge-app-188", host="10.10.10.188", nlb=True, incus_vm="win2016")
    inv = SimpleNamespace(servers=[vm, fisico, no], winrm=None,
                          defaults={"nlb": {"incus_ssh": "rmon-reset@10.10.10.161"}})
    for i in range(3):
        es = {s.name: {"ok": True, "ts": T0 - 30 if i == 0 else None} for s in (vm, fisico)}
        reinicio.run_cycle(inv, {s.name: {"reachable": False} for s in inv.servers}, es, now=T0 + i * 120)
    assert resets == ["vm-jobs"]                   # o no do NLB nao e com este modulo
    por = {a[0]: a for a in alertas}
    assert por["jobs-vm"][1:3] == ("reinicio", "reset_unreachable") and "Reiniciado:" in por["jobs-vm"][3]
    assert por["fin-sge-jobs-222"][2] == "unreachable_crit"
    assert "fin-sge-app-188" not in por
    assert len(enviadas) == 2
