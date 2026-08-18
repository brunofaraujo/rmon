/* RMonitor - mural de TV.
   Renderiza tudo a partir do JSON de /api/tv e recalcula a grade para caber
   exatamente na tela (sem rolagem). ES5 de proposito: TVs costumam ter
   navegadores antigos. */
(function () {
  "use strict";

  var el = {
    grid: document.getElementById("tvgrid"),
    kpis: document.getElementById("tvkpis"),
    status: document.getElementById("tvstatus"),
    clock: document.getElementById("tvclock"),
    date: document.getElementById("tvdate"),
    ticker: document.getElementById("tvticker"),
    bar: document.getElementById("tvbar"),
    offline: document.getElementById("tvoffline"),
    tv: document.getElementById("tv")
  };

  var state = {
    data: null,
    sig: "",          // assinatura dos dados ja desenhados
    fails: 0,
    refresh: 15,
    issueIx: 0,
    timer: null
  };

  var CARD_RATIO = 1.55;   // proporcao ideal (largura/altura) de um cartao

  // ---------------------------------------------------------------- utils
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function plural(n, um, muitos) { return n === 1 ? um : muitos; }

  function fmtDur(sec) {
    if (!sec) { return "-"; }
    sec = Math.floor(sec);
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    if (d) { return d + "d " + h + "h"; }
    if (h) { return h + "h " + m + "m"; }
    return m + "m";
  }

  function fmtAge(sec) {
    if (sec === null || sec === undefined) { return "-"; }
    if (sec < 90) { return Math.max(0, Math.round(sec)) + "s"; }
    if (sec < 5400) { return Math.round(sec / 60) + "min"; }
    return Math.round(sec / 3600) + "h";
  }

  function fmtClock(ts) {
    if (!ts) { return "-"; }
    var d = new Date(ts * 1000);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function lvl(pct, limite) {
    if (pct >= limite) { return "crit"; }
    if (pct >= Math.max(60, limite - 15)) { return "warn"; }
    return "";
  }

  // ------------------------------------------------------------- cartoes
  function barHTML(rotulo, pct, limite) {
    if (pct === null || pct === undefined) { pct = 0; }
    var c = lvl(pct, limite);
    return '<div class="tvb"><div class="l"><span>' + rotulo + '</span><b>' + pct + '%</b></div>' +
      '<div class="t"><i class="' + c + '" style="width:' + Math.max(0, Math.min(100, pct)) + '%"></i></div></div>';
  }

  /* Servicos do RM compartilham o mesmo prefixo ("RM.Host.Service",
     "RM.Host.Cleanner"...). Mostrar o prefixo uma vez so e listar o resto
     abreviado faz caber muito mais nome na mesma faixa da TV. */
  function prefixoComum(svcs) {
    var cont = {};
    var i, nm, corte, pre;
    for (i = 0; i < svcs.length; i++) {
      if (!svcs[i].ok) { continue; }
      nm = String(svcs[i].n || "");
      corte = Math.max(nm.lastIndexOf("."), Math.max(nm.lastIndexOf("_"), nm.lastIndexOf("-")));
      if (corte < 3) { continue; }
      pre = nm.slice(0, corte + 1);
      cont[pre] = (cont[pre] || 0) + 1;
    }
    var melhor = "", n = 0;
    for (pre in cont) {
      if (cont.hasOwnProperty(pre) && (cont[pre] > n || (cont[pre] === n && pre.length > melhor.length))) {
        melhor = pre; n = cont[pre];
      }
    }
    return n >= 2 ? melhor : "";
  }

  /* Ordem: o que exige acao primeiro (servico parado, app, disco cheio),
     depois o que e so informacao. O corte por espaco acontece em cortaTags(). */
  function tagsHTML(s, th) {
    var t = [];
    var i;
    for (i = 0; i < s.svcs.length; i++) {
      var sv = s.svcs[i];
      if (!sv.ok) {
        t.push('<span class="tv-tag bad"><span class="d"></span>' + esc(sv.n) + " " +
          esc((sv.st || "").toLowerCase() === "not_found" ? "ausente" : "parado") + "</span>");
      }
    }
    if (s.app === false) { t.push('<span class="tv-tag bad">App fora do ar</span>'); }
    for (i = 0; i < s.disks.length; i++) {
      var dk = s.disks[i];
      if (dk.p >= th.disk - 12) {
        t.push('<span class="tv-tag ' + (dk.p >= th.disk ? "bad" : "warn") + '">' + esc(dk.d) + " " + dk.p + "%" +
          (dk.free !== null && dk.free !== undefined ? " &middot; " + dk.free + "GB" : "") + "</span>");
      }
    }
    if (s.stale) { t.push('<span class="tv-tag warn">coleta ha ' + fmtAge(s.age) + "</span>"); }
    if (s.events) {
      t.push('<span class="tv-tag warn">' + s.events + " " + plural(s.events, "ocorrencia", "ocorrencias") + "</span>");
    }
    var pre = prefixoComum(s.svcs);
    var marcou = false;
    for (i = 0; i < s.svcs.length; i++) {
      if (s.svcs[i].ok) {
        var nm = String(s.svcs[i].n || "");
        var curto = pre && nm.length > pre.length && nm.slice(0, pre.length) === pre;
        // nomes fora do grupo ficam inteiros: "W3SVC" nao vira sobra de "RM.Host."
        if (curto && !marcou) {
          t.push('<span class="tv-tag pref">' + esc(pre) + "*</span>");
          marcou = true;
        }
        t.push('<span class="tv-tag good"><span class="d"></span>' +
          esc(curto ? nm.slice(pre.length) : nm) + "</span>");
      }
    }
    if (s.app === true) {
      var lento = s.app_ms !== null && s.app_ms > th.app_ms_limit;
      t.push('<span class="tv-tag ' + (lento ? "warn" : "good") + '">App ' +
        (s.app_ms !== null && s.app_ms !== undefined ? s.app_ms + "ms" : "OK") + "</span>");
    }
    for (i = 0; i < s.disks.length; i++) {
      if (s.disks[i].p < th.disk - 12) {
        t.push('<span class="tv-tag">' + esc(s.disks[i].d) + " " + s.disks[i].p + "%" +
          (s.disks[i].free !== null && s.disks[i].free !== undefined ? " &middot; " + s.disks[i].free + "GB" : "") +
          "</span>");
      }
    }
    return t.join("");
  }

  /* Jobs e usuarios vao para o rodape: sao contexto da aplicacao, nao saude do
     servidor. Job que sai com erro quase sempre e validacao/regra de negocio,
     entao nunca pinta o cartao de vermelho - no maximo fica em ambar. */
  function rodapeHTML(s) {
    var t = [];
    if (s.jobs) {
      if (s.jobs.err) { t.push('<span class="jb warn">jobs: erro SQL</span>'); }
      else {
        var falhou = s.jobs.failed > 0;
        t.push('<span class="jb' + (falhou ? " warn" : "") + '">jobs ' + s.jobs.win + "min: " +
          s.jobs.ok + " ok" + (falhou ? " &middot; " + s.jobs.failed + " com erro" : "") + "</span>");
      }
    }
    if (s.users !== null && s.users !== undefined) {
      t.push("<span>" + s.users + " " + plural(s.users, "usuario", "usuarios") + "</span>");
    }
    return t.join("");
  }

  function cardHTML(s, th, mostraTags) {
    // cartao normal nao ganha etiqueta: o ponto verde ja diz "online" e a
    // largura toda fica para o hostname. Etiqueta so quando ha o que avisar.
    var estado = s.sev === 2 ? "atencao" : (s.sev === 1 ? "aviso" : "");
    var nome = String(s.name || "");
    var cls = nome.length > 22 ? " extenso" : (nome.length > 15 ? " longo" : "");
    function head(pill) {
      return '<div class="tvc-hd"><span class="d"></span>' +
        '<div class="tvc-id"><span class="n' + cls + '">' + esc(nome) + "</span>" +
        '<span class="ip">' + esc(s.host) + "</span></div>" +
        (pill ? '<span class="s">' + pill + "</span>" : "") + "</div>";
    }

    if (!s.up) {
      // o bloco vermelho ja anuncia o estado: etiqueta so repetiria
      return '<article class="tvc sev2">' + head("") +
        '<div class="tvc-down"><b>SEM CONTATO</b><span>' + esc(s.err || "servidor inacessivel") + "</span></div>" +
        '<div class="tvc-ft alerta"><span>ultima coleta ' + (s.ts ? "ha " + fmtAge(s.age) : "-") + "</span></div></article>";
    }

    var rod = rodapeHTML(s);
    // no cartao apertado o rodape so sobrevive se tiver algo a dizer
    var alerta = s.jobs && (s.jobs.err || s.jobs.failed > 0) ? " alerta" : "";
    return '<article class="tvc sev' + s.sev + '">' + head(estado) +
      '<div class="tvc-bars">' + barHTML("CPU", s.cpu, 90) + barHTML("MEM", s.mem, th.mem) + "</div>" +
      (mostraTags ? '<div class="tvc-tags">' + tagsHTML(s, th) + "</div>" : "") +
      '<div class="tvc-ft' + alerta + '"><span class="up">uptime ' + fmtDur(s.uptime) + "</span>" +
      '<span class="jobs">' + rod + "</span>" +
      '<span class="hr">' + fmtClock(s.ts) + "</span></div>" +
      "</article>";
  }

  /* Etiquetas cortadas ao meio ficam ilegiveis na TV. Depois de desenhar, some
     com as que nao couberam inteiras e resume o resto num chip "+N". */
  function cortaTags() {
    // o chip do prefixo nao e um item de verdade: nao entra na conta do "+N"
    function ocultas(kids, ate) {
      var q = 0;
      for (var k = ate; k < kids.length; k++) {
        if (kids[k].className.indexOf("pref") < 0) { q++; }
      }
      return q;
    }

    var conts = el.grid.getElementsByClassName("tvc-tags");
    for (var i = 0; i < conts.length; i++) {
      var c = conts[i];
      var lim = c.getBoundingClientRect().bottom + 1;
      var kids = [];
      var j;
      for (j = 0; j < c.children.length; j++) { kids.push(c.children[j]); }
      var vis = kids.length;
      for (j = 0; j < kids.length; j++) {
        if (kids[j].getBoundingClientRect().bottom > lim) { vis = j; break; }
      }
      if (vis >= kids.length) { continue; }

      var chip = document.createElement("span");
      chip.className = "tv-tag mais";
      c.appendChild(chip);
      while (vis > 0) {
        // "RM.Host.*" sozinho no fim nao diz nada: some junto com os servicos
        while (vis > 0 && kids[vis - 1].className.indexOf("pref") >= 0) { vis--; }
        if (!vis) { break; }
        for (j = 0; j < kids.length; j++) { kids[j].style.display = j < vis ? "" : "none"; }
        chip.textContent = "+" + ocultas(kids, vis);
        if (chip.getBoundingClientRect().bottom <= lim) { break; }
        vis--;
      }
      if (vis === 0) {
        for (j = 0; j < kids.length; j++) { kids[j].style.display = "none"; }
        chip.textContent = "+" + ocultas(kids, 0);
      }
    }
  }

  // -------------------------------------------------------------- layout
  /* Escolhe colunas x linhas maximizando o tamanho da letra (o que decide a
     leitura a 5 metros de distancia), penalizando celulas vazias e cartoes
     muito desproporcionais. Como a grade ocupa uma altura fixa, isso garante
     que tudo cabe na tela sem rolagem. */
  function escala(w, h) {
    var teto = Math.max(16, Math.min(64, window.innerHeight * 0.05));
    return Math.max(9, Math.min(teto, Math.min(h * 0.108, w * 0.052)));
  }

  function layout() {
    var n = state.data ? state.data.servers.length : 0;
    if (!n) { return true; }
    var box = el.grid.getBoundingClientRect();
    if (box.width < 10 || box.height < 10) { return true; }
    var gap = parseFloat(getComputedStyle(el.grid).columnGap) || 10;

    var best = null;
    for (var c = 1; c <= n; c++) {
      var r = Math.ceil(n / c);
      var w = (box.width - gap * (c - 1)) / c;
      var h = (box.height - gap * (r - 1)) / r;
      if (w < 90 || h < 52) { continue; }
      var esc = escala(w, h);
      var buracos = 1 - n / (c * r);
      var desvio = Math.abs(Math.log((w / h) / CARD_RATIO));
      var score = -Math.log(esc) + buracos * 0.4 + desvio * 0.25;
      if (!best || score < best.score) {
        best = { score: score, c: c, r: r, h: h, esc: esc };
      }
    }
    if (!best) {                      // grade minuscula: cai no quadrado
      var c0 = Math.ceil(Math.sqrt(n));
      var r0 = Math.ceil(n / c0);
      best = { c: c0, r: r0, h: box.height / r0, esc: escala(box.width / c0, box.height / r0) };
    }

    el.grid.style.gridTemplateColumns = "repeat(" + best.c + ",minmax(0,1fr))";
    el.grid.style.gridTemplateRows = "repeat(" + best.r + ",minmax(0,1fr))";
    el.grid.style.setProperty("--cs", best.esc.toFixed(2) + "px");

    /* Em cartao apertado a lista de servicos vale mais que uptime/relogio:
       o modo compacto entra cedo e libera a faixa de etiquetas. */
    var micro = best.h < 132;
    var compact = !micro && best.h < 245;
    el.grid.className = "tv-grid" + (micro ? " t-micro" : (compact ? " t-compact" : ""));
    return !micro;
  }

  // --------------------------------------------------------------- render
  function renderStatus(d) {
    var s = d.summary;
    var cls, txt;
    if (s.crit) { cls = "crit"; txt = s.crit + " " + plural(s.crit, "SERVIDOR EM FALHA", "SERVIDORES EM FALHA"); }
    else if (s.warn) { cls = "warn"; txt = s.warn + " " + plural(s.warn, "SERVIDOR EM ATENCAO", "SERVIDORES EM ATENCAO"); }
    else { cls = "ok"; txt = "TUDO OPERACIONAL"; }
    el.status.className = "tv-status " + cls;
    el.status.innerHTML = '<span class="ring"></span><b>' + txt + "</b>" +
      "<small>" + s.online + "/" + s.total + " online &middot; coleta a cada " + d.poll + "s" +
      " &middot; atualizado " + fmtClock(d.ts) + "</small>";
  }

  function kpi(cls, valor, rotulo) {
    return '<div class="tv-kpi ' + cls + '"><div class="v">' + valor + '</div><div class="k">' + rotulo + "</div></div>";
  }

  function renderKpis(d) {
    var s = d.summary;
    el.kpis.innerHTML =
      kpi("", s.total, "Servidores") +
      kpi(s.online === s.total ? "ok" : "", s.online, "Online") +
      kpi(s.offline ? "bad" : "ok", s.offline, "Sem contato") +
      kpi(s.services_down ? "bad" : "ok", s.services_down, "Servicos parados") +
      kpi(s.events ? "warn" : "ok", s.events, "Ocorrencias 24h");
  }

  function renderTicker() {
    var d = state.data;
    if (!d) { return; }
    var msg = el.ticker.querySelector(".msg");
    var cnt = el.ticker.querySelector(".cnt");
    var ico = el.ticker.querySelector(".ico");
    var lista = d.issues;

    if (!lista.length) {
      el.ticker.className = "tv-ticker ok";
      ico.textContent = "✔";
      msg.innerHTML = "Todos os servicos monitorados estao operacionais.";
      cnt.textContent = "";
      return;
    }
    if (state.issueIx >= lista.length) { state.issueIx = 0; }
    var it = lista[state.issueIx];
    el.ticker.className = "tv-ticker " + (it.sev === 2 ? "crit" : "warn");
    ico.textContent = it.sev === 2 ? "●" : "⚠";
    msg.innerHTML = "<b>" + esc(it.server) + "</b> &mdash; " + esc(it.text);
    cnt.textContent = (state.issueIx + 1) + "/" + lista.length;
  }

  function rodaTicker() {
    var d = state.data;
    if (!d || d.issues.length < 2) { renderTicker(); return; }
    var msg = el.ticker.querySelector(".msg");
    msg.className = "msg fade";
    setTimeout(function () {
      state.issueIx = (state.issueIx + 1) % d.issues.length;
      renderTicker();
      msg.className = "msg";
    }, 350);
  }

  function render(d) {
    state.data = d;
    state.refresh = Math.max(5, d.refresh || 15);
    renderStatus(d);
    renderKpis(d);

    var sig = JSON.stringify(d.servers, function (k, v) { return k === "age" ? undefined : v; });
    var mudou = sig !== state.sig;
    state.sig = sig;

    if (mudou) {
      var mostraTags = layout();
      var th = {
        mem: d.thresholds ? d.thresholds.mem : 90,
        disk: d.thresholds ? d.thresholds.disk : 90,
        app_ms_limit: d.thresholds ? d.thresholds.app_ms : 3000
      };
      if (!d.servers.length) {
        el.grid.innerHTML = '<div class="tv-empty">Nenhum servidor no inventario.</div>';
      } else {
        var html = "";
        for (var i = 0; i < d.servers.length; i++) { html += cardHTML(d.servers[i], th, mostraTags); }
        el.grid.innerHTML = html;
        cortaTags();
      }
      renderTicker();
    }
  }

  // ------------------------------------------------------------- carga
  function progresso(seg) {
    if (!el.bar) { return; }
    el.bar.className = "";
    el.bar.style.animationDuration = seg + "s";
    void el.bar.offsetWidth;      // reinicia a animacao
    el.bar.className = "run";
  }

  function agenda(seg) {
    if (state.timer) { clearTimeout(state.timer); }
    progresso(seg);
    state.timer = setTimeout(carrega, seg * 1000);
  }

  function carrega() {
    if (document.hidden) { agenda(state.refresh); return; }   // TV apagada: nao insiste
    fetch("/api/tv", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (r.status === 401 || r.status === 403) { location.href = "/login"; return null; }
        if (!r.ok) { throw new Error("http " + r.status); }
        return r.json();
      })
      .then(function (d) {
        if (!d) { return; }
        state.fails = 0;
        if (el.offline) { el.offline.hidden = true; }
        render(d);
        agenda(state.refresh);
      })
      .catch(function () {
        state.fails++;
        if (el.offline && state.fails >= 2) { el.offline.hidden = false; }
        // recuo exponencial ate 60s para nao martelar um servidor fora do ar
        agenda(Math.min(60, state.refresh * Math.pow(2, Math.min(4, state.fails))));
      });
  }

  // ------------------------------------------------------------- relogio
  var DIAS = ["domingo", "segunda-feira", "terca-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sabado"];
  var MESES = ["janeiro", "fevereiro", "marco", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"];

  function relogio() {
    var d = new Date();
    function p(n) { return (n < 10 ? "0" : "") + n; }
    el.clock.innerHTML = p(d.getHours()) + ":" + p(d.getMinutes()) + "<span>:" + p(d.getSeconds()) + "</span>";
    el.date.textContent = DIAS[d.getDay()] + ", " + d.getDate() + " de " + MESES[d.getMonth()];
  }

  // --------------------------------------------------- conforto de TV
  function antiBurnIn() {
    var passo = 0;
    var pontos = [[0, 0], [4, 3], [0, 6], [-4, 3], [-4, -3], [4, -3]];
    setInterval(function () {
      passo = (passo + 1) % pontos.length;
      el.tv.style.setProperty("--sx", pontos[passo][0] + "px");
      el.tv.style.setProperty("--sy", pontos[passo][1] + "px");
    }, 300000);   // a cada 5 min
  }

  /* Wake lock so existe em contexto seguro (https ou localhost). Em producao o
     painel roda em http, entao aqui e sempre inerte - por isso o "voltou a
     aparecer, atualiza ja" mora fora desta funcao, senao se perderia junto. */
  function mantemAcesa() {
    if (!navigator.wakeLock || !navigator.wakeLock.request) { return null; }
    var trava = null;
    function pede() {
      navigator.wakeLock.request("screen").then(function (w) { trava = w; }).catch(function () {});
    }
    pede();
    return function () { if (!trava) { pede(); } };
  }

  function observaVisibilidade() {
    var repoe = mantemAcesa();
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { return; }
      if (repoe) { repoe(); }
      carrega();                      // voltou a aparecer: atualiza sem esperar o timer
    });
  }

  function ociosidade() {
    var t = null;
    function ativo() {
      document.body.classList.remove("idle");
      if (t) { clearTimeout(t); }
      t = setTimeout(function () { document.body.classList.add("idle"); }, 4000);
    }
    ["mousemove", "keydown", "touchstart"].forEach(function (ev) {
      document.addEventListener(ev, ativo, { passive: true });
    });
    ativo();
  }

  function telaCheia() {
    var b = document.getElementById("tvfull");
    if (!b) { return; }
    b.addEventListener("click", function () {
      if (document.fullscreenElement) { document.exitFullscreen(); }
      else if (document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen().catch(function () {});
      }
    });
  }

  // ---------------------------------------------------------------- init
  function redesenha() {
    state.sig = "";                    // forca o redesenho com a nova densidade
    if (state.data) { render(state.data); }
  }

  var rz = null;
  function agendaRedesenho() {
    if (rz) { clearTimeout(rz); }
    rz = setTimeout(redesenha, 200);
  }
  window.addEventListener("resize", agendaRedesenho);
  window.addEventListener("orientationchange", agendaRedesenho);
  document.addEventListener("fullscreenchange", agendaRedesenho);

  /* Rede de seguranca: alguns navegadores de TV mudam a area util sem emitir
     "resize" (entrar em tela cheia, barra do sistema sumindo). Confere o
     tamanho da grade de tempos em tempos - custo desprezivel. */
  var ultimo = "";
  setInterval(function () {
    var r = el.grid.getBoundingClientRect();
    var atual = Math.round(r.width) + "x" + Math.round(r.height);
    if (ultimo && atual !== ultimo) { redesenha(); }
    ultimo = atual;
  }, 2000);

  relogio();
  setInterval(relogio, 1000);
  setInterval(rodaTicker, 7000);
  ociosidade();
  telaCheia();
  antiBurnIn();
  observaVisibilidade();

  var inicial = document.getElementById("tvdata");
  try {
    render(JSON.parse(inicial.textContent || inicial.innerText));
  } catch (e) { /* sem payload inicial: o fetch resolve */ }
  agenda(state.refresh);
})();
