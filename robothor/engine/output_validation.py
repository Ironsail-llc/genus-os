"""Bounded corrections for trusted workflow-owned output validators."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


class OutputValidationError(ValueError):
    """The run exhausted its correction opportunities without valid output."""


@dataclass
class _Validation:
    validate: Callable[..., Any]
    max_repairs: int
    repairs: int = 0
    active: bool = True


_validation: ContextVar[_Validation | None] = ContextVar("workflow_output_validation", default=None)


@contextmanager
def output_validation_scope(
    validate: Callable[..., Any], *, max_repairs: int = 2
) -> Iterator[None]:
    if type(max_repairs) is not int or not 0 <= max_repairs <= 2:
        raise ValueError("Output repair limit must be between zero and two")
    state = _Validation(validate, max_repairs)
    token = _validation.set(state)
    try:
        yield
    finally:
        state.active = False
        _validation.reset(token)


def _problem(session: Any, text: str) -> tuple[_Validation | None, str | None]:
    state = _validation.get()
    if state is None or not state.active:
        return state, None
    try:
        problem = state.validate(session.run, text)
    except Exception:
        problem = "Workflow output validator failed"
    return state, str(problem)[:1500] if problem else None


def request_output_repair(session: Any) -> bool:
    """Use another ordinary iteration; never add time, tools, tokens or money."""
    state, problem = _problem(session, session.get_final_text())
    # `_problem` returns a problem only when it had a live validation state to
    # ask, so these two are never out of step; say so rather than imply it.
    if state is None or not problem:
        return False
    if state.repairs >= state.max_repairs:
        raise OutputValidationError("Workflow output validation failed: " + problem)
    state.repairs += 1
    session.messages.append(
        {
            "role": "developer",
            "content": "[workflow output validation] Your final result was rejected: "
            + problem
            + ". Correct the result using the available evidence and tools. Preserve unknowns; "
            "do not invent missing facts. Return the requested output contract.",
        }
    )
    session.record_error("Output correction requested: " + problem)
    return True


def validated_completion(session: Any, text: str) -> Any:
    """Budget/finalizer exits cannot bypass the workflow's acceptance contract."""
    _, problem = _problem(session, text)
    if problem:
        return session.fail("Workflow output validation failed: " + problem)
    return session.complete(text)
