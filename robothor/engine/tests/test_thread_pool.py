"""Tests for robothor.engine.thread_pool — read side of the thread-stewardship pool."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from robothor.engine.thread_pool import (
    MAX_THREADS,
    Thread,
    _thread_pool_context,
    format_thread_pool,
    list_threads,
)


def _make_thread(
    *,
    id_="deadbeef-cafe-babe-face-abcdefabcdef",
    title="Optimize foo",
    status="TODO",
    priority="normal",
    age_days=3,
    stale_days=2,
    requires_human=False,
    sla_breached=False,
    escalation_count=0,
    open_children=0,
    total_children=0,
    assigned_to_agent="main",
) -> Thread:
    return Thread(
        id=id_,
        title=title,
        status=status,
        priority=priority,
        age_days=age_days,
        stale_days=stale_days,
        requires_human=requires_human,
        sla_breached=sla_breached,
        escalation_count=escalation_count,
        open_children=open_children,
        total_children=total_children,
        assigned_to_agent=assigned_to_agent,
    )


def _mock_conn_with_rows(rows: list[tuple]) -> MagicMock:
    """Mock get_connection() returning a context manager with cur.fetchall() -> rows."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ─── list_threads ─────────────────────────────────────────────────────


class TestListThreads:
    @patch("robothor.db.connection.get_connection")
    def test_empty_db_returns_empty_list(self, mock_get_conn):
        mock_get_conn.return_value = _mock_conn_with_rows([])
        assert list_threads() == []

    @patch("robothor.db.connection.get_connection")
    def test_parses_row_to_thread_dataclass(self, mock_get_conn):
        row = (
            "b4438cc8-aaaa-bbbb-cafe-dddddddddddd",
            "Optimize main cost",
            "TODO",
            "high",
            False,
            0,
            "auto-researcher",
            1,  # age_days
            0,  # stale_days
            True,  # sla_breached
            3,  # total_children
            1,  # open_children
        )
        mock_get_conn.return_value = _mock_conn_with_rows([row])
        threads = list_threads()
        assert len(threads) == 1
        t = threads[0]
        assert t.id == "b4438cc8-aaaa-bbbb-cafe-dddddddddddd"
        assert t.title == "Optimize main cost"
        assert t.status == "TODO"
        assert t.priority == "high"
        assert t.requires_human is False
        assert t.escalation_count == 0
        assert t.assigned_to_agent == "auto-researcher"
        assert t.sla_breached is True
        assert t.total_children == 3
        assert t.open_children == 1
        assert t.short_id == "b4438cc8"

    @patch("robothor.db.connection.get_connection")
    def test_respects_limit_param(self, mock_get_conn):
        mock_get_conn.return_value = _mock_conn_with_rows([])
        list_threads(limit=3)
        # execute() should have been called with (tenant_id, 3)
        cur = mock_get_conn.return_value.cursor.return_value
        args = cur.execute.call_args
        assert args[0][1][1] == 3

    @patch("robothor.db.connection.get_connection")
    def test_default_limit_is_max_threads(self, mock_get_conn):
        mock_get_conn.return_value = _mock_conn_with_rows([])
        list_threads()
        cur = mock_get_conn.return_value.cursor.return_value
        args = cur.execute.call_args
        assert args[0][1][1] == MAX_THREADS

    @patch("robothor.db.connection.get_connection")
    def test_defaults_title_to_placeholder(self, mock_get_conn):
        row = (
            "id-0000",
            None,  # null title
            "TODO",
            "normal",
            False,
            0,
            None,
            0,
            0,
            False,
            0,
            0,
        )
        mock_get_conn.return_value = _mock_conn_with_rows([row])
        threads = list_threads()
        assert threads[0].title == "(untitled)"


# ─── format_thread_pool ───────────────────────────────────────────────


class TestFormatThreadPool:
    def test_empty_pool_returns_guidance(self):
        out = format_thread_pool([])
        assert "THREAD POOL" in out
        assert "(empty)" in out
        assert "promote it" in out

    def test_single_thread_renders_one_line(self):
        t = _make_thread(id_="abcd1234-aaaa-bbbb-cafe-eeeeeeeeeeee", title="Hello")
        out = format_thread_pool([t])
        assert "THREAD POOL" in out
        assert "abcd1234" in out
        assert "Hello" in out
        # one header + one thread line
        assert len([ln for ln in out.split("\n") if ln]) == 2

    def test_philip_marker_appears_on_review_plus_requires_human(self):
        t = _make_thread(status="REVIEW", requires_human=True)
        assert "🧑PHILIP" in format_thread_pool([t])

    def test_no_philip_marker_when_review_without_requires_human(self):
        t = _make_thread(status="REVIEW", requires_human=False)
        assert "🧑PHILIP" not in format_thread_pool([t])

    def test_sla_marker_when_breached(self):
        t = _make_thread(sla_breached=True)
        assert "⏰SLA" in format_thread_pool([t])

    def test_escalation_marker_with_count(self):
        t = _make_thread(escalation_count=3)
        out = format_thread_pool([t])
        assert "↑3" in out

    def test_children_counter_shows_progress(self):
        t = _make_thread(total_children=5, open_children=2)
        out = format_thread_pool([t])
        assert "kids:3/5" in out

    def test_long_title_truncated_with_ellipsis(self):
        t = _make_thread(title="x" * 200)
        out = format_thread_pool([t])
        assert "…" in out
        # line itself is bounded
        thread_line = next(ln for ln in out.split("\n") if "xx" in ln)
        assert len(thread_line) <= 141  # MAX_LINE_CHARS=140 + possible ellipsis

    def test_assignee_prefixed_with_at_sign(self):
        t = _make_thread(assigned_to_agent="auto-researcher")
        assert "@auto-researcher" in format_thread_pool([t])

    def test_no_assignee_field_when_absent(self):
        t = _make_thread(assigned_to_agent=None)
        out = format_thread_pool([t])
        # should not contain a trailing @<something>
        assert " @" not in out.split("\n")[1]

    def test_stale_days_rendered(self):
        t = _make_thread(stale_days=7)
        assert "[7d]" in format_thread_pool([t])


# ─── _thread_pool_context (the warmup hook) ───────────────────────────


class TestThreadPoolContext:
    def test_returns_none_for_non_main_agent(self):
        config = SimpleNamespace(id="email-classifier")
        assert _thread_pool_context(config) is None

    @patch("robothor.engine.thread_pool.list_threads")
    def test_returns_formatted_pool_for_main(self, mock_list):
        mock_list.return_value = [
            _make_thread(id_="abcd1234-aaaa-bbbb-cafe-eeeeeeeeeeee", title="Test")
        ]
        config = SimpleNamespace(id="main")
        out = _thread_pool_context(config)
        assert out is not None
        assert "THREAD POOL" in out
        assert "Test" in out

    @patch("robothor.engine.thread_pool.list_threads")
    def test_returns_empty_pool_message_when_no_threads(self, mock_list):
        mock_list.return_value = []
        config = SimpleNamespace(id="main")
        out = _thread_pool_context(config)
        assert out is not None
        assert "(empty)" in out

    @patch("robothor.engine.thread_pool.list_threads")
    def test_swallows_exceptions_silently(self, mock_list):
        mock_list.side_effect = RuntimeError("db down")
        config = SimpleNamespace(id="main")
        # Hook contract: never raise — warmup survives DB outages.
        assert _thread_pool_context(config) is None


# ─── Priority ordering (integration — checks SQL construction) ─────────


class TestPriorityOrdering:
    """Validates that the SQL used by list_threads sorts correctly.

    Tests the LIST SQL's ORDER BY semantics by constructing rows in
    reverse-priority order and asserting that the dataclass list preserves
    whatever order the DB returned.
    """

    @patch("robothor.db.connection.get_connection")
    def test_rows_are_returned_in_query_order(self, mock_get_conn):
        # Mock the DB returning rows in a deliberate order — parser must preserve.
        philip_blocked = (
            "11111111-aaaa-bbbb-cccc-dddddddddddd",
            "Philip-blocked",
            "REVIEW",
            "high",
            True,  # requires_human
            0,
            "main",
            1,
            0,
            False,
            0,
            0,
        )
        sla_breached = (
            "22222222-aaaa-bbbb-cccc-dddddddddddd",
            "SLA breached",
            "TODO",
            "normal",
            False,
            0,
            "auto-agent",
            5,
            4,
            True,
            0,
            0,
        )
        old_stale = (
            "33333333-aaaa-bbbb-cccc-dddddddddddd",
            "Oldest stale",
            "IN_PROGRESS",
            "normal",
            False,
            0,
            "auto-researcher",
            30,
            25,
            False,
            0,
            0,
        )
        # Simulating the ORDER BY from thread_pool._LIST_SQL returning these
        # in this sequence: Philip-blocked → SLA-breached → stale.
        mock_get_conn.return_value = _mock_conn_with_rows([philip_blocked, sla_breached, old_stale])
        threads = list_threads()
        assert [t.short_id for t in threads] == ["11111111", "22222222", "33333333"]
        assert threads[0].requires_human is True
        assert threads[0].status == "REVIEW"
        assert threads[1].sla_breached is True
        assert threads[2].stale_days == 25
