"""Which tenant's engine a plugin lockfile belongs to.

Every tenant runs its own engine, and each engine must have its own lockfile:
a child admin may enable and disable plugins in their own tenant, and on a
lockfile shared with the root that disable would turn the plugin off for the
root and every sibling on their next reload. The Helm chart gives every cell
its own workspace, so its lockfile is its own; a single-host deployment that
starts a child engine on the root's workspace would not be.

So a small marker beside the lockfile names the tenant whose engine owns it.
The engine claims it at boot, and the first write claims an unclaimed file. A
ROOT tenant's engine (its tenant row has no parent) takes a claim over from a
child, so the child is refused whichever engine booted first. A root never
takes another root's claim: two root engines on one workspace are refused.
An engine whose lockfile names a DIFFERENT tenant refuses to change it (the
engine admin API answers 409), and the remedy is a workspace of its own.

Never raises from :func:`claim` or :func:`foreign_owner`: a marker that cannot
be read or written constrains nothing, the same posture the lockfile reader
takes, and it is logged.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["SHARED_REMEDY", "claim", "foreign_owner"]

#: What an engine told its lockfile is another tenant's should do. Names the
#: settings, never a path.
SHARED_REMEDY = (
    "this engine's plugin lockfile belongs to another tenant's engine; give "
    "each engine its own ROBOTHOR_WORKSPACE (or ROBOTHOR_PLUGIN_LOCKFILE) before "
    "changing plugins here"
)

_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _engine_tenant() -> str:
    from robothor import constants

    return constants.DEFAULT_TENANT


def _is_root_tenant(tenant: str) -> bool:
    """Whether ``tenant`` is a root: its ``crm_tenants`` row has no parent.

    Not ``PLATFORM_TENANT``: in a child engine that constant is the child's own
    id unless ``ROBOTHOR_PLATFORM_TENANT`` is set, so comparing against it
    would let every child take the file. False when the row cannot be read,
    so an unreadable database never lets anybody take a claim over.
    """
    try:
        from robothor.crm.dal import get_tenant

        row = get_tenant(tenant)
    except Exception as exc:  # noqa: BLE001 - unknown is "not root", never fatal
        logger.debug("Tenant row unreadable for the lockfile claim (%s)", type(exc).__name__)
        return False
    return row is not None and not row.get("parentTenantId")


def _marker() -> Path | None:
    from robothor.plugins.lockfile import lockfile_path

    lock = lockfile_path()
    return None if lock is None else lock.with_name(lock.name + ".tenant")


def _read(marker: Path) -> str | None:
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return text if _TENANT.match(text) else None


def _write(marker: Path, tenant: str) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(marker.parent), prefix=".plugins.lock.tenant.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(tenant + "\n")
        tmp.chmod(0o600)
        tmp.replace(marker)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def claim(tenant: str | None = None) -> str | None:
    """The tenant that owns this engine's lockfile, claiming it when nobody has.

    None when no lockfile path resolves, or the marker cannot be read or
    written; that is logged and constrains nothing.
    """
    me = tenant or _engine_tenant()
    try:
        marker = _marker()
        if marker is None:
            return None
        owner = _read(marker)
        if owner is None:
            _write(marker, me)
            return me
        if owner != me and _is_root_tenant(me) and not _is_root_tenant(owner):
            # The root's claim is authoritative. Boot order on a shared
            # workspace is random after an upgrade, and the child is the side
            # that must be refused, whoever started first.
            logger.error(
                "Plugin lockfile was claimed by tenant %r; the root engine takes it back. "
                "Give that engine a workspace of its own.",
                owner,
            )
            _write(marker, me)
            return me
        return owner
    except Exception as exc:  # noqa: BLE001 - governance must never block boot
        logger.warning(
            "Plugin lockfile owner could not be read or claimed (%s)", type(exc).__name__
        )
        return None


def foreign_owner(tenant: str | None = None) -> str | None:
    """Another tenant that owns this engine's lockfile, or None when it is ours."""
    me = tenant or _engine_tenant()
    owner = claim(me)
    return owner if owner is not None and owner != me else None
