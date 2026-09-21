"""Money that overshoots is heard, blocked, and never refused in silence.

Three reviewed findings:

1. An overcharge set ``limit_exceeded``/``reconciliation_required`` on a private
   panel and nothing else happened -- no alert, no block on the next payment.
2. A refused append rolled its own audit row back, so the refusal left no trace.
3. A renewal above its period allowance escaped both the alert and the cap.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robothor.autonomy import alerts
from robothor.autonomy.models import Recurrence, WebOperation
from robothor.autonomy.payment_journal import PaymentJournal
from robothor.autonomy.payment_lifecycle import PaymentFact
from robothor.autonomy.tests.test_store import policy, proposal


@pytest.fixture
def alerted(monkeypatch):
    """Capture owner alerts. A test never reaches a real channel."""
    sent: list[dict[str, str]] = []

    def capture(**kwargs: str) -> str:
        sent.append(kwargs)
        return "test-notification-id"

    monkeypatch.setattr(alerts, "notify_owner", capture)
    return sent


def purchase(store, identity, key="purchase-1", amount=600):
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal(key=key, amount=amount))
    store.begin_submit(identity, op["id"], "main")
    return grant["id"], op["id"]


def charge(key="issuer-private-reference", amount=600, kind="charged"):
    return PaymentFact(
        event_key=key, kind=kind, amount_minor=amount, currency="USD", source="issuer"
    )


def membership(store, identity, due):
    grant = store.create_grant(
        identity,
        policy().model_copy(
            update={
                "actions": frozenset({"subscription"}),
                "recurring_minor": 600,
                "annual_minor": 20000,
                "monthly_minor": 20000,
            }
        ),
    )
    op = store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://shop.example",
            action="subscription",
            purpose="Requested membership",
            idempotency_key="membership-1",
            amount_minor=100,
            recurring_minor=600,
            annual_commitment_minor=7200,
            recurrence=Recurrence(interval_months=1, next_charge_on=due),
        ),
    )
    store.begin_submit(identity, op["id"], "main")
    store.finish(identity, op["id"], "completed", {"origin": "https://shop.example"})
    return grant["id"], op["id"]


def renewal_proposal(due, key="membership-2"):
    return WebOperation(
        origin="https://shop.example",
        action="subscription",
        purpose="Requested membership",
        idempotency_key=key,
        amount_minor=100,
        recurring_minor=600,
        annual_commitment_minor=7200,
        recurrence=Recurrence(interval_months=1, next_charge_on=due),
    )


# --- Finding 1: money nobody hears about -----------------------------------


def test_overcharge_raises_an_owner_alert(store, identity, alerted):
    """400x the reservation must page the owner, not just colour a panel."""
    _, op = purchase(store, identity)
    result = PaymentJournal(store).append(identity, op, charge(amount=240000))
    assert result["position"]["limit_exceeded"]
    assert alerted, "a 400x overcharge notified nobody"
    assert "limit_exceeded" in alerted[0]["body"]
    assert op in alerted[0]["body"]
    assert alerted[0]["tenant_id"] == identity.tenant_id
    assert "issuer-private-reference" not in str(alerted)


def test_authorization_overshoot_raises_an_owner_alert(store, identity, alerted):
    _, op = purchase(store, identity)
    journal = PaymentJournal(store)
    journal.append(identity, op, charge(key="auth", amount=100, kind="authorized"))
    assert not alerted
    # Captured above the hold, but still under the reservation: only the
    # authorization is exceeded, so this pins that signal on its own.
    result = journal.append(identity, op, charge(key="capture", amount=200))
    assert not result["position"]["limit_exceeded"]
    assert result["position"]["authorization_exceeded"]
    assert alerted and "authorization_exceeded" in alerted[0]["body"]


def test_next_payment_on_an_overshot_grant_is_refused(store, identity, alerted):
    """The gate: a grant that already overshot cannot authorise the next payment."""
    grant, op = purchase(store, identity)
    PaymentJournal(store).append(identity, op, charge(amount=240000))
    with pytest.raises(PermissionError, match="grant_payment_hold"):
        store.reserve(identity, grant, "main", proposal(key="purchase-2", amount=100))


def test_an_already_reserved_operation_cannot_submit_under_a_hold(store, identity, alerted):
    grant = store.create_grant(identity, policy())
    first = store.reserve(identity, grant["id"], "main", proposal(key="purchase-a", amount=300))
    later = store.reserve(identity, grant["id"], "main", proposal(key="purchase-b", amount=300))
    store.begin_submit(identity, first["id"], "main")
    PaymentJournal(store).append(identity, first["id"], charge(amount=240000))
    with pytest.raises(PermissionError, match="grant_payment_hold"):
        store.begin_submit(identity, later["id"], "main")
    with pytest.raises(PermissionError, match="grant_payment_hold"):
        store.check_authority(identity, later["id"], "main")


def test_a_hold_never_blocks_reconciling_the_operation_that_caused_it(store, identity, alerted):
    """Recovery is not new spend; money already moved must still be finished."""
    _, op = purchase(store, identity)
    PaymentJournal(store).append(identity, op, charge(amount=240000))
    store.check_reconcile_authority(identity, op, "main")
    store.finish(identity, op, "completed", {"origin": "https://shop.example"})
    assert store.operation(identity, op)["state"] == "completed"


def test_the_owner_can_clear_a_hold_and_spending_resumes(store, identity, alerted):
    grant, op = purchase(store, identity)
    PaymentJournal(store).append(identity, op, charge(amount=240000))
    assert store.payment_hold(identity, grant)
    store.clear_payment_hold(identity, grant)
    assert not store.payment_hold(identity, grant)
    assert store.reserve(identity, grant, "main", proposal(key="purchase-2", amount=100))["state"]


def test_a_hold_is_scoped_to_its_own_grant(store, identity, alerted):
    grant, op = purchase(store, identity)
    other = store.create_grant(identity, policy())
    PaymentJournal(store).append(identity, op, charge(amount=240000))
    assert store.reserve(identity, other["id"], "main", proposal(key="elsewhere", amount=100))
    assert not store.payment_hold(identity, other["id"])


def test_the_owner_is_alerted_once_per_hold_not_once_per_fact(store, identity, alerted):
    _, op = purchase(store, identity)
    journal = PaymentJournal(store)
    journal.append(identity, op, charge(key="one", amount=240000))
    journal.append(identity, op, charge(key="two", amount=1))
    assert len(alerted) == 1


def test_the_delivery_path_is_the_platform_notification_surface(monkeypatch):
    """No new channel: the alert is a crm_agent_notifications escalation row."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(alerts, "_in_pytest", lambda: False)
    from robothor.crm import dal

    monkeypatch.setattr(
        dal, "send_notification", lambda **kwargs: calls.append(kwargs) or "notif-1"
    )
    assert alerts.notify_owner(tenant_id="t", subject="Overcharge", body="details") == "notif-1"
    assert calls[0]["to_agent"] == "main"
    assert calls[0]["notification_type"] == "escalation"
    assert calls[0]["tenant_id"] == "t"


def test_no_alert_leaves_a_pytest_session_unpatched():
    """Backstop: an un-mocked test can never page the operator."""
    assert alerts._in_pytest()
    assert alerts.notify_owner(tenant_id="t", subject="s", body="b") is None


# --- Finding 2: a refused append leaves an audit row ------------------------


def events(store, identity, subject):
    with store.transaction() as cur:
        cur.execute(
            "SELECT event FROM autonomy_events WHERE tenant_id=%s AND owner_id=%s AND subject_id=%s"
            " ORDER BY id",
            (identity.tenant_id, identity.owner_id, subject),
        )
        return [row["event"] for row in cur.fetchall()]


def test_a_refused_append_leaves_a_durable_audit_row(store, identity):
    _, op = purchase(store, identity)
    journal = PaymentJournal(store)
    journal.append(identity, op, charge())
    with pytest.raises(ValueError, match="payment_event_conflict"):
        journal.append(identity, op, charge(amount=500))
    assert "payment_evidence_refused:payment_event_conflict" in events(store, identity, op)


def test_a_refusal_row_never_carries_the_issuer_reference(store, identity):
    _, op = purchase(store, identity)
    journal = PaymentJournal(store)
    journal.append(identity, op, charge())
    with pytest.raises(ValueError):
        journal.append(identity, op, charge(amount=500))
    assert all("issuer-private-reference" not in event for event in events(store, identity, op))


def test_a_refusal_from_a_foreign_scope_writes_nothing(store, identity):
    _, op = purchase(store, identity)
    other = identity.model_copy(update={"owner_id": "bob"})
    with pytest.raises(PermissionError):
        PaymentJournal(store).append(other, op, charge(key="probe"))
    assert events(store, other, op) == []


def test_an_unstarted_payment_refusal_is_still_recorded(store, identity):
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal())
    with pytest.raises(PermissionError, match="payment_submission_not_started"):
        PaymentJournal(store).append(identity, op["id"], charge())
    assert "payment_evidence_refused:payment_submission_not_started" in events(
        store, identity, op["id"]
    )


# --- Finding 3: renewals escape the cap -------------------------------------


def test_a_renewal_above_its_period_allowance_alerts_and_freezes(store, identity, alerted):
    due = (datetime.now(UTC) + timedelta(days=40)).date()
    grant, op = membership(store, identity, due)
    result = PaymentJournal(store).append(
        identity,
        op,
        PaymentFact(
            event_key="renewal-charge",
            kind="charged",
            amount_minor=240000,
            currency="USD",
            source="issuer",
            renewal_id="private-invoice-canary",
            renewal_on=due,
        ),
    )
    assert result["renewals"][0]["period_limit_exceeded"]
    assert alerted, "a renewal 400x over its allowance notified nobody"
    assert "renewal" in alerted[0]["body"]
    assert "private-invoice-canary" not in str(alerted)
    with pytest.raises(PermissionError, match="grant_payment_hold"):
        store.reserve(identity, grant, "main", renewal_proposal(due))


def test_revoking_a_grant_is_documented_as_not_cancelling_a_renewal(store, identity):
    """Finding 3(b): the operator must be told to cancel with the merchant."""
    doc = Path("docs/AUTONOMOUS_EXECUTION.md").read_text()
    assert "Revoking a grant does not cancel a scheduled renewal" in doc
    panel = Path("app/src/components/personal-automation-panel.tsx").read_text()
    assert "does not cancel a scheduled renewal" in panel
