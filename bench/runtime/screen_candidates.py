"""Offline screening only. No provider results or runtime promotion inferred."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import time
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


async def screen(samples, record_event=None):
    def record(event):
        if record_event is not None:
            record_event(event)

    versions = {
        p: importlib.metadata.version(p)
        for p in ["pydantic-ai-slim", "deepagents", "langchain", "langgraph"]
    }
    record({"event": "screen_started", "samples_per_runtime": samples, "versions": versions})
    records, warmups = [], []
    for name, cls, factory in [
        ("pydantic-ai", PydanticCandidate, pydantic_model),
        ("deepagents", DeepAgentsCandidate, deep_model),
    ]:
        for repetition in range(samples + 1):
            gateway = FixtureGateway("fixture")
            record(
                {
                    "event": "sample_started",
                    "runtime": name,
                    "repetition": repetition,
                    "warmup": repetition == 0,
                }
            )
            started = time.perf_counter()
            try:
                result = await cls(factory()).run(gateway, tenant="fixture")
                checks = {
                    "adapter_verified": result.get("verified") is True,
                    "fixture_verified": gateway.verified,
                    "single_write": gateway.writes == 1,
                    "single_dispatch": gateway.dispatches == 1,
                }
                result.update(
                    status="completed" if all(checks.values()) else "failed",
                    checks=checks,
                )
            except Exception as exc:
                result = {
                    "status": "timeout" if isinstance(exc, TimeoutError) else "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            result.update(
                runtime=name,
                repetition=repetition,
                warmup=repetition == 0,
                single_write=gateway.writes == 1,
                writes=gateway.writes,
                dispatches=gateway.dispatches,
                execution_ms=(time.perf_counter() - started) * 1000,
                queue_ms=0,
            )
            for metric in ("model_calls", "input_tokens", "output_tokens", "cost_usd"):
                result.setdefault(metric, None)
            (records if repetition else warmups).append(result)
            record({"event": "sample_finished", "sample": result})
    return {
        "scope": "Synthetic gateway smoke screening; not a matched production workload or promotion result",
        "versions": versions,
        "samples": records,
        "warmups": warmups,
        "correctness_passed": all(row["status"] == "completed" for row in records + warmups),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 30:
        parser.error("at least 30 screening repetitions required")
    journal_path = args.output.with_suffix(args.output.suffix + ".jsonl")
    if args.output.exists() or journal_path.exists():
        parser.error("Refusing to overwrite earlier screening evidence")
    with journal_path.open("x") as journal:

        def record(event):
            journal.write(json.dumps(event) + "\n")
            journal.flush()
            os.fsync(journal.fileno())

        result = asyncio.run(screen(args.samples, record_event=record))
        record({"event": "screen_finished", "correctness_passed": result["correctness_passed"]})
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                name: sum(
                    r["status"] != "completed"
                    for r in result["samples"] + result["warmups"]
                    if r["runtime"] == name
                )
                for name in ("pydantic-ai", "deepagents")
            }
        )
    )

    if not result["correctness_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
