"""Genus OS authentication core.

The CRM/engine *authorization* layer already exists (``role_permissions`` +
``robothor.engine.permissions``). This package adds the missing *authentication*
layer: human user accounts, password/SSO credentials, and bridge-issued JWT
sessions that carry a VERIFIED ``{user_id, tenant_id, role}`` — replacing the
unverified ``X-Agent-Id`` / ``X-Tenant-Id`` header trust.

Shared by the bridge (verifies sessions), the engine (service tokens), and the
CLI (owner bootstrap), so it lives in ``robothor.auth`` rather than under the
bridge.

Modules:
    tokens     — issue/verify the stateless access JWT + opaque refresh tokens.
    passwords  — argon2 hashing for break-glass local passwords.
    accounts   — user_accounts / user_sessions DAL + JIT provisioning + bootstrap.
    deps       — AuthContext + request verification (framework-agnostic + FastAPI).
"""

from __future__ import annotations

from robothor.auth.deps import AuthContext, require_access

__all__ = ["AuthContext", "require_access"]
