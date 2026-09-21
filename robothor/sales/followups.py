"""Deterministic, opt-in cold follow-ups. This module never grants send authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from robothor.operations.store import Conflict, digest
from robothor.sales.models import Contact, Dossier, SalesSettings


def due_at(sent: datetime, days: int, timezone: str) -> datetime:
    local = sent.astimezone(ZoneInfo(timezone))
    while days:
        local += timedelta(days=1)
        if local.weekday() < 5:
            days -= 1
    return local.astimezone(UTC)


class Followups:
    def __init__(self, sales):
        self.sales = sales

    def basis(self, prospect_id, *, cur=None, now=None, exclude_action=None):
        """Recompute authority from owned receipts, never from model output.

        The settings and prospect locks serialize planners with state changes.
        Only the current leased action may be excluded during send validation.
        """
        if cur is None:
            with self.sales.ops.transaction() as cursor:
                return self.basis(prospect_id, cur=cursor, now=now, exclude_action=exclude_action)
        now = now or datetime.now(UTC)
        tenant = self.sales.tenant
        cur.execute("SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE", (tenant,))
        row = cur.fetchone()
        settings = SalesSettings.model_validate(row["config"] if row else {})
        if (
            not settings.research_enabled
            or not settings.sending_enabled
            or not settings.followup_delays_business_days
        ):
            raise Conflict("Follow-up cadence is disabled")
        p = self.sales.require(prospect_id, cur)
        if (
            p["status"] not in {"accepted", "promoted"}
            or p["owner"] != "agent"
            or p["outcome_version"]
        ):
            raise Conflict("Follow-up requires an accepted prospect without customer activity")
        self.sales.require_assessment(prospect_id, cur=cur)
        q: dict[str, Any] = p["qualification"] or {}
        if q.get("decision") != "qualified" or settings.active_policy_versions.get(
            q.get("buying_case") or ""
        ) != q.get("policy_version"):
            raise Conflict("Follow-up qualification is stale")
        cur.execute(
            "SELECT * FROM sales_messages WHERE tenant_id=%s AND prospect_id=%s ORDER BY occurred_at,provider_id",
            (tenant, prospect_id),
        )
        messages = list(cur.fetchall())
        if not messages or any(m["direction"] != "outbound" for m in messages):
            raise Conflict("Follow-up requires a quiet confirmed outbound conversation")
        cur.execute(
            "SELECT * FROM operation_actions WHERE tenant_id=%s AND kind='sales.email' "
            "AND payload->>'prospect_id'=%s ORDER BY created_at,id",
            (tenant, str(prospect_id)),
        )
        actions = [dict(a) for a in cur.fetchall() if str(a["id"]) != str(exclude_action)]
        # Cancelled/rejected/unknown work ends this cold sequence. No silent retry.
        if len(actions) != len(messages) or any(a["status"] != "completed" for a in actions):
            raise Conflict("Follow-up has outstanding or interrupted email work")
        roots = [
            a
            for a in actions
            if a["payload"].get("purpose", "initial") == "initial"
            and not a["payload"].get("reply_to_uuid")
        ]
        if len(roots) != 1:
            raise Conflict("Follow-up requires one unambiguous initial campaign")
        root = roots[0]
        payload = root["payload"]
        cur.execute(
            "SELECT kind,dedup_key,status,receipt,payload_hash FROM operation_effects WHERE tenant_id=%s "
            "AND dedup_key=ANY(%s)",
            (tenant, [str(a["id"]) for a in actions]),
        )
        effects = {(e["dedup_key"], e["kind"]): dict(e) for e in cur.fetchall()}
        if any(e["status"] != "completed" for e in effects.values()):
            raise Conflict("Follow-up has unresolved provider effects")
        gmail = None
        if settings.email_provider == "gmail":
            from robothor.sales.gmail_cadence import GmailCadence

            gmail = GmailCadence(self.sales, root, effects)
            campaign = None
        elif settings.email_provider == "instantly":
            campaign = (
                effects.get((str(root["id"]), "instantly.campaign"), {})
                .get("receipt", {})
                .get("id")
            )
            activation = effects.get((str(root["id"]), "instantly.activate"))
            if (
                not campaign
                or not activation
                or activation["payload_hash"] != digest({"campaign_id": campaign})
            ):
                raise Conflict("Follow-up requires an owned activated campaign")
        else:
            raise Conflict("Follow-up email provider is not configured")
        previous: Any = None
        for index, message in enumerate(messages):
            matches = [
                a
                for a in actions
                if (a["receipt"] or {}).get("provider_message_id") == message["provider_id"]
            ]
            if len(matches) != 1:
                raise Conflict("Follow-up send lacks a unique canonical receipt")
            a = matches[0]
            draft = a["payload"]
            data = message["data"]
            if (
                a["approved_hash"] != digest(draft)
                or a["payload_hash"] != digest(draft)
                or not (a["decided_by"] or "").startswith("operator:")
                or a["receipt"].get("delivery_status")
                != ("sent_copy_verified" if gmail else "sent")
                or message["occurred_at"] > now
                or any(
                    data.get(k) != draft.get(k)
                    for k in ("sender", "recipient", "subject", "body", "prospect_id")
                )
                or data.get("campaign_id") != campaign
                or any(draft.get(k) != payload.get(k) for k in ("sender", "recipient"))
            ):
                raise Conflict("Follow-up canonical message does not match its approved send")
            if gmail:
                gmail.validate(a, message, effects)
            if index == 0:
                if a["id"] != root["id"]:
                    raise Conflict("Follow-up root is not the first confirmed send")
            else:
                effect = effects.get(
                    (str(a["id"]), "gmail.send" if gmail else "instantly.reply"), {}
                )
                basis = draft.get("followup_basis", {})
                if (
                    draft.get("purpose") != "followup"
                    or draft.get("reply_to_uuid") != previous["provider_id"]
                    or basis.get("root_action_id") != str(root["id"])
                    or basis.get("ordinal") != index
                    or effect.get("payload_hash") != digest(draft)
                    or effect.get("receipt", {}).get("id") != message["provider_id"]
                    or message["occurred_at"] <= previous["occurred_at"]
                    or (
                        data.get("thread_id")
                        and previous["data"].get("thread_id")
                        and data["thread_id"] != previous["data"]["thread_id"]
                    )
                ):
                    raise Conflict("Follow-up chain identity is inconsistent")
            previous = message
        ordinal = len(messages)
        if ordinal > len(settings.followup_delays_business_days):
            raise Conflict("Follow-up cadence is exhausted")
        due = due_at(
            previous["occurred_at"],
            settings.followup_delays_business_days[ordinal - 1],
            settings.timezone,
        )
        if now < due:
            raise Conflict("Follow-up is not due from the preceding confirmed send")
        if (
            payload["sender"] not in settings.senders
            or self.sales._suppressed(payload["recipient"], cur)
            or payload["dossier_version"] != p["version"]
            or payload.get("library_revision", 0) != settings.library_revision
            or payload["knowledge_version"] != settings.active_knowledge_version
        ):
            raise Conflict("Follow-up sender, contact, dossier or library changed")
        cur.execute(
            "SELECT data FROM sales_contacts WHERE tenant_id=%s AND prospect_id=%s AND email=%s",
            (tenant, prospect_id, payload["recipient"]),
        )
        row = cur.fetchone()
        contact = Contact.model_validate(row["data"]) if row else None
        if (
            not contact
            or contact.verification != "valid"
            or not contact.verified_at
            or not contact.verified_at.tzinfo
            or not now - timedelta(days=30) <= contact.verified_at <= now
        ):
            raise Conflict("Follow-up requires current contact verification")
        evidence = {e.id: e for e in Dossier.model_validate(p["dossier"]).evidence}
        if any(
            ref not in evidence
            or evidence[ref].confidence != "supported"
            or not now - timedelta(days=90) <= evidence[ref].retrieved_at <= now
            or (evidence[ref].expires_at and evidence[ref].expires_at <= now)
            for ref in payload["evidence_ids"]
        ):
            raise Conflict("Follow-up evidence is stale")
        if gmail:
            result = gmail.coverage(cur, now)
        else:
            # Any pending event for this campaign or sender holds the sequence. A
            # previously received reply ends it even before canonical text is read.
            cur.execute(
                "SELECT 1 FROM operation_inbox WHERE tenant_id=%s AND provider='instantly' "
                "AND (payload->>'campaign_id'=%s OR payload->>'sender'=%s) "
                "AND (processed_at IS NULL OR payload->>'kind' IN "
                "('reply_received','auto_reply_received','lead_unsubscribed','lead_not_interested','lead_wrong_person','email_bounced','account_error')) LIMIT 1",
                (tenant, campaign, payload["sender"]),
            )
            if cur.fetchone():
                raise Conflict("Follow-up provider events require attention")
            cur.execute(
                "SELECT status,result,payload FROM operation_jobs WHERE tenant_id=%s AND kind='sales.reconcile' "
                "AND payload->>'campaign_id'=%s ORDER BY created_at DESC,id DESC LIMIT 1",
                (tenant, campaign),
            )
            scan = cur.fetchone()
            result = scan["result"] if scan else {}
            try:
                fresh = (
                    scan
                    and scan["status"] == "completed"
                    and result.get("final") is True
                    and scan["payload"].get("action_id") == str(root["id"])
                    and result.get("workspace")
                    and now - timedelta(minutes=15)
                    <= datetime.fromisoformat(result["through"])
                    <= now
                    and now - timedelta(days=1) <= datetime.fromisoformat(result["full_at"]) <= now
                )
            except (KeyError, TypeError, ValueError):
                fresh = False
            if not fresh:
                raise Conflict("Follow-up requires fresh completed campaign reconciliation")
        return {
            **({"provider": "gmail", "thread_id": gmail.thread} if gmail else {}),
            "root_action_id": str(root["id"]),
            "campaign_id": campaign,
            "ordinal": ordinal,
            "reply_to_uuid": previous["provider_id"],
            "sender": payload["sender"],
            "recipient": payload["recipient"],
            "due_at": due.isoformat(),
            "workspace": result["workspace"],
            "delays_business_days": settings.followup_delays_business_days,
            "timezone": settings.timezone,
        }

    def plan(self, *, now=None):
        """At most one immutable queue identity per root/ordinal, including failures."""
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
                (self.sales.tenant,),
            )
            row = cur.fetchone()
            settings = SalesSettings.model_validate(row["config"] if row else {})
            if (
                not settings.research_enabled
                or not settings.sending_enabled
                or not settings.followup_delays_business_days
            ):
                return 0
            cur.execute(
                "SELECT id FROM sales_prospects WHERE tenant_id=%s AND owner='agent' "
                "AND status IN ('accepted','promoted') ORDER BY updated_at,id",
                (self.sales.tenant,),
            )
            prospects = [str(p["id"]) for p in cur.fetchall()]
            planned = 0
            for prospect in prospects:
                try:
                    basis = self.basis(prospect, cur=cur, now=now)
                except Conflict:
                    continue
                key = f"followup:{basis['root_action_id']}:{basis['ordinal']}"
                cur.execute(
                    "SELECT 1 FROM operation_jobs WHERE tenant_id=%s AND kind='sales.draft' AND dedup_key=%s",
                    (self.sales.tenant, key),
                )
                if cur.fetchone():
                    continue
                self.sales.ops.enqueue(
                    "sales.draft",
                    key,
                    {"prospect_id": prospect, "purpose": "followup", "followup_basis": basis},
                    cur=cur,
                )
                planned += 1
            return planned
