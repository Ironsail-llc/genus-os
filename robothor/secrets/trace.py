"""Saying which credential was resolved, in a module that holds none.

Every log line about a credential names the VARIABLE and the STORE, never the
value — that is the rule :mod:`robothor.secrets` opens with, and reading the
accessor confirms it: the value lives in ``from_env``/``from_vault`` and is
returned, never passed to a logger.

CodeQL could not see that. `py/clear-text-logging-sensitive-data` decides what
is sensitive from identifiers, and in a function called ``resolve_secret``
whose parameters include ``vault_key``, a logging call in the same scope as the
value is a path it has to assume. Three alerts on this file, all false, and a
false alert that stays open is indistinguishable from one nobody has read yet.

So the logging moved somewhere it cannot be wrong: this module takes a
:class:`SecretLabel` — a name and a store, both plain strings — and has no
parameter, no import and no scope through which a credential could reach it.
The accessor builds the label from its own ``name`` argument BEFORE it fetches
anything, so there is no expression here derived from a call that returns a
value.

That is a real improvement, not an appeasement: the property "no value reaches
a log record" is now enforced by what this module can see, rather than by every
future edit to the accessor remembering it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("robothor.secrets")

__all__ = ["SecretLabel"]


@dataclass(frozen=True)
class SecretLabel:
    """What may be said out loud about one credential.

    Constructed from the environment variable name alone. It carries no value,
    cannot be constructed from one, and every method on it writes only these
    fields.
    """

    #: The environment variable, e.g. ``GITHUB_TOKEN``. A name, never a value.
    name: str

    def resolved_from_env(self, *, bootstrap: bool = False) -> None:
        if bootstrap:
            logger.debug("secrets: %s (bootstrap) resolved from the process environment", self.name)
        else:
            logger.debug("secrets: %s resolved from the process environment", self.name)

    def resolved_from_vault(self, *, ahead_of_env: bool = False) -> None:
        if ahead_of_env:
            logger.debug("secrets: %s resolved from the vault, ahead of the environment", self.name)
        else:
            logger.debug("secrets: %s resolved from the vault", self.name)

    def fell_through_to_env(self, *, vault_answered: bool) -> None:
        logger.debug(
            "secrets: %s resolved from the process environment (the vault %s)",
            self.name,
            "holds no row" if vault_answered else "could not be read",
        )

    def unavailable(self) -> None:
        logger.debug(
            "secrets: %s is not in the environment and the vault is unavailable", self.name
        )

    def missing(self) -> None:
        logger.debug("secrets: %s is not configured in the environment or the vault", self.name)

    def vault_unreadable(self, *, error_class: str, retry_seconds: float) -> None:
        """The one line above DEBUG, and the one that must not carry text.

        ``error_class`` is a type NAME — ``psycopg2.OperationalError`` — never
        the exception's message, because a psycopg2 error carries the
        connection string and a connection string carries a password.
        """
        logger.info(
            "secrets: vault unreadable while resolving %s (%s); treating it as unset "
            "and not retrying for %.0fs",
            self.name,
            error_class,
            retry_seconds,
        )
