"""Scoped, trusted observation of native handler results; not an agent capability."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass
class _Observer:
    callback: Callable[..., Any] | None
    names: frozenset[str]
    annotations: bool = False
    active: bool = True


_active: ContextVar[_Observer | None] = ContextVar("native_tool_observer", default=None)


@contextmanager
def tool_observation_scope(
    callback: Callable[..., Any] | None, *, names: set[str], annotations: bool = False
) -> Iterator[None]:
    observer = _Observer(callback, frozenset(names), annotations=annotations)
    token = _active.set(observer)
    try:
        yield
    finally:
        observer.active = False
        _active.reset(token)


def observe_tool_result(
    name: str, args: dict[str, Any], result: Any, ctx: Any
) -> dict[str, Any] | None:
    """Observe after execution, before annotation. Observer failures are not ignored."""
    observer = _active.get()
    if (
        observer is not None
        and observer.active
        and observer.callback is not None
        and name in observer.names
    ):
        annotation = observer.callback(name, deepcopy(args), deepcopy(result), deepcopy(ctx))
        if observer.annotations and annotation is not None:
            if not isinstance(annotation, dict):
                raise ValueError("Workflow tool context must be an object")
            return deepcopy(annotation)
    return None
