"""Atomic attempt limits shared by a run's entire delegated tree."""

from __future__ import annotations

from contextlib import ExitStack
from threading import Lock


class SpawnAllowance:
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.lock = Lock()


def extend_limits(inherited: tuple[SpawnAllowance, ...], limit: int) -> tuple[SpawnAllowance, ...]:
    """Zero adds no limit; it never removes an ancestor's limit."""
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError("max_spawn_total must be an integer between 0 and 100")
    return (*inherited, SpawnAllowance(limit)) if limit else inherited


def try_claim(limits: tuple[SpawnAllowance, ...]) -> bool:
    """Claim one attempt from every ancestor atomically, without a refund."""
    unique = {id(limit): limit for limit in limits}
    ordered = [unique[key] for key in sorted(unique)]
    with ExitStack() as locks:
        for limit in ordered:
            locks.enter_context(limit.lock)
        if any(limit.used >= limit.limit for limit in ordered):
            return False
        for limit in ordered:
            limit.used += 1
        return True
