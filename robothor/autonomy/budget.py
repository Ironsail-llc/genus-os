"""Calendar projections for initial charges and recorded recurring commitments."""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from typing import Any

from robothor.autonomy.models import Recurrence


def _month_index(value: date) -> int:
    return value.year * 12 + value.month - 1


def monthly_projection(rows: list[dict[str, Any]], *, today: date) -> dict[str, int]:
    # A new first renewal is at most a year away. Two years cover its first
    # complete billing cycle and every supported interval (divisors of 12).
    first = _month_index(today)
    months = dict.fromkeys(range(first, first + 25), 0)
    for row in rows:
        if row["state"] not in {
            "reserved",
            "awaiting_input",
            "submitting",
            "reconciling",
            "completed",
        }:
            continue
        proposal = row["proposal"]
        updated = row["updated_at"].astimezone(UTC).date()
        if row["state"] != "completed" or _month_index(updated) == first:
            months[first] += proposal["amount_minor"]
        recurring = proposal.get("recurring_minor", 0)
        if not recurring:
            continue
        if not proposal.get("recurrence"):
            raise ValueError("renewal_schedule_missing")
        terms = Recurrence.model_validate(proposal["recurrence"])
        anchor = terms.next_charge_on
        anchor_month = _month_index(anchor)
        offset = max(0, (first - anchor_month + terms.interval_months - 1) // terms.interval_months)
        index = anchor_month + offset * terms.interval_months
        while index < first + 25:
            year, month = divmod(index, 12)
            month += 1
            charge_day = date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))
            if terms.ends_on is not None and charge_day > terms.ends_on:
                break
            if index in months:
                months[index] += recurring
            index += terms.interval_months
    return {f"{index // 12:04d}-{index % 12 + 1:02d}": amount for index, amount in months.items()}


def proposal_record(proposal: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    return {"state": "reserved", "updated_at": now, "proposal": proposal}
