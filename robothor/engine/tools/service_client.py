"""Shared HTTP client for engine tools calling local backing services.

Engine tools depend on loopback services (vision, bridge, voice) that can be
down for legitimate reasons — the operator stopped them, the box just booted,
a GPU wedge. ``call_service`` turns those failures into short, structured,
LLM-actionable errors instead of raw exception text:

- it never echoes the target URL (internal loopback addresses must not leak
  into agent context),
- it logs exactly one ``logger.warning`` line per failure (transport errors
  are operational states, not bugs — no ``logger.exception`` tracebacks),
- a per-service in-process circuit breaker short-circuits repeat dials to a
  dead service: after ``BREAKER_THRESHOLD`` consecutive connect failures the
  cached offline error is returned without dialing for
  ``BREAKER_COOLDOWN_SECONDS``, then one probe is allowed (half-open).

``bridge_headers`` mints (and caches) the short-lived service token the
bridge's fail-closed auth middleware requires — see crm/bridge/middleware.py:
POSTs need ``bridge:write`` and /log-interaction additionally requires the
narrow ``integration:write`` scope, verified against audience "genus-bridge".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 60.0

BRIDGE_AUDIENCE = "genus-bridge"
BRIDGE_SCOPES = ("bridge:read", "bridge:write", "integration:write")

# Mint a fresh token this many seconds before the cached one expires, so a
# request never leaves with a token that dies in flight.
_TOKEN_EXPIRY_MARGIN_SECONDS = 60.0


@dataclass
class _BreakerState:
    consecutive_failures: int = 0
    opened_at: float | None = None


_breakers: dict[str, _BreakerState] = {}

# (service_id, tenant_id, scopes) -> (token, expires_at). Keyed by service_id
# too so one agent's cached identity is never reused for another agent.
_bridge_token_cache: dict[tuple[str, str, tuple[str, ...]], tuple[str, float]] = {}


def reset_circuit_breakers() -> None:
    """Test hook — forget all breaker state."""
    _breakers.clear()


def reset_bridge_token_cache() -> None:
    """Test hook — forget all cached bridge tokens."""
    _bridge_token_cache.clear()


def _offline_error(service: str) -> dict[str, Any]:
    return {"error": f"{service} service offline", "service": service, "retryable": False}


def _record_connect_failure(state: _BreakerState) -> None:
    state.consecutive_failures += 1
    if state.consecutive_failures >= BREAKER_THRESHOLD:
        state.opened_at = time.monotonic()


def _reset_breaker(state: _BreakerState) -> None:
    state.consecutive_failures = 0
    state.opened_at = None


async def call_service(
    service: str,
    method: str,
    url: str,
    *,
    json: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Call a backing service, mapping failures to short structured errors.

    Always returns a dict. Failure payloads carry ``error`` (short, URL-free),
    ``service``, and ``retryable``; success returns the parsed JSON body
    (wrapped as ``{"result": ...}`` when the body is not a JSON object).
    """
    state = _breakers.setdefault(service, _BreakerState())
    if (
        state.opened_at is not None
        and time.monotonic() - state.opened_at < BREAKER_COOLDOWN_SECONDS
    ):
        # Open circuit: short-circuit without dialing. Not a new failure, so
        # no extra warning line — the trip itself was logged. Once the
        # cooldown elapses the next call is allowed through (half-open).
        return {**_offline_error(service), "circuit_open": True}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        _record_connect_failure(state)
        logger.warning("%s service offline (%s)", service, type(e).__name__)
        return _offline_error(service)
    except httpx.TimeoutException as e:
        # The service is up but slow — do not count toward the offline breaker.
        logger.warning("%s service timed out (%s)", service, type(e).__name__)
        return {"error": f"{service} service timed out", "service": service, "retryable": True}
    except httpx.HTTPError as e:
        _record_connect_failure(state)
        logger.warning("%s service request failed (%s)", service, type(e).__name__)
        return {
            "error": f"{service} service request failed ({type(e).__name__})",
            "service": service,
            "retryable": False,
        }

    # Any received response proves the transport works.
    _reset_breaker(state)

    code = resp.status_code
    if code in (401, 403):
        logger.warning("%s rejected engine credentials (HTTP %d)", service, code)
        return {
            "error": f"{service} rejected engine credentials — check service auth config",
            "service": service,
            "retryable": False,
        }
    if code >= 500:
        logger.warning("%s service unavailable (HTTP %d)", service, code)
        return {
            "error": f"{service} service unavailable (HTTP {code})",
            "service": service,
            "retryable": True,
        }
    if code >= 400:
        logger.warning("%s service error (HTTP %d)", service, code)
        return {
            "error": f"{service} service error (HTTP {code})",
            "service": service,
            "retryable": False,
        }

    try:
        data = resp.json()
    except ValueError:
        logger.warning("%s service returned a non-JSON body", service)
        return {
            "error": f"{service} service returned an unreadable response",
            "service": service,
            "retryable": True,
        }
    if isinstance(data, dict):
        return data
    return {"result": data}


def bridge_headers(
    service_id: str,
    tenant_id: str,
    *,
    scopes: tuple[str, ...] = BRIDGE_SCOPES,
) -> dict[str, str]:
    """Authorization header for engine→bridge calls, with token caching.

    Mints via ``issue_service_token`` (shared HS256 key — env or DB vault) and
    caches per (service_id, tenant, scopes) until ~60s before expiry.
    """
    key = (service_id, tenant_id, scopes)
    now = time.time()
    cached = _bridge_token_cache.get(key)
    if cached is not None and now < cached[1]:
        return {"Authorization": f"Bearer {cached[0]}"}

    from robothor.auth.tokens import SERVICE_TTL_SECONDS, issue_service_token

    token = issue_service_token(
        service_id,
        tenant_id,
        audience=BRIDGE_AUDIENCE,
        scopes=scopes,
    )
    _bridge_token_cache[key] = (token, now + SERVICE_TTL_SECONDS - _TOKEN_EXPIRY_MARGIN_SECONDS)
    return {"Authorization": f"Bearer {token}"}
