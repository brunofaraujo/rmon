"""Ciclo de alertas de ponta a ponta (scheduler.poll_all), com banco e canais falsos.

O que se testa aqui e o que o usuario ve no celular ao longo de varios ciclos:
um restart do RMon (todo deploy) nao pode renotificar o que ja estava aberto, e
um engasgo do WinRM nao pode "resolver" e reabrir tudo.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import db, nlb, notify, scheduler


class Banco:
    """O pedaco do app.db que o poll_all usa, em memoria."""

    def __init__(self):
        self.alertas: list[tuple[str, str, str, str]] = []
        self.checks: dict[str, list[bool]] = {}

    def open_alerts(self):
        ultimo: dict[tuple[str, str], tuple[str, str]] = {}
        for server, kind, problem, msg in self.alertas:
            if kind in ("raised", "resolved"):
                ultimo[(server, problem)] = (kind, msg)
        abertos: dict[str, dict[str, str]] = {}
        for (server, problem), (kind, msg) in ultimo.items():
            if kind == "raised":
                abertos.setdefault(server, {})[problem] = msg
        return abertos

    def record_alert(self, server, kind, problem, message):
        self.alertas.append((server, kind, problem, message))

    def insert_check(self, server, result):
        self.checks.setdefault(server, []).insert(0, bool(result.get("reachable")))

    def fail_streak(self, server, limit=20):
        n = 0
        for ok in self.checks.get(server, [])[:limit]:
            if ok:
                break
            n += 1
        return n


@pytest.fixture
def ambiente(monkeypatch):
    banco = Banco()
    enviadas: list[str] = []
    coletas: dict[str, dict] = {}
    for nome in ("open_alerts", "record_alert", "insert_check", "fail_streak"):
        monkeypatch.setattr(db, nome, getattr(banco, nome))
    monkeypatch.setattr(db, "get_config", lambda k, d=None: d)
    monkeypatch.setattr(db, "broker_max", lambda dias=30: {})
    monkeypatch.setattr(db, "broker_estado", lambda dias=1: {})
    monkeypatch.setattr(db, "fila_hist", lambda n=3: {})
    monkeypatch.setattr(nlb, "run_cycle", lambda inv, res: None)
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(notify, "send", lambda texto: enviadas.append(texto) or True)
    monkeypatch.setattr(scheduler, "collect_server",
                        lambda s, winrm, defaults: dict(coletas[s.name]))
    servidor = SimpleNamespace(name="app-34", host="10.0.0.34", jobs=None)
    inv = SimpleNamespace(servers=[servidor], winrm=None, defaults={})

    def reiniciar():
        """O que um restart do servico faz com o scheduler: memoria zerada."""
        monkeypatch.setattr(scheduler, "_last", {})
        monkeypatch.setattr(scheduler, "_restaurado", False)

    def ciclo(**r):
        coletas["app-34"] = r
        enviadas.clear()
        scheduler.poll_all(inv, None)
        return list(enviadas)

    reiniciar()
    return SimpleNamespace(banco=banco, ciclo=ciclo, reiniciar=reiniciar)


def ok(**kw):
    r = {"reachable": True, "mem_pct": 40, "disks": [], "app_ok": True, "app_ms": 100,
         "services": [{"name": "RM.Host.Service", "status": "Running"},
                      {"name": "RM.Host.Service1", "status": "Running"}]}
    r.update(kw)
    return r


def parado(nome="RM.Host.Service1"):
    return ok(services=[{"name": "RM.Host.Service", "status": "Running"},
                        {"name": nome, "status": "Stopped"}])


def sem_contato():
    return {"reachable": False, "error": "WinRMOperationTimeoutError"}


def kinds(banco):
    return [(k, p) for _, k, p, _ in banco.alertas]


def test_restart_nao_renotifica_o_que_ja_estava_aberto(ambiente):
    assert len(ambiente.ciclo(**parado())) == 1
    ambiente.reiniciar()
    assert ambiente.ciclo(**parado()) == []
    assert kinds(ambiente.banco) == [("raised", "svc:RM.Host.Service1")]


def test_o_que_se_resolveu_com_o_rmon_parado_ganha_o_resolvido(ambiente):
    ambiente.ciclo(**parado())
    ambiente.reiniciar()
    msgs = ambiente.ciclo(**ok())
    assert len(msgs) == 1 and "resolvido" in msgs[0]
    assert kinds(ambiente.banco)[-1] == ("resolved", "svc:RM.Host.Service1")


def test_engasgo_do_winrm_nao_resolve_nem_reabre(ambiente):
    ambiente.ciclo(**parado())
    assert ambiente.ciclo(**sem_contato()) == []
    assert ambiente.ciclo(**parado()) == []
    assert kinds(ambiente.banco) == [("raised", "svc:RM.Host.Service1")]


def test_host_fora_do_ar_abre_down_e_volta_resolvendo_so_o_que_voltou(ambiente):
    ambiente.ciclo(**parado())
    assert ambiente.ciclo(**sem_contato()) == []
    assert ambiente.ciclo(**sem_contato()) == []
    msgs = ambiente.ciclo(**sem_contato())
    assert len(msgs) == 1 and "sem contato" in msgs[0]
    msgs = ambiente.ciclo(**parado())
    assert "resolvido: sem contato" in msgs[0]
    assert "svc:RM.Host.Service1" not in msgs[0]
    assert ("resolved", "svc:RM.Host.Service1") not in kinds(ambiente.banco)


def test_servico_tirado_do_inventario_fecha_calado(ambiente):
    ambiente.ciclo(**parado("RM.Host.Service3"))
    ambiente.reiniciar()  # o servers.yaml so e relido no start
    assert ambiente.ciclo(**ok()) == []
    server, kind, problem, msg = ambiente.banco.alertas[-1]
    assert (kind, problem) == ("resolved", "svc:RM.Host.Service3")
    assert msg.endswith("(saiu do monitoramento)")


def test_restauracao_so_acontece_uma_vez(ambiente, monkeypatch):
    chamadas = []
    original = db.open_alerts
    monkeypatch.setattr(db, "open_alerts", lambda: chamadas.append(1) or original())
    ambiente.ciclo(**ok())
    ambiente.ciclo(**ok())
    assert chamadas == [1]


def test_banco_fora_no_start_nao_avalia_com_estado_vazio(ambiente, monkeypatch):
    ambiente.ciclo(**parado())
    ambiente.reiniciar()

    def fora():
        raise RuntimeError("connection refused")

    original = db.open_alerts
    monkeypatch.setattr(db, "open_alerts", fora)
    with pytest.raises(RuntimeError):
        ambiente.ciclo(**parado())
    monkeypatch.setattr(db, "open_alerts", original)
    assert ambiente.ciclo(**parado()) == []


def test_servidor_tirado_do_inventario_fecha_calado(ambiente):
    ambiente.banco.record_alert("app-antigo", "raised", "DISK", "disco C: em 95%")
    ambiente.ciclo(**ok())
    assert ambiente.banco.alertas[-1] == ("app-antigo", "resolved", "DISK",
                                          "disco C: em 95% (saiu do monitoramento)")
    assert ambiente.banco.open_alerts() == {}
