"""Regeracao do broker com a conta de administracao depois do instalador.

Casos reais de 17-18/11/2026: com o RM.Host.Service na NetworkService o
`_BrokerCustom.dat` sai sem a pasta Custom (55.063 bytes, 8 de 8 no .190); com
a conta de administracao ele sai completo (.188 579.821, .190 580.706, .34
579.821). E a CONTA, nao o executavel. Sem "Fazer logon como servico" o Windows
recusa a partida (evento 7041). E a senha nao pode ir no texto do script: o log
de script do PowerShell (4104) gravou a senha nos tres servidores quando foi.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from app import collector, execution, scheduler

SENHA = "S3nh@-que-nao-pode-vazar"


def _acoes(**extra):
    base = {"id": "inno-silencioso", "label": "Inno", "args": "/VERYSILENT",
            "extensions": [".exe"], "timeout_sec": 900, "stop_services": ["RM.Host*"]}
    return execution.actions_for({"execution": {"actions": [{**base, **extra}]}})


def _acao(**extra):
    return _acoes(broker_regen=True, **extra)["inno-silencioso"]


# ---------- configuracao da acao ----------
def test_regeracao_desligada_por_padrao_e_com_padroes_do_sge():
    assert _acoes()["inno-silencioso"]["broker_regen"] is False
    a = _acao()
    assert a["broker_regen"] is True
    assert a["broker_service"] == "RM.Host.Service"
    assert a["broker_file"] == "_BrokerCustom.dat"
    assert a["broker_wait_sec"] == execution.BROKER_ESPERA_PADRAO
    assert a["broker_cred"] == ""


def test_regeracao_sem_stop_services_fica_desligada():
    acoes = execution.actions_for({"execution": {"actions": [{
        "id": "x-y", "args": "/S", "extensions": [".exe"], "broker_regen": True}]}})
    assert acoes["x-y"]["broker_regen"] is False


def test_valores_perigosos_descartam_a_acao():
    # curinga trocaria a conta de varios servicos; injecao viraria comando
    assert _acoes(broker_regen=True, broker_service="RM.Host*") == {}
    assert _acoes(broker_regen=True, broker_service="x'; Remove-Item C:\\ #") == {}
    assert _acoes(broker_regen=True, broker_file="..\\..\\Windows\\win.ini") == {}
    assert _acoes(broker_regen=True, broker_file="broker.exe") == {}
    assert _acoes(broker_regen=True, broker_cred="FIN; drop") == {}


def test_espera_limitada():
    assert _acao(broker_wait_sec=5)["broker_wait_sec"] == 60
    assert _acao(broker_wait_sec=99999)["broker_wait_sec"] == 900


def test_prazos_cobrem_a_regeracao():
    a = _acao(stop_wait_sec=120, broker_wait_sec=300)
    host = (2 * (execution.PARAR_SEC + 120) + execution.BROKER_SUBIR_SEC + 300
            + execution.PARAR_SEC + execution.RELIGAR_SEC)
    assert execution.prazo_broker(a) == host + execution.FOLGA_SEC
    assert execution.prazo_tarefa(a) == execution.prazo_total(a) + execution.prazo_broker(a)
    sem = _acoes()["inno-silencioso"]
    assert execution.prazo_broker(sem) == 0
    assert execution.prazo_tarefa(sem) == execution.prazo_total(sem)


# ---------- script no host ----------
def test_script_nao_leva_a_senha_e_troca_a_conta_pelo_metodo_wmi():
    s = execution._script_broker(_acao(), "sistemafiep\\admin")
    assert SENHA not in s
    assert "$senha = $env:RMON_BROKER_SENHA" in s
    assert "$conta = 'sistemafiep\\admin'" in s
    # cmdlet com a senha nos parametros iria para o log de modulo
    assert "Invoke-CimMethod" not in s and "StartPassword" not in s
    assert "[wmi](" in s and ".Change($null" in s
    assert "__" not in s.replace("__init__", "")     # nenhum marcador sobrou


def test_script_concede_logon_como_servico_antes_de_trocar():
    s = execution._script_broker(_acao(), "sistemafiep\\admin")
    assert "SeServiceLogonRight" in s and "secedit.exe /configure" in s
    assert s.index("SeServiceLogonRight") < s.index("Trocar-Conta $conta $senha")


def test_script_so_mexe_em_servico_de_conta_embutida():
    s = execution._script_broker(_acao(), "sistemafiep\\admin")
    assert "'NT AUTHORITY\\NetworkService'" in s
    assert "$embutidas -notcontains $contaOriginal" in s
    assert s.index("$embutidas -notcontains") < s.index("Get-ServicosParar $parar")


def test_script_sempre_devolve_a_conta_antes_de_religar():
    s = execution._script_broker(_acao(), "sistemafiep\\admin")
    fim = s.split("} finally {", 1)[1]
    # o bloco final devolve a conta original e so depois religa os servicos
    assert fim.index("Trocar-Conta $contaOriginal ''") < fim.index("$s.Start()")
    # e religa mesmo se os processos nao sairam (o .34 ficou parado em 18/11)
    assert "foreach ($n in $religar)" in fim


def test_script_guarda_o_broker_anterior_e_devolve_se_nao_gerar():
    s = execution._script_broker(_acao(), "sistemafiep\\admin")
    assert ".antes-regen-" in s and "Select-Object -Skip 3" in s
    assert "broker anterior devolvido" in s.split("} finally {", 1)[1]


# ---------- chamada ----------
def _servidor():
    return SimpleNamespace(name="fin-sge-app-188", host="10.10.10.188", cred="fin")


def test_regerar_manda_a_senha_por_variavel_de_ambiente(monkeypatch):
    visto = {}
    monkeypatch.setattr(execution, "credential_for", lambda p: ("sistemafiep\\admin", SENHA))

    def falso(host, wc, user, pw, script, attempts=2, budget_sec=None, env=None):
        visto.update(script=script, env=env, attempts=attempts, budget=budget_sec)
        saida = {"passos": ["parado: RM.Host.Service", "gerado com a conta de administração: 579821 bytes"],
                 "erro": "", "antes": 55063, "gerado": 579821, "final": 579821,
                 "conta_original": "NT AUTHORITY\\NetworkService",
                 "arquivo": "C:\\totvs\\CorporeRM\\RM.Net\\_BrokerCustom.dat"}
        return SimpleNamespace(std_out=json.dumps(saida).encode(), std_err=b"", status_code=0)

    monkeypatch.setattr(execution, "_run_ps_resilient", falso)
    a = _acao()
    r = execution.regerar_broker(_servidor(), None, a)
    assert r["ok"] and r["gerado"] == 579821 and r["antes"] == 55063
    assert visto["env"] == {"RMON_BROKER_SENHA": SENHA}
    assert SENHA not in visto["script"]
    assert visto["attempts"] == 1          # repetir pararia o RM.Host de novo
    assert visto["budget"] == execution.prazo_broker(a)


def test_regerar_com_erro_no_host_nao_e_ok(monkeypatch):
    monkeypatch.setattr(execution, "credential_for", lambda p: ("u", "p"))
    saida = {"passos": ["religado: RM.Host.Service (Running)"], "erro": "depois de 180 s ainda havia processo",
             "antes": 579821, "gerado": -1, "final": 579821}
    monkeypatch.setattr(execution, "_run_ps_resilient", lambda *a, **k: SimpleNamespace(
        std_out=json.dumps(saida).encode(), std_err=b"", status_code=0))
    r = execution.regerar_broker(_servidor(), None, _acao())
    assert not r["ok"] and "processo" in r["erro"]


def test_regerar_sem_credencial_nao_chama_o_host(monkeypatch):
    monkeypatch.setattr(execution, "credential_for", lambda p: ("", ""))
    monkeypatch.setattr(execution, "_run_ps_resilient",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("não devia chamar")))
    r = execution.regerar_broker(_servidor(), None, _acao(broker_cred="admin"))
    assert not r["ok"] and "admin" in r["erro"]


# ---------- veredito e texto ----------
def test_veredito_contra_a_referencia_do_host():
    ok = {"ok": True, "final": 579821, "erro": None}
    assert execution.veredito_broker(ok, 580706, 60)["completo"] is True
    curto = {"ok": True, "final": 55063, "erro": None}
    assert execution.veredito_broker(curto, 580706, 60)["completo"] is False
    # sem historico, vale ter gerado
    assert execution.veredito_broker(ok, 0, 60)["completo"] is True
    assert execution.veredito_broker({"ok": False, "final": -1, "erro": "x"}, 580706, 60)["completo"] is False


def test_texto_do_resultado():
    b = execution.veredito_broker({"ok": True, "final": 579821, "erro": None}, 580706, 60)
    assert execution.texto_broker(b) == ("broker regerado com a conta de administração: "
                                         "579.821 bytes, completo (referência 580.706)")
    b = execution.veredito_broker({"ok": True, "final": 55063, "erro": None}, 580706, 60)
    assert execution.texto_broker(b).startswith("broker INCOMPLETO: 55.063 bytes")
    b = execution.veredito_broker({"ok": False, "final": -1, "erro": "evento 7041"}, 0, 60)
    assert execution.texto_broker(b) == "broker NÃO regerado: evento 7041"


def test_scheduler_mede_contra_o_maior_broker_do_host(monkeypatch):
    monkeypatch.setattr(scheduler, "_limiares", lambda inv: {"broker_min_pct": 60, "broker_history_days": 30})
    monkeypatch.setattr(scheduler, "broker_reference",
                        lambda dias: {"fin-sge-app-188": {"_BrokerCustom.dat": 579821}})
    b = {"ok": True, "final": 55063, "erro": None,
         "arquivo": "C:\\totvs\\CorporeRM\\RM.Net\\_BrokerCustom.dat"}
    v = scheduler._veredito_broker_da_tarefa("fin-sge-app-188", b, None)
    assert v["referencia"] == 579821 and v["completo"] is False
    assert scheduler._veredito_broker_da_tarefa("fin-sge-app-188", None, None) is None


# ---------- execucao: instalou, entao regera ----------
def test_execute_regera_o_broker_depois_de_instalar(monkeypatch, tmp_path):
    (tmp_path / "p.exe").write_bytes(b"MZ")
    settings = SimpleNamespace(packages_dir=str(tmp_path))
    defaults = {"execution": {"enabled": True, "hosts": ["fin-sge-app-188"], "actions": [{
        "id": "inno-silencioso", "args": "/VERYSILENT", "extensions": [".exe"],
        "stop_services": ["RM.Host*"], "broker_regen": True}]}}
    monkeypatch.setattr(execution, "credential_for", lambda p: ("u", "p"))
    chamadas = []

    def falso(host, wc, user, pw, script, attempts=2, budget_sec=None, env=None):
        chamadas.append(env)
        if env:   # a regeracao
            saida = {"passos": ["gerado com a conta de administração: 579821 bytes"], "erro": "",
                     "antes": 55063, "gerado": 579821, "final": 579821}
        else:     # o instalador
            saida = {"passos": ["sha256 conferido"], "saida": "", "codigo": 0,
                     "registro": ["atualizado: VTEX 12.1.2602.012 -> 12.1.2602.013 | desinstalar com: x"]}
        return SimpleNamespace(std_out=json.dumps(saida).encode(), std_err=b"", status_code=0)

    monkeypatch.setattr(execution, "_run_ps_resilient", falso)
    task = {"server": "fin-sge-app-188", "action": "inno-silencioso", "arquivo": "p.exe"}
    r = execution.execute(task, _servidor(), None, defaults, settings, "http://x/s")
    assert r["ok"] and r["broker"]["gerado"] == 579821
    assert "— broker —" in r["output"]
    assert chamadas == [None, {"RMON_BROKER_SENHA": "p"}]


def test_execute_nao_regera_se_o_instalador_falhou(monkeypatch, tmp_path):
    (tmp_path / "p.exe").write_bytes(b"MZ")
    settings = SimpleNamespace(packages_dir=str(tmp_path))
    defaults = {"execution": {"enabled": True, "hosts": ["fin-sge-app-188"], "actions": [{
        "id": "inno-silencioso", "args": "/VERYSILENT", "extensions": [".exe"],
        "stop_services": ["RM.Host*"], "broker_regen": True}]}}
    monkeypatch.setattr(execution, "credential_for", lambda p: ("u", "p"))
    chamadas = []

    def falso(*a, env=None, **k):
        chamadas.append(env)
        saida = {"passos": [], "saida": "abortou", "codigo": 5, "registro": []}
        return SimpleNamespace(std_out=json.dumps(saida).encode(), std_err=b"", status_code=0)

    monkeypatch.setattr(execution, "_run_ps_resilient", falso)
    task = {"server": "fin-sge-app-188", "action": "inno-silencioso", "arquivo": "p.exe"}
    r = execution.execute(task, _servidor(), None, defaults, settings, "http://x/s")
    assert not r["ok"] and chamadas == [None]


# ---------- acentos no host ----------
def test_bootstrap_le_o_script_em_utf8():
    # [Console]::In usa a pagina de codigo do console (cp850): o aviso de
    # manutencao saiu "Atualiza├º├úo" na tela dos usuarios em 18/11/2026
    b = base64.b64decode(collector._BOOTSTRAP).decode("utf-16-le")
    assert "[Console]::In.ReadToEnd()" not in b
    assert "OpenStandardInput()" in b and "UTF8Encoding $false" in b


# ---------- tela ----------
def test_tela_de_tarefas_mostra_o_broker(monkeypatch):
    from fastapi.testclient import TestClient

    from app import db, main
    from app.config import ServerConfig, WinRMConfig
    from app.security import hash_password

    usuario = {"username": "admin", "role": "admin", "active": True,
               "password_hash": hash_password("senha-de-teste-123")}
    base = {"created_at": 0, "server": "app-34", "produto": "VTEX", "version": "12.1.2602.013",
            "action": "inno-silencioso", "mode": "real", "status": "ok", "comando": "", "error": None,
            "output": "instalado", "checks": None}
    tarefas = [
        {**base, "id": 3, "broker": execution.veredito_broker(
            {"ok": True, "final": 55063, "erro": None}, 579821, 60)},
        {**base, "id": 2, "broker": execution.veredito_broker(
            {"ok": True, "final": 579821, "erro": None}, 579821, 60)},
        {**base, "id": 1, "broker": None},
    ]
    monkeypatch.setattr(db, "_conn", lambda: (_ for _ in ()).throw(AssertionError("banco")))
    monkeypatch.setattr(db, "get_user", lambda u: usuario if u == "admin" else None)
    monkeypatch.setattr(db, "touch_login", lambda u: None)
    monkeypatch.setattr(db, "audit", lambda *a, **k: None)
    monkeypatch.setattr(db, "list_tasks", lambda n=100: tarefas)
    monkeypatch.setattr(db, "list_catalog", lambda: [])
    acoes = {"execution": {"actions": [{"id": "inno-silencioso", "args": "/VERYSILENT",
                                        "extensions": [".exe"], "stop_services": ["RM.Host*"],
                                        "broker_regen": True}]}}
    inv = SimpleNamespace(servers=[ServerConfig(name="app-34", host="10.0.0.34")],
                          winrm=WinRMConfig(), defaults=acoes, poll_interval_seconds=60,
                          environments={})
    monkeypatch.setitem(main.STATE, "inv", inv)
    c = TestClient(main.app)
    assert c.post("/login", data={"username": "admin", "password": "senha-de-teste-123"},
                  follow_redirects=False).status_code == 302
    html = c.get("/pacotes/tarefas").text
    assert "broker incompleto" in html and "broker completo" in html
    assert "broker INCOMPLETO: 55.063 bytes (referência 579.821)" in html
