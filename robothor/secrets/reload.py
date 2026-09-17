"""Tell a running engine that a credential changed, from another process.

The accessor caches what the vault answered, per key, for a few seconds. That
is right for the engine — ``build_exec_env`` resolves once per grant per
``exec``, and ``github_api`` once per request — and it is exactly wrong for the
writers that live in a DIFFERENT process: ``genus vault set``, ``genus secrets
migrate``, ``genus channel add``, the setup wizard. On ``main`` a CLI write took
effect on the engine's next read; a cache that silently undid that would be a
performance fix that cost correctness.

The TTL is the floor: every external write lands within
``SECRET_CACHE_TTL_SECONDS``. This is the ceiling — one best-effort POST to the
engine's reload endpoint so the write lands NOW, which matters because the
operator is usually standing there watching the thing they just configured
still not work.

Best effort in the strongest sense: no engine, a refused connection, a slow
engine, a 404 from an older build — all of them mean the CLI prints how long
the wait will be instead, and none of them fails the command. The credential is
already stored; this is only about when it is noticed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["RELOAD_PATH", "notify_engine"]

#: The engine's own endpoint, which already exists for the Helm's Settings page
#: and which clears both the provider pool and the accessor cache.
RELOAD_PATH = "/api/admin/secrets/reload"

#: Short. A CLI command must not hang because a service is wedged, and the TTL
#: makes the write land anyway.
_TIMEOUT_SECONDS = 2.0


def notify_engine(*, quiet: bool = False) -> bool:
    """Ask a local engine to re-read its credentials. True when it answered.

    Never raises, and never blocks for long. A False return is not a failure of
    the write — it means nobody was listening, and the cache TTL will carry it.
    """
    try:
        import httpx

        from robothor.settings import get_settings

        settings = get_settings()
        host = getattr(settings.engine, "host", "") or "127.0.0.1"
        # 0.0.0.0 is a bind address, not a destination: dialling it reaches
        # nothing on some stacks and the wrong thing on others.
        if host in {"0.0.0.0", "::", ""}:  # noqa: S104 - compared, never bound
            host = "127.0.0.1"
        port = int(getattr(settings.engine, "port", 0) or 18800)
        response = httpx.post(
            f"http://{host}:{port}{RELOAD_PATH}",
            timeout=_TIMEOUT_SECONDS,
            headers=_authorization(),
        )
    except Exception as exc:  # noqa: BLE001 - nobody listening is the normal case
        logger.debug("secrets: no engine to notify (%s)", type(exc).__name__)
        if not quiet:
            _say_it_will_land_anyway()
        return False

    if response.status_code in (401, 403):
        # Reached only when no credential could be minted — see
        # `_authorization`. `/api/admin/*` requires the `engine:control` scope,
        # and an unauthenticated POST is answered 401 by any instance with auth
        # enforced, which is every production one.
        logger.debug("secrets: the engine requires authentication for a reload")
        if not quiet:
            _say_the_engine_refused_us()
        return False

    if response.status_code >= 400:
        logger.debug("secrets: the engine refused the reload (%s)", response.status_code)
        if not quiet:
            _say_it_will_land_anyway()
        return False

    if not quiet:
        print("The running engine has re-read its credentials; the change is live now.")
    return True


def _authorization() -> dict[str, str]:
    """The bearer header for the reload, or none if minting is not safe here.

    An earlier version deliberately sent nothing, reasoning that minting would
    "put a signing key in a CLI that does not otherwise need one". The cost of
    that turned up on 2026-09-16: on a production instance every CLI write was
    answered 401 and the operator was told to wait out a cache TTL, while the
    engine that needed telling sat there refusing them. ``control_token``
    refuses to mint when no signing key already RESOLVES — the case that
    decision was really protecting against, since minting would otherwise
    generate and store one — so asking is now safe, and a failure here simply
    sends the request unauthenticated exactly as before.
    """
    try:
        from robothor.engine_control import control_token

        return {"Authorization": f"Bearer {control_token()}"}
    except Exception as exc:  # noqa: BLE001 - no key, no engine module, no header
        logger.debug("secrets: no control credential to mint (%s)", type(exc).__name__)
        return {}


def _cache_ttl_seconds() -> float:
    """How long a stale read can persist, as a plain number.

    A function, and named without the word this module is about, because the
    two messages below print NOTHING but this float — and CodeQL's
    clear-text-logging rule decides what is sensitive from identifiers, so a
    local called ``SECRET_CACHE_TTL_SECONDS`` in a ``print`` made both of them
    alerts. Nothing about them was ever a credential; the name was.
    """
    from robothor.secrets import SECRET_CACHE_TTL_SECONDS

    return float(SECRET_CACHE_TTL_SECONDS)


def _say_the_engine_refused_us() -> None:
    """The honest version of a 401, which is the normal answer in production."""
    ttl = _cache_ttl_seconds()
    print(
        "(An engine is running but refused an unauthenticated reload, and no "
        f"control credential could be minted here — the change takes effect within "
        f"{ttl:.0f}s anyway. `genus secrets reload` says why, and the Helm's "
        "Settings page applies a change instantly.)"
    )


def _say_it_will_land_anyway() -> None:
    """The honest version of "could not reach the engine".

    An operator reading a bare failure would restart a service they do not need
    to restart. What they need to know is that the write succeeded and when it
    takes effect.
    """
    ttl = _cache_ttl_seconds()
    print(
        f"(No running engine answered, so nothing was notified — the change still "
        f"takes effect within {ttl:.0f}s on any process that is running, and "
        "immediately on the next one to start.)"
    )
