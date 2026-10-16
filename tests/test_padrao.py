"""Padronizacao da coleta entre os servidores (scheduler.padrao_cluster).

O caso que motivou: o .188 com SecurityLayer=0 (RDP nativo) nao grava o 1149,
os outros dois nos gravam - e o NLB passou meses sem conseguir julga-lo pelo
sinal "autentica sem abrir sessao" sem que ninguem soubesse.
"""
from __future__ import annotations

import time

from app import fontes, main, scheduler
from app.config import ServerConfig
from app.scheduler import DEFAULT_ALERTS
from tests.test_rotas import app_falso, coleta  # noqa: F401

AGORA = 1_760_000_000.0


def no(nome, sl=1, nla=0, u1149=12, status=1008, cluster="10.0.0.228", reach=True, **extra):
    s = ServerConfig(name=nome, host="x", nlb=True)
    n = {"status": status, "cluster": cluster, "auth": 3, "sess": 3,
         "security_layer": sl, "nla": nla, "ultimo_1149_min": u1149, **extra}
    return s, {"reachable": reach, "nlb": n}


def test_o_caso_do_188():
    p = scheduler.padrao_cluster([no("app-34"), no("app-188", sl=0, u1149=None), no("app-190", u1149=40)])
    assert set(p) == {"app-188"}
    texto = p["app-188"]["PADRAO:1149"]
    assert "app-34, app-190 grava" in texto and "SecurityLayer do RDP = 0 (RDP nativo, sem TLS)" in texto
    # a causa vai junto no mesmo aviso, nao em dois
    assert "PADRAO:security_layer" not in p["app-188"]


def test_188_ja_corrigido_sem_teste_da_porta():
    # SecurityLayer ja trocado para 1 no registro, 1149 ainda nao apareceu
    p = scheduler.padrao_cluster([no("app-34"), no("app-188", u1149=None), no("app-190", u1149=40)])
    texto = p["app-188"]["PADRAO:1149"]
    # sem o teste da porta 3389 nao da para prometer que some sozinho
    assert "Causa" not in texto
    assert "registro ja e 1 (Negociar), igual aos outros nos" in texto
    assert "ainda nao conseguiu testar" in texto and "some sozinho" not in texto


def test_188_registro_trocado_mas_rdp_nao_releu():
    # 16/10: SecurityLayer=1 no registro desde a vespera, 248 conexoes e nenhum
    # 1149 - o servico de RDP (no ar desde 08/10) seguia recusando TLS
    p = scheduler.padrao_cluster([no("app-34", rdp_tls=True),
                                  no("app-188", u1149=None, rdp_tls=False, rdp_servico_min=8 * 1440 + 30),
                                  no("app-190", u1149=40, rdp_tls=True)])
    assert set(p) == {"app-188"} and set(p["app-188"]) == {"PADRAO:1149"}
    texto = p["app-188"]["PADRAO:1149"]
    assert "Causa: o registro ja diz SecurityLayer do RDP = 1 (Negociar)" in texto
    assert "ainda recusa TLS" in texto and "no ar ha 8 dia(s)" in texto and "nao releu" in texto
    assert "some sozinho" not in texto


def test_registro_trocado_sem_releitura_acusa_mesmo_gravando_1149():
    p = scheduler.padrao_cluster([no("a", rdp_tls=True), no("b", rdp_tls=False), no("c")])
    assert set(p) == {"b"} and "ainda recusa TLS" in p["b"]["PADRAO:security_layer"]


def test_corrigido_e_em_vigor_aguarda_o_primeiro_logon():
    p = scheduler.padrao_cluster([no("app-34"), no("app-188", u1149=None, rdp_tls=True), no("app-190")])
    texto = p["app-188"]["PADRAO:1149"]
    assert "ja aceita TLS" in texto and "primeiro logon RDP novo" in texto


def test_configuracao_diferente_mesmo_gravando():
    p = scheduler.padrao_cluster([no("a"), no("b", nla=1), no("c")])
    assert set(p) == {"b"} and "NLA do RDP = 1 (ligada)" in p["b"]["PADRAO:nla"]


def test_fim_de_semana_sem_logon_nao_acusa_ninguem():
    p = scheduler.padrao_cluster([no("a", u1149=3000), no("b", u1149=None), no("c", u1149=4000)])
    assert p == {}


def test_no_drenado_nao_e_cobrado_pelo_1149():
    p = scheduler.padrao_cluster([no("a"), no("b", status=1009, u1149=None), no("c")])
    assert p == {}


def test_coleta_antiga_sem_o_campo_nao_acusa():
    s, r = no("b")
    for campo in ("ultimo_1149_min", "security_layer", "nla"):
        r["nlb"].pop(campo)
    assert scheduler.padrao_cluster([no("a"), (s, r), no("c")]) == {}


def test_empate_nao_tem_padrao_para_cobrar():
    assert scheduler.padrao_cluster([no("a", sl=0), no("b", sl=1)]) == {}


def test_clusters_diferentes_nao_se_comparam():
    p = scheduler.padrao_cluster([no("a", sl=0, cluster="x"), no("b", sl=1, cluster="y"), no("c", sl=1, cluster="y")])
    assert p == {}


def test_no_sem_contato_fica_de_fora():
    p = scheduler.padrao_cluster([no("a"), no("b", sl=0, reach=False), no("c")])
    assert p == {}


def test_agente_do_elastic_parado_enquanto_os_outros_mandam():
    srv = [ServerConfig(name=n, host="x") for n in ("a", "b", "c")]
    coletas = [(s, {"reachable": True}) for s in srv]
    nomes = {"a": "ha", "b": "hb", "c": "hc"}
    p = scheduler.padrao_cluster(coletas, {"ha": AGORA - 5, "hb": AGORA - 3600}, nomes, True, AGORA)
    assert set(p) == {"b", "c"}
    assert "desde" in p["b"]["PADRAO:elastic"] and "ultimos 15 min" in p["c"]["PADRAO:elastic"]
    # Elastic inteiro calado (ou desligado): ninguem e acusado
    assert scheduler.padrao_cluster(coletas, {"hb": AGORA - 3600}, nomes, True, AGORA) == {}
    assert scheduler.padrao_cluster(coletas, {"ha": AGORA}, nomes, False, AGORA) == {}


def test_divergencia_vira_aviso_que_notifica():
    r = {"reachable": True, "padrao": {"PADRAO:1149": "nao grava o 1149"}}
    p = scheduler.problems(r, dict(DEFAULT_ALERTS))
    assert p == {"PADRAO:1149": "nao grava o 1149"}
    assert not scheduler.critico("PADRAO:1149") and scheduler.notifica("PADRAO:1149")


def test_pagina_e_faixa_mostram_quem_esta_fora(app_falso):  # noqa: F811
    srv = main.STATE["inv"].servers[0]
    srv.nlb = True
    app_falso.estado.coletas = [coleta(nlb={"status": 1008, "cluster": "vip", "security_layer": 0, "nla": 0,
                                            "ultimo_1149_min": None},
                                       padrao={"PADRAO:1149": "nao grava o evento 1149 (RDP autenticado)"})]
    fontes.registrar([(srv, coleta())], {}, {}, False, time.time())
    fontes.registrar_padrao({"app-34": {"PADRAO:1149": "x"}})
    app_falso.entrar()
    html = app_falso.c.get("/fontes").text
    assert "1 divergencia(s) do padrao" in html and "Fora do padrao: evento 1149" in html
    assert "<b>nenhum no log</b>" in html and "0 <span class=\"small\">(RDP nativo, sem TLS)</span>" in html
    assert "fora do padrao</span>" in html
    html = app_falso.c.get("/").text
    assert "1 servidor(es) fora do padrao de coleta" in html and "(app-34)" in html


def test_pagina_mostra_tls_recusado(app_falso):  # noqa: F811
    srv = main.STATE["inv"].servers[0]
    srv.nlb = True
    app_falso.estado.coletas = [coleta(nlb={"status": 1008, "cluster": "vip", "security_layer": 1, "nla": 0,
                                            "ultimo_1149_min": None, "rdp_tls": False},
                                       padrao={"PADRAO:1149": "nao grava o evento 1149"})]
    app_falso.entrar()
    html = app_falso.c.get("/fontes").text
    assert "TLS em vigor (porta 3389)" in html
    assert "<b>recusa</b> <span class=\"small\">(servico de RDP nao releu o registro)</span>" in html


class _Sock:
    def __init__(self, resposta):
        self.resposta, self.enviado = resposta, b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        pass

    def sendall(self, b):
        self.enviado += b

    def recv(self, n):
        return self.resposta


def _resposta(tipo, valor):
    import struct
    return struct.pack(">BBH", 3, 0, 19) + bytes([14, 0xD0, 0, 0, 0x12, 0x34, 0]) + struct.pack("<BBHI", tipo, 0, 8, valor)


def test_teste_da_porta_3389(monkeypatch):
    from app import collector
    for resp, esperado in ((_resposta(2, 1), True), (_resposta(3, 2), False), (b"", None)):
        sock = _Sock(resp)
        monkeypatch.setattr(collector.socket, "create_connection", lambda *a, **k: sock)
        assert collector.rdp_aceita_tls("h") is esperado
        assert sock.enviado == collector._RDP_NEG_TLS

    def falha(*a, **k):
        raise OSError("recusado")
    monkeypatch.setattr(collector.socket, "create_connection", falha)
    assert collector.rdp_aceita_tls("h") is None


def test_teste_da_porta_tem_cache_e_refaz_se_o_registro_mudar(monkeypatch):
    from app import collector
    chamadas = []
    monkeypatch.setattr(collector, "_rdp_tls_cache", {})
    monkeypatch.setattr(collector, "rdp_aceita_tls", lambda h: chamadas.append(h) or False)
    assert collector.rdp_tls_em_vigor("h", 1, agora=AGORA) is False
    collector.rdp_tls_em_vigor("h", 1, agora=AGORA + 60)
    assert len(chamadas) == 1
    collector.rdp_tls_em_vigor("h", 0, agora=AGORA + 120)          # registro mudou
    collector.rdp_tls_em_vigor("h", 0, agora=AGORA + 120 + collector.RDP_TLS_TTL_S)  # venceu
    assert len(chamadas) == 3
