"""Todo list tool handler — in-conversation progress tracking."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("todo_write")
async def _todo_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Validate todo_write input and return structured result.

    The actual TodoList state update happens in the runner (which has session
    access). This handler validates input and builds the result dict.
    """
    from robothor.engine.todolist import TodoItem, TodoList

    raw_todos = args.get("todos")
    if not raw_todos or not isinstance(raw_todos, list):
        return {"error": "todos must be a non-empty array"}

    items = [
        TodoItem(
            content=str(raw.get("content", "")),
            active_form=str(raw.get("active_form", raw.get("activeForm", ""))),
            status=str(raw.get("status", "pending")),
        )
        for raw in raw_todos
    ]

    error = TodoList.validate(items)
    if error:
        return {"error": error}

    # Return validated items for the runner to apply via session.todo_list.replace()
    # The runner will merge in oldTodos and verificationNudgeNeeded.
    return {
        "_validated_items": [item.to_dict() for item in items],
        "_needs_apply": True,
    }
