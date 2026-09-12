"""Fakes for the dependencies the checks reach through the context.

Every check takes its database, its HTTP client and its settings from the
context, which is the whole reason the context exists: a check can be exercised
against a dictionary and a fake cursor, with no PostgreSQL, no engine and no
network anywhere in the suite.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from typing import Any

import pytest

from robothor.doctor.context import DoctorContext, HttpResponse


class FakeCursor:
    """A cursor answering from the CONNECTION's queue of scripted rows.

    The queue is shared rather than copied, so a test can script "0 rows, then
    1 row" and have the second answer reach a re-run of the same check -- which
    is how a repair is proved to have changed something.
    """

    def __init__(self, rows: list[Any], error: Exception | None = None) -> None:
        self._rows = rows
        self._error = error
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        if self._error is not None:
            raise self._error
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[Any]:
        rows, self._rows = self._rows, []
        return rows


class FakeConnection:
    def __init__(self, rows: list[Any], error: Exception | None = None) -> None:
        self.cursors: list[FakeCursor] = []
        self._rows = rows
        self._error = error
        self.commits = 0

    def cursor(self, *_args: Any, **_kwargs: Any) -> FakeCursor:
        cursor = FakeCursor(self._rows, self._error)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1


def fake_db(rows: list[Any] | None = None, *, error: Exception | None = None):
    """A ``db_factory`` yielding a :class:`FakeConnection`.

    ``error`` is raised from the FACTORY when there is no connection to give
    (an unreachable database) and from ``execute`` when there is a connection
    but the statement fails (a missing table).
    """
    connection = FakeConnection(rows or [], error if rows else None)

    @contextlib.contextmanager
    def factory():
        if error is not None and not rows:
            raise error
        yield connection

    factory.connection = connection  # type: ignore[attr-defined]
    return factory


def fake_http(routes: dict[str, HttpResponse], default: HttpResponse | None = None):
    """An ``http_fetch`` answering from a URL table."""
    calls: list[str] = []

    def fetch(url: str, _timeout: float) -> HttpResponse:
        calls.append(url)
        return routes.get(url, default or HttpResponse(status=0, error="URLError: refused"))

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def make_ctx(**kwargs: Any) -> DoctorContext:
    """A context with a short budget and no real dependency unless given one."""
    kwargs.setdefault("timeout_s", 2.0)
    return DoctorContext(**kwargs)


@pytest.fixture
def ctx() -> DoctorContext:
    return make_ctx()


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """None of this box's own configuration reaches a check under test.

    The suite runs on a machine with a real instance configured; without this
    a check would read a live workspace, a live owner email and a live token,
    and the result would depend on whose machine it is.
    """
    from robothor.settings import reset_settings
    from robothor.settings.aliases import DEPRECATED_ALIASES, reset_alias_warnings

    for name in list(os.environ):
        # Provider credentials too. They are not ROBOTHOR_/GENUS_ names, so the
        # sibling fixtures in tests/ leave them alone -- and a models check that
        # consults the credential pool then reads whatever this machine happens
        # to export. That made two tests green here and red on CI, which is the
        # precise failure this fixture exists to prevent.
        if (
            name.startswith(("ROBOTHOR_", "GENUS_"))
            or name.endswith(("_API_KEY", "_API_TOKEN"))
            or name in DEPRECATED_ALIASES
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", tempfile.mkdtemp(prefix="genus-doctor-"))
    reset_alias_warnings()
    reset_settings()
    yield
    reset_settings()
    reset_alias_warnings()
