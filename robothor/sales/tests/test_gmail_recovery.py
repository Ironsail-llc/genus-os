"""A human reconciles an uncertain send from fresh exact Gmail evidence, never a retry."""

from datetime import UTC, datetime

import pytest

from robothor.operations.store import Conflict
from robothor.sales.providers import ProviderError
from robothor.sales.tests.test_gmail import message
from robothor.sales.tests.test_gmail_delivery import action, gmail_setup, sends
from robothor.sales.tests.test_guards import draft


async def uncertain(sales, monkeypatch):
    p, cli, worker = gmail_setup(sales, monkeypatch)
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    cli.response = {"error": "timeout"}
    await worker.tick()
    assert action(sales, key)["status"] == "unknown"
    data = action(sales, key)["payload"]
    cli.matches = ["abc123"]
    record = message(
        message_id=worker.provider.message_id(key, data),
        sender=data["sender"],
        recipient=data["recipient"],
        subject=data["subject"],
        body=data["body"] + "\n",
        labels=["SENT"],
    )
    record["internalDate"] = str(int(datetime.now(UTC).timestamp() * 1000))
    cli.messages["abc123"] = record
    return p, cli, worker, key


@pytest.mark.asyncio
async def test_inspection_does_not_resolve_unknown_write_and_human_commit_is_atomic(
    sales, monkeypatch
):
    from robothor.sales.gmail_recovery import GmailRecovery

    p, cli, worker, key = await uncertain(sales, monkeypatch)
    recovery = GmailRecovery(sales, worker.provider)
    proof = await recovery.inspect(key, actor="operator:test")
    assert action(sales, key)["status"] == "unknown"
    assert sales.messages(p["id"]) == []
    assert proof["message"]["body"] == action(sales, key)["payload"]["body"]
    result = await GmailRecovery(sales, worker.provider).reconcile(
        key,
        expected_hash=proof["content_hash"],
        reason="Reviewed the exact sent copy and recipient",
        actor="operator:test",
    )
    assert result["delivery_status"] == "sent_copy_verified"
    assert action(sales, key)["status"] == "completed"
    assert len(sales.messages(p["id"])) == 1
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT status,receipt FROM operation_effects WHERE tenant_id=%s AND kind='gmail.send' AND dedup_key=%s",
            (sales.tenant, key),
        )
        effect = cur.fetchone()
        assert effect["status"] == "completed"
        assert effect["receipt"]["id"] == worker.provider.provider_id("abc123")
    assert not await worker.tick()
    assert sends(cli) == 1


@pytest.mark.asyncio
async def test_changed_evidence_requires_new_human_review(sales, monkeypatch):
    from robothor.sales.gmail_recovery import GmailRecovery

    p, cli, worker, key = await uncertain(sales, monkeypatch)
    recovery = GmailRecovery(sales, worker.provider)
    proof = await recovery.inspect(key, actor="operator:test")
    cli.messages["abc123"]["internalDate"] = str(int(cli.messages["abc123"]["internalDate"]) + 1000)
    with pytest.raises(Conflict, match="changed"):
        await recovery.reconcile(
            key,
            expected_hash=proof["content_hash"],
            reason="Reviewed the exact sent copy and recipient",
            actor="operator:test",
        )
    assert action(sales, key)["status"] == "unknown"
    assert sales.messages(p["id"]) == []


@pytest.mark.asyncio
async def test_cross_tenant_and_service_identity_cannot_inspect_or_reconcile(sales, monkeypatch):
    from robothor.sales.gmail_recovery import GmailRecovery
    from robothor.sales.service import Sales

    _, cli, worker, key = await uncertain(sales, monkeypatch)
    before = len(cli.calls)
    with pytest.raises(Conflict):
        await GmailRecovery(sales, worker.provider).inspect(key, actor="service:agent")
    with pytest.raises(Conflict):
        await GmailRecovery(Sales("other-tenant"), worker.provider).inspect(
            key, actor="operator:test"
        )
    assert len(cli.calls) == before


@pytest.mark.asyncio
async def test_missing_sent_copy_never_releases_unknown_action(sales, monkeypatch):
    from robothor.sales.gmail_recovery import GmailRecovery

    p, cli, worker, key = await uncertain(sales, monkeypatch)
    cli.matches = []
    with pytest.raises(ProviderError):
        await GmailRecovery(sales, worker.provider).inspect(key, actor="operator:test")
    assert action(sales, key)["status"] == "unknown"
    assert sends(cli) == 1 and sales.messages(p["id"]) == []


@pytest.mark.asyncio
async def test_recovery_cannot_race_a_live_action_lease(sales, monkeypatch):
    from robothor.sales.gmail_recovery import GmailRecovery

    _, cli, worker, key = await uncertain(sales, monkeypatch)
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_actions SET status='executing',lease_until=now()+interval '2 minutes' WHERE tenant_id=%s AND id=%s",
            (sales.tenant, key),
        )
    before = len(cli.calls)
    with pytest.raises(Conflict, match="flight"):
        await GmailRecovery(sales, worker.provider).inspect(key, actor="operator:test")
    assert len(cli.calls) == before


@pytest.mark.asyncio
async def test_recovery_database_failure_rolls_back_action_effect_and_message(sales, monkeypatch):
    from robothor.sales.gmail_recovery import GmailRecovery

    p, _, worker, key = await uncertain(sales, monkeypatch)
    recovery = GmailRecovery(sales, worker.provider)
    proof = await recovery.inspect(key, actor="operator:test")
    original_audit = sales.ops.audit

    def fail(cur, entity, event, *args, **kwargs):
        if event == "gmail.action_reconciled":
            raise RuntimeError("Simulated audit write failure")
        return original_audit(cur, entity, event, *args, **kwargs)

    monkeypatch.setattr(sales.ops, "audit", fail)
    with pytest.raises(RuntimeError):
        await recovery.reconcile(
            key,
            expected_hash=proof["content_hash"],
            reason="Reviewed the exact sent copy and recipient",
            actor="operator:test",
        )
    assert action(sales, key)["status"] == "unknown"
    assert sales.messages(p["id"]) == []
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT status FROM operation_effects WHERE tenant_id=%s AND kind='gmail.send' AND dedup_key=%s",
            (sales.tenant, key),
        )
        assert cur.fetchone()["status"] == "unknown"
