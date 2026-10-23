"""scripts/devops_send_emails.py: one message to all recipients; fail on unresolvable."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "devops_send_emails.py"


def _load():
    spec = importlib.util.spec_from_file_location("devops_send_emails_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["devops_send_emails_script"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def run_dir(tmp_path):
    week = tmp_path / "2026-05-11"
    week.mkdir()
    (week / "report.html").write_text("<html>report</html>")
    (week / "meta.json").write_text(json.dumps({"subject": "Dev Team Operations Report — W"}))
    return tmp_path


def _args(run_dir, *extra):
    return ["--week-start", "2026-05-11", "--runs-dir", str(run_dir), *extra]


def test_env_recipients_get_one_message(run_dir, monkeypatch):
    mod = _load()
    sent = []
    monkeypatch.setenv("DEVOPS_REPORT_RECIPIENTS", "alice@example.com, bob@example.com")
    monkeypatch.setattr(mod, "_send_html", lambda to, s, h: sent.append((to, s, h)) or {})
    monkeypatch.setattr(mod, "_load_recipients", lambda: pytest.fail("memory block not used"))
    assert mod.main(_args(run_dir)) == 0
    assert len(sent) == 1
    assert sent[0][0] == ["alice@example.com", "bob@example.com"]
    assert sent[0][1] == "Dev Team Operations Report — W"


def test_to_override_wins(run_dir, monkeypatch):
    mod = _load()
    sent = []
    monkeypatch.setenv("DEVOPS_REPORT_RECIPIENTS", "alice@example.com")
    monkeypatch.setattr(mod, "_send_html", lambda to, s, h: sent.append(to) or {})
    assert mod.main(_args(run_dir, "--to", "carol@example.com")) == 0
    assert sent == [["carol@example.com"]]


def test_memory_block_mixed_entries(run_dir, monkeypatch):
    mod = _load()
    sent = []
    monkeypatch.delenv("DEVOPS_REPORT_RECIPIENTS", raising=False)
    monkeypatch.setattr(
        mod,
        "_load_recipients",
        lambda: [
            {"name": "Alice", "person_id": "p1"},
            {"name": "Bob", "email": "bob@example.com"},
            "dave@example.com",
        ],
    )
    monkeypatch.setattr(mod, "_lookup_email", lambda pid: "alice@example.com")
    monkeypatch.setattr(mod, "_send_html", lambda to, s, h: sent.append(to) or {})
    assert mod.main(_args(run_dir)) == 0
    assert sent == [["alice@example.com", "bob@example.com", "dave@example.com"]]


def test_unresolvable_recipient_exits_nonzero(run_dir, monkeypatch):
    mod = _load()
    sent = []
    monkeypatch.delenv("DEVOPS_REPORT_RECIPIENTS", raising=False)
    monkeypatch.setattr(
        mod,
        "_load_recipients",
        lambda: [{"name": "Alice", "person_id": "p1"}, {"name": "Ghost", "person_id": "p2"}],
    )
    monkeypatch.setattr(
        mod, "_lookup_email", lambda pid: "alice@example.com" if pid == "p1" else None
    )
    monkeypatch.setattr(mod, "_send_html", lambda to, s, h: sent.append(to) or {})
    assert mod.main(_args(run_dir)) == 1
    assert sent == [["alice@example.com"]]


def test_invalid_env_address_is_unresolvable(run_dir, monkeypatch):
    mod = _load()
    monkeypatch.setenv("DEVOPS_REPORT_RECIPIENTS", "alice@example.com,not-an-email")
    monkeypatch.setattr(mod, "_send_html", lambda to, s, h: {})
    assert mod.main(_args(run_dir)) == 1


def test_missing_report_fails(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setenv("DEVOPS_REPORT_RECIPIENTS", "alice@example.com")
    assert mod.main(_args(tmp_path)) == 1
