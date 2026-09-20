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
database that is down — is "no". This decides how verbose a prompt is; it is
not a security boundary, and it must never fail a run. The broker re-checks
real authority on every operation (``AutonomyStore.check_authority``), so a
wrong "yes" here costs tokens and a wrong "no" costs a hint, never authority
either way.

**Because it must never cost a run, it is bounded three ways.**
``AutonomyStore.transaction()`` opens a fresh ``psycopg2.connect`` every time
it is called, and this asked three separate times: against a hanging Postgres
that is three times ``connect_timeout=5``, fifteen seconds of an agent run
spent deciding how wordy a prompt should be. Now the settings and the grants
share one transaction, that transaction sets a ``statement_timeout`` (which
is what bounds a server that answers the handshake and then stops — the case
``connect_timeout`` does not reach at all), and the caller
(``toolset_prep._autonomy_active``) wraps the lot in ``asyncio.wait_for``,
because ``except Exception`` catches errors and not latency. The outer wait
is the real guarantee: whatever happens below it, the run waits one second.

Two connections, not one. The remaining one is
``identity.scope_for_actor``, and collapsing it would mean copying an
identity-resolution query — the one that decides WHOSE money a grant spends —
into a second file. A drift-prone duplicate of that is a worse trade than a
second bounded connect. The clean fix is a ``cur`` parameter on
``scope_for_actor`` so it can join a caller's transaction.

And "no" is not always quiet. On an instance that has NOT opted in there is
nothing to say and a warning would fire on every run forever. On one that
HAS, a lookup that keeps failing means an enrolled owner's agent keeps asking
for approval it was already given, and silence is how that goes unnoticed —
so that case warns.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: How long the whole lookup may hold the server, once connected.
#:
#: ``connect_timeout`` covers a server that will not answer the handshake. It
#: does nothing for one that answers it and then stops, which is the shape of
#: a Postgres under lock contention — the case an agent run is most likely to
#: meet. Generous next to the ~6 µs the flag-off path costs, and tiny next to
#: the run it is protecting.
_STATEMENT_TIMEOUT_MS = 800

#: The owner's switch and their grants, read together.
#:
#: These duplicate ``AutonomyStore.settings`` and ``AutonomyStore.grants``,
#: which are one transaction each. Read here so the pair costs one connection
#: instead of two; ``tests/test_autonomy_availability_cost.py`` fails if
#: either text drifts from the method it copies. Both are plain reads of two
#: columns — unlike identity resolution, there is no logic to get wrong.
_SETTINGS_SQL = "SELECT settings FROM autonomy_settings WHERE tenant_id=%s AND owner_id=%s"
_GRANTS_SQL = (
    "SELECT policy,revoked_at IS NOT NULL AS revoked "
    "FROM autonomy_grants WHERE tenant_id=%s AND owner_id=%s"
)


def feature_offered() -> bool:
    """Does this INSTANCE offer personal automation? Off unless configured on.

    Separate from :func:`autonomy_active` so the dashboard and the bridge can
    ask the instance-level question without an owner in hand.
    """
    try:
        from robothor.settings import get_settings

        return bool(get_settings().autonomy.enabled)
    except Exception as exc:  # noqa: BLE001 - unreadable settings mean "off"
        logger.warning("Autonomy feature flag unreadable: %s", _for_log(exc))
        return False


def _for_log(exc: BaseException) -> str:
    """An exception's type and message, safe to put in the journal.

    Two passes, and ``sanitize_log`` alone is not enough for either job.
    ``redact`` first, because psycopg2 puts the whole DSN in an
    ``OperationalError`` and that DSN carries ``password=``; then
    ``sanitize_log``, because the message is a string from outside and a
    newline in it forges a log line.

    Host, user and dbname deliberately survive. They are topology, not
    secrets, and they are the entire diagnostic content of "the autonomy
    lookup is broken" — an operator who cannot see which database was
    unreachable has been told nothing they can act on.
    """
    from robothor.sanitize import sanitize_log
    from robothor.secrets.redaction import redact

    return sanitize_log(f"{type(exc).__name__}: {redact(str(exc))}")


def autonomy_active(tenant_id: str | None, actor_id: str | None, agent_id: str | None) -> bool:
    """True iff autonomy is enabled for this owner and a live grant names ``agent_id``."""
    if not feature_offered() or not tenant_id or not actor_id or not agent_id:
        return False
    try:
        return _lookup(tenant_id, actor_id, agent_id)
    except Exception as exc:  # noqa: BLE001 - a prompt hint must not fail a run
        # WARNING, not DEBUG. We only get here with the feature switched ON,
        # which means somebody enrolled — and the symptom of a silent failure
        # is an agent that keeps asking for an approval its owner already
        # granted, which looks like the agent's fault and never like this.
        logger.warning("Autonomy availability could not be determined: %s", _for_log(exc))
        return False


def _lookup(tenant_id: str, actor_id: str, agent_id: str) -> bool:
    """Resolve the owner, then read their switch and grants on one connection."""
    from robothor.autonomy.identity import scope_for_actor
    from robothor.autonomy.store import AutonomyStore

    scope = scope_for_actor(tenant_id, actor_id)
    with AutonomyStore().transaction() as cur:
        # Bounds a server that completed the handshake and then stopped
        # answering — lock contention, a saturated pool. `connect_timeout`
        # never sees that case, and it is the likelier one.
        cur.execute(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}")

        cur.execute(_SETTINGS_SQL, (scope.tenant_id, scope.owner_id))
        row = cur.fetchone()
        if not row or not (row["settings"] or {}).get("enabled"):
            return False

        cur.execute(_GRANTS_SQL, (scope.tenant_id, scope.owner_id))
        return any(_covers(dict(grant), agent_id) for grant in cur.fetchall())


def _covers(grant: dict[str, Any], agent_id: str) -> bool:
    """Is this one grant live, and does it name this agent?

    Mirrors what the broker will check later, minus the parts that need an
    actual proposal (origin, action, budget). Deliberately conservative: a
    grant row this cannot parse is not a grant this will advertise.

    The policy goes through ``Delegation`` rather than being read key by key.
    Hand-rolling it read ``agent_ids`` with ``set(...)``, so a row whose
    ``agent_ids`` was the STRING ``"xyz"`` — not a list — iterated its
    characters, and this returned True for an agent called ``"x"``.
    ``create_grant`` cannot produce that, but a migration or a hand-written
    row can, and a schema that only holds because one writer is careful is
    not a schema. Validating also settles ``expires_at`` parsing and the
    naive-vs-aware datetime divergence in the one place that owns them.
    """
    if grant.get("revoked"):
        return False
    from pydantic import ValidationError

    from robothor.autonomy.models import Delegation

    try:
        policy = Delegation.model_validate(grant.get("policy") or {})
    except ValidationError:
        return False
    if not policy.enabled or agent_id not in policy.agent_ids:
        return False
    expires_at = policy.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > datetime.now(UTC)
