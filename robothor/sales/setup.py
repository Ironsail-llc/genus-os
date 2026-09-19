"""Reviewable sales integration configuration and secret-name-only readiness."""

from datetime import UTC, datetime

from pydantic import Field, field_validator

from robothor import vault
from robothor.operations.store import digest
from robothor.sales.models import Contract, SalesSettings

SETUP_FIELDS = {
    "senders",
    "mailbox_approved_until",
    "postal_address",
    "unsubscribe_url",
    "business_sources",
    "discovery_segments",
}


class SetupChange(Contract):
    changes: dict = Field(min_length=1)
    expected_revision: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=10, max_length=2000)

    @field_validator("changes")
    @classmethod
    def configuration_only(cls, value):
        if not value.keys() <= SETUP_FIELDS:
            raise ValueError(
                "Setup accepts sender, business source and discovery configuration only; use the vault for credentials"
            )
        return value


class Setup:
    def __init__(self, sales, *, secret_names=None, service_get=None):
        from robothor.engine.services import get_service

        self.sales, self.tenant = sales, sales.tenant
        self.secret_names = secret_names or vault.list
        self.service_get = service_get or get_service

    def snapshot(self):
        snapshot = self.sales.settings_snapshot()
        settings = SalesSettings.model_validate(snapshot["config"])
        present = set(self.secret_names(tenant_id=self.tenant))
        required = {
            "pipedrive": ["providers/pipedrive/api_key", "providers/pipedrive/company_domain"],
            "instantly": [
                "providers/instantly/api_key",
                "providers/instantly/workspace_id",
                "providers/instantly/webhook_secret",
            ],
        }
        if settings.email_provider == "none":
            required.pop("instantly")
        sources = []
        for source in settings.business_sources:
            factory = self.service_get("sales.business." + source.source)
            requirements = (
                getattr(factory, "credential_requirements", ()) if callable(factory) else ()
            )
            keys = [
                key
                for key in requirements
                if isinstance(key, str) and key.startswith("providers/" + source.source + "/")
            ][:20]
            if keys:
                required[source.source] = keys
            sources.append(
                {
                    "source": source.source,
                    "account_id": source.account_id,
                    "installed": callable(factory),
                    "credential_metadata_available": bool(keys),
                }
            )
        credentials = [
            {
                "provider": provider,
                "complete": all(key in present for key in keys),
                "keys": [{"name": key, "present": key in present} for key in keys],
            }
            for provider, keys in required.items()
        ]
        now = datetime.now(UTC)
        mailbox_reviews = [
            {
                "email": sender,
                "approved_until": settings.mailbox_approved_until.get(sender),
                "current_review": bool(
                    settings.mailbox_approved_until.get(sender)
                    and settings.mailbox_approved_until[sender] > now
                ),
            }
            for sender in settings.senders
        ]
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT id,status,error,result,updated_at FROM operation_jobs WHERE tenant_id=%s AND kind='sales.provider_status' ORDER BY updated_at DESC LIMIT 20",
                (self.tenant,),
            )
            checks = [dict(r) for r in cur.fetchall()]
        return {
            **snapshot,
            "credentials": credentials,
            "business_services": sources,
            "mailboxes": mailbox_reviews,
            "provider_checks": checks,
            "readiness": {
                "email_provider": settings.email_provider,
                "connected": None,
                "connection_check": "Not performed by configuration inspection",
                "fleet_selected": bool(settings.fleet_release_id),
                "buying_cases_published": bool(settings.active_policy_versions),
                "claims_published": bool(settings.active_knowledge_version),
                "sender_details_configured": bool(
                    settings.senders and settings.postal_address and settings.unsubscribe_url
                ),
            },
            "notes": (
                ["Email delivery is excluded. Research and CRM can run without an email provider."]
                if settings.email_provider == "none"
                else []
            )
            + [
                "Credential presence is not proof of authentication, subscription entitlement, mailbox health or a successful live pilot.",
                "Saving setup does not enable any integration. Provider reads and sending remain controlled separately.",
            ],
        }

    def save(self, changes, expected_revision, actor, reason):
        change = SetupChange(changes=changes, expected_revision=expected_revision, reason=reason)
        self.sales.configure(
            change.changes, actor, expected_revision=change.expected_revision, reason=change.reason
        )
        return self.snapshot()


def sender_context_hash(config, sender):
    expiry = config.get("mailbox_approved_until", {}).get(sender)
    if expiry:
        expiry = datetime.fromisoformat(expiry).astimezone(UTC).isoformat()
    return digest(
        {
            "sender": sender,
            "enabled": sender in config.get("senders", []),
            "postal_address": config.get("postal_address", ""),
            "unsubscribe_url": config.get("unsubscribe_url", ""),
            "timezone": config.get("timezone", "America/Chicago"),
            "readiness_until": expiry,
        }
    )
