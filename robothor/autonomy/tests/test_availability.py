"""The one fact that decides whether autonomy costs an unenrolled instance anything.

``autonomy_active`` gates the system-prompt paragraph and ~1,100 schema
tokens of browser wording. It has to say "no" for every instance that has not
opted in — which is nearly all of them — and it has to say "no" without ever
failing the run that asked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robothor.autonomy.availability import _covers, autonomy_active
from robothor.autonomy.models import Delegation, RuntimeSettings


def _grant(**changes):
    values = {
        "agent_ids": {"main"},
        "origins": {"https://shop.example"},
        "actions": {"purchase", "account"},
        "expires_at": datetime.now(UTC) + timedelta(days=30),
    }
    return Delegation(**(values | changes))


class TestOneGrantAtATime:
    """``_covers`` alone — no database, so every branch is cheap to pin."""

    def _row(self, policy, revoked=False):
        return {"policy": policy.model_dump(mode="json"), "revoked": revoked}

    def test_a_live_grant_naming_the_agent_counts(self):
        assert _covers(self._row(_grant()), "main") is True

    def test_a_grant_naming_another_agent_does_not(self):
        assert _covers(self._row(_grant(agent_ids={"scout"})), "main") is False

    def test_a_revoked_grant_does_not(self):
        assert _covers(self._row(_grant(), revoked=True), "main") is False

    def test_a_disabled_grant_does_not(self):
        assert _covers(self._row(_grant(enabled=False)), "main") is False

    def test_an_expired_grant_does_not(self):
        policy = _grant().model_dump(mode="json")
        policy["expires_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        assert _covers({"policy": policy, "revoked": False}, "main") is False

    def test_an_unparseable_row_does_not(self):
        assert _covers({}, "main") is False
        assert _covers({"policy": {"agent_ids": ["main"], "expires_at": "not-a-date"}}, "main") is (
            False
        )


class TestTheInstanceFlagComesFirst:
    """``ROBOTHOR_AUTONOMY_ENABLED`` ships OFF, and off means off.

    Everything else about autonomy — the resource vault, the grants, the
    dashboard page, the browser service — is per-owner state inside one
    instance's database. There was no switch for "this appliance does not
    offer personal automation at all", so the feature's surfaces shipped to
    every instance and were merely inert. Inert is not absent: the prompt
    paragraph and the schema wording were real tokens on real turns.
    """

    def test_it_is_off_by_default(self):
        from robothor.settings import get_settings

        assert get_settings().autonomy.enabled is False

    def test_a_live_grant_on_an_instance_with_the_feature_off_is_still_no(
        self, store, identity, monkeypatch
    ):
        store.create_grant(identity, _grant())
        monkeypatch.setattr("robothor.autonomy.identity.scope_for_actor", lambda *_a: identity)
        monkeypatch.setattr("robothor.autonomy.store.AutonomyStore", lambda *a, **k: store)
        assert autonomy_active(identity.tenant_id, "actor", "main") is False

    def test_the_flag_is_checked_before_any_query(self, monkeypatch):
        """On the overwhelming majority of instances this is the whole answer,
        and it must not cost a connection to reach it."""

        calls = []

        def record(*args, **_kwargs):
            # Not `raise`: ``autonomy_active`` swallows every exception on
            # purpose, so an assertion in here would be caught and the test
            # would pass for the wrong reason.
            calls.append(args)
            raise LookupError("unreachable")

        monkeypatch.setattr("robothor.autonomy.identity.scope_for_actor", record)
        assert autonomy_active("tenant", "actor", "main") is False
        assert calls == [], "the database was consulted with the feature off"


@pytest.fixture
def offered(monkeypatch):
    """An instance that has opted in to personal automation."""
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_AUTONOMY_ENABLED", "true")
    reset_settings()
    yield
    reset_settings()


@pytest.mark.usefixtures("offered")
class TestTheWholeQuestion:
    def test_no_identified_human_is_an_immediate_no(self):
        """A cron or sub-agent run has no owner, so it cannot be under a grant.

        It must also not touch the database to find that out — this is the
        common case on every instance, enrolled or not.
        """
        assert autonomy_active("tenant", None, "main") is False
        assert autonomy_active(None, "actor", "main") is False
        assert autonomy_active("tenant", "actor", None) is False

    def test_an_instance_with_no_autonomy_at_all_says_no_and_does_not_raise(self, monkeypatch):
        """No tables, no enrolment, database down — all the same answer."""

        def boom(*_args, **_kwargs):
            raise RuntimeError('relation "autonomy_settings" does not exist')

        monkeypatch.setattr("robothor.autonomy.identity.scope_for_actor", boom)
        assert autonomy_active("tenant", "actor", "main") is False

    def test_enabled_with_a_matching_grant_says_yes(self, store, identity, monkeypatch):
        store.create_grant(identity, _grant())
        monkeypatch.setattr("robothor.autonomy.identity.scope_for_actor", lambda *_a: identity)
        monkeypatch.setattr("robothor.autonomy.store.AutonomyStore", lambda *a, **k: store)
        assert autonomy_active(identity.tenant_id, "actor", "main") is True

    def test_enabled_with_no_grant_naming_this_agent_says_no(self, store, identity, monkeypatch):
        store.create_grant(identity, _grant(agent_ids={"scout"}))
        monkeypatch.setattr("robothor.autonomy.identity.scope_for_actor", lambda *_a: identity)
        monkeypatch.setattr("robothor.autonomy.store.AutonomyStore", lambda *a, **k: store)
        assert autonomy_active(identity.tenant_id, "actor", "main") is False

    def test_a_grant_on_a_disabled_instance_says_no(self, store, identity, monkeypatch):
        """The owner wrote a grant, then switched execution off. Off wins."""
        store.create_grant(identity, _grant())
        store.configure(identity, RuntimeSettings(enabled=False))
        monkeypatch.setattr("robothor.autonomy.identity.scope_for_actor", lambda *_a: identity)
        monkeypatch.setattr("robothor.autonomy.store.AutonomyStore", lambda *a, **k: store)
        assert autonomy_active(identity.tenant_id, "actor", "main") is False
