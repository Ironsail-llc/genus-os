"""The in-memory chat session map is a bounded cache, not a leak.

With per-user sessions enforced every member gets a long-lived entry in
``chat._sessions`` that nothing removed. History per session was capped; the
number of sessions was not. The store in PostgreSQL is the truth, so an idle
session can leave memory and come back from the store on its next message.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from robothor.engine import chat


@pytest.fixture(autouse=True)
def _clean_map(monkeypatch):
    chat._sessions.clear()
    chat._evicted.clear()
    monkeypatch.setattr(chat, "load_session", lambda *_a, **_k: {})
    yield
    chat._sessions.clear()
    chat._evicted.clear()


def _touch(key: str, at: float) -> chat.ChatSession:
    session = chat._get_session(key)
    session.last_used = at
    return session


def test_under_the_cap_nothing_is_evicted(monkeypatch):
    monkeypatch.setattr(chat._cache, "max_sessions", 10)
    for k in "abc":
        _touch(k, 1.0)
    assert chat.session_count() == 3


def test_a_new_session_beyond_the_cap_evicts_the_least_recently_used_idle_one(monkeypatch):
    monkeypatch.setattr(chat._cache, "max_sessions", 3)
    _touch("a", 1.0)
    _touch("b", 2.0)
    _touch("c", 3.0)
    _touch("a", 4.0)  # a is now the most recent
    chat._get_session("d")
    assert set(chat._sessions) == {"a", "c", "d"}


def test_a_session_with_a_running_task_is_never_evicted(monkeypatch):
    monkeypatch.setattr(chat._cache, "max_sessions", 2)
    busy = _touch("busy", 1.0)
    task = MagicMock()
    task.done.return_value = False
    busy.active_task = task
    _touch("idle", 2.0)
    chat._get_session("new")
    assert "busy" in chat._sessions
    assert "idle" not in chat._sessions


def test_a_session_with_a_pending_plan_is_never_evicted(monkeypatch):
    monkeypatch.setattr(chat._cache, "max_sessions", 2)
    planning = _touch("planning", 1.0)
    planning.active_plan = MagicMock()
    _touch("idle", 2.0)
    chat._get_session("new")
    assert "planning" in chat._sessions
    assert "idle" not in chat._sessions


def test_the_main_session_is_never_evicted(monkeypatch):
    monkeypatch.setattr(chat._cache, "max_sessions", 2)
    main_key = chat.get_main_session_key()
    _touch(main_key, 1.0)
    _touch("x", 2.0)
    chat._get_session("y")
    assert main_key in chat._sessions
    assert "x" not in chat._sessions


def test_when_nothing_is_evictable_the_map_grows_rather_than_dropping_live_work(monkeypatch):
    monkeypatch.setattr(chat._cache, "max_sessions", 1)
    busy = _touch("busy", 1.0)
    task = MagicMock()
    task.done.return_value = False
    busy.active_task = task
    chat._get_session("new")
    assert set(chat._sessions) == {"busy", "new"}


def test_an_evicted_session_comes_back_from_the_store_on_its_next_message(monkeypatch):
    monkeypatch.setattr(chat._cache, "max_sessions", 1)
    loaded: list[str] = []

    def _load(key, *_a, **_k):
        loaded.append(key)
        return {"history": [{"role": "user", "content": "hi"}], "model_override": "m"}

    monkeypatch.setattr(chat, "load_session", _load)
    _touch("old", 1.0)
    chat._get_session("newer")  # evicts old
    assert "old" not in chat._sessions

    back = chat._get_session("old")
    assert loaded == ["old"], "only an evicted key is rehydrated; fresh keys never hit the store"
    assert back.history == [{"role": "user", "content": "hi"}]
    assert back.model_override == "m"


def test_a_store_failure_on_rehydration_yields_an_empty_session_not_a_crash(monkeypatch):
    monkeypatch.setattr(chat._cache, "max_sessions", 1)

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(chat, "load_session", _boom)
    _touch("old", 1.0)
    chat._get_session("newer")
    back = chat._get_session("old")
    assert back.history == []


def test_idle_sweep_removes_only_sessions_idle_longer_than_the_ttl(monkeypatch):
    monkeypatch.setattr(chat._cache, "max_sessions", 100)
    _touch("stale", 0.0)
    _touch("fresh", 900.0)
    removed = chat.evict_idle_sessions(ttl_s=600, now=1000.0)
    assert removed == 1
    assert set(chat._sessions) == {"fresh"}


def test_accessing_a_session_refreshes_its_recency(monkeypatch):
    monkeypatch.setattr(chat._cache, "max_sessions", 100)
    s = _touch("k", 0.0)
    chat._get_session("k")
    assert s.last_used > 0.0
