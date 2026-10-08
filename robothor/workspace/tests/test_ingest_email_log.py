"""The email-log.json writer: merge-not-truncate, atomic, bounded."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robothor.events import contract
from robothor.workspace.ingest import email_log
from robothor.workspace.ingest.email_log import EmailLog, merge_entries, new_entry

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _entry(mid: str, thread: str | None = None, *, at: datetime = NOW) -> dict:
    return new_entry(
        message_id=mid,
        thread_id=thread or mid,
        sender="Alice <alice@example.com>",
        subject="Hello",
        date="Wed, 07 Oct 2026 12:00:00 +0000",
        labels=["UNREAD", "INBOX"],
        provider="microsoft365",
        fetched_at=at,
    )


def test_a_new_entry_matches_the_contract() -> None:
    contract.validate("email_log", {"lastCheckedAt": None, "entries": {"m1": _entry("m1")}})


def test_merge_keeps_foreign_entries_and_never_re_adds_a_known_id() -> None:
    triaged = dict(_entry("m1"), category="sales", categorizedAt="2026-10-07T11:00:00")
    log = {"lastCheckedAt": None, "entries": {"m1": triaged}, "extra": {"kept": True}}
    added = merge_entries(log, [_entry("m1"), _entry("m2")], now=NOW)
    assert added == ["m2"]
    assert log["entries"]["m1"]["category"] == "sales"
    assert log["extra"] == {"kept": True}
    assert log["lastCheckedAt"] == NOW.isoformat()


def test_a_reply_resets_the_conversations_parent_for_re_triage() -> None:
    parent = dict(_entry("m1", "conv"), category="sales", reviewedAt="2026-10-07T10:00:00")
    log = {"entries": {"m1": parent}}
    merge_entries(log, [_entry("m2", "conv")], now=NOW)
    assert log["entries"]["m1"]["category"] is None
    assert log["entries"]["m1"]["reviewedAt"] is None
    assert log["entries"]["m1"]["resetByReplyId"] == "m2"


def test_a_just_answered_parent_is_not_reset() -> None:
    answered = (NOW - timedelta(seconds=30)).isoformat()
    parent = dict(_entry("m1", "conv"), category="sales", actionCompletedAt=answered)
    log = {"entries": {"m1": parent}}
    merge_entries(log, [_entry("m2", "conv")], now=NOW)
    assert log["entries"]["m1"]["category"] == "sales"


def test_the_log_is_capped_dropping_the_oldest() -> None:
    log = {"entries": {}}
    old = [_entry(f"m{i}", at=NOW - timedelta(minutes=10 - i)) for i in range(5)]
    merge_entries(log, old, now=NOW, max_entries=3)
    assert sorted(log["entries"]) == ["m2", "m3", "m4"]


def test_the_file_merge_preserves_what_is_there(tmp_path) -> None:
    path = tmp_path / "email-log.json"
    path.write_text(json.dumps({"lastCheckedAt": "x", "entries": {"g1": {"id": "g1", "k": 1}}}))
    EmailLog(path).merge_sync([_entry("m1")], now=NOW)
    data = json.loads(path.read_text())
    assert data["entries"]["g1"] == {"id": "g1", "k": 1}
    assert data["entries"]["m1"]["id"] == "m1"
    assert (tmp_path / ".email-log.lock").exists()
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_a_failed_write_leaves_the_old_file_whole(tmp_path, monkeypatch) -> None:
    path = tmp_path / "email-log.json"
    original = json.dumps({"lastCheckedAt": None, "entries": {"g1": {"id": "g1"}}})
    path.write_text(original)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(email_log.json, "dump", boom)
    with pytest.raises(OSError):
        EmailLog(path).merge_sync([_entry("m1")], now=NOW)
    assert path.read_text() == original
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_the_write_is_a_rename_over_the_old_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "email-log.json"
    renames: list[tuple[str, str]] = []
    real = Path.replace

    def spy(self, target):
        renames.append((str(self.parent), str(target)))
        return real(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    EmailLog(path).merge_sync([_entry("m1")], now=NOW)
    assert renames == [(str(tmp_path), str(path))]


def test_an_unreadable_log_is_moved_aside_not_overwritten(tmp_path) -> None:
    path = tmp_path / "email-log.json"
    path.write_text("{not json")
    EmailLog(path).merge_sync([_entry("m1")], now=NOW)
    assert list(json.loads(path.read_text())["entries"]) == ["m1"]
    assert [p.name for p in tmp_path.glob("email-log.corrupt.*.json")]


def test_the_default_path_is_the_workspace_memory_dir(monkeypatch, tmp_path) -> None:
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_EMAIL_LOG_PATH", raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    reset_settings()
    try:
        assert email_log.default_path() == tmp_path / "brain" / "memory" / "email-log.json"
        monkeypatch.setenv("ROBOTHOR_EMAIL_LOG_PATH", str(tmp_path / "x.json"))
        reset_settings()
        assert email_log.default_path() == tmp_path / "x.json"
    finally:
        reset_settings()


def test_the_default_path_is_the_one_the_dashboards_read(monkeypatch, tmp_path) -> None:
    import importlib

    from robothor.engine.dashboards import data
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_EMAIL_LOG_PATH", raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    reset_settings()
    try:
        reloaded = importlib.reload(data)
        assert email_log.default_path() == reloaded.MEMORY_DIR / "email-log.json"
    finally:
        monkeypatch.undo()
        importlib.reload(data)
        reset_settings()
