"""Who this instance belongs to — one identity, one file, one tenant.

``owner.yaml`` is what the platform reads (``owner_config.load_owner_config``),
and the operator account in ``user_accounts`` is what the dashboard signs in.
Those two have to agree about the tenant or the instance answers to a name it
cannot authenticate, so the disagreement is checked rather than assumed.

No password is set here. The account is seeded with no credentials and the
browser wizard, which is the only place an operator can type one into a form
rather than a terminal's scrollback, sets it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robothor.init.steps import StepError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable
    from pathlib import Path

__all__ = ["Identity", "bootstrap_operator", "write_identity"]


@dataclass(frozen=True)
class Identity:
    """What the wizard asked the operator for."""

    name: str
    email: str
    tenant_id: str

    @property
    def complete(self) -> bool:
        return bool(self.name.strip() and self.email.strip())


def write_identity(
    identity: Identity,
    *,
    path: Path | None = None,
    writer: Callable[..., bool] | None = None,
) -> bool:
    """Write ``owner.yaml``. True when a file was created.

    Delegates to :func:`robothor.owner_config.write_owner_config` rather than
    formatting the YAML here: one writer means the file init produces and the
    file the loader expects cannot drift, and that function already refuses to
    overwrite an existing identity.
    """
    write = writer
    if write is None:
        from robothor.owner_config import write_owner_config

        write = write_owner_config
    if path is None:
        from robothor.constants import owner_config_path

        path = owner_config_path()
    return bool(write(identity.name, identity.email, tenant_id=identity.tenant_id, path=path))


def bootstrap_operator(
    identity: Identity,
    *,
    bootstrap: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Seed the operator's ``owner`` account, in the tenant owner.yaml names.

    Raises :class:`StepError` when there is no operator to seed or when the
    account came back under a different tenant. The second case looks like a
    successful install and is not one: the dashboard would sign the operator
    into a tenant holding none of their data, and nothing downstream would say
    why.
    """
    seed = bootstrap
    if seed is None:
        from robothor.auth.accounts import bootstrap_owner_account

        seed = bootstrap_owner_account

    account = seed()
    if not account:
        raise StepError(
            "no operator account was created — owner.yaml is missing or has no email; "
            "re-run the identity step"
        )
    tenant = str(account.get("tenant_id", ""))
    if tenant != identity.tenant_id:
        raise StepError(
            f"the operator account was created in tenant {tenant!r} but owner.yaml says "
            f"{identity.tenant_id!r}; the dashboard would sign in to the wrong tenant"
        )
    return account
