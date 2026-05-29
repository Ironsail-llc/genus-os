"""Tests for the Rip 4 skill write-origin ContextVar."""

from __future__ import annotations

import asyncio

from robothor.engine.skill_provenance import (
    BACKGROUND_REVIEW,
    get_current_write_origin,
    is_background_review,
    reset_current_write_origin,
    set_current_write_origin,
)


class TestProvenanceBasics:
    def test_default_is_foreground(self) -> None:
        assert get_current_write_origin() == "foreground"
        assert is_background_review() is False

    def test_set_and_reset(self) -> None:
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            assert get_current_write_origin() == BACKGROUND_REVIEW
            assert is_background_review() is True
        finally:
            reset_current_write_origin(token)
        assert get_current_write_origin() == "foreground"
        assert is_background_review() is False

    def test_falsy_origin_becomes_foreground(self) -> None:
        # An empty/None origin must fall back to the default rather
        # than tagging the write as some empty bucket.
        token = set_current_write_origin("")
        try:
            assert get_current_write_origin() == "foreground"
        finally:
            reset_current_write_origin(token)

    def test_custom_origin_string(self) -> None:
        # Future rips may add more sentinels (e.g. "curator"). Verify
        # arbitrary strings are stored verbatim.
        token = set_current_write_origin("curator")
        try:
            assert get_current_write_origin() == "curator"
            assert is_background_review() is False
        finally:
            reset_current_write_origin(token)


class TestAsyncTaskIsolation:
    """The whole point of ContextVar over a thread-local: per-Task scope."""

    def test_sibling_tasks_dont_leak(self) -> None:
        captured: dict[str, str] = {}

        async def task_with_origin(label: str, origin: str) -> None:
            token = set_current_write_origin(origin)
            try:
                await asyncio.sleep(0)
                captured[label] = get_current_write_origin()
            finally:
                reset_current_write_origin(token)

        async def task_without_origin(label: str) -> None:
            await asyncio.sleep(0)
            captured[label] = get_current_write_origin()

        async def main() -> None:
            t1 = asyncio.create_task(task_with_origin("review", BACKGROUND_REVIEW))
            t2 = asyncio.create_task(task_without_origin("plain"))
            t3 = asyncio.create_task(task_with_origin("curator", "curator"))
            await asyncio.gather(t1, t2, t3)

        asyncio.run(main())

        assert captured["review"] == BACKGROUND_REVIEW
        assert captured["plain"] == "foreground"
        assert captured["curator"] == "curator"

    def test_child_task_inherits_parent_origin(self) -> None:
        """A task created inside a set-origin block sees the parent's
        origin (asyncio Task inherits Context)."""
        captured: list[str] = []

        async def child() -> None:
            captured.append(get_current_write_origin())

        async def main() -> None:
            token = set_current_write_origin(BACKGROUND_REVIEW)
            try:
                await asyncio.create_task(child())
            finally:
                reset_current_write_origin(token)

        asyncio.run(main())

        assert captured == [BACKGROUND_REVIEW]
