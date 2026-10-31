"""Execution lanes: a person's turn never queues behind background work.

Every background loop the daemon starts (scheduler, hooks, watchdog, autodream,
curator, curiosity, capacity) shares one event loop and the loop's one default
thread pool with interactive turns. A burst of scheduled runs therefore delays
a reply twice over: its blocking calls hold the pool's workers when the turn's
own ``asyncio.to_thread`` calls need one, and its local inference holds the
GPU slots.

Behind ``ROBOTHOR_EXECUTION_LANES`` (default off) work runs in one of two lanes:

* ``interactive``: runs a person is waiting on (the trigger set
  ``agent_priority`` already treats as interactive: Telegram, webchat, Slack,
  plugin channels, IDE, manual/CLI, channel events);
* ``background``: everything else, and every piece of work that never said
  which lane it is in (cron, hooks, workflows, autodream, curator, ingest,
  embeddings, post-turn persistence).

Each lane has its own concurrency limit, taken once per top-level
``runner.execute`` (a nested run inherits the slot, so a run that starts a
sub-agent cannot wait on itself), and its own thread executor. A background
run asking for a slot while an interactive turn waits for one yields for up to
``ROBOTHOR_LANE_BACKGROUND_YIELD_MS``. The local inference gate reads the lane
too (``robothor/llm/local_gate.py``).

With the flag off: no slot is taken, the default executor is untouched and the
gate behaves as before. Nothing here changes run semantics, retries, timeouts
or budgets; it only decides who waits for a slot, and for how long.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import os
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

# Imported here, not on first use: a first use is on the event loop, inside a
# run, and these imports are not free.
from robothor.engine.agent_priority import _INTERACTIVE_TRIGGERS
from robothor.engine.feature_flags import execution_lanes_enabled
from robothor.engine.metrics import set_lane_occupancy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Iterator

logger = logging.getLogger(__name__)

#: 0 = uncapped. Interactive turns were never queued before lanes, and a cap
#: held by open ``ask_user`` questions would queue a person silently.
DEFAULT_INTERACTIVE_LIMIT = 0
DEFAULT_BACKGROUND_LIMIT = 2
DEFAULT_BACKGROUND_YIELD_MS = 200


class Lane(StrEnum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"


#: The lane the current task's work belongs to. Unset means background.
_LANE: contextvars.ContextVar[Lane | None] = contextvars.ContextVar("execution_lane", default=None)


class _Holder:
    """The slot a run holds. Tasks the run creates copy a reference to it, and
    see ``released`` once the run ends, so leftover work stops riding on it."""

    __slots__ = ("lane", "released")

    def __init__(self, lane: Lane) -> None:
        self.lane = lane
        self.released = False


#: The slot held by the run this task belongs to, if any.
_HOLDING: contextvars.ContextVar[_Holder | None] = contextvars.ContextVar(
    "lane_slot_held", default=None
)


def lanes_enabled() -> bool:
    """The rollout flag. An unreadable flag means off: today's behaviour."""
    try:
        return execution_lanes_enabled()
    except Exception:  # noqa: BLE001 - a flag read must not break a run
        return False


def current_lane() -> Lane | None:
    """The lane of the current work. Work still running after the run that
    created it has ended is background, whatever lane that run was in."""
    holder = _HOLDING.get()
    if holder is not None and holder.released:
        return Lane.BACKGROUND
    return _LANE.get()


def is_interactive() -> bool:
    return current_lane() is Lane.INTERACTIVE


@contextmanager
def in_lane(lane: Lane) -> Iterator[None]:
    """Run the enclosed work (and every task it creates) in ``lane``."""
    token = _LANE.set(lane)
    try:
        yield
    finally:
        _LANE.reset(token)


@contextmanager
def detached() -> Iterator[None]:
    """Create fire-and-forget work here: it runs in the background lane and any
    run it starts takes its own background slot instead of the creator's."""
    lane, held = _LANE.set(Lane.BACKGROUND), _HOLDING.set(None)
    try:
        yield
    finally:
        _HOLDING.reset(held)
        _LANE.reset(lane)


def lane_for_trigger(trigger_type: Any) -> Lane:
    return Lane.INTERACTIVE if trigger_type in _INTERACTIVE_TRIGGERS else Lane.BACKGROUND


def _publish(lane: Lane, active: int, waiting: int) -> None:
    try:
        set_lane_occupancy(lane.value, active, waiting)
    except Exception:  # noqa: BLE001 - metrics are never load-bearing
        logger.debug("Could not publish lane occupancy", exc_info=True)


class _LaneState:
    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))  # 0 = uncapped
        self.active = 0
        self.waiters: deque[asyncio.Future[None]] = deque()
        # Waiters a freed slot was handed to, until they resume or are cancelled.
        self.handed: set[asyncio.Future[None]] = set()
        self.yielding = 0

    @property
    def waiting(self) -> int:
        return len(self.waiters) + self.yielding


class ExecutionLanes:
    """Per-lane concurrency, with background yielding to a waiting interactive turn.

    A slot freed while others wait is handed straight to the first waiter, so a
    newcomer can never overtake the queue. Waiters are loop futures resolved
    with ``call_soon_threadsafe``, so a release from any thread is safe.
    """

    def __init__(
        self,
        interactive_limit: int = DEFAULT_INTERACTIVE_LIMIT,
        background_limit: int = DEFAULT_BACKGROUND_LIMIT,
        yield_ms: int = DEFAULT_BACKGROUND_YIELD_MS,
    ) -> None:
        self._lock = threading.Lock()
        self._states = {
            Lane.INTERACTIVE: _LaneState(interactive_limit),
            Lane.BACKGROUND: _LaneState(background_limit),
        }
        self._yield_s = max(0, int(yield_ms)) / 1000.0
        self._interactive_clear: list[asyncio.Future[None]] = []

    def _publish_locked(self, lane: Lane) -> None:
        st = self._states[lane]
        _publish(lane, st.active, st.waiting)

    async def _yield_to_interactive(self) -> None:
        """Wait (bounded) while an interactive turn is queued for a slot.

        The lanes' slots are separate, so this frees no interactive slot. What
        the two lanes still share is the event loop, the CPU and local
        inference; starting background work later leaves those to the turns
        already running. It only applies when the interactive lane is capped
        (ROBOTHOR_MAX_CONCURRENT_INTERACTIVE > 0): uncapped, nothing waits.
        """
        with self._lock:
            if self._yield_s <= 0 or not self._states[Lane.INTERACTIVE].waiters:
                return
            fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._interactive_clear.append(fut)
            self._states[Lane.BACKGROUND].yielding += 1
            self._publish_locked(Lane.BACKGROUND)
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=self._yield_s)
        except TimeoutError:
            pass
        finally:
            with self._lock:
                self._states[Lane.BACKGROUND].yielding -= 1
                if fut in self._interactive_clear:
                    self._interactive_clear.remove(fut)
                self._publish_locked(Lane.BACKGROUND)

    def _notify_interactive_clear_locked(self) -> None:
        if self._states[Lane.INTERACTIVE].waiters:
            return
        for fut in self._interactive_clear:
            fut.get_loop().call_soon_threadsafe(_resolve, fut)
        self._interactive_clear.clear()

    async def acquire(self, lane: Lane) -> None:
        if lane is Lane.BACKGROUND:
            await self._yield_to_interactive()
        st = self._states[lane]
        with self._lock:
            if (st.limit == 0 or st.active < st.limit) and not st.waiters:
                st.active += 1
                self._publish_locked(lane)
                return
            fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            st.waiters.append(fut)
            self._publish_locked(lane)
        try:
            await fut
        except asyncio.CancelledError:
            with self._lock:
                handed = fut in st.handed
                st.handed.discard(fut)
                if fut in st.waiters:
                    st.waiters.remove(fut)
                    if lane is Lane.INTERACTIVE:
                        self._notify_interactive_clear_locked()
                self._publish_locked(lane)
            if handed:
                # The slot was handed over just as we were cancelled: pass it on.
                self.release(lane)
            raise
        with self._lock:
            st.handed.discard(fut)

    def release(self, lane: Lane) -> None:
        st = self._states[lane]
        with self._lock:
            if not self._hand_over_locked(st):
                st.active = max(0, st.active - 1)
            if lane is Lane.INTERACTIVE:
                self._notify_interactive_clear_locked()
            self._publish_locked(lane)

    @staticmethod
    def _hand_over_locked(st: _LaneState) -> bool:
        """Give a freed slot to the first live waiter; ``active`` stays the same.

        A waiter already done was cancelled while queued and holds no slot.
        """
        while st.waiters:
            fut = st.waiters.popleft()
            if fut.done():
                continue
            st.handed.add(fut)
            fut.get_loop().call_soon_threadsafe(_resolve, fut)
            return True
        return False

    @asynccontextmanager
    async def slot(self, lane: Lane) -> AsyncIterator[None]:
        await self.acquire(lane)
        try:
            yield
        finally:
            self.release(lane)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {"enabled": lanes_enabled()}
            for lane, st in self._states.items():
                out[lane.value] = {"active": st.active, "waiting": st.waiting, "limit": st.limit}
            return out


def _resolve(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", name, raw, default)
        return default


_LANES: ExecutionLanes | None = None
_LANES_LOCK = threading.Lock()


def get_lanes() -> ExecutionLanes:
    """The process-wide lanes, sized from the environment on first use."""
    global _LANES
    with _LANES_LOCK:
        if _LANES is None:
            _LANES = ExecutionLanes(
                interactive_limit=_env_int(
                    "ROBOTHOR_MAX_CONCURRENT_INTERACTIVE", DEFAULT_INTERACTIVE_LIMIT
                ),
                background_limit=_env_int(
                    "ROBOTHOR_MAX_CONCURRENT_BACKGROUND", DEFAULT_BACKGROUND_LIMIT
                ),
                yield_ms=_env_int("ROBOTHOR_LANE_BACKGROUND_YIELD_MS", DEFAULT_BACKGROUND_YIELD_MS),
            )
        return _LANES


_P = ParamSpec("_P")
_R = TypeVar("_R")


def lane_gated(
    fn: Callable[_P, Coroutine[Any, Any, _R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """Run ``fn`` (a runner's ``execute``) inside a slot of its trigger's lane.

    The slot is taken before the run does anything, so waiting for it is not
    run time: no row, no timeout clock, no budget is touched while queued.
    """
    parameters = inspect.signature(fn)
    trigger_default = parameters.parameters.get("trigger_type")
    default = trigger_default.default if trigger_default is not None else None

    @functools.wraps(fn)
    async def gated(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        holder = _HOLDING.get()
        if (holder is not None and not holder.released) or not lanes_enabled():
            return await fn(*args, **kwargs)
        try:
            bound = parameters.bind_partial(*args, **kwargs).arguments
        except TypeError:
            return await fn(*args, **kwargs)
        lane = lane_for_trigger(bound.get("trigger_type", default))
        async with get_lanes().slot(lane):
            mine = _Holder(lane)
            held, current = _HOLDING.set(mine), _LANE.set(lane)
            try:
                return await fn(*args, **kwargs)
            finally:
                mine.released = True
                _LANE.reset(current)
                _HOLDING.reset(held)

    gated.__lane_gated__ = True  # type: ignore[attr-defined]
    return gated


def _default_workers() -> int:
    # concurrent.futures' own default for ThreadPoolExecutor.
    return min(32, (os.cpu_count() or 1) + 4)


class LaneExecutor(ThreadPoolExecutor):
    """The loop's default executor, split by lane.

    ``asyncio.to_thread`` and ``run_in_executor(None, ...)`` call ``submit`` in
    the caller's context, so the lane is read there: interactive work gets its
    own workers, everything else (including untagged work) the background ones.
    """

    def __init__(
        self, interactive_workers: int | None = None, background_workers: int | None = None
    ) -> None:
        super().__init__(
            max_workers=background_workers or _default_workers(),
            thread_name_prefix="lane-background",
        )
        self._interactive = ThreadPoolExecutor(
            max_workers=interactive_workers or _default_workers(),
            thread_name_prefix="lane-interactive",
        )

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        if current_lane() is Lane.INTERACTIVE:
            return self._interactive.submit(fn, *args, **kwargs)
        return super().submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._interactive.shutdown(wait=wait, cancel_futures=cancel_futures)
        super().shutdown(wait=wait, cancel_futures=cancel_futures)


def install_lane_executor(loop: asyncio.AbstractEventLoop) -> bool:
    """Give ``loop`` the lane-split default executor. A no-op with the flag off."""
    if not lanes_enabled():
        return False
    loop.set_default_executor(LaneExecutor())
    logger.info("Execution lanes on: %s", get_lanes().snapshot())
    return True
