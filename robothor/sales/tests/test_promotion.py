"""The reviewed prospect is the boundary between Genus and external CRM."""

import pytest

from robothor.sales.promotion import PromotionWorker
from robothor.sales.tests.test_service import researched


class PipedriveStub:
    def __init__(self):
        self.created = []
        self.matches = []
        self.fail_lead = False

    async def identity_scope(self):
        return "https://example-account.pipedrive.com"

    async def search_organizations(self, name):
        return {"items": self.matches}

    async def create_organization(self, name):
        self.created.append("organization")
        return {"id": 11}

    async def create_lead(self, title, organization_id, person_id=None):
        self.created.append("lead")
        if self.fail_lead:
            raise TimeoutError()
        return {"id": "lead-1"}


@pytest.mark.asyncio
async def test_accepted_prospect_promotes_once_and_records_receipts(sales):
    p = researched(sales)
    provider = PipedriveStub()
    worker = PromotionWorker(sales, provider)
    sales.configure({"promotion_enabled": True}, "operator:test")
    assert await worker.tick() is False
    sales.accept(p["id"], True, "operator:test", expected_version=1, expected_policy_version="1")
    assert await worker.tick() is True
    assert sales.get(p["id"])["pipedrive_ids"] == {"organization_id": 11, "lead_id": "lead-1"}
    assert sales.get(p["id"])["status"] == "promoted"
    assert provider.created == ["organization", "lead"]
    assert await worker.tick() is False


@pytest.mark.asyncio
async def test_pause_and_possible_existing_identity_prevent_writes(sales):
    p = researched(sales)
    sales.accept(p["id"], True, "operator:test", expected_version=1, expected_policy_version="1")
    provider = PipedriveStub()
    worker = PromotionWorker(sales, provider)
    assert await worker.tick() is False
    sales.configure({"promotion_enabled": True}, "operator:test")
    provider.matches = [{"item": {"id": 1}}]
    assert await worker.tick() is True
    assert provider.created == []
    assert sales.get(p["id"])["pipedrive_ids"] == {}


@pytest.mark.asyncio
async def test_partial_promotion_preserves_created_organization_on_timeout(sales):
    p = researched(sales)
    sales.accept(p["id"], True, "operator:test", expected_version=1, expected_policy_version="1")
    sales.configure({"promotion_enabled": True}, "operator:test")
    provider = PipedriveStub()
    provider.fail_lead = True
    worker = PromotionWorker(sales, provider)
    assert await worker.tick() is True
    assert provider.created == ["organization", "lead"]
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET available_at=now() WHERE tenant_id=%s AND kind='sales.promote'",
            (sales.tenant,),
        )
    assert await worker.tick() is True
    assert provider.created == ["organization", "lead"]


@pytest.mark.asyncio
async def test_identity_conflict_does_not_create_an_unknown_write_receipt(sales):
    p = researched(sales)
    sales.accept(p["id"], True, "operator:test", expected_version=1, expected_policy_version="1")
    sales.configure({"promotion_enabled": True}, "operator:test")
    provider = PipedriveStub()
    provider.matches = [{"item": {"id": 1}}]
    await PromotionWorker(sales, provider).tick()
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM operation_effects WHERE tenant_id=%s", (sales.tenant,)
        )
        assert cur.fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_stop_during_identity_search_prevents_the_following_create(sales):
    p = researched(sales)
    sales.accept(p["id"], True, "operator:test", expected_version=1, expected_policy_version="1")
    sales.configure({"promotion_enabled": True}, "operator:test")
    provider = PipedriveStub()

    async def stop(_):
        sales.configure({"promotion_enabled": False}, "operator:test")
        return {"items": []}

    provider.search_organizations = stop
    await PromotionWorker(sales, provider).tick()
    assert provider.created == []


@pytest.mark.asyncio
async def test_restarted_promotion_cannot_reuse_ids_in_a_different_provider_account(sales):
    p = researched(sales)
    sales.accept(p["id"], True, "operator:test", expected_version=1, expected_policy_version="1")
    sales.configure({"promotion_enabled": True}, "operator:test")
    provider = PipedriveStub()
    provider.fail_lead = True

    async def scope():
        return "https://first-account.pipedrive.com"

    provider.identity_scope = scope
    await PromotionWorker(sales, provider).tick()
    provider.created.clear()
    provider.fail_lead = False

    async def changed_scope():
        return "https://second-account.pipedrive.com"

    provider.identity_scope = changed_scope
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET available_at=now() WHERE tenant_id=%s AND kind='sales.promote'",
            (sales.tenant,),
        )
    await PromotionWorker(sales, provider).tick()
    assert provider.created == []
    assert sales.get(p["id"])["pipedrive_account_scope"] == "https://first-account.pipedrive.com"


@pytest.mark.asyncio
async def test_promotion_does_not_queue_a_duplicate_of_an_existing_manual_draft(sales):
    from robothor.sales.tests.test_guards import draft, prepared

    p = prepared(sales)
    draft(sales, p)
    # Existing reviewed contact is already represented in Pipedrive.
    from psycopg2.extras import Json

    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE sales_prospects SET pipedrive_ids=%s,pipedrive_account_scope=%s WHERE tenant_id=%s AND id=%s",
            (
                Json({"organization_id": 11, "person:alice@example.com": 22}),
                "https://example-account.pipedrive.com",
                sales.tenant,
                p["id"],
            ),
        )
    sales.configure({"promotion_enabled": True}, "operator:test")
    await PromotionWorker(sales, PipedriveStub()).tick()
    assert sales.ops.claim("sales.draft") is None
