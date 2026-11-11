"""Alertas saem so pelo Telegram: um webhook do Slack esquecido no .env nao e usado."""
from app import notify


class _Resp:
    status_code = 200


def test_envia_so_ao_telegram(monkeypatch):
    chamadas = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: chamadas.append(url) or _Resp())
    monkeypatch.setenv("RMON_TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("RMON_TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("RMON_SLACK_WEBHOOK", "https://hooks.slack.com/services/x")

    assert notify.send("teste") is True
    assert chamadas == ["https://api.telegram.org/bott/sendMessage"]


def test_slack_sozinho_nao_liga_alertas(monkeypatch):
    chamadas = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: chamadas.append(url) or _Resp())
    monkeypatch.delenv("RMON_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("RMON_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("RMON_SLACK_WEBHOOK", "https://hooks.slack.com/services/x")

    assert notify.enabled() is False
    assert notify.send("teste") is False
    assert chamadas == []
