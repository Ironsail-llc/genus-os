"""Configured provider screening, synthetic gateway only. No promotion decision."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import time
from pathlib import Path

import yaml

from bench.interactive.statistics import summary
from bench.runtime.candidates import DeepAgentsCandidate, FixtureGateway, PydanticCandidate


async def screen(manifest, output, samples):
    from langchain_openai import ChatOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    config = yaml.safe_load(manifest.read_text())["model"]
    models = list(dict.fromkeys([config["primary"], *config.get("fallbacks", [])]))
    key = os.environ["OPENROUTER_API_KEY"]
    provider = OpenAIProvider(base_url="https://openrouter.ai/api/v1", api_key=key)
    rows = []
    gate = asyncio.Semaphore(3)

    async def sample(model, runtime, index):
        async with gate:
            gateway = FixtureGateway("fixture")
            started = time.perf_counter()
            try:
                selected = model.removeprefix("openrouter/")
                if runtime == "pydantic-ai":
                    candidate = PydanticCandidate(OpenAIChatModel(selected, provider=provider))
                else:
                    candidate = DeepAgentsCandidate(
                        ChatOpenAI(
                            model=selected,
                            base_url="https://openrouter.ai/api/v1",
                            api_key=key,
                            max_tokens=512,
                            temperature=config.get("temperature", 0.5),
                            max_retries=0,
                        )
                    )
                result = await candidate.run(gateway, tenant="fixture")
                result["status"] = "completed" if result["verified"] else "failed"
            except Exception as exc:
                result = {
                    "status": "timeout" if isinstance(exc, TimeoutError) else "failed",
                    "error_type": type(exc).__name__,
                    "duration_ms": (time.perf_counter() - started) * 1000,
                }
            row = {
                "runtime": runtime,
                "model": model,
                "repetition": index,
                "writes": gateway.writes,
                "dispatches": gateway.dispatches,
                **result,
            }
            rows.append(row)
            # Flush each outcome, including failures, before another sample starts.
            with output.open("a") as file:
                file.write(json.dumps(row) + "\n")

    cloud = [model for model in models if model.startswith("openrouter/")]
    for index in range(samples):
        await asyncio.gather(
            *(
                sample(model, runtime, index)
                for model in cloud
                for runtime in ("pydantic-ai", "deepagents")
            )
        )
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
