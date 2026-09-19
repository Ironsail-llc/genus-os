"""Only freshly fetched and reviewed CRM identities can replace duplicate creation."""

from copy import deepcopy

import pytest

from robothor.operations.store import Conflict
from robothor.sales.tests.test_guards import prepared


class Provider:
    def __init__(self):
        self.org = {"id": 11, "name": "Example Clinic", "is_deleted": False}
        self.person = {
            "id": 22,
            "name": "Alice",
            "org_id": 11,
            "emails": [{"value": "alice@example.com"}],
        }
        self.lead_record = {
            "id": "00000000-0000-4000-8000-000000000001",
            "title": "Existing enquiry",
            "organization_id": 11,
            "person_id": 22,
            "is_deleted": False,
        }
        self.writes = []

    async def identity_scope(self):
        return "https://example-account.pipedrive.com"

    async def organization(self, _id):
        return deepcopy(self.org)

    async def person_record(self, _id):
        return deepcopy(self.person)

    async def lead(self, _id):
        return deepcopy(self.lead_record)


def review(sales, provider):
    from robothor.sales.pipedrive_identity import IdentityReview

    return IdentityReview(sales, provider)


@pytest.mark.asyncio
async def test_reviewed_existing_ids_are_adopted_without_provider_writes(sales):
    p = prepared(sales)
    provider = Provider()
    identities = review(sales, provider)
    packet = await identities.inspect(
        p["id"], organization_id=11, person_ids=[22], lead_id=provider.lead_record["id"]
    )
    assert not sales.get(p["id"])["pipedrive_ids"]
    result = await identities.adopt(
        p["id"],
        packet["id"],
        packet["content_hash"],
        "operator:test",
        "Confirmed the organization and owner relationship",
    )
    assert result == {
        "organization_id": 11,
        "person:alice@example.com": 22,
        "lead_id": provider.lead_record["id"],
    }
    assert provider.writes == []
    assert sales.get(p["id"])["pipedrive_ids"] == result
    assert sales.ops.claim_action() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_org", "wrong_email", "deleted", "wrong_lead"])
async def test_unrelated_or_deleted_provider_records_cannot_be_adopted(sales, fault):
    p = prepared(sales)
    provider = Provider()
    if fault == "wrong_org":
        provider.person["org_id"] = 999
    if fault == "wrong_email":
        provider.person["emails"] = [{"value": "someone-else@example.com"}]
    if fault == "deleted":
        provider.org["is_deleted"] = True
    if fault == "wrong_lead":
        provider.lead_record["organization_id"] = 999
    with pytest.raises(Conflict):
        await review(sales, provider).inspect(
            p["id"], organization_id=11, person_ids=[22], lead_id=provider.lead_record["id"]
        )


@pytest.mark.asyncio
async def test_stale_tampered_or_cross_tenant_review_is_rejected(sales):
    p = prepared(sales)
    identities = review(sales, Provider())
    packet = await identities.inspect(p["id"], organization_id=11, person_ids=[])
    with pytest.raises(Conflict):
        await identities.adopt(
            p["id"], packet["id"], "0" * 64, "operator:test", "Tampered review must fail"
        )
    from robothor.sales.service import Sales

    with pytest.raises(Conflict):
        await review(Sales("other-tenant"), Provider()).adopt(
            p["id"],
            packet["id"],
            packet["content_hash"],
            "operator:test",
            "Cross-tenant review must fail",
        )
    sales.takeover(p["id"], "operator:test")
    with pytest.raises(Conflict):
        await identities.adopt(
            p["id"],
            packet["id"],
            packet["content_hash"],
            "operator:test",
            "Stale ownership review must fail",
        )


@pytest.mark.asyncio
async def test_adoption_rechecks_remote_relationships_after_human_preview(sales):
    p = prepared(sales)
    provider = Provider()
    identities = review(sales, provider)
    packet = await identities.inspect(p["id"], organization_id=11, person_ids=[22])
    provider.person["org_id"] = 999
    with pytest.raises(Conflict):
        await identities.adopt(
            p["id"],
            packet["id"],
            packet["content_hash"],
            "operator:test",
            "The original relationship was reviewed",
        )
    assert not sales.get(p["id"])["pipedrive_ids"]


@pytest.mark.asyncio
async def test_review_reconciles_uncertain_organization_and_completes_without_duplicate_writes(
    sales,
):
    from robothor.operations.effects import Effects, UnresolvedEffect
    from robothor.sales.promotion import PromotionWorker

    p = prepared(sales)

    async def timeout():
        raise TimeoutError()

    with pytest.raises(UnresolvedEffect):
        await Effects(sales.tenant).perform(
            "pipedrive.organization", str(p["id"]), {"name": "Example Clinic"}, timeout
        )
    provider = Provider()
    identities = review(sales, provider)
    packet = await identities.inspect(
        p["id"], organization_id=11, person_ids=[22], lead_id=provider.lead_record["id"]
    )
    await identities.adopt(
        p["id"],
        packet["id"],
        packet["content_hash"],
        "operator:test",
        "Verified the original provider write committed",
    )
    sales.configure({"promotion_enabled": True}, "operator:test")
    assert await PromotionWorker(sales, provider).tick()
    assert sales.get(p["id"])["status"] == "promoted"
    assert provider.writes == []


@pytest.mark.asyncio
async def test_identity_review_expires_without_writing_provider_or_local_binding(sales):
    from datetime import UTC, datetime, timedelta

    from psycopg2.extras import Json

    from robothor.operations.store import digest

    p = prepared(sales)
    identities = review(sales, Provider())
    packet = await identities.inspect(p["id"], organization_id=11, person_ids=[])
    packet["content"]["observed_at"] = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    packet["content_hash"] = digest(packet["content"])
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET result=%s WHERE tenant_id=%s AND id=%s",
            (Json(packet), sales.tenant, packet["id"]),
        )
    with pytest.raises(Conflict, match="expired"):
        await identities.adopt(
            p["id"],
            packet["id"],
            packet["content_hash"],
            "operator:test",
            "An expired review cannot authorize adoption",
        )
