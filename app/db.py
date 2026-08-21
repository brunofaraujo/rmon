"""Camada de dados em PostgreSQL (psycopg3). Conexoes curtas (sem estado compartilhado entre threads)."""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_DSN = ""

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id serial PRIMARY KEY,
        username text UNIQUE NOT NULL,
        password_hash text NOT NULL,
        role text NOT NULL DEFAULT 'viewer',
        active boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(),
        last_login timestamptz
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id bigserial PRIMARY KEY,
        ts timestamptz NOT NULL DEFAULT now(),
        username text, action text NOT NULL, detail text, ip text
    )""",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC)",
    """CREATE TABLE IF NOT EXISTS checks (
        id bigserial PRIMARY KEY,
        ts timestamptz NOT NULL DEFAULT now(),
        server text NOT NULL,
        reachable boolean NOT NULL,
        cpu real, mem_pct real, uptime_sec bigint,
        disks jsonb, services jsonb, events jsonb,
        app_ok boolean, app_ms integer, error text
    )""",
    "CREATE INDEX IF NOT EXISTS idx_checks_server_ts ON checks(server, ts DESC)",
    """CREATE TABLE IF NOT EXISTS alerts_log (
        id bigserial PRIMARY KEY,
        ts timestamptz NOT NULL DEFAULT now(),
        server text NOT NULL, kind text NOT NULL, problem text NOT NULL, message text
    )""",
    "CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts_log(ts DESC)",
    """CREATE TABLE IF NOT EXISTS app_config (
        key text PRIMARY KEY, value jsonb, updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name text",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email text",
    "ALTER TABLE checks ADD COLUMN IF NOT EXISTS users_count integer",
    "ALTER TABLE checks ADD COLUMN IF NOT EXISTS jobs jsonb",
    # --- inventario de software (cadencia propria, nao entra em checks) ---
    """CREATE TABLE IF NOT EXISTS host_packages (
        server text NOT NULL,
        pkg_key text NOT NULL,
        name text NOT NULL,
        version text,
        publisher text,
        install_date date,
        arch text,
        source text NOT NULL DEFAULT 'registry',
        detail text,
        first_seen timestamptz NOT NULL DEFAULT now(),
        last_seen timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (server, pkg_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_host_packages_key ON host_packages(pkg_key)",
    """CREATE TABLE IF NOT EXISTS package_events (
        id bigserial PRIMARY KEY,
        ts timestamptz NOT NULL DEFAULT now(),
        server text NOT NULL, pkg_key text NOT NULL, name text NOT NULL,
        kind text NOT NULL, old_version text, new_version text, source text
    )""",
    "CREATE INDEX IF NOT EXISTS idx_package_events_ts ON package_events(ts DESC)",
    """CREATE TABLE IF NOT EXISTS inventory_runs (
        server text PRIMARY KEY,
        ts timestamptz NOT NULL DEFAULT now(),
        ok boolean NOT NULL, packages integer, error text, duration_ms integer,
        computer text, os text
    )""",
]


def init(dsn: str) -> None:
    global _DSN
    _DSN = dsn


def _conn() -> psycopg.Connection:
    return psycopg.connect(_DSN, row_factory=dict_row, autocommit=True)


# Versao do formato do inventario. Subir este numero faz o inventario ser
# recolhido do zero na proxima coleta.
INVENTORY_SCHEMA = 2


def init_db() -> None:
    with _conn() as c:
        for stmt in _SCHEMA:
            c.execute(stmt)
    _migrar_inventario()


def _migrar_inventario() -> None:
    """Zera o inventario quando o formato dos itens muda.

    As fontes e as chaves de pacote mudaram (de "todo software instalado" para
    "software TOTVS"). Sem limpar, a coleta seguinte veria as linhas antigas
    como pacotes desinstalados e encheria a linha do tempo de remocoes que nunca
    aconteceram. Zerado, o proximo ciclo semeia de novo - sem gerar eventos.
    """
    try:
        atual = int(get_config("inventory_schema", 0) or 0)
    except (TypeError, ValueError):
        atual = 0
    if atual >= INVENTORY_SCHEMA:
        return
    with _conn() as c:
        c.execute("DELETE FROM host_packages")
        c.execute("DELETE FROM inventory_runs")
    set_config("inventory_schema", INVENTORY_SCHEMA)


def _epoch(rows: list[dict]) -> list[dict]:
    for r in rows:
        if r.get("ts") is not None and not isinstance(r["ts"], (int, float)):
            r["ts"] = r["ts"].timestamp()
    return rows


# ---------- metricas ----------
def insert_check(server: str, result: dict[str, Any]) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO checks (server, reachable, cpu, mem_pct, uptime_sec,
                 disks, services, events, app_ok, app_ms, error, users_count, jobs)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (server, bool(result.get("reachable")), result.get("cpu"), result.get("mem_pct"),
             result.get("uptime_sec"), Jsonb(result.get("disks") or []),
             Jsonb(result.get("services") or []), Jsonb(result.get("events") or []),
             result.get("app_ok"), result.get("app_ms"), result.get("error"), result.get("users_count"),
             Jsonb(result["jobs"]) if result.get("jobs") else None),
        )


def latest_per_server() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT ON (server) * FROM checks ORDER BY server, ts DESC"
        ).fetchall()
    return _epoch(rows)


def fail_streak(server: str, limit: int = 20) -> int:
    """Quantas coletas consecutivas (a partir da mais recente) falharam.

    Base do antiflapping: uma coleta isolada que estoura o timeout do WinRM nao
    significa servidor fora do ar. Le do banco em vez de guardar em memoria para
    que o valor sobreviva a um restart do RMon e seja igual no alerta e no mural.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT reachable FROM checks WHERE server=%s ORDER BY ts DESC LIMIT %s",
            (server, limit),
        ).fetchall()
    n = 0
    for r in rows:
        if r["reachable"]:
            break
        n += 1
    return n


def history(server: str, limit: int = 120) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM checks WHERE server=%s ORDER BY ts DESC LIMIT %s", (server, limit)
        ).fetchall()
    return _epoch(rows)


def series(server: str, hours: int = 24) -> list[dict]:
    """Serie temporal p/ graficos futuros."""
    with _conn() as c:
        rows = c.execute(
            """SELECT ts, cpu, mem_pct, app_ms, reachable FROM checks
               WHERE server=%s AND ts > now() - (%s || ' hours')::interval ORDER BY ts""",
            (server, str(hours)),
        ).fetchall()
    return _epoch(rows)


def prune(keep_days: int = 30) -> int:
    with _conn() as c:
        cur = c.execute("DELETE FROM checks WHERE ts < now() - (%s || ' days')::interval", (str(keep_days),))
        c.execute("DELETE FROM audit_log WHERE ts < now() - interval '180 days'")
        c.execute("DELETE FROM alerts_log WHERE ts < now() - interval '180 days'")
        # mudancas de pacote sao raras e valiosas na investigacao: guarda 1 ano
        c.execute("DELETE FROM package_events WHERE ts < now() - interval '365 days'")
        return cur.rowcount


# ---------- usuarios ----------
def count_users() -> int:
    with _conn() as c:
        return c.execute("SELECT count(*) AS n FROM users").fetchone()["n"]


def get_user(username: str) -> dict | None:
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE username=%s", (username,)).fetchone()


def list_users() -> list[dict]:
    with _conn() as c:
        return c.execute("SELECT id, username, full_name, email, role, active, created_at, last_login FROM users ORDER BY username").fetchall()


def update_profile(username: str, full_name: str, email: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET full_name=%s, email=%s WHERE username=%s", (full_name, email, username))


def create_user(username: str, password_hash: str, role: str = "viewer") -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO users (username, password_hash, role) VALUES (%s,%s,%s)
               ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash, role=EXCLUDED.role, active=true""",
            (username, password_hash, role),
        )


def insert_user(username: str, password_hash: str, role: str = "viewer",
                full_name: str = "", email: str = "") -> bool:
    """Cria usuario novo. Retorna False se o login ja existe."""
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO users (username, password_hash, role, full_name, email)
               VALUES (%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING""",
            (username, password_hash, role, full_name or None, email or None),
        )
        return cur.rowcount > 0


def update_user(username: str, full_name: str, email: str, role: str, active: bool) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE users SET full_name=%s, email=%s, role=%s, active=%s WHERE username=%s",
            (full_name or None, email or None, role, active, username),
        )


def delete_user(username: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM users WHERE username=%s", (username,))


def count_active_admins(exclude: str | None = None) -> int:
    with _conn() as c:
        return c.execute(
            "SELECT count(*) AS n FROM users WHERE role='admin' AND active AND username <> %s",
            (exclude or "",),
        ).fetchone()["n"]


def set_password(username: str, password_hash: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET password_hash=%s WHERE username=%s", (password_hash, username))


def set_active(username: str, active: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET active=%s WHERE username=%s", (active, username))


def set_role(username: str, role: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET role=%s WHERE username=%s", (role, username))


def touch_login(username: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET last_login=now() WHERE username=%s", (username,))


# ---------- auditoria / alertas / config ----------
def audit(username: str | None, action: str, detail: str | None = None, ip: str | None = None) -> None:
    try:
        with _conn() as c:
            c.execute("INSERT INTO audit_log (username, action, detail, ip) VALUES (%s,%s,%s,%s)",
                      (username, action, detail, ip))
    except Exception:  # nunca quebra o fluxo por causa de log
        pass


def recent_audit(limit: int = 200) -> list[dict]:
    with _conn() as c:
        return _epoch(c.execute("SELECT * FROM audit_log ORDER BY ts DESC LIMIT %s", (limit,)).fetchall())


def record_alert(server: str, kind: str, problem: str, message: str) -> None:
    try:
        with _conn() as c:
            c.execute("INSERT INTO alerts_log (server, kind, problem, message) VALUES (%s,%s,%s,%s)",
                      (server, kind, problem, message))
    except Exception:
        pass


def get_config(key: str, default: Any = None) -> Any:
    with _conn() as c:
        row = c.execute("SELECT value FROM app_config WHERE key=%s", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: Any) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO app_config (key, value, updated_at) VALUES (%s,%s,now())
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
            (key, Jsonb(value)),
        )


# ---------- inventario de software ----------
def packages_of(server: str) -> dict[str, dict]:
    """Estado atual do inventario de um servidor, indexado pela chave do pacote."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM host_packages WHERE server=%s", (server,)).fetchall()
    return {r["pkg_key"]: r for r in rows}


def _as_date(value: Any):
    """'yyyy-mm-dd' -> date. Data invalida vira NULL em vez de quebrar a coleta."""
    import datetime
    if not value:
        return None
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def replace_packages(server: str, items: list[dict]) -> None:
    """Grava o inventario do servidor: insere/atualiza o que veio e apaga o
    que nao veio (desinstalado). `first_seen` da linha existente e preservado."""
    chaves = [i["pkg_key"] for i in items]
    with _conn() as c:
        with c.transaction():
            for i in items:
                c.execute(
                    """INSERT INTO host_packages
                         (server, pkg_key, name, version, publisher, install_date,
                          arch, source, detail, first_seen, last_seen)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())
                       ON CONFLICT (server, pkg_key) DO UPDATE SET
                         name=EXCLUDED.name, version=EXCLUDED.version,
                         publisher=EXCLUDED.publisher,
                         install_date=COALESCE(EXCLUDED.install_date, host_packages.install_date),
                         arch=EXCLUDED.arch, source=EXCLUDED.source,
                         detail=EXCLUDED.detail, last_seen=now()""",
                    (server, i["pkg_key"], i["name"], i.get("version"), i.get("publisher"),
                     _as_date(i.get("install_date")), i.get("arch"), i.get("source"),
                     i.get("detail")),
                )
            if chaves:
                c.execute("DELETE FROM host_packages WHERE server=%s AND pkg_key <> ALL(%s)",
                          (server, chaves))
            else:
                c.execute("DELETE FROM host_packages WHERE server=%s", (server,))


def insert_package_events(server: str, events: list[dict]) -> None:
    if not events:
        return
    with _conn() as c:
        with c.transaction():
            for e in events:
                c.execute(
                    """INSERT INTO package_events
                         (server, pkg_key, name, kind, old_version, new_version, source)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (server, e["pkg_key"], e["name"], e["kind"],
                     e.get("old_version"), e.get("new_version"), e.get("source")),
                )


def all_packages() -> list[dict]:
    """Inventario de todos os servidores (base da matriz de comparacao)."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM host_packages ORDER BY lower(name), server").fetchall()


def recent_package_events(limit: int = 300, server: str | None = None,
                          days: int = 90) -> list[dict]:
    sql = ["SELECT * FROM package_events WHERE ts > now() - (%s || ' days')::interval"]
    params: list[Any] = [str(int(days))]
    if server:
        sql.append("AND server=%s")
        params.append(server)
    sql.append("ORDER BY ts DESC LIMIT %s")
    params.append(int(limit))
    with _conn() as c:
        return _epoch(c.execute(" ".join(sql), params).fetchall())


def record_inventory_run(server: str, ok: bool, packages: int, error: str | None,
                         duration_ms: int, computer: str | None = None,
                         os_name: str | None = None) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO inventory_runs (server, ts, ok, packages, error, duration_ms, computer, os)
               VALUES (%s, now(), %s,%s,%s,%s,%s,%s)
               ON CONFLICT (server) DO UPDATE SET
                 ts=now(), ok=EXCLUDED.ok, packages=EXCLUDED.packages, error=EXCLUDED.error,
                 duration_ms=EXCLUDED.duration_ms,
                 computer=COALESCE(EXCLUDED.computer, inventory_runs.computer),
                 os=COALESCE(EXCLUDED.os, inventory_runs.os)""",
            (server, bool(ok), int(packages), error, int(duration_ms), computer, os_name),
        )


def inventory_runs() -> dict[str, dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM inventory_runs").fetchall()
    return {r["server"]: r for r in _epoch(rows)}
