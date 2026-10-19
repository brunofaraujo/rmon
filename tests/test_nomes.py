"""Nome do servidor na tela (app/nomes.py): label do YAML, senao o COMPUTERNAME."""
from __future__ import annotations

import pytest

from app import db, nomes
from app.config import Inventory, ServerConfig, WinRMConfig


@pytest.fixture
def inv(monkeypatch):
    servidores = [
        ServerConfig(name="fin-sge-app-34", host="10.10.10.34", env="fin", role="app"),
        ServerConfig(name="fin-sge-jobs-222", host="10.10.10.222", env="fin", role="jobs",
                     label="Jobs do SGE"),
        ServerConfig(name="sem-coleta", host="10.10.10.9"),
    ]
    inventario = Inventory(60, WinRMConfig(), {}, servidores, {"fin": "SGE/Financeiro"})
    monkeypatch.setattr(db, "computadores", lambda: {"fin-sge-app-34": "srv06n",
                                                     "fin-sge-jobs-222": "SRV11"})
    nomes.configurar(inventario)
    yield inventario
    nomes.configurar(None)


def test_sem_label_usa_o_nome_do_computador(inv):
    assert nomes.titulo("fin-sge-app-34") == "SRV06N"


def test_label_do_yaml_tem_precedencia(inv):
    assert nomes.titulo("fin-sge-jobs-222") == "Jobs do SGE"


def test_sem_coleta_cai_na_chave(inv):
    assert nomes.titulo("sem-coleta") == "sem-coleta"


def test_descricao_por_extenso(inv):
    assert nomes.descricao("fin-sge-app-34") == "Servidor de aplicação · SGE/Financeiro"
    assert nomes.descricao("fin-sge-app-34", curta=True) == "Aplicação · SGE/Financeiro"
    assert nomes.descricao("sem-coleta") == ""


def test_mensagem_leva_nome_e_ip(inv):
    assert nomes.com_ip("fin-sge-app-34") == "SRV06N (10.10.10.34)"


def test_confirmacao_aceita_o_nome_da_tela_ou_a_chave(inv):
    assert nomes.confere("srv06n", "fin-sge-app-34")
    assert nomes.confere(" fin-sge-app-34 ", "fin-sge-app-34")
    assert not nomes.confere("", "fin-sge-app-34")
    assert not nomes.confere("SRV11", "fin-sge-app-34")


def test_banco_fora_nao_derruba_a_tela(inv, monkeypatch):
    def falha():
        raise RuntimeError("sem banco")
    monkeypatch.setattr(db, "computadores", falha)
    nomes.configurar(inv)
    assert nomes.titulo("fin-sge-app-34") == "fin-sge-app-34"


def test_sem_inventario_devolve_a_chave():
    nomes.configurar(None)
    assert nomes.titulo("qualquer") == "qualquer"
    assert nomes.descricao("qualquer") == ""
