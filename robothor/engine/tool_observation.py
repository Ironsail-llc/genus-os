"""Scoped, trusted observation of native handler results; not an agent capability."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass
class _Observer:
    callback: Callable[..., None]
    names: frozenset[str]
    active: bool = True


_active: ContextVar[_Observer | None] = ContextVar("native_tool_observer", default=None)


@contextmanager
def tool_observation_scope(callback, *, names: set[str]) -> Iterator[None]:
    observer = _Observer(callback, frozenset(names))
    token = _active.set(observer)
    try:
        yield
    finally:
        observer.active = False
        _active.reset(token)


def observe_tool_result(name: str, args: dict, result: Any, ctx: Any) -> None:
    """Observe after execution, before annotation. Observer failures are not ignored."""
    observer = _active.get()
    if observer is not None and observer.active and name in observer.names:
        observer.callback(name, deepcopy(args), deepcopy(result), deepcopy(ctx))
