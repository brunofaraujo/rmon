"""Notificacoes: Telegram. Inerte se nao estiver configurado no .env."""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("rmon.notify")


def _telegram(text: str) -> bool:
    token = os.environ.get("RMON_TELEGRAM_TOKEN")
    chat = os.environ.get("RMON_TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("falha Telegram: %s", exc)
        return False


def enabled() -> bool:
    return bool(os.environ.get("RMON_TELEGRAM_TOKEN") and os.environ.get("RMON_TELEGRAM_CHAT_ID"))


def send(text: str) -> bool:
    return _telegram(text)
