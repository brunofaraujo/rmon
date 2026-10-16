"""Decisao de drenagem/reingresso do NLB (app/nlb.py), sem rede nem banco.

Os cenarios reproduzem os incidentes de 13-14/10/2026: um no que autenticava o
RDP sem criar sessao por horas enquanto o NLB seguia mandando usuarios para
ele, e dois nos doentes no mesmo dia.
"""
from __future__ import annotations

from app import nlb

T0 = 1_760_000_000.0
MIN = 60


def no(nome, status=1007, users=10, auth=6, sess=6, commit=30.0, reach=True,
       rm=True, uptime_min=3000, jr=None, cluster="vip", now=T0):
    """Coleta de um no; o host_id no NLB e o digito final do nome (n1 -> 1)."""
    r = {
        "reachable": reach,
        "users_count": users,
        "commit_pct": commit,
        "uptime_sec": uptime_min * 60,
        "services": [{"name": "RM.Host.Service", "status": "Running" if rm else "Stopped", "src": "auto"},
                     {"name": "RM.Host.Service1", "status": "Running", "src": "auto"}],
        "nlb": {"status": status, "cluster": cluster, "host_id": int(nome[-1]), "auth": auth, "sess": sess},
        "jobrunners": ({"count": 30, "top": [{"pid": 4242, "private_mb": jr * 1024}, {"pid": 7, "private_mb": 70}]}
                       if jr is not None else None),
        "error": None if reach else "WinRMOperationTimeoutError",
    }
    return nlb.view(nome, "10.0.0." + nome[-1], r, now)


def cfg(**kw):
    return nlb.settings_for({"nlb": kw})


def rodar(vistas, state, c, now):
    return nlb.evaluate(vistas, state, c, now)


def saudaveis(now=T0):
    return [no("n2", users=17, sess=8, now=now), no("n3", users=17, sess=8, now=now)]


def kinds(decisoes):
    return [d["kind"] for d in decisoes]


# ------------------------------------------------------------- sinais

def test_cluster_saudavel_nao_decide_nada():
    d, _ = rodar([no("n1")] + saudaveis(), {}, cfg(), T0)
    assert d == []


def test_auth_sem_sessao_14_10():
    """srv06n na manha de 14/10: 7 a 18 logons autenticados, 0 sessoes."""
    v = no("n1", users=0, auth=7, sess=0)
    motivos = nlb.sick_reasons(v, [v] + saudaveis(), cfg())
    assert any("autentica sem criar sessao" in m for m in motivos)


def test_criacao_parcial_13_10_17h():
    """win2016-2 as 17h de 13/10: 27 autenticacoes para 8 sessoes."""
    v = no("n1", users=5, auth=27, sess=8)
    assert nlb.sick_reasons(v, [v] + saudaveis(), cfg())


def test_razao_normal_nao_e_doenca():
    v = no("n1", users=12, auth=10, sess=9)
    assert nlb.sick_reasons(v, [v] + saudaveis(), cfg()) == []


def test_no_sem_1149_pego_pela_comparacao_com_os_pares():
    """O .188 nao registra o 1149: sobra o sinal 'ninguem entra aqui'."""
    v = no("n1", users=0, auth=0, sess=0)
    motivos = nlb.sick_reasons(v, [v] + saudaveis(), cfg())
    assert any("nenhuma sessao aqui" in m for m in motivos)


def test_pares_quietos_nao_acusam_ninguem():
    """Madrugada: todos com zero - nao e doenca."""
    v = no("n1", users=0, auth=0, sess=0)
    pares = [no("n2", users=0, sess=0), no("n3", users=1, sess=1)]
    assert nlb.sick_reasons(v, [v] + pares, cfg()) == []


def test_carencia_apos_reboot_desliga_comparacao_com_pares():
    v = no("n1", users=0, auth=0, sess=0, uptime_min=12)
    assert nlb.sick_reasons(v, [v] + saudaveis(), cfg()) == []


def test_rm_host_parado_e_commit_no_teto():
    v = no("n1", rm=False, commit=97)
    motivos = nlb.sick_reasons(v, [v] + saudaveis(), cfg())
    assert any("RM.Host fora do ar" in m for m in motivos)
    assert any("commit" in m for m in motivos)


def test_jobrunner_so_drena_se_ligado():
    v = no("n1", jr=30.0)
    assert nlb.sick_reasons(v, [v] + saudaveis(), cfg()) == []
    assert nlb.sick_reasons(v, [v] + saudaveis(), cfg(drain_jobrunner_gb=20))


def test_no_fora_do_nlb_nao_e_julgado_doente():
    v = no("n1", status=1005, users=0, auth=0, sess=0)
    assert nlb.sick_reasons(v, [v] + saudaveis(), cfg()) == []


# ------------------------------------------------------------- histerese / modo alerta

def doente(now):
    return [no("n1", users=0, auth=7, sess=0, now=now)] + saudaveis(now)


def test_histerese_e_modo_alerta_avisam_uma_vez_so():
    c = cfg()  # modo alerta e o padrao
    d, st = rodar(doente(T0), {}, c, T0)
    assert d == []                                   # acabou de ficar doente
    d, st = rodar(doente(T0 + 10 * MIN), st, c, T0 + 10 * MIN)
    assert d == []                                   # ainda dentro dos 15 min
    d, st = rodar(doente(T0 + 15 * MIN), st, c, T0 + 15 * MIN)
    assert kinds(d) == ["would_drain"] and d[0]["action"] is None and d[0]["notify"]
    d, st = rodar(doente(T0 + 16 * MIN), st, c, T0 + 16 * MIN)
    assert d == []                                   # mesma situacao: nao repete
    # sarou: um aviso de recuperacao
    d, st = rodar([no("n1", now=T0 + 20 * MIN)] + saudaveis(), st, c, T0 + 20 * MIN)
    assert kinds(d) == ["recovered"]


def test_interrupcao_zera_a_histerese():
    c = cfg(mode="auto")
    _, st = rodar(doente(T0), {}, c, T0)
    _, st = rodar([no("n1")] + saudaveis(), st, c, T0 + 8 * MIN)
    _, st = rodar(doente(T0 + 9 * MIN), st, c, T0 + 9 * MIN)
    d, _ = rodar(doente(T0 + 16 * MIN), st, c, T0 + 16 * MIN)
    assert d == []                                   # so 7 min seguidos


def test_modo_off_nao_avalia():
    d, _ = rodar(doente(T0 + 60 * MIN), {"nodes": {"n1": {"bad_since": T0}}}, cfg(mode="off"), T0 + 60 * MIN)
    assert d == []


# ------------------------------------------------------------- ciclo completo no modo auto

def test_drena_e_so_reingressa_depois_do_reboot():
    c = cfg(mode="auto")
    _, st = rodar(doente(T0), {}, c, T0)
    t = T0 + 15 * MIN
    d, st = rodar(doente(t), st, c, t)
    assert kinds(d) == ["drain"] and d[0]["action"] == "drain"
    st, _ = nlb.apply_result(st, d[0], True, t, "1007 -> 1009")
    assert st["nodes"]["n1"]["owned"] and st["nodes"]["n1"]["drained_at"] == t

    # drenado, sem reboot e sem sessao nova: nao volta
    t2 = t + 40 * MIN
    fora = no("n1", status=1009, users=0, auth=0, sess=0, uptime_min=3100, now=t2)
    d, st = rodar([fora] + saudaveis(t2), st, c, t2)
    assert d == []
    assert any("sem reboot" in g for g in st["nodes"]["n1"]["rejoin_gaps"])

    # reiniciou (NLB guardou o estado parado): apto, espera 10 min e volta
    t3 = t2 + 30 * MIN
    rebootado = no("n1", status=1005, users=0, auth=0, sess=0, uptime_min=15, commit=9, now=t3)
    d, st = rodar([rebootado] + saudaveis(t3), st, c, t3)
    assert d == [] and st["nodes"]["n1"]["good_since"] == t3
    t4 = t3 + 10 * MIN
    rebootado = no("n1", status=1005, users=0, auth=0, sess=0, uptime_min=25, commit=9, now=t4)
    d, st = rodar([rebootado] + saudaveis(t4), st, c, t4)
    assert kinds(d) == ["rejoin"] and d[0]["action"] == "rejoin"
    st, _ = nlb.apply_result(st, d[0], True, t4, "1005 -> 1006")
    assert not st["nodes"]["n1"]["owned"]

    # recem-reingressado, ainda sem usuario: a carencia impede drenar de novo
    t5 = t4 + 16 * MIN
    vazio = no("n1", users=0, auth=0, sess=0, uptime_min=41, now=t5)
    d, st = rodar([vazio] + saudaveis(t5), st, c, t5)
    assert d == []


def test_sessao_rdp_direta_tambem_libera_o_reingresso():
    c = cfg(mode="auto")
    st = {"nodes": {"n1": {"owned": True, "drained_at": T0}}}
    v = no("n1", status=1005, users=1, auth=1, sess=1, uptime_min=3000, commit=20, now=T0 + MIN)
    assert nlb.rejoin_gaps(v, st["nodes"]["n1"], c) == []


def test_reingresso_bloqueado_com_rm_host_parado_ou_commit_alto():
    c = cfg()
    v = no("n1", status=1005, rm=False, commit=90, uptime_min=60)
    falta = nlb.rejoin_gaps(v, {}, c)
    assert any("RM.Host" in f for f in falta) and any("commit" in f for f in falta)


def test_falha_ao_drenar_avisa_uma_vez_e_tenta_de_novo_depois():
    c = cfg(mode="auto")
    st = {"nodes": {"n1": {"bad_since": T0}}}
    t = T0 + 15 * MIN
    d, st = rodar(doente(t), st, c, t)
    st, nova = nlb.apply_result(st, d[0], False, t, "WinRMOperationTimeoutError")
    assert nova
    d, st = rodar(doente(t + MIN), st, c, t + MIN)
    assert d == []                                    # dentro do retry_min
    d, st = rodar(doente(t + 5 * MIN), st, c, t + 5 * MIN)
    assert kinds(d) == ["drain"]
    st, nova = nlb.apply_result(st, d[0], False, t + 5 * MIN, "de novo")
    assert not nova                                   # mesma falha: nao repete a mensagem


# ------------------------------------------------------------- seguranca

def test_dois_nos_doentes_13_10_o_segundo_fica():
    """Com 3 nos, drenar o segundo doente deixaria 1 ativo: bloqueado."""
    c = cfg(mode="auto", cooldown_min=0)
    st = {"nodes": {"n1": {"bad_since": T0}, "n2": {"bad_since": T0 + MIN}}}
    t = T0 + 20 * MIN
    vistas = [no("n1", users=0, auth=9, sess=0, now=t), no("n2", users=4, auth=27, sess=8, now=t),
              no("n3", users=17, sess=8, now=t)]
    d, st = rodar(vistas, st, c, t)
    assert kinds(d) == ["drain"] and d[0]["server"] == "n1"   # o mais antigo, um por ciclo
    st, _ = nlb.apply_result(st, d[0], True, t)
    t2 = t + MIN
    vistas = [no("n1", status=1009, users=0, auth=0, sess=0, now=t2),
              no("n2", users=4, auth=27, sess=8, now=t2), no("n3", users=17, sess=8, now=t2)]
    d, st = rodar(vistas, st, c, t2)
    assert kinds(d) == ["blocked"] and d[0]["server"] == "n2"
    assert "deixaria 1 no(s) ativo(s)" in d[0]["msg"]


def test_modo_alerta_simula_o_piso_para_o_segundo_doente():
    c = cfg()
    st = {"nodes": {"n1": {"bad_since": T0}, "n2": {"bad_since": T0 + MIN}}}
    t = T0 + 20 * MIN
    vistas = [no("n1", users=0, auth=9, sess=0, now=t), no("n2", users=4, auth=27, sess=8, now=t),
              no("n3", users=17, sess=8, now=t)]
    d, _ = rodar(vistas, st, c, t)
    assert kinds(d) == ["would_drain", "blocked"]


def test_piso_minimo_de_um_saudavel_nao_e_configuravel_para_zero():
    c = cfg(mode="auto", min_healthy_remaining=0, min_active_remaining=0)
    assert c["min_healthy_remaining"] == 1 and c["min_active_remaining"] == 1
    st = {"nodes": {"n1": {"bad_since": T0}}}
    t = T0 + 20 * MIN
    # os outros dois tambem doentes (recem): drenar n1 deixaria 0 saudaveis
    vistas = [no("n1", users=0, auth=9, sess=0, now=t), no("n2", rm=False, now=t),
              no("n3", commit=99, now=t)]
    d, _ = rodar(vistas, st, c, t)
    assert kinds(d) == ["blocked"]


def test_par_sem_contato_nao_conta_como_ativo():
    c = cfg(mode="auto")
    st = {"nodes": {"n1": {"bad_since": T0}}}
    t = T0 + 20 * MIN
    vistas = [no("n1", users=0, auth=9, sess=0, now=t), no("n2", reach=False, now=t),
              no("n3", users=17, sess=8, now=t)]
    d, _ = rodar(vistas, st, c, t)
    assert kinds(d) == ["blocked"]


def test_cooldown_entre_drenagens_automaticas():
    c = cfg(mode="auto", min_active_remaining=1)
    st = {"nodes": {"n1": {"owned": True, "drained_at": T0 + 10 * MIN}, "n2": {"bad_since": T0}},
          "clusters": {"vip": {"last_auto": T0 + 10 * MIN}}}
    t = T0 + 20 * MIN
    vistas = [no("n1", status=1009, users=0, auth=0, sess=0, now=t),
              no("n2", users=0, auth=9, sess=0, now=t), no("n3", users=17, sess=8, now=t)]
    d, _ = rodar(vistas, st, c, t)
    assert kinds(d) == ["blocked"] and "cooldown" in d[0]["msg"]


def test_flapping_vira_so_aviso():
    c = cfg(mode="auto")
    st = {"nodes": {"n1": {"bad_since": T0, "drains": [T0 - 3600, T0 - 7200]}}}
    t = T0 + 20 * MIN
    d, _ = rodar(doente(t), st, c, t)
    assert kinds(d) == ["blocked"] and "flapping" in d[0]["msg"]


def test_manutencao_nao_e_tocada():
    c = cfg(mode="auto", hold=["n1"])
    st = {"nodes": {"n1": {"bad_since": T0}}}
    t = T0 + 20 * MIN
    d, _ = rodar(doente(t), st, c, t)
    assert all(x["action"] is None for x in d)
    # hold ligado pela tela vale igual, inclusive para reingresso
    st = {"nodes": {"n1": {"hold": True, "owned": True, "drained_at": T0}}}
    v = no("n1", status=1005, uptime_min=60, commit=10, now=t)
    d, _ = rodar([v] + saudaveis(t), st, cfg(mode="auto", rejoin_after_min=0), t)
    assert d == []


def test_doente_sem_winrm_avisa_que_nao_da_para_drenar():
    """Sem a drenagem por falta de contato, o RMon so consegue avisar."""
    c = cfg(mode="auto", unreachable_drain=False)
    st = {"nodes": {"n1": {"bad_since": T0, "last_status": 1008}}}
    t = T0 + 20 * MIN
    vistas = [no("n1", reach=False, now=t)] + saudaveis(t)
    d, st = rodar(vistas, st, c, t)
    assert kinds(d) == ["unreachable_sick"]
    d, st = rodar(vistas, st, c, t + MIN)
    assert d == []
    assert st["nodes"]["n1"]["bad_since"] == T0     # nao perde a contagem


# ------------------------------------------------------------- no fora sem dono

def test_fora_do_nlb_sem_dono_avisa_e_nao_reingressa_por_padrao():
    """srv06n em 14/10 08:04: saiu do NLB sem o RMon ter drenado."""
    c = cfg(mode="auto")
    v = no("n1", status=1005, users=0, auth=0, sess=0, uptime_min=60, commit=10)
    d, st = rodar([v] + saudaveis(), {}, c, T0)
    assert kinds(d) == ["out_unowned"] and "ja esta apto" in d[0]["msg"]
    d, st = rodar([v] + saudaveis(), st, c, T0 + 30 * MIN)
    assert d == []


def test_fora_do_nlb_sem_dono_reingressa_com_rejoin_unowned():
    c = cfg(mode="auto", rejoin_unowned=True)
    v = no("n1", status=1005, users=0, auth=0, sess=0, uptime_min=60, commit=10)
    d, st = rodar([v] + saudaveis(), {}, c, T0)
    assert d == []
    d, st = rodar([v] + saudaveis(), st, c, T0 + 10 * MIN)
    assert kinds(d) == ["rejoin"]


def test_start_manual_por_fora_limpa_a_posse():
    c = cfg(mode="auto")
    st = {"nodes": {"n1": {"owned": True, "drained_at": T0}}}
    d, st = rodar([no("n1")] + saudaveis(), st, c, T0 + MIN)
    assert not st["nodes"]["n1"]["owned"]


# ------------------------------------------------------------- piso da acao manual

def test_drenagem_manual_respeita_o_piso():
    c = cfg()
    vistas = [no("n1"), no("n2", status=1005), no("n3")]
    ok, motivo = nlb.floor_ok(vistas, "n1", c)
    assert not ok and "restariam 1" in motivo
    ok, _ = nlb.floor_ok([no("n1"), no("n2"), no("n3")], "n1", c)
    assert ok


# ------------------------------------------------------------- medicao do JobRunner (main #32)

def test_view_le_o_maior_jobrunner_da_medicao_jobrunners():
    v = no("n1", jr=30.0)
    assert v["jobrunner_gb"] == 30.0 and v["jobrunner_pid"] == 4242
    # PowerShell 5.1 manda colecao unitaria como objeto, nao lista
    um = nlb.view("n1", "h", {"reachable": True, "jobrunners": {"top": {"pid": 9, "private_mb": 512}}}, T0)
    assert um["jobrunner_gb"] == 0.5 and um["jobrunner_pid"] == 9
    assert nlb.view("n1", "h", {"reachable": True}, T0)["jobrunner_gb"] is None


# ------------------------------------------------------------- no sem contato (16/10)
# .188 em 16/10: parou de responder ao WinRM as 10:01 e voltou ao NLB sozinho as
# 10:05:58, sem estar pronto; ate 10:16 recebeu usuarios sem criar sessao nenhuma.

def mudo(nome, now=T0):
    """Coleta que falhou: e assim que a vista de um no sem WinRM chega."""
    return nlb.view(nome, "10.0.0." + nome[-1],
                    {"reachable": False, "error": "WinRMOperationTimeoutError"}, now)


def ciclos_sem_contato(c, st, inicio, n, alvo="n1"):
    """`n` coletas seguidas com `alvo` mudo e os outros dois saudaveis."""
    d = []
    for i in range(n):
        t = inicio + i * MIN
        vistas = [mudo(alvo, t) if x == alvo else no(x, users=17, sess=8, now=t)
                  for x in ("n1", "n2", "n3")]
        d, st = rodar(vistas, st, c, t)
        if i < n - 1:
            assert d == [], f"decidiu cedo, na coleta {i + 1}: {d}"
    return d, st


def test_sem_contato_drena_pelo_vizinho_na_terceira_coleta():
    c = cfg(mode="auto")
    _, st = rodar([no("n1")] + saudaveis(), {}, c, T0)        # sabe que n1 e o host 1
    d, st = ciclos_sem_contato(c, st, T0 + MIN, 3)
    assert kinds(d) == ["drain_unreachable"] and d[0]["action"] == "drain_remote"
    assert d[0]["server"] == "n1" and d[0]["target"] == "vip:1"
    assert d[0]["relays"] == ["n2", "n3"]
    assert "sem contato" in d[0]["msg"] and d[0]["notify"]


def test_sem_contato_respeita_a_janela_configurada():
    c = cfg(mode="auto", unreachable_cycles=5)
    _, st = rodar([no("n1")] + saudaveis(), {}, c, T0)
    d, _ = ciclos_sem_contato(c, st, T0 + MIN, 5)
    assert kinds(d) == ["drain_unreachable"]


def test_sem_contato_no_modo_alerta_so_avisa_uma_vez():
    c = cfg()
    _, st = rodar([no("n1")] + saudaveis(), {}, c, T0)
    d, st = ciclos_sem_contato(c, st, T0 + MIN, 3)
    assert kinds(d) == ["would_drain"] and d[0]["action"] is None
    assert "sem contato" in d[0]["msg"]
    d, st = rodar([mudo("n1", T0 + 5 * MIN)] + saudaveis(T0 + 5 * MIN), st, c, T0 + 5 * MIN)
    assert d == []


def test_volta_do_contato_zera_a_contagem():
    c = cfg(mode="auto")
    _, st = rodar([no("n1")] + saudaveis(), {}, c, T0)
    _, st = rodar([mudo("n1", T0 + MIN)] + saudaveis(), st, c, T0 + MIN)
    _, st = rodar([mudo("n1", T0 + 2 * MIN)] + saudaveis(), st, c, T0 + 2 * MIN)
    _, st = rodar([no("n1")] + saudaveis(), st, c, T0 + 3 * MIN)   # um engasgo so
    d, st = rodar([mudo("n1", T0 + 4 * MIN)] + saudaveis(), st, c, T0 + 4 * MIN)
    assert d == [] and st["nodes"]["n1"]["unreach"] == 1


def test_sem_contato_que_ja_estava_fora_do_nlb_nao_e_drenado():
    c = cfg(mode="auto")
    v = no("n1", status=1005, users=0, auth=0, sess=0)
    _, st = rodar([v] + saudaveis(), {}, c, T0)
    d, _ = ciclos_sem_contato(c, st, T0 + MIN, 4)
    assert d == []


def test_sem_contato_sem_host_id_conhecido_avisa_que_nao_da():
    """Estado anterior ao RMon guardar o host_id: avisa, nao chuta o alvo."""
    c = cfg(mode="auto")
    st = {"nodes": {"n1": {"last_status": 1007, "cluster": "vip"}}}
    d, _ = ciclos_sem_contato(c, st, T0, 3)
    assert kinds(d) == ["blocked"] and "host_id" in d[0]["msg"]


def test_sem_contato_em_manutencao_nao_e_tocado():
    c = cfg(mode="auto", hold=["n1"])
    _, st = rodar([no("n1")] + saudaveis(), {}, c, T0)
    d, _ = ciclos_sem_contato(c, st, T0 + MIN, 4)
    assert d == []


def test_dois_sem_contato_nenhum_e_drenado_pelo_piso():
    """Sobraria 1 no ativo: o problema pode ser a rede do RMon, nao os nos."""
    c = cfg(mode="auto")
    _, st = rodar([no("n1"), no("n2"), no("n3")], {}, c, T0)
    d = []
    for i in range(3):
        t = T0 + (i + 1) * MIN
        d, st = rodar([mudo("n1", t), mudo("n2", t), no("n3", users=17, sess=8, now=t)], st, c, t)
    assert kinds(d) == ["blocked", "blocked"]
    assert all("deixaria 1 no(s) ativo(s)" in x["msg"] for x in d)


def test_sem_contato_nao_depende_de_cooldown_nem_de_flapping():
    c = cfg(mode="auto")
    _, st = rodar([no("n1")] + saudaveis(), {}, c, T0)
    st["clusters"] = {"vip": {"last_auto": T0}}
    st["nodes"]["n1"]["drains"] = [T0 - 3600, T0 - 7200]
    d, _ = ciclos_sem_contato(c, st, T0 + MIN, 3)
    assert kinds(d) == ["drain_unreachable"]


def test_drenado_por_falta_de_contato_volta_sem_exigir_reboot():
    c = cfg(mode="auto")
    _, st = rodar([no("n1")] + saudaveis(), {}, c, T0)
    d, st = ciclos_sem_contato(c, st, T0 + MIN, 3)
    t = T0 + 3 * MIN
    st, _ = nlb.apply_result(st, d[0], True, t, "via n2: 1007 -> 1009")
    n1 = st["nodes"]["n1"]
    assert n1["owned"] and n1["drain_reason"] == "unreachable" and n1["last_status"] == 1009
    # continua mudo: nada de novo
    d, st = rodar([mudo("n1", t + MIN)] + saudaveis(t + MIN), st, c, t + MIN)
    assert d == []
    # voltou sem reiniciar, drenando e apto: espera os 10 min e reingressa
    t2 = t + 20 * MIN
    volta = no("n1", status=1009, users=0, auth=0, sess=0, uptime_min=3000, commit=20, now=t2)
    d, st = rodar([volta] + saudaveis(t2), st, c, t2)
    assert d == [] and st["nodes"]["n1"]["rejoin_gaps"] == []
    t3 = t2 + 10 * MIN
    volta = no("n1", status=1009, users=0, auth=0, sess=0, uptime_min=3010, commit=20, now=t3)
    d, st = rodar([volta] + saudaveis(t3), st, c, t3)
    assert kinds(d) == ["rejoin"]


def test_drenagem_remota_que_falhou_tenta_de_novo_e_avisa_uma_vez():
    c = cfg(mode="auto")
    _, st = rodar([no("n1")] + saudaveis(), {}, c, T0)
    d, st = ciclos_sem_contato(c, st, T0 + MIN, 3)
    t = T0 + 3 * MIN
    st, nova = nlb.apply_result(st, d[0], False, t, "controle remoto desligado")
    assert nova
    d, st = rodar([mudo("n1", t + MIN)] + saudaveis(t + MIN), st, c, t + MIN)
    assert d == []                                     # dentro do retry_min
    d, st = rodar([mudo("n1", t + 5 * MIN)] + saudaveis(t + 5 * MIN), st, c, t + 5 * MIN)
    assert kinds(d) == ["drain_unreachable"]
    st, nova = nlb.apply_result(st, d[0], False, t + 5 * MIN, "de novo")
    assert not nova


def test_vizinho_saudavel_e_tentado_primeiro():
    c = cfg(mode="auto")
    _, st = rodar([no("n1")] + saudaveis(), {}, c, T0)
    d = []
    for i in range(3):
        t = T0 + (i + 1) * MIN
        d, st = rodar([mudo("n1", t), no("n2", rm=False, now=t), no("n3", users=17, sess=8, now=t)],
                      st, c, t)
    assert d[0]["relays"] == ["n3", "n2"]


# ------------------------------------------------------------- cluster vazio (ClusterModeOnStart=0)

def test_cluster_vazio_reingressa_o_primeiro_apto_sem_esperar():
    """Queda de energia: os tres sobem parados. Nao da para esperar 20 min."""
    c = cfg(mode="auto", rejoin_unowned=True)
    vistas = [no(x, status=1005, users=0, auth=0, sess=0, uptime_min=4, commit=10)
              for x in ("n1", "n2", "n3")]
    d, st = rodar(vistas, {}, c, T0)
    assert kinds(d) == ["rejoin"] and len(d) == 1


def test_cluster_vazio_ainda_exige_rm_host_no_ar():
    c = cfg(mode="auto", rejoin_unowned=True)
    vistas = [no(x, status=1005, users=0, auth=0, sess=0, uptime_min=4, commit=10, rm=False)
              for x in ("n1", "n2", "n3")]
    d, _ = rodar(vistas, {}, c, T0)
    assert d == []


def test_reboot_com_outros_no_ar_espera_o_normal():
    c = cfg(mode="auto", rejoin_unowned=True)
    v = no("n1", status=1005, users=0, auth=0, sess=0, uptime_min=4, commit=10)
    d, _ = rodar([v] + saudaveis(), {}, c, T0)
    assert d == []


# ------------------------------------------------------------- drenagem pelo vizinho (collector)

def test_alvo_da_drenagem_remota_e_validado():
    from app import collector
    assert collector.nlb_target("10.10.10.228:5") == ("10.10.10.228", 5)
    for ruim in ("", "10.10.10.228", "vip:1", "10.10.10.228:0", "10.10.10.228:5'; rm", "1.2.3.4:x"):
        assert collector.nlb_target(ruim) is None, ruim


def test_interpretacao_da_drenagem_remota():
    from app.collector import interpretar_drenagem_remota as ler
    assert ler({"via": "wmi", "before": 1007, "after": 1009, "members_after": [1, 2, 5]}, 5)[0]
    ok, msg = ler({"via": "wmi", "rc": 1000, "after": None, "members_after": [1, 2]}, 5)
    assert ok and "fora da convergencia" in msg
    ok, msg = ler({"via": "wmi", "rc": 1000, "after": None, "members_after": [1, 2, 5]}, 5)
    assert not ok and "sem confirmacao" in msg
    assert ler({"via": "ja-fora", "before": 1005}, 5)[0]
    assert ler({"via": "fora-da-convergencia", "members_before": [1, 2]}, 5)[0]
    ok, msg = ler({"via": None, "output": "o WMI deste no nao enxerga o estado do alvo"}, 5)
    assert not ok and "nao enxerga" in msg


def test_drenagem_remota_tenta_o_segundo_vizinho(monkeypatch):
    from app import collector
    chamados = []

    def falso(relay, wc, target):
        chamados.append(relay.name)
        return (relay.name == "n3"), f"resposta de {relay.name}"

    monkeypatch.setattr(collector, "nlb_remote_drain", falso)

    class S:
        def __init__(self, nome):
            self.name = nome

    servidores = {x: S(x) for x in ("n1", "n2", "n3")}
    ok, det = nlb.execute_remote(servidores, None, {"target": "vip:1", "relays": ["n2", "n3"]})
    assert ok and det.startswith("via n3") and chamados == ["n2", "n3"]
    ok, det = nlb.execute_remote(servidores, None, {"target": "vip:1", "relays": []})
    assert not ok and "nenhum vizinho" in det
