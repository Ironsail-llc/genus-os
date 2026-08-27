"""Precedence for who a run is acting for.

Extracted from `execute`. Identity drives the CURRENT USER prompt block and
person attribution, so getting the precedence wrong misattributes work to the
wrong human — or invents one for a run that has none.

    explicit `identity=` kwarg
      > webchat resolution from the database
      > legacy Telegram `|sender:` parse

The legacy parse is last on purpose: it trusts a string inside
`trigger_detail`, where the other two resolve against a record.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from robothor.engine.models import TriggerType
from robothor.engine.run_identity import resolve_run_identity


def _resolve(**kw):
    return resolve_run_identity(
        kw.pop("identity", None),
        agent_id=kw.pop("agent_id", "main"),
        trigger_type=kw.pop("trigger_type", TriggerType.WEBCHAT),
        trigger_detail=kw.pop("trigger_detail", None),
        user_id=kw.pop("user_id", "u-1"),
        user_role=kw.pop("user_role", "owner"),
        tenant_id=kw.pop("tenant_id", "t-1"),
        is_service_caller=kw.pop("is_service_caller", False),
    )


# ── Precedence ────────────────────────────────────────────────────────


def test_an_explicit_identity_wins_over_everything():
    sentinel = object()
    with patch("robothor.identity.resolve_identity") as db:
        assert _resolve(identity=sentinel) is sentinel

    db.assert_not_called()


def test_webchat_resolves_against_the_database():
    with patch("robothor.identity.resolve_identity", return_value="resolved") as db:
        assert _resolve(trigger_type=TriggerType.WEBCHAT) == "resolved"

    db.assert_called_once_with("webchat", "u-1", "t-1")


def test_the_legacy_telegram_parse_is_used_only_as_a_last_resort():
    result = _resolve(
        trigger_type=TriggerType.TELEGRAM,
        trigger_detail="msg-123|sender:Alice Example",
    )

    assert result.display_name == "Alice Example"
    assert result.channel == "telegram"


# ── Runs with no human on the other end ───────────────────────────────


def test_a_service_caller_on_webchat_is_not_given_an_identity():
    """A system-triggered run has no human on the other end. Inventing one
    attributes autonomous work to whoever last used the channel."""
    with patch("robothor.identity.resolve_identity") as db:
        assert _resolve(trigger_type=TriggerType.WEBCHAT, is_service_caller=True) is None

    db.assert_not_called()


def test_a_cron_run_has_no_identity():
    assert _resolve(trigger_type=TriggerType.CRON) is None


def test_telegram_without_a_sender_marker_has_no_identity():
    assert _resolve(trigger_type=TriggerType.TELEGRAM, trigger_detail="msg-123") is None


def test_telegram_with_no_trigger_detail_at_all_has_no_identity():
    assert _resolve(trigger_type=TriggerType.TELEGRAM, trigger_detail=None) is None


# ── The verified flag ─────────────────────────────────────────────────


def test_a_caller_with_id_and_role_is_verified():
    result = _resolve(
        trigger_type=TriggerType.TELEGRAM,
        trigger_detail="m|sender:Alice",
        user_id="u-1",
        user_role="owner",
    )
    assert result.verified is True


@pytest.mark.parametrize(
    ("user_id", "user_role"),
    [("", "owner"), ("u-1", ""), ("", "")],
    ids=["no-id", "no-role", "neither"],
)
def test_a_display_name_alone_does_not_make_a_caller_verified(user_id, user_role):
    """The name is parsed out of trigger_detail. It proves nothing about who
    sent it."""
    result = _resolve(
        trigger_type=TriggerType.TELEGRAM,
        trigger_detail="m|sender:Alice",
        user_id=user_id,
        user_role=user_role,
    )
    assert result.verified is False


def test_a_sender_containing_the_marker_again_keeps_the_whole_remainder():
    """`split(..., 1)` — a display name is not a place to lose characters."""
    result = _resolve(
        trigger_type=TriggerType.TELEGRAM,
        trigger_detail="m|sender:Alice|sender:Bob",
    )
    assert result.display_name == "Alice|sender:Bob"
