"""A schedule that cannot fire must not report success.

`compute_next_run` returns None for a `once` schedule whose fire_at has
already passed. `create_user_cronjob` wrote the row anyway with
next_run_at=NULL, the poller (which selects on next_run_at <= now) never saw
it, and the caller got a job_id back as though it were scheduled.

Measured on the live instance:

    future one-shot : next_run=2026-08-27T00:17:19+00:00  -> SCHEDULED
    past one-shot   : next_run=None                       -> SILENTLY DROPPED
    ...but the caller got a job_id anyway: ucron-9089ae0530df

So "remind me at 3pm", asked at 3:01pm, returns a confirmation and does
nothing. Clock skew or a slow round-trip that pushes creation past the target
does the same. The agent tells the operator it is scheduled; nothing fires;
no error and no log line anywhere.

A one-shot whose moment has passed is almost always still wanted — the
request was late, not wrong — so it fires on the next tick rather than being
refused. What it must never do is silently vanish.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from robothor.engine.user_cron import compute_next_run


def test_a_future_one_shot_is_scheduled_for_its_moment():
    now = datetime.now(UTC)
    when = now + timedelta(minutes=5)
    assert compute_next_run({"kind": "once", "fire_at": when.isoformat()}, now) == when


def test_a_past_one_shot_is_not_dropped():
    """The defect: this returned None and the job never fired."""
    now = datetime.now(UTC)
    late = (now - timedelta(minutes=1)).isoformat()

    nxt = compute_next_run({"kind": "once", "fire_at": late}, now)

    assert nxt is not None, "a late one-shot was silently discarded"
    assert nxt >= now, "a late one-shot must be scheduled now, not in the past"
    assert nxt - now < timedelta(seconds=5), "it should fire on the next tick"


def test_a_one_shot_exactly_now_is_not_dropped():
    """The boundary the original `>` comparison fell through."""
    now = datetime.now(UTC)
    assert compute_next_run({"kind": "once", "fire_at": now.isoformat()}, now) is not None


def test_a_one_shot_with_no_fire_at_is_still_refused():
    """Malformed is different from late: there is no moment to schedule."""
    now = datetime.now(UTC)
    assert compute_next_run({"kind": "once"}, now) is None


def test_interval_and_cron_are_unchanged():
    now = datetime.now(UTC)
    every = compute_next_run({"kind": "interval", "every_seconds": 300}, now)
    assert every == now + timedelta(seconds=300)
    assert compute_next_run({"kind": "cron", "expression": "*/5 * * * *"}, now) is not None
