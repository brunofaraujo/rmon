"""Teto de memoria do JobRunner no Windows (deploy/windows): leitura e tela."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import collector, limite_jr
from app.config import ServerConfig, WinRMConfig

RAIZ = Path(__file__).resolve().parents[1]


def estado(**kw):
    e = {"servico": "Running", "inicio": "Auto", "ativo": True, "modo": "registrar", "teto_mb": 6144,
         "processos": 32, "sob_teto": 32, "falhas": 0, "ignorados": 0, "maior_mb": 512,
         "maior_pid": 4242, "ultimo_erro": None, "versao": "1.0", "idade_s": 12, "erro": None,
         "estouros_24h": 0, "estouros": []}
    e.update(kw)
    return e


# ------------------------------------------------------------ resumo

def test_sem_componente_e_nao_instalado():
    r = limite_jr.resumo(None)
    assert r["estado"] == "ausente" and r["cor"] == ""
    assert "Não instalado" in r["texto"]


def test_so_registra_e_azul_e_diz_que_nada_e_bloqueado():
    r = limite_jr.resumo(estado())
    assert r["estado"] == "registrando" and r["cor"] == "blue"
    assert "Só registra" in r["texto"] and "6 GB" in r["texto"] and "nada é bloqueado" in r["texto"]
    assert "32 de 32 processos de job sob o teto" in r["detalhes"]
    assert any("maior agora: 0,5 GB" in t for t in r["detalhes"])


def test_aplicando_e_verde():
    r = limite_jr.resumo(estado(modo="aplicar", teto_mb=3072))
    assert r["estado"] == "aplicando" and r["cor"] == "green"
    assert "Ligado" in r["texto"] and "3 GB" in r["texto"]


@pytest.mark.parametrize("svc", ["Stopped", "StartPending"])
def test_servico_fora_do_ar_e_ambar_e_diz_que_esta_sem_teto(svc):
    r = limite_jr.resumo(estado(servico=svc))
    assert r["estado"] == "parado" and r["cor"] == "amber"
    assert "sem teto" in r["texto"]


def test_estado_velho_e_sem_sinal():
    r = limite_jr.resumo(estado(idade_s=limite_jr.SEM_SINAL_S + 60))
    assert r["estado"] == "sem_sinal" and r["cor"] == "amber"
    assert "4 min" in r["texto"]


def test_estado_ilegivel():
    r = limite_jr.resumo(estado(erro="estado ilegível: arquivo não existe"))
    assert r["estado"] == "sem_sinal" and "arquivo não existe" in r["texto"]


def test_processo_fora_do_teto_deixa_ambar():
    r = limite_jr.resumo(estado(sob_teto=30, falhas=2))
    assert r["cor"] == "amber"
    assert "30 de 32 processos de job sob o teto" in r["detalhes"]
    assert any("2 não puderam" in t for t in r["detalhes"])


def test_estouro_no_modo_registrar_diz_que_teria_passado():
    e = estado(estouros_24h=1, estouros=[{"pid": 7760, "mb": 6200, "pico_mb": 9800, "modo": "registrar",
                                           "quando": "2026-10-19 10:36:00", "fim": "2026-10-19 10:40:00"}])
    r = limite_jr.resumo(e)
    assert any("1 processo de job teria passado do teto nas últimas 24 horas" in t for t in r["detalhes"])
    assert r["estouros"][0]["texto"].startswith("PID 7760")
    assert "pico de 9,6 GB" in r["estouros"][0]["texto"]


def test_estouro_no_modo_aplicar_diz_que_o_servidor_seguiu():
    e = estado(modo="aplicar", estouros_24h=2,
               estouros=[{"pid": 1, "mb": 6144, "modo": "aplicar", "quando": "2026-10-19 10:36:00"}])
    r = limite_jr.resumo(e)
    assert any("2 processos de job bateram no teto" in t and "servidor seguiu de pé" in t
               for t in r["detalhes"])


def test_estouros_viram_lista_mesmo_com_um_so():
    e = estado(estouros={"pid": 1, "mb": 7000, "modo": "registrar", "quando": "2026-10-19 10:36:00"})
    assert len(limite_jr.resumo(e)["estouros"]) == 1


# ------------------------------------------------------------ coleta

def test_script_da_coleta_le_o_estado_sem_alterar_nada():
    s = collector._build_script(ServerConfig(name="x", host="h"), {})
    assert "'RMonLimiteJobRunner'" in s and "estado.json" in s and "limite_jr = $limiteJr" in s
    bloco = s[s.index("$limiteJr = $null"):s.index("$limiteJr = [pscustomobject]$lj")]
    for verbo in ("Set-", "New-", "Remove-", "Start-", "Stop-", "Restart-"):
        assert verbo not in bloco


def _coleta_com(monkeypatch, saida: dict):
    monkeypatch.setattr(collector, "credential_for", lambda c: ("u", "p"))
    monkeypatch.setattr(collector, "_run_ps_resilient",
                        lambda *a, **k: SimpleNamespace(status_code=0, std_out=json.dumps(saida).encode(),
                                                        std_err=b""))
    monkeypatch.setattr(collector, "_check_app_health", lambda h: (None, None))
    return collector.collect_server(ServerConfig(name="x", host="h"), WinRMConfig(), {})


def test_coleta_traz_o_estado(monkeypatch):
    r = _coleta_com(monkeypatch, {"cpu": 1, "limite_jr": estado(estouros={"pid": 1, "mb": 7000})})
    assert r["limite_jr"]["modo"] == "registrar"
    assert r["limite_jr"]["estouros"] == [{"pid": 1, "mb": 7000}]


def test_coleta_sem_componente(monkeypatch):
    r = _coleta_com(monkeypatch, {"cpu": 1, "limite_jr": None})
    assert r["limite_jr"] is None


# ------------------------------------------------------------ o componente no repositorio

def test_componente_e_scripts_sao_ascii():
    """PowerShell 5.1 le .ps1 sem BOM como ANSI: acento quebra o script."""
    for nome in ("RMonLimiteJobRunner.cs", "instalar-limite-jobrunner.ps1",
                 "desinstalar-limite-jobrunner.ps1"):
        texto = (RAIZ / "deploy" / "windows" / nome).read_text(encoding="utf-8")
        assert texto.isascii(), nome


def test_modo_padrao_e_so_registrar():
    cs = (RAIZ / "deploy" / "windows" / "RMonLimiteJobRunner.cs").read_text(encoding="utf-8")
    ps = (RAIZ / "deploy" / "windows" / "instalar-limite-jobrunner.ps1").read_text(encoding="utf-8")
    assert 'public string Modo = "registrar";' in cs
    assert "[string]$Modo = 'registrar'" in ps
    # nomes que a coleta procura
    assert f'Nome = "{limite_jr.SERVICO}"' in cs and f"$Servico = '{limite_jr.SERVICO}'" in ps
    assert 'KILL_ON_JOB_CLOSE' not in cs  # parar o servico nunca pode matar JobRunner


def test_estado_gravado_com_o_servico_parado_nao_conta_como_protegendo():
    r = limite_jr.resumo(estado(ativo=False))
    assert r["estado"] == "sem_sinal" and r["cor"] == "amber"


# ------------------------------------------------------------ instalacao pela VM

@pytest.fixture
def remoto(monkeypatch):
    import importlib.util
    monkeypatch.setenv("RMON_HOME", str(RAIZ))
    spec = importlib.util.spec_from_file_location("limite_jobrunner_remoto",
                                                  RAIZ / "deploy" / "limite_jobrunner_remoto.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _embutidos(script: str) -> list[str]:
    import base64
    import re
    return [base64.b64decode(b).decode("utf-8") for b in re.findall(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)]


def test_instalar_leva_o_fonte_e_o_instalador(remoto):
    s = remoto.script_instalar("registrar", 0)
    fonte, instalador = _embutidos(s)
    assert "class Motor" in fonte and "== 6/6" in instalador
    assert "-Modo registrar -TetoMB 0 -FonteTexto $fonte" in s


@pytest.mark.parametrize("modo,teto", [("ligar", 0), ("aplicar", 100), ("aplicar'; rm -r C:\\", 0)])
def test_instalar_recusa_parametro_invalido(remoto, modo, teto):
    with pytest.raises(ValueError):
        remoto.script_instalar(modo, teto)


def test_modo_so_aceita_os_dois(remoto):
    assert "-Value 'aplicar'" in remoto.script_modo("aplicar")
    with pytest.raises(ValueError):
        remoto.script_modo("desligar")


def test_checar_usa_o_mesmo_bloco_da_coleta(remoto):
    assert collector._PS_LIMITE_JR.split("\n", 2)[1] in remoto._PS_CHECAR
    assert "__LJ_SVC__" not in remoto._PS_CHECAR
