"""Local acceleration of durable controls; authority remains in PostgreSQL."""

from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio


@dataclass
class Activity:
    task: asyncio.Task
    loop: asyncio.AbstractEventLoop
    sessions: dict[str, Any] = field(default_factory=dict)
    goal_id: str | None = None
    parent_goal_id: str | None = None


current: ContextVar[Activity | None] = ContextVar("runtime_activity", default=None)
_lock = threading.RLock()
_active: dict[tuple[str, str], Activity] = {}


def register(session: Any) -> None:
    activity = current.get()
    if activity is not None:
        with _lock:
            activity.sessions[session.run_id] = session
            _active[(session.run.tenant_id, session.run_id)] = activity


def remove(activity: Activity) -> None:
    with _lock:
        for key, value in list(_active.items()):
            if value is activity:
                _active.pop(key)


def stop_local(tenant: str, run_id: str, note: str) -> None:
    with _lock:
        activity = _active.get((tenant, run_id))
    if activity is not None:
        activity.sessions[run_id].interrupt(note)
        activity.loop.call_soon_threadsafe(activity.task.cancel, note)


def runs(tenant: str) -> list[str]:
    with _lock:
        return [run_id for t, run_id in _active if t == tenant]


def stop_goal(tenant: str, goal_id: str) -> None:
    with _lock:
        targets = [
            run
            for (t, run), activity in _active.items()
            if t == tenant and goal_id in {activity.goal_id, activity.parent_goal_id}
        ]
    for run in targets:
        stop_local(tenant, run, "Operator stopped goal execution")
