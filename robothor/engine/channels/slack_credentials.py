"""The one place this instance's Slack credentials are read.

Four surfaces answer the question "is Slack configured here?" — the daemon's
inbound gate, ``SlackBot.start``, the outbound :class:`~robothor.engine.channels.
slack.SlackChannel`, and the doctor. When each read the credential its own way
they gave different answers on the same box, and the disagreement was silent:

* ``genus channel add slack`` writes both tokens to the **vault** whenever the
  instance has a master key — the normal full install, and the CLI's default.
* The daemon gate and ``SlackBot.start`` read ``os.environ`` only, and nothing
  preloads channel credentials into the process environment (``key_pool``
  filters its vault→env export down to *provider* key names). So the Socket
  Mode bot never started, and the env gate logs nothing at all when it declines.
* The doctor read ``ctx.settings``, which never consults the vault, and so
  reported "not configured" for an instance ``genus channel list`` called
  configured — taking the ``xoxb-``/``xapp-`` swap check, the entire reason that
  check exists, out of service for every vault install.
* Only the outbound channel resolved through the secrets accessor, which is why
  posting worked and everything else did not.

One function, so there is one answer. The resolution order is
:func:`robothor.secrets.resolve_secret`'s: the process environment first (an
operator who exported a token meant it, and a rotation that reaches the
environment must not be shadowed by a stale vault row), then the vault, then
nothing — reported as ``missing`` rather than guessed at.

**Nothing here logs a value, and neither may a caller.** The ``source`` is safe
to print and is worth printing: "which layer answered" is the question an
operator has when the UI and the daemon disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from robothor.constants import DEFAULT_TENANT
from robothor.secrets import SecretSource, resolve_secret
from robothor.settings.env import process_env_get
from robothor.vault.naming import channel_field

__all__ = [
    "APP_TOKEN_ENV",
    "APP_TOKEN_VAULT_KEY",
    "BOT_TOKEN_ENV",
    "BOT_TOKEN_VAULT_KEY",
    "SlackCredentials",
    "environment_token",
    "slack_credentials",
]

#: The environment names, and the A3 vault rows beside them. The vault key has
#: to be passed to ``resolve_secret`` explicitly rather than derived:
#: ``channels/slack/bot_token`` exports as ``CHANNELS_SLACK_BOT_TOKEN``, NOT
#: ``ROBOTHOR_SLACK_BOT_TOKEN``, so a derived lookup would miss a credential
#: sitting right there in the vault.
BOT_TOKEN_ENV = "ROBOTHOR_SLACK_BOT_TOKEN"
APP_TOKEN_ENV = "ROBOTHOR_SLACK_APP_TOKEN"
BOT_TOKEN_VAULT_KEY = channel_field("slack", "bot_token")
APP_TOKEN_VAULT_KEY = channel_field("slack", "app_token")


@dataclass(frozen=True)
class SlackCredentials:
    """What this instance holds, and which layer held it.

    The two capabilities are separate on purpose, and they are what callers
    should branch on rather than on the tokens themselves:

    ``can_send`` — outbound delivery needs only the bot token. An instance that
    wants a daily briefing in a channel should not have to run a socket.

    ``can_listen`` — Socket Mode needs both. This is the daemon's gate.
    """

    bot_token: str | None = None
    app_token: str | None = None
    bot_source: SecretSource = "missing"
    app_source: SecretSource = "missing"

    @property
    def can_send(self) -> bool:
        """Whether outbound delivery can post at all."""
        return bool(self.bot_token)

    @property
    def can_listen(self) -> bool:
        """Whether the inbound Socket Mode bot can start."""
        return bool(self.bot_token and self.app_token)


def slack_credentials(*, tenant_id: str = DEFAULT_TENANT, live: bool = False) -> SlackCredentials:
    """This instance's Slack tokens, from wherever it actually keeps them.

    Never raises and never logs a value: an unreadable vault is reported as
    ``unavailable`` rather than crashing whichever surface asked, because three
    of the four callers run at boot or inside a diagnostic.

    Args:
        tenant_id: whose vault to read.
        live: probe the vault even inside its failure cooldown. For a caller
            about to *act* on "not configured" — the daemon gate declining to
            start the bot is exactly that — and which must not act on a
            five-minute-old verdict about a vault that may have recovered.
    """
    bot = resolve_secret(
        BOT_TOKEN_ENV, vault_key=BOT_TOKEN_VAULT_KEY, tenant_id=tenant_id, live=live
    )
    app = resolve_secret(
        APP_TOKEN_ENV, vault_key=APP_TOKEN_VAULT_KEY, tenant_id=tenant_id, live=live
    )
    return SlackCredentials(
        bot_token=bot.value,
        app_token=app.value,
        bot_source=bot.source,
        app_source=app.source,
    )


def environment_token(env_name: str) -> str:
    """The environment layer alone, for the one caller that is deciding what to STORE.

    ``genus channel add`` picks up an exported token so an operator does not
    have to paste one they have already set. That is a question about the
    *environment*, not about what this instance can currently resolve: answering
    it from the vault would have ``add --to env`` copy a vault row into the
    instance env file, which is a credential moving somewhere the operator did
    not ask for.

    It lives here anyway so that every Slack credential name is spelled in one
    module, and it goes through ``settings.env`` rather than ``os.environ`` so
    the env-read ratchet keeps counting call sites elsewhere.
    """
    return (process_env_get(env_name, "") or "").strip()
