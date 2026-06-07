"""Audit-to-SIEM forwarder (Wave-2, W2-21)."""

from __future__ import annotations

import json

from robothor.audit import siem

_EVENT = {"id": 1, "event_type": "tool.call", "action": "exec", "status": "ok"}


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_SIEM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ROBOTHOR_SIEM_SYSLOG_HOST", raising=False)
    assert siem.siem_enabled() is False


def test_enabled_with_webhook(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SIEM_WEBHOOK_URL", "http://siem.example/intake")
    assert siem.siem_enabled() is True


def test_webhook_forward(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SIEM_WEBHOOK_URL", "http://siem.example/intake")
    monkeypatch.delenv("ROBOTHOR_SIEM_SYSLOG_HOST", raising=False)
    import httpx

    captured = {}
    monkeypatch.setattr(httpx, "post", lambda url, **k: captured.update({"url": url, **k}))
    siem.forward_event(_EVENT)
    assert captured["url"] == "http://siem.example/intake"
    assert captured["json"] == _EVENT


def test_syslog_format_is_rfc5424():
    line = siem.format_syslog(_EVENT)
    assert line.startswith("<13>1 ")
    assert "robothor" in line
    # the JSON payload is appended
    assert json.loads(line.split(" - ", 3)[-1])["event_type"] == "tool.call"


def test_forward_never_raises(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SIEM_WEBHOOK_URL", "http://siem.example/intake")
    import httpx

    def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(httpx, "post", _boom)
    siem.forward_event(_EVENT)  # must not raise


def test_empty_event_noop():
    siem.forward_event({})  # no targets touched, no error
