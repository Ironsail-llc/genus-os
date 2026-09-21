"""Read-only Gmail proof used by the shared, individually approved follow-up cadence."""

from datetime import datetime, timedelta

from robothor.operations.store import Conflict, digest
from robothor.sales.gmail import Gmail
from robothor.sales.providers import ProviderError


class GmailCadence:
    def __init__(self, sales, root, effects):
        self.sales = sales
        try:
            self.provider = Gmail(sales.tenant)
            receipt = effects.get((str(root["id"]), "gmail.send"), {}).get("receipt") or {}
            self.thread = receipt.get("thread_id")
            self.provider.raw_id(self.thread)
            if (
                receipt.get("mailbox") != self.provider.mailbox
                or root["payload"]["sender"] != self.provider.mailbox
            ):
                raise ProviderError("Gmail account changed")
        except ProviderError:
            raise Conflict("Follow-up requires an owned Gmail account and thread") from None

    def validate(self, action, message, effects):
        effect = effects.get((str(action["id"]), "gmail.send"), {})
        receipt = effect.get("receipt") or {}
        try:
            self.provider.raw_id(message["provider_id"])
        except ProviderError:
            raise Conflict("Follow-up Gmail message belongs to another account") from None
        if (
            effect.get("status") != "completed"
            or effect.get("payload_hash") != digest(action["payload"])
            or receipt.get("id") != message["provider_id"]
            or receipt.get("mailbox") != self.provider.mailbox
            or receipt.get("thread_id") != self.thread
            or message["data"].get("thread_id") != self.thread
            or (action["receipt"] or {}).get("delivery_failure")
        ):
            raise Conflict("Follow-up requires an intact Gmail send and quiet owned thread")

    def coverage(self, cur, now):
        cur.execute(
            "SELECT detail FROM operation_audit WHERE tenant_id=%s AND event='gmail.thread_observed' "
            "AND detail->>'thread_id'=%s AND detail->>'mailbox'=%s ORDER BY created_at DESC,id DESC LIMIT 1",
            (self.sales.tenant, self.thread, self.provider.mailbox),
        )
        row = cur.fetchone()
        thread = row["detail"] if row else {}
        cur.execute(
            "SELECT status,result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.gmail_bounces' "
            "AND payload->>'mailbox'=%s ORDER BY created_at DESC,id DESC LIMIT 1",
            (self.sales.tenant, self.provider.mailbox),
        )
        scan = cur.fetchone()
        report = (scan["result"] or {}) if scan else {}
        try:
            fresh = (
                thread.get("complete") is True
                and now - timedelta(minutes=2)
                <= datetime.fromisoformat(thread["observed_at"])
                <= now
                and scan
                and scan["status"] == "completed"
                and report.get("final") is True
                and now - timedelta(minutes=2) <= datetime.fromisoformat(report["through"]) <= now
                and now - timedelta(days=1) <= datetime.fromisoformat(report["full_at"]) <= now
            )
        except (ValueError, TypeError, KeyError):
            fresh = False
        if not fresh:
            raise Conflict(
                "Follow-up requires fresh complete Gmail thread and delivery-report scans"
            )
        return {"provider": "gmail", "thread_id": self.thread, "workspace": self.provider.mailbox}
