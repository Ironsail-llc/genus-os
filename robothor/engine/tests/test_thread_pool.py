"""Tests for robothor.engine.thread_pool — read side of the thread-stewardship pool."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from robothor.engine.thread_pool import (
    MAX_THREADS,
    PENDING_EXPIRY_SECONDS,
    Thread,
    _thread_pool_context,
    auto_close_completed_threads,
    classify_stall,
    format_thread_pool,
    is_pending,
    list_threads,
    parse_accept_block,
    pending_marker,
    run_accept,
)
from robothor.engine.warmup import set_warmup_kind


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
            "body text",  # body (for pending-marker filter)
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
        # execute() should have been called with (tenant_id, 6) because
        # list_threads fetches 2x to leave headroom for pending-filter.
        cur = mock_get_conn.return_value.cursor.return_value
        args = cur.execute.call_args
        assert args[0][1][1] == 6

    @patch("robothor.db.connection.get_connection")
    def test_default_limit_is_max_threads(self, mock_get_conn):
        mock_get_conn.return_value = _mock_conn_with_rows([])
        list_threads()
        cur = mock_get_conn.return_value.cursor.return_value
        args = cur.execute.call_args
        assert args[0][1][1] == MAX_THREADS * 2

    @patch("robothor.db.connection.get_connection")
    def test_include_pending_skips_fetch_inflation(self, mock_get_conn):
        mock_get_conn.return_value = _mock_conn_with_rows([])
        list_threads(limit=5, include_pending=True)
        cur = mock_get_conn.return_value.cursor.return_value
        args = cur.execute.call_args
        # When pending is included, no need to over-fetch.
        assert args[0][1][1] == 5

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
            None,
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

    def test_operator_marker_appears_on_review_plus_requires_human(self):
        t = _make_thread(status="REVIEW", requires_human=True)
        assert "🧑OPERATOR" in format_thread_pool([t])

    def test_no_operator_marker_when_review_without_requires_human(self):
        t = _make_thread(status="REVIEW", requires_human=False)
        assert "🧑OPERATOR" not in format_thread_pool([t])

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
        with set_warmup_kind("cron"):
            assert _thread_pool_context(config) is None

    @patch("robothor.engine.thread_pool.list_threads")
    def test_returns_formatted_pool_for_main(self, mock_list):
        mock_list.return_value = [
            _make_thread(id_="abcd1234-aaaa-bbbb-cafe-eeeeeeeeeeee", title="Test")
        ]
        config = SimpleNamespace(id="main")
        with set_warmup_kind("cron"):
            out = _thread_pool_context(config)
        assert out is not None
        assert "THREAD POOL" in out
        assert "Test" in out

    @patch("robothor.engine.thread_pool.list_threads")
    def test_returns_empty_pool_message_when_no_threads(self, mock_list):
        mock_list.return_value = []
        config = SimpleNamespace(id="main")
        with set_warmup_kind("cron"):
            out = _thread_pool_context(config)
        assert out is not None
        assert "(empty)" in out

    @patch("robothor.engine.thread_pool.list_threads")
    def test_swallows_exceptions_silently(self, mock_list):
        mock_list.side_effect = RuntimeError("db down")
        config = SimpleNamespace(id="main")
        # Hook contract: never raise — warmup survives DB outages.
        with set_warmup_kind("cron"):
            assert _thread_pool_context(config) is None

    @patch("robothor.engine.thread_pool.list_threads")
    def test_returns_none_for_interactive_main_run(self, mock_list):
        """Thread pool must NOT inject into interactive (Telegram/webchat) runs.

        Regression: when main is triggered by a direct operator message, the
        model should focus on the user's ask, not be handed a thread-
        management directive. Only scheduled heartbeat (cron) runs get the
        thread pool context.
        """
        mock_list.return_value = [
            _make_thread(id_="abcd1234-aaaa-bbbb-cafe-eeeeeeeeeeee", title="Test")
        ]
        config = SimpleNamespace(id="main")
        with set_warmup_kind("interactive"):
            assert _thread_pool_context(config) is None
        # And with no kind set at all (defensive default)
        assert _thread_pool_context(config) is None

    @patch("robothor.engine.thread_pool.list_threads")
    def test_formatted_line_contains_full_uuid_not_short_id(self, mock_list):
        """Regression: the model calls get_task(id) off the bracketed id in the
        thread-pool line. If we emit the 8-char short_id, every such call
        fails and the agent burns tokens recovering via search_records."""
        full_id = "230d4039-6a32-4b8a-82ab-24d0132ecd9b"
        mock_list.return_value = [_make_thread(id_=full_id, title="Fix bug")]
        config = SimpleNamespace(id="main")
        with set_warmup_kind("cron"):
            out = _thread_pool_context(config)
        assert out is not None
        assert full_id in out, "Full 36-char UUID must be in thread-pool output"
        # And the 8-char prefix must not appear bracketed on its own.
        assert f"[{full_id[:8]}]" not in out


# ─── Priority ordering (integration — checks SQL construction) ─────────


class TestAcceptBlock:
    def test_no_block_returns_empty_list(self):
        assert parse_accept_block("no accept block here") == []
        assert parse_accept_block("") == []
        assert parse_accept_block(None) == []

    def test_single_command_block(self):
        body = "some text\n\n```accept\ntest -f /etc/hostname\n```\n"
        assert parse_accept_block(body) == ["test -f /etc/hostname"]

    def test_multiple_commands_parsed(self):
        body = "```accept\ntest -f /etc/hostname\necho hello\ntrue\n```\n"
        assert parse_accept_block(body) == [
            "test -f /etc/hostname",
            "echo hello",
            "true",
        ]

    def test_comments_stripped(self):
        body = "```accept\n# this is a comment, ignored\ntrue\n# another comment\nfalse\n```\n"
        assert parse_accept_block(body) == ["true", "false"]

    def test_blank_lines_stripped(self):
        body = "```accept\n\ntrue\n\n```\n"
        assert parse_accept_block(body) == ["true"]


class TestRunAccept:
    def test_empty_commands_returns_passed(self):
        result = run_accept([])
        assert result == {"passed": True, "failures": [], "ran": 0}

    def test_all_passing_commands(self):
        result = run_accept(["true", "echo hello"])
        assert result["passed"] is True
        assert result["failures"] == []
        assert result["ran"] == 2

    def test_failing_command_recorded(self):
        result = run_accept(["true", "false"])
        assert result["passed"] is False
        assert len(result["failures"]) == 1
        assert result["failures"][0]["command"] == "false"
        assert result["failures"][0]["exit_code"] == 1

    def test_timeout_recorded(self):
        # Tiny timeout on a sleep forces a timeout
        result = run_accept(["sleep 5"], timeout=1)
        assert result["passed"] is False
        assert "timeout" in result["failures"][0]["error"].lower()


class TestPendingMarker:
    def test_renders_marker_format(self):
        marker = pending_marker("abc-123", "2026-04-18T19:00:00+00:00")
        assert marker == "<!-- pending: run=abc-123 ts=2026-04-18T19:00:00+00:00 -->"

    def test_renders_with_default_timestamp(self):
        marker = pending_marker("abc-123")
        assert "pending: run=abc-123" in marker
        assert "ts=" in marker

    def test_is_pending_detects_fresh_marker(self):
        now = datetime.now(UTC)
        ts = now.isoformat(timespec="seconds")
        body = f"Task body here.\n{pending_marker('run-1', ts)}"
        assert is_pending(body) is True

    def test_is_pending_ignores_expired_marker(self):
        expired = datetime.now(UTC) - timedelta(seconds=PENDING_EXPIRY_SECONDS + 60)
        body = f"body\n{pending_marker('run-1', expired.isoformat())}"
        assert is_pending(body) is False

    def test_is_pending_false_for_no_marker(self):
        assert is_pending("regular task body with no marker") is False
        assert is_pending(None) is False

    def test_is_pending_handles_malformed_ts(self):
        body = "<!-- pending: run=abc ts=not-a-date -->"
        assert is_pending(body) is False


class TestListThreadsFiltersPending:
    @patch("robothor.db.connection.get_connection")
    def test_pending_threads_filtered_from_pool(self, mock_get_conn):
        fresh_marker = pending_marker("run-1")
        pending_row = (
            "11111111-aaaa-bbbb-cafe-000000000000",
            "Pending",
            "IN_PROGRESS",
            "normal",
            False,
            0,
            "main",
            1,
            0,
            False,
            0,
            0,
            f"Body with {fresh_marker}",
        )
        free_row = (
            "22222222-aaaa-bbbb-cafe-000000000000",
            "Free",
            "TODO",
            "normal",
            False,
            0,
            "main",
            1,
            0,
            False,
            0,
            0,
            "Body with no marker",
        )
        mock_get_conn.return_value = _mock_conn_with_rows([pending_row, free_row])
        threads = list_threads()
        assert [t.short_id for t in threads] == ["22222222"]

    @patch("robothor.db.connection.get_connection")
    def test_include_pending_flag_returns_all(self, mock_get_conn):
        fresh_marker = pending_marker("run-1")
        pending_row = (
            "11111111-aaaa-bbbb-cafe-000000000000",
            "Pending",
            "IN_PROGRESS",
            "normal",
            False,
            0,
            "main",
            1,
            0,
            False,
            0,
            0,
            f"Body with {fresh_marker}",
        )
        mock_get_conn.return_value = _mock_conn_with_rows([pending_row])
        threads = list_threads(include_pending=True)
        assert len(threads) == 1
        assert threads[0].short_id == "11111111"


class TestClassifyStall:
    def test_fresh_recent_updates(self):
        t = _make_thread(stale_days=0, escalation_count=0)
        assert classify_stall(t) == "fresh"

    def test_stall1_at_one_day(self):
        t = _make_thread(stale_days=1, escalation_count=0)
        assert classify_stall(t) == "stall1"

    def test_stall2_by_days(self):
        t = _make_thread(stale_days=2, escalation_count=0)
        assert classify_stall(t) == "stall2"

    def test_stall2_by_escalation_count(self):
        t = _make_thread(stale_days=0, escalation_count=1)
        assert classify_stall(t) == "stall2"

    def test_stall3_requires_review_plus_human_plus_3d(self):
        t = _make_thread(stale_days=3, status="REVIEW", requires_human=True)
        assert classify_stall(t) == "stall3"

    def test_stall3_requires_human_field_to_trigger(self):
        # 3 days stale but no requires_human → stall2, not stall3
        t = _make_thread(stale_days=3, status="REVIEW", requires_human=False)
        assert classify_stall(t) == "stall2"

    def test_stall3_requires_review_status(self):
        # 3 days stale + requires_human but not in REVIEW → stall2
        t = _make_thread(stale_days=3, status="IN_PROGRESS", requires_human=True)
        assert classify_stall(t) == "stall2"


class TestStallMarkerInFormat:
    def test_fresh_threads_have_no_stall_marker(self):
        t = _make_thread(stale_days=0, escalation_count=0)
        out = format_thread_pool([t])
        assert "[stall" not in out

    def test_stall1_marker_renders(self):
        t = _make_thread(stale_days=1)
        assert "[stall1]" in format_thread_pool([t])

    def test_stall2_marker_renders(self):
        t = _make_thread(stale_days=2)
        assert "[stall2]" in format_thread_pool([t])

    def test_stall3_marker_renders(self):
        t = _make_thread(stale_days=3, status="REVIEW", requires_human=True)
        assert "[stall3]" in format_thread_pool([t])


class TestAutoCloseCompletedThreads:
    @patch("robothor.db.connection.get_connection")
    def test_returns_empty_when_no_candidates(self, mock_get_conn):
        mock_get_conn.return_value = _mock_conn_with_rows([])
        assert auto_close_completed_threads() == []

    @patch("robothor.db.connection.get_connection")
    def test_flips_candidate_to_review(self, mock_get_conn):
        # Mock: one candidate returned by the SELECT, one row updated by the UPDATE.
        cursor = MagicMock()
        cursor.fetchall.return_value = [("abcd1234-aaaa-bbbb-cafe-000000000000",)]
        cursor.rowcount = 1
        conn = MagicMock()
        conn.cursor.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = conn

        result = auto_close_completed_threads()
        assert result == ["abcd1234-aaaa-bbbb-cafe-000000000000"]
        # statement_timeout + SELECT + UPDATE
        assert cursor.execute.call_count == 3
        assert conn.commit.called

    @patch("robothor.db.connection.get_connection")
    def test_skips_when_update_finds_no_rows(self, mock_get_conn):
        cursor = MagicMock()
        cursor.fetchall.return_value = [("ghost-id",)]
        cursor.rowcount = 0  # UPDATE didn't match (race condition)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = conn

        assert auto_close_completed_threads() == []


class TestHookRunsAutoSweep:
    @patch("robothor.engine.thread_pool.list_threads")
    @patch("robothor.engine.thread_pool.auto_close_completed_threads")
    def test_hook_calls_sweep_before_list(self, mock_sweep, mock_list):
        mock_sweep.return_value = ["abc"]
        mock_list.return_value = []
        config = SimpleNamespace(id="main")
        with set_warmup_kind("cron"):
            out = _thread_pool_context(config)
        assert mock_sweep.called
        assert "auto-sweep: flipped 1 parent thread" in out

    @patch("robothor.engine.thread_pool.list_threads")
    @patch("robothor.engine.thread_pool.auto_close_completed_threads")
    def test_hook_swallows_sweep_errors(self, mock_sweep, mock_list):
        mock_sweep.side_effect = RuntimeError("db error")
        mock_list.return_value = []
        config = SimpleNamespace(id="main")
        # Sweep failure must not block the pool view.
        with set_warmup_kind("cron"):
            out = _thread_pool_context(config)
        assert out is not None
        assert "auto-sweep" not in out


class TestPriorityOrdering:
    """Validates that the SQL used by list_threads sorts correctly.

    Tests the LIST SQL's ORDER BY semantics by constructing rows in
    reverse-priority order and asserting that the dataclass list preserves
    whatever order the DB returned.
    """

    @patch("robothor.db.connection.get_connection")
    def test_rows_are_returned_in_query_order(self, mock_get_conn):
        # Mock the DB returning rows in a deliberate order — parser must preserve.
        operator_blocked = (
            "11111111-aaaa-bbbb-cccc-dddddddddddd",
            "Operator-blocked",
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
            None,  # body
        )
        sla_breached = (
            "22222222-aaaa-bbbb-cafe-000000000000",
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
            None,
        )
        old_stale = (
            "33333333-aaaa-bbbb-cafe-000000000000",
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
            None,
        )
        # Simulating the ORDER BY from thread_pool._LIST_SQL returning these
        # in this sequence: operator-blocked → SLA-breached → stale.
        mock_get_conn.return_value = _mock_conn_with_rows(
            [operator_blocked, sla_breached, old_stale]
        )
        threads = list_threads()
        assert [t.short_id for t in threads] == ["11111111", "22222222", "33333333"]
        assert threads[0].requires_human is True
        assert threads[0].status == "REVIEW"
        assert threads[1].sla_breached is True
        assert threads[2].stale_days == 25
