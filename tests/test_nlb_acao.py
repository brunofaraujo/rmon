"""drainstop/start do NLB: o estado "depois" tem de ser o final, nao o transitorio.

O caso real e o .188 em 17/11/2026: o drainstop funcionou (o no foi para 1009,
drenando), mas o RMon leu o estado 5 s depois, ainda em 1006 (convergindo),
registrou falha e nao ligou a manutencao do no.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app import collector


def _rodar(monkeypatch, acao, depois):
    visto = {}

    def falso(host, wc, user, pw, script, attempts, budget_sec):
        visto["script"] = script
        visto["budget"] = budget_sec
        return SimpleNamespace(status_code=0, std_err=b"",
                               std_out=json.dumps({"before": 1007, "after": depois,
                                                   "output": "ok"}).encode())

    monkeypatch.setattr(collector, "credential_for", lambda cred: ("u", "p"))
    monkeypatch.setattr(collector, "_run_ps_resilient", falso)
    srv = SimpleNamespace(host="10.0.0.1", cred="fin")
    ok, msg = collector.nlb_action(srv, None, acao)
    return ok, msg, visto


def test_drain_espera_sair_do_convergindo(monkeypatch):
    ok, msg, visto = _rodar(monkeypatch, "drain", 1009)
    s = visto["script"]
    assert ok and "1007 -> 1009" in msg
    assert "$alvo = @(1005,1009)" in s and "while ($alvo -notcontains $depois" in s
    assert "__" not in s
    # a espera de ate 30 s cabe no prazo da chamada
    assert visto["budget"] >= 60


def test_rejoin_espera_o_convergido(monkeypatch):
    ok, _, visto = _rodar(monkeypatch, "rejoin", 1008)
    assert ok and "$alvo = @(1007,1008)" in visto["script"]


def test_drain_que_ficou_em_convergindo_continua_falha(monkeypatch):
    # se nem em 30 s saiu do 1006, algo esta errado: segue avisando
    ok, msg, _ = _rodar(monkeypatch, "drain", 1006)
    assert not ok and "-> 1006" in msg
