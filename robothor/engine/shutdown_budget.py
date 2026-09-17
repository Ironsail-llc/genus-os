"""The engine daemon's SIGTERM-to-exit budget, in one place.

``robothor-engine.service`` stops the daemon with SIGTERM and gives it
``TimeoutStopSec`` (15 s) to exit. Past that systemd SIGKILLs the cgroup and
files ``Failed with result 'timeout'``, which ``OnFailure=`` pages — the same
operator-visible symptom as a crash, for a stop the operator asked for.

The stop path in ``daemon.main`` awaits, in series: the shutdown announcement
to the operator's chat, the task-registry drain, and the Telegram dispatcher
stop, then a tail of steps that are not individually bounded (NATS, scheduler,
hooks, Slack, the leader elector, cancelling what is left). The three awaited
bounds are slices of ONE budget below, and the margin is what the tail gets.
``robothor/engine/tests/test_daemon_shutdown_budget.py`` reads the unit file
and fails if ``STOP_BUDGET_SECONDS + STOP_MARGIN_SECONDS`` stops fitting
inside ``TimeoutStopSec``.

Only this module carries numbers; ``daemon.py`` and ``telegram.py`` import
theirs from here so there is no second place for the sum to drift.
"""

from __future__ import annotations

#: Everything the daemon awaits with a timeout on its way out, added up.
#: Deliberately under robothor-engine.service's TimeoutStopSec=15.
STOP_BUDGET_SECONDS = 12.0

#: What the unbounded tail (NATS disconnect, scheduler, hooks, Slack, elector,
#: cancelling the remaining tasks) is left with before systemd's SIGKILL. An
#: observed real stop spends ~1.5 s in total, so this is headroom, not a plan.
STOP_MARGIN_SECONDS = 3.0

#: The "Engine Shutting Down" Telegram message. Best-effort: a chat that will
#: not answer in this long is not worth a page.
ANNOUNCE_TIMEOUT_SECONDS = 2.0

#: aiogram's ``stop_polling``; it cancels the in-flight long poll rather than
#: waiting it out, so this is rarely reached.
POLLING_STOP_TIMEOUT_SECONDS = 3.0

#: Tracked background tasks (agent runs in flight). Whatever the other two
#: slices leave — the drain is the step worth the most time.
DRAIN_TIMEOUT_SECONDS = (
    STOP_BUDGET_SECONDS - ANNOUNCE_TIMEOUT_SECONDS - POLLING_STOP_TIMEOUT_SECONDS
)
