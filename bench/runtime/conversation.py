"""Validated host-owned text history for bounded runtime comparison."""

from copy import deepcopy


def text_history(history):
    messages = deepcopy(list(history))
    if len(messages) % 2 or any(
        not isinstance(message, dict)
        or set(message) != {"role", "content"}
        or message["role"] != ("user" if index % 2 == 0 else "assistant")
        or not isinstance(message["content"], str)
        for index, message in enumerate(messages)
    ):
        raise ValueError("candidate text history requires complete user/assistant text pairs")
    return messages


def pydantic_history(prompts, history):
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        SystemPromptPart,
        TextPart,
        UserPromptPart,
    )

    if not history:
        return []
    prompts = (prompts,) if isinstance(prompts, str) else prompts
    result = [ModelRequest(parts=[SystemPromptPart(prompt) for prompt in prompts])]
    result.extend(
        ModelRequest(parts=[UserPromptPart(message["content"])])
        if message["role"] == "user"
        else ModelResponse(parts=[TextPart(message["content"])])
        for message in history
    )
    return result
