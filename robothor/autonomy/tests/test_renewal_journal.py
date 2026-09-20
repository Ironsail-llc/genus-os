"""Encrypted renewal grouping survives replay, revocation and process restart."""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from robothor.autonomy.models import Recurrence, WebOperation
from robothor.autonomy.payment_journal import PaymentJournal
from robothor.autonomy.payment_lifecycle import PaymentFact
from robothor.autonomy.tests.test_store import policy


def test_durable_renewal_evidence_keeps_initial_payment_and_budget_separate(store, identity):
    due = (datetime.now(UTC) + timedelta(days=40)).date()
    grant = store.create_grant(
        identity,
        policy().model_copy(
            update={
                "actions": frozenset({"subscription"}),
                "recurring_minor": 600,
                "annual_minor": 7200,
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
            idempotency_key="renewal-test",
            amount_minor=100,
            recurring_minor=600,
            annual_commitment_minor=7200,
            recurrence=Recurrence(interval_months=1, next_charge_on=due),
        ),
    )
    store.begin_submit(identity, op["id"], "main")
    store.finish(identity, op["id"], "completed", {"origin": "https://shop.example"})
    store.revoke_grant(identity, grant["id"])
    budget = store.spending_projection(identity)
    journal = PaymentJournal(store)
    renewal = PaymentFact(
        event_key="renewal-charge",
        kind="charged",
        amount_minor=600,
        currency="USD",
        source="issuer",
        renewal_id="private-invoice-canary",
        renewal_on=due,
    )
    result = journal.append(identity, op["id"], renewal)
    assert journal.append(identity, op["id"], renewal) == result
    assert result["event_count"] == 2 and result["position"]["state"] == "submitted"
    assert result["position"]["charged_minor"] == 0
    assert result["renewals"][0]["position"]["charged_minor"] == 600
    assert not result["reconciliation_required"]
    assert "private-invoice-canary" not in str(result)
    assert store.spending_projection(identity) == budget
    with store.transaction() as cur:
        cur.execute(
            "SELECT encrypted_value FROM autonomy_payment_events WHERE operation_id=%s", (op["id"],)
        )
        assert all(
            b"private-invoice-canary" not in bytes(row["encrypted_value"]) for row in cur.fetchall()
        )
    script = """
import json,os,sys
import psycopg2
from robothor.autonomy.store import AutonomyStore
from robothor.autonomy.models import Scope
from robothor.autonomy.payment_journal import PaymentJournal
args=json.load(sys.stdin)
store=AutonomyStore(lambda: psycopg2.connect(os.environ['AUTONOMY_TEST_DSN']), keys={'v1': b'x'*32}, key_id='v1')
print(json.dumps(PaymentJournal(store).read(Scope.model_validate(args['scope']), args['operation'])))
"""
    fresh = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"scope": identity.model_dump(), "operation": op["id"]}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(fresh.stdout) == result
