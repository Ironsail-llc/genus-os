"""Which files hold credentials, so no tool ever opens them for a model.

Measured on this instance over the 48 hours to 2026-09-14: sub-agents read
``~/.config/robothor/connectors.env``, the workspace ``.env``, ``crm/.env``,
a config backup's ``robothor.env`` and ``/etc/robothor/secrets.enc.json``
through ``read_file``. The output redactor caught the values every time —
those were the operator's daily "credential warnings" — but a model has no
use for the file that holds a secret: credentials reach tools through the
platform (env, vault, SOPS), never through the transcript. Refusing the read
removes the exposure instead of cleaning up after it.

The rules are about NAMES and LOCATIONS, deliberately: a denylist that has
to open the file to decide has already lost. Documentation and examples
(``*.example``, ``*.md``) stay readable even when named like the real thing.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import os

#: Exact basenames that always hold credentials.
_SECRET_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".pgpass",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "token.json",
        "client_secret.json",
        "service-account.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "kubeconfig",
    }
)

#: Basename patterns (case-insensitive, whole name).
_SECRET_NAME_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\.env(\.[a-z0-9_-]+)*",  # .env, .env.local, .env.production
        r"[a-z0-9_.-]*\.env",  # robothor.env, connectors.env
        r"secrets?[a-z0-9_.-]*\.(json|ya?ml|env|toml|ini)",  # secrets.json, secrets-prod.yaml
        r"[a-z0-9_.-]*\.enc\.(json|ya?ml)",  # SOPS files
        r"[a-z0-9_.-]*\.(pem|key|p12|pfx|kdbx|keystore|jks)",
        r"id_(rsa|dsa|ecdsa|ed25519)(\.pub)?",
        r"[a-z0-9_.-]*service[-_]account[a-z0-9_.-]*\.json",
    )
)

#: Suffixes that mark a file as documentation of a secret, not the secret.
_DOC_SUFFIXES = (".example", ".sample", ".template", ".md", ".txt", ".rst")

#: Directory names anywhere in the path that hold nothing but credentials.
_SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config-robothor-secrets"})

#: Absolute directories this platform decrypts or stages secrets into.
_SECRET_ROOTS = ("/run/robothor", "/etc/robothor")


def is_secret_path(path: str | os.PathLike[str]) -> bool:
    """True when *path* names a file that holds credentials.

    Pure: no filesystem access, so the answer is the same for a file that
    does not exist (the refusal must not reveal whether it does).
    """
    p = PurePath(Path(str(path)).expanduser())
    name = p.name
    lowered = name.lower()

    if lowered.endswith(_DOC_SUFFIXES):
        return False

    parts = [part.lower() for part in p.parts]
    if any(part in _SECRET_DIRS for part in parts):
        return True
    if p.is_absolute() and any(str(p).startswith(root + "/") for root in _SECRET_ROOTS):
        return True
    # A dot-directory the platform reserves for instance secrets: .robothor/secrets*
    for i, part in enumerate(parts[:-1]):
        if part == ".robothor" and parts[i + 1].startswith("secret"):
            return True

    if lowered in _SECRET_NAMES:
        return True
    return any(pat.fullmatch(name) for pat in _SECRET_NAME_PATTERNS)


REFUSAL = (
    "refused: {name} is a secrets file. Credentials are supplied to tools by the "
    "platform and are never read from disk by an agent; if a value is missing, "
    "say so to the operator instead of reading the file."
)


def refusal_for(path: str | os.PathLike[str]) -> str:
    """The error text a tool returns for a refused path (names the file, never a value)."""
    return REFUSAL.format(name=PurePath(str(path)).name)
