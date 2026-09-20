"""Controlled single-action comparison using the native runner's captured payload.

Preserve host prompts and provider-specific reasoning/output parameters. The
public extra_body option prevents SDK renaming/defaults from changing them.
Only the isolated record fixture and complete text conversations are supported here.
"""

from copy import deepcopy

from bench.runtime.candidates import DeepAgentsCandidate, FixtureGateway, PydanticCandidate
from bench.runtime.conversation import text_history


def candidate_from_native(runtime, payload, provider, client):
    from langchain_openai import ChatOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel

    payload = deepcopy(payload)
    messages = payload["messages"]
    if not isinstance(messages, list) or any(
        not isinstance(m, dict)
        or set(m) != {"role", "content"}
        or not isinstance(m["content"], str)
        for m in messages
    ):
        raise ValueError("native comparison requires text-only message objects")
    prefix = 0
    while prefix < len(messages) and messages[prefix].get("role") == "system":
        prefix += 1
    if (
        len(messages) < 2
        or not prefix
        or messages[-1]["role"] != "user"
        or payload["tools"] != FixtureGateway("fixture").schemas
        or payload.get("stream", False) is not False
    ):
        raise ValueError(
            "native comparison requires a supported synthetic text-action conversation"
        )
    prompts = tuple(m["content"] for m in messages[:prefix])
    history = text_history(messages[prefix:-1])
    settings = {k: v for k, v in payload.items() if k not in {"model", "messages"}}
    if runtime == "pydantic-ai":
        candidate = PydanticCandidate(
            OpenAIChatModel(payload["model"], provider=provider),
            system_prompt=prompts,
            history=history,
            model_settings={"max_tokens": None, "temperature": None, "extra_body": settings},
        )
    elif runtime == "deepagents":
        candidate = DeepAgentsCandidate(
            ChatOpenAI(
                model=payload["model"],
                api_key="synthetic-key",
                base_url="https://openrouter.ai/api/v1",
                max_retries=0,
                http_async_client=client,
                extra_body=settings,
            ),
            system_prompt=prompts,
            history=history,
        )
    else:
        raise ValueError("unsupported candidate")
    return candidate, messages[-1]["content"]
