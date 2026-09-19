"""Canonical provider status reads recover missed negative and account events."""

from datetime import UTC, datetime

import pytest

from robothor.operations.effects import Effects
from robothor.sales.tests.test_guards import draft, prepared


class Provider:
    def __init__(self, status=-2):
        self.status = status
        self.account_status = 1
        self.reads = []

    async def secret(self, key):
        return "workspace-1"

    async def lead(self, lead_id):
        self.reads.append("lead")
        return {
            "id": lead_id,
            "email": "alice@example.com",
            "campaign": "campaign-1",
            "organization": "workspace-1",
            "status": self.status,
            "lt_interest_status": None,
        }

    async def account(self, email):
        self.reads.append("account")
        return {
            "email": email,
            "status": self.account_status,
            "warmup_status": 1,
            "setup_pending": False,
            "stat_warmup_score": 98,
            "timestamp_warmup_start": "2026-01-01T00:00:00Z",
            "smtp_password": "must-not-be-stored",
        }


async def owned_campaign(sales, p):
    action = draft(sales, p)

    async def campaign():
        return {"id": "campaign-1"}

    async def lead():
        return {"id": "lead-1"}

    await Effects(sales.tenant).perform("instantly.campaign", action, {"fixture": True}, campaign)
    await Effects(sales.tenant).perform(
        "instantly.lead", action, {"campaign_id": "campaign-1", "email": "alice@example.com"}, lead
    )
    return action


@pytest.mark.asyncio
async def test_missed_unsubscribe_is_recovered_and_cancels_all_pending_outreach(sales):
    from robothor.sales.provider_status import ProviderStatusWorker

    p = prepared(sales)
    await owned_campaign(sales, p)
    provider = Provider(-2)
    worker = ProviderStatusWorker(sales, provider)
    for _ in range(3):
        await worker.tick()
    assert "lead" in provider.reads
    with sales.ops.transaction() as cur:
        assert sales._suppressed("alice@example.com", cur)
    assert all(a["status"] == "cancelled" for a in sales.overview()["actions"])
    assert sales.ops.claim_action() is None


@pytest.mark.asyncio
async def test_account_failure_revokes_readiness_and_invalidates_stale_setup_review(sales):
    from datetime import timedelta

    from robothor.operations.store import Conflict
    from robothor.sales.provider_status import ProviderStatusWorker

    prepared(sales)
    sales.configure(
        {
            "mailbox_approved_until": {
                "sales@example.com": (datetime.now(UTC) + timedelta(days=1)).isoformat()
            }
        },
        "operator:test",
    )
    before = sales.settings_snapshot()
    provider = Provider()
    provider.account_status = -1
    await ProviderStatusWorker(sales, provider).tick()
    assert "sales@example.com" not in sales.settings()["mailbox_approved_until"]
    with pytest.raises(Conflict):
        sales.configure(
            {"mailbox_approved_until": before["config"]["mailbox_approved_until"]},
            "operator:test",
            expected_revision=before["revision"],
            reason="Stale readiness must not overwrite an account failure",
        )
    assert "must-not-be-stored" not in str(sales.overview())


@pytest.mark.asyncio
async def test_inactive_empty_configuration_makes_no_provider_calls(sales):
    from robothor.sales.provider_status import ProviderStatusWorker

    provider = Provider()
    assert not await ProviderStatusWorker(sales, provider).tick()
    assert provider.reads == []


@pytest.mark.asyncio
async def test_status_scope_mismatch_is_held_without_using_foreign_negative_evidence(sales):
    from robothor.sales.provider_status import ProviderStatusWorker

    p = prepared(sales)
    await owned_campaign(sales, p)
    provider = Provider()
    original = provider.lead

    async def foreign(lead_id):
        return (await original(lead_id)) | {"organization": "other-workspace"}

    provider.lead = foreign
    for _ in range(3):
        await ProviderStatusWorker(sales, provider).tick()
    with sales.ops.transaction() as cur:
        assert not sales._suppressed("alice@example.com", cur)
    assert any(
        j["kind"] == "sales.provider_status" and j["error"] for j in sales.overview()["jobs"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [2, -3, 999])
async def test_paused_or_unknown_status_never_permits_delivery(sales, code):
    from robothor.operations.store import Conflict
    from robothor.sales.provider_status import ProviderStatusWorker

    p = prepared(sales)
    action_id = await owned_campaign(sales, p)
    action = next(a for a in sales.overview()["actions"] if str(a["id"]) == str(action_id))
    with pytest.raises(Conflict):
        await ProviderStatusWorker(sales, Provider(code)).check_before_send(action)
    assert sales.get(p["id"])["owner"] == ("agent" if code == 999 else "human_review")


@pytest.mark.asyncio
async def test_replaced_read_lease_cannot_commit_or_break_the_workflow(sales):
    from robothor.sales.provider_status import ProviderStatusWorker

    p = prepared(sales)
    await owned_campaign(sales, p)
    provider = Provider(-2)
    original = provider.lead

    async def replace_lease(lead_id):
        with sales.ops.transaction() as cur:
            cur.execute(
                "UPDATE operation_jobs SET lease_until=now()-interval '1 second' WHERE tenant_id=%s AND kind='sales.provider_status' AND status='running'",
                (sales.tenant,),
            )
        return await original(lead_id)

    provider.lead = replace_lease
    worker = ProviderStatusWorker(sales, provider)
    for _ in range(3):
        await worker.tick()
    with sales.ops.transaction() as cur:
        assert not sales._suppressed("alice@example.com", cur)
