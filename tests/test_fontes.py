"""Fontes de dados a vista (app/fontes.py): o que a tela diz sobre WinRM x Elastic."""
from __future__ import annotations

import time

import pytest

from app import fontes, main
from app.config import ServerConfig
from tests.test_rotas import app_falso, coleta  # noqa: F401 - fixture e fabrica

AGORA = 1_760_000_000.0


@pytest.fixture(autouse=True)
def limpo(monkeypatch):
    monkeypatch.setattr(fontes, "_estado", {"ts": None, "servidores": {}})


def ciclo_exemplo():
    a = ServerConfig(name="app-34", host="x", nlb=True)
    b = ServerConfig(name="app-188", host="y", nlb=True)
    c = ServerConfig(name="jobs-222", host="z")
    coletas = [
        (a, {"reachable": True, "jobrunners": {"top": []},
             "nlb": {"auth": 3, "sess": 3, "fonte": {"auth": "elastic", "sess": "winrm"}}}),
        (b, {"reachable": True, "nlb": {"auth": 0, "sess": 2}}),  # Elastic desligado: sem `fonte`
        (c, {"reachable": False, "error": "WinRMOperationTimeoutError",
             "jobrunners": {"top": [{"pid": 1, "private_mb": 9000}], "fonte": "elastic"}}),
    ]
    fontes.registrar(coletas, {"app-34": "srv06n", "app-188": "win2016", "jobs-222": "srv11"},
                     {"srv06n": AGORA - 5, "srv11": AGORA - 5}, es_ativo=True, agora=AGORA)


def test_registro_diz_de_onde_veio_cada_dado():
    ciclo_exemplo()
    a, b, c = (fontes.do_servidor(n) for n in ("app-34", "app-188", "jobs-222"))
    assert (a["metricas"], a["jobrunner"], a["nlb_auth"], a["nlb_sess"]) == ("winrm", "winrm", "elastic", "winrm")
    assert (b["nlb_auth"], b["nlb_sess"]) == ("winrm", "winrm") and b["elastic_visto"] is None
    assert c["winrm_ok"] is False and c["metricas"] is None and c["jobrunner"] == "elastic"
    assert c["elastic_host"] == "srv11" and c["elastic_visto"] == AGORA - 5


def test_resumo_do_cabecalho():
    assert fontes.resumo({"ativo": True})["coletado"] is False
    ciclo_exemplo()
    r = fontes.resumo({"ativo": True, "ok_ts": AGORA - 12, "erro": None}, agora=AGORA)
    assert (r["winrm_ok"], r["total"], r["winrm_estado"]) == (2, 3, "parcial")
    assert (r["es_estado"], r["es_texto"]) == ("ok", "há 12s")
    assert fontes.resumo({"ativo": True, "ok_ts": AGORA, "erro": "HTTP 401"})["es_estado"] == "erro"
    assert fontes.resumo({"ativo": False})["es_estado"] == "off"


def test_cabecalho_mostra_as_fontes_em_toda_tela(app_falso):  # noqa: F811
    app_falso.entrar()
    html = app_falso.c.get("/").text
    assert "aguardando a primeira coleta" in html
    fontes.registrar([(main.STATE["inv"].servers[0], coleta())], {}, {}, es_ativo=False)
    for rota in ("/", "/ocorrencias", "/admin", "/server/app-34"):
        html = app_falso.c.get(rota).text
        assert 'class="src-strip" href="/fontes"' in html, rota
        assert '<span class="src-tag winrm">WinRM</span> <b>1 de 1</b>' in html, rota
        assert "<b>desligado</b>" in html, rota


def test_pagina_fontes(app_falso, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RMON_ES_URL", "https://elastic.exemplo:9200")
    monkeypatch.setenv("RMON_ES_API_KEY", "chave-de-teste")
    fontes.registrar([(main.STATE["inv"].servers[0], coleta(reachable=False, error="timeout",
                       jobrunners={"top": [{"pid": 4242, "private_mb": 9000}], "fonte": "elastic"}))],
                     {"app-34": "hosta"}, {"hosta": time.time() - 30}, es_ativo=True)
    app_falso.entrar()
    html = app_falso.c.get("/fontes").text
    assert "Origem dos dados" in html and "Qual fonte fornece cada dado" in html
    assert "sem resposta" in html and '<span class="src-tag elastic">Elastic</span></td>' in html
    assert '<span class="mono small">hosta</span> <span class="faint small">há 3' in html


def test_viewer_nao_ve_fontes(app_falso):  # noqa: F811
    app_falso.entrar("tv")
    assert app_falso.c.get("/fontes", follow_redirects=False).status_code in (302, 303, 403)


def test_tela_do_servidor_diz_as_fontes(app_falso):  # noqa: F811
    fontes.registrar([(main.STATE["inv"].servers[0], coleta())], {"app-34": "hosta"},
                     {"hosta": time.time()}, es_ativo=True)
    app_falso.entrar()
    html = app_falso.c.get("/server/app-34").text
    assert "Origem dos dados desta tela:" in html and "processador, memória, serviços, discos e broker, processos de job agora" in html
    assert 'Processos de job do RM (JobRunner) <span class="src-tag winrm">WinRM</span>' in html


def test_css_do_painel_tem_selo_de_deploy(app_falso):  # noqa: F811
    """Sem o selo, o navegador seguia com o style.css antigo em cache e as
    etiquetas de fonte apareciam sem cor em producao."""
    app_falso.entrar()
    for rota in ("/", "/fontes", "/login"):
        html = app_falso.c.get(rota).text
        assert f'href="/static/style.css?v={main.templates.env.globals["assets"]}"' in html, rota
