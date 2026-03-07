"""Vault data models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from datetime import datetime


class VaultEntry(BaseModel):
    """A vault secret entry (metadata only — never includes the decrypted value)."""

    id: str
    tenant_id: str
    key: str
    category: str = "credential"
    metadata: dict[str, Any] = {}
    created_at: datetime | None = None
    updated_at: datetime | None = None
