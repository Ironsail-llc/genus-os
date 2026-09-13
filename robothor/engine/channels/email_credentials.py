"""The one place this instance's SMTP password is read.

The same rule ``slack_credentials`` was written for, applied before the second
surface exists rather than after: ``genus channel add email`` writes to the
**vault** whenever the instance has a master key, so anything that answers "is
email configured here?" out of ``ctx.settings`` or ``os.environ`` alone reports
"not configured" for the default install. The doctor asks that question, the
channel asks it, and ``genus channel verify`` asks it — three callers, one
function, one answer.

Resolution order is :func:`robothor.secrets.resolve_secret`'s: the process
environment first (an operator who exported a password meant it, and a rotation
that reaches the environment must not be shadowed by a stale vault row), then
the vault, then nothing — reported as ``missing`` rather than guessed at.

**Nothing here logs a value, and neither may a caller.** The ``source`` is safe
to print and worth printing: "which layer answered" is the question an operator
has when the doctor and ``genus channel list`` disagree.

The host, port, user and from-address are NOT here. They are ordinary settings
(``ChannelSettings.email_*``) because they are not credentials, and putting them
behind a master key would make them unreadable by the doctor on an instance
with no vault — the mistake the Slack verify target avoided for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass

from robothor.constants import DEFAULT_TENANT
from robothor.secrets import SecretSource, resolve_secret
from robothor.vault.naming import channel_field

__all__ = [
    "SMTP_PASSWORD_ENV",
    "SMTP_PASSWORD_VAULT_KEY",
    "EmailCredentials",
    "email_credentials",
]

#: The environment name, and the vault row beside it. The vault key is passed to
#: ``resolve_secret`` explicitly rather than derived: ``channels/email/
#: smtp_password`` exports as ``CHANNELS_EMAIL_SMTP_PASSWORD``, NOT
#: ``ROBOTHOR_EMAIL_SMTP_PASSWORD``, so a derived lookup would miss a credential
#: sitting right there in the vault.
SMTP_PASSWORD_ENV = "ROBOTHOR_EMAIL_SMTP_PASSWORD"
SMTP_PASSWORD_VAULT_KEY = channel_field("email", "smtp_password")


@dataclass(frozen=True)
class EmailCredentials:
    """What this instance holds for SMTP, and which layer held it."""

    smtp_password: str | None = None
    smtp_password_source: SecretSource = "missing"

    @property
    def can_send_smtp(self) -> bool:
        """Whether a password is available at all.

        Deliberately not "whether SMTP is configured": an internal relay that
        authenticates by sending host needs no password, and the channel decides
        that from the host and the from-address. This answers the narrower
        question the doctor asks — whether a password that was *stored* can be
        found again.
        """
        return bool(self.smtp_password)


def email_credentials(*, tenant_id: str = DEFAULT_TENANT, live: bool = False) -> EmailCredentials:
    """This instance's SMTP password, from wherever it actually keeps it.

    Never raises and never logs a value: an unreadable vault is reported as
    ``unavailable`` rather than crashing whichever surface asked, because two of
    the three callers run inside a diagnostic.

    Args:
        tenant_id: whose vault to read.
        live: probe the vault even inside its failure cooldown, for a caller
            about to act on "not configured".
    """
    password = resolve_secret(
        SMTP_PASSWORD_ENV,
        vault_key=SMTP_PASSWORD_VAULT_KEY,
        tenant_id=tenant_id,
        live=live,
    )
    return EmailCredentials(
        smtp_password=password.value,
        smtp_password_source=password.source,
    )
