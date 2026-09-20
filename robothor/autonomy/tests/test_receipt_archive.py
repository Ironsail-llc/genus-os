"""Receipts reuse encrypted observations but require completed payment evidence."""

import pytest

from robothor.autonomy.terms_audit import TermsAudit, TermsSnapshot
from robothor.autonomy.tests.test_payment_journal import purchase


def receipt(**changes):
    return TermsSnapshot.model_validate(
        {
            "origin": "https://shop.example",
            "phase": "after_confirmation",
            "confirmation_sha256": "a" * 64,
            "documents": [{"origin": "https://shop.example", "text": "Private receipt canary"}],
            **changes,
        }
    )


def test_receipt_requires_matching_completed_payment_and_keeps_text_private(store, identity):
    op = purchase(store, identity)
    archive = TermsAudit(store)
    with pytest.raises(PermissionError):
        archive.record(identity, op, "main", receipt())
    store.finish(
        identity,
        op,
        "completed",
        {"origin": "https://shop.example", "confirmation_sha256": "a" * 64},
    )
    with pytest.raises(PermissionError):
        archive.record(identity, op, "main", receipt(confirmation_sha256="b" * 64))
    stored = archive.record(identity, op, "main", receipt())
    assert "Private receipt canary" not in str(stored) + str(archive.list(identity, op))
    assert (
        archive.read(identity, op, stored["id"])["snapshot"]["documents"][0]["text"]
        == "Private receipt canary"
    )
    with pytest.raises(PermissionError):
        archive.read(identity.model_copy(update={"owner_id": "other"}), op, stored["id"])


def test_revocation_after_completion_does_not_discard_receipt(store, identity):
    op = purchase(store, identity)
    store.finish(
        identity,
        op,
        "completed",
        {"origin": "https://shop.example", "confirmation_sha256": "a" * 64},
    )
    store.revoke_grant(identity, store.operation(identity, op)["grant_id"])
    assert (
        TermsAudit(store).record(identity, op, "main", receipt())["phase"] == "after_confirmation"
    )


def test_withheld_receipt_cannot_contain_page_text_or_links():
    with pytest.raises(ValueError):
        receipt(capture_status="withheld_after_code")
    value = receipt(
        capture_status="withheld_after_code",
        documents=[{"origin": "https://shop.example", "text": ""}],
    )
    assert value.capture_status == "withheld_after_code"


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "https://other.example"},
        {"documents": [{"origin": "https://other.example", "text": "Foreign page"}]},
    ],
)
def test_receipt_cannot_bind_foreign_page_to_payment(store, identity, changes):
    op = purchase(store, identity)
    store.finish(
        identity,
        op,
        "completed",
        {"origin": "https://shop.example", "confirmation_sha256": "a" * 64},
    )
    with pytest.raises(PermissionError):
        TermsAudit(store).record(identity, op, "main", receipt(**changes))


def test_receipt_is_encrypted_and_withheld_status_survives_read(store, identity):
    op = purchase(store, identity)
    store.finish(
        identity,
        op,
        "completed",
        {"origin": "https://shop.example", "confirmation_sha256": "a" * 64},
    )
    archive = TermsAudit(store)
    first = archive.record(identity, op, "main", receipt())
    with store.transaction() as cur:
        cur.execute(
            "SELECT encrypted_value FROM autonomy_terms_snapshots WHERE id=%s", (first["id"],)
        )
        assert b"Private receipt canary" not in bytes(cur.fetchone()["encrypted_value"])
    withheld = archive.record(
        identity,
        op,
        "main",
        receipt(
            capture_status="withheld_after_code",
            documents=[{"origin": "https://shop.example", "text": ""}],
        ),
    )
    assert (
        archive.read(identity, op, withheld["id"])["snapshot"]["capture_status"]
        == "withheld_after_code"
    )
