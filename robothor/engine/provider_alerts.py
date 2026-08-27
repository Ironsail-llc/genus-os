"""The page an operator gets when a provider's whole credential pool dies.

On 2026-08-27 the instance's single OpenRouter key hit its weekly cap and
the fleet was degraded for 48 hours without one page about the credential.
The evidence was everywhere and reached nobody:

* 949 x ``403 Forbidden``, 278 x ``Key limit exceeded (weekly limit)``
* 452 x ``every configured credential for it is retired`` — at ``info``,
  once per model, per call
* 18 agents raising ``Primary model unreached`` — at ``warning``, so
  ``alerts.py`` (``_PAGE_LEVELS = {"critical"}``) never paged
* ``grep -c "from robothor.engine.alerts import" llm_client.py`` -> **0**

The LLM path had no way to page at all, and the one component that can
(``model_breaker``) is deliberately bypassed on every credential branch —
correctly, since a rejected key says nothing about a model, but nothing
was put in its place. This module is that missing piece.

Two rules it exists to enforce:

1. **One page per outage, not one per skipped model.** ``KeyPool`` fires
   this on the exhaustion TRANSITION and re-arms on recovery.
2. **The page carries its consequence.** The operator already received ~11
   pages that night reading ``robothor-wal-offsite.service FAILED`` and
   correctly ignored them, because a unit name is not a consequence. A
   page that does not say what stopped working is noise wearing a siren.

Keys are never named. Everything identifies a credential by its env var or
by ``KeyPool.fingerprint``, never by material.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable  # noqa: TC003

from robothor.engine.key_pool import Retirement

logger = logging.getLogger(__name__)


def _in_pytest() -> bool:
    """Never page the operator from a test session.

    Mirrors ``model_breaker._in_pytest``: the suite drives real pools against
    the shared box, and 92 of 145 production escalation rows were once pytest
    fixture models.
    """
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _deliver(level: str, title: str, body: str) -> None:
    """Escalation row + Telegram, the path ``model_breaker`` already proves.

    Deliberately synchronous: ``KeyPool.retire`` is sync and is called from
    inside the dispatch loop, so hopping to the async ``alerts.alert`` would
    need an event loop that may not be the running one.
    """
    from robothor.constants import DEFAULT_TENANT
    from robothor.crm import dal
    from robothor.engine.feature_flags import _post_telegram

    notif_id = dal.send_notification(
        from_agent="engine",
        to_agent="main",
        notification_type="escalation",
        subject=title,
        body=body,
        tenant_id=DEFAULT_TENANT,
    )
    delivered = _post_telegram(f"🚨 <b>{title}</b>\n{body}")
    if not notif_id and not delivered:
        logger.error(
            "provider exhaustion for %s was not delivered anywhere — "
            "the operator has not been told",
            title,
        )


#: Why each retirement happened, in words that name the remedy. A page that
#: says "exhausted" makes the operator go find out which kind; these do not.
_REASON_TEXT: dict[Retirement, str] = {
    Retirement.CREDIT_EXHAUSTED: (
        "the account balance is spent — a top-up restores service without "
        "restarting the engine"
    ),
    Retirement.QUOTA_EXHAUSTED_PERIODIC: (
        "a calendar quota window is exhausted — a top-up will NOT fix this; "
        "it clears when the provider's window rolls, or on a raised cap"
    ),
    Retirement.AUTH_FAILED: (
        "the credential was rejected — it is revoked or mistyped, and it will "
        "not be retried for the life of this process"
    ),
}


def alert_provider_exhausted(var: str, reason: Retirement, *, pool_size: int) -> None:
    """Page: every credential for one provider is out of rotation.

    Args:
        var: The env var naming the pool (e.g. ``OPENROUTER_API_KEY``).
        reason: Why the last key went out.
        pool_size: How many keys the pool held. One is the precondition for
            a total outage and is called out explicitly — the module that
            prevents this shipped 2026-08-25 and was running with a single
            key, which is why it did nothing.
    """
    if _in_pytest():
        logger.info("provider exhaustion alert suppressed under pytest: %s", var)
        return
    try:
        why = _REASON_TEXT.get(reason, str(reason))
        spare = (
            f"This pool holds only one key ({var}), so it has no spare to rotate to. "
            f"Add {var}_2 to restore redundancy."
            if pool_size <= 1
            else f"All {pool_size} keys in this pool are out of rotation."
        )
        title = f"Credential pool exhausted: {var}"
        body = (
            f"No model authenticating with {var} can be reached — {why}.\n\n"
            f"{spare}\n\n"
            f"Consequence: every agent whose chain ends on this provider is now "
            f"falling back to the local tier, which serves a small number of "
            f"concurrent requests. Expect fleet-wide stalls and run timeouts "
            f"until a working credential is available."
        )
        _deliver("critical", title, body)
    except Exception as exc:  # noqa: BLE001 — an alert must never break dispatch
        logger.error("could not alert on provider exhaustion for %s: %s", var, exc)


def exhaustion_hook(var: str, *, pool_size: int) -> Callable[[Retirement], None]:
    """The ``on_exhausted`` callback for one provider's pool."""

    def _hook(reason: Retirement) -> None:
        alert_provider_exhausted(var, reason, pool_size=pool_size)

    return _hook
