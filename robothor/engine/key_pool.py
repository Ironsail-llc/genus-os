"""More than one credential per provider, so one dead key is not an outage.

On 2026-08-25 the instance's single OpenRouter key hit its spend cap. Every
model in every fallback chain shares that one credential, so the chain — four
cloud models deep, plus a local tier — bought nothing: all five links failed
identically at the same instant, and the whole fleet stopped until an operator
was awake to top it up. A fallback chain that shares a credential is one link.

A competitive audit of four agent harnesses put reliability engineering third
for this platform, and a credential pool is the specific thing the leaders
have that we did not. This is that seam.

Two retirement reasons, because they are not the same failure:

* **Credit exhausted** is temporary and operator-fixable. The key comes back
  on its own after a cooldown, so topping up the account restores service
  without an engine restart — which is exactly the situation that prompted
  this module.
* **Auth failed** is a revoked or mistyped key. Retrying it forever adds
  latency to every rotation and never succeeds, so it is out for the life of
  the process.

Keys are never logged. Everything that names a credential — status, repr,
alerts — names a fingerprint instead. An OpenRouter key has already leaked
into a bench log through an exception repr once on this instance; rotation
multiplies the number of places a key gets mentioned, so the identifier used
in those places is a one-way hash rather than the usual last-four convention,
which would print real key material.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)

#: How long a credit-exhausted key sits out before it is tried again. Long
#: enough that a capped account is not hammered every few seconds; short
#: enough that a top-up is picked up without anyone touching the engine.
CREDIT_COOLDOWN_SECONDS = 900.0

#: Numbered siblings are walked from _2 upward. The ceiling only stops a
#: pathological environment from being scanned forever.
_MAX_POOL_KEYS = 16


class SecretKey(str):
    """A credential that is a real ``str`` everywhere except ``repr()``.

    structlog's console renderer formats exceptions with
    ``RichTracebackFormatter(show_locals=True)``, which prints every frame
    local through ``repr()``. The engine binds the credential into the frame
    it re-raises from, so a capped key would print in full into the journal —
    73 characters, under rich's 80-character truncation. Masking ``repr`` is
    the only fix that covers every path out of that frame, including the
    ``CancelledError`` that no ``except Exception`` catches.

    Subclassing ``str`` keeps it a working credential: litellm, hashing, and
    equality are all unchanged; only the printed form differs.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<api-key redacted>"


class Retirement(StrEnum):
    """Why a key was taken out of rotation, which decides whether it returns."""

    CREDIT_EXHAUSTED = "credit_exhausted"
    AUTH_FAILED = "auth_failed"


@dataclass(frozen=True)
class KeyStatus:
    """What is knowable about one credential without disclosing it."""

    fingerprint: str
    position: int
    available: bool
    reason: Retirement | None


#: Model-prefix -> the env var holding that provider's credential. Only
#: providers whose pooling has actually been exercised belong here: a model
#: whose prefix is absent gets no pool and keeps litellm's own env
#: resolution, which is the behaviour every deployment has today.
_PROVIDER_KEY_VARS = {
    "openrouter/": "OPENROUTER_API_KEY",
}


def env_var_for_model(model: str) -> str | None:
    """Which credential env var a model authenticates with, if we pool it."""
    for prefix, var in _PROVIDER_KEY_VARS.items():
        if model.startswith(prefix):
            return var
    return None


def keys_from_env(var: str) -> list[str]:
    """The pool for one provider: ``VAR``, then ``VAR_2``, ``VAR_3``, ...

    The walk stops at the first gap rather than scanning the whole range. A
    hole in the sequence is far more likely a typo than a deliberate gap, and
    quietly skipping it hides a key the operator believes is loaded — the
    failure mode being fixed here is precisely "the credential I thought was
    configured was not".
    """
    keys: list[str] = []
    seen: set[str] = set()
    for index in range(1, _MAX_POOL_KEYS + 1):
        name = var if index == 1 else f"{var}_{index}"
        value = os.environ.get(name, "").strip()
        if not value:
            break
        if value not in seen:
            seen.add(value)
            keys.append(value)
    return keys


class KeyPool:
    """An ordered set of interchangeable credentials for one provider.

    Order is the operator's stated preference — usually cheapest or
    highest-limit first — so it is preserved on recovery rather than treated
    as a queue. A key that comes back goes back where it was.
    """

    def __init__(self, keys: list[str], clock: Callable[[], float] = time.monotonic) -> None:
        self._keys = list(keys)
        self._clock = clock
        # key -> (reason, retired_at). Absent means available.
        self._retired: dict[str, tuple[Retirement, float]] = {}

    def fingerprint(self, key: str) -> str:
        """A short, stable, non-reversible name for a key.

        Deliberately not the last four characters. That convention prints
        real key material, and this string is written to logs and alerts.
        """
        return "key-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]

    def _available(self, key: str) -> bool:
        entry = self._retired.get(key)
        if entry is None:
            return True
        reason, retired_at = entry
        if reason is Retirement.AUTH_FAILED:
            return False
        if self._clock() - retired_at >= CREDIT_COOLDOWN_SECONDS:
            # Cooled off. Drop the record so the key resumes its original
            # priority instead of being appended behind the one that
            # replaced it.
            del self._retired[key]
            logger.info(
                "%s returning to rotation after %.0fs cooldown",
                self.fingerprint(key),
                CREDIT_COOLDOWN_SECONDS,
            )
            return True
        return False

    def current(self) -> SecretKey | None:
        """The highest-priority key available right now, or None if all are out.

        Returned masked so that binding it into a caller's frame cannot leak
        it through a rendered traceback.
        """
        for key in self._keys:
            if self._available(key):
                return SecretKey(key)
        return None

    def retire(self, key: str, reason: Retirement) -> None:
        """Take a key out of rotation.

        Idempotent on purpose: two in-flight calls sharing a credential will
        both fail and both report it, and the second report must not burn the
        key that replaced the first.
        """
        if key not in self._keys or key in self._retired:
            return
        self._retired[key] = (reason, self._clock())
        remaining = sum(1 for k in self._keys if self._available(k))
        logger.warning(
            "%s retired (%s); %d of %d credentials still in rotation",
            self.fingerprint(key),
            reason.value,
            remaining,
            len(self._keys),
        )

    def __len__(self) -> int:
        """How many credentials are configured, retired or not."""
        return len(self._keys)

    def exhausted(self) -> bool:
        """Is there nothing left to try? The caller then fails as it does today."""
        return self.current() is None

    def status(self) -> list[KeyStatus]:
        """The whole pool, in priority order, with no key material."""
        return [
            KeyStatus(
                fingerprint=self.fingerprint(key),
                position=index,
                available=self._available(key),
                reason=(self._retired.get(key) or (None, None))[0],
            )
            for index, key in enumerate(self._keys, start=1)
        ]

    def __repr__(self) -> str:
        """Never prints a credential — this object appears in traceback frames."""
        live = sum(1 for k in self._keys if self._available(k))
        return f"<KeyPool {live}/{len(self._keys)} available {self.status()!r}>"
