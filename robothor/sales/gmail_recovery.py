"""Fresh provider evidence plus human review resolves held Gmail writes atomically."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from psycopg2.extras import Json

from robothor.operations.store import Conflict, digest
from robothor.sales.gmail import Gmail
from robothor.sales.gmail_sync import authorized, canonical
from robothor.sales.service import operator


class GmailRecovery:
    def __init__(self, sales, provider=None):
        self.sales = sales
        self.provider = provider

    def _load(self, action_id, *, cur=None):
        if cur is None:
            with self.sales.ops.transaction() as cursor:
                return self._load(action_id, cur=cursor)
        cur.execute(
            "SELECT a.*,e.payload_hash AS effect_hash,e.status AS effect_status,e.receipt AS provider_receipt,e.updated_at AS effect_updated_at "
            "FROM operation_actions a JOIN operation_effects e ON e.tenant_id=a.tenant_id AND e.dedup_key=a.id::text AND e.kind='gmail.send' "
            "WHERE a.tenant_id=%s AND a.id=%s AND a.kind='sales.email' FOR UPDATE OF a,e",
            (self.sales.tenant, str(action_id)),
        )
        row = cur.fetchone()
        if not row:
            raise Conflict("Gmail send effect not found in this tenant")
        action = dict(row)
        authorized(action)
        now = datetime.now(UTC)
        if (
            action["status"] == "executing"
            and action["lease_until"]
            and action["lease_until"] > now
        ) or (
            action["effect_status"] == "executing"
            and action["effect_updated_at"] > now - timedelta(minutes=2)
        ):
            raise Conflict("Gmail write may still be in flight")
        if action["status"] not in {"executing", "unknown", "completed"}:
            raise Conflict("Only a held or acknowledged Gmail send can be reconciled")
        return action

    @staticmethod
    def _state(action):
        return digest(
            {
                "id": str(action["id"]),
                "payload_hash": action["payload_hash"],
                "approved_hash": action["approved_hash"],
                "status": action["status"],
                "receipt": action["receipt"],
                "effect_status": action["effect_status"],
                "provider_receipt": action["provider_receipt"],
            }
        )

    async def inspect(self, action_id, *, actor):
        operator(actor)
        action = await asyncio.to_thread(self._load, action_id)
        provider = self.provider or Gmail(self.sales.tenant)
        evidence = await provider.find_sent(str(action_id), action["payload"], include_record=True)
        receipt = evidence["receipt"]
        if (
            action["effect_status"] == "completed"
            and (action["provider_receipt"] or {}).get("id") != receipt["id"]
        ):
            raise Conflict("Gmail evidence conflicts with a completed effect")
        event = canonical(
            provider, {**action, "provider_receipt": receipt}, evidence["record"], [action]
        )
        if event["action_id"] != str(action_id):
            raise Conflict("Gmail recovery lacks the exact approved message")
        failure = (action["receipt"] or {}).get("delivery_failure")
        if failure and failure.get("action") in {"failed", "delayed"}:
            receipt = {
                **receipt,
                "delivery_failure": failure,
                "delivery_status": "bounced"
                if failure["action"] == "failed"
                else "delivery_delayed",
            }
        content = {
            "action_id": str(action_id),
            "state_hash": self._state(action),
            "receipt": {**receipt, "provider_message_id": receipt["id"]},
            "message": event["message"],
        }
        return {**content, "content_hash": digest(content)}

    async def reconcile(self, action_id, *, expected_hash, reason, actor):
        operator(actor)
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 2000:
            raise ValueError("An explicit recovery review reason is required")
        proof = await self.inspect(action_id, actor=actor)
        if proof["content_hash"] != expected_hash:
            raise Conflict("Gmail evidence changed; inspect and review it again")
        return await asyncio.to_thread(self._commit, proof, actor, reason.strip())

    def _commit(self, proof, actor, reason):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
                (self.sales.tenant,),
            )
            action = self._load(proof["action_id"], cur=cur)
            if self._state(action) != proof["state_hash"]:
                raise Conflict("Gmail action changed during evidence review")
            self.sales.record_message(proof["message"], cur=cur)
            cur.execute(
                "UPDATE operation_effects SET status='completed',receipt=%s,updated_at=now() WHERE tenant_id=%s AND kind='gmail.send' AND dedup_key=%s",
                (Json(proof["receipt"]), self.sales.tenant, proof["action_id"]),
            )
            cur.execute(
                "UPDATE operation_actions SET status='completed',receipt=%s,lease_token=NULL,lease_until=NULL WHERE tenant_id=%s AND id=%s",
                (Json(proof["receipt"]), self.sales.tenant, proof["action_id"]),
            )
            self.sales.ops.audit(
                cur,
                proof["action_id"],
                "gmail.action_reconciled",
                actor,
                {
                    "reason": reason,
                    "proof_hash": proof["content_hash"],
                    "provider_message_id": proof["receipt"]["id"],
                },
            )
            return proof["receipt"]
