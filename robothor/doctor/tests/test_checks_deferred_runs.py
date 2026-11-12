"""deferred_runs.recent: hook workflow runs the tool-call rate limit deferred.

The engine counts each deferral in Redis per workflow and per minute window
(``robothor.engine.deferrals``). This check sums the last hour, warns past a
threshold naming the workflows that deferred most, and sends that warning to
the operator's alert channel once per look-back period. It only reads the
counts, so running it again reports the same number.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from robothor.doctor.checks import deferred_runs
from robothor.doctor.tests.conftest import make_ctx
from robothor.engine.deferrals import WINDOW_SECONDS, window_key


class FakeRedis:
    def __init__(self, hashes: dict[str, dict[str, int]] | None = None) -> None:
        self.hashes = hashes or {}
        self.strings: dict[str, str] = {}
        self.writes = 0

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        return {f.encode(): str(v).encode() for f, v in self.hashes.get(key, {}).items()}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        self.writes += 1
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    def close(self) -> None:
        pass


def _this_window() -> int:
    now = int(time.time())
    return now - now % WINDOW_SECONDS


def _with(monkeypatch, client: FakeRedis | Exception) -> AsyncMock:
    def factory(_ctx):
        if isinstance(client, Exception):
            raise client
        return client

    monkeypatch.setattr(deferred_runs, "_client", factory)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("robothor.engine.alerts.alert", sent)
    return sent


def _run(ctx):
    (check,) = deferred_runs.CHECKS
    answer = asyncio.run(check.run(ctx))
    return answer if not isinstance(answer, list) else answer[0]


@pytest.fixture
def busy() -> FakeRedis:
    start = _this_window()
    return FakeRedis(
        {
            window_key(start): {"example-workflow": 90, "other-workflow": 5},
            window_key(start - WINDOW_SECONDS): {"example-workflow": 40, "third-workflow": 12},
        }
    )


def test_the_check_is_registered():
    from robothor.doctor.registry import builtin_ids

    assert "deferred_runs.recent" in builtin_ids()
    (check,) = deferred_runs.CHECKS
    assert check.severity == "recommended"
    assert check.category == "deferred_runs"


def test_past_the_threshold_it_warns_with_the_count_and_top_workflows(monkeypatch, busy):
    _with(monkeypatch, busy)
    result = _run(make_ctx())
    assert result.status == "fail"
    assert "147" in result.detail
    # Top workflows by deferrals, most first.
    assert result.detail.index("example-workflow") < result.detail.index("third-workflow")
    assert result.detail.index("third-workflow") < result.detail.index("other-workflow")
    assert "130" in result.detail


def test_under_the_threshold_it_passes(monkeypatch):
    client = FakeRedis({window_key(_this_window()): {"example-workflow": 100}})
    sent = _with(monkeypatch, client)
    result = _run(make_ctx())
    assert result.status == "pass"
    assert "100" in result.detail
    sent.assert_not_awaited()


def test_nothing_deferred_passes(monkeypatch):
    _with(monkeypatch, FakeRedis())
    result = _run(make_ctx())
    assert result.status == "pass"
    assert "0" in result.detail


def test_windows_older_than_the_look_back_are_not_counted(monkeypatch):
    old = _this_window() - deferred_runs.LOOKBACK_SECONDS - WINDOW_SECONDS
    _with(monkeypatch, FakeRedis({window_key(old): {"example-workflow": 5000}}))
    assert _run(make_ctx()).status == "pass"


def test_the_threshold_is_a_setting(monkeypatch, busy):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_DEFERRED_RUNS_WARN_THRESHOLD", "500")
    reset_settings()
    _with(monkeypatch, busy)
    assert _run(make_ctx()).status == "pass"


def test_running_twice_reports_the_same_count(monkeypatch, busy):
    _with(monkeypatch, busy)
    first = _run(make_ctx())
    second = _run(make_ctx())
    assert first.detail == second.detail
    assert sum(sum(b.values()) for b in busy.hashes.values()) == 147


def test_the_warning_goes_to_the_alert_channel_once(monkeypatch, busy):
    sent = _with(monkeypatch, busy)
    _run(make_ctx())
    _run(make_ctx())
    sent.assert_awaited_once()
    level, title, body = sent.await_args.args[:3]
    assert level == "warning"
    assert "deferred" in title.lower()
    assert "147" in body and "example-workflow" in body


def test_a_dry_run_sends_no_alert(monkeypatch, busy):
    sent = _with(monkeypatch, busy)
    assert _run(make_ctx(dry_run=True)).status == "fail"
    sent.assert_not_awaited()


def test_an_alert_that_fails_still_reports_the_warning(monkeypatch, busy):
    sent = _with(monkeypatch, busy)
    sent.side_effect = RuntimeError("no channel")
    assert _run(make_ctx()).status == "fail"


def test_unreachable_redis_is_a_skip(monkeypatch):
    _with(monkeypatch, ConnectionError("refused"))
    result = _run(make_ctx())
    assert result.status == "skip"
    assert "ConnectionError" in result.detail
