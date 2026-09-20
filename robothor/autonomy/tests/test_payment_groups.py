"""Recurring payments retain independent capture/refund balances and authority."""

from datetime import date

import pytest

from robothor.autonomy.payment_groups import summarize_payments
from robothor.autonomy.payment_lifecycle import PaymentFact, project_payment


def operation():
    return {
        "id": "operation",
        "proposal": {
            "action": "subscription",
            "amount_minor": 100,
            "recurring_minor": 600,
            "currency": "USD",
            "recurrence": {
                "interval_months": 1,
                "next_charge_on": "2026-01-31",
                "ends_on": "2026-04-30",
            },
        },
    }


def fact(key, kind, amount, renewal=None, due=None):
    return PaymentFact(
        event_key=key,
        kind=kind,
        amount_minor=amount,
        currency="USD",
        source="issuer",
        renewal_id=renewal,
        renewal_on=due,
    )


def test_initial_and_renewal_refunds_do_not_cross_payment_boundaries():
    rows = [
        fact("initial", "charged", 100),
        fact("renewal-refund", "refunded", 200, "private-period", "2026-02-28"),
        fact("renewal-charge", "charged", 600, "private-period", "2026-02-28"),
    ]
    result = summarize_payments(operation(), rows)
    assert result["position"]["net_charged_minor"] == 100
    renewal = result["renewals"][0]
    assert renewal["position"]["net_charged_minor"] == 400
    assert renewal["due_on"] == "2026-02-28" and renewal["schedule_matches"]
    assert not result["reconciliation_required"]
    assert "private-period" not in str(result)
    with pytest.raises(ValueError, match="payment_group_mismatch"):
        project_payment(rows, limit_minor=600)


def test_refund_cannot_use_a_different_renewals_charge():
    result = summarize_payments(
        operation(),
        [
            fact("a", "charged", 600, "period-a", "2026-02-28"),
            fact("b", "refunded", 600, "period-b", "2026-03-31"),
        ],
    )
    assert result["reconciliation_required"]
    assert result["renewals"][1]["position"] is None
    assert result["position"]["state"] == "unconfirmed"


@pytest.mark.parametrize("due", ["2026-01-30", "2026-03-30", "2026-05-31"])
def test_wrong_period_date_is_recorded_and_flagged(due):
    result = summarize_payments(operation(), [fact("charge", "charged", 600, "period", due)])
    assert result["renewals"][0]["position"]["charged_minor"] == 600
    assert result["reconciliation_required"] and not result["renewals"][0]["schedule_matches"]


def test_duplicate_charges_for_same_period_exceed_period_limit():
    result = summarize_payments(
        operation(),
        [
            fact("a", "charged", 600, "attempt-a", "2026-02-28"),
            fact("b", "charged", 600, "attempt-b", "2026-02-28"),
        ],
    )
    assert result["reconciliation_required"]
    assert all(r["period_limit_exceeded"] for r in result["renewals"])
    assert all(r["position"]["charged_minor"] == 600 for r in result["renewals"])


def test_conflicting_dates_for_one_payment_are_not_split_or_silently_accepted():
    result = summarize_payments(
        operation(),
        [
            fact("a", "charged", 600, "same", "2026-02-28"),
            fact("b", "refunded", 100, "same", "2026-03-31"),
        ],
    )
    assert result["reconciliation_required"] and len(result["renewals"]) == 1
    assert result["renewals"][0]["due_on"] is None


def test_renewal_requires_both_identity_and_date_and_issuer_provenance():
    for updates in (
        {"renewal_id": "period"},
        {"renewal_on": date(2026, 2, 28)},
        {"renewal_id": "period", "renewal_on": date(2026, 2, 28), "source": "merchant"},
    ):
        with pytest.raises(ValueError):
            PaymentFact(
                event_key="x",
                kind="submitted",
                amount_minor=0,
                currency="USD",
                **{"source": "issuer", **updates},
            )


@pytest.mark.parametrize("due,matches", [("2026-04-30", True), ("2026-02-28", False)])
def test_quarterly_schedule_uses_saved_interval_with_month_end_clamping(due, matches):
    op = operation()
    op["proposal"]["recurrence"]["interval_months"] = 3
    result = summarize_payments(op, [fact("charge", "charged", 600, "period", due)])
    assert result["renewals"][0]["schedule_matches"] is matches
    assert result["reconciliation_required"] is not matches


def test_unexpected_renewal_on_one_time_purchase_is_preserved_and_flagged():
    op = operation()
    op["proposal"].update(action="purchase", recurring_minor=0, recurrence=None)
    result = summarize_payments(op, [fact("charge", "charged", 600, "unexpected", "2026-02-28")])
    assert result["reconciliation_required"]
    assert result["renewals"][0]["position"]["charged_minor"] == 600
    assert result["renewals"][0]["position"]["limit_exceeded"]


def test_renewal_display_identity_does_not_change_when_late_earlier_period_arrives():
    later = fact("later", "charged", 600, "private-later", "2026-03-31")
    before = summarize_payments(operation(), [later])
    after = summarize_payments(
        operation(), [later, fact("earlier", "charged", 600, "private-earlier", "2026-02-28")]
    )
    assert after["renewals"][1]["id"] == before["renewals"][0]["id"]
