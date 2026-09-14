"""Observability handlers refuse a non-UUID id before any SQL runs.

An LLM-fabricated short id ("2c777e07") reached the uuid-typed parameter and
crashed with psycopg2 InvalidTextRepresentation (live, 2026-09-14). The CRM
handlers got this guard in #527; these did not.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from robothor.engine.tools.handlers import observability as obs


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.tenant_id = "t"
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["2c777e07", "run_abc123", "", "not a uuid"])
async def test_get_agent_review_refuses_a_bad_id_without_querying(monkeypatch, bad):
    boom = MagicMock(side_effect=AssertionError("must not query"))
    monkeypatch.setattr("robothor.db.connection.get_connection", boom)
    out = await obs._get_agent_review({"review_id": bad}, _ctx())
    assert "error" in out
    assert "UUID" in out["error"]
    assert boom.call_count == 0


@pytest.mark.asyncio
async def test_get_agent_run_refuses_a_bad_id_without_querying(monkeypatch):
    import robothor.engine.tracking as tracking

    monkeypatch.setattr(tracking, "get_run", MagicMock(side_effect=AssertionError("no")))
    out = await obs._get_agent_run({"run_id": "2c777e07"}, _ctx())
    assert "error" in out and "UUID" in out["error"]


@pytest.mark.asyncio
async def test_classify_run_failure_refuses_a_bad_id_without_querying(monkeypatch):
    import robothor.engine.tracking as tracking

    monkeypatch.setattr(tracking, "get_run", MagicMock(side_effect=AssertionError("no")))
    out = await obs._classify_run_failure({"run_id": "run_xyz"}, _ctx())
    assert "error" in out and "UUID" in out["error"]
