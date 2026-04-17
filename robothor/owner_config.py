"""Operator identity loader.

Reads ``~/.robothor/owner.yaml`` (hardcoded path, gitignored content) to
answer "who is the operator of this Genus OS instance?". Callers use this
to:

- Bootstrap the ``tenant_users.person_id`` link on daemon start.
- Resolve any bare first-name to the operator's CRM row with priority over
  other contacts sharing the name.
- Auto-attend the operator on outgoing calendar invites.

Fallback order:
    1. ``owner.yaml`` at the hardcoded path (authoritative).
    2. Legacy env vars ``ROBOTHOR_OWNER_EMAIL`` / ``ROBOTHOR_OWNER_NAME`` —
       emits a ``DeprecationWarning`` and synthesizes a minimal config.
    3. ``None`` — caller must handle (log + degrade gracefully, never crash).

The loader is intentionally tolerant: missing optional fields produce empty
values rather than errors. Callers should never pass the dataclass directly
to external APIs — treat it as an internal identity record only.
"""

from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

from robothor.constants import DEFAULT_TENANT, owner_config_path

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnerConfig:
    """Immutable operator identity for a single Genus OS instance."""

    tenant_id: str
    first_name: str
    last_name: str
    email: str
    additional_emails: tuple[str, ...] = ()
    phone: str | None = None
    nicknames: frozenset[str] = field(default_factory=frozenset)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def matches_name(self, name: str) -> bool:
        """True if ``name`` (case-insensitive) refers to the operator.

        Matches on: first name, last name, full name, or any configured
        nickname. Used by the contact resolver to prefer the owner row on
        name-only lookups.
        """
        if not name:
            return False
        needle = name.strip().lower()
        if not needle:
            return False
        candidates = {
            self.first_name.lower(),
            self.last_name.lower(),
            self.full_name.lower(),
            *self.nicknames,
        }
        candidates.discard("")
        return needle in candidates

    def all_emails(self) -> tuple[str, ...]:
        """Primary email first, then additional emails, deduplicated."""
        seen: set[str] = set()
        out: list[str] = []
        for e in (self.email, *self.additional_emails):
            e = (e or "").strip().lower()
            if e and e not in seen:
                seen.add(e)
                out.append(e)
        return tuple(out)


def _coerce_nicknames(raw: Any) -> frozenset[str]:
    if not raw:
        return frozenset()
    if isinstance(raw, str):
        raw = [raw]
    return frozenset(str(n).strip().lower() for n in raw if str(n).strip())


def _coerce_emails(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    seen: set[str] = set()
    out: list[str] = []
    for e in raw:
        s = str(e).strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out)


def _from_yaml(path: Path) -> OwnerConfig | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("owner.yaml exists at %s but is unreadable: %s", path, exc)
        return None

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        logger.error("owner.yaml at %s is not valid YAML: %s", path, exc)
        return None

    if not isinstance(data, dict):
        logger.error("owner.yaml at %s must be a mapping, got %s", path, type(data).__name__)
        return None

    first = str(data.get("first_name", "")).strip()
    last = str(data.get("last_name", "")).strip()
    email = str(data.get("email", "")).strip().lower()

    if not first or not email:
        logger.error("owner.yaml at %s is missing required fields (first_name, email)", path)
        return None

    phone_raw = data.get("phone")
    phone = str(phone_raw).strip() if phone_raw else None

    return OwnerConfig(
        tenant_id=str(data.get("tenant_id") or DEFAULT_TENANT).strip(),
        first_name=first,
        last_name=last,
        email=email,
        additional_emails=_coerce_emails(data.get("additional_emails")),
        phone=phone or None,
        nicknames=_coerce_nicknames(data.get("nicknames")),
    )


def _from_env() -> OwnerConfig | None:
    email = os.environ.get("ROBOTHOR_OWNER_EMAIL", "").strip().lower()
    name = os.environ.get("ROBOTHOR_OWNER_NAME", "").strip()
    if not email or not name:
        return None
    warnings.warn(
        "ROBOTHOR_OWNER_EMAIL / ROBOTHOR_OWNER_NAME are deprecated. "
        "Create ~/.robothor/owner.yaml from templates/owner.yaml.example instead.",
        DeprecationWarning,
        stacklevel=3,
    )
    parts = name.split(None, 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else ""
    return OwnerConfig(
        tenant_id=DEFAULT_TENANT,
        first_name=first,
        last_name=last,
        email=email,
    )


def load_owner_config(path: Path | None = None) -> OwnerConfig | None:
    """Load the operator identity. ``None`` when nothing is configured."""
    target = path or owner_config_path()
    config = _from_yaml(target)
    if config is not None:
        return config
    return _from_env()
