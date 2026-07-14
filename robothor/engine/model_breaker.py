"""Per-model circuit breaker for the LLM fallback chain.

``broken_models`` in ``llm_client`` is per-run: it resets every time. So a model
that is genuinely dead — a revoked key, a banned account — is retried on *every*
run, burning the full per-call timeout before falling back, indefinitely.

That is exactly what happened: ``codex/*`` auth died on 2026-06-01 and the fleet
kept dialling it for a month, paying the timeout tax on every run and silently
spending on the fallback provider, because nothing noticed the primary was dead.

This breaker fixes both halves of that failure:

* **Stop paying the tax** — after ``threshold`` consecutive failures a model is
  skipped until ``cooldown_seconds`` elapse, then probed again (half-open).
* **Stop the silence** — the operator is told the *first* time a model trips,
  and not again while it stays open. A dead primary can no longer go unnoticed.

State is in-process. That is deliberate: the breaker protects a single engine's
call path, and a restart should re-probe rather than inherit a stale verdict.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# A model has to fail this many times in a row before we stop dialling it. Low
# enough to stop the bleeding fast, high enough to ride out a blip.
DEFAULT_THRESHOLD = int(os.environ.get("ROBOTHOR_MODEL_BREAKER_THRESHOLD", "3"))
# How long to leave it open before probing again.
DEFAULT_COOLDOWN = int(os.environ.get("ROBOTHOR_MODEL_BREAKER_COOLDOWN", "600"))


@dataclass
class _State:
    consecutive_failures: int = 0
    open_until: float = 0.0
    alerted: bool = False


@dataclass
class ModelBreaker:
    """Tracks consecutive failures per model and opens the circuit on a streak."""

    threshold: int = DEFAULT_THRESHOLD
    cooldown_seconds: int = DEFAULT_COOLDOWN
    now: Callable[[], float] = time.monotonic
    # Called once when a model's circuit opens. Wired to the operator alert.
    on_open: Callable[[str, str], None] | None = None
    _models: dict[str, _State] = field(default_factory=dict)

    def _state(self, model: str) -> _State:
        return self._models.setdefault(model, _State())

    def is_open(self, model: str) -> bool:
        """True while this model should be skipped entirely."""
        st = self._models.get(model)
        if st is None or st.open_until == 0.0:
            return False
        if self.now() >= st.open_until:
            # Cooldown elapsed — half-open: let the next call probe it.
            st.open_until = 0.0
            st.consecutive_failures = 0
            st.alerted = False
            logger.info("model breaker half-open, will re-probe: %s", model)
            return False
        return True

    def record_failure(self, model: str, reason: str = "") -> None:
        st = self._state(model)
        st.consecutive_failures += 1
        if st.consecutive_failures < self.threshold or st.open_until:
            return

        st.open_until = self.now() + self.cooldown_seconds
        logger.error(
            "model breaker OPEN for %s after %d consecutive failures (%ds cooldown): %s",
            model,
            st.consecutive_failures,
            self.cooldown_seconds,
            reason,
        )
        if not st.alerted and self.on_open is not None:
            st.alerted = True
            try:
                self.on_open(model, reason)
            except Exception as exc:  # noqa: BLE001 — an alert must never break a run
                logger.error("model breaker could not alert the operator: %s", exc)

    def record_success(self, model: str) -> None:
        st = self._models.get(model)
        if st is None:
            return
        st.consecutive_failures = 0
        st.open_until = 0.0
        st.alerted = False


def _alert_operator(model: str, reason: str) -> None:
    from robothor.engine.feature_flags import notify_guardrail_alert

    notify_guardrail_alert(
        guardrail_name="model_breaker",
        agent_id="engine",
        reason=(
            f"{model} failed {DEFAULT_THRESHOLD}x in a row and is now being "
            f"skipped for {DEFAULT_COOLDOWN}s. Last error: {reason}. "
            f"If this is the fleet primary, check the provider key/account — a "
            f"dead primary previously went unnoticed for a month."
        ),
    )


_BREAKER = ModelBreaker(on_open=_alert_operator)


def get_model_breaker() -> ModelBreaker:
    """The engine-wide breaker used by the LLM fallback chain."""
    return _BREAKER
