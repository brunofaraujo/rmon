"""Elasticsearch como fonte opcional (app/elastic.py e o uso dele no scheduler).

As respostas falsas tem o formato das que o Elastic 9 devolveu de verdade para
estas consultas (host.name com dominio, nome do processo com ".exe", memoria em
bytes), com nomes de host trocados.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app import db, elastic, scheduler
from app.config import ServerConfig
from app.scheduler import DEFAULT_ALERTS

AGORA = time.time()
TH = dict(DEFAULT_ALERTS)


@pytest.fixture
def es(monkeypatch):
    """Elastic configurado, com as respostas por indice num dicionario."""
    monkeypatch.setenv("RMON_ES_URL", "https://elastic.exemplo:9200")
    monkeypatch.setenv("RMON_ES_API_KEY", "chave-de-teste")
    monkeypatch.setenv("RMON_ES_CA", "off")
    respostas: dict[str, object] = {}
    pedidos: list[tuple[str, dict]] = []

    class Resp:
        def __init__(self, dados, status=200):
            self.status_code, self._d, self.text = status, dados, str(dados)

        def json(self):
            return self._d

    def post(url, json=None, headers=None, timeout=None, verify=None):
        indice = url.split("/")[3]
        pedidos.append((indice, json))
        r = respostas.get(indice)
        if isinstance(r, Exception):
            raise r
        return Resp(r) if r is not None else Resp({"error": "x"}, 500)

    monkeypatch.setattr(elastic.httpx, "post", post)
    monkeypatch.setattr(elastic, "_saude", {"ok_ts": None, "erro": None, "erro_ts": None})
    cfg = elastic.settings_for({})
    return SimpleNamespace(cfg=cfg, respostas=respostas, pedidos=pedidos)


def vivos_resp(**hosts):
    return {"aggregations": {"h": {"buckets": [
        {"key": k, "ultimo": {"value": v * 1000}} for k, v in hosts.items()]}}}


# ------------------------------------------------------------ configuracao

def test_sem_url_ou_chave_fica_inerte(monkeypatch):
    monkeypatch.delenv("RMON_ES_URL", raising=False)
    monkeypatch.delenv("RMON_ES_API_KEY", raising=False)
    cfg = elastic.settings_for({})
    assert not elastic.ativo(cfg)
    assert elastic.vivos(cfg, ["a"]) == {}


def test_desligado_no_yaml(es):
    assert not elastic.ativo(elastic.settings_for({"elastic": {"enabled": False}}))


def test_ca_por_caminho_ou_desligada(monkeypatch):
    monkeypatch.setenv("RMON_ES_CA", "/opt/rmon/config/elastic-ca.crt")
    assert elastic.settings_for({})["verify"] == "/opt/rmon/config/elastic-ca.crt"
    monkeypatch.setenv("RMON_ES_CA", "off")
    assert elastic.settings_for({})["verify"] is False
    monkeypatch.setenv("RMON_ES_CA", "")
    assert elastic.settings_for({})["verify"] is True


def test_nome_do_host_no_elastic():
    assert elastic.nome_es(ServerConfig(name="app-34", host="x"), "SRV06N") == "srv06n"
    assert elastic.nome_es(ServerConfig(name="a", host="x", elastic_host="Outro.dom"), "SRV06N") == "outro.dom"
    assert elastic.nome_es(ServerConfig(name="a", host="x"), None) == ""


# ------------------------------------------------------------ consultas

def test_vivos_casa_host_com_dominio(es):
    es.respostas["metrics-system.cpu-*"] = vivos_resp(**{"hosta.dom.local": AGORA - 20,
                                                         "hostb.dom.local": AGORA - 3600,
                                                         "intruso.dom.local": AGORA})
    assert elastic.frescos(es.cfg, ["hosta", "hostb"], AGORA) == {"hosta"}
    filtro = es.pedidos[0][1]["query"]["bool"]["filter"][1]["bool"]["should"]
    assert {"prefix": {"host.name": "hosta."}} in filtro and {"term": {"host.name": "hosta"}} in filtro


def test_relogio_do_host_adiantado_conta_como_fresco(es):
    es.respostas["metrics-system.cpu-*"] = vivos_resp(**{"hosta.dom": AGORA + 45})
    assert elastic.frescos(es.cfg, ["hosta"], AGORA) == {"hosta"}


def test_rdp_canal_nao_coletado_e_nao_sei(es):
    es.respostas["logs-winlog.rdp_*"] = {"aggregations": {"h": {"buckets": [
        {"key": "hosta.dom", "auth_canal": {"doc_count": 40}, "sess_canal": {"doc_count": 80},
         "agora": {"auth": {"doc_count": 7}, "sess": {"doc_count": 0}}},
        {"key": "hostb.dom", "auth_canal": {"doc_count": 0}, "sess_canal": {"doc_count": 12},
         "agora": {"auth": {"doc_count": 0}, "sess": {"doc_count": 2}}}]}}}
    assert elastic.rdp(es.cfg, ["hosta", "hostb"], 15) == {
        "hosta": {"auth": 7, "sess": 0}, "hostb": {"auth": None, "sess": 2}}
    agora = es.pedidos[0][1]["aggs"]["h"]["aggs"]["agora"]["filter"]["range"]["@timestamp"]["gte"]
    assert agora == "now-15m"


def test_jobrunners_agora_no_formato_da_coleta(es):
    es.respostas["metrics-system.process-*"] = {"aggregations": {"pid": {"buckets": [
        {"key": 34508, "mem": {"value": 9.5 * 2**30}, "ts": {"value": AGORA * 1000}},
        {"key": 777, "mem": {"value": 300 * 2**20}, "ts": {"value": (AGORA - 60) * 1000}}]}}}
    jr = elastic.jobrunners_agora(es.cfg, "hosta", "RM.Host.JobRunner", "HOSTA")
    assert jr["fonte"] == "elastic" and jr["computer"] == "HOSTA"
    assert jr["top"] == [{"pid": 34508, "private_mb": 9728}, {"pid": 777, "private_mb": 300}]
    assert jr["ts"] == pytest.approx(AGORA)
    assert {"prefix": {"process.name": "RM.Host.JobRunner"}} in es.pedidos[0][1]["query"]["bool"]["filter"]


def test_serie_do_jobrunner_e_grafico(es):
    es.respostas["metrics-system.process-*"] = {"aggregations": {"t": {"buckets": [
        {"key": 1_760_360_000_000, "mem": {"value": 2 * 2**30},
         "dono": {"top": [{"metrics": {"process.pid": 34508}}]}},
        {"key": 1_760_361_800_000, "mem": {"value": 80 * 2**30},
         "dono": {"top": [{"metrics": {"process.pid": 34508}}]}},
        {"key": 1_760_363_600_000, "mem": {"value": None}, "dono": {"top": []}}]}}}
    serie = elastic.jobrunner_serie(es.cfg, "hosta", "RM.Host.JobRunner", 72)
    assert [p["mb"] for p in serie] == [2048, 81920] and serie[1]["pid"] == 34508
    assert es.pedidos[0][1]["aggs"]["t"]["date_histogram"]["fixed_interval"] == "30m"
    g = elastic.grafico(serie, 4, 8)
    assert g["pico_gb"] == 80.0 and g["pico_pid"] == 34508
    # linhas de aviso/critico abaixo do pico, dentro do desenho
    assert 0 < g["pico_y"] < g["critico_y"] < g["aviso_y"] < g["h"]


def test_grafico_mantem_os_limiares_na_tela_quando_esta_tudo_baixo():
    g = elastic.grafico([{"t": 0, "mb": 100, "pid": 1}, {"t": 60, "mb": 200, "pid": 1}], 4, 8)
    assert g["critico_y"] > 0  # 8 GB cabe no desenho mesmo com pico de 0,2 GB


def test_elastic_fora_vira_none_e_nao_levanta(es):
    es.respostas["metrics-system.process-*"] = ConnectionError("recusado")
    assert elastic.jobrunners_agora(es.cfg, "hosta", "RM.Host.JobRunner") is None
    assert elastic.jobrunner_serie(es.cfg, "hosta", "RM.Host.JobRunner") == []
    assert "recusado" in elastic.erro_atual()
    es.respostas["metrics-system.cpu-*"] = vivos_resp()
    elastic.vivos(es.cfg, ["hosta"])
    assert elastic.erro_atual() is None  # sucesso depois do erro


def test_http_de_erro_tambem_cai(es):
    assert elastic.rdp(es.cfg, ["hosta"], 15) == {}  # sem resposta cadastrada = HTTP 500
    assert "HTTP 500" in elastic.erro_atual()


# ------------------------------------------------------------ RDP no NLB

def test_com_as_duas_leituras_vale_a_mais_saudavel():
    """Ingestao atrasada mostra o 1149 antes do 21/25: a fonte secundaria nao
    pode fazer um no saudavel parecer "autentica sem abrir sessao"."""
    r = {"reachable": True, "nlb": {"status": 1008, "auth": 6, "sess": 5}}
    scheduler.aplicar_rdp(r, {"auth": 7, "sess": 0})
    n = r["nlb"]
    assert (n["auth"], n["sess"]) == (6, 5)
    assert (n["auth_winrm"], n["sess_winrm"], n["auth_elastic"], n["sess_elastic"]) == (6, 5, 7, 0)
    assert n["fonte"] == {"auth": "winrm", "sess": "winrm"}


def test_elastic_vale_quando_e_ele_que_ve_o_no_mais_saudavel():
    r = {"reachable": True, "nlb": {"status": 1008, "auth": 6, "sess": 2}}
    scheduler.aplicar_rdp(r, {"auth": 6, "sess": 4})
    assert (r["nlb"]["auth"], r["nlb"]["sess"]) == (6, 4)
    assert r["nlb"]["fonte"] == {"auth": "elastic", "sess": "elastic"}


def test_o_que_o_elastic_nao_sabe_fica_com_o_winrm():
    r = {"reachable": True, "nlb": {"status": 1008, "auth": 0, "sess": 2}}
    scheduler.aplicar_rdp(r, {"auth": None, "sess": 3})
    assert (r["nlb"]["auth"], r["nlb"]["sess"]) == (0, 3)
    assert r["nlb"]["fonte"] == {"auth": "winrm", "sess": "elastic"}
    r = {"reachable": True, "nlb": {"status": 1008, "auth": None, "sess": 1}}
    scheduler.aplicar_rdp(r, {"auth": 5, "sess": 1})  # so o Elastic sabe o 1149
    assert r["nlb"]["auth"] == 5 and r["nlb"]["fonte"]["auth"] == "elastic"
    r = {"reachable": True, "nlb": {"status": 1008, "auth": 2, "sess": 2}}
    scheduler.aplicar_rdp(r, None)  # Elastic fora: tudo do WinRM
    assert r["nlb"]["fonte"] == {"auth": "winrm", "sess": "winrm"} and r["nlb"]["auth"] == 2


def test_host_sem_winrm_ganha_so_as_contagens():
    r = {"reachable": False, "nlb": None}
    scheduler.aplicar_rdp(r, {"auth": 9, "sess": 0})
    assert r["nlb"]["auth"] == 9 and r["nlb"]["status"] is None
    r = {"reachable": False}
    scheduler.aplicar_rdp(r, None)
    assert "nlb" not in r


# ------------------------------------------------------------ JobRunner sem WinRM

def jr_es(gb):
    return {"top": [{"pid": 34508, "private_mb": gb * 1024}], "fonte": "elastic", "computer": "HOSTA"}


def test_sem_winrm_o_jobrunner_do_elastic_ainda_alerta():
    p = scheduler.problems({"reachable": False, "jobrunners": jr_es(9)}, TH, fail_streak=5)
    assert set(p) == {"DOWN", "jobrunner:34508", "jobrunner-crit:34508"}
    # antes do DOWN confirmar, o estouro ja aparece
    p = scheduler.problems({"reachable": False, "jobrunners": jr_es(5)}, TH, fail_streak=1)
    assert set(p) == {"jobrunner:34508"}


def test_sem_winrm_jobrunner_velho_da_coleta_nao_conta():
    velho = {"top": [{"pid": 1, "private_mb": 9 * 1024}]}  # sem fonte: lixo da coleta falha
    assert set(scheduler.problems({"reachable": False, "jobrunners": velho}, TH, 5)) == {"DOWN"}


@pytest.fixture
def ciclo(es, monkeypatch):
    monkeypatch.setattr(db, "computadores", lambda: {"app-34": "HOSTA", "app-190": "HOSTB"})
    servidores = [ServerConfig(name="app-34", host="10.0.0.34", nlb=True),
                  ServerConfig(name="app-190", host="10.0.0.190", nlb=True)]
    inv = SimpleNamespace(servers=servidores, defaults={})
    return SimpleNamespace(es=es, inv=inv, s=servidores)


def test_enriquecer_rdp_e_jobrunner_no_ciclo(ciclo):
    es = ciclo.es
    es.respostas["metrics-system.cpu-*"] = vivos_resp(**{"hosta.dom": AGORA, "hostb.dom": AGORA})
    es.respostas["logs-winlog.rdp_*"] = {"aggregations": {"h": {"buckets": [
        {"key": "hostb.dom", "auth_canal": {"doc_count": 9}, "sess_canal": {"doc_count": 9},
         "agora": {"auth": {"doc_count": 4}, "sess": {"doc_count": 4}}}]}}}
    es.respostas["metrics-system.process-*"] = {"aggregations": {"pid": {"buckets": [
        {"key": 34508, "mem": {"value": 9 * 2**30}, "ts": {"value": AGORA * 1000}}]}}}
    caido = {"reachable": False, "error": "timeout"}
    vivo = {"reachable": True, "nlb": {"status": 1008, "auth": 3, "sess": 3}}
    scheduler._enriquecer_elastic(ciclo.inv, [(ciclo.s[0], caido), (ciclo.s[1], vivo)])
    assert caido["jobrunners"]["fonte"] == "elastic" and caido["jobrunners"]["top"][0]["pid"] == 34508
    # auth: o menor (3, WinRM); sess: o maior (4, Elastic)
    assert (vivo["nlb"]["auth"], vivo["nlb"]["sess"]) == (3, 4)
    assert vivo["nlb"]["fonte"] == {"auth": "winrm", "sess": "elastic"}


def test_agente_parado_nao_vira_zero(ciclo):
    es = ciclo.es
    es.respostas["metrics-system.cpu-*"] = vivos_resp(**{"hostb.dom": AGORA - 3 * 3600})
    vivo = {"reachable": True, "nlb": {"status": 1008, "auth": 3, "sess": 0}}
    scheduler._enriquecer_elastic(ciclo.inv, [(ciclo.s[1], vivo)])
    assert vivo["nlb"]["auth"] == 3 and vivo["nlb"]["fonte"] == {"auth": "winrm", "sess": "winrm"}
    assert not any(i == "logs-winlog.rdp_*" for i, _ in es.pedidos)  # nem perguntou


def test_elastic_desligado_nao_consulta_nada(monkeypatch, ciclo):
    monkeypatch.delenv("RMON_ES_URL")
    vivo = {"reachable": True, "nlb": {"status": 1008, "auth": 3, "sess": 0}}
    scheduler._enriquecer_elastic(ciclo.inv, [(ciclo.s[1], vivo)])
    assert ciclo.es.pedidos == [] and "fonte" not in vivo["nlb"]


def test_resumo_lista_agente_parado(ciclo):
    ciclo.es.respostas["metrics-system.cpu-*"] = vivos_resp(**{"hostb.dom": AGORA - 26 * 3600})
    mudos = scheduler._elastic_mudos(ciclo.inv, AGORA)
    assert mudos[0] == "app-190 (desde " + time.strftime("%d/%m %H:%M", time.localtime(AGORA - 26 * 3600)) + ")"
    assert mudos[1] == "app-34 (nada em 7 dias)"
    texto = scheduler.texto_resumo({}, {}, {}, [], ["app-34"], AGORA, mudos)
    assert "Elastic sem dados de: app-190" in texto


def test_resumo_nao_acusa_agentes_quando_o_elastic_e_que_esta_fora(ciclo):
    ciclo.es.respostas["metrics-system.cpu-*"] = ConnectionError("fora")
    assert scheduler._elastic_mudos(ciclo.inv, AGORA) == []
