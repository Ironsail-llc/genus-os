"""Human contact identity review is separate from provider deliverability."""

import pytest

from robothor.operations.store import Conflict
from robothor.sales.tests.test_guards import draft, prepared


def review(sales):
    from robothor.sales.contacts import Contacts

    return Contacts(sales)


def test_reviewed_identity_preserves_email_verification_and_cancels_old_drafts(sales):
    p = prepared(sales)
    action = draft(sales, p)
    contact = review(sales).list(p["id"])[0]
    result = review(sales).review(
        p["id"],
        contact["id"],
        expected_hash=contact["identity_hash"],
        name="Practice team",
        role="General business inbox",
        actor="operator:test",
        reason="The public source does not name an individual",
    )
    assert result["data"]["name"] == "Practice team"
    assert result["data"]["email"] == "alice@example.com"
    assert result["data"]["verification"] == "valid"
    assert (
        next(a for a in sales.overview()["actions"] if str(a["id"]) == str(action))["status"]
        == "cancelled"
    )
    assert result["review"]["actor"] == "operator:test"
    with pytest.raises(Conflict):
        review(sales).review(
            p["id"],
            contact["id"],
            expected_hash=contact["identity_hash"],
            name="Alice",
            role="Owner",
            actor="operator:test",
            reason="Stale identity must not overwrite the review",
        )


def test_manual_contact_entry_cannot_assert_deliverability_or_replace_identity(sales):
    p = prepared(sales)
    contacts = review(sales)
    body = {
        "name": "Bob",
        "role": "Operations",
        "email": "bob@example.com",
        "source_url": "https://clinic.example.com/team",
    }
    result = contacts.add(
        p["id"], body, "operator:test", "Reviewed the public team contact listing"
    )
    assert result["data"]["verification"] == "unknown"
    job = sales.ops.claim("sales.verify")
    assert job["payload"]["email"] == "bob@example.com"
    with pytest.raises(ValueError):
        contacts.add(
            p["id"],
            body | {"verification": "valid"},
            "operator:test",
            "Cannot override the provider result",
        )
    with pytest.raises(Conflict):
        contacts.add(
            p["id"],
            body | {"name": "Other person"},
            "operator:test",
            "Cannot silently overwrite a stored identity",
        )


def test_contact_change_after_claim_invalidates_the_preflight(sales):
    from psycopg2.extras import Json

    p = prepared(sales)
    action_id = draft(sales, p)
    sales.ops.decide(action_id, True, "operator:test")
    action = sales.ops.claim_action(kind="sales.email")
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE sales_contacts SET data=data || %s WHERE tenant_id=%s",
            (Json({"name": "Different person"}), sales.tenant),
        )
    with pytest.raises(Conflict, match="Contact identity"):
        sales.validate_send(action)


def test_later_agent_contact_research_cannot_overwrite_a_reviewed_identity(sales):
    p = prepared(sales)
    contact = review(sales).list(p["id"])[0]
    review(sales).review(
        p["id"],
        contact["id"],
        expected_hash=contact["identity_hash"],
        name="Practice team",
        role="General business inbox",
        actor="operator:test",
        reason="Only a shared business address is supported",
    )
    with pytest.raises(Conflict, match="reviewed identity"):
        sales.add_contact(p["id"], contact["data"])
