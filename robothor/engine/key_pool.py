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

#: How long a key capped on a CALENDAR window (weekly/daily/monthly quota)
#: sits out. The short credit cooldown is wrong here: a spend cap clears the
#: moment an operator tops up, but a weekly cap clears when the provider says
#: so and not before. On 2026-08-27 the fleet retried a weekly cap every 900s
#: — ~96 revivals a day, each firing a fresh burst of 403s through every
#: agent's fallback chain. That retry loop was the outage the operator
#: actually experienced, far more than the missing capacity itself.
PERIODIC_QUOTA_COOLDOWN_SECONDS = float(
    os.environ.get("ROBOTHOR_PERIODIC_QUOTA_COOLDOWN_SECONDS", 6 * 60 * 60)
)

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
    QUOTA_EXHAUSTED_PERIODIC = "quota_exhausted_periodic"
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

    def __init__(
        self,
        keys: list[str],
        clock: Callable[[], float] = time.monotonic,
        on_exhausted: Callable[[Retirement], None] | None = None,
    ) -> None:
        self._keys = list(keys)
        self._clock = clock
        # key -> (reason, retired_at). Absent means available.
        self._retired: dict[str, tuple[Retirement, float]] = {}
        # Fired when the LAST key goes out, so the operator gets one page
        # per outage instead of one log line per skipped model. Latched so
        # a sustained outage does not re-page on every lookup, and re-armed
        # the moment any key returns — a second outage is a second page.
        self._on_exhausted = on_exhausted
        self._exhaustion_announced = False

    def fingerprint(self, key: str) -> str:
        """A short, stable, non-reversible name for a key.

        Deliberately not the last four characters. That convention prints
        real key material, and this string is written to logs and alerts.
        """
        return "key-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def _cooldown_for(reason: Retirement) -> float:
        """How long this retirement reason sits out before a retry.

        Keyed on the reason because the reasons recover differently: a spend
        cap is operator-fixable in a minute, a calendar quota is not fixable
        at all until the window rolls.
        """
        if reason is Retirement.QUOTA_EXHAUSTED_PERIODIC:
            return PERIODIC_QUOTA_COOLDOWN_SECONDS
        return CREDIT_COOLDOWN_SECONDS

    def _available(self, key: str) -> bool:
        entry = self._retired.get(key)
        if entry is None:
            return True
        reason, retired_at = entry
        if reason is Retirement.AUTH_FAILED:
            return False
        cooldown = self._cooldown_for(reason)
        if self._clock() - retired_at >= cooldown:
            # Cooled off. Drop the record so the key resumes its original
            # priority instead of being appended behind the one that
            # replaced it.
            del self._retired[key]
            self._exhaustion_announced = False
            logger.info(
                "%s returning to rotation after %.0fs cooldown (%s)",
                self.fingerprint(key),
                cooldown,
                reason.value,
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
        if remaining == 0 and not self._exhaustion_announced:
            self._exhaustion_announced = True
            if self._on_exhausted is not None:
                try:
                    self._on_exhausted(reason)
                except Exception:
                    # Alerting is best-effort. A pager that is itself down
                    # must not take the LLM path down with it — that would
                    # convert a degraded fleet into a stopped one.
                    logger.exception("exhaustion callback failed")
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


# ── Process-wide pools ──────────────────────────────────────────────
#
# A credential is a property of the process, not of whoever happens to hold
# a client object. Before this, LLMClient cached pools per instance and
# memory/generation kept its own, so retiring a key in one left the other
# still dialling a credential the provider had already rejected. On
# 2026-08-27 that is precisely what kept 403s flowing after the engine's own
# pool had correctly given up.

_SHARED: dict[str, KeyPool] = {}


def reset_shared_pools() -> None:
    """Drop every cached pool. For tests and for a secrets reload."""
    _SHARED.clear()


def shared_pool(
    var: str, on_exhausted: Callable[[Retirement], None] | None = None
) -> KeyPool | None:
    """The one pool for ``var`` in this process, or None if unconfigured.

    Built lazily: secrets land in tmpfs after import, so a pool constructed at
    module scope would be permanently empty on a real box. Returning None for
    an unconfigured provider preserves today's behaviour — litellm resolves
    the environment itself — rather than reporting an empty pool "exhausted"
    and skipping every model on it.
    """
    pool = _SHARED.get(var)
    if pool is None:
        keys = keys_from_env(var)
        if not keys:
            return None
        pool = KeyPool(keys, on_exhausted=on_exhausted)
        _SHARED[var] = pool
    return pool


def api_key_for_model(model: str) -> str | None:
    """The credential this model should authenticate with right now.

    None means "not pooled, or nothing left in rotation" — callers should then
    fall through to their existing behaviour rather than inventing one.
    """
    var = env_var_for_model(model)
    if var is None:
        return None
    pool = shared_pool(var)
    if pool is None:
        return None
    key = pool.current()
    return str(key) if key is not None else None


def retire_for_model(model: str, key: str, reason: Retirement) -> None:
    """Take a credential out of rotation for every caller in this process."""
    var = env_var_for_model(model)
    if var is None:
        return
    pool = shared_pool(var)
    if pool is not None:
        pool.retire(key, reason)
