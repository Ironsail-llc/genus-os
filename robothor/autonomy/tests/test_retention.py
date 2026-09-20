"""The private observation archive can be erased by its owner, and it expires.

`grep -rn "DELETE FROM autonomy"` returned nothing. `autonomy_terms_snapshots`
stores the rendered review page — the owner's name, date of birth, address and
the answers they gave a website — and `autonomy_payment_events` stores money
facts. Both were kept forever, both openable with the vault master key, with
no owner-facing delete and no expiry, while the routes offered
`DELETE /resources/{id}` and `DELETE /grants/{id}` and nothing else.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from robothor.autonomy.models import Delegation, WebOperation
from robothor.autonomy.terms_audit import TermsAudit, TermsSnapshot


def operation(store, identity):
    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins=frozenset({"https://club.example"}),
            actions={"application"},
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    return store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://club.example",
            action="application",
            purpose="Requested membership",
            idempotency_key=uuid4().hex,
        ),
    )


def snapshot():
    return TermsSnapshot.model_validate(
        {
            "origin": "https://club.example",
            "phase": "before_input",
            "documents": [
                {
                    "origin": "https://club.example",
                    "text": "Applicant private-canary-dob 1970-01-01, private-canary-street 4.",
                }
            ],
            "coverage": "visible_text_only",
        }
    )


def age(store, record_id, days):
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_terms_snapshots SET created_at=now() - %s * interval '1 day' "
            "WHERE id=%s",
            (days, record_id),
        )


class TestAnOwnerCanErase:
    def test_the_page_text_goes_and_the_audit_fact_stays(self, store, identity):
        audit = TermsAudit(store)
        op = operation(store, identity)
        first = audit.record(identity, op["id"], "main", snapshot())
        second = audit.record(identity, op["id"], "main", snapshot())

        assert audit.forget(identity, op["id"]) == 2

        listed = audit.list(identity, op["id"])
        assert [row["version"] for row in listed] == [1, 2], "the audit trail was destroyed"
        assert all(row["redacted_at"] is not None for row in listed)
        for record in (first, second):
            read = audit.read(identity, op["id"], record["id"])
            assert read["redacted_at"] is not None
            assert read["snapshot"] is None
            assert "private-canary" not in str(read)

    def test_erasing_twice_is_not_an_error_and_does_not_re_stamp(self, store, identity):
        audit = TermsAudit(store)
        op = operation(store, identity)
        audit.record(identity, op["id"], "main", snapshot())
        audit.forget(identity, op["id"])
        stamped = audit.list(identity, op["id"])[0]["redacted_at"]

        assert audit.forget(identity, op["id"]) == 0
        assert audit.list(identity, op["id"])[0]["redacted_at"] == stamped

    def test_another_owner_cannot_erase_this_one(self, store, identity):
        from robothor.autonomy.models import Scope

        audit = TermsAudit(store)
        op = operation(store, identity)
        audit.record(identity, op["id"], "main", snapshot())
        other = Scope(tenant_id=identity.tenant_id, owner_id="bob")

        assert audit.forget(other, op["id"]) == 0
        assert audit.list(identity, op["id"])[0]["redacted_at"] is None


class TestTheArchiveExpires:
    """The sweep is tenant-wide on purpose — it runs as the platform, after
    every owner's own erasure has had its chance — so these assert on THIS
    operation's rows rather than on a database-wide count that other tests in
    the same throwaway database also contribute to."""

    def test_a_snapshot_past_the_window_is_deleted_outright(self, store, identity):
        audit = TermsAudit(store)
        op = operation(store, identity)
        old = audit.record(identity, op["id"], "main", snapshot())
        age(store, old["id"], 400)
        fresh = audit.record(identity, op["id"], "main", snapshot())

        assert store.purge_expired(terms_days=180)["terms_snapshots"] >= 1

        assert [row["id"] for row in audit.list(identity, op["id"])] == [fresh["id"]]

    def test_a_snapshot_inside_the_window_is_left_alone(self, store, identity):
        audit = TermsAudit(store)
        op = operation(store, identity)
        record = audit.record(identity, op["id"], "main", snapshot())
        age(store, record["id"], 179)

        store.purge_expired(terms_days=180)
        assert [row["id"] for row in audit.list(identity, op["id"])] == [record["id"]]

    def test_a_zero_window_keeps_everything(self, store, identity):
        audit = TermsAudit(store)
        op = operation(store, identity)
        record = audit.record(identity, op["id"], "main", snapshot())
        age(store, record["id"], 100_000)

        assert store.purge_expired(terms_days=0)["terms_snapshots"] == 0
        assert audit.list(identity, op["id"])

    def test_the_default_window_comes_from_the_declared_setting(self, store, identity):
        from robothor.settings import get_settings

        declared = get_settings().autonomy.terms_retention_days
        assert declared > 0
        audit = TermsAudit(store)
        op = operation(store, identity)
        record = audit.record(identity, op["id"], "main", snapshot())
        age(store, record["id"], declared + 1)

        store.purge_expired()
        assert audit.list(identity, op["id"]) == []
