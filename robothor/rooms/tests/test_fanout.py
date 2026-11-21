"""``room_lock``: one agent turn per room at a time."""

from __future__ import annotations

import asyncio
import gc
import uuid

from robothor.rooms.fanout import room_lock


def test_one_room_gets_one_lock_and_two_rooms_two() -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    lock = room_lock(a)
    assert room_lock(a) is lock
    assert room_lock(b) is not lock


def test_a_lock_nobody_holds_is_not_kept_forever() -> None:
    from robothor.rooms import fanout

    room = str(uuid.uuid4())
    room_lock(room)
    gc.collect()
    assert room not in fanout._locks


async def test_a_held_lock_is_the_one_every_later_caller_gets() -> None:
    room = str(uuid.uuid4())
    async with room_lock(room) as _:
        gc.collect()
        assert room_lock(room).locked()


async def test_two_turns_in_one_room_run_one_after_the_other() -> None:
    room = str(uuid.uuid4())
    order: list[str] = []

    async def turn(name: str) -> None:
        async with room_lock(room):
            order.append(f"{name}-start")
            await asyncio.sleep(0.01)
            order.append(f"{name}-end")

    await asyncio.gather(turn("a"), turn("b"))
    assert order in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )


async def test_turns_in_different_rooms_do_not_wait_for_each_other() -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    async with room_lock(a):
        assert not room_lock(b).locked()
