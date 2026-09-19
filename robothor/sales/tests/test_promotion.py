"""The reviewed prospect is the boundary between Genus and external CRM."""

import pytest

from robothor.sales.promotion import PromotionWorker
from robothor.sales.tests.test_service import researched


class PipedriveStub:
    def __init__(self):
        self.created = []
        self.matches = []
        self.fail_lead = False

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
