"""Ciclo de alertas de ponta a ponta (scheduler.poll_all), com banco e canais falsos.

O que se testa aqui e o que o usuario ve no celular ao longo de varios ciclos:
um restart do RMon (todo deploy) nao pode renotificar o que ja estava aberto, e
um engasgo do WinRM nao pode "resolver" e reabrir tudo.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import db, nlb, notify, scheduler

HORA = 3600


class Banco:
    """O pedaco do app.db que o poll_all usa, em memoria."""

    def __init__(self, relogio):
        self.relogio = relogio
        self.alertas: list[tuple[str, str, str, str]] = []
        self.ts: list[float] = []
        self.checks: dict[str, list[bool]] = {}
        self.config: dict = {}

    def get_config(self, chave, padrao=None):
        return self.config.get(chave, padrao)

    def set_config(self, chave, valor):
        self.config[chave] = valor

    def open_alerts_tempos(self):
        tempos: dict[str, dict[str, dict[str, float]]] = {}
        for (server, kind, problem, _), ts in zip(self.alertas, self.ts):
            if kind == "raised":
                tempos.setdefault(server, {})[problem] = {"desde": ts, "aviso": ts}
            elif kind == "reminder" and problem in tempos.get(server, {}):
                tempos[server][problem]["aviso"] = ts
            elif kind == "resolved":
                tempos.get(server, {}).pop(problem, None)
        return tempos

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
        self.ts.append(self.relogio[0])

    def acoes_desde(self, server, problem, desde):
        return [ts for (s, k, p, _), ts in zip(self.alertas, self.ts)
                if s == server and p == problem and k == "acao" and ts >= desde]

    def insert_check(self, server, result):
        self.checks.setdefault(server, []).insert(0, bool(result.get("reachable")))

    def fail_streak(self, server, limit=20):
        n = 0
        for ok in self.checks.get(server, [])[:limit]:
            if ok:
                break
            n += 1
        return n


def _sem_banco_de_verdade():
    raise AssertionError("o teste tentou abrir conexão com o PostgreSQL")


@pytest.fixture
def ambiente(monkeypatch):
    relogio = [1_760_000_000.0]
    banco = Banco(relogio)
    enviadas: list[str] = []
    coletas: dict[str, dict] = {}
    holds: list[set] = []
    # qualquer funcao do db que o teste esqueceu de trocar falha na hora, em
    # vez de ficar esperando um PostgreSQL que nao existe
    monkeypatch.setattr(db, "_conn", _sem_banco_de_verdade)
    for nome in ("open_alerts", "open_alerts_tempos", "record_alert", "insert_check",
                 "fail_streak", "get_config", "set_config", "acoes_desde"):
        monkeypatch.setattr(db, nome, getattr(banco, nome))
    monkeypatch.setattr(db, "broker_max", lambda dias=30: {})
    monkeypatch.setattr(db, "broker_estado", lambda dias=1: {})
    monkeypatch.setattr(db, "fila_hist", lambda n=3: {})
    monkeypatch.setattr(db, "recent_alerts", lambda h=48, lim=500, server=None: [
        {"server": s, "kind": k, "problem": p, "message": m}
        for (s, k, p, m), ts in zip(banco.alertas, banco.ts) if relogio[0] - ts <= h * HORA])
    monkeypatch.setattr(nlb, "run_cycle",
                        lambda inv, res, now=None, hold=(), es=None: holds.append(set(hold)))
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(notify, "send", lambda texto: enviadas.append(texto) or True)
    monkeypatch.setattr(scheduler, "_agora", lambda: relogio[0])
    monkeypatch.setattr(scheduler, "collect_server",
                        lambda s, winrm, defaults: dict(coletas[s.name]))
    servidor = SimpleNamespace(name="app-34", host="10.0.0.34", jobs=None)
    inv = SimpleNamespace(servers=[servidor], winrm=None, defaults={})

    def reiniciar():
        """O que um restart do servico faz com o scheduler: memoria zerada."""
        monkeypatch.setattr(scheduler, "_last", {})
        monkeypatch.setattr(scheduler, "_desde", {})
        monkeypatch.setattr(scheduler, "_avisado", {})
        monkeypatch.setattr(scheduler, "_tentado", {})
        monkeypatch.setattr(scheduler, "_boot", {})
        monkeypatch.setattr(scheduler, "_restaurado", False)

    def ciclo(**r):
        coletas["app-34"] = r
        enviadas.clear()
        scheduler.poll_all(inv, None)
        return list(enviadas)

    def passar(horas):
        relogio[0] += horas * HORA

    reiniciar()
    return SimpleNamespace(banco=banco, ciclo=ciclo, reiniciar=reiniciar, passar=passar,
                           relogio=relogio, enviadas=enviadas, holds=holds, inv=inv)


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


# ------------------------------------------------------------ manutencao

def test_manutencao_cala_e_avisa_uma_vez_no_fim(ambiente):
    from app import manutencao
    manutencao.iniciar("app-34", 60, "reboot planejado", "bruno", agora=ambiente.relogio[0])
    assert ambiente.ciclo(**sem_contato()) == []
    assert ambiente.ciclo(**sem_contato()) == []
    assert ambiente.ciclo(**sem_contato()) == []      # DOWN abre, calado
    assert ambiente.ciclo(**parado()) == []           # volta com servico parado, calado
    assert ("raised", "DOWN") in kinds(ambiente.banco)  # o historico nao perde nada
    assert ambiente.holds[-1] == {"app-34"}            # NLB nao mexe no no
    ambiente.passar(1.1)
    msgs = ambiente.ciclo(**parado())
    assert len(msgs) == 1 and "fim da manutenção" in msgs[0]
    assert "RM.Host.Service1 parado" in msgs[0] and "sem contato" not in msgs[0]
    assert manutencao.todas() == {}
    assert ambiente.holds[-1] == set()
    assert ambiente.ciclo(**parado()) == []           # e nao repete


def test_manutencao_que_volta_limpa_diz_isso(ambiente):
    from app import manutencao
    manutencao.iniciar("app-34", 30, "", "bruno", agora=ambiente.relogio[0])
    ambiente.ciclo(**parado())
    ambiente.ciclo(**ok())
    manutencao.encerrar("app-34", agora=ambiente.relogio[0])
    msgs = ambiente.ciclo(**ok())
    assert len(msgs) == 1 and "voltou limpo" in msgs[0]


def test_janela_estendida_nao_e_fechada_pelo_ciclo(ambiente):
    from app import manutencao
    manutencao.iniciar("app-34", 30, "", "bruno", agora=ambiente.relogio[0])
    ambiente.passar(1)
    manutencao.iniciar("app-34", 30, "", "bruno", agora=ambiente.relogio[0])
    assert ambiente.ciclo(**parado()) == []
    assert "app-34" in manutencao.todas()


# ------------------------------------------------------------ lembrete

def test_critico_aberto_e_lembrado_a_cada_intervalo(ambiente):
    ambiente.ciclo(**parado())
    ambiente.passar(3.9)
    assert ambiente.ciclo(**parado()) == []
    ambiente.passar(0.2)
    msgs = ambiente.ciclo(**parado())
    assert len(msgs) == 1 and "segue aberto há 4h06" in msgs[0]
    assert ambiente.ciclo(**parado()) == []           # um lembrete por intervalo
    ambiente.passar(4)
    assert "segue aberto há 8h06" in ambiente.ciclo(**parado())[0]


def test_aviso_ambar_nao_tem_lembrete(ambiente):
    ambiente.ciclo(**ok(mem_pct=95))
    ambiente.passar(10)
    assert ambiente.ciclo(**ok(mem_pct=95)) == []


def test_lembrete_sobrevive_ao_restart(ambiente):
    ambiente.ciclo(**parado())
    ambiente.passar(4.1)
    assert len(ambiente.ciclo(**parado())) == 1       # lembrou
    ambiente.reiniciar()
    ambiente.passar(1)
    assert ambiente.ciclo(**parado()) == []           # restart nao relembra
    ambiente.passar(3.1)
    assert "segue aberto há 8h" in ambiente.ciclo(**parado())[0]


def test_lembrete_desligado(ambiente):
    ambiente.banco.config["alerts"] = {"lembrete_h": 0}
    ambiente.ciclo(**parado())
    ambiente.passar(30)
    assert ambiente.ciclo(**parado()) == []


def test_sem_lembrete_durante_manutencao_e_relogio_recomeca_no_fim(ambiente):
    from app import manutencao
    ambiente.ciclo(**parado())
    manutencao.iniciar("app-34", 6 * 60, "", "bruno", agora=ambiente.relogio[0])
    ambiente.passar(5)
    assert ambiente.ciclo(**parado()) == []
    ambiente.passar(1.1)
    assert "fim da manutenção" in ambiente.ciclo(**parado())[0]
    ambiente.passar(1)
    assert ambiente.ciclo(**parado()) == []           # o fim valeu como aviso


# ------------------------------------------------------------ resumo diario

def test_resumo_sai_na_hora_e_uma_vez_por_dia(ambiente, monkeypatch):
    import time as _t
    ambiente.ciclo(**parado())
    ambiente.ciclo(**ok(mem_pct=95, services=parado()["services"]))
    ambiente.banco.record_alert("app-34", "raised", "JOBS", "5 execuções de job com erro")
    hora = _t.localtime(ambiente.relogio[0]).tm_hour
    ambiente.banco.config["alerts"] = {"resumo_hora": (hora + 1) % 24}
    assert not scheduler.resumo_diario(ambiente.inv)
    ambiente.banco.config["alerts"] = {"resumo_hora": hora}
    ambiente.enviadas.clear()
    assert scheduler.resumo_diario(ambiente.inv)
    texto = ambiente.enviadas[-1]
    assert "Críticos abertos (1)" in texto and "RM.Host.Service1" in texto
    assert "Avisos abertos (2)" in texto and "job com erro" in texto and "memória" in texto
    assert not scheduler.resumo_diario(ambiente.inv)  # mesmo dia: nao repete


def test_resumo_desligado(ambiente):
    ambiente.banco.config["alerts"] = {"resumo_hora": -1}
    assert not scheduler.resumo_diario(ambiente.inv)
    assert ambiente.enviadas == []


def test_resumo_sem_nada_aberto_e_sinal_de_vida():
    texto = scheduler.texto_resumo({}, {}, {}, [], ["a"], 1_760_000_000.0)
    assert "Nenhum problema aberto" in texto and "Últimas 24h: 0" in texto


def test_resumo_corta_lista_longa():
    abertos = {"a": {f"svc:S{i:02d}": f"serviço S{i:02d} = Stopped" for i in range(30)}}
    texto = scheduler.texto_resumo(abertos, {}, {}, [], ["a"], 1_760_000_000.0)
    assert "Críticos abertos (30)" in texto and "... e mais 15" in texto


def test_elastic_sem_resposta_nao_resolve_agente_parado(ambiente, monkeypatch):
    from app import elastic
    monkeypatch.setattr(elastic, "ativo", lambda cfg: True)
    vistos = {}
    monkeypatch.setattr(scheduler, "_enriquecer_elastic", lambda inv, col: ({"app-34": "h34"}, dict(vistos)))
    vistos.update({"h34": ambiente.relogio[0] - 3600, "outro": ambiente.relogio[0]})
    monkeypatch.setattr(scheduler.time, "time", lambda: ambiente.relogio[0])
    msgs = ambiente.ciclo(**ok())
    assert len(msgs) == 1 and "agente do Elastic" in msgs[0]
    vistos.clear()  # Elastic nao respondeu neste ciclo
    assert ambiente.ciclo(**ok()) == []
    vistos.update({"h34": ambiente.relogio[0], "outro": ambiente.relogio[0]})
    assert "resolvido" in ambiente.ciclo(**ok())[0]


# ------------------------------------------------------------ servico que nao subiu no boot

def boot(uptime, status="Stopped"):
    """O rh-web-248 depois do reboot do Windows Update de 15/10/2026."""
    return ok(uptime_sec=uptime, services=[
        {"name": "RM.Host.Service", "status": status, "src": "auto", "start": "Auto"}])


@pytest.fixture
def partidas(monkeypatch):
    """collector.subir_servicos falso: registra cada chamada (uma por host)."""
    chamadas: list[str] = []
    lotes: list[list[tuple[str, str]]] = []
    resposta = [(True, "subiu")]

    def falso(server, wc, itens, allowed=None, espera_s=25):
        lotes.append(list(itens))
        out = {}
        for acao, nome in itens:
            assert nome in (allowed or [])
            chamadas.append(nome if acao == "start" else f"{acao}:{nome}")
            out[nome] = resposta[0]
        return out
    monkeypatch.setattr(scheduler, "subir_servicos", falso)
    return SimpleNamespace(chamadas=chamadas, lotes=lotes, resposta=resposta)


def test_incidente_248_sobe_sozinho_sem_ninguem_clicar(ambiente, partidas):
    msgs = ambiente.ciclo(**boot(228))
    assert len(msgs) == 1 and "não subiu após o reboot de" in msgs[0]
    assert partidas.chamadas == []           # cedo demais: o boot ainda esta assentando
    ambiente.passar(0.15)
    msgs = ambiente.ciclo(**boot(228 + 540))
    assert partidas.chamadas == ["RM.Host.Service"]
    assert any("RMon iniciando RM.Host.Service automaticamente (1ª tentativa; não subiu após o reboot" in m
               and m.endswith(": subiu") for m in msgs)
    assert ("acao", "svc:RM.Host.Service") in kinds(ambiente.banco)
    ambiente.passar(0.02)
    msgs = ambiente.ciclo(**boot(228 + 600, status="Running"))
    assert "resolvido" in msgs[0]


def test_guardiao_nunca_desiste(ambiente, partidas):
    # o .188 em 16/10: o Elastic Agent estourou os 30 s do Windows por uma hora
    # depois do boot e o limite antigo de 3 tentativas ja tinha desistido
    partidas.resposta[0] = (False, "não subiu (Stopped): Tempo limite esgotado (30000 milissegundos)")
    up, minutos, canal, t0 = 700, [], [], ambiente.relogio[0]
    for i in range(6 * 60):                  # 6 h de coleta a cada minuto
        antes = len(partidas.chamadas)
        msgs = ambiente.ciclo(**boot(up))
        if len(partidas.chamadas) > antes:
            minutos.append(round((ambiente.relogio[0] - t0) / 60))
            canal.append(any("RMon iniciando RM.Host.Service automaticamente" in m for m in msgs))
            if len(minutos) == 4:
                ambiente.reiniciar()         # deploy do RMon no meio: a conta continua
        ambiente.passar(1 / 60)
        up += 60
    # 1a na hora; depois espera 2, 5, 10, 15 e fica de 30 em 30 min, sem parar
    assert minutos[:7] == [0, 2, 7, 17, 32, 62, 92]
    assert len(minutos) >= 15
    textos = [m for _, k, _, m in ambiente.banco.alertas if k == "acao"]
    assert len(textos) == len(minutos) and not any("desisti " in t for t in textos)
    # canal: as 3 primeiras e uma a cada 6 (o resto fica so na linha do tempo)
    assert canal[:3] == [True, True, True] and canal[3:5] == [False, False] and canal[5] is True


def test_texto_da_falha_diz_quando_tenta_de_novo(ambiente, partidas):
    partidas.resposta[0] = (False, "não subiu (Stopped)")
    msgs = ambiente.ciclo(**boot(700))
    assert any("(1ª tentativa;" in m and "nova tentativa em 2 min, sem desistir" in m for m in msgs)


def test_host_que_nao_respondeu_repete_no_ciclo_seguinte(ambiente, partidas):
    # .188, 16/10 11:13: ReadTimeout do WinRM contou como tentativa e a proxima
    # ficou para dali a 10 min
    partidas.resposta[0] = (False, "sem resposta do host: ReadTimeout: read timeout=30")
    ambiente.ciclo(**boot(700))
    assert ("acao", "svc:RM.Host.Service") not in kinds(ambiente.banco)
    partidas.resposta[0] = (True, "subiu")
    ambiente.passar(1 / 60)
    msgs = ambiente.ciclo(**boot(760))
    assert partidas.chamadas == ["RM.Host.Service"] * 2
    assert any("(1ª tentativa;" in m and m.endswith(": subiu") for m in msgs)


def test_servico_em_transicao_espera_e_preso_e_reiniciado(ambiente, partidas):
    parando = ok(uptime_sec=5 * HORA, services=[
        {"name": "Elastic Agent", "status": "StopPending", "src": "agente", "start": "Auto"}])
    ambiente.ciclo(**parando)
    ambiente.passar(4 / 60)
    ambiente.ciclo(**parando)
    assert partidas.chamadas == []           # deixa o Windows terminar de parar
    ambiente.passar(2 / 60)
    msgs = ambiente.ciclo(**parando)
    assert partidas.chamadas == ["restart:Elastic Agent"]
    assert any("RMon reiniciando Elastic Agent automaticamente (1ª tentativa; preso em StopPending desde" in m for m in msgs)


def test_partida_desligada_so_avisa(ambiente, partidas):
    ambiente.banco.config["alerts"] = {"boot_iniciar_min": 0}
    ambiente.ciclo(**boot(700))
    ambiente.passar(2)
    ambiente.ciclo(**boot(700 + 7200))
    assert partidas.chamadas == []


def test_novo_boot_ganha_nova_tentativa(ambiente, partidas):
    ambiente.ciclo(**boot(700))
    ambiente.passar(2)
    ambiente.ciclo(**boot(700))              # reiniciou de novo e o servico segue parado
    assert partidas.chamadas == ["RM.Host.Service", "RM.Host.Service"]


def test_servico_que_caiu_longe_do_boot_tambem_volta(ambiente, partidas):
    ambiente.ciclo(**boot(5 * HORA, status="Running"))
    msgs = ambiente.ciclo(**boot(5 * HORA + 60))
    assert partidas.chamadas == ["RM.Host.Service"]
    assert any("RMon iniciando RM.Host.Service automaticamente (1ª tentativa; parado desde" in m for m in msgs)


def test_desabilitado_ou_inexistente_nao_e_mexido(ambiente, partidas):
    ambiente.ciclo(**ok(uptime_sec=5 * HORA, services=[
        {"name": "Fixo.Desligado", "status": "Stopped", "src": "fixo", "start": "Disabled"},
        {"name": "Fixo.Sumiu", "status": "NOT_FOUND", "src": "fixo"}]))
    assert partidas.chamadas == []


def test_todos_do_host_vao_numa_chamada_so(ambiente, partidas):
    # o reboot do .188: RM.Host.Service, Service1 e Elastic Agent parados juntos
    ambiente.ciclo(**ok(uptime_sec=700, services=[
        {"name": "RM.Host.Service", "status": "Stopped", "src": "auto", "start": "Auto"},
        {"name": "RM.Host.Service1", "status": "StartPending", "src": "auto", "start": "Auto"},
        {"name": "Elastic Agent", "status": "Stopped", "src": "agente", "start": "Auto"}]))
    # o que ja esta subindo (StartPending) e deixado assentar
    assert partidas.lotes == [[("start", "RM.Host.Service"), ("start", "Elastic Agent")]]


def test_sem_partida_automatica_durante_manutencao(ambiente, partidas):
    from app import manutencao
    manutencao.iniciar("app-34", 120, "reboot planejado", "bruno", agora=ambiente.relogio[0])
    ambiente.ciclo(**boot(700))
    assert partidas.chamadas == []


# ------------------------------------------------------------ agentes de coleta

def com_agente(uptime, status="Stopped"):
    return ok(uptime_sec=uptime, services=[
        {"name": "RM.Host.Service", "status": "Running", "src": "auto", "start": "Auto"},
        {"name": "Elastic Agent", "status": status, "src": "agente", "start": "Auto"}])


def test_agente_parado_a_qualquer_hora_sobe_sozinho(ambiente, partidas):
    msgs = ambiente.ciclo(**com_agente(5 * HORA))
    assert partidas.chamadas == ["Elastic Agent"]
    assert any("agente de coleta Elastic Agent parado" in m for m in msgs)
    assert any("RMon iniciando Elastic Agent automaticamente (1ª tentativa; parado desde" in m for m in msgs)
    ambiente.passar(1 / 60)
    assert "resolvido" in ambiente.ciclo(**com_agente(5 * HORA + 60, "Running"))[0]


def test_agente_logo_apos_o_boot_espera_assentar(ambiente, partidas):
    ambiente.ciclo(**com_agente(120))        # partida atrasada do Windows ainda nao veio
    assert partidas.chamadas == []
    ambiente.passar(10 / 60)
    ambiente.ciclo(**com_agente(720))
    assert partidas.chamadas == ["Elastic Agent"]


def test_agente_que_nao_sobe_segue_sendo_tentado(ambiente, partidas):
    partidas.resposta[0] = (False, "não subiu (Stopped)")
    for i in range(12):
        ambiente.ciclo(**com_agente(5 * HORA + i * 600))
        ambiente.passar(10 / 60)
    # minutos 0, 10, 20, 30, 50, 80, 110: nada de parar na 3a
    assert partidas.chamadas == ["Elastic Agent"] * 7


def test_agente_mudo_para_o_elastic_e_reiniciado(ambiente, partidas, monkeypatch):
    monkeypatch.setattr(scheduler, "padrao_cluster", lambda *a, **k: {
        "app-34": {"PADRAO:elastic": "agente do Elastic (h34) sem mandar dados nos últimos 15 min"}})
    msgs = ambiente.ciclo(**com_agente(5 * HORA, "Running"))
    assert partidas.chamadas == ["restart:Elastic Agent"]
    assert any("RMon reiniciando Elastic Agent automaticamente (1ª tentativa; no ar, mas sem mandar dados" in m
               for m in msgs)
    assert ("acao", "PADRAO:elastic") in kinds(ambiente.banco)
    ambiente.passar(1 / 60)
    ambiente.ciclo(**com_agente(5 * HORA + 60, "Running"))
    assert len(partidas.chamadas) == 1       # espera o intervalo antes de tentar de novo


def test_subir_servicos_le_o_estado_de_cada_um(monkeypatch):
    from app import collector
    from app.config import ServerConfig, WinRMConfig
    scripts = []

    def falso(host, wc, user, pw, script, attempts=2, budget_sec=None):
        scripts.append(script)
        saida = ('[{"name":"RM.Host.Service","status":"Running","erro":""},'
                 '{"name":"Elastic Agent","status":"Stopped","erro":"Tempo limite esgotado (30000 milissegundos)"},'
                 '{"name":"RM.Host.Service1","status":"StartPending","erro":""}]')
        return SimpleNamespace(status_code=0, std_out=saida.encode(), std_err=b"")
    monkeypatch.setattr(collector, "_run_ps_resilient", falso)
    monkeypatch.setattr(collector, "credential_for", lambda cred: ("u", "p"))
    srv = ServerConfig(name="x", host="h", services=["RM.Host.Service"])
    r = collector.subir_servicos(srv, WinRMConfig(), [("start", "RM.Host.Service"), ("restart", "Elastic Agent"),
                                                       ("start", "RM.Host.Service1"), ("start", "Outro"),
                                                       ("start", "x'; Stop-Computer")],
                                 allowed=["Elastic Agent", "RM.Host.Service1", "x'; Stop-Computer"])
    assert r["RM.Host.Service"] == (True, "subiu")
    assert r["RM.Host.Service1"][0] is True
    assert r["Elastic Agent"] == (False, "não subiu (Stopped): Tempo limite esgotado (30000 milissegundos)")
    assert r["Outro"][0] is False and "não monitorado" in r["Outro"][1]
    assert r["x'; Stop-Computer"][0] is False             # nome fora do padrao nao chega ao script
    assert len(scripts) == 1 and "[void]$itens.Add(@('restart', 'Elastic Agent'))" in scripts[0]
    assert "Stop-Computer" not in scripts[0] and "Start-Service" not in scripts[0]


def test_subir_servicos_winrm_fora_vira_falha_de_todos(monkeypatch):
    from app import collector
    from app.config import ServerConfig, WinRMConfig

    def falha(*a, **k):
        raise collector.WinRMOperationTimeoutError()
    monkeypatch.setattr(collector, "_run_ps_resilient", falha)
    monkeypatch.setattr(collector, "credential_for", lambda cred: ("u", "p"))
    r = collector.subir_servicos(ServerConfig(name="x", host="h"), WinRMConfig(), [("start", "A"), ("start", "B")],
                                 allowed=["A", "B"])
    assert r == {"A": (False, "sem resposta do host: WinRMOperationTimeoutError: "),
                 "B": (False, "sem resposta do host: WinRMOperationTimeoutError: ")}


# ------------------------------------------------------------ encerramento automatico do JobRunner (19/10)

@pytest.fixture
def encerrar(ambiente, monkeypatch):
    from app import jobstats
    feitos = []
    monkeypatch.setattr(scheduler, "_jr_amostra", {})
    monkeypatch.setattr(scheduler, "kill_jobrunner",
                        lambda srv, wc, pid, inicio, processo: feitos.append((pid, inicio)) or
                        (True, f"PID {pid} encerrado (4280 MB de memória privada liberados)"))
    monkeypatch.setattr(jobstats, "executions_by_pid", lambda comp, pids: {
        str(p): {"idjob": 2447428, "nome": "Gerar Relatório", "usuario": "DJACY.MARIANO"} for p in pids})
    return feitos


def jr_de(mb, pid=7760):
    return {"top": [{"pid": pid, "private_mb": mb, "start": "2026-10-19 09:24:38"}],
            "computer": "WIN2016", "count": 30}


def test_jobrunner_estourando_e_encerrado_na_coleta(ambiente, encerrar):
    msgs = ambiente.ciclo(**ok(jobrunners=jr_de(4280)))
    assert encerrar == [(7760, "2026-10-19 09:24:38")]
    texto = "\n".join(msgs)
    assert "encerrou o processo de job PID 7760" in texto and "DJACY.MARIANO" in texto
    acoes = [(p, m) for (_, k, p, m) in ambiente.banco.alertas if k == "acao"]
    assert acoes and acoes[0][0] == "jobrunner-kill:7760"
    # o encerrado nao abre o alerta de 4 GB no mesmo ciclo
    assert not [p for (_, k, p, _) in ambiente.banco.alertas if k == "raised" and p.startswith("jobrunner")]


def test_crescimento_rapido_e_encerrado_na_segunda_coleta(ambiente, encerrar):
    ambiente.ciclo(**ok(jobrunners=jr_de(340, pid=2172)))
    assert encerrar == []
    ambiente.relogio[0] += 60
    ambiente.ciclo(**ok(jobrunners=jr_de(854, pid=2172)))
    assert [pid for pid, _ in encerrar] == [2172]


def test_host_em_manutencao_nao_tem_job_encerrado(ambiente, encerrar, monkeypatch):
    from app import manutencao
    monkeypatch.setattr(manutencao, "ativas", lambda agora=None, janelas=None: {"app-34": {"ate": 9e12}})
    monkeypatch.setattr(manutencao, "todas", lambda: {"app-34": {"ate": 9e12}})
    ambiente.ciclo(**ok(jobrunners=jr_de(4280)))
    assert encerrar == []
