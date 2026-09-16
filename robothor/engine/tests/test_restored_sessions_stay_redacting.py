"""A session restored at boot must still redact. Every boot. Every session.

Review N1, and it is the shape the round-2 implementation named in its own
comment and then shipped anyway: "the shape of bug that passes every test
written against a fresh session".

``ChatHistory`` redacts on the way in, and round 2 wrapped the eviction/rehydrate
path. It did not wrap ``chat._restore_sessions`` (run at daemon startup) or
``telegram.py``'s own restore, both of which assigned a plain ``list`` straight
from the store. So after every deploy every live session — including
``agent:main:primary``, the one the operator pastes tokens into — held a plain
list again, ``telegram.py``'s verbatim append kept the raw token, and
``/chat/history`` served it out of RAM.

The fix is not a third call site. Assignment itself now wraps: ``ChatSession``
converts on ``__setattr__``, so there is no way to put a plain list there, from
any module, including ones not written yet. A list of blessed call sites is
what produced this finding twice.
"""

from __future__ import annotations

import json

import pytest

TOKEN = "ghp_FAKE0000_restored_from_the_store_0000"


def test_assigning_a_plain_list_produces_a_redacting_container():
    """The property that makes the two restore sites — and the next one —
    correct without being edited."""
    from robothor.engine.chat import ChatSession
    from robothor.engine.chat_history import ChatHistory

    session = ChatSession()
    session.history = [{"role": "user", "content": f"here: {TOKEN}"}]

    assert isinstance(session.history, ChatHistory)
    assert TOKEN not in json.dumps(list(session.history), default=str)


def test_rows_already_present_in_the_assigned_list_are_redacted():
    """Wrapping the container is not enough on its own: the rows restored FROM
    THE STORE are the ones that carry the credential, and they are already in
    the list at the moment it is assigned."""
    from robothor.engine.chat import ChatSession

    session = ChatSession()
    session.history = [
        {"role": "user", "content": f"store this: {TOKEN}"},
        {"role": "assistant", "content": "Stored."},
    ]
    assert TOKEN not in json.dumps(list(session.history), default=str)
    assert "store this" in json.dumps(list(session.history), default=str)


def test_an_append_after_the_restore_is_still_redacted():
    """The exact sequence: restore at boot, then Telegram's verbatim append."""
    from robothor.engine.chat import ChatSession

    session = ChatSession()
    session.history = []  # the plain list the restore used to hand over
    session.history.append({"role": "user", "content": f"token {TOKEN}"})
    assert TOKEN not in json.dumps(list(session.history), default=str)


def test_the_startup_restore_leaves_a_redacting_history(monkeypatch):
    """Driven through ``_restore_sessions`` itself, with the store stubbed.

    The suite tested fresh sessions and the cache path and never this one,
    which is why the finding survived a round.
    """
    import robothor.engine.chat as chat
    from robothor.engine.chat_history import ChatHistory

    monkeypatch.setattr(
        chat,
        "load_all_sessions",
        lambda **_kw: {
            "agent:main:primary": {
                "history": [{"role": "user", "content": f"pasted: {TOKEN}"}],
            }
        },
    )
    chat._sessions.pop("agent:main:primary", None)
    from robothor.engine.config import EngineConfig

    chat._restore_sessions(EngineConfig.from_env())

    session = chat.get_shared_session("agent:main:primary")
    assert isinstance(session.history, ChatHistory), (
        "the startup restore handed the session a plain list — after a deploy "
        "every live session loses the redaction"
    )
    assert TOKEN not in json.dumps(list(session.history), default=str)
    chat._sessions.pop("agent:main:primary", None)


def test_the_chat_history_endpoint_cannot_serve_a_restored_token(monkeypatch):
    """``/chat/history`` reads ``session.history`` out of RAM, so it served
    whatever the restore put there."""
    import robothor.engine.chat as chat

    monkeypatch.setattr(
        chat,
        "load_all_sessions",
        lambda **_kw: {
            "agent:main:primary": {"history": [{"role": "user", "content": f"k: {TOKEN}"}]}
        },
    )
    chat._sessions.pop("agent:main:primary", None)
    from robothor.engine.config import EngineConfig

    chat._restore_sessions(EngineConfig.from_env())

    served = list(chat.get_shared_session("agent:main:primary").history)
    assert TOKEN not in json.dumps(served, default=str)
    chat._sessions.pop("agent:main:primary", None)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        None,
        ({"role": "user", "content": f"tuple {TOKEN}"},),
        iter([{"role": "user", "content": f"iterator {TOKEN}"}]),
    ],
)
def test_every_shape_the_store_might_hand_back_is_handled(rows):
    """A restore that raised on an empty history, or on a generator, would take
    the daemon down at boot — a redaction that costs the instance its startup
    is not a redaction anybody keeps."""
    from robothor.engine.chat import ChatSession
    from robothor.engine.chat_history import ChatHistory

    session = ChatSession()
    session.history = rows
    assert isinstance(session.history, ChatHistory)
    assert TOKEN not in json.dumps(list(session.history), default=str)


def test_the_container_is_what_a_reviewer_sees():
    """A reviewer reading a bare ``session.history = rows`` has to be able to
    find out that it is safe."""
    from robothor.engine.chat import ChatSession
    from robothor.engine.chat_history import ChatHistory

    session = ChatSession()
    session.history = [{"role": "user", "content": "hello"}]
    assert type(session.history) is ChatHistory
