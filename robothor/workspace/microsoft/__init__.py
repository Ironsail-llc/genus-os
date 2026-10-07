"""Microsoft 365 (Exchange Online) transport: Entra app-only auth + Microsoft Graph.

:func:`graph_client_from_vault` is the one way the platform builds a Graph
client: the app registration's credential lives in the vault under
``workspace/microsoft365/<field>`` (see :func:`robothor.vault.naming.workspace_key`)
and is re-read on every token refresh, so a rotated certificate applies without
a restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.vault.naming import workspace_key
from robothor.workspace.errors import AuthError
from robothor.workspace.microsoft.auth import (
    ClientCredentials,
    ClientCredentialTokenSource,
    TokenSource,
)
from robothor.workspace.microsoft.graph import GraphClient

if TYPE_CHECKING:
    import httpx

__all__ = [
    "PROVIDER",
    "VAULT_FIELDS",
    "ClientCredentialTokenSource",
    "GraphClient",
    "TokenSource",
    "graph_client_from_vault",
    "load_credentials",
]

PROVIDER = "microsoft365"

#: Every vault field the Microsoft transport reads.
VAULT_FIELDS = (
    "tenant_id",
    "client_id",
    "client_certificate_pem",
    "client_private_key_pem",
    "client_secret",
)

_NOT_CONNECTED = "microsoft365 not connected"

SecretGetter = Callable[..., Any]


async def load_credentials(
    tenant_id: str = DEFAULT_TENANT, *, secret_get: SecretGetter | None = None
) -> ClientCredentials:
    """The app credential from the vault of platform tenant ``tenant_id``.

    Raises :class:`AuthError` ``"microsoft365 not connected"`` when the
    directory, the client id, or every credential is missing.
    """
    if not tenant_id:
        raise ValueError("explicit platform tenant required")
    if secret_get is None:
        from robothor import vault

        secret_get = vault.get

    async def read(field: str) -> str:
        value = await asyncio.to_thread(
            secret_get, workspace_key(PROVIDER, field), tenant_id=tenant_id
        )
        return str(value or "").strip()

    values = dict(
        zip(VAULT_FIELDS, await asyncio.gather(*(read(f) for f in VAULT_FIELDS)), strict=True)
    )
    if not values["tenant_id"] or not values["client_id"]:
        raise AuthError(_NOT_CONNECTED)
    if not values["client_certificate_pem"] and not values["client_secret"]:
        raise AuthError(_NOT_CONNECTED)
    return ClientCredentials(
        tenant_id=values["tenant_id"],
        client_id=values["client_id"],
        certificate_pem=values["client_certificate_pem"] or None,
        private_key_pem=values["client_private_key_pem"] or None,
        client_secret=values["client_secret"] or None,
    ).validated()


async def graph_client_from_vault(
    tenant_id: str = DEFAULT_TENANT,
    *,
    secret_get: SecretGetter | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GraphClient:
    """A :class:`GraphClient` authenticated from the vault of platform tenant ``tenant_id``.

    The configuration is checked now (so "not connected" surfaces at build
    time) and re-read on every token refresh (so rotations apply).
    """
    await load_credentials(tenant_id, secret_get=secret_get)

    async def loader() -> ClientCredentials:
        return await load_credentials(tenant_id, secret_get=secret_get)

    source = ClientCredentialTokenSource.from_loader(loader, transport=transport)
    return GraphClient(source, transport=transport)
