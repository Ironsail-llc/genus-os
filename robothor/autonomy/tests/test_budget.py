"""Renewals occupy the calendar months in which the merchant can charge."""

from datetime import UTC, date, datetime

from robothor.autonomy.budget import monthly_projection


def record(
    *, amount=0, recurring=600, interval=1, next_charge="2026-10-31", end=None, state="completed"
):
    return {
        "state": state,
        "updated_at": datetime(2026, 9, 19, tzinfo=UTC),
        "proposal": {
            "amount_minor": amount,
            "recurring_minor": recurring,
            "recurrence": {
                "interval_months": interval,
                "next_charge_on": next_charge,
                "ends_on": end,
            },
        },
    }


def test_month_end_renewals_keep_the_original_anchor_and_include_final_day():
    usage = monthly_projection([record(end="2027-02-28")], today=date(2026, 9, 19))
    assert usage["2026-09"] == 0
    assert usage["2026-10"] == usage["2027-02"] == 600
    assert usage["2027-03"] == 0


def test_annual_renewal_is_not_treated_as_a_monthly_charge():
    usage = monthly_projection([record(interval=12)], today=date(2026, 9, 19))
    assert usage["2026-10"] == 600
    assert usage["2026-11"] == 0
    assert usage["2027-10"] == 600


def test_current_month_includes_purchase_and_scheduled_renewal():
    usage = monthly_projection(
        [record(amount=100, next_charge="2026-09-20")], today=date(2026, 9, 19)
    )
    assert usage["2026-09"] == 700
    assert usage["2026-10"] == 600


def test_old_completed_purchase_rolls_off_but_renewal_and_unknown_submission_do_not():
    completed = record(amount=100, next_charge="2026-09-20")
    pending = record(amount=50, recurring=0, state="reconciling")
    usage = monthly_projection([completed, pending], today=date(2026, 10, 1))
    assert usage["2026-10"] == 650


def test_legacy_commitment_without_dates_is_reported_as_unresolved():
    from pytest import raises

    legacy = record()
    legacy["proposal"].pop("recurrence")
    with raises(ValueError, match="renewal_schedule_missing"):
        monthly_projection([legacy], today=date(2026, 9, 19))
