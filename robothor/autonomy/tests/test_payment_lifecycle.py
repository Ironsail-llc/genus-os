"""Payment evidence does not conflate submission, charge or recovered funds."""

import pytest

from robothor.autonomy.payment_lifecycle import PaymentFact, project_payment


def fact(kind, amount=0, key=None, source="issuer"):
    return PaymentFact(
        event_key=key or kind, kind=kind, amount_minor=amount, currency="USD", source=source
    )


def test_merchant_confirmation_only_proves_submission():
    result = project_payment([fact("submitted", source="merchant")], limit_minor=600)
    assert result.state == "submitted"
    assert result.charged_minor == result.refunded_minor == 0
    with pytest.raises(ValueError, match="issuer_evidence_required"):
        project_payment([fact("charged", 600, source="merchant")], limit_minor=600)


def test_partial_charges_and_refunds_keep_gross_and_net_distinct():
    rows = [
        fact("authorized", 600),
        fact("charged", 400, "charge-1"),
        fact("charged", 200, "charge-2"),
        fact("refunded", 150, "refund-1"),
    ]
    result = project_payment(rows, limit_minor=600)
    assert result.state == "partially_refunded"
    assert (result.authorized_minor, result.charged_minor, result.refunded_minor) == (600, 600, 150)
    assert result.net_charged_minor == 450
    assert (
        project_payment(rows + [fact("refunded", 450, "refund-2")], limit_minor=600).state
        == "refunded"
    )


def test_identical_delivery_replays_but_changed_delivery_is_rejected():
    event = fact("charged", 600, "capture")
    assert project_payment([event, event], limit_minor=600).charged_minor == 600
    with pytest.raises(ValueError, match="payment_event_conflict"):
        project_payment([event, fact("charged", 500, "capture")], limit_minor=600)


def test_reversed_authorization_does_not_become_a_refund_or_allow_late_capture():
    rows = [fact("authorized", 600), fact("reversed", 600)]
    result = project_payment(rows, limit_minor=600)
    assert result.state == "reversed"
    assert result.charged_minor == result.refunded_minor == 0
    with pytest.raises(ValueError, match="charge_after_reversal"):
        project_payment(rows + [fact("charged", 600)], limit_minor=600)


@pytest.mark.parametrize(
    "rows,reason",
    [
        ([fact("refunded", 1)], "refund_exceeds_charge"),
        ([fact("charged", 600), fact("reversed", 600)], "invalid_authorization_reversal"),
        (
            [fact("authorized", 600), fact("authorized", 600, "again")],
            "authorization_already_recorded",
        ),
    ],
)
def test_inconsistent_evidence_cannot_change_balances(rows, reason):
    with pytest.raises(ValueError, match=reason):
        project_payment(rows, limit_minor=600)


def test_currency_is_bound_to_reserved_payment():
    row = fact("charged", 600).model_copy(update={"currency": "EUR"})
    with pytest.raises(ValueError, match="payment_currency_mismatch"):
        project_payment([row], limit_minor=600, currency="USD")


def test_verified_overcharge_is_visible_and_flagged_not_discarded():
    result = project_payment([fact("authorized", 500), fact("charged", 601)], limit_minor=600)
    assert result.charged_minor == 601
    assert result.limit_exceeded and result.authorization_exceeded


def test_late_authorization_preserves_charge_and_reports_discrepancy():
    result = project_payment([fact("charged", 600), fact("authorized", 500)], limit_minor=600)
    assert result.state == "charged"
    assert not result.limit_exceeded and result.authorization_exceeded


@pytest.mark.parametrize("amount", [True, 1.5, -1, "600"])
def test_money_amounts_are_integer_minor_units(amount):
    with pytest.raises(ValueError):
        fact("charged", amount)


def test_late_submission_never_downgrades_a_verified_charge():
    result = project_payment(
        [fact("charged", 600), fact("submitted", source="merchant")], limit_minor=600
    )
    assert result.state == "charged" and result.charged_minor == 600


def test_delivery_order_does_not_change_known_charge_and_refund_totals():
    from itertools import permutations

    rows = [
        fact("authorized", 600),
        fact("charged", 400, "capture-one"),
        fact("charged", 200, "capture-two"),
        fact("refunded", 150, "refund-one"),
        fact("refunded", 450, "refund-two"),
    ]
    for delivered in permutations(rows):
        position = project_payment(list(delivered), limit_minor=600)
        assert position.state == "refunded"
        assert (position.authorized_minor, position.charged_minor, position.refunded_minor) == (
            600,
            600,
            600,
        )
        assert position.net_charged_minor == 0


def test_reversal_delivered_before_authorization_resolves_when_authorization_arrives():
    with pytest.raises(ValueError):
        project_payment([fact("reversed", 600)], limit_minor=600)
    position = project_payment([fact("reversed", 600), fact("authorized", 600)], limit_minor=600)
    assert position.state == "reversed" and position.reversed_minor == 600
    assert position.charged_minor == position.refunded_minor == 0


def test_reordering_does_not_conceal_excess_refunds_or_authorization_conflicts():
    with pytest.raises(ValueError, match="refund_exceeds_charge"):
        project_payment([fact("refunded", 601), fact("charged", 600)], limit_minor=600)
    with pytest.raises(ValueError, match="authorization_already_recorded"):
        project_payment(
            [
                fact("refunded", 600),
                fact("authorized", 600),
                fact("charged", 600),
                fact("authorized", 700, "other"),
            ],
            limit_minor=600,
        )
