"""``identity.tenant_name``: the tenant has a real display name.

The agent now tells every conversation which organisation it serves, by the
tenant's display name. A tenant still carrying the migration seed's
``Default Instance``, its own id (what ``genus tenant create`` falls back to
without ``--name``), or nothing at all would have the agent introduce the
organisation by a placeholder. A warning, not a failure: nothing breaks.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.doctor.checks import identity as identity_checks
from robothor.doctor.tests.conftest import fake_db, make_ctx


def _run(rows):
    check = next(c for c in identity_checks.CHECKS if c.id == "identity.tenant_name")
    ctx = make_ctx(db_factory=fake_db(rows))
    answer = asyncio.run(check.run(ctx))
    return check, ctx, (answer if isinstance(answer, list) else [answer])


def test_the_check_is_a_warning() -> None:
    check = next(c for c in identity_checks.CHECKS if c.id == "identity.tenant_name")
    assert check.severity == "recommended"
    assert check.category == "identity"


def test_a_real_name_passes(monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "acme-parent")
    _check, ctx, rows = _run([("Acme Holdings",)])
    (row,) = rows
    assert row.status == "pass"
    assert "Acme Holdings" in row.detail
    (cursor,) = ctx.db_factory.connection.cursors
    assert cursor.executed[0][1] == ("acme-parent",)


@pytest.mark.parametrize("name", ["Default Instance", "", None, "acme-parent"])
def test_a_scaffold_or_missing_name_warns(monkeypatch, name) -> None:
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "acme-parent")
    _check, _ctx, rows = _run([(name,)])
    (row,) = rows
    assert row.status == "fail"
    assert "acme-parent" in row.detail
    assert "display name" in row.detail


def test_no_tenant_row_warns(monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "acme-parent")
    _check, _ctx, rows = _run([])
    (row,) = rows
    assert row.status == "fail"
    assert "no crm_tenants row" in row.detail


def test_an_unreachable_database_fails_without_raising(monkeypatch) -> None:
    check = next(c for c in identity_checks.CHECKS if c.id == "identity.tenant_name")
    ctx = make_ctx(db_factory=fake_db(error=RuntimeError("down")))
    row = asyncio.run(check.run(ctx))
    assert row.status == "fail"
