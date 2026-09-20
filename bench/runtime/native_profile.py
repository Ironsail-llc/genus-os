"""Controlled single-action comparison using the native runner's captured payload.

Preserve host prompts and provider-specific reasoning/output parameters. The
public extra_body option prevents SDK renaming/defaults from changing them.
Only the isolated record fixture and initial text messages are supported here.
"""

from bench.runtime.candidates import DeepAgentsCandidate, FixtureGateway, PydanticCandidate


def candidate_from_native(runtime, payload, provider, client):
    from langchain_openai import ChatOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel

    messages = payload["messages"]
    if (
        len(messages) < 2
        or messages[-1]["role"] != "user"
        or any(m["role"] != "system" for m in messages[:-1])
        or any(not isinstance(m.get("content"), str) for m in messages)
        or payload["tools"] != FixtureGateway("fixture").schemas
        or payload.get("stream", False) is not False
    ):
        raise ValueError("native comparison requires the initial synthetic text-action request")
    prompts = tuple(m["content"] for m in messages[:-1])
    settings = {k: v for k, v in payload.items() if k not in {"model", "messages"}}
    if runtime == "pydantic-ai":
        candidate = PydanticCandidate(
            OpenAIChatModel(payload["model"], provider=provider),
            system_prompt=prompts,
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
        )
    else:
        raise ValueError("unsupported candidate")
    return candidate, messages[-1]["content"]
