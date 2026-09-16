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

import contextlib
import hashlib
import logging
import os
import secrets as _secrets
import tempfile
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

#: A salt is 32 random bytes. Anything shorter under this name was not written
#: by this code finishing its work -- an empty file from an interrupted write,
#: or a truncated one -- and is treated as damage rather than as a key.
_SALT_BYTES = 32
_MIN_SALT_BYTES = 16

#: How many times to try to place a salt before giving up and degrading. More
#: than one because a loser of the creation race must come back and read the
#: winner's; bounded because a loop that cannot end is worse than a fingerprint
#: that is not private.
_PLACEMENT_ATTEMPTS = 5

_lock = threading.Lock()
_cached_key: bytes | None = None
_warned_unsalted = False


def _workspace() -> Path:
    from robothor.settings.env import process_env_get

    configured = (process_env_get("ROBOTHOR_WORKSPACE", "") or "").strip()
    return Path(configured) if configured else Path.home() / "robothor"


def _read_salt(path: Path) -> bytes | None:
    """The stored salt, or ``None`` when there is nothing usable there yet.

    The length check is the whole point. The first cut re-read the file after
    losing the creation race and returned whatever was there, which in the
    window between ``O_CREAT`` and ``write`` was zero bytes -- so that process
    keyed with ``b""`` for its life and agreed with no other process on the
    box. There is no such window now, but a file left behind by an older build
    still has to be recognised as unusable rather than adopted.
    """
    try:
        found = path.read_bytes()
    except OSError:
        return None
    return found if len(found) >= _MIN_SALT_BYTES else None


def _is_repairable(path: Path) -> bool:
    """Whether what is there is readable AND too short to be a salt.

    Only then may it be replaced. A file we cannot READ is a different
    situation -- a permissions problem, another account's file -- and might
    hold a perfectly good salt that other processes are already using, so
    overwriting it would be the very disagreement this guards against.
    """
    try:
        return len(path.read_bytes()) < _MIN_SALT_BYTES
    except OSError:
        return False


def _place(path: Path, salt: bytes, *, replacing: bool) -> None:
    """Put ``salt`` under ``path`` with no moment at which it is half-written.

    Written to a temporary file in the same directory (``mkstemp`` creates it
    0600, before anything is in it), flushed to disk, and only then given its
    real name in one atomic step. ``os.link`` fails if the name is taken, so
    the first writer wins and a loser adopts the winner's salt instead of
    overwriting it -- which is what keeps every process on the box agreeing.

    ``replacing`` swaps that for ``os.rename``, for the single case where the
    file already there has been read and found unusable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=_SALT_FILENAME + ".")
    try:
        os.write(descriptor, salt)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if replacing:
            Path(temporary).rename(path)
        else:
            os.link(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            Path(temporary).unlink()


def _degrade() -> bytes:
    """No salt could be stored. Say so once, and keep working."""
    global _warned_unsalted  # noqa: PLW0603

    if not _warned_unsalted:
        _warned_unsalted = True
        logger.info(
            "secrets: no fingerprint salt could be stored under %s, so credential "
            "fingerprints on this instance are computable from the published source. "
            "They still compare correctly here; they are not private.",
            _SALT_FILENAME,
        )
    return _UNSALTED_FALLBACK


def _read_or_create_salt() -> bytes:
    """This instance's fingerprint salt, creating it on first use.

    Never raises. A fingerprint is used on the status path, in tool results and
    in the doctor; a filesystem problem must degrade it, not take those down.
    """
    path = _workspace() / _SALT_FILENAME

    for attempt in range(_PLACEMENT_ATTEMPTS):
        found = _read_salt(path)
        if found is not None:
            return found

        last = attempt == _PLACEMENT_ATTEMPTS - 1
        try:
            # Repair, rather than degrade, on the final attempt: a short or
            # empty salt is no good to ANY process, so replacing it makes them
            # all agree again, while degrading leaves each one keyed
            # differently until it restarts.
            _place(path, _secrets.token_bytes(_SALT_BYTES), replacing=last and _is_repairable(path))
        except FileExistsError:
            continue  # another process won the race; read its salt next time round
        except OSError:
            break

        # Re-read rather than trusting what we just wrote: if two processes
        # repaired at the same moment, the file is the one answer both can
        # agree on.
        placed = _read_salt(path)
        if placed is not None:
            return placed

    return _degrade()


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
