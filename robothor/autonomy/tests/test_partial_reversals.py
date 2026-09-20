"""Partial authorization releases must not masquerade as refunds or free budget."""

from itertools import permutations

import pytest

from robothor.autonomy.payment_lifecycle import project_payment
from robothor.autonomy.tests.test_payment_lifecycle import fact


def test_partial_reversals_accumulate_without_reopening_spending():
    rows = [fact("authorized", 600), fact("reversed", 100, "release-1")]
    position = project_payment(rows, limit_minor=600)
    assert position.state == "authorized"
    assert position.reversed_minor == 100 and position.authorization_open_minor == 500
    position = project_payment(rows + [fact("reversed", 500, "release-2")], limit_minor=600)
    assert position.state == "reversed" and position.authorization_open_minor == 0
    assert position.charged_minor == position.refunded_minor == 0


def test_capture_and_release_unused_authorization_are_arrival_independent():
    rows = [
        fact("authorized", 600),
        fact("charged", 400),
        fact("reversed", 100, "release-1"),
        fact("reversed", 100, "release-2"),
        fact("refunded", 200),
    ]
    for order in permutations(rows):
        position = project_payment(list(order), limit_minor=600)
        assert position.state == "partially_refunded"
        assert position.reversed_minor == 200 and position.authorization_open_minor == 0
        assert position.net_charged_minor == 200 and not position.authorization_exceeded


def test_partial_refund_does_not_reopen_an_authorization():
    position = project_payment(
        [fact("authorized", 600), fact("charged", 400), fact("refunded", 400)], limit_minor=600
    )
    assert position.state == "refunded" and position.authorization_open_minor == 200


def test_excess_releases_stay_unresolved_and_replay_is_idempotent():
    release = fact("reversed", 200)
    position = project_payment(
        [fact("authorized", 600), fact("charged", 400), release, release], limit_minor=600
    )
    assert position.reversed_minor == 200
    with pytest.raises(ValueError):
        project_payment(
            [fact("authorized", 600), fact("charged", 400), fact("reversed", 201)], limit_minor=600
        )


def test_encrypted_journal_resolves_partial_release_without_changing_budget(store, identity):
    from robothor.autonomy.payment_journal import PaymentJournal
    from robothor.autonomy.tests.test_handoffs import pending

    op = pending(store, identity)
    store.begin_submit(identity, op["id"], "main")
    before = store.spending_projection(identity)
    journal = PaymentJournal(store)
    assert journal.append(identity, op["id"], fact("reversed", 200))["reconciliation_required"]
    journal.append(identity, op["id"], fact("charged", 400))
    store.revoke_grant(identity, store.operation(identity, op["id"])["grant_id"])
    result = journal.append(identity, op["id"], fact("authorized", 600))
    assert not result["reconciliation_required"]
    assert result["position"]["reversed_minor"] == 200
    assert result["position"]["authorization_open_minor"] == 0
    assert result["position"]["net_charged_minor"] == 400
    assert journal.append(identity, op["id"], fact("reversed", 200)) == result
    assert PaymentJournal(store).read(identity, op["id"]) == result
    assert store.spending_projection(identity) == before
