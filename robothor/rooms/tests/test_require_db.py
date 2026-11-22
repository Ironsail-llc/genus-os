"""``ROOMS_TESTS_REQUIRE_DB=1``: a rooms test with no database fails, never skips.

The rooms fixtures skip when Postgres cannot be reached, which keeps a laptop
without one usable; but then a run that lost its database looks green. Where a
database is meant to be there (CI, an evidence run), the switch makes the
missing database a failure.
"""

from __future__ import annotations

import pytest

from robothor.rooms.tests import conftest

_NOWHERE = "dbname=nothing_test user=nobody host=127.0.0.1 port=1 connect_timeout=1"


def test_without_the_switch_a_missing_database_skips(monkeypatch) -> None:
    monkeypatch.delenv("ROOMS_TESTS_REQUIRE_DB", raising=False)
    monkeypatch.setattr(conftest, "PG_DSN", _NOWHERE)
    with pytest.raises(pytest.skip.Exception):
        conftest.connect_test_db()


def test_with_the_switch_a_missing_database_fails(monkeypatch) -> None:
    monkeypatch.setenv("ROOMS_TESTS_REQUIRE_DB", "1")
    monkeypatch.setattr(conftest, "PG_DSN", _NOWHERE)
    with pytest.raises(pytest.fail.Exception, match="ROOMS_TESTS_REQUIRE_DB"):
        conftest.connect_test_db()
