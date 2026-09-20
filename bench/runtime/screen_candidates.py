"""Offline screening only. No provider results or runtime promotion inferred."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path

from bench.runtime.candidates import DeepAgentsCandidate, FixtureGateway, PydanticCandidate


def pydantic_model():
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    def function(messages, info):
        return ModelResponse(
            parts=[ToolCallPart("record", {"key": "report", "value": "delivered"})]
        )

    return FunctionModel(function)


def deep_model():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            assert {tool.name for tool in tools} == {"record"}
            return self

    return Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "record",
                        "args": {"key": "report", "value": "delivered"},
                        "id": "fixture-call",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )


async def screen(samples):
    records = []
    for name, cls, factory in [
        ("pydantic-ai", PydanticCandidate, pydantic_model),
        ("deepagents", DeepAgentsCandidate, deep_model),
    ]:
        for repetition in range(samples + 1):
            gateway = FixtureGateway("fixture")
            try:
                result = await cls(factory()).run(gateway, tenant="fixture")
                result.update(
                    status="completed",
                    single_write=gateway.writes == 1,
                    dispatches=gateway.dispatches,
                )
            except Exception as exc:
                result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            if repetition:
                records.append({"runtime": name, "repetition": repetition, **result})
    return {
        "scope": "Synthetic gateway smoke screening; not a matched production workload or promotion result",
        "versions": {
            p: importlib.metadata.version(p)
            for p in ["pydantic-ai-slim", "deepagents", "langchain", "langgraph"]
        },
        "samples": records,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 30:
        parser.error("at least 30 screening repetitions required")
    result = asyncio.run(screen(args.samples))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                name: sum(
                    r["status"] != "completed" for r in result["samples"] if r["runtime"] == name
                )
                for name in ("pydantic-ai", "deepagents")
            }
        )
    )


if __name__ == "__main__":
    main()
