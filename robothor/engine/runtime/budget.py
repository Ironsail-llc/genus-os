"""Shared reservations. Unknown provider usage stays charged until reconciliation."""

from __future__ import annotations

import threading


class SharedBudget:
    def __init__(self, limit: int):
        if type(limit) is not int or limit < 0:
            raise ValueError("nonnegative integer limit required")
        self.limit = limit
        self._charges: dict[str, int] = {}
        self._settled: dict[str, int] = {}
        self._lock = threading.Lock()
        self._overrun = False

    @property
    def charged(self) -> int:
        with self._lock:
            return sum(self._charges.values())

    def reserve(self, call_id: str, maximum: int) -> None:
        if not call_id or type(maximum) is not int or maximum < 0:
            raise ValueError("call identity and nonnegative bound required")
        with self._lock:
            if call_id in self._charges:
                raise ValueError("provider call already reserved; do not redispatch")
            if sum(self._charges.values()) + maximum > self.limit:
                raise ValueError("shared budget exhausted")
            self._charges[call_id] = maximum

    def settle(self, call_id: str, actual: int | None) -> None:
        with self._lock:
            reserved = self._charges[call_id]
            if actual is None:
                return
            if type(actual) is not int or actual < 0:
                raise ValueError("invalid usage")
            if call_id in self._settled:
                if self._settled[call_id] != actual:
                    raise ValueError("conflicting usage event")
                return
            self._settled[call_id] = actual
            self._charges[call_id] = actual
            if actual > reserved:
                # A single call reporting more than its ESTIMATED bound is
                # tokenizer variance, not a provider contract violation: the
                # bound is context_fit.estimate_for(), a heuristic, and a real
                # provider tokenizer can legitimately exceed it. Record the
                # overrun, but do NOT poison the budget and do NOT raise --
                # _charges[call_id] already holds the true usage, so the
                # cumulative check in reserve() remains a correct hard stop.
                # Latching here (and raising) blocked goals at 1-21% of budget
                # spent with the misleading "shared budget exhausted" message;
                # see task f227bd47.
                self._overrun = True
