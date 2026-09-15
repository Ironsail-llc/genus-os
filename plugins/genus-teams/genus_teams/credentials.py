"""The one place this instance's Teams credentials are read.

The rule ``robothor/engine/channels/slack_credentials.py`` was written for,
applied to a channel that lives outside the platform: four surfaces will ask
"is Teams configured here?" — the outbound channel, the inbound endpoint (which
needs the application id to check an audience), ``genus channel verify`` and the
doctor — and when each reads it its own way they disagree silently on the same
box.

Resolution order, per value:

1. :func:`robothor.secrets.resolve_secret` — the process environment first (an
   operator who exported a value meant it, and a rotation that reaches the
   environment must not be shadowed by a stale vault row), then this instance's
   vault under ``channels/teams/<field>``;
2. for the two values that are **not secrets**, the settings model — which is
   where ``genus channel add teams --app-id`` writes them, because a value
   behind a master key is one the doctor cannot read on an instance without one.

Which layer answered is reported and is safe to print; the *values* are not. The
client secret is never logged, never returned in ``health()``, and never put in
a ``verify`` step's detail — see :mod:`genus_teams.channel`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from robothor.constants import DEFAULT_TENANT
from robothor.secrets import resolve_secret
from robothor.vault.naming import channel_field

#: Which layer answered. The platform's ``SecretSource`` plus ``settings``: two
#: of these three values are not secrets and are legitimately read out of
#: ``config.yaml``, which the secrets accessor does not consult.
CredentialSource = Literal["env", "vault", "settings", "missing", "unavailable"]

__all__ = [
    "CredentialSource",
    "APP_ID_ENV",
    "APP_ID_VAULT_KEY",
    "APP_PASSWORD_ENV",
    "APP_PASSWORD_VAULT_KEY",
    "TENANT_ID_ENV",
    "TENANT_ID_VAULT_KEY",
    "TeamsCredentials",
    "teams_credentials",
]

#: The environment names, and the vault rows beside them. The vault key is
#: passed explicitly rather than derived: ``channels/teams/app_password``
#: exports as ``CHANNELS_TEAMS_APP_PASSWORD``, not
#: ``ROBOTHOR_TEAMS_APP_PASSWORD``, so a derived lookup would miss a credential
#: sitting right there in the vault.
APP_ID_ENV = "ROBOTHOR_TEAMS_APP_ID"
APP_PASSWORD_ENV = "ROBOTHOR_TEAMS_APP_PASSWORD"
TENANT_ID_ENV = "ROBOTHOR_TEAMS_TENANT_ID"
APP_ID_VAULT_KEY = channel_field("teams", "app_id")
APP_PASSWORD_VAULT_KEY = channel_field("teams", "app_password")
TENANT_ID_VAULT_KEY = channel_field("teams", "tenant_id")

#: The directory a MULTI-tenant bot authenticates against. Microsoft's own
#: tenant, not the customer's — a bot registered as multi-tenant has no
#: directory of its own to name.
BOTFRAMEWORK_TENANT = "botframework.com"


@dataclass(frozen=True)
class TeamsCredentials:
    """What this instance holds, and which layer held it.

    The two capabilities are separate, and callers branch on them rather than on
    the values:

    ``can_send`` — an outbound activity needs the application id AND the client
    secret, because both go into the token request.

    ``can_authenticate_inbound`` — validating an inbound activity's token needs
    only the application id: it is the ``aud`` claim the signature is checked
    against. An instance that has the id but not the secret can prove a request
    genuine and cannot answer it, and saying so precisely is the difference
    between "your endpoint is unreachable" and "your bot cannot reply".
    """

    app_id: str | None = None
    app_password: str | None = None
    directory_tenant_id: str | None = None
    app_id_source: CredentialSource = "missing"
    app_password_source: CredentialSource = "missing"
    directory_tenant_source: CredentialSource = "missing"

    @property
    def can_send(self) -> bool:
        return bool(self.app_id and self.app_password)

    @property
    def can_authenticate_inbound(self) -> bool:
        return bool(self.app_id)

    @property
    def token_tenant(self) -> str:
        """The directory the token request is addressed to.

        A single-tenant bot must name its own directory or Entra refuses the
        grant; a multi-tenant one uses ``botframework.com``. Defaulting to the
        multi-tenant value is right because that is what the Azure Bot wizard
        creates unless the operator chose otherwise.
        """
        return (self.directory_tenant_id or "").strip() or BOTFRAMEWORK_TENANT


def _setting(field: str) -> str:
    """One non-secret value from the settings model, or ``""``.

    Through ``get_settings()`` rather than the environment, so the value
    ``genus channel add teams`` wrote to ``config.yaml`` is found, the setting
    appears in ``genus config`` and in the configuration reference, and the
    platform's env-read ratchet keeps counting call sites elsewhere.

    Spelled out field by field rather than reached with ``getattr(settings,
    field)``, because the platform's plugin scanner **blocks** a wheel that
    resolves an attribute by a name computed at runtime — that is how ``exec``
    and ``os.system`` get called without being named, and a scanner cannot tell
    this use from that one. It is right to refuse it, and two branches are a
    small price for a distribution an operator can actually install.
    """
    try:
        from robothor.settings import get_settings

        channels = get_settings().channels
        if field == "teams_app_id":
            value = channels.teams_app_id
        elif field == "teams_tenant_id":
            value = channels.teams_tenant_id
        else:  # pragma: no cover - there is no third non-secret value
            return ""
        return str(value or "").strip()
    except Exception:  # noqa: BLE001 — unrelated bad config must not make a
        # configured channel look unconfigured; the secrets layer still answers.
        return ""


def _with_settings_fallback(
    value: str | None, source: str, field: str
) -> tuple[str | None, CredentialSource]:
    """A resolved value, or the settings model's — for the non-secrets only.

    ``genus channel add teams`` writes the application id and the directory
    tenant id to ``config.yaml`` on purpose (a value behind a master key is one
    the doctor cannot read on an instance with no vault), and the secrets
    accessor does not look there. So this is the third layer, and it is
    deliberately NOT offered for the client secret.
    """
    if value:
        return value, source  # type: ignore[return-value]
    from_settings = _setting(field)
    if from_settings:
        return from_settings, "settings"
    return None, "missing"


def teams_credentials(*, tenant_id: str = DEFAULT_TENANT, live: bool = False) -> TeamsCredentials:
    """This instance's Teams credentials, from wherever it actually keeps them.

    Never raises and never logs a value: an unreadable vault is reported as
    ``unavailable`` rather than crashing whichever surface asked, because three
    of the four callers run at boot or inside a diagnostic.

    Args:
        tenant_id: whose vault to read.
        live: probe the vault even inside its failure cooldown — for a caller
            about to *act* on "not configured".
    """
    app_id = resolve_secret(APP_ID_ENV, vault_key=APP_ID_VAULT_KEY, tenant_id=tenant_id, live=live)
    password = resolve_secret(
        APP_PASSWORD_ENV, vault_key=APP_PASSWORD_VAULT_KEY, tenant_id=tenant_id, live=live
    )
    directory = resolve_secret(
        TENANT_ID_ENV, vault_key=TENANT_ID_VAULT_KEY, tenant_id=tenant_id, live=live
    )

    app_id_value, app_id_source = _with_settings_fallback(
        app_id.value, app_id.source, "teams_app_id"
    )
    directory_value, directory_source = _with_settings_fallback(
        directory.value, directory.source, "teams_tenant_id"
    )

    return TeamsCredentials(
        app_id=app_id_value,
        app_password=password.value,
        directory_tenant_id=directory_value,
        app_id_source=app_id_source,
        app_password_source=password.source,
        directory_tenant_source=directory_source,
    )
