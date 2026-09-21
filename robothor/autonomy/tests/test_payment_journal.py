"""Durable payment facts preserve uncertainty, scope and idempotency."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from robothor.autonomy.payment_journal import PaymentJournal
from robothor.autonomy.payment_lifecycle import PaymentFact
from robothor.autonomy.tests.test_store import policy, proposal


def purchase(store, identity):
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal())
    store.begin_submit(identity, op["id"], "main")
    return op["id"]


def charge(key="issuer-private-reference", amount=600, kind="charged"):
    return PaymentFact(
        event_key=key, kind=kind, amount_minor=amount, currency="USD", source="issuer"
    )


def test_facts_survive_new_journal_and_repeated_delivery(store, identity):
    op = purchase(store, identity)
    journal = PaymentJournal(store)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: journal.append(identity, op, charge()), range(2)))
    assert results[0] == results[1]
    result = PaymentJournal(store).read(identity, op)
    assert result["event_count"] == 1 and result["position"]["charged_minor"] == 600
    assert "issuer-private-reference" not in str(result)
    with store.transaction() as cur:
        cur.execute(
            "SELECT encrypted_value FROM autonomy_payment_events WHERE operation_id=%s", (op,)
        )
        assert b"issuer-private-reference" not in bytes(cur.fetchone()["encrypted_value"])


def test_conflict_does_not_overwrite_prior_evidence(store, identity):
    op = purchase(store, identity)
    journal = PaymentJournal(store)
    journal.append(identity, op, charge())
    with pytest.raises(ValueError, match="payment_event_conflict"):
        journal.append(identity, op, charge(amount=500))
    assert journal.read(identity, op)["position"]["charged_minor"] == 600
    for field in ("owner_id", "tenant_id"):
        other = identity.model_copy(update={field: "other"})
        with pytest.raises(PermissionError):
            journal.read(other, op)
        with pytest.raises(PermissionError):
            journal.append(other, op, charge(key="other"))


def test_inconsistent_issuer_evidence_is_preserved_for_reconciliation(store, identity):
    op = purchase(store, identity)
    journal = PaymentJournal(store)
    result = journal.append(identity, op, charge(kind="refunded"))
    assert result["event_count"] == 1 and result["reconciliation_required"]
    assert result["position"] is None


def test_browser_completion_is_submission_not_bank_settlement(store, identity):
    op = purchase(store, identity)
    store.finish(
        identity,
        op,
        "completed",
        {"origin": "https://shop.example", "confirmation_sha256": "a" * 64, "kind": "confirmation"},
    )
    result = PaymentJournal(store).read(identity, op)
    assert result["event_count"] == 1 and result["position"]["state"] == "submitted"
    assert result["position"]["charged_minor"] == 0


def test_revocation_does_not_prevent_recording_existing_charge(store, identity):
    op = purchase(store, identity)
    row = store.operation(identity, op)
    store.revoke_grant(identity, row["grant_id"])
    result = PaymentJournal(store).append(identity, op, charge(amount=700))
    assert result["position"]["charged_minor"] == 700 and result["position"]["limit_exceeded"]


def test_journal_failure_rolls_back_browser_completion(store, identity, monkeypatch):
    op = purchase(store, identity)

    def fail(*args, **kwargs):
        raise RuntimeError("simulated storage outage")

    monkeypatch.setattr(PaymentJournal, "_append", fail)
    with pytest.raises(RuntimeError):
        store.finish(identity, op, "completed", {"origin": "https://shop.example"})
    assert store.operation(identity, op)["state"] == "submitting"


def test_unattempted_payment_cannot_receive_issuer_facts(store, identity):
    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal())
    with pytest.raises(PermissionError, match="payment_submission_not_started"):
        PaymentJournal(store).append(identity, op["id"], charge())


def test_encrypted_fact_cannot_be_moved_to_another_version(store, identity):
    op = purchase(store, identity)
    journal = PaymentJournal(store)
    journal.append(identity, op, charge())
    journal.append(identity, op, charge(key="refund", amount=100, kind="refunded"))
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_payment_events SET encrypted_value=(SELECT encrypted_value FROM autonomy_payment_events WHERE operation_id=%s AND version=1) WHERE operation_id=%s AND version=2",
            (op, op),
        )
    with pytest.raises(PermissionError, match="payment_evidence_unavailable"):
        journal.read(identity, op)


def test_new_process_reads_durable_encrypted_position(store, identity):
    import json
    import subprocess
    import sys

    op = purchase(store, identity)
    PaymentJournal(store).append(identity, op, charge())
    script = """
import json, os, sys
import psycopg2
from robothor.autonomy.store import AutonomyStore
from robothor.autonomy.models import Scope
from robothor.autonomy.payment_journal import PaymentJournal
args=json.load(sys.stdin)
store=AutonomyStore(lambda: psycopg2.connect(os.environ['AUTONOMY_TEST_DSN']), keys={'v1': b'x'*32}, key_id='v1')
print(json.dumps(PaymentJournal(store).read(Scope.model_validate(args['scope']), args['operation'])))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"scope": identity.model_dump(), "operation": op}),
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    result = json.loads(completed.stdout)
    assert result["event_count"] == 1 and result["position"]["charged_minor"] == 600
    assert "issuer-private-reference" not in completed.stdout + completed.stderr


def test_refund_record_does_not_release_spending_reservation(store, identity):
    op = purchase(store, identity)
    before = store.spending_projection(identity)
    journal = PaymentJournal(store)
    journal.append(identity, op, charge())
    journal.append(identity, op, charge(key="refund", kind="refunded"))
    assert journal.read(identity, op)["position"]["state"] == "refunded"
    assert store.spending_projection(identity) == before


def test_recurring_membership_records_initial_submission(store, identity):
    from datetime import UTC, datetime, timedelta

    from robothor.autonomy.models import WebOperation

    grant = store.create_grant(
        identity,
        policy().model_copy(
            update={
                "actions": frozenset({"subscription"}),
                "recurring_minor": 600,
                "annual_minor": 10000,
                "monthly_minor": 2000,
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
            idempotency_key="membership-journal",
            amount_minor=600,
            recurring_minor=600,
            annual_commitment_minor=7800,
            recurrence={
                "interval_months": 1,
                "next_charge_on": (datetime.now(UTC) + timedelta(days=40)).date(),
            },
        ),
    )
    store.begin_submit(identity, op["id"], "main")
    store.finish(identity, op["id"], "completed", {"origin": "https://shop.example"})
    result = PaymentJournal(store).read(identity, op["id"])
    assert result["event_count"] == 1 and result["position"]["state"] == "submitted"


def test_late_charge_resolves_refund_without_rewriting_evidence_or_freeing_budget(store, identity):
    op = purchase(store, identity)
    journal = PaymentJournal(store)
    budget_before = store.spending_projection(identity)
    pending = journal.append(
        identity, op, charge(key="refund-delivered-first", amount=200, kind="refunded")
    )
    assert pending["reconciliation_required"] and pending["position"] is None
    with store.transaction() as cur:
        cur.execute(
            "SELECT id,version,encrypted_value FROM autonomy_payment_events WHERE operation_id=%s ORDER BY version",
            (op,),
        )
        original = [dict(row) for row in cur.fetchall()]
    resolved = journal.append(identity, op, charge(key="capture-delivered-late"))
    assert not resolved["reconciliation_required"]
    assert resolved["position"]["state"] == "partially_refunded"
    assert resolved["position"]["net_charged_minor"] == 400
    assert PaymentJournal(store).read(identity, op) == resolved
    assert store.spending_projection(identity) == budget_before
    with store.transaction() as cur:
        cur.execute(
            "SELECT id,version,encrypted_value FROM autonomy_payment_events WHERE operation_id=%s ORDER BY version",
            (op,),
        )
        final = [dict(row) for row in cur.fetchall()]
    assert final[:1] == original and len(final) == 2
