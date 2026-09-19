"""Explicitly excluding email disables provider work without stopping research."""

import pytest

from robothor.sales.models import SalesSettings
from robothor.sales.setup import Setup


def test_excluded_email_has_no_credential_requirement_and_cannot_enable_sending(sales):
    sales.configure({"email_provider": "none"}, "operator:test")
    result = Setup(sales, secret_names=lambda **kw: [], service_get=lambda name: None).snapshot()
    assert all(c["provider"] != "instantly" for c in result["credentials"])
    assert result["readiness"]["email_provider"] == "none"
    assert sales.settings()["research_enabled"]
    with pytest.raises(ValueError):
        SalesSettings(email_provider="none", sending_enabled=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["verify", "delivery", "stop", "inbox", "reconcile", "status"])
async def test_excluded_provider_workflows_do_not_construct_workers(sales, monkeypatch, stage):
    from robothor.sales import queue

    sales.configure(
        {"email_provider": "none", "workflow_bindings": {stage: "test-workflow"}}, "operator:test"
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Excluded provider worker must not run")

    for name in (
        "VerificationWorker",
        "DeliveryWorker",
        "StopWorker",
        "InstantlyInboxWorker",
        "ReconciliationWorker",
        "ProviderStatusWorker",
    ):
        monkeypatch.setattr(queue, name, forbidden)
    result = await queue.QueueDriver(sales).tick(stage, "test-workflow")
    assert result["worked"] is False
    assert result["reason"] == "email_provider_not_configured"


@pytest.mark.asyncio
async def test_gmail_delivery_routes_only_to_gmail_worker(sales, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from robothor.sales import queue

    sales.configure(
        {"email_provider": "gmail", "workflow_bindings": {"delivery": "gmail-delivery"}},
        "operator:test",
    )
    worker = AsyncMock(return_value=True)
    monkeypatch.setattr(queue, "GmailDeliveryWorker", lambda sales: SimpleNamespace(tick=worker))
    monkeypatch.setattr(
        queue, "DeliveryWorker", lambda sales: pytest.fail("Instantly must not run")
    )
    result = await queue.QueueDriver(sales).tick("delivery", "gmail-delivery")
    assert result["worked"] is True
    worker.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["verify"])
async def test_gmail_verification_never_falls_back_to_instantly(sales, monkeypatch, stage):
    from robothor.sales import queue

    sales.configure(
        {"email_provider": "gmail", "workflow_bindings": {stage: "gmail-workflow"}}, "operator:test"
    )
    for name in (
        "VerificationWorker",
        "StopWorker",
        "InstantlyInboxWorker",
        "ReconciliationWorker",
        "ProviderStatusWorker",
    ):
        monkeypatch.setattr(queue, name, lambda sales: pytest.fail("Instantly must not run"))
    result = await queue.QueueDriver(sales).tick(stage, "gmail-workflow")
    assert result == {"stage": stage, "worked": False, "reason": "email_verification_not_provided"}


def test_gmail_setup_does_not_require_instantly_keys(sales):
    sales.configure({"email_provider": "gmail"}, "operator:test")
    result = Setup(sales, secret_names=lambda **kw: [], service_get=lambda name: None).snapshot()
    assert all(c["provider"] != "instantly" for c in result["credentials"])
