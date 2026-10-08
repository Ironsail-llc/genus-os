"""Mailbox verification is Gmail-only, and fail-closed on Microsoft 365.

The anti-spoofing rule in :func:`extract_verification` trusts an
``Authentication-Results`` header only when it starts ``mx.google.com;`` --
Gmail's receiving MTA adds it, so a sender cannot. An Exchange mailbox gets no
such header from Google, but a SENDER can write one into the message: on
Microsoft 365 that header proves nothing, and accepting it would let anyone
who can mail the assistant mint a "verified" website code. Until the Exchange
equivalent has its own security review, ``workspace_provider=microsoft365``
disables mailbox verification outright: no Gmail query, no mailbox read, no
resource consumed, and a reason the agent can relay.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from robothor.autonomy.tests.test_verification import email, read

REASON = "mailbox verification is not supported on Microsoft 365 yet"


@pytest.fixture
def microsoft365(monkeypatch: pytest.MonkeyPatch) -> None:
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE_PROVIDER", "microsoft365")
    reset_settings()


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> None:
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_WORKSPACE_PROVIDER", raising=False)
    reset_settings()


def test_google_still_extracts_an_authenticated_code(google: None) -> None:
    assert read(email()) == "123456"


def test_microsoft365_never_trusts_a_google_authentication_header(microsoft365: None) -> None:
    """The exact message Google accepts: on Exchange the header is the sender's."""
    assert read(email()) is None


async def test_microsoft365_retrieval_refuses_before_any_mailbox_or_store_call(
    microsoft365: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from robothor.autonomy.mailbox import retrieve_verification
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import gws

    def never(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("mailbox verification touched the mailbox on microsoft365")

    monkeypatch.setitem(gws.HANDLERS, "gws_gmail_search", never)
    monkeypatch.setattr(gws, "_fetch_message", never)
    monkeypatch.setattr(gws, "_run_gws", never)
    store = MagicMock()

    result = await retrieve_verification(
        store,
        MagicMock(owner_id="person:alice"),
        "op-1",
        {"proposal": {"origin": "https://shop.example"}, "created_epoch": 0},
        {"profile_id": "00000000-0000-0000-0000-000000000001"},
        ToolContext(agent_id="main", tenant_id="default"),
    )

    assert result["error"] == "mailbox_verification_unsupported"
    assert result["reason"] == REASON
    assert "id" not in result
    assert store.method_calls == [], "a refused retrieval must not consume the profile"
