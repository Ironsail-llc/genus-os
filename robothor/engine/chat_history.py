"""A session history that cannot hold a credential, whoever appends to it.

Round 1 routed the webchat path through a redacting helper and the report said
the remaining appends were platform-generated system messages. They were not:
``telegram.py`` — the incident's OWN channel, where the operator pasted the
token — appended the raw user turn, and so did ``telegram_plan_mode.py``,
``ide.py`` and ``channels/webchat.py``. Twenty-one call sites in four modules.

A per-call-site fix is a LIST of call sites, and the one that gets forgotten is
the one that matters; that is the mistake that produced this finding. So the
redaction moved into the container. :class:`ChatHistory` is a ``list`` that
redacts on the way in, which means:

* every existing ``session.history.append(...)`` is covered without being
  edited, including the ones nobody has found yet,
* the next channel is covered before it is written,
* and a guard test can assert the property on the TYPE rather than maintaining
  a list of blessed call sites.

What is redacted is the TEXT, not the message. "here is the token:
<redacted>" keeps an operator's own history readable and says plainly that the
platform took the value out. The current turn is unaffected — it never enters
the history; it goes to the model as ``user_message``, which it must, because
an assistant that cannot read a token cannot store one.
"""

from __future__ import annotations

from typing import Any, SupportsIndex

__all__ = ["MAX_HISTORY", "ChatHistory", "append_turn", "as_history"]


def _clean(entry: Any) -> Any:
    """One history row with credentials taken out of it."""
    from robothor.secrets.redaction import redact_message

    return redact_message(entry) if isinstance(entry, dict) else entry


class ChatHistory(list):  # type: ignore[type-arg]
    """A list of chat messages that redacts every row it is given.

    Overrides every mutator that can introduce a row. ``__setitem__`` is not
    theoretical: the trim idiom used throughout the channel modules is
    ``session.history[:] = session.history[-MAX_HISTORY:]``, a slice assignment,
    and a subclass that covered only ``append`` would be silently bypassed by
    it the moment a history was rehydrated from the store.
    """

    def __init__(self, rows: Any = ()) -> None:
        super().__init__(_clean(row) for row in rows)

    def append(self, entry: Any) -> None:
        super().append(_clean(entry))

    def insert(self, index: SupportsIndex, entry: Any) -> None:
        super().insert(index, _clean(entry))

    def extend(self, rows: Any) -> None:
        super().extend(_clean(row) for row in rows)

    def __iadd__(self, rows: Any) -> ChatHistory:  # type: ignore[misc]
        """``history += rows``.

        Needed, and the test is what proved it: ``list.__iadd__`` is implemented
        in C and concatenates directly, so it does NOT go through the
        ``extend`` above. A comment claiming otherwise was written here and the
        parametrised "every way in" case failed on it immediately.

        The ``type: ignore`` is for mypy's ``__iadd__``/``__add__`` variance
        rule. Narrowing the return type to ``ChatHistory`` is the whole point:
        an in-place add must not quietly produce a plain list and lose the
        property.
        """
        self.extend(rows)
        return self

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            super().__setitem__(index, [_clean(row) for row in value])
        else:
            super().__setitem__(index, _clean(value))


def as_history(rows: Any) -> ChatHistory:
    """Wrap rows restored from the store, so a rehydrated session is covered too.

    Without this, a session evicted and reloaded came back with a plain ``list``
    and lost the property for the rest of its life — the shape of bug that
    passes every test written against a fresh session.
    """
    if isinstance(rows, ChatHistory):
        return rows
    if rows is None:
        return ChatHistory()
    # Materialised before wrapping: a generator handed here would be consumed
    # by the truthiness test and arrive empty, which is a silently emptied
    # history rather than a loud failure.
    return ChatHistory(list(rows))


#: How many rows a session keeps. Lives here with the container it bounds; a
#: caller that remembered to append and forgot to trim is how a session grows
#: without bound.
MAX_HISTORY = 40  # 20 turns (user + assistant)


def append_turn(session: Any, *, user_message: str, assistant_text: str | None = None) -> None:
    """Record one exchange in a session's history, and trim it.

    Lives beside :class:`ChatHistory` rather than in ``chat.py`` because it is
    the same concern — what enters a history and how much of it is kept — and
    because ``chat.py`` is on a decomposition ratchet that (rightly) makes a new
    behaviour pay for itself by extraction.

    Redaction is the container's job now, not this function's: every mutator on
    :class:`ChatHistory` redacts, so a caller that bypasses this helper is
    covered anyway. What this adds is the TRIM, and one obvious place for a
    caller to reach for.
    """
    session.history.append({"role": "user", "content": user_message})
    if assistant_text:
        session.history.append({"role": "assistant", "content": assistant_text})
    if len(session.history) > MAX_HISTORY:
        session.history[:] = session.history[-MAX_HISTORY:]
