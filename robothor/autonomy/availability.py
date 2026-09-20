"""Is delegated execution actually live for this run?

One question, asked once per run, that decides whether the engine spends
anything at all on autonomy: the system-prompt paragraph
(:data:`robothor.engine.session.AUTONOMY_BROWSER_PROMPT`) and the ~1,100
schema tokens of browser wording
(:data:`robothor.engine.tools.schemas.BROWSER_AUTONOMY_DESCRIPTION`).

Both used to be unconditional. A feature nobody had enabled changed every
agent's prompt and every agent's tool schema on every instance, and one of
those changes told the model that a standing grant is prior authorization and
not to "impose a blanket stop before submission" — on instances that had no
grants at all.

Four things have to be true, and they are four separate opt-ins:

0. this instance offers personal automation at all
   (``ROBOTHOR_AUTONOMY_ENABLED``, off by default) — checked first, and
   without touching the database, because on almost every instance it is the
   whole answer;
1. the run has an identified human owner (a cron or sub-agent run has none,
   and a grant is written by a person);
2. delegated execution is switched on for that owner;
3. a live standing grant names THIS agent.

Anything else — no autonomy tables, no enrolment, an unlinked account, a
database that is down — is "no", quietly. This decides how verbose a prompt
is; it is not a security boundary, and it must never fail a run. The broker
re-checks real authority on every operation
(``AutonomyStore.check_authority``), so a wrong "yes" here costs tokens and a
wrong "no" costs a hint, never authority either way.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def feature_offered() -> bool:
    """Does this INSTANCE offer personal automation? Off unless configured on.

    Separate from :func:`autonomy_active` so the dashboard and the bridge can
    ask the instance-level question without an owner in hand.
    """
    try:
        from robothor.settings import get_settings

        return bool(get_settings().autonomy.enabled)
    except Exception as exc:  # noqa: BLE001 - unreadable settings mean "off"
        logger.debug("autonomy feature flag unreadable (%s): %s", type(exc).__name__, exc)
        return False


def autonomy_active(tenant_id: str | None, actor_id: str | None, agent_id: str | None) -> bool:
    """True iff autonomy is enabled for this owner and a live grant names ``agent_id``."""
    if not feature_offered() or not tenant_id or not actor_id or not agent_id:
        return False
    try:
        from robothor.autonomy.identity import scope_for_actor
        from robothor.autonomy.store import AutonomyStore

        store = AutonomyStore()
        scope = scope_for_actor(tenant_id, actor_id)
        if not store.settings(scope).enabled:
            return False
        return any(_covers(grant, agent_id) for grant in store.grants(scope))
    except Exception as exc:  # noqa: BLE001 - a prompt hint must not fail a run
        logger.debug("autonomy availability unknown (%s): %s", type(exc).__name__, exc)
        return False


def _covers(grant: dict[str, Any], agent_id: str) -> bool:
    """Is this one grant live, and does it name this agent?

    Mirrors what the broker will check later, minus the parts that need an
    actual proposal (origin, action, budget). Deliberately conservative: a
    grant row this cannot parse is not a grant this will advertise.
    """
    if grant.get("revoked"):
        return False
    policy = grant.get("policy") or {}
    if not policy.get("enabled", True):
        return False
    if agent_id not in set(policy.get("agent_ids") or ()):
        return False
    expires_at = policy.get("expires_at")
    if not expires_at:
        return False
    try:
        deadline = (
            expires_at
            if isinstance(expires_at, datetime)
            else datetime.fromisoformat(str(expires_at))
        )
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline > datetime.now(UTC)
