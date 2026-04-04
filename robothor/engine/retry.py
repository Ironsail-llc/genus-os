"""Shared retry utilities for the Agent Engine.

Provides both async and sync retry wrappers with exponential backoff.
Pattern follows hub_client.py and telegram.py _retry_on_flood().

Usage::

    from robothor.engine.retry import retry_async, retry_sync

    # Async (DB operations via run_in_executor, HTTP calls, etc.)
    result = await retry_async(
        some_async_fn,
        max_attempts=3,
        retryable_exceptions=(httpx.TimeoutException, ConnectionError),
    )

    # Sync (direct DB operations in tracking.py, audit/logger.py, etc.)
    result = retry_sync(
        lambda: create_run(run),
        max_attempts=3,
        retryable_exceptions=(psycopg2.OperationalError,),
    )
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable  # noqa: TC003
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_RETRYABLE = (ConnectionError, TimeoutError, OSError)


async def retry_async(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    max_backoff: float = 30.0,
    retryable_exceptions: tuple[type[BaseException], ...] = _DEFAULT_RETRYABLE,
    on_retry: Callable[[int, BaseException], Any] | None = None,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff.

    Args:
        fn: Async callable to invoke.
        max_attempts: Total attempts (1 = no retry).
        backoff_base: Initial backoff in seconds (doubles each retry).
        max_backoff: Maximum backoff cap in seconds.
        retryable_exceptions: Exception types that trigger a retry.
        on_retry: Optional callback(attempt, exception) called before each retry sleep.

    Returns:
        The return value of *fn*.

    Raises:
        The last exception if all attempts fail.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except retryable_exceptions as e:
            last_exc = e
            if attempt >= max_attempts:
                break
            delay = min(backoff_base * (2 ** (attempt - 1)), max_backoff) * random.uniform(0.5, 1.0)
            if on_retry:
                try:
                    on_retry(attempt, e)
                except Exception:
                    logger.warning("on_retry callback failed", exc_info=True)
            else:
                logger.warning(
                    "Retry %d/%d after %s (%.1fs backoff): %s",
                    attempt,
                    max_attempts,
                    type(e).__name__,
                    delay,
                    e,
                )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def retry_sync(
    fn: Callable[..., T],
    *args: Any,
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    max_backoff: float = 30.0,
    retryable_exceptions: tuple[type[BaseException], ...] = _DEFAULT_RETRYABLE,
    on_retry: Callable[[int, BaseException], Any] | None = None,
    **kwargs: Any,
) -> T:
    """Retry a sync callable with exponential backoff.

    Same interface as :func:`retry_async` but uses ``time.sleep``.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except retryable_exceptions as e:
            last_exc = e
            if attempt >= max_attempts:
                break
            delay = min(backoff_base * (2 ** (attempt - 1)), max_backoff) * random.uniform(0.5, 1.0)
            if on_retry:
                try:
                    on_retry(attempt, e)
                except Exception:
                    logger.warning("on_retry callback failed", exc_info=True)
            else:
                logger.warning(
                    "Retry %d/%d after %s (%.1fs backoff): %s",
                    attempt,
                    max_attempts,
                    type(e).__name__,
                    delay,
                    e,
                )
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]
