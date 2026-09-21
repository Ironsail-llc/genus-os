"""Only authenticated provider results establish contact deliverability."""

import pytest

from robothor.sales.tests.test_service import researched
from robothor.sales.verification import VerificationWorker


class Verifier:
    def __init__(self, result):
        self.result = result
        self.starts = 0

    async def start_verification(self, email):
        self.starts += 1
        return self.result

    async def verification(self, email):
        return self.result


def setup(sales):
    p = researched(sales)
    data = {
        "name": "Alice",
        "role": "Owner",
        "email": "alice@example.com",
        "source_url": "https://clinic.example.com/team",
    }
    cid = sales.add_contact(p["id"], data)
    job = sales.ops.enqueue(
        "sales.verify", cid, {"prospect_id": p["id"], "contact_id": cid, "email": data["email"]}
    )
    sales.configure(
        {
            "enrichment_enabled": True,
            "verification_allowance_units": 10000,
            "monthly_limit_units": 50_000_000,
            "daily_limit_units": 10_000_000,
        },
        "operator:test",
    )
    return p, job


@pytest.mark.asyncio
async def test_verified_non_catch_all_address_is_recorded_with_timestamp(sales):
    p, job = setup(sales)
    provider = Verifier(
        {"email": "alice@example.com", "verification_status": "verified", "catch_all": False}
    )
    assert await VerificationWorker(sales, provider).tick()
    contact = sales.contacts(p["id"])[0]["data"]
    assert contact["verification"] == "valid"
    assert contact["verified_at"]
    assert sales.ops.get_job(job)["status"] == "completed"
    assert provider.starts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "catch_all,status,expected",
    [
        (True, "verified", "accept_all"),
        (False, "invalid", "invalid"),
        ("pending", "verified", "unknown"),
    ],
)
async def test_unsafe_or_incomplete_results_never_become_valid(sales, catch_all, status, expected):
    p, _ = setup(sales)
    provider = Verifier(
        {"email": "alice@example.com", "verification_status": status, "catch_all": catch_all}
    )
    await VerificationWorker(sales, provider).tick()
    assert sales.contacts(p["id"])[0]["data"]["verification"] == expected


@pytest.mark.asyncio
async def test_pending_verification_polls_without_purchasing_again(sales):
    p, job = setup(sales)
    provider = Verifier({"email": "alice@example.com", "verification_status": "pending"})
    worker = VerificationWorker(sales, provider)
    await worker.tick()
    provider.result = {
        "email": "alice@example.com",
        "verification_status": "verified",
        "catch_all": False,
    }
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET available_at=now() WHERE tenant_id=%s AND id=%s",
            (sales.tenant, job),
        )
    await worker.tick()
    assert provider.starts == 1
    assert sales.contacts(p["id"])[0]["data"]["verification"] == "valid"


@pytest.mark.asyncio
async def test_no_verification_allowance_means_no_purchase(sales):
    p, _ = setup(sales)
    sales.configure({"verification_allowance_units": 0}, "operator:test")
    provider = Verifier({})
    await VerificationWorker(sales, provider).tick()
    assert provider.starts == 0
    assert sales.contacts(p["id"])[0]["data"]["verification"] == "unknown"


@pytest.mark.asyncio
async def test_unknown_purchase_retains_budget_and_cannot_repeat_after_restart(sales):
    from robothor.sales.providers import UnknownEffect

    p, job = setup(sales)

    class TimeoutVerifier(Verifier):
        async def start_verification(self, email):
            self.starts += 1
            raise UnknownEffect("Request timed out")

    provider = TimeoutVerifier({})
    await VerificationWorker(sales, provider).tick()
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET available_at=now() WHERE tenant_id=%s AND id=%s",
            (sales.tenant, job),
        )
    await VerificationWorker(sales, provider).tick()
    assert provider.starts == 1
    assert sales.contacts(p["id"])[0]["data"]["verification"] == "unknown"
    assert all(b["reserved_units"] == 10000 for b in sales.overview()["budgets"])


@pytest.mark.asyncio
async def test_provider_result_for_another_address_cannot_verify_the_contact(sales):
    p, _ = setup(sales)
    provider = Verifier(
        {"email": "different@example.com", "verification_status": "verified", "catch_all": False}
    )
    await VerificationWorker(sales, provider).tick()
    assert sales.contacts(p["id"])[0]["data"]["verification"] == "unknown"
