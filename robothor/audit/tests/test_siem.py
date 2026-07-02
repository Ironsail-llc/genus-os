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


class _FakeResp:
    def __init__(self):
        self.raised = False

    def raise_for_status(self):
        self.raised = True


def test_webhook_forward(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_SIEM_WEBHOOK_URL", "http://siem.example/intake")
    monkeypatch.delenv("ROBOTHOR_SIEM_SYSLOG_HOST", raising=False)
    import httpx

    captured = {}
    resp = _FakeResp()

    def _post(url, **k):
        captured.update({"url": url, **k})
        return resp

    monkeypatch.setattr(httpx, "post", _post)
    # Drive the blocking path directly (forward_event offloads to a thread).
    siem._forward_event_blocking(_EVENT)
    assert captured["url"] == "http://siem.example/intake"
    assert captured["json"] == _EVENT
    assert resp.raised is True  # response status is checked, not silently dropped


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
    siem._forward_event_blocking(_EVENT)  # must not raise


def test_forward_event_is_non_blocking(monkeypatch):
    """forward_event must return immediately even if the sink hangs — the I/O
    runs on a daemon thread so the audited operation never stalls."""
    import threading

    monkeypatch.setenv("ROBOTHOR_SIEM_WEBHOOK_URL", "http://siem.example/intake")
    monkeypatch.delenv("ROBOTHOR_SIEM_SYSLOG_HOST", raising=False)
    import httpx

    started = threading.Event()
    release = threading.Event()

    def _hang(*a, **k):
        started.set()
        release.wait(2.0)  # simulate a slow/dead sink
        return _FakeResp()

    monkeypatch.setattr(httpx, "post", _hang)
    siem.forward_event(_EVENT)  # returns without waiting on _hang
    assert started.wait(1.0)  # the forward really ran, on another thread
    release.set()


def test_empty_event_noop():
    siem.forward_event({})  # no targets touched, no error
