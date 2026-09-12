"""The one-shot credential that gets a browser into the first-run wizard.

A fresh install has no account, so ``/setup`` cannot sit behind a session —
and a page that can create the owner account, store provider credentials and
install agents must not be reachable by anyone who can route to the box. The
answer is the smallest credential that works: ``genus init`` mints a random
token, prints it once in the operator's terminal as part of a URL, and stores
only its SHA-256 digest.

Four properties, and each one closes a way this could have gone wrong:

**The plaintext exists in one place.** It is returned to the caller and never
written. Reading ``setup_token.yaml`` — a backup, a bug report, another local
user — yields a digest that cannot re-issue the token.

**The comparison is constant-time.** ``POST /api/setup/claim`` is public and
unauthenticated, so a comparison that stops at the first wrong byte is a
digest oracle. :func:`verify_setup_token` runs ``hmac.compare_digest`` on
every path, including the one where there is no file at all, so the clock does
not answer "has anyone run init here?" either.

**Expiry and single use are enforced here, not by the caller.** A 30-minute
window and one use: a token that lives in a terminal scrollback forever is a
permanent back door into the appliance.

**Two questions, not one.** :func:`setup_complete` asks the DATABASE whether an
owner account exists; it deliberately does not look at any file, because a
file-backed signal is a first-run wizard that anyone who can delete a file can
re-open, and re-opening it means a stranger creating the second owner account.
An unreadable database reads as *complete*, because "nobody knows" must not
publish a public write surface.

But that answer becomes true in the MIDDLE of the ceremony — the operator step
is what creates the owner row — so it cannot also be what closes the wizard's
later steps. :func:`setup_recorded` is that second question: a
``setup_completed_at`` marker written by the last step. ``claim`` and
``operator`` gate on the database (they must not run twice); everything after
them gates on the marker, and needs a claim that only the database gate could
have issued. The marker is additive, never a replacement.

The file lives at ``<workspace>/.robothor/setup_token.yaml``. That is the
workspace's private directory, not ``~/.robothor`` — the operator identity
belongs to the person and the setup token belongs to one installation.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "SETUP_COMPLETED_KEY",
    "configured_ttl_seconds",
    "consume_setup_token",
    "create_setup_token",
    "is_loopback_host",
    "port_forward_hint",
    "record_setup_completed",
    "setup_complete",
    "setup_recorded",
    "setup_link",
    "token_path",
    "verify_setup_token",
]

#: The window a printed link stays usable for. Long enough to read a terminal,
#: find a browser and type a password; short enough that a scrollback is not a
#: standing credential. The declared setting ``GENUS_SETUP_TOKEN_TTL_SECONDS``
#: carries the same default, and :func:`create_setup_token` reads it — a
#: constant the setting could not override would make the setting inert.
DEFAULT_TTL_SECONDS = 1800

#: Bytes of entropy in the token. 32 bytes, urlsafe-base64 encoded, is 43
#: characters: short enough to survive a copy out of a terminal, far past any
#: online guess against a route that answers five attempts a minute.
_TOKEN_BYTES = 32

_CONFIG_DIRNAME = ".robothor"
_TOKEN_FILENAME = "setup_token.yaml"
_CONFIG_FILENAME = "config.yaml"

#: Top-level key in config.yaml recording that the wizard finished. Top level,
#: not inside ``settings:``, because that block is validated against the
#: settings registry and an undeclared key in it is rejected under strict mode.
SETUP_COMPLETED_KEY = "setup_completed_at"

#: Mode for the token file and for the directory holding it. Anything wider is
#: a credential every local account can read.
_FILE_MODE = 0o600
_DIR_MODE = 0o700

#: A digest no token will ever hash to, used so the "no file" and "malformed
#: file" branches still pay for one ``compare_digest`` call. Constant-time
#: against a constant is not about secrecy — it is about the branches costing
#: the same from outside.
_ABSENT_DIGEST = "0" * 64

#: Hosts a browser on the operator's own machine can already reach, so no
#: port-forward line is needed.
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def token_path(workspace: Path | str) -> Path:
    """Where this installation keeps its setup token digest."""
    return Path(workspace) / _CONFIG_DIRNAME / _TOKEN_FILENAME


def configured_ttl_seconds() -> int:
    """The instance's declared token lifetime, or the default.

    Wrapped so a config.yaml that does not parse cannot stop ``genus init``
    from printing a link: init is the command an operator runs to FIX such a
    box, and a hard failure here would leave them with no way in at all.
    """
    try:
        from robothor.settings import get_settings

        return int(get_settings().auth.setup_token_ttl_seconds)
    except Exception:  # noqa: BLE001 - a broken config must not block first run
        logger.debug("setup_token: settings unavailable; using the default TTL", exc_info=True)
        return DEFAULT_TTL_SECONDS


def create_setup_token(workspace: Path | str, *, ttl_seconds: int | None = None) -> str:
    """Mint a setup token, store its digest, and return the plaintext once.

    ``ttl_seconds`` defaults to ``GENUS_SETUP_TOKEN_TTL_SECONDS`` (itself 1800).
    The return value is the ONLY copy of the token; nothing here logs it, and
    the file it writes could not produce it again.

    Any existing token is replaced. Re-running ``genus init`` is routine, and
    an operator who has lost the printed link needs the next one to work.
    """
    ttl = configured_ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
    if ttl <= 0:
        raise ValueError("ttl_seconds must be positive")

    token = secrets.token_urlsafe(_TOKEN_BYTES)
    now = datetime.now(UTC)
    document = {
        "token_sha256": _digest(token),
        "created_at": _stamp(now),
        "expires_at": _stamp(now + timedelta(seconds=ttl)),
        "consumed": False,
    }
    _write(token_path(workspace), document)
    return token


def verify_setup_token(workspace: Path | str, token: str) -> bool:
    """Whether *token* is this installation's live, unconsumed setup token.

    False for a wrong token, an expired one, a consumed one, a malformed file
    and no file at all — and the caller must answer all five with the same
    generic 401, because the difference between them is information about the
    box that an unauthenticated caller has no business learning.
    """
    document = _read(workspace)
    stored = document.get("token_sha256") if document else None
    expected = stored if isinstance(stored, str) and len(stored) == 64 else _ABSENT_DIGEST

    # Always compared, even with nothing on disk: the branch structure must not
    # be visible in the response time.
    matches = hmac.compare_digest(expected, _digest(token))

    if document is None or not isinstance(stored, str) or not token:
        return False
    if document.get("consumed") is True:
        return False
    if _expired(document.get("expires_at")):
        return False
    return bool(matches)


def consume_setup_token(workspace: Path | str) -> bool:
    """Mark the stored token spent. True when a document was updated.

    Called once the wizard has created the owner account: from then on the
    printed link is dead even though the terminal it was printed in is not.
    """
    document = _read(workspace)
    if document is None:
        return False
    if document.get("consumed") is True:
        return True
    document["consumed"] = True
    document["consumed_at"] = _stamp(datetime.now(UTC))
    _write(token_path(workspace), document)
    return True


def setup_complete(workspace: Path | str | None = None) -> bool:
    """Whether this instance has finished first-run setup.

    The signal is an owner account in the database. ``workspace`` is accepted
    for symmetry with the rest of this module and is deliberately UNUSED: no
    file on disk decides this, because a file can be deleted and the wizard
    must not come back.

    A database that cannot be reached reads as complete. That direction is the
    fail-closed one: the alternative publishes ``/api/setup/*`` — routes that
    create the owner account and write provider credentials — whenever
    PostgreSQL is down.
    """
    del workspace  # the database is the only signal; see the docstring
    from robothor.auth import accounts

    try:
        return bool(accounts.owner_account_exists())
    except Exception:  # noqa: BLE001 - unreachable is not "no owner"
        logger.warning(
            "setup_token: could not ask the database whether an owner exists; "
            "treating setup as complete so the first-run routes stay closed",
            exc_info=True,
        )
        return True


def setup_recorded(workspace: Path | str) -> bool:
    """Whether the wizard has recorded that it FINISHED.

    A different question from :func:`setup_complete`, and keeping them apart is
    the whole reason this function exists. "Has this instance got an owner?"
    becomes true in the MIDDLE of the ceremony — the operator step creates that
    row — so gating the wizard's later steps on it makes the wizard kill itself
    at step 2: the provider, channel, agent and complete routes all vanish, the
    browser holding a live claim sees 404 on every call, and
    ``setup_completed_at`` is never written on any real install.

    So the later steps close on THIS instead: a ``setup_completed_at`` key at
    the top level of ``config.yaml``, written by ``POST /api/setup/complete``.

    That is only safe because it is additive, never a replacement. ``claim`` and
    ``operator`` stay gated on :func:`setup_complete` — the database — so an
    attacker who deletes ``config.yaml`` to clear this marker cannot mint a new
    claim on an instance that has an owner, and the routes below it require one.

    An unreadable or unparseable file reads as RECORDED, i.e. closed. Missing is
    not unreadable: a file that is simply not there yet is the fresh-install
    case and reads as "still in setup".
    """
    path = _config_path(workspace)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("setup_token: %s could not be read; treating setup as finished", path)
        return True
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        logger.warning("setup_token: %s does not parse; treating setup as finished", path)
        return True
    if not isinstance(document, dict):
        return True
    return bool(document.get(SETUP_COMPLETED_KEY))


def record_setup_completed(workspace: Path | str) -> str:
    """Write ``setup_completed_at`` and return the stamp.

    Top level of ``config.yaml``, deliberately NOT inside ``settings:``: that
    block is validated against the registry, and under
    ``config_strict_mode: enforce`` an undeclared key in it is rejected by name
    — a marker there would stop a freshly completed instance from starting.
    """
    from robothor.settings.config_file import write_top_level

    stamp = _stamp(datetime.now(UTC))
    write_top_level(SETUP_COMPLETED_KEY, stamp, path=_config_path(workspace))
    return stamp


def _config_path(workspace: Path | str) -> Path:
    return Path(workspace) / _CONFIG_DIRNAME / _CONFIG_FILENAME


def setup_link(host: str, port: int, token: str) -> str:
    """The URL the operator opens, with the token percent-encoded."""
    return f"http://{_authority(host, port)}/setup?token={quote(token, safe='')}"


def is_loopback_host(host: str) -> bool:
    """Whether a browser on the operator's own machine can reach ``host``.

    ``0.0.0.0`` is deliberately NOT loopback: it is a bind address, not a
    destination, and an operator handed ``http://0.0.0.0:3004`` gets a page
    that may or may not load depending on their OS.
    """
    candidate = (host or "").strip().lower()
    if candidate in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate.strip("[]")).is_loopback
    except ValueError:
        return False


def port_forward_hint(host: str, port: int) -> str:
    """The ``ssh -L`` line that makes a headless box's wizard reachable."""
    return f"ssh -L {port}:127.0.0.1:{port} {host}"


# ── internals ────────────────────────────────────────────────────────


def _authority(host: str, port: int) -> str:
    """``host:port``, with an IPv6 literal bracketed so the URL parses."""
    candidate = (host or "").strip()
    bare = candidate.strip("[]")
    try:
        needs_brackets = isinstance(ipaddress.ip_address(bare), ipaddress.IPv6Address)
    except ValueError:
        needs_brackets = False
    return f"[{bare}]:{port}" if needs_brackets else f"{candidate}:{port}"


def _digest(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _stamp(moment: datetime) -> str:
    """An ISO-8601 instant in UTC, ``Z``-suffixed.

    Explicit UTC rather than a local-time stamp: the file is read by a service
    that may run in a different timezone than the CLI that wrote it, and a
    naive stamp would make a 30-minute window anything from -12 to +14 hours.
    """
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _expired(raw: Any) -> bool:
    """Whether ``expires_at`` is in the past. Unparseable reads as expired."""
    if not isinstance(raw, str):
        return True
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return datetime.now(UTC) >= moment


def _read(workspace: Path | str) -> dict[str, Any] | None:
    """The stored document, or None for anything that is not one."""
    try:
        text = token_path(workspace).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        logger.warning("setup_token: %s does not parse as YAML", token_path(workspace))
        return None
    return loaded if isinstance(loaded, dict) else None


def _write(path: Path, document: dict[str, Any]) -> None:
    """Replace the token file in one step, private to the operator.

    Atomic because a torn write is a box with a token nobody can use and no
    way to tell that from a box with no token. The mode is set on the temp file
    BEFORE the rename, so the digest is never briefly world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(_DIR_MODE)
    except OSError:  # pragma: no cover - a pre-existing dir we do not own
        logger.debug("setup_token: could not tighten %s", path.parent)

    descriptor, name = tempfile.mkstemp(dir=str(path.parent), prefix=".setup_token.", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                "# Digest of this installation's first-run setup token.\n"
                "# Written by `genus init` / `genus auth setup-link`. The token itself\n"
                "# is printed once and never stored: this file cannot re-issue it.\n"
                + yaml.safe_dump(document, sort_keys=False)
            )
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(_FILE_MODE)
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
