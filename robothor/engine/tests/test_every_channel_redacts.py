"""Every inbound channel, not only webchat.

Review R4. Round 1 routed the webchat path through ``chat.append_turn`` and the
report claimed "the two that remain are platform-generated system messages".
That was wrong: ``telegram.py`` appends the raw user turn to the persistent
session history and hands it to ``save_exchange_async``, which writes it to
``chat_messages`` — from which ``backfill_chat_embeddings`` sends it to an
embedding model.

Telegram is the incident's OWN channel. The operator pasted the token there.

So the redaction moved to the door every persisted turn passes through
(``chat_store.save_exchange``) rather than to each caller, and the Telegram
history append goes through ``append_turn`` like the webchat one. A per-caller
fix is a list of callers, and the caller that gets forgotten is the one that
matters — which is exactly what happened here.
"""

from __future__ import annotations

import json

import pytest

TOKEN = "ghp_FAKE0000_pasted_into_telegram_0000"


# ── the door to chat_messages ────────────────────────────────────────────────


def test_save_exchange_redacts_before_it_writes(monkeypatch):
    """The persistence door, asserted without a database.

    ``chat_messages`` outlives the session, lands in support bundles, and is
    read by ``backfill_chat_embeddings`` — which sends the text to an embedding
    model. Every channel reaches this one function, so redacting here covers
    the callers nobody has enumerated.
    """
    import robothor.engine.chat_store as chat_store

    seen: dict[str, str] = {}

    class _Cursor:
        def execute(self, _sql, params=None):
            if params and len(params) > 4 and isinstance(params[2], str):
                seen.setdefault(params[1] if isinstance(params[1], str) else "", "")

        def fetchone(self):
            return {"id": 1}

        def close(self):
            pass

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self, **_kw):
            return _Cursor()

        def commit(self):
            pass

    monkeypatch.setattr(chat_store, "get_connection", lambda: _Conn())

    captured: list[str] = []
    real_redact = chat_store.__dict__.get("redact")
    assert real_redact is None, "redact must be imported inside the function, not module-level"

    from robothor.secrets import redaction

    original = redaction.redact

    def spy(text):
        cleaned = original(text)
        captured.append(cleaned)
        return cleaned

    monkeypatch.setattr(redaction, "redact", spy)
    chat_store.save_exchange("telegram:1", f"store this: {TOKEN}", "Stored.")

    assert captured, "save_exchange did not redact its content at all"
    assert TOKEN not in json.dumps(captured, default=str)


# ── the Telegram session history ─────────────────────────────────────────────


def test_a_history_redacts_whatever_is_appended_to_it():
    """The property, asserted on the TYPE.

    Round 1 fixed webchat by editing its call sites, and the report claimed the
    rest were platform-generated. Twenty-one raw appends in four modules said
    otherwise — telegram, telegram_plan_mode, ide, channels/webchat. A list of
    blessed call sites is the thing that was wrong; this is the property that
    replaces it.
    """
    from robothor.engine.chat_history import ChatHistory

    history = ChatHistory()
    history.append({"role": "user", "content": f"store this: {TOKEN}"})
    assert TOKEN not in json.dumps(history, default=str)
    assert "store this" in json.dumps(history, default=str), (
        "redaction took the message, not the value"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h, row: h.append(row),
        lambda h, row: h.extend([row]),
        lambda h, row: h.insert(0, row),
        lambda h, row: h.__setitem__(slice(0, 0), [row]),
        lambda h, row: h.__iadd__([row]),
    ],
)
def test_every_way_in_is_covered(mutate):
    """``__setitem__`` is not theoretical: the trim idiom in every channel
    module is ``session.history[:] = session.history[-MAX:]``, a slice
    assignment, and a subclass covering only ``append`` would be bypassed by it
    the moment a history was rehydrated."""
    from robothor.engine.chat_history import ChatHistory

    history = ChatHistory()
    mutate(history, {"role": "user", "content": f"token {TOKEN}"})
    assert TOKEN not in json.dumps(list(history), default=str)


def test_a_session_uses_one():
    from robothor.engine.chat import ChatSession
    from robothor.engine.chat_history import ChatHistory

    assert isinstance(ChatSession().history, ChatHistory)


def test_a_rehydrated_session_keeps_the_property():
    """A session evicted and reloaded from the store used to come back as a
    plain list and lose the property for the rest of its life."""
    from robothor.engine.chat_history import as_history

    restored = as_history([{"role": "user", "content": f"token {TOKEN}"}])
    assert TOKEN not in json.dumps(list(restored), default=str)
    restored.append({"role": "user", "content": f"another {TOKEN}"})
    assert TOKEN not in json.dumps(list(restored), default=str)


def test_the_telegram_turn_is_redacted_where_it_is_appended():
    """The named finding, through the container rather than the call site."""
    from robothor.engine.chat import ChatSession

    session = ChatSession()
    session.history.append({"role": "user", "content": f"here: {TOKEN}"})
    session.history.append({"role": "assistant", "content": "Stored."})
    assert TOKEN not in json.dumps(list(session.history), default=str)
