"""Separate initial payment and issuer-identified recurring transactions."""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import date

from robothor.autonomy.models import Recurrence
from robothor.autonomy.payment_lifecycle import PaymentFact, project_payment


def _position(facts: list[PaymentFact], limit: int, currency: str) -> dict[str, Any]:
    try:
        position = project_payment(facts, limit_minor=limit, currency=currency)
    except ValueError:
        return {"position": None, "reconciliation_required": True}
    return {
        "position": {**asdict(position), "net_charged_minor": position.net_charged_minor},
        "reconciliation_required": position.limit_exceeded or position.authorization_exceeded,
    }


def _matches(proposal: dict[str, Any], due: date | None) -> bool:
    if proposal["action"] != "subscription" or not due or not proposal.get("recurrence"):
        return False
    terms = Recurrence.model_validate(proposal["recurrence"])
    anchor = terms.next_charge_on
    months = (due.year - anchor.year) * 12 + due.month - anchor.month
    return bool(
        months >= 0
        and months % terms.interval_months == 0
        and due.day == min(anchor.day, calendar.monthrange(due.year, due.month)[1])
        and (terms.ends_on is None or due <= terms.ends_on)
    )


def summarize_payments(op: dict[str, Any], facts: list[PaymentFact]) -> dict[str, Any]:
    proposal = op["proposal"]
    groups: dict[str | None, list[PaymentFact]] = defaultdict(list)
    for original in facts:
        fact = PaymentFact.model_validate(original.model_dump())
        groups[fact.renewal_id].append(fact)
    result = _position(groups.pop(None, []), proposal["amount_minor"], proposal["currency"])
    result.update(event_count=len(facts), renewals=[])
    for index, group in enumerate(groups.values(), start=1):
        dates = {fact.renewal_on for fact in group}
        due = next(iter(dates)) if len(dates) == 1 else None
        matches = _matches(proposal, due)
        renewal = {
            **_position(group, proposal.get("recurring_minor", 0), proposal["currency"]),
            "id": f"renewal-{index}",
            "due_on": due.isoformat() if due else None,
            "schedule_matches": matches,
            "event_count": len(group),
            "period_limit_exceeded": False,
        }
        renewal["reconciliation_required"] |= not matches
        result["renewals"].append(renewal)
    # Multiple issuer transaction IDs can refer to the same billing period.
    # Sum gross charges, not net refunds: refunds do not authorize another charge.
    totals: dict[str, int] = defaultdict(int)
    for renewal in result["renewals"]:
        if renewal["position"] and renewal["due_on"]:
            totals[renewal["due_on"]] += renewal["position"]["charged_minor"]
    for renewal in result["renewals"]:
        renewal["period_limit_exceeded"] = bool(
            renewal["due_on"] and totals[renewal["due_on"]] > proposal.get("recurring_minor", 0)
        )
        renewal["reconciliation_required"] |= renewal["period_limit_exceeded"]
        result["reconciliation_required"] |= renewal["reconciliation_required"]
    result["renewals"].sort(key=lambda item: (item["due_on"] or "", item["id"]))
    return result
