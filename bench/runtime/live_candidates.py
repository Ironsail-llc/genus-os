"""Configured provider screening, synthetic gateway only. No promotion decision."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import time
from pathlib import Path

import httpx
import yaml

from bench.interactive.statistics import summary
from bench.runtime.candidates import DeepAgentsCandidate, FixtureGateway, PydanticCandidate
from bench.runtime.provider_capture import ProviderCapture


def screening_provider(api_key, *, http_client=None):
    """One HTTP attempt per model request; the host owns retry policy."""
    from openai import AsyncOpenAI
    from pydantic_ai.providers.openai import OpenAIProvider

    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        max_retries=0,
        http_client=http_client,
    )
    return OpenAIProvider(openai_client=client)


def screening_candidate(runtime, model, config, key, provider, *, http_client=None):
    """Controlled settings, separate from framework-default evaluation."""
    temperature = config.get("temperature", 0.5)
    if runtime == "pydantic-ai":
        from pydantic_ai.models.openai import OpenAIChatModel

        return PydanticCandidate(
            OpenAIChatModel(model, provider=provider),
            model_settings={"temperature": temperature, "max_tokens": 512},
        )
    if runtime != "deepagents":
        raise ValueError("unsupported screening runtime")
    from langchain_openai import ChatOpenAI

    return DeepAgentsCandidate(
        ChatOpenAI(
            model=model,
            base_url="https://openrouter.ai/api/v1",
            api_key=key,
            temperature=temperature,
            max_tokens=512,
            max_retries=0,
            http_async_client=http_client,
        ),
        model_settings={"strict": True},
        tool_choice="auto",
    )


async def screen(manifest, output, samples):
    config = yaml.safe_load(manifest.read_text())["model"]
    models = list(dict.fromkeys([config["primary"], *config.get("fallbacks", [])]))
    key = os.environ["OPENROUTER_API_KEY"]
    capture = ProviderCapture()
    client = httpx.AsyncClient(timeout=60, event_hooks={"request": [capture.record]})
    provider = screening_provider(key, http_client=client)
    rows = []
    gate = asyncio.Semaphore(3)

    async def sample(model, runtime, index):
        queued = time.perf_counter()
        async with gate:
            gateway = FixtureGateway("fixture")
            started = time.perf_counter()
            queue_ms = (started - queued) * 1000
            trace_token = capture.start()
            try:
                selected = model.removeprefix("openrouter/")
                candidate = screening_candidate(
                    runtime, selected, config, key, provider, http_client=client
                )
                result = await candidate.run(gateway, tenant="fixture")
                result["status"] = "completed" if result["verified"] else "failed"
            except Exception as exc:
                result = {
                    "status": "timeout" if isinstance(exc, TimeoutError) else "failed",
                    "error_type": type(exc).__name__,
                    "input_tokens": None,
                    "output_tokens": None,
                    "cost_usd": None,
                    "duration_ms": (time.perf_counter() - started) * 1000,
                }
            finally:
                provider_requests = capture.finish(trace_token)
            execution_ms = (time.perf_counter() - started) * 1000
            row = {
                "runtime": runtime,
                "model": model,
                "repetition": index,
                "writes": gateway.writes,
                "dispatches": gateway.dispatches,
                "provider_requests": provider_requests,
                **result,
                "framework_model_calls": result.get("model_calls"),
                "model_calls": len(provider_requests),
                "queue_ms": queue_ms,
                "framework_duration_ms": result["duration_ms"],
                "execution_ms": execution_ms,
                "duration_ms": execution_ms + queue_ms,
            }
            rows.append(row)
            # Flush each outcome, including failures, before another sample starts.
            with output.open("a") as file:
                file.write(json.dumps(row) + "\n")

    cloud = [model for model in models if model.startswith("openrouter/")]
    try:
        for index in range(samples):
            await asyncio.gather(
                *(
                    sample(model, runtime, index)
                    for model in cloud
                    for runtime in ("pydantic-ai", "deepagents")
                )
            )
    finally:
        await provider.client.close()
    report = {
        "scope": "Configured cloud models, synthetic business gateway, no matched native-runner cohort; not a promotion result",
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("pydantic-ai-slim", "deepagents", "langchain-openai")
        },
        "scenarios": {
            f"{model}:{runtime}": {
                "duration_ms": summary(
                    [
                        r["duration_ms"]
                        for r in rows
                        if r["model"] == model and r["runtime"] == runtime
                    ]
                ),
                "failures": sum(
                    r["status"] != "completed"
                    for r in rows
                    if r["model"] == model and r["runtime"] == runtime
                ),
            }
            for model in cloud
            for runtime in ("pydantic-ai", "deepagents")
        },
    }
    output.with_suffix(".summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()
    if args.samples < 30:
        parser.error("screening requires at least 30 samples")
    if args.output.exists():
        parser.error("choose a new artifact path; do not mix runs")
    print(json.dumps(asyncio.run(screen(args.manifest, args.output, args.samples)), indent=2))


if __name__ == "__main__":
    main()
