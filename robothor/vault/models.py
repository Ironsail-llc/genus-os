"""Vault data models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class VaultEntry(BaseModel):
    """A vault secret entry (metadata only — never includes the decrypted value)."""

    id: str
    tenant_id: str
    key: str
    category: str = "credential"
    metadata: dict = {}
    created_at: datetime | None = None
    updated_at: datetime | None = None
