"""What the autonomy prompt-hint lookup is allowed to cost, and to say.

``robothor.autonomy.availability.autonomy_active`` runs once per agent run to
decide whether this run pays for the autonomy system-prompt paragraph and the
autonomy half of the browser tool schema. It is a HINT. It gates no authority
— the broker re-checks ``check_authority`` on every operation — so nothing it
does may be allowed to cost a run.

Two ways it could, and both were real:

* **Latency.** It made three separate ``AutonomyStore.transaction()`` calls,
  and each one opens a fresh ``psycopg2.connect``. With ``connect_timeout=5``
  that is up to fifteen seconds per run against a hanging Postgres, and
  against a connected-but-slow server there was no statement timeout at all
  and therefore no bound. ``except Exception`` catches errors, not latency, so
  the "a prompt hint must not fail a run" promise was not being kept.
* **Silence.** Both swallows logged at DEBUG, so an enrolled owner whose
  lookup was broken got no signal at all — just an agent that kept asking for
  approval it already had. And the message interpolated the exception
  unsanitised, while the sibling best-effort path two lines away routed its
  own through ``sanitize_log``.

These tests need no database: they count connections and read log records.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from robothor.autonomy.availability import autonomy_active


@pytest.fixture
def offered(monkeypatch):
    """An instance that has opted in to personal automation."""
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_AUTONOMY_ENABLED", "true")
    reset_settings()
    yield
    reset_settings()


class _CountingStore:
    """Stands in for ``AutonomyStore``, counting connections, not queries.

    One ``transaction()`` is one ``psycopg2.connect``. That is the unit the
    latency bound is made of, so it is the unit the test counts.
    """

    def __init__(self, rows=None):
        self.transactions = 0
        self.statements: list[str] = []
        self._rows = rows or {}

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def _txn():
            self.transactions += 1
            yield self

        return _txn()

    # -- cursor surface --
    def execute(self, sql, params=None):
        self.statements.append(" ".join(str(sql).split()))
        self._last = sql

    def fetchall(self):
        if "user_accounts" in self._last:
            return [{"person_id": "p1"}]
        if "autonomy_grants" in self._last:
            return self._rows.get("grants", [])
        return []

    def fetchone(self):
        if "autonomy_settings" in self._last:
            return self._rows.get("settings", {"settings": {"enabled": True}})
        return None


@pytest.mark.usefixtures("offered")
class TestItStopsOpeningAConnectionPerQuestion:
    """Three became two. The third is ``identity.scope_for_actor``, which
    resolves WHOSE money a grant spends; collapsing it would mean copying that
    query into a second file, and a drift-prone duplicate of an
    identity-resolution query is the worse trade. ``scope_for_actor`` needs a
    ``cur`` parameter for the last one, which is a change to a file this
    branch does not own."""

    def _patched(self, monkeypatch, store):
        from robothor.autonomy.models import Scope

        monkeypatch.setattr(
            "robothor.autonomy.identity.scope_for_actor",
            lambda *_a: Scope(tenant_id="tenant", owner_id="person:p1"),
        )
        monkeypatch.setattr("robothor.autonomy.store.AutonomyStore", lambda *a, **k: store)

    def test_the_switch_and_the_grants_share_one_transaction(self, monkeypatch):
        store = _CountingStore()
        self._patched(monkeypatch, store)
        autonomy_active("tenant", "actor", "main")
        assert store.transactions == 1, (
            f"{store.transactions} connections after identity; each one is a "
            "psycopg2.connect bounded only by connect_timeout=5"
        )

    def test_the_transaction_bounds_its_own_statements(self, monkeypatch):
        """connect_timeout does nothing once the server has answered the TCP
        handshake and then stopped answering anything else."""
        store = _CountingStore()
        self._patched(monkeypatch, store)
        autonomy_active("tenant", "actor", "main")
        assert any("statement_timeout" in s for s in store.statements), store.statements


def _normalise(text: str) -> str:
    # Drop the quote characters too: the canonical copies are written as
    # adjacent string literals, so their source carries a `" "` seam in the
    # middle of the SQL that the runtime string does not have.
    return " ".join(text.replace('"', "").split())


@pytest.mark.parametrize(
    ("copied", "canonical"),
    [("_SETTINGS_SQL", "settings"), ("_GRANTS_SQL", "grants")],
)
def test_the_copied_store_queries_have_not_drifted(copied, canonical):
    """``availability`` reads these two itself, to pay one connection for both.

    ``AutonomyStore.settings`` and ``.grants`` remain the canonical
    definitions. A copy needs a guard, or the day somebody adds a column or a
    scoping clause there, the prompt hint keeps answering the old way.
    """
    import inspect

    from robothor.autonomy import availability
    from robothor.autonomy.store import AutonomyStore

    source = _normalise(inspect.getsource(getattr(AutonomyStore, canonical)))
    # Compare from the first column onward. The store selects columns this
    # hint has no use for (a grant's id and version); what must not drift is
    # the columns it DOES read, the table, and the scoping clause.
    projection = _normalise(getattr(availability, copied)).split("SELECT ", 1)[1]
    assert projection in source, (
        f"availability.{copied} no longer appears in AutonomyStore.{canonical}; "
        "one of the two was changed without the other"
    )


class TestItCannotOutlastItsWelcome:
    async def test_a_slow_lookup_is_abandoned_not_waited_on(self, monkeypatch):
        """``toolset_prep`` must return promptly even if the lookup does not.

        ``asyncio.to_thread`` is not cancellable, so the thread runs on; what
        matters is that the RUN stops waiting for it.
        """
        from robothor.engine import toolset_prep

        def slow(*_args, **_kwargs):
            time.sleep(5)
            return True

        monkeypatch.setattr("robothor.autonomy.availability.autonomy_active", slow)
        started = time.monotonic()
        result = await toolset_prep._autonomy_active("tenant", "actor", "main")
        elapsed = time.monotonic() - started

        assert result is False
        assert elapsed < 3.0, f"the run waited {elapsed:.1f}s on a prompt hint"

    async def test_a_prompt_hint_timeout_is_not_a_failed_run(self, monkeypatch):
        from robothor.engine import toolset_prep

        async def never(*_args, **_kwargs):
            await asyncio.sleep(30)

        monkeypatch.setattr(asyncio, "to_thread", never)
        assert await toolset_prep._autonomy_active("t", "a", "main") is False


class TestItSaysSomethingWhenItBreaks:
    @staticmethod
    def _resolves(monkeypatch):
        """Get past identity resolution so the store is what fails."""
        from robothor.autonomy.models import Scope

        monkeypatch.setattr(
            "robothor.autonomy.identity.scope_for_actor",
            lambda *_a: Scope(tenant_id="tenant", owner_id="person:p1"),
        )

    def test_a_broken_lookup_on_an_enrolled_instance_warns(self, monkeypatch, caplog, offered):
        self._resolves(monkeypatch)

        def boom(*_args, **_kwargs):
            raise RuntimeError('relation "autonomy_grants" does not exist')

        monkeypatch.setattr("robothor.autonomy.store.AutonomyStore", boom)
        with caplog.at_level(logging.WARNING, logger="robothor.autonomy.availability"):
            assert autonomy_active("tenant", "actor", "main") is False

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "an enrolled owner's broken lookup was silent"
        assert "RuntimeError" in warnings[0].getMessage()

    def test_an_instance_that_never_opted_in_stays_quiet(self, monkeypatch, caplog):
        """No flag, no lookup, and therefore nothing to say. A warning here
        would fire on every run of every unenrolled instance."""

        def boom(*_args, **_kwargs):
            raise RuntimeError("should not be reached")

        monkeypatch.setattr("robothor.autonomy.store.AutonomyStore", boom)
        with caplog.at_level(logging.DEBUG, logger="robothor.autonomy.availability"):
            assert autonomy_active("tenant", "actor", "main") is False
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_the_connection_password_does_not_reach_the_log(self, monkeypatch, caplog, offered):
        """psycopg2 puts the whole DSN in ``OperationalError``."""
        self._resolves(monkeypatch)

        def boom(*_args, **_kwargs):
            raise OSError(
                'connection to server at "db.internal" failed: '
                "dbname=robothor_prod password=hunter2seventeen"
            )

        monkeypatch.setattr("robothor.autonomy.store.AutonomyStore", boom)
        with caplog.at_level(logging.WARNING, logger="robothor.autonomy.availability"):
            autonomy_active("tenant", "actor", "main")

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "hunter2seventeen" not in logged, logged

    def test_a_forged_log_line_cannot_be_injected_through_the_message(
        self, monkeypatch, caplog, offered
    ):
        self._resolves(monkeypatch)

        def boom(*_args, **_kwargs):
            raise OSError("broken\nWARNING robothor: everything is fine")

        monkeypatch.setattr("robothor.autonomy.store.AutonomyStore", boom)
        with caplog.at_level(logging.WARNING, logger="robothor.autonomy.availability"):
            autonomy_active("tenant", "actor", "main")

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "\n" not in logged, repr(logged)
        assert "\\n" in logged, repr(logged)
