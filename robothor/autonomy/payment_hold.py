"""A grant that has already overshot stops authorising new payments.

Recording ``limit_exceeded`` on a private panel is not a control: the reviewed
finding was a 400x overcharge that changed no state, blocked no budget and
notified nobody. This module is the block. When issuer evidence shows a payment
above the reservation, above its own authorization, or a renewal above its
period allowance, the grant goes on hold and the operator is paged. Until the
operator clears it, that grant authorises no further ``purchase`` or
``subscription``.

The hold lives in ``autonomy_events``, the append-only journal the rest of the
subsystem already writes, so it needs no schema change and survives a restart:
the most recent of the placed/cleared pair for a grant wins. Non-payment
actions (``login``, ``account``, ``application``) are deliberately untouched --
the hold is about money, and freezing a mailbox login would not protect any.

Reconciliation is deliberately untouched too. Money that has already moved must
still be finished and recorded; refusing that would strand the very operation
the hold exists to surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.autonomy.models import Scope
    from robothor.autonomy.store import AutonomyStore

#: Event names, written with ``AutonomyStore._event`` against the grant id.
HOLD_PLACED = "grant_payment_hold_placed"
HOLD_CLEARED = "grant_payment_hold_cleared"

#: Refusal code every gated path raises.
REFUSAL = "grant_payment_hold"

#: Actions a hold refuses. A hold is about money, not about access.
GATED_ACTIONS = frozenset({"purchase", "subscription"})


def overspend_reasons(summary: dict[str, Any]) -> list[str]:
    """Overspend signals in a payment summary, named for the operator.

    Only signals that mean *more money moved than was authorised*. A summary
    can also require reconciliation because evidence is inconsistent or a
    renewal date is unexpected; those are unresolved facts, not proof of
    overspend, and they must not freeze a grant on their own.
    """
    reasons: list[str] = []
    position = summary.get("position")
    if position:
        if position.get("limit_exceeded"):
            reasons.append("limit_exceeded")
        if position.get("authorization_exceeded"):
            reasons.append("authorization_exceeded")
    for renewal in summary.get("renewals", []):
        # ``id`` is the opaque ordinal reference the owner view already shows;
        # the issuer's own transaction identity never leaves the journal.
        reference = renewal.get("id", "renewal")
        if renewal.get("period_limit_exceeded"):
            reasons.append(f"renewal_period_limit_exceeded:{reference}")
        inner = renewal.get("position")
        if not inner:
            continue
        if inner.get("limit_exceeded"):
            reasons.append(f"renewal_limit_exceeded:{reference}")
        if inner.get("authorization_exceeded"):
            reasons.append(f"renewal_authorization_exceeded:{reference}")
    return reasons


def grant_on_hold(cur: Any, scope: Scope, grant_id: str) -> bool:
    """True when the latest hold event for this grant is a placement."""
    cur.execute(
        "SELECT event FROM autonomy_events WHERE tenant_id=%s AND owner_id=%s "
        "AND subject_id=%s AND event IN (%s,%s) ORDER BY id DESC LIMIT 1",
        (scope.tenant_id, scope.owner_id, grant_id, HOLD_PLACED, HOLD_CLEARED),
    )
    row = cur.fetchone()
    return bool(row and row["event"] == HOLD_PLACED)


def place_hold(
    cur: Any,
    store: AutonomyStore,
    scope: Scope,
    grant_id: str,
    operation_id: str,
    reasons: list[str],
) -> bool:
    """Freeze the grant. Returns True only on the transition into a hold.

    The caller alerts on that transition, so a second overshooting fact on an
    already-frozen grant does not page the operator again.
    """
    if grant_on_hold(cur, scope, grant_id):
        return False
    store._event(cur, scope, grant_id, HOLD_PLACED)
    for reason in reasons:
        store._event(cur, scope, operation_id, f"payment_overspend:{reason}")
    return True


def alert_body(operation_id: str, grant_id: str, reasons: list[str]) -> str:
    """What the operator needs to act, with no issuer reference in it."""
    lines = [
        "Issuer evidence shows a delegated payment above the authority it was given.",
        "",
        f"Operation: {operation_id}",
        f"Grant: {grant_id}",
        f"Signals: {', '.join(reasons)}",
        "",
        "Autonomous spending on this grant is now FROZEN. No further purchase or "
        "subscription will be authorised against it until you clear the hold in "
        "Account -> Personal automation. Evidence recording and reconciliation of "
        "operations already in flight continue normally.",
    ]
    if any(reason.startswith("renewal_") for reason in reasons):
        lines += [
            "",
            "This is a recurring charge above its saved allowance. Revoking the grant "
            "does not cancel a scheduled renewal -- the merchant holds the mandate, so "
            "cancel it with the merchant directly.",
        ]
    return "\n".join(lines)
