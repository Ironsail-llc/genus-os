"""Internal provider adapters. Agents cannot call these mutation methods directly.

Vault credentials are loaded per request so rotations take effect immediately.
No automatic write retries: an ambiguous outcome belongs in reconciliation.
"""

from __future__ import annotations

import asyncio
import re
from html import escape
from urllib.parse import quote

import httpx

from robothor import vault
from robothor.operations.store import Operations
from robothor.vault.naming import provider_key


class ProviderError(RuntimeError):
    """A redacted provider failure, safe for operator diagnostics."""

    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class UnknownEffect(ProviderError):  # noqa: N818
    """A write may have committed. Reconcile before attempting another write."""


class RateLimited(ProviderError):  # noqa: N818
    def __init__(self, retry_after=60):
        super().__init__("Provider rate limit; retry later", status=429)
        self.retry_after = retry_after


class Provider:
    provider = ""

    def __init__(self, tenant_id, *, secret_get=None, transport=None):
        if not tenant_id:
            raise ValueError("Explicit tenant required")
        self.tenant = tenant_id
        self.secret_get = secret_get or vault.get
        self.transport = transport

    async def secret(self, key):
        value = await asyncio.to_thread(self.secret_get, key, tenant_id=self.tenant)
        if not value:
            raise ProviderError(f"{self.provider} vault configuration missing")
        return value

    async def connection(self):
        raise NotImplementedError

    async def request(self, method, path, *, payload=None, params=None, _connection=None):
        """Use fixed provider origins and header auth; never log response bodies."""
        if not path.startswith("/api/") or "?" in path or "#" in path or ".." in path:
            raise ProviderError("Invalid provider resource path")
        origin, headers = _connection or await self.connection()
        writes = method.upper() not in {"GET", "HEAD"}
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=20, follow_redirects=False
            ) as client:
                response = await client.request(
                    method, origin + path, headers=headers, json=payload, params=params
                )
        except httpx.RequestError:
            error = UnknownEffect if writes else ProviderError
            raise error(f"{self.provider} transport failure") from None
        if response.status_code == 429:
            try:
                delay = min(3600, max(1, int(response.headers.get("Retry-After", "60"))))
            except ValueError:
                delay = 60
            raise RateLimited(delay)
        if not 200 <= response.status_code < 300:
            error = UnknownEffect if writes and response.status_code >= 500 else ProviderError
            raise error(f"{self.provider} HTTP {response.status_code}", status=response.status_code)
        try:
            result = response.json()
        except ValueError:
            error = UnknownEffect if writes else ProviderError
            raise error(f"{self.provider} invalid response") from None
        if not isinstance(result, dict):
            error = UnknownEffect if writes else ProviderError
            raise error(f"{self.provider} invalid response shape")
        return result


class Pipedrive(Provider):
    provider = "pipedrive"

    async def connection(self):
        domain = await self.secret("providers/pipedrive/company_domain")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", domain):
            raise ProviderError("Invalid Pipedrive company domain")
        token = await self.secret(provider_key(self.provider))
        origin = f"https://{domain}.pipedrive.com"
        if getattr(self, "expected_scope", origin) != origin:
            raise ProviderError("Pipedrive account changed during the operation")
        return origin, {"x-api-token": token}

    async def data(self, method, path, **kwargs):
        result = await self.request(method, path, **kwargs)
        if result.get("success") is not True or "data" not in result:
            error = UnknownEffect if method != "GET" else ProviderError
            raise error("Pipedrive did not acknowledge the operation")
        return result["data"]

    async def identity(self):
        return await self.data("GET", "/api/v1/users/me")

    async def identity_scope(self):
        origin, _ = await self.connection()
        return origin

    async def organization(self, organization_id):
        if type(organization_id) is not int or organization_id <= 0:
            raise ProviderError("Positive organization ID required")
        return await self.data("GET", f"/api/v2/organizations/{organization_id}")

    async def person_record(self, person_id):
        if type(person_id) is not int or person_id <= 0:
            raise ProviderError("Positive person ID required")
        return await self.data("GET", f"/api/v2/persons/{person_id}")

    async def lead(self, lead_id):
        from uuid import UUID

        return await self.data("GET", "/api/v1/leads/" + str(UUID(str(lead_id))))

    async def search_organizations(self, name):
        return await self.data(
            "GET",
            "/api/v2/organizations/search",
            params={"term": name, "fields": "name", "exact_match": "true", "limit": 100},
        )

    async def create_organization(self, name):
        return await self.data("POST", "/api/v2/organizations", payload={"name": name})

    async def search_people(self, email):
        return await self.data(
            "GET",
            "/api/v2/persons/search",
            params={"term": email, "fields": "email", "exact_match": "true", "limit": 100},
        )

    async def create_person(self, name, email, organization_id):
        return await self.data(
            "POST",
            "/api/v2/persons",
            payload={
                "name": name,
                "org_id": organization_id,
                "emails": [{"value": email, "primary": True, "label": "work"}],
            },
        )

    async def create_lead(self, title, organization_id, person_id=None):
        payload = {"title": title, "organization_id": organization_id}
        if person_id is not None:
            payload["person_id"] = person_id
        return await self.data("POST", "/api/v1/leads", payload=payload)


class Instantly(Provider):
    provider = "instantly"

    async def connection(self):
        token = await self.secret(provider_key(self.provider))
        return "https://api.instantly.ai", {"Authorization": "Bearer " + token}

    async def lead(self, lead_id):
        from uuid import UUID

        return await self.request("GET", "/api/v2/leads/" + str(UUID(str(lead_id))))

    async def account(self, email):
        return await self.request("GET", "/api/v2/accounts/" + quote(email, safe=""))

    async def start_verification(self, email):
        return await self.request("POST", "/api/v2/email-verification", payload={"email": email})

    async def verification(self, email):
        return await self.request("GET", "/api/v2/email-verification/" + quote(email, safe=""))

    async def accounts(self, cursor=None):
        return await self.request(
            "GET",
            "/api/v2/accounts",
            params={"limit": 100, **({"starting_after": cursor} if cursor else {})},
        )

    async def emails(
        self,
        *,
        cursor=None,
        campaign_id=None,
        min_timestamp_created=None,
        max_timestamp_created=None,
        sort_order=None,
        latest_of_thread=None,
        workspace_id=None,
    ):
        params = {"limit": 100}
        if cursor:
            params["starting_after"] = cursor
        if campaign_id:
            params["campaign_id"] = campaign_id
        params.update(
            {
                key: value
                for key, value in {
                    "min_timestamp_created": min_timestamp_created,
                    "max_timestamp_created": max_timestamp_created,
                    "sort_order": sort_order,
                    "latest_of_thread": latest_of_thread,
                }.items()
                if value is not None
            }
        )
        if not await asyncio.to_thread(
            Operations(self.tenant).admit_request, "instantly.emails", limit=20, window_seconds=60
        ):
            raise RateLimited(60)
        # Pin credentials across identity check and page fetch. An empty page
        # cannot prove workspace identity from its nonexistent message records.
        connection = await self.connection()
        expected = await self.secret("providers/instantly/workspace_id")
        if workspace_id is not None and expected != workspace_id:
            raise ProviderError("Instantly workspace configuration changed")
        workspace = await self.request("GET", "/api/v2/workspaces/current", _connection=connection)
        if not isinstance(workspace, dict) or workspace.get("id") != expected:
            raise ProviderError("Instantly workspace identity mismatch")
        return await self.request("GET", "/api/v2/emails", params=params, _connection=connection)

    async def get_email(self, email_id):
        return await self.request("GET", "/api/v2/emails/" + quote(email_id, safe=""))

    async def reply(self, draft):
        if not draft.get("reply_to_uuid"):
            raise ProviderError("Existing thread required for an Instantly reply")
        return await self.request(
            "POST",
            "/api/v2/emails/reply",
            payload={
                "eaccount": draft["sender"],
                "reply_to_uuid": draft["reply_to_uuid"],
                "subject": draft["subject"],
                "body": {
                    "text": draft["body"],
                    "html": escape(draft["body"]).replace("\n", "<br/>"),
                },
            },
        )

    async def create_campaign(self, action_id, draft, timezone, send_date):
        """Create an inert, one-recipient, one-message campaign for an approved draft.

        The executor adds exactly one lead and separately checks authorization
        before activation. No provider follow-ups or variant generation.
        """
        return await self.request(
            "POST",
            "/api/v2/campaigns",
            payload={
                "name": "Genus approved " + str(action_id),
                "campaign_schedule": {
                    "start_date": send_date,
                    "end_date": send_date,
                    "schedules": [
                        {
                            "name": "Approved send window",
                            "timing": {"from": "09:00", "to": "16:00"},
                            # The executor admits only weekdays; the date is
                            # fixed to that one day, avoiding weekday-index ambiguity.
                            "days": {str(d): True for d in range(7)},
                            "timezone": timezone,
                        }
                    ],
                },
                "sequences": [
                    {
                        "steps": [
                            {
                                "type": "email",
                                "delay": 0,
                                "variants": [
                                    {
                                        "subject": draft["subject"],
                                        "body": escape(draft["body"]).replace("\n", "<br/>"),
                                    }
                                ],
                            }
                        ]
                    }
                ],
                "email_list": [draft["sender"]],
                "daily_limit": 1,
                "daily_max_leads": 1,
                "text_only": True,
                "stop_on_reply": True,
                "stop_on_auto_reply": True,
                "stop_for_company": True,
                "link_tracking": False,
                "open_tracking": False,
                "insert_unsubscribe_header": True,
                "allow_risky_contacts": False,
                "disable_bounce_protect": False,
            },
        )

    async def add_lead(self, campaign_id, email):
        return await self.request(
            "POST",
            "/api/v2/leads",
            payload={
                "campaign": campaign_id,
                "email": email,
                "skip_if_in_workspace": True,
                "skip_if_in_campaign": True,
            },
        )

    async def activate(self, campaign_id):
        return await self.request(
            "POST", "/api/v2/campaigns/" + quote(campaign_id, safe="") + "/activate"
        )

    async def pause(self, campaign_id):
        return await self.request(
            "POST", "/api/v2/campaigns/" + quote(campaign_id, safe="") + "/pause"
        )

    async def suppress(self, email_or_domain):
        return await self.request(
            "POST",
            "/api/v2/block-lists-entries",
            payload={"bl_value": email_or_domain.removeprefix("@")},
        )
