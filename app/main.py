"""Aplicacao FastAPI do RMonitor: login, dashboard e historico."""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, db, execution, inventory, jobstats, packages, scheduler
from .collector import list_sessions, logoff_session, service_action
from .config import load_inventory, load_settings
from .scheduler import build_scheduler, poll_all, poll_inventory, scan_packages
from .security import hash_password, verify_password

log = logging.getLogger("rmon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _fmt_dt(ts) -> str:
    """Aceita epoch (o que vem das consultas de historico) ou datetime."""
    import datetime
    if not ts:
        return "-"
    if isinstance(ts, datetime.datetime):
        return ts.strftime("%d/%m %H:%M:%S")
    return datetime.datetime.fromtimestamp(ts).strftime("%d/%m %H:%M:%S")


def _fmt_dur(sec: float | None) -> str:
    if not sec:
        return "-"
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["dur"] = _fmt_dur
templates.env.globals["ui_refresh"] = 60
templates.env.globals["default_theme"] = "dark"
templates.env.globals["tv_refresh"] = 15
# Regra unica de "servico parado" (ver scheduler.service_down): o HTML nao
# pode pintar de vermelho um RM.Host que a coleta nem considera falha.
templates.env.globals["svc_down"] = scheduler.service_down
templates.env.globals["svc_groups"] = scheduler.service_groups
# Rotulo curto de cada fonte do inventario (produto, customizacao, biblioteca...)
templates.env.globals["fonte_label"] = inventory.FONTE_LABEL


def _thresholds() -> dict:
    """Limiares efetivos de alerta: defaults do codigo < YAML < ajuste no painel."""
    inv = STATE.get("inv")
    yaml_alerts = (inv.defaults.get("alerts") or {}) if inv else {}
    return {**scheduler.DEFAULT_ALERTS, **yaml_alerts, **(db.get_config("alerts", {}) or {})}


def _assets_tag() -> str:
    """Selo de cache dos estaticos: muda a cada deploy, senao a TV fica com o
    CSS/JS antigo em cache ate alguem limpar o navegador na marra."""
    d = BASE_DIR / "static"
    try:
        recente = max((f.stat().st_mtime for f in d.glob("*") if f.is_file()), default=0)
    except OSError:
        recente = 0
    return f"{__version__}-{int(recente)}"


templates.env.globals["assets"] = _assets_tag()


def _apply_ui_globals() -> None:
    ui = db.get_config("ui", {}) or {}
    templates.env.globals["ui_refresh"] = int(ui.get("refresh", 60))
    templates.env.globals["default_theme"] = ui.get("theme", "dark")
    templates.env.globals["tv_refresh"] = int(ui.get("tv_refresh", 15))

# Estado do processo (preenchido no lifespan)
STATE: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    inv = load_inventory(settings.config_path)
    db.init(settings.db_dsn)
    db.init_db()
    # seed do admin inicial (a partir do .env) se ainda nao houver usuarios
    if db.count_users() == 0 and settings.admin_password_hash:
        db.create_user(settings.admin_user, settings.admin_password_hash, role="admin")
        log.info("usuario admin inicial '%s' criado no banco", settings.admin_user)
    STATE.update(settings=settings, inv=inv)
    _apply_ui_globals()
    presas = db.reset_running_tasks()
    if presas:
        log.warning("%d tarefa(s) de instalacao ficaram presas no restart e foram "
                    "marcadas como falha (nao serao repetidas)", presas)

    threading.Thread(target=poll_all, args=(inv, settings), daemon=True).start()

    sched = build_scheduler(inv, settings)
    sched.start()
    STATE["scheduler"] = sched
    log.info("RMonitor %s iniciado (%d servidores, intervalo %ds)",
             __version__, len(inv.servers), inv.poll_interval_seconds)
    try:
        yield
    finally:
        sched.shutdown(wait=False)


app = FastAPI(title="RMonitor (RMon)", version=__version__, lifespan=lifespan)

# Perfil sem admin (viewer) e um quiosque: enxerga apenas o painel de TV.
VIEWER_PATHS = frozenset({"/", "/tv", "/api/tv", "/logout", "/login", "/healthz", "/favicon.ico"})


async def _kiosk_guard(request: Request, call_next):
    """Mantem o viewer restrito ao painel de TV.

    Registrado ANTES do SessionMiddleware para ficar por dentro dele na pilha
    (o ultimo middleware adicionado e o mais externo) e ter request.session pronto.
    """
    path = request.url.path
    if (request.session.get("user") and request.session.get("role") != "admin"
            and path not in VIEWER_PATHS and not path.startswith("/static/")):
        if path.startswith("/api/") or request.method != "GET":
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return RedirectResponse("/", status_code=302)
    return await call_next(request)


app.add_middleware(BaseHTTPMiddleware, dispatch=_kiosk_guard)

# SessionMiddleware precisa da secret key no momento de montar o app.
_boot_settings = load_settings()
# Em iframe de outro dominio (central de monitoramento) o cookie "lax" nao viaja:
# o navegador o descarta e o login volta para a tela de login sem erro nenhum.
# Nesse cenario use RMON_COOKIE_SAMESITE=none, que so vale sobre HTTPS (o Secure
# passa a ser obrigatorio). Sem TLS, prefira o token de quiosque do mural (/tv).
_COOKIE_SAMESITE = (_boot_settings.cookie_samesite
                    if _boot_settings.cookie_samesite in {"lax", "strict", "none"} else "lax")
_COOKIE_SECURE = _boot_settings.cookie_secure or _COOKIE_SAMESITE == "none"
app.add_middleware(
    SessionMiddleware,
    secret_key=_boot_settings.secret_key or "dev-inseguro-troque",
    session_cookie="rmon_session",
    https_only=_COOKIE_SECURE,
    same_site=_COOKIE_SAMESITE,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _settings():
    return STATE["settings"]


def _is_authed(request: Request) -> bool:
    return bool(request.session.get("user"))


def _tv_token_ok(request: Request) -> bool:
    """Mural liberado por token, sem sessao (para embutir em iframe de outro dominio).

    Vale so para /tv e /api/tv, que sao somente leitura. Aceita o token na query
    (?token=) ou no cabecalho X-RMon-Token.
    """
    esperado = (_settings().tv_token or "").strip()
    if not esperado:
        return False
    recebido = (request.query_params.get("token")
                or request.headers.get("x-rmon-token") or "")
    return bool(recebido) and secrets.compare_digest(recebido, esperado)


def _role(request: Request) -> str:
    return request.session.get("role", "viewer")


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, erro: str | None = None):
    if _is_authed(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "erro": erro})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    u = db.get_user(username)
    ok = bool(u) and u["active"] and verify_password(password, u["password_hash"])
    if not ok:
        db.audit(username, "login_fail", "usuario/senha invalidos ou inativo", _ip(request))
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "erro": "Usuario ou senha invalidos."},
            status_code=401,
        )
    request.session["user"] = username
    request.session["role"] = u["role"]
    db.touch_login(username)
    db.audit(username, "login", None, _ip(request))
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    db.audit(request.session.get("user"), "logout", None, _ip(request))
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ---------- painel de TV (mural / perfil viewer) ----------
# Problemas que pintam o cartao de vermelho; o resto e apenas aviso (ambar).
# JOBS fica de fora de proposito: job que termina com erro costuma ser validacao
# ou regra de negocio da aplicacao, nao falha do servidor.
_TV_CRIT_KEYS = ("DOWN", "APP")
_TV_CACHE_TTL = 3.0
_TV_CACHE: dict = {"ts": 0.0, "data": None}


def _pct(v) -> int | None:
    return None if v is None else int(round(float(v)))


def _tv_thresholds() -> dict:
    inv = STATE["inv"]
    return {**scheduler.DEFAULT_ALERTS, **(inv.defaults.get("alerts") or {}),
            **(db.get_config("alerts", {}) or {})}


def _tv_payload() -> dict:
    """Estado completo do mural em JSON. Cache curto: varias TVs nao multiplicam consultas."""
    now = time.time()
    cached = _TV_CACHE["data"]
    if cached and (now - _TV_CACHE["ts"]) < _TV_CACHE_TTL:
        return cached

    inv = STATE["inv"]
    th = _tv_thresholds()
    latest = {r["server"]: r for r in db.latest_per_server()}
    stale_after = max(180, inv.poll_interval_seconds * 3)

    servers: list[dict] = []
    issues: list[dict] = []
    summary = {"total": 0, "online": 0, "offline": 0, "services_down": 0,
               "events": 0, "crit": 0, "warn": 0}

    for cfg in inv.servers:
        d = latest.get(cfg.name)
        up = bool(d and d["reachable"])
        # Sem contato: so e DOWN depois de `down_after` coletas seguidas falhando.
        # Antes disso o card fica ambar (instavel), nao vermelho.
        streak = db.fail_streak(cfg.name) if (d and not up) else None
        probs = scheduler.problems(
            d or {"reachable": False, "error": "aguardando a primeira coleta"}, th, streak)
        crit = any(k.startswith("svc:") or k in _TV_CRIT_KEYS for k in probs)
        sev = 2 if crit else (1 if (probs or not up) else 0)
        ts = d["ts"] if d else None
        svcs = (d["services"] or []) if d else []
        disks = (d["disks"] or []) if d else []
        events = (d["events"] or []) if d else []
        jobs = d["jobs"] if (d and d.get("jobs")) else None

        summary["total"] += 1
        summary["online" if up else "offline"] += 1
        summary["services_down"] += sum(1 for x in svcs if scheduler.service_down(x))
        summary["events"] += sum(int(e.get("count", 1)) for e in events)
        if sev == 2:
            summary["crit"] += 1
        elif sev == 1:
            summary["warn"] += 1

        for key, text in probs.items():
            issues.append({"server": cfg.name, "text": text,
                           "sev": 2 if (key.startswith("svc:") or key in _TV_CRIT_KEYS) else 1})
        if not up and "DOWN" not in probs:
            issues.append({"server": cfg.name, "sev": 1,
                           "text": f"coleta instavel ({streak}x sem resposta do WinRM)"})

        servers.append({
            "name": cfg.name, "host": cfg.host, "up": up, "sev": sev,
            # sem contato ainda nao confirmado: mural mostra ambar, nao vermelho
            "unstable": (not up and "DOWN" not in probs),
            "cpu": _pct(d["cpu"]) if d else None,
            "mem": _pct(d["mem_pct"]) if d else None,
            "uptime": (d["uptime_sec"] if d else None),
            "ts": ts,
            "age": int(now - ts) if ts else None,
            "stale": bool(ts and (now - ts) > stale_after),
            "disks": [{"d": x.get("drive"), "p": _pct(x.get("used_pct")) or 0,
                       "free": x.get("free_gb")} for x in disks],
            "svcs": [{"n": x.get("name"), "ok": x.get("status") == "Running",
                      "bad": scheduler.service_down(x), "st": x.get("status")} for x in svcs],
            "users": (d.get("users_count") if d else None),
            "app": (d.get("app_ok") if d else None),
            "app_ms": (d.get("app_ms") if d else None),
            "jobs": ({"ok": jobs.get("ok"), "failed": jobs.get("failed"),
                      "win": jobs.get("window_min"), "err": bool(jobs.get("error"))}
                     if jobs else None),
            "events": len(events),
            "err": (d["error"] if d else "aguardando a primeira coleta") if not up else None,
        })

    issues.sort(key=lambda i: (-i["sev"], i["server"]))
    data = {
        "ts": now,
        "refresh": int(templates.env.globals.get("tv_refresh", 15)),
        "poll": inv.poll_interval_seconds,
        "thresholds": {"mem": th["mem_pct"], "disk": th["disk_pct"], "app_ms": th["app_ms"]},
        "version": __version__,
        "summary": summary, "servers": servers, "issues": issues,
    }
    _TV_CACHE.update(ts=now, data=data)
    return data


def _tv_response(request: Request, token: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        "tv.html", {"request": request, "payload": _tv_payload(),
                    "version": __version__, "tv_token": token})


@app.get("/tv", response_class=HTMLResponse)
def tv_page(request: Request):
    """Mural em tela cheia: sem rolagem, feito para TV widescreen."""
    if _tv_token_ok(request):
        return _tv_response(request, token=request.query_params.get("token"))
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return _tv_response(request)


@app.get("/api/tv")
def api_tv(request: Request):
    if not (_tv_token_ok(request) or _is_authed(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(_tv_payload(), headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    if _role(request) != "admin":
        return _tv_response(request)  # viewer so ve o mural
    inv = STATE["inv"]
    latest = {r["server"]: r for r in db.latest_per_server()}
    rows = [{"cfg": srv, "data": latest.get(srv.name)} for srv in inv.servers]

    summary = {"total": len(rows), "online": 0, "offline": 0, "services_down": 0, "alerts": 0}
    for r in rows:
        d = r["data"]
        if d and d["reachable"]:
            summary["online"] += 1
            summary["services_down"] += sum(1 for s in (d["services"] or []) if scheduler.service_down(s))
            summary["alerts"] += sum(int(e.get("count", 1)) for e in (d["events"] or []))
        else:
            summary["offline"] += 1
    # Veredito do broker calculado aqui (uma vez, com os limiares lidos uma vez)
    # e nao no template: e o mesmo criterio do alerta, sem duplicar regra no HTML.
    th = _thresholds()
    ref = scheduler.broker_reference(th.get("broker_history_days"))
    for r in rows:
        r["broker"] = scheduler.broker_problems(r["data"] or {}, th, ref.get(r["cfg"].name))
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "rows": rows, "summary": summary, "broker_ref": ref,
         "interval": inv.poll_interval_seconds, "version": __version__},
    )


@app.get("/server/{name}", response_class=HTMLResponse)
def server_detail(request: Request, name: str):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    hist = db.history(name, limit=120)
    if not hist:
        return templates.TemplateResponse(
            "server.html",
            {"request": request, "name": name, "hist": [], "current": None},
            status_code=404,
        )
    th = _thresholds()
    ref = scheduler.broker_reference(th.get("broker_history_days")).get(name) or {}
    return templates.TemplateResponse(
        "server.html",
        {"request": request, "name": name, "current": hist[0], "hist": hist,
         "broker_ref": ref,
         "broker_alerta": scheduler.broker_problems(hist[0], th, ref)},
    )


@app.get("/api/status")
def api_status(request: Request):
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"servers": db.latest_per_server()}


@app.get("/api/series")
def api_series(request: Request, server: str, hours: int = 24):
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"server": server, "series": db.series(server, min(max(hours, 1), 168))}


@app.post("/service/action")
async def service_act(request: Request):
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _role(request) != "admin":
        return JSONResponse({"ok": False, "msg": "acao requer perfil admin"}, status_code=403)
    form = await request.form()
    server_name, svc, action = form.get("server"), form.get("service"), (form.get("action") or "restart")
    inv = STATE["inv"]
    srv = {s.name: s for s in inv.servers}.get(server_name)
    if not srv:
        return JSONResponse({"ok": False, "msg": "servidor desconhecido"}, status_code=404)
    # servicos descobertos por padrao nao estao no inventario: a ultima coleta
    # daquele servidor e quem diz o que existe hoje no host.
    ultimo = {r["server"]: r for r in db.latest_per_server()}.get(server_name) or {}
    vistos = [x.get("name") for x in (ultimo.get("services") or []) if x.get("name")]
    ok, msg = service_action(srv, inv.winrm, svc, action, allowed=vistos)
    db.audit(request.session.get("user"), f"service_{action}", f"{server_name}/{svc} -> {ok}: {msg}", _ip(request))
    log.info("%s %s/%s por %s -> %s (%s)", action, server_name, svc, request.session.get("user"), ok, msg)
    return JSONResponse({"ok": ok, "msg": msg})


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    try:
        window = min(max(int(request.query_params.get("min", 60)), 5), 1440)
    except ValueError:
        window = 60
    pool = jobstats.pool_summary(window)
    inv = STATE["inv"]
    hostmap = {s.jobs["servidor"].upper(): f"{s.name} ({s.host})"
               for s in inv.servers if s.jobs and s.jobs.get("servidor")}
    if pool and pool.get("by_server"):
        for r in pool["by_server"]:
            r["label"] = hostmap.get((r["host"] or "").upper())
    return templates.TemplateResponse("jobs.html", {"request": request, "pool": pool, "window": window, "version": __version__})


@app.get("/ocorrencias", response_class=HTMLResponse)
def ocorrencias_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    rows = []
    for r in db.latest_per_server():
        for e in (r.get("events") or []):
            rows.append({"server": r["server"], **e})
    return templates.TemplateResponse("ocorrencias.html", {"request": request, "rows": rows, "version": __version__})


@app.get("/profile", response_class=HTMLResponse)
def profile_get(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    u = db.get_user(request.session["user"])
    return templates.TemplateResponse(
        "profile.html",
        {"request": request, "u": u, "ok": request.query_params.get("ok"),
         "erro": request.query_params.get("erro"), "min_senha": MIN_SENHA, "version": __version__})


@app.post("/profile")
async def profile_post(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    db.update_profile(request.session["user"], (form.get("full_name") or "").strip(), (form.get("email") or "").strip())
    db.audit(request.session["user"], "profile_update", None, _ip(request))
    return RedirectResponse("/profile?ok=1", status_code=303)


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    if _role(request) != "admin":
        return HTMLResponse("Acesso restrito a administradores.", status_code=403)
    return templates.TemplateResponse("logs.html", {"request": request, "audit": db.recent_audit(500), "version": __version__})


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    if _role(request) != "admin":
        return HTMLResponse("Acesso restrito a administradores.", status_code=403)
    ui = db.get_config("ui", {}) or {}
    alerts = db.get_config("alerts", {}) or {}
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "users": db.list_users(), "ok": request.query_params.get("ok"),
         "ui": {"refresh": ui.get("refresh", 60), "theme": ui.get("theme", "dark"),
                "tv_refresh": ui.get("tv_refresh", 15)},
         "alerts": {"disk_pct": alerts.get("disk_pct", 90), "mem_pct": alerts.get("mem_pct", 90),
                    "app_ms": alerts.get("app_ms", 3000),
                    "down_after": alerts.get("down_after", scheduler.DEFAULT_ALERTS["down_after"])},
         "version": __version__},
    )


@app.post("/admin/config")
async def admin_config(request: Request):
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    form = await request.form()

    def _int(name, dflt, lo, hi):
        try:
            return max(lo, min(hi, int(form.get(name) or dflt)))
        except ValueError:
            return dflt
    theme = form.get("theme", "dark")
    db.set_config("ui", {"refresh": _int("refresh", 60, 10, 3600),
                         "theme": theme if theme in ("dark", "light") else "dark",
                         "tv_refresh": _int("tv_refresh", 15, 5, 600)})
    db.set_config("alerts", {"disk_pct": _int("disk_pct", 90, 1, 100), "mem_pct": _int("mem_pct", 90, 1, 100),
                             "app_ms": _int("app_ms", 3000, 100, 60000),
                             "down_after": _int("down_after", 3, 1, 10)})
    _apply_ui_globals()
    db.audit(request.session.get("user"), "config_update", None, _ip(request))
    return RedirectResponse("/admin?ok=1", status_code=303)


# ---------- gerenciamento de usuarios (admin) ----------
USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,31}$")
MIN_SENHA = 8


def _flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["uflash"] = {"kind": kind, "msg": msg}


def _users_redirect(request: Request, msg: str, kind: str = "ok") -> RedirectResponse:
    _flash(request, msg, kind)
    return RedirectResponse("/admin/usuarios", status_code=303)


def _check_password(pw: str, pw2: str) -> str | None:
    """Retorna mensagem de erro ou None."""
    if len(pw) < MIN_SENHA:
        return f"A senha deve ter ao menos {MIN_SENHA} caracteres."
    if pw != pw2:
        return "As senhas nao conferem."
    return None


@app.get("/admin/usuarios", response_class=HTMLResponse)
def users_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    if _role(request) != "admin":
        return HTMLResponse("Acesso restrito a administradores.", status_code=403)
    return templates.TemplateResponse(
        "users.html",
        {"request": request, "users": db.list_users(), "me": request.session["user"],
         "flash": request.session.pop("uflash", None), "min_senha": MIN_SENHA,
         "version": __version__},
    )


@app.post("/admin/usuarios/criar")
async def users_create(request: Request):
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    form = await request.form()
    username = (form.get("username") or "").strip()
    role = form.get("role") if form.get("role") in ("admin", "viewer") else "viewer"
    pw, pw2 = form.get("password") or "", form.get("password2") or ""

    if not USERNAME_RE.match(username):
        return _users_redirect(request, "Login invalido: use 3 a 32 caracteres (letras, numeros, . _ -).", "err")
    erro = _check_password(pw, pw2)
    if erro:
        return _users_redirect(request, erro, "err")
    criado = db.insert_user(username, hash_password(pw), role,
                            (form.get("full_name") or "").strip(), (form.get("email") or "").strip())
    if not criado:
        return _users_redirect(request, f"Ja existe um usuario com o login '{username}'.", "err")
    db.audit(request.session["user"], "user_create", f"{username} ({role})", _ip(request))
    return _users_redirect(request, f"Usuario '{username}' criado.")


@app.post("/admin/usuarios/{username}/editar")
async def users_update(request: Request, username: str):
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    alvo = db.get_user(username)
    if not alvo:
        return _users_redirect(request, "Usuario nao encontrado.", "err")
    form = await request.form()
    role = form.get("role") if form.get("role") in ("admin", "viewer") else alvo["role"]
    active = form.get("active") == "on"

    # nunca deixar o painel sem nenhum admin ativo
    if (alvo["role"] == "admin" and alvo["active"]) and (role != "admin" or not active) \
            and db.count_active_admins(exclude=username) == 0:
        return _users_redirect(request, "Este e o unico administrador ativo: mantenha o papel admin e a conta ativa.", "err")

    db.update_user(username, (form.get("full_name") or "").strip(),
                   (form.get("email") or "").strip(), role, active)
    db.audit(request.session["user"], "user_update",
             f"{username} -> {role}, {'ativo' if active else 'inativo'}", _ip(request))
    if username == request.session["user"]:
        request.session["role"] = role
    return _users_redirect(request, f"Usuario '{username}' atualizado.")


@app.post("/admin/usuarios/{username}/senha")
async def users_password(request: Request, username: str):
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not db.get_user(username):
        return _users_redirect(request, "Usuario nao encontrado.", "err")
    form = await request.form()
    erro = _check_password(form.get("password") or "", form.get("password2") or "")
    if erro:
        return _users_redirect(request, erro, "err")
    db.set_password(username, hash_password(form.get("password")))
    db.audit(request.session["user"], "user_password_reset", username, _ip(request))
    return _users_redirect(request, f"Senha de '{username}' redefinida.")


@app.post("/admin/usuarios/{username}/excluir")
async def users_delete(request: Request, username: str):
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not db.get_user(username):
        return _users_redirect(request, "Usuario nao encontrado.", "err")
    if username == request.session["user"]:
        return _users_redirect(request, "Voce nao pode excluir a propria conta.", "err")
    if db.count_active_admins(exclude=username) == 0:
        return _users_redirect(request, "Nao e possivel excluir o unico administrador ativo.", "err")
    db.delete_user(username)
    db.audit(request.session["user"], "user_delete", username, _ip(request))
    return _users_redirect(request, f"Usuario '{username}' excluido.")


@app.post("/profile/senha")
async def profile_password(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    username = request.session["user"]
    form = await request.form()
    u = db.get_user(username)
    if not u or not verify_password(form.get("atual") or "", u["password_hash"]):
        db.audit(username, "password_change_fail", "senha atual incorreta", _ip(request))
        return RedirectResponse(f"/profile?erro={quote('A senha atual esta incorreta.')}", status_code=303)
    erro = _check_password(form.get("password") or "", form.get("password2") or "")
    if erro:
        return RedirectResponse(f"/profile?erro={quote(erro)}", status_code=303)
    db.set_password(username, hash_password(form.get("password")))
    db.audit(username, "password_change", None, _ip(request))
    return RedirectResponse("/profile?ok=senha", status_code=303)


@app.get("/sessions", response_class=HTMLResponse)
def sessions_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    inv = STATE["inv"]
    results: dict[str, dict] = {}
    if inv.servers:
        with ThreadPoolExecutor(max_workers=min(8, len(inv.servers))) as pool:
            futs = {pool.submit(list_sessions, s, inv.winrm): s for s in inv.servers}
            for fut, srv in futs.items():
                try:
                    results[srv.name] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    results[srv.name] = {"error": f"{exc}", "sessions": []}
    rows = [{"cfg": s, "res": results.get(s.name, {"error": None, "sessions": []})} for s in inv.servers]
    total = sum(len(r["res"]["sessions"]) for r in rows)
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        "sessions.html",
        {"request": request, "rows": rows, "total": total, "flash": flash, "version": __version__},
    )


@app.post("/sessions/logoff")
async def sessions_logoff(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    if _role(request) != "admin":
        request.session["flash"] = "Acao requer perfil admin."
        return RedirectResponse("/sessions", status_code=303)
    form = await request.form()
    targets = form.getlist("target")  # cada item: "server|id"
    inv = STATE["inv"]
    by_name = {s.name: s for s in inv.servers}
    ok_count = 0
    fails: list[str] = []
    for t in targets:
        server_name, _, sid = str(t).partition("|")
        srv = by_name.get(server_name)
        if not srv:
            continue
        success, msg = logoff_session(srv, inv.winrm, sid)
        if success:
            ok_count += 1
            log.info("logoff %s sessao %s por %s", server_name, sid, request.session.get("user"))
        else:
            fails.append(f"{server_name}#{sid}: {msg}")
            log.warning("logoff FALHOU %s sessao %s: %s", server_name, sid, msg)
    parts = []
    if ok_count:
        parts.append(f"{ok_count} sessao(oes) encerrada(s).")
    if fails:
        parts.append("Falhas: " + "; ".join(fails))
    if not targets:
        parts.append("Nenhuma sessao selecionada.")
    db.audit(request.session.get("user"), "logoff", " ".join(parts), _ip(request))
    request.session["flash"] = " ".join(parts)
    return RedirectResponse("/sessions", status_code=303)


# ---------- inventario de software (pacotes/versoes por host) ----------
def _pacotes_ctx(request: Request) -> dict:
    """Contexto comum das telas de inventario: matriz, filtros e ultima coleta."""
    inv = STATE["inv"]
    q = request.query_params
    fonte = q.get("fonte") if q.get("fonte") in inventory.FONTES else "totvs"
    filtros = ("todos", "atualizavel", "drift", "ausentes", "problemas")
    filtro = q.get("filtro") if q.get("filtro") in filtros else "todos"
    busca = (q.get("q") or "").strip()[:80]
    servidores = [s.name for s in inv.servers]
    servidor = q.get("servidor") if q.get("servidor") in servidores else ""
    catalogo = db.list_catalog()
    linhas = inventory.build_matrix(db.all_packages(), servidores, fonte=fonte,
                                    busca=busca, filtro=filtro, servidor=servidor,
                                    disponivel=packages.available_by_key(catalogo))
    runs = db.inventory_runs()
    return {
        "request": request, "servidores": servidores, "linhas": linhas, "runs": runs,
        "fonte": fonte, "filtro": filtro, "busca": busca, "servidor": servidor,
        "resumo": {
            "pacotes": len(linhas),
            "drift": sum(1 for l in linhas if l["drift"]),
            "parcial": sum(1 for l in linhas if l["parcial"]),
            "sem_coleta": [s for s in servidores if s not in runs],
            "falhas": [s for s, r in runs.items() if not r["ok"]],
            "atualizavel": sum(1 for l in linhas if l["atualizavel"]),
            "catalogo": len(catalogo),
            "sem_vinculo": sum(1 for e in catalogo if not e["pkg_key"]),
        },
        "version": __version__,
    }


@app.get("/pacotes", response_class=HTMLResponse)
def pacotes_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    ctx = _pacotes_ctx(request)
    ctx["flash"] = request.session.pop("pflash", None)
    return templates.TemplateResponse("pacotes.html", ctx)


@app.get("/pacotes.csv")
def pacotes_csv(request: Request):
    """Inventario achatado (um pacote por linha, por servidor) para planilha."""
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    import csv
    import io as _io
    buf = _io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["servidor", "pacote", "versao", "fabricante", "arquitetura",
                "fonte", "instalado_em", "data_estimada", "detalhe"])
    for r in db.all_packages():
        data = r["install_date"] or (r["first_seen"].date() if r.get("first_seen") else None)
        w.writerow([r["server"], r["name"], r["version"] or "", r["publisher"] or "",
                    r["arch"] or "", r["source"], data or "",
                    "sim" if not r["install_date"] else "nao", r["detail"] or ""])
    from fastapi.responses import Response
    return Response(
        buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="rmon-pacotes.csv"'})


@app.get("/pacotes/mudancas", response_class=HTMLResponse)
def pacotes_mudancas(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    inv = STATE["inv"]
    servidores = [s.name for s in inv.servers]
    q = request.query_params
    servidor = q.get("servidor") if q.get("servidor") in servidores else ""
    try:
        dias = min(max(int(q.get("dias", 90)), 1), 365)
    except ValueError:
        dias = 90
    eventos = db.recent_package_events(limit=500, server=servidor or None, days=dias)
    return templates.TemplateResponse(
        "pacotes_mudancas.html",
        {"request": request, "eventos": eventos, "servidores": servidores,
         "servidor": servidor, "dias": dias, "labels": inventory.EVENT_LABEL,
         "version": __version__})


@app.post("/pacotes/coletar")
def pacotes_coletar(request: Request):
    """Forca uma coleta de inventario fora da cadencia (admin).

    Roda em thread: varrer o registro de todos os hosts leva mais tempo do que
    o navegador espera por uma resposta.
    """
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    if _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    inv, settings = STATE["inv"], _settings()
    threading.Thread(target=poll_inventory, args=(inv, settings), daemon=True).start()
    db.audit(request.session.get("user"), "inventory_collect", None, _ip(request))
    request.session["pflash"] = ("Coleta de inventario disparada. "
                                "Atualize a pagina em alguns instantes.")
    return RedirectResponse("/pacotes", status_code=303)


# ---------- catalogo de versoes disponiveis (pacotes baixados do TDN) ----------
def _itens_do_inventario() -> list[dict]:
    """Itens distintos do inventario, para vincular um pacote baixado a um deles."""
    vistos: dict[str, str] = {}
    for r in db.all_packages():
        vistos.setdefault(r["pkg_key"], r["name"])
    return [{"key": k, "name": n} for k, n in sorted(vistos.items(), key=lambda x: x[1].lower())]


@app.get("/pacotes/catalogo", response_class=HTMLResponse)
def catalogo_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    if _role(request) != "admin":
        return HTMLResponse("Acesso restrito a administradores.", status_code=403)
    catalogo = db.list_catalog()
    instalado = {}
    for r in db.all_packages():
        atual = instalado.get(r["pkg_key"])
        if atual is None or inventory.compare_versions(r["version"], atual) > 0:
            instalado[r["pkg_key"]] = r["version"]
    return templates.TemplateResponse(
        "catalogo.html",
        {"request": request, "catalogo": catalogo, "itens": _itens_do_inventario(),
         "instalado": instalado, "pasta": _settings().packages_dir,
         "flash": request.session.pop("cflash", None), "version": __version__})


def _catalogo_redirect(request: Request, msg: str) -> RedirectResponse:
    request.session["cflash"] = msg
    return RedirectResponse("/pacotes/catalogo", status_code=303)


@app.post("/pacotes/catalogo/manual")
async def catalogo_manual(request: Request):
    """Registra a mao uma versao disponivel (o que o nome do arquivo nao revela)."""
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    form = await request.form()
    produto = (form.get("produto") or "").strip()[:200]
    versao = (form.get("version") or "").strip()[:80]
    if not produto or not versao:
        return _catalogo_redirect(request, "Informe produto e versao.")
    if not re.fullmatch(r"[0-9][0-9.]{0,78}[0-9]", versao):
        return _catalogo_redirect(request, "Versao invalida: use apenas numeros e pontos.")
    url = (form.get("url") or "").strip()[:400]
    if url and not url.startswith(("http://", "https://")):
        return _catalogo_redirect(request, "O link deve comecar com http:// ou https://.")
    db.insert_manual_catalog(produto, versao, (form.get("pkg_key") or "").strip() or None,
                             url or None, (form.get("nota") or "").strip()[:300] or None)
    db.audit(request.session["user"], "catalog_add", f"{produto} {versao}", _ip(request))
    return _catalogo_redirect(request, f"Versao {versao} de '{produto}' registrada.")


@app.post("/pacotes/catalogo/{entry_id}/excluir")
def catalogo_delete(request: Request, entry_id: int):
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    db.delete_catalog(entry_id)
    db.audit(request.session["user"], "catalog_delete", str(entry_id), _ip(request))
    return _catalogo_redirect(request, "Entrada removida.")


@app.post("/pacotes/catalogo/vincular")
async def catalogo_vincular(request: Request):
    """Diz a que item do inventario um pacote baixado se refere.

    O vinculo e por nome de produto, nao pela linha do catalogo: as entradas do
    repositorio sao reescritas a cada varredura, e o vinculo precisa sobreviver
    a isso (e valer para a proxima versao do mesmo pacote).
    """
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    form = await request.form()
    produto = (form.get("produto") or "").strip()
    pkg_key = (form.get("pkg_key") or "").strip()
    chave = packages.match_key(produto)
    if not chave:
        return _catalogo_redirect(request, "Produto invalido.")
    vinculos = dict(db.get_config("catalog_bind", {}) or {})
    if pkg_key:
        vinculos[chave] = pkg_key
    else:
        vinculos.pop(chave, None)
    db.set_config("catalog_bind", vinculos)
    scan_packages(_settings())
    db.audit(request.session["user"], "catalog_bind", f"{produto} -> {pkg_key or '(nenhum)'}",
             _ip(request))
    return _catalogo_redirect(request, f"Vinculo de '{produto}' atualizado.")


@app.post("/pacotes/catalogo/varrer")
def catalogo_varrer(request: Request):
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    n = scan_packages(_settings())
    db.audit(request.session["user"], "catalog_scan", f"{n} arquivo(s)", _ip(request))
    return _catalogo_redirect(request, f"Repositorio lido: {n} arquivo(s).")


# ---------- fila de instalacao (F4) ----------
TASK_LABEL = {"pending": "na fila", "running": "executando", "ok": "concluida",
              "failed": "falhou", "blocked": "bloqueada", "canceled": "cancelada"}
templates.env.globals["task_label"] = TASK_LABEL


def _exec_cfg() -> dict:
    return execution.settings_for(STATE["inv"].defaults)


@app.get("/pacotes/tarefas", response_class=HTMLResponse)
def tarefas_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=302)
    if _role(request) != "admin":
        return HTMLResponse("Acesso restrito a administradores.", status_code=403)
    inv = STATE["inv"]
    cfg = _exec_cfg()
    catalogo = [e for e in db.list_catalog() if e["arquivo"]]
    return templates.TemplateResponse(
        "tarefas.html",
        {"request": request, "tarefas": db.list_tasks(60),
         "servidores": [s.name for s in inv.servers], "catalogo": catalogo,
         "acoes": execution.actions_for(inv.defaults),
         "cfg": {"enabled": bool(cfg.get("enabled")), "hosts": list(cfg.get("hosts") or []),
                 "window": cfg.get("window") or "", "max_sessions": cfg.get("max_sessions", 0),
                 "base_url": cfg.get("base_url") or "", "temp_dir": cfg.get("temp_dir")},
         "flash": request.session.pop("tflash", None), "version": __version__})


def _tarefas_redirect(request: Request, msg: str) -> RedirectResponse:
    request.session["tflash"] = msg
    return RedirectResponse("/pacotes/tarefas", status_code=303)


@app.post("/pacotes/tarefas")
async def tarefas_criar(request: Request):
    """Enfileira uma tarefa.

    O modo real so passa com a trava mestra ligada, o host na lista de liberados
    e o nome do host digitado a mao - as tres coisas, sempre. Na duvida, cai
    para o pre-voo, que nao executa nada.
    """
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    form = await request.form()
    inv = STATE["inv"]
    servidor = form.get("server") or ""
    if servidor not in {s.name for s in inv.servers}:
        return _tarefas_redirect(request, "Servidor desconhecido.")
    acao = form.get("action") or ""
    if acao not in execution.actions_for(inv.defaults):
        return _tarefas_redirect(request, "Acao fora do catalogo permitido.")
    try:
        entrada_id = int(form.get("catalog_id") or 0)
    except ValueError:
        entrada_id = 0
    entrada = next((e for e in db.list_catalog() if e["id"] == entrada_id and e["arquivo"]), None)
    if not entrada:
        return _tarefas_redirect(request, "Pacote nao encontrado no repositorio.")

    modo = "dry_run"
    if form.get("mode") == "real":
        cfg = _exec_cfg()
        if not cfg.get("enabled"):
            return _tarefas_redirect(
                request, "Execucao real esta desligada (execution.enabled). "
                         "Nada foi enfileirado.")
        if servidor not in (cfg.get("hosts") or []):
            return _tarefas_redirect(
                request, f"{servidor} nao esta em execution.hosts. Nada foi enfileirado.")
        if (form.get("confirm") or "").strip() != servidor:
            return _tarefas_redirect(
                request, "Para executar de verdade, digite o nome do host exatamente "
                         "como aparece no painel. Nada foi enfileirado.")
        modo = "real"

    task_id = db.create_task(servidor, entrada["produto"], entrada["version"],
                             entrada["arquivo"], entrada["pkg_key"], acao, modo,
                             request.session["user"])
    db.audit(request.session["user"], "install_task_create",
             f"#{task_id} {servidor} {entrada['produto']} {entrada['version']} "
             f"({acao}, {modo})", _ip(request))
    return _tarefas_redirect(
        request, f"Tarefa #{task_id} na fila "
                 f"({'EXECUCAO REAL' if modo == 'real' else 'pre-voo, sem executar'}).")


@app.post("/pacotes/tarefas/{task_id}/cancelar")
def tarefas_cancelar(request: Request, task_id: int):
    if not _is_authed(request) or _role(request) != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if db.cancel_task(task_id):
        db.audit(request.session["user"], "install_task_cancel", str(task_id), _ip(request))
        return _tarefas_redirect(request, f"Tarefa #{task_id} cancelada.")
    return _tarefas_redirect(request, f"Tarefa #{task_id} ja saiu da fila.")


@app.get("/pacotes/stage/{task_id}")
def pacote_stage(request: Request, task_id: int, exp: int = 0, sig: str = ""):
    """Entrega o pacote ao host durante a instalacao.

    Sem sessao, porque quem baixa e o servidor Windows. Em compensacao: so
    existe com a execucao habilitada, exige assinatura HMAC com prazo, so serve
    a tarefa daquele id enquanto ela esta viva, e o arquivo tem de estar dentro
    do repositorio de pacotes.
    """
    if not _exec_cfg().get("enabled"):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not execution.stage_token_ok(_settings().secret_key, task_id, exp, sig):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    task = db.get_task(task_id)
    if not task or task["status"] not in ("pending", "running") or task["mode"] != "real":
        return JSONResponse({"error": "not found"}, status_code=404)
    caminho = execution.pacote_local(_settings(), task["arquivo"] or "")
    if not caminho:
        return JSONResponse({"error": "not found"}, status_code=404)
    from fastapi.responses import FileResponse
    log.info("stage: entregando %s para a tarefa %d", caminho.name, task_id)
    return FileResponse(str(caminho), filename=caminho.name,
                        media_type="application/octet-stream")
