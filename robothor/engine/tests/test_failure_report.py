"""``genus engine failures``: what failed, grouped by tool, agent and error.

Three tables hold the failures today and nothing reads them together:
``agent_tool_events`` (a tool's own failure), ``audit_log`` (a call denied by
RBAC or an allowlist) and ``workflow_run_steps`` (a failed workflow step). The
report reads all three for one tenant and one window, normalizes each error so
the same failure with a different id groups as one, and redacts it.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from robothor.engine import failure_report
from robothor.engine.failure_report import normalize_error

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class TestNormalizeError:
    def test_ids_and_numbers_collapse(self) -> None:
        a = normalize_error("record 4f1c2a9e-1111-2222-3333-444455556666 not found (status 404)")
        b = normalize_error("record 0d3b5c7e-aaaa-bbbb-cccc-ddddeeeeffff not found (status 500)")
        assert a == b

    def test_long_hex_and_digit_runs_collapse(self) -> None:
        assert normalize_error("job 12345 failed at 0xdeadbeefcafe") == normalize_error(
            "job 99 failed at 0x0123456789ab"
        )

    def test_different_errors_stay_apart(self) -> None:
        assert normalize_error("connection refused") != normalize_error("permission denied")

    def test_it_is_redacted_and_short(self) -> None:
        out = normalize_error("password=hunter2hunter2hunter2 " + "q " * 10_000)
        assert "hunter2hunter2hunter2" not in out
        assert len(out) <= 200

    def test_empty_is_named(self) -> None:
        assert normalize_error(None) == "(no message)"


def _rows(monkeypatch, *, tools=(), denied=(), steps=()) -> None:
    monkeypatch.setattr(failure_report, "_tool_event_rows", lambda h, t: list(tools))
    monkeypatch.setattr(failure_report, "_denied_rows", lambda h, t: list(denied))
    monkeypatch.setattr(failure_report, "_workflow_step_rows", lambda h, t: list(steps))


class TestCollect:
    def test_the_same_failure_with_different_ids_groups_as_one(self, monkeypatch) -> None:
        _rows(
            monkeypatch,
            tools=[
                ("example_tool", "example-agent", "record 111 not found", 2, T0),
                ("example_tool", "example-agent", "record 222 not found", 3, T0),
            ],
        )
        groups = failure_report.collect(hours=24, tenant_id="test-tenant")
        assert len(groups) == 1
        assert groups[0]["count"] == 5
        assert groups[0]["source"] == "tool"

    def test_all_three_sources_are_reported(self, monkeypatch) -> None:
        _rows(
            monkeypatch,
            tools=[("example_tool", "example-agent", "boom", 1, T0)],
            denied=[("example_tool", "example-agent", "role 'viewer' may not use it", 4, T0)],
            steps=[("fetch", "example-flow", "the remote service answered 503", 2, T0)],
        )
        groups = failure_report.collect(hours=24, tenant_id="test-tenant")
        assert {g["source"] for g in groups} == {"tool", "denied", "workflow_step"}
        wf = next(g for g in groups if g["source"] == "workflow_step")
        assert wf["workflow"] == "example-flow"
        assert wf["agent"] == ""
        assert [g["count"] for g in groups] == sorted((g["count"] for g in groups), reverse=True)

    def test_the_window_and_tenant_reach_every_query(self, monkeypatch) -> None:
        seen: list[tuple[str, tuple[Any, ...]]] = []

        class _Cursor:
            def execute(self, sql: str, params: tuple[Any, ...]) -> None:
                seen.append((sql, params))

            def fetchall(self) -> list[Any]:
                return []

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a: Any) -> None:
                return None

            def cursor(self, *a: Any, **k: Any) -> _Cursor:
                return _Cursor()

        monkeypatch.setattr("robothor.db.connection.get_connection", lambda *a, **k: _Conn())
        failure_report.collect(hours=6, tenant_id="test-tenant")
        assert len(seen) == 3
        for sql, params in seen:
            assert "tenant_id" in sql
            assert "test-tenant" in params
            assert 6 in params


class TestCommand:
    def _run(self, monkeypatch, capsys, *, as_json: bool, hours: int = 24) -> str:
        from robothor.cli.engine import _cmd_engine_failures

        _rows(
            monkeypatch,
            tools=[("example_tool", "example-agent", "boom 42", 3, T0)],
            steps=[("fetch", "example-flow", "answered 503", 1, T0)],
        )
        monkeypatch.setattr(
            "robothor.engine.tracking.get_tool_stats",
            lambda hours, tenant_id=None: [
                {"tool_name": "example_tool", "total_calls": 30, "failures": 3}
            ],
        )
        args = argparse.Namespace(hours=hours, json=as_json, tenant=None, limit=50)
        assert _cmd_engine_failures(args) == 0
        return capsys.readouterr().out

    def test_json_lists_the_groups(self, monkeypatch, capsys) -> None:
        out = json.loads(self._run(monkeypatch, capsys, as_json=True))
        assert out["hours"] == 24
        assert out["total"] == 4
        assert {g["tool"] for g in out["groups"]} == {"example_tool", "fetch"}
        assert out["tool_calls"] == {"example_tool": {"calls": 30, "failures": 3}}

    def test_text_names_tool_agent_and_error(self, monkeypatch, capsys) -> None:
        out = self._run(monkeypatch, capsys, as_json=False)
        assert "example_tool" in out
        assert "example-agent" in out
        assert "example-flow" in out
        assert "boom <n>" in out

    def test_the_parser_has_the_verb(self) -> None:
        from robothor.cli import _build_parser as build_parser

        args = build_parser().parse_args(["engine", "failures", "--hours", "6", "--json"])
        assert args.hours == 6
        assert args.json is True

    @pytest.mark.parametrize("hours", [0, -1])
    def test_a_window_must_be_positive(self, monkeypatch, capsys, hours) -> None:
        from robothor.cli.engine import _cmd_engine_failures

        args = argparse.Namespace(hours=hours, json=False, tenant=None, limit=50)
        assert _cmd_engine_failures(args) == 2


def test_a_denial_recorded_in_both_tables_is_one_group(monkeypatch) -> None:
    """A dispatch denial writes an audit row and a failed tool event: count it once."""
    denial = ("example_tool", "example-agent", "role 'viewer' may not use example_tool", 3, T0)
    _rows(monkeypatch, tools=[denial], denied=[denial])
    groups = failure_report.collect(hours=24, tenant_id="test-tenant")
    assert len(groups) == 1
    assert groups[0]["source"] == "denied"
    assert groups[0]["count"] == 3
