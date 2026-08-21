"""Carregamento de configuracao: variaveis de ambiente + inventario YAML."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _load_dotenv(path: str = ".env") -> None:
    """Carrega um .env simples para os.environ (para dev; em prod usa-se o systemd).

    Nao sobrescreve variaveis ja definidas no ambiente.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class WinRMConfig:
    scheme: str = "https"
    port: int = 5986
    transport: str = "ntlm"
    server_cert_validation: str = "ignore"
    read_timeout_sec: int = 30
    operation_timeout_sec: int = 25


@dataclass
class ServerConfig:
    name: str
    host: str
    services: list[str] = field(default_factory=list)
    # Padroes (curinga) expandidos no proprio host a cada coleta: o que casar
    # esta instalado, o que sumiu foi desinstalado e simplesmente nao aparece.
    service_patterns: list[str] = field(default_factory=list)
    app_health: dict[str, Any] | None = None
    cred: str = "default"
    jobs: dict[str, Any] | None = None


def credential_for(profile: str) -> tuple[str, str]:
    """Resolve (usuario, senha) WinRM para um perfil, lendo do ambiente.

    Procura RMON_CRED_<PERFIL>_USER / _PASSWORD. Se nao houver, cai no par
    legado RMON_WINRM_USER / RMON_WINRM_PASSWORD (perfil 'default').
    Nunca registra os valores.
    """
    prof = (profile or "default").strip().upper()
    user = os.environ.get(f"RMON_CRED_{prof}_USER")
    pw = os.environ.get(f"RMON_CRED_{prof}_PASSWORD")
    if user and pw:
        return user, pw
    return os.environ.get("RMON_WINRM_USER", ""), os.environ.get("RMON_WINRM_PASSWORD", "")


@dataclass
class Settings:
    secret_key: str
    admin_user: str
    admin_password_hash: str
    winrm_user: str
    winrm_password: str
    config_path: str
    db_path: str
    db_dsn: str
    host: str
    port: int
    tv_token: str
    cookie_samesite: str
    cookie_secure: bool


def load_settings() -> Settings:
    _load_dotenv(os.environ.get("RMON_DOTENV", ".env"))
    return Settings(
        secret_key=os.environ.get("RMON_SECRET_KEY", ""),
        admin_user=os.environ.get("RMON_ADMIN_USER", "admin"),
        admin_password_hash=os.environ.get("RMON_ADMIN_PASSWORD_HASH", ""),
        winrm_user=os.environ.get("RMON_WINRM_USER", ""),
        winrm_password=os.environ.get("RMON_WINRM_PASSWORD", ""),
        config_path=os.environ.get("RMON_CONFIG", "./config/servers.yaml"),
        db_path=os.environ.get("RMON_DB_PATH", "./data/rmon.db"),
        db_dsn=os.environ.get("RMON_DB_DSN", ""),
        host=os.environ.get("RMON_HOST", "127.0.0.1"),
        port=int(os.environ.get("RMON_PORT", "8080")),
        tv_token=os.environ.get("RMON_TV_TOKEN", "").strip(),
        cookie_samesite=(os.environ.get("RMON_COOKIE_SAMESITE", "lax").strip().lower() or "lax"),
        cookie_secure=(os.environ.get("RMON_COOKIE_SECURE", "").strip().lower()
                       in {"1", "true", "yes", "on"}),
    )


@dataclass
class Inventory:
    poll_interval_seconds: int
    winrm: WinRMConfig
    defaults: dict[str, Any]
    servers: list[ServerConfig]


def load_inventory(path: str) -> Inventory:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    winrm = WinRMConfig(**(data.get("winrm") or {}))
    defaults = data.get("defaults") or {}
    default_services = list((defaults.get("services") or []))
    default_patterns = list((defaults.get("service_patterns") or []))
    servers: list[ServerConfig] = []
    for raw in data.get("servers") or []:
        servers.append(
            ServerConfig(
                name=raw["name"],
                host=raw["host"],
                services=list(raw.get("services") or default_services),
                service_patterns=list(raw.get("service_patterns") or default_patterns),
                app_health=raw.get("app_health"),
                cred=raw.get("cred", "default"),
                jobs=raw.get("jobs"),
            )
        )
    return Inventory(
        poll_interval_seconds=int(data.get("poll_interval_seconds", 60)),
        winrm=winrm,
        defaults=defaults,
        servers=servers,
    )
