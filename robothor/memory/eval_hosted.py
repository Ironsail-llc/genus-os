"""Model calls for the benchmark runner: local Ollama, or a hosted model.

Placeholder for the local path only; the hosted path is added with its tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def chat_for(model: str) -> Callable[..., Awaitable[Any]]:
    from robothor.llm import ollama

    return ollama.chat
