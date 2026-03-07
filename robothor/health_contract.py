"""
Standardized health endpoint contract — shared response builder.

All services implement:
- /health (liveness): 200 if process running. No dep checks. Never blocks.
- /ready (readiness): 200 if all deps reachable, 503 with details if degraded.

Response shape:
    {status, service, version, timestamp, checks: {component: "ok"|"error:..."}}
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


def liveness_response(service: str, version: str) -> dict[str, Any]:
    """Build a liveness response. Always 200. No dependency checks."""
    return {
        "status": "ok",
        "service": service,
        "version": version,
        "timestamp": datetime.now(UTC).isoformat(),
    }


async def readiness_response(
    service: str,
    version: str,
    checks: dict[str, Callable[[], Awaitable[str]]],
) -> tuple[dict[str, Any], int]:
    """Build a readiness response by running all dependency checks.

    Each check callable should return "ok" or "error:..." string.
    Returns (response_dict, status_code) — 200 if all ok, 503 if degraded.
    """
    results: dict[str, str] = {}
    for name, check_fn in checks.items():
        try:
            results[name] = await asyncio.wait_for(check_fn(), timeout=5.0)
        except TimeoutError:
            results[name] = "error:timeout"
        except Exception as e:
            results[name] = f"error:{e}"

    all_ok = all(v == "ok" for v in results.values())
    status_code = 200 if all_ok else 503

    return {
        "status": "ok" if all_ok else "degraded",
        "service": service,
        "version": version,
        "timestamp": datetime.now(UTC).isoformat(),
        "checks": results,
    }, status_code


async def wait_for_ready(
    url: str,
    timeout: float = 30.0,
    backoff: float = 1.0,
    max_backoff: float = 10.0,
) -> bool:
    """Wait for a service's /ready endpoint to return 200.

    Polls with exponential backoff. Returns True if ready within timeout.
    Used for startup gating in the engine daemon.
    """
    import httpx

    deadline = asyncio.get_event_loop().time() + timeout
    delay = backoff

    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{url}/ready")
                if resp.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(min(delay, max_backoff))
        delay *= 2

    return False
