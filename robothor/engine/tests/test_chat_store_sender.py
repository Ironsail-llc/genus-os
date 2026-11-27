"""Each stored user turn says who sent it, so memory can tell the owner's words apart."""

from __future__ import annotations

from pathlib import Path

from robothor.engine import chat_store

ROOT = Path(__file__).resolve().parents[3]


def test_a_telegram_turn_records_its_senders_channel_id() -> None:
    extras = chat_store.build_user_extras(user_message_id="7", sender_telegram_id="4242")
    assert extras == {"telegram_message_id": "7", "sender_telegram_id": "4242"}
    assert chat_store.build_user_extras(sender_telegram_id="4242") == {"sender_telegram_id": "4242"}


def test_a_webchat_turn_records_its_authenticated_member() -> None:
    assert chat_store.with_sender(None, user_id="member-1") == {"sender_user_id": "member-1"}
    assert chat_store.with_sender({"a": 1}, user_id="member-1") == {
        "a": 1,
        "sender_user_id": "member-1",
    }
    assert chat_store.with_sender({"a": 1}, user_id=None) == {"a": 1}
    assert chat_store.with_sender(None, user_id="") is None


def test_the_telegram_path_passes_the_sender() -> None:
    src = (ROOT / "robothor/engine/telegram.py").read_text()
    call = src[src.index("user_extras = build_user_extras(") :]
    assert "sender_telegram_id=" in call[: call.index(")\n")]


def test_the_webchat_path_passes_the_member() -> None:
    src = (ROOT / "robothor/engine/chat.py").read_text()
    call = src[src.index("saving = save_exchange_async(") :]
    assert "with_sender(" in call[: call.index("user_at=")]


async def test_history_never_shows_who_sent_a_turn(monkeypatch) -> None:  # noqa: ANN001
    """The sender is for memory's attribution only; no history reader sees it."""
    from types import SimpleNamespace

    from robothor.engine import chat_history_page

    stored = {
        "messages": [
            {"role": "user", "content": "hi", "sender_user_id": "member-1"},
            {
                "role": "user",
                "content": "yo",
                "sender_telegram_id": "4242",
                "telegram_message_id": "9",
            },
        ],
        "hasMore": False,
    }
    monkeypatch.setattr(chat_store, "load_history_page", lambda *a, **k: stored)
    page = await chat_history_page.history_page(
        [], "agent:main:primary", 50, {}, SimpleNamespace(tenant_id="t")
    )
    for message in page["messages"]:
        assert "sender_user_id" not in message and "sender_telegram_id" not in message
    assert page["messages"][1]["telegram_message_id"] == "9"  # everything else is unchanged
