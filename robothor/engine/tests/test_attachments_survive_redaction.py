"""The attachment row survives redaction; the caption does not survive a secret.

Where the two branches meet. `feat/vault-managed-secrets` made
``chat_store.save_exchange`` the one door every persisted turn passes through
and redacts ``user_content``/``assistant_content`` there; the Telegram
attachment work writes the file list into ``user_extras``, which that function
passes to the JSONB **untouched**.

Two things have to be true at once and they pull in opposite directions:

* the ``attachments`` list must arrive INTACT — paths, sizes and ids are the
  record the Helm chat UI reads, and a redactor that mangled a path would break
  the feature while looking like it was protecting something;
* the ``caption`` inside that list is the operator's own free text, the same
  class as the message body, so a credential pasted there must be scrubbed. The
  hostile review flagged exactly this: the same words in the body would be
  redacted and in the caption would not.

Asserted against the real ``save_exchange`` with the connection faked, so what
is captured is the JSON that would have reached the column.
"""

from __future__ import annotations

import contextlib
import json

import pytest

TOKEN = "ghp_FAKE0000_pasted_as_a_photo_caption0"


@pytest.fixture
def written(monkeypatch):
    """Capture the JSONB payloads `save_exchange` would insert."""
    import robothor.engine.chat_store as chat_store

    payloads: list[dict] = []

    class _Cursor:
        def execute(self, _sql, params=None):
            if params and len(params) == 2 and isinstance(params[1], str):
                with contextlib.suppress(TypeError, ValueError):
                    payloads.append(json.loads(params[1]))

        def fetchone(self):
            return {"id": len(payloads) + 1}

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
    return payloads


ROW = {
    "path": "/w/inbox/telegram/100200300/2026-09-16/AgACpic-photo.jpg",
    "name": "photo.jpg",
    "kind": "image",
    "mime": "image/jpeg",
    "size": 245_300,
    "width": 1280,
    "height": 720,
    "telegram_file_id": "AgADBAADfake",
    "telegram_file_unique_id": "AgACpic",
    "caption": "",
}


def _user_turn(payloads):
    return next(p for p in payloads if p.get("role") == "user")


class TestTheListSurvives:
    def test_the_attachment_row_reaches_the_column_intact(self, written) -> None:
        import robothor.engine.chat_store as chat_store

        chat_store.save_exchange(
            "telegram:100200300",
            "here you go",
            "Got it.",
            user_extras=chat_store.build_user_extras(attachments=[dict(ROW)]),
        )
        stored = _user_turn(written)["attachments"]
        assert stored == [ROW], "the record the Helm chat UI reads must not be rewritten"

    def test_a_path_is_never_mistaken_for_a_secret(self, written) -> None:
        """A redactor let loose on the whole payload could eat a path or an id.
        Those are the feature, not the risk."""
        import robothor.engine.chat_store as chat_store

        chat_store.save_exchange(
            "telegram:100200300",
            "hi",
            "ok",
            user_extras=chat_store.build_user_extras(attachments=[dict(ROW)], user_message_id="7"),
        )
        turn = _user_turn(written)
        assert turn["attachments"][0]["path"] == ROW["path"]
        assert turn["attachments"][0]["telegram_file_id"] == ROW["telegram_file_id"]
        assert turn["telegram_message_id"] == "7"


class TestTheCaptionIsScrubbed:
    def test_a_credential_in_the_caption_does_not_reach_the_column(self, written) -> None:
        """The hostile review's cross-branch note: the same words in the body
        are redacted and in the caption were not."""
        import robothor.engine.chat_store as chat_store

        row = dict(ROW, caption=f"save this for me: {TOKEN}")
        chat_store.save_exchange(
            "telegram:100200300",
            f"save this for me: {TOKEN}",
            "Saved.",
            user_extras=chat_store.build_user_extras(attachments=[row]),
        )
        blob = json.dumps(written, default=str)
        assert TOKEN not in blob, "the caption carried the credential into chat_messages"
        assert "save this for me" in blob, "redaction took the sentence, not the value"

    def test_the_body_is_still_redacted(self, written) -> None:
        """The vault branch's own property, unbroken by the extras."""
        import robothor.engine.chat_store as chat_store

        chat_store.save_exchange(
            "telegram:100200300",
            f"store this: {TOKEN}",
            "Stored.",
            user_extras=chat_store.build_user_extras(attachments=[dict(ROW)]),
        )
        assert TOKEN not in json.dumps(written, default=str)

    def test_an_ordinary_caption_is_left_alone(self, written) -> None:
        import robothor.engine.chat_store as chat_store

        row = dict(ROW, caption="what's this?")
        chat_store.save_exchange(
            "telegram:100200300",
            "what's this?",
            "A whiteboard.",
            user_extras=chat_store.build_user_extras(attachments=[row]),
        )
        assert _user_turn(written)["attachments"][0]["caption"] == "what's this?"

    def test_the_caller_s_own_row_is_not_mutated(self, written) -> None:
        """Redacting must not reach back into the dict the handler still holds
        — the intake logs it and the album buffer keeps it."""
        import robothor.engine.chat_store as chat_store

        row = dict(ROW, caption=f"save this: {TOKEN}")
        extras = chat_store.build_user_extras(attachments=[row])
        chat_store.save_exchange("telegram:1", "x", "y", user_extras=extras)
        assert row["caption"] == f"save this: {TOKEN}"
