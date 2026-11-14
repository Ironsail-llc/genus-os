"""deferred_runs.recent: hook workflow runs the tool-call rate limit deferred.

The engine counts each deferral in Redis per workflow and per minute window
(``robothor.engine.deferrals``). This check sums the last hour, warns past a
threshold naming the workflows that deferred most, and alerts in two tiers:
past the warn threshold a ``warning`` alert (an alert-digest row), past the
higher page threshold a ``critical`` alert (paged). Each tier alerts at most
once per look-back period, measured from when it last alerted. It only reads
the counts, so running it again reports the same number.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from robothor.doctor.checks import deferred_runs
from robothor.doctor.tests.conftest import make_ctx
from robothor.engine.deferrals import WINDOW_SECONDS, window_key

#: The tenant a doctor with no tenant configured runs as.
TENANT = "default"


class FakeRedis:
    def __init__(self, hashes: dict[str, dict[str, int]] | None = None) -> None:
        self.hashes = hashes or {}
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.writes = 0
        self.closed = 0

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        return {f.encode(): str(v).encode() for f, v in self.hashes.get(key, {}).items()}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        self.writes += 1
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        self.ttls[key] = ex
        return True

    def delete(self, key: str) -> int:
        return 1 if self.strings.pop(key, None) is not None else 0

    def close(self) -> None:
        self.closed += 1


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
            window_key(TENANT, start): {"example-workflow": 90, "other-workflow": 5},
            window_key(TENANT, start - WINDOW_SECONDS): {
                "example-workflow": 40,
                "third-workflow": 12,
            },
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
    client = FakeRedis({window_key(TENANT, _this_window()): {"example-workflow": 100}})
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
    _with(monkeypatch, FakeRedis({window_key(TENANT, old): {"example-workflow": 5000}}))
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


def _as_tenant(monkeypatch, tenant: str) -> None:
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", tenant)


def test_a_doctor_reads_only_its_own_tenants_deferrals(monkeypatch):
    start = _this_window()
    shared = FakeRedis(
        {
            window_key("tenant-a", start): {"example-workflow": 3},
            window_key("tenant-b", start): {"example-workflow": 500},
        }
    )
    _with(monkeypatch, shared)
    _as_tenant(monkeypatch, "tenant-a")
    result = _run(make_ctx())
    assert result.status == "pass"
    assert "3 deferred" in result.detail

    _as_tenant(monkeypatch, "tenant-b")
    assert _run(make_ctx()).status == "fail"


def test_each_tenant_gets_its_own_hourly_alert(monkeypatch):
    start = _this_window()
    shared = FakeRedis(
        {
            window_key("tenant-a", start): {"example-workflow": 500},
            window_key("tenant-b", start): {"example-workflow": 500},
        }
    )
    sent = _with(monkeypatch, shared)
    _as_tenant(monkeypatch, "tenant-a")
    _run(make_ctx())
    _as_tenant(monkeypatch, "tenant-b")
    _run(make_ctx())
    assert sent.await_count == 2


def test_an_alert_that_was_not_delivered_is_tried_again(monkeypatch, busy):
    sent = _with(monkeypatch, busy)
    sent.return_value = False
    _run(make_ctx())
    sent.return_value = True
    _run(make_ctx())
    _run(make_ctx())
    assert sent.await_count == 2  # the failed send, its retry, then nothing


def test_an_alert_that_raised_is_tried_again(monkeypatch, busy):
    sent = _with(monkeypatch, busy)
    sent.side_effect = [RuntimeError("no channel"), True]
    _run(make_ctx())
    _run(make_ctx())
    assert sent.await_count == 2


def test_the_store_client_is_closed(monkeypatch, busy):
    _with(monkeypatch, busy)
    _run(make_ctx())
    assert busy.closed == 1


def _deferred(total: int, tenant: str = TENANT, start: int | None = None) -> FakeRedis:
    start = _this_window() if start is None else start
    return FakeRedis({window_key(tenant, start): {"example-workflow": total}})


def _levels(sent: AsyncMock) -> list[str]:
    return [call.args[0] for call in sent.await_args_list]


def test_past_the_page_threshold_the_alert_is_critical(monkeypatch):
    sent = _with(monkeypatch, _deferred(600))
    result = _run(make_ctx())
    assert result.status == "fail"
    assert "600" in result.detail
    assert _levels(sent) == ["critical"]
    body = sent.await_args.args[2]
    assert "600" in body and "example-workflow" in body


def test_between_the_two_thresholds_the_alert_is_a_warning(monkeypatch):
    sent = _with(monkeypatch, _deferred(500))
    assert _run(make_ctx()).status == "fail"
    assert _levels(sent) == ["warning"]


def test_the_critical_alert_goes_out_once_a_period(monkeypatch):
    sent = _with(monkeypatch, _deferred(600))
    _run(make_ctx())
    _run(make_ctx())
    assert _levels(sent) == ["critical"]


def test_the_page_threshold_is_a_setting(monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_DEFERRED_RUNS_PAGE_THRESHOLD", "1000")
    reset_settings()
    sent = _with(monkeypatch, _deferred(600))
    _run(make_ctx())
    assert _levels(sent) == ["warning"]


def test_the_page_threshold_is_never_below_the_warn_threshold(monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_DEFERRED_RUNS_WARN_THRESHOLD", "1000")
    monkeypatch.setenv("ROBOTHOR_DEFERRED_RUNS_PAGE_THRESHOLD", "200")
    reset_settings()
    sent = _with(monkeypatch, _deferred(600))
    assert _run(make_ctx()).status == "pass"
    sent.assert_not_awaited()


def test_a_warning_already_sent_does_not_hold_back_the_page(monkeypatch):
    client = _deferred(150)
    sent = _with(monkeypatch, client)
    _run(make_ctx())
    client.hashes[window_key(TENANT, _this_window())]["example-workflow"] = 600
    _run(make_ctx())
    _run(make_ctx())
    assert _levels(sent) == ["warning", "critical"]


def test_the_period_runs_from_the_alert_not_the_clock_hour(monkeypatch):
    """Two runs a few seconds apart across an hour boundary alert once, not twice."""
    boundary = (int(time.time()) // 3600 + 1) * 3600
    clock = {"now": float(boundary - 2)}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    client = _deferred(150, start=boundary - WINDOW_SECONDS)
    sent = _with(monkeypatch, client)
    _run(make_ctx())
    clock["now"] = float(boundary + 2)
    _run(make_ctx())
    assert sent.await_count == 1
    (ttl,) = client.ttls.values()
    assert ttl == deferred_runs.LOOKBACK_SECONDS
