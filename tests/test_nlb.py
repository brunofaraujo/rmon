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
    return nlb.view(nome, "10.0.0." + nome[-1], r, now, incus_vm="vm-" + nome,
                    es={"ok": True, "ts": now})


def cfg(**kw):
    """Canal do Incus configurado por padrao; cada no e a VM "vm-<nome>"."""
    return nlb.settings_for({"nlb": {"incus_ssh": "rmon-reset@hv", **kw}})


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
    assert any("aceita o login mas não abre a sessão" in m for m in motivos)


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
    assert any("nenhum usuário aqui" in m for m in motivos)


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
    assert any("memória reservada" in m for m in motivos)


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
    assert any("não reiniciou" in g for g in st["nodes"]["n1"]["rejoin_gaps"])

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
    assert any("RM.Host" in f for f in falta) and any("memória reservada" in f for f in falta)


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
    assert "deixaria só 1 servidor(es)" in d[0]["msg"]


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
    assert kinds(d) == ["blocked"] and "intervalo mínimo" in d[0]["msg"]


def test_flapping_vira_so_aviso():
    c = cfg(mode="auto")
    st = {"nodes": {"n1": {"bad_since": T0, "drains": [T0 - 3600, T0 - 7200]}}}
    t = T0 + 20 * MIN
    d, _ = rodar(doente(t), st, c, t)
    assert kinds(d) == ["blocked"] and "instável" in d[0]["msg"]


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
    assert kinds(d) == ["out_unowned"] and "já está pronto para voltar" in d[0]["msg"]
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
    assert not ok and "sobrariam só 1" in motivo
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
# .188 em 16/10: parou de responder ao WinRM as 10:01 (o Elastic ficou mudo no
# mesmo minuto) e voltou ao NLB sozinho as 10:05:58, sem estar pronto; ate 10:16
# recebeu usuarios sem criar sessao nenhuma. Um vizinho nao consegue drena-lo no
# Server 2016: a saida e reiniciar a VM pelo Incus.

def mudo(nome, now=T0, es_ok=True, es_ts=None, vm=True):
    """Coleta que falhou: e assim que a vista de um no sem WinRM chega.

    Por padrao o Elastic respondeu e nao tem nada do host em 15 min."""
    return nlb.view(nome, "10.0.0." + nome[-1],
                    {"reachable": False, "error": "WinRMOperationTimeoutError"}, now,
                    incus_vm=("vm-" + nome) if vm else "", es={"ok": es_ok, "ts": es_ts})


def ciclos_sem_contato(c, st, inicio, n, alvo="n1", **kw):
    """`n` coletas seguidas com `alvo` mudo e os outros dois saudaveis."""
    d = []
    for i in range(n):
        t = inicio + i * MIN
        vistas = [mudo(alvo, t, **kw) if x == alvo else no(x, users=17, sess=8, now=t)
                  for x in ("n1", "n2", "n3")]
        d, st = rodar(vistas, st, c, t)
        if i < n - 1:
            assert d == [], f"decidiu cedo, na coleta {i + 1}: {d}"
    return d, st


def conhecido(c):
    """Uma coleta boa antes: o RMon sabe o cluster e viu o host no Elastic em T0."""
    _, st = rodar([no("n1")] + saudaveis(), {}, c, T0)
    return st


def test_sem_contato_e_elastic_mudo_reinicia_a_vm_na_terceira_coleta():
    c = cfg(mode="auto")
    d, st = ciclos_sem_contato(c, conhecido(c), T0 + MIN, 3)
    assert kinds(d) == ["reset_unreachable"] and d[0]["action"] == "reset"
    assert d[0]["server"] == "n1" and d[0]["vm"] == "vm-n1" and d[0]["notify"]
    assert "sem contato" in d[0]["msg"] and "nada há 3min" in d[0]["msg"]


def test_sem_contato_respeita_a_janela_configurada():
    c = cfg(mode="auto", unreachable_cycles=5)
    d, _ = ciclos_sem_contato(c, conhecido(c), T0 + MIN, 5)
    assert kinds(d) == ["reset_unreachable"]


def test_elastic_ainda_recebendo_dados_nao_reinicia():
    """So o WinRM travou (engasgo do WinRM nos app servers RM): o Windows esta de pe."""
    c = cfg(mode="auto")
    st = conhecido(c)
    d = []
    for i in range(4):
        t = T0 + (i + 1) * MIN
        d_i, st = rodar([mudo("n1", t, es_ts=t - 20)] + saudaveis(t), st, c, t)
        d += d_i
    assert kinds(d) == ["unreachable_alive"] and d[0]["action"] is None
    assert "só o WinRM travou" in d[0]["msg"]


def test_elastic_fora_do_ar_nao_autoriza_reset():
    c = cfg(mode="auto")
    d, _ = ciclos_sem_contato(c, conhecido(c), T0 + MIN, 3, es_ok=False)
    assert kinds(d) == ["unreachable_crit"] and d[0]["action"] is None
    assert "sem a confirmação do Elastic" in d[0]["msg"]


def test_host_que_o_elastic_nunca_viu_nao_e_reiniciado():
    """Nome errado no Elastic ou agente nunca instalado: "sem dados" nao prova nada."""
    c = cfg(mode="auto")
    st = {"nodes": {"n1": {"last_status": 1007, "cluster": "vip", "host_id": 1}}}
    d, _ = ciclos_sem_contato(c, st, T0, 3)
    assert kinds(d) == ["unreachable_crit"] and "nunca recebeu dados" in d[0]["msg"]


def test_servidor_fisico_so_gera_alerta_critico():
    """.34 (HPE fisico, iLO sem rede): nao ha como reinicia-lo."""
    c = cfg(mode="auto")
    d, st = ciclos_sem_contato(c, conhecido(c), T0 + MIN, 3, vm=False)
    assert kinds(d) == ["unreachable_crit"] and "servidor físico" in d[0]["msg"]
    t = T0 + 4 * MIN
    d, _ = rodar([mudo("n1", t, vm=False)] + saudaveis(t), st, c, t)
    assert d == []                                       # avisa uma vez por episodio


def test_sem_canal_do_incus_so_gera_alerta_critico():
    c = cfg(mode="auto", incus_ssh="")
    d, _ = ciclos_sem_contato(c, conhecido(c), T0 + MIN, 3)
    assert kinds(d) == ["unreachable_crit"] and "sem hypervisor" in d[0]["msg"]


def test_limite_de_resets_por_dia():
    c = cfg(mode="auto")
    st = conhecido(c)
    st["nodes"]["n1"]["resets"] = [T0 - 3600, T0 - 7200]
    d, _ = ciclos_sem_contato(c, st, T0 + MIN, 3)
    assert kinds(d) == ["unreachable_crit"] and "2x em 24h" in d[0]["msg"]
    # reset de mais de 24h atras nao conta
    st = conhecido(c)
    st["nodes"]["n1"]["resets"] = [T0 - 90000]
    d, _ = ciclos_sem_contato(c, st, T0 + MIN, 3)
    assert kinds(d) == ["reset_unreachable"]


def test_sem_contato_no_modo_alerta_so_avisa_uma_vez():
    c = cfg()
    d, st = ciclos_sem_contato(c, conhecido(c), T0 + MIN, 3)
    assert kinds(d) == ["would_reset"] and d[0]["action"] is None
    assert "reiniciaria a VM vm-n1" in d[0]["msg"]
    t = T0 + 5 * MIN
    d, st = rodar([mudo("n1", t)] + saudaveis(t), st, c, t)
    assert d == []


def test_volta_do_contato_zera_a_contagem():
    c = cfg(mode="auto")
    st = conhecido(c)
    _, st = rodar([mudo("n1", T0 + MIN)] + saudaveis(), st, c, T0 + MIN)
    _, st = rodar([mudo("n1", T0 + 2 * MIN)] + saudaveis(), st, c, T0 + 2 * MIN)
    _, st = rodar([no("n1", now=T0 + 3 * MIN)] + saudaveis(), st, c, T0 + 3 * MIN)   # um engasgo so
    d, st = rodar([mudo("n1", T0 + 4 * MIN)] + saudaveis(), st, c, T0 + 4 * MIN)
    assert d == [] and st["nodes"]["n1"]["unreach"] == 1


def test_sem_contato_que_ja_estava_fora_do_nlb_nao_e_tocado():
    c = cfg(mode="auto")
    v = no("n1", status=1005, users=0, auth=0, sess=0)
    _, st = rodar([v] + saudaveis(), {}, c, T0)
    d, _ = ciclos_sem_contato(c, st, T0 + MIN, 4)
    assert d == []


def test_sem_contato_em_manutencao_nao_e_tocado():
    c = cfg(mode="auto", hold=["n1"])
    d, _ = ciclos_sem_contato(c, conhecido(c), T0 + MIN, 4)
    assert d == []


def test_dois_sem_contato_nenhum_e_reiniciado_pelo_piso():
    """Sobraria 1 no ativo: o problema pode ser a rede do RMon, nao os nos."""
    c = cfg(mode="auto")
    _, st = rodar([no("n1"), no("n2"), no("n3")], {}, c, T0)
    d = []
    for i in range(3):
        t = T0 + (i + 1) * MIN
        d, st = rodar([mudo("n1", t), mudo("n2", t), no("n3", users=17, sess=8, now=t)], st, c, t)
    assert kinds(d) == ["blocked", "blocked"]
    assert all("deixaria só 1 servidor(es)" in x["msg"] for x in d)


def test_sem_contato_nao_depende_de_cooldown_nem_de_flapping_da_drenagem():
    c = cfg(mode="auto")
    st = conhecido(c)
    st["clusters"] = {"vip": {"last_auto": T0}}
    st["nodes"]["n1"]["drains"] = [T0 - 3600, T0 - 7200]
    d, _ = ciclos_sem_contato(c, st, T0 + MIN, 3)
    assert kinds(d) == ["reset_unreachable"]


def test_vm_reiniciada_volta_fora_do_nlb_e_e_reingressada():
    c = cfg(mode="auto")
    d, st = ciclos_sem_contato(c, conhecido(c), T0 + MIN, 3)
    t = T0 + 3 * MIN
    st, _ = nlb.apply_result(st, d[0], True, t, "vm-n1 reiniciada")
    n1 = st["nodes"]["n1"]
    assert n1["owned"] and n1["drain_reason"] == "reset" and n1["last_status"] == 1005
    assert n1["resets"] == [t]
    # bootando, ainda mudo: nada de novo (nao reinicia de novo)
    for k in range(1, 6):
        d, st = rodar([mudo("n1", t + k * MIN)] + saudaveis(t + k * MIN), st, c, t + k * MIN)
        assert d == []
    # subiu parado (ClusterModeOnStart=0): espera o Windows assentar e reingressa
    t2 = t + 8 * MIN
    volta = no("n1", status=1005, users=0, auth=0, sess=0, uptime_min=4, commit=20, now=t2)
    d, st = rodar([volta] + saudaveis(t2), st, c, t2)
    assert d == [] and "ligado há menos de 10 min" in st["nodes"]["n1"]["rejoin_gaps"][0]
    t3 = t2 + 20 * MIN
    volta = no("n1", status=1005, users=0, auth=0, sess=0, uptime_min=24, commit=20, now=t3)
    d, st = rodar([volta] + saudaveis(t3), st, c, t3)
    assert d == [] and st["nodes"]["n1"]["rejoin_gaps"] == []
    t4 = t3 + 10 * MIN
    volta = no("n1", status=1005, users=0, auth=0, sess=0, uptime_min=34, commit=20, now=t4)
    d, st = rodar([volta] + saudaveis(t4), st, c, t4)
    assert kinds(d) == ["rejoin"]


def test_reset_que_falhou_tenta_de_novo_e_avisa_uma_vez():
    c = cfg(mode="auto")
    d, st = ciclos_sem_contato(c, conhecido(c), T0 + MIN, 3)
    t = T0 + 3 * MIN
    st, nova = nlb.apply_result(st, d[0], False, t, "Permission denied (publickey)")
    assert nova and not st["nodes"]["n1"].get("resets")
    d, st = rodar([mudo("n1", t + MIN)] + saudaveis(t + MIN), st, c, t + MIN)
    assert d == []                                     # dentro do retry_min
    d, st = rodar([mudo("n1", t + 5 * MIN)] + saudaveis(t + 5 * MIN), st, c, t + 5 * MIN)
    assert kinds(d) == ["reset_unreachable"]
    st, nova = nlb.apply_result(st, d[0], False, t + 5 * MIN, "de novo")
    assert not nova


def test_relogio_adiantado_do_host_conta_como_vivo():
    """sistemafiep anda ~45 s a frente do Elastic: metrica "do futuro" e fresca."""
    c = cfg(mode="auto")
    st = {"nodes": {"n1": {}}}
    v = mudo("n1", T0, es_ts=T0 + 45)
    assert nlb.elastic_confirma(v, st["nodes"]["n1"], c, T0)[0] == "vivo"


# ------------------------------------------------------------- canal com o Incus (app/incus.py)

def test_lista_do_comando_forcado():
    from app import incus
    vms = incus.ler_lista("WIN2016 RUNNING 10:66:6a:cc:09:90\nWIN2016-2 STOPPED\nlixo\n; rm -rf / x\n")
    assert vms == {"WIN2016": {"estado": "RUNNING", "mac": "10:66:6a:cc:09:90"},
                   "WIN2016-2": {"estado": "STOPPED", "mac": ""}}


def test_checar_aponta_vm_do_inventario_nao_liberada(monkeypatch):
    from app import incus
    monkeypatch.setattr(incus, "_ssh", lambda cfg, args, timeout: (True, "WIN2016 RUNNING m"))
    r = incus.checar({"incus_ssh": "u@h"}, {"n1": "WIN2016", "n2": "WIN2016-2"})
    assert not r["ok"] and "n2 (WIN2016-2)" in r["detail"]
    r = incus.checar({"incus_ssh": "u@h"}, {"n1": "WIN2016"})
    assert r["ok"] and r["vms"]["WIN2016"]["estado"] == "RUNNING"
    monkeypatch.setattr(incus, "_ssh", lambda cfg, args, timeout: (False, "Permission denied"))
    r = incus.checar({"incus_ssh": "u@h"}, {"n1": "WIN2016"})
    assert not r["ok"] and r["detail"] == "Permission denied"


def test_reset_so_aceita_nome_de_instancia(monkeypatch):
    from app import incus
    chamadas = []
    monkeypatch.setattr(incus, "_ssh", lambda cfg, args, timeout: chamadas.append(args) or (True, "ok"))
    for ruim in ("", "a b", "x;reboot", "-rf", "$(id)", "a" * 70):
        ok, msg = incus.reset({"incus_ssh": "u@h"}, ruim)
        assert not ok and "invalida" in msg, ruim
    assert chamadas == []
    assert incus.reset({"incus_ssh": "u@h"}, "WIN2016-2") == (True, "ok")
    assert chamadas == [["reset", "WIN2016-2"]]


def test_ssh_sem_destino_nao_roda_nada():
    from app import incus
    assert incus.reset({"incus_ssh": ""}, "WIN2016") == (False, "defaults.nlb.incus_ssh vazio")


def test_canal_conferido_de_hora_em_hora_e_em_10_min_se_falhou():
    from app import incus
    assert incus.devido(None, T0)
    assert not incus.devido({"ok": True, "ts": T0}, T0 + 3000)
    assert incus.devido({"ok": True, "ts": T0}, T0 + 3600)
    assert incus.devido({"ok": False, "ts": T0}, T0 + 600)


def test_run_cycle_reinicia_pelo_incus_e_registra(monkeypatch):
    """O adaptador: decisao de reset -> incus.reset -> estado, alerta e auditoria."""
    from types import SimpleNamespace

    from app import config, db, incus
    estado = {"nlb_state": conhecido(cfg(mode="auto"))}
    registros, resets = [], []
    monkeypatch.setattr(db, "get_config", lambda k, d=None: estado.get(k, d))
    monkeypatch.setattr(db, "set_config", lambda k, v: estado.__setitem__(k, v))
    monkeypatch.setattr(db, "record_alert", lambda *a: registros.append(a))
    monkeypatch.setattr(db, "audit", lambda *a: registros.append(a))
    monkeypatch.setattr(incus, "reset", lambda c, vm: resets.append(vm) or (True, "reiniciada"))
    monkeypatch.setattr(incus, "checar", lambda c, vms: {"ok": True, "detail": "2", "vms": {}})
    from app import notify
    monkeypatch.setattr(notify, "enabled", lambda: False)
    srv = [config.ServerConfig(name=x, host="10.0.0." + x[-1], nlb=True,
                               incus_vm="vm-" + x if x != "n3" else "") for x in ("n1", "n2", "n3")]
    inv = SimpleNamespace(servers=srv, winrm=None,
                          defaults={"nlb": {"mode": "auto", "incus_ssh": "rmon-reset@hv"}})
    vivo = {"reachable": True, "users_count": 17, "commit_pct": 30, "uptime_sec": 180000,
            "services": [{"name": "RM.Host.Service", "status": "Running"}],
            "nlb": {"status": 1007, "cluster": "vip", "host_id": 2, "auth": 8, "sess": 8}}
    for i in range(3):
        t = T0 + (i + 1) * MIN
        es = {"n1": {"ok": True, "ts": None}, "n2": {"ok": True, "ts": t}, "n3": {"ok": True, "ts": t}}
        d = nlb.run_cycle(inv, {"n1": {"reachable": False}, "n2": vivo,
                                "n3": {**vivo, "nlb": {**vivo["nlb"], "host_id": 3}}}, now=t, es=es)
    assert resets == ["vm-n1"] and kinds(d) == ["reset_unreachable"]
    n1 = estado["nlb_state"]["nodes"]["n1"]
    assert n1["drain_reason"] == "reset" and n1["last_action"]["ok"]
    assert any("VM reiniciada" in str(r) for r in registros)
    assert estado["nlb_state"]["incus"]["ok"]


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


def test_agente_do_elastic_ja_parado_antes_nao_confirma_reset():
    """Agente mudo ha dias + WinRM engasgado agora: o silencio do Elastic nao prova nada."""
    c = cfg(mode="auto")
    st = {"nodes": {"n1": {"last_status": 1007, "cluster": "vip", "host_id": 1,
                           "es_last": T0 - 3 * 86400}}}
    d, _ = ciclos_sem_contato(c, st, T0, 3)
    assert kinds(d) == ["unreachable_crit"] and "já não recebia dados" in d[0]["msg"]
