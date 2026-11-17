"""Instalador com parada de servicos (app/execution.py, stop_services).

O caso real e o .223 em 17/11/2026: o instalador do TOTVS_CES_RM_ProcessoSeletivoVTEX
parou o RM.Host sozinho, mas comecou a copiar 3 s depois de parar o Cleanner,
achou a DLL de customizacao em uso e, sem janela para "Repetir", abortou e
deixou o Cleanner parado. A acao agora para os servicos, espera os processos da
pasta deles sairem e religa no fim - e a tarefa real abre manutencao no host
para o guardiao nao religar o RM.Host no meio da copia.
"""
from __future__ import annotations

from app import execution, manutencao, scheduler


def _acoes(**extra):
    base = {"id": "inno-silencioso", "label": "Inno", "args": "/VERYSILENT",
            "extensions": [".exe"], "timeout_sec": 900}
    return execution.actions_for({"execution": {"actions": [{**base, **extra}]}})


def test_stop_services_validado_e_com_espera_padrao():
    a = _acoes(stop_services=["RM.Host*"])["inno-silencioso"]
    assert a["stop_services"] == ["RM.Host*"]
    assert a["stop_wait_sec"] == execution.STOP_WAIT_PADRAO
    # sem a opcao, nada muda para quem ja usava a acao
    assert _acoes()["inno-silencioso"]["stop_services"] == []


def test_stop_services_aceita_texto_unico_e_limita_a_espera():
    a = _acoes(stop_services="RM.Host*", stop_wait_sec=99999)["inno-silencioso"]
    assert a["stop_services"] == ["RM.Host*"] and a["stop_wait_sec"] == 900


def test_nome_de_servico_com_injecao_descarta_a_acao():
    assert _acoes(stop_services=["RM.Host*'; Remove-Item C:\\ -Recurse #"]) == {}
    assert _acoes(stop_services=["$(calc)"]) == {}


def test_prazo_da_chamada_e_maior_que_a_soma_dos_prazos_do_host():
    # se a chamada WinRM vencesse antes do script, o shell morreria antes do
    # bloco que religa os servicos
    com = _acoes(stop_services=["RM.Host*"], stop_wait_sec=120)["inno-silencioso"]
    sem = _acoes()["inno-silencioso"]
    host_sem = 900 + execution.DOWNLOAD_SEC
    host_com = host_sem + execution.PARAR_SEC + 120 + execution.RELIGAR_SEC
    assert execution.prazo_total(sem) == host_sem + execution.FOLGA_SEC
    assert execution.prazo_total(com) == host_com + execution.FOLGA_SEC


def test_instalador_tem_prazo_no_host_e_e_encerrado_com_a_arvore():
    a = _acoes(stop_services=["RM.Host*"])["inno-silencioso"]
    s = execution._script_execucao(a, "C:\totvs\rmon-stage", "p.exe", "http://x/s", "ab")
    assert "-Wait -PassThru" not in s and s.count("-PassThru") == 3
    assert "$proc.WaitForExit(900000)" in s and "taskkill.exe /T /F /PID $proc.Id" in s
    assert "$null = $proc.Handle" in s     # sem isso o ExitCode vem vazio no PS 5.1
    assert "-TimeoutSec %d" % execution.DOWNLOAD_SEC in s


def test_parada_nunca_pega_servico_do_diretorio_do_windows():
    a = _acoes(stop_services=["Win*"])["inno-silencioso"]
    s = execution._script_execucao(a, "C:\totvs\rmon-stage", "p.exe", "http://x/s", "ab")
    assert "StartsWith($env:windir" in s.split("function Get-ServicosParar", 1)[1].split("function Get-PastasServicos")[0]


def test_script_para_espera_e_religa_so_quando_declarado():
    com = _acoes(stop_services=["RM.Host*"], stop_wait_sec=120)["inno-silencioso"]
    s = execution._script_execucao(com, "C:\\totvs\\rmon-stage", "p.exe", "http://x/s", "ab")
    assert "$parar = @('RM.Host*')" in s and "$espera = 120" in s
    assert "Get-ProcessosDe $pastas" in s and "religado: " in s
    # parar vem DEPOIS de conferir o pacote: pacote ruim nao derruba servico
    assert s.index("sha256 conferido") < s.index("Get-ServicosParar $parar")
    assert s.index("Get-ServicosParar $parar") < s.index("Start-Process")
    assert "__" not in s.replace("__init__", "")   # nenhum marcador sobrou

    sem = _acoes()["inno-silencioso"]
    s2 = execution._script_execucao(sem, "C:\\totvs\\rmon-stage", "p.exe", "http://x/s", "ab")
    assert "Get-ServicosParar $parar" not in s2 and "$parar = @()" in s2


def test_comando_previsto_mostra_a_parada():
    com = _acoes(stop_services=["RM.Host*"])["inno-silencioso"]
    txt = execution.comando_previsto(com, "C:\\totvs\\rmon-stage", "p.exe")
    assert txt.startswith("parar RM.Host* e esperar os processos saírem (até 180 s)")
    assert "'C:\\totvs\\rmon-stage\\p.exe' /VERYSILENT" in txt
    assert txt.endswith("religar o que estava rodando")


def test_checagem_parada_diz_o_que_para_e_quantos_processos_esperar():
    a = _acoes(stop_services=["RM.Host*"])["inno-silencioso"]
    c = execution.checagem_parada(a, {
        "parar": [{"nome": "RM.Host.Cleanner", "estado": "Running"},
                  {"nome": "RM.Host.Service", "estado": "Running"}],
        "pastas_parar": "C:\\totvs\\CorporeRM\\RM.Net",   # 1 elemento vem sem lista
        "processos_parar": 17})
    assert c["ok"] and not c["dura"]
    assert "RM.Host.Cleanner, RM.Host.Service" in c["texto"]
    assert "C:\\totvs\\CorporeRM\\RM.Net" in c["texto"] and "(agora: 17)" in c["texto"]


def test_checagem_parada_sem_servico_e_aviso_nao_bloqueio():
    a = _acoes(stop_services=["RM.Host*"])["inno-silencioso"]
    c = execution.checagem_parada(a, {"parar": [], "processos_parar": 0})
    assert not c["ok"] and not c["dura"] and "nada será parado" in c["texto"]


# ---------- manutencao durante a tarefa real ----------
class _Config:
    def __init__(self):
        self.v = {}

    def get(self, chave, padrao=None):
        return self.v.get(chave, padrao)

    def set(self, chave, valor):
        self.v[chave] = valor


def _cfg(monkeypatch, agora):
    c = _Config()
    monkeypatch.setattr(manutencao.db, "get_config", c.get)
    monkeypatch.setattr(manutencao.db, "set_config", c.set)
    monkeypatch.setattr(scheduler, "_agora", lambda: agora)
    monkeypatch.setattr(manutencao.time, "time", lambda: agora)
    return c


TASK = {"id": 9, "server": "fin-sge-web-223", "produto": "VTEX", "version": "12.1.2602.012"}


def test_tarefa_real_abre_manutencao_e_encurta_no_fim(monkeypatch):
    t0 = 1_763_380_000.0
    _cfg(monkeypatch, t0)
    aberta = scheduler._manutencao_da_tarefa(TASK, 1200)
    j = manutencao.todas()["fin-sge-web-223"]
    assert aberta == j
    assert j["por"] == "RMon" and "instalação #9" in j["motivo"]
    assert j["ate"] == t0 + (20 + 1 + scheduler.ASSENTAR_APOS_INSTALAR_MIN) * 60
    scheduler._fim_manutencao_da_tarefa(TASK, aberta)
    j = manutencao.todas()["fin-sge-web-223"]
    assert j["ate"] == t0 + scheduler.ASSENTAR_APOS_INSTALAR_MIN * 60
    assert j["desde"] == t0    # mesma janela, so mais curta


def test_janela_aberta_por_alguem_fica_como_esta(monkeypatch):
    t0 = 1_763_380_000.0
    _cfg(monkeypatch, t0)
    manutencao.iniciar("fin-sge-web-223", 120, "troca de disco", "bruno")
    antes = dict(manutencao.todas()["fin-sge-web-223"])
    abriu = scheduler._manutencao_da_tarefa(TASK, 1200)
    scheduler._fim_manutencao_da_tarefa(TASK, abriu)
    assert abriu is None and manutencao.todas()["fin-sge-web-223"] == antes


def test_janela_estendida_no_painel_durante_a_instalacao_nao_e_encurtada(monkeypatch):
    t0 = 1_763_380_000.0
    _cfg(monkeypatch, t0)
    aberta = scheduler._manutencao_da_tarefa(TASK, 1200)
    manutencao.iniciar("fin-sge-web-223", 120, "aproveitar e trocar o disco", "bruno")
    estendida = dict(manutencao.todas()["fin-sge-web-223"])
    scheduler._fim_manutencao_da_tarefa(TASK, aberta)
    assert manutencao.todas()["fin-sge-web-223"] == estendida
