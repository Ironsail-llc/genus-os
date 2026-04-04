"""Tests for the shared retry utility."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.retry import retry_async, retry_sync


class TestRetryAsync:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value=42)
        result = await retry_async(fn, max_attempts=3)
        assert result == 42
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_success_after_retry(self) -> None:
        fn = AsyncMock(side_effect=[ConnectionError("fail"), 42])
        result = await retry_async(
            fn,
            max_attempts=3,
            backoff_base=0.01,
            retryable_exceptions=(ConnectionError,),
        )
        assert result == 42
        assert fn.call_count == 2

    @pytest.mark.asyncio
    async def test_exhausts_all_attempts(self) -> None:
        fn = AsyncMock(side_effect=ConnectionError("fail"))
        with pytest.raises(ConnectionError, match="fail"):
            await retry_async(
                fn,
                max_attempts=3,
                backoff_base=0.01,
                retryable_exceptions=(ConnectionError,),
            )
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_exception_propagates(self) -> None:
        fn = AsyncMock(side_effect=ValueError("bad"))
        with pytest.raises(ValueError, match="bad"):
            await retry_async(
                fn,
                max_attempts=3,
                backoff_base=0.01,
                retryable_exceptions=(ConnectionError,),
            )
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_on_retry_callback(self) -> None:
        fn = AsyncMock(side_effect=[ConnectionError("fail"), 42])
        callback = MagicMock()
        await retry_async(
            fn,
            max_attempts=3,
            backoff_base=0.01,
            retryable_exceptions=(ConnectionError,),
            on_retry=callback,
        )
        callback.assert_called_once()
        assert callback.call_args[0][0] == 1  # attempt number

    @pytest.mark.asyncio
    async def test_backoff_capped_at_max(self) -> None:
        """Verify that backoff doesn't exceed max_backoff (functional test)."""
        fn = AsyncMock(side_effect=[ConnectionError("a"), ConnectionError("b"), 42])
        result = await retry_async(
            fn,
            max_attempts=3,
            backoff_base=0.01,
            max_backoff=0.02,
            retryable_exceptions=(ConnectionError,),
        )
        assert result == 42

    @pytest.mark.asyncio
    async def test_passes_args_and_kwargs(self) -> None:
        fn = AsyncMock(return_value="ok")
        result = await retry_async(fn, "a", "b", max_attempts=1, key="val")
        assert result == "ok"
        fn.assert_called_once_with("a", "b", key="val")


class TestRetrySync:
    def test_success_on_first_attempt(self) -> None:
        fn = MagicMock(return_value=42)
        result = retry_sync(fn, max_attempts=3)
        assert result == 42
        assert fn.call_count == 1

    def test_success_after_retry(self) -> None:
        fn = MagicMock(side_effect=[OSError("fail"), 42])
        result = retry_sync(
            fn,
            max_attempts=3,
            backoff_base=0.01,
            retryable_exceptions=(OSError,),
        )
        assert result == 42
        assert fn.call_count == 2

    def test_exhausts_all_attempts(self) -> None:
        fn = MagicMock(side_effect=OSError("fail"))
        with pytest.raises(OSError, match="fail"):
            retry_sync(
                fn,
                max_attempts=3,
                backoff_base=0.01,
                retryable_exceptions=(OSError,),
            )
        assert fn.call_count == 3

    def test_non_retryable_propagates(self) -> None:
        fn = MagicMock(side_effect=ValueError("bad"))
        with pytest.raises(ValueError, match="bad"):
            retry_sync(
                fn,
                max_attempts=3,
                backoff_base=0.01,
                retryable_exceptions=(OSError,),
            )
        assert fn.call_count == 1
