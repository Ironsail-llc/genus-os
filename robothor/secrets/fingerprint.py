"""A short, stable, non-reversible name for a credential.

The question an operator and an assistant both need to answer without anybody
reading a credential is "which one is stored?" — after a rotation, between two
stores, across the Helm page and a tool result. A fingerprint answers it: the
same value gives the same eight characters everywhere on this instance, a
different value does not, and nothing about the value can be recovered from
them.

**Keyed, and keyed PER INSTANCE.** The first cut was HMAC-SHA256 under a
constant compiled into the source, which is a key every reader of the
repository has. Against a high-entropy token that is fine; against a short or
low-entropy secret it is no better than a bare digest, because an attacker
holding a leaked fingerprint can compute candidates offline at the same speed
we can. CodeQL called that out as ``py/weak-sensitive-data-hashing`` and was
right to. The key is now derived from a random salt this instance generates on
first use, so a fingerprint is meaningless to anyone who does not already have
the instance's own file.

BLAKE2b's native keyed mode rather than HMAC-SHA256: it is built for exactly
this, it is faster, and it is not on anyone's list of constructions to argue
about. The printed label moves with it, from ``sha256:`` to ``b2:`` — a digest
whose prefix names the wrong algorithm is a small lie somebody eventually
relies on.

**Comparability.** Fingerprints are compared within one instance — the vault
row against the environment copy, the value handed over against the value
stored — and across its processes, so the salt is per instance and persistent,
never per process. It is NOT comparable between instances, which is the point:
two boxes holding the same credential produce different fingerprints, and
neither can confirm the other's.

This lives in ``robothor.secrets`` rather than in ``engine/key_pool`` (where it
was born) because four surfaces print one digest: the provider API, the settings
API, the vault tools and ``genus secrets status``. Four implementations of one
digest is four digests the moment one of them is "improved", and a fingerprint
whose value depends on which code path printed it answers nothing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets as _secrets
import stat
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["FINGERPRINT_PREFIX", "fingerprint", "reset_fingerprint_key"]

FINGERPRINT_PREFIX = "b2:"

#: How many hex characters of the digest are shown. Eight is enough to tell two
#: credentials apart by eye and far too few to be worth attacking directly; the
#: per-instance key is what makes the short form safe rather than the length.
_DIGEST_CHARS = 8

#: Where the instance keeps its fingerprint salt. Beside the vault master key,
#: because it has the same lifetime and the same "belongs to this instance"
#: property — but it is NOT the master key: deriving from that would mean no
#: process could fingerprint anything without being able to decrypt everything,
#: and the CLI, the doctor and the status table all fingerprint without a vault.
_SALT_FILENAME = ".fingerprint-salt"

#: Used when no salt can be written — a read-only filesystem, a workspace that
#: does not exist, a container with no persistent volume. Deliberately NOT
#: silent: an instance in this state still produces stable, comparable
#: fingerprints, but they are computable by anyone with the source, so it says
#: so once.
_UNSALTED_FALLBACK = b"genus-fingerprint-unsalted"

_lock = threading.Lock()
_cached_key: bytes | None = None
_warned_unsalted = False


def _workspace() -> Path:
    from robothor.settings.env import process_env_get

    configured = (process_env_get("ROBOTHOR_WORKSPACE", "") or "").strip()
    return Path(configured) if configured else Path.home() / "robothor"


def _read_or_create_salt() -> bytes:
    """This instance's fingerprint salt, creating it on first use.

    Never raises. A fingerprint is used on the status path, in tool results and
    in the doctor; a filesystem problem must degrade it, not take those down.
    """
    global _warned_unsalted  # noqa: PLW0603

    path = _workspace() / _SALT_FILENAME
    try:
        if path.is_file():
            found = path.read_bytes()
            if len(found) >= 16:
                return found
    except OSError:
        pass

    salt = _secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written 0600 before anything is in it: a salt readable by another
        # account would put this instance's fingerprints back within reach of
        # an offline comparison.
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR
        )
        try:
            os.write(descriptor, salt)
        finally:
            os.close(descriptor)
        return salt
    except FileExistsError:
        # Another process won the race; its salt is the instance's salt.
        try:
            return path.read_bytes()
        except OSError:
            pass
    except OSError:
        pass

    if not _warned_unsalted:
        _warned_unsalted = True
        logger.info(
            "secrets: no fingerprint salt could be stored under %s, so credential "
            "fingerprints on this instance are computable from the published source. "
            "They still compare correctly here; they are not private.",
            _SALT_FILENAME,
        )
    return _UNSALTED_FALLBACK


def _key() -> bytes:
    """The keyed-digest key for this process, read once."""
    global _cached_key  # noqa: PLW0603
    if _cached_key is None:
        with _lock:
            if _cached_key is None:
                _cached_key = _read_or_create_salt()
    return _cached_key


def reset_fingerprint_key() -> None:
    """Forget the cached salt. For the suite, and after a workspace change."""
    global _cached_key  # noqa: PLW0603
    _cached_key = None


def fingerprint(value: str) -> str:
    """``b2:xxxxxxxx`` for ``value``. Safe to print, log and return.

    Stable for the life of the instance and meaningless outside it.
    """
    digest = hashlib.blake2b(value.encode("utf-8"), key=_key(), digest_size=16).hexdigest()
    return FINGERPRINT_PREFIX + digest[:_DIGEST_CHARS]
