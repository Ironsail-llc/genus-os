"""Provider-specific verification and normalization, before logs or the inbox.

Instantly supports configured delivery headers, not a documented HMAC
signature. Custom business-system signatures belong in instance adapters.
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime

from robothor.operations.store import digest
from robothor.sales.models import Contact, Outcome


class AuthenticationError(ValueError):
    """An inbound integration cannot establish the expected sender identity."""


def _document(raw):
    if len(raw) > 262144:
        raise ValueError("Webhook payload too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Webhook object required")
    return data


def instantly_event(raw, authorization, secret, workspace):
    """Authenticate a configured custom header and minimize the event payload."""
    if (
        not secret
        or not workspace
        or not hmac.compare_digest(authorization or "", "Bearer " + secret)
    ):
        raise AuthenticationError("Webhook authentication required")
    source = _document(raw)
    if source.get("workspace") != workspace:
        raise AuthenticationError("Webhook workspace mismatch")
    kind = source.get("event_type")
    allowed = {
        "email_sent",
        "reply_received",
        "auto_reply_received",
        "email_bounced",
        "lead_unsubscribed",
        "lead_not_interested",
        "account_error",
        "campaign_completed",
        "supersearch_enrichment_completed",
    }
    if kind not in allowed:
        raise ValueError("Unsupported sales event")
    occurred = Outcome.aware(datetime.fromisoformat(source["timestamp"]))
    result = {
        "kind": kind,
        "workspace": workspace,
        "occurred_at": occurred.isoformat(),
        "campaign_id": str(source.get("campaign_id") or ""),
        "provider_id": str(source.get("email_id") or ""),
    }
    for source_key, target_key in [("lead_email", "recipient"), ("email_account", "sender")]:
        if source.get(source_key):
            result[target_key] = Contact.email_address(source[source_key])
    inbound = kind in {"reply_received", "auto_reply_received"}
    if kind in {"email_sent", "reply_received", "auto_reply_received"}:
        result["body"] = str(source.get("reply_text" if inbound else "email_text") or "")[:50000]
        result["subject"] = str(source.get("reply_subject" if inbound else "email_subject") or "")[
            :2000
        ]
        result["direction"] = "inbound" if inbound else "outbound"
    result["event_id"] = "instantly:" + digest(result)
    return result
