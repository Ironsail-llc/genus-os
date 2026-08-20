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

import json
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# A model has to fail this many times in a row before we stop dialling it. Low
# enough to stop the bleeding fast, high enough to ride out a blip.
DEFAULT_THRESHOLD = int(os.environ.get("ROBOTHOR_MODEL_BREAKER_THRESHOLD", "3"))
# How long to leave it open before probing again.
DEFAULT_COOLDOWN = int(os.environ.get("ROBOTHOR_MODEL_BREAKER_COOLDOWN", "600"))

# Re-alert floor per model. The in-process ``alerted`` flag re-arms on every
# cooldown cycle, which turned one flaky provider afternoon into an escalation
# every ~10-40 minutes (145 rows). The floor is persisted to a small state
# file so it also survives daemon restarts and covers sibling processes
# (CLI, workers) that share the file.
ALERT_DEDUP_SECONDS = int(os.environ.get("ROBOTHOR_MODEL_BREAKER_ALERT_DEDUP", str(6 * 3600)))

# The run whose LLM call tripped the breaker, so the trip can be recorded in
# agent_guardrail_events (run_id is NOT NULL there). Set by
# ``LLMClient._do_llm_call`` around the dispatch; None outside run context.
_current_run_id_var: ContextVar[str | None] = ContextVar(
    "model_breaker_current_run_id", default=None
)


def _alert_state_path() -> Path:
    """Location of the persistent per-model last-alerted state file."""
    return Path(
        os.environ.get("ROBOTHOR_MODEL_BREAKER_STATE", "/run/robothor/model-breaker-alerts.json")
    )


def _load_alert_state() -> dict[str, float]:
    """Read {model: last_alerted_epoch}. Missing/corrupt file → empty (fail open)."""
    try:
        raw = json.loads(_alert_state_path().read_text())
        if isinstance(raw, dict):
            return {str(k): float(v) for k, v in raw.items()}
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("model breaker alert state unreadable (%s) — treating as empty", exc)
    return {}


def _should_alert(model: str, now: float) -> bool:
    """True when this model's last alert is older than the dedup floor."""
    last = _load_alert_state().get(model)
    return last is None or (now - last) >= ALERT_DEDUP_SECONDS


def _mark_alerted(model: str, now: float) -> None:
    """Persist the alert timestamp. Best-effort — never blocks the alert."""
    try:
        path = _alert_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        state = _load_alert_state()
        state[model] = now
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(path)
    except Exception as exc:
        logger.warning("model breaker could not persist alert dedup state: %s", exc)


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


def _in_pytest() -> bool:
    """True when running inside a pytest session (seam for the alert guard)."""
    return "PYTEST_CURRENT_TEST" in os.environ


def _record_guardrail_event(message: str) -> None:
    """Land the trip in agent_guardrail_events, where dashboards and
    flag-evidence queries look. Only possible inside run context — the table's
    run_id is NOT NULL — and best-effort like every guardrail write."""
    run_id = _current_run_id_var.get()
    if not run_id:
        return
    try:
        from robothor.engine.tracking import log_guardrail_event

        log_guardrail_event(
            run_id,
            "model_breaker",
            "blocked",
            reason=message,
            mode="enforce",
        )
    except Exception as exc:  # noqa: BLE001 — evidence must never break a run
        logger.error("model breaker could not record guardrail event: %s", exc)


def _notify_operator(model: str, message: str) -> None:
    """Deliver the trip to the operator: DB escalation row + Telegram.

    Deliberately NOT via ``notify_guardrail_alert`` — its template says the
    guardrail "would have BLOCKED this call under enforce", which is false
    here: the breaker always enforces (open models are skipped outright).
    """
    if _in_pytest():
        # Test sessions trip the process-global breaker against the shared
        # DB; 92 of the 145 production escalation rows were pytest fixture
        # models. Never page the operator from a test run.
        logger.info("model breaker alert suppressed under pytest: %s", message)
        return
    try:
        from robothor.constants import DEFAULT_TENANT
        from robothor.crm import dal
        from robothor.engine.feature_flags import _post_telegram

        body = (
            f"{message}\n\n"
            f"If this is the fleet primary, check the provider key/account — a "
            f"dead primary previously went unnoticed for a month."
        )
        notif_id = dal.send_notification(
            from_agent="engine",
            to_agent="main",
            notification_type="escalation",
            subject=f"Model circuit open: {model}",
            body=body,
            tenant_id=DEFAULT_TENANT,
        )
        delivered = _post_telegram(f"⚠️ {body}")
        if not notif_id and not delivered:
            logger.error(
                "model breaker trip for %s was not delivered anywhere — "
                "the operator has not been told",
                model,
            )
    except Exception as exc:  # noqa: BLE001 — an alert must never break a run
        logger.error("model breaker could not alert the operator: %s", exc)


def _alert_operator(model: str, reason: str) -> None:
    """on_open hook for the engine-wide breaker.

    Every trip records a guardrail event (evidence); the operator ping is
    deduped by a persistent per-model floor (``ALERT_DEDUP_SECONDS``) so a
    provider that flaps every cooldown cycle pages once, not every ~10
    minutes. A model's first trip ever still alerts immediately.
    """
    message = (
        f"model {model} circuit OPEN ({DEFAULT_THRESHOLD} consecutive failures), "
        f"skipped for {DEFAULT_COOLDOWN}s. Last error: {reason}"
    )
    _record_guardrail_event(message)
    now = time.time()
    if not _should_alert(model, now):
        logger.info(
            "model breaker re-alert for %s suppressed (last alert < %ds ago)",
            model,
            ALERT_DEDUP_SECONDS,
        )
        return
    _notify_operator(model, message)
    _mark_alerted(model, now)


_BREAKER = ModelBreaker(on_open=_alert_operator)


def get_model_breaker() -> ModelBreaker:
    """The engine-wide breaker used by the LLM fallback chain."""
    return _BREAKER
