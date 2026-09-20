"""Diagnostic first-turn proposals only; never dispatch business tools.

Replay messages from test_runtime_live_native's opt-in diagnostic capture.
This is not an acceptance cohort or a production prompt configuration.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path

import litellm
import yaml

from bench.runtime.candidates import SCHEMA, SYSTEM
from robothor.engine.key_pool import api_key_for_model


def variants(messages):
    versions = {"full": copy.deepcopy(messages), "no_skills": copy.deepcopy(messages)}
    original = versions["no_skills"][0]["content"]
    start = original.index("## Available Skills")
    end = original.index("\n---\n", start)
    versions["no_skills"][0]["content"] = original[:start] + original[end:]
    versions["no_account_note"] = [copy.deepcopy(m) for m in messages if m["role"] != "developer"]
    versions["system_note"] = copy.deepcopy(versions["no_account_note"])
    notes = [copy.deepcopy(m) for m in messages if m["role"] == "developer"]
    versions["system_note"][0]["content"] += "\n\n" + "\n\n".join(m["content"] for m in notes)
    versions["minimal"] = [{"role": "system", "content": SYSTEM}, copy.deepcopy(messages[-1])]
    versions["minimal_with_note"] = [
        {"role": "system", "content": SYSTEM},
        *notes,
        copy.deepcopy(messages[-1]),
    ]
    return versions


async def run(args):
    config = yaml.safe_load(args.manifest.read_text())["model"]
    configured = [config["primary"], *config.get("fallbacks", [])]
    if args.model not in configured:
        raise ValueError("Use an existing configured model")
    source = json.loads(args.capture.read_text().splitlines()[0])["provider_messages"][0]
    versions = variants(source)
    if args.output.exists():
        raise ValueError("Refusing to overwrite earlier diagnostic samples")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repetition in range(args.samples):
        for name in args.variants:
            messages = versions[name]
            row = {
                "scope": "diagnostic first-turn proposal; no tool execution",
                "variant": name,
                "repetition": repetition,
                "model": args.model,
                "prompt_sha256": hashlib.sha256(
                    json.dumps(messages, sort_keys=True).encode()
                ).hexdigest(),
            }
            started = time.monotonic()
            try:
                async with asyncio.timeout(60):
                    response = await litellm.acompletion(
                        model=args.model,
                        api_key=api_key_for_model(args.model),
                        messages=messages,
                        tools=[
                            {
                                "type": "function",
                                "function": {
                                    "name": "record",
                                    "description": "Store an authorized value",
                                    "parameters": SCHEMA,
                                },
                            }
                        ],
                        temperature=config.get("temperature", 0.5),
                        max_tokens=4096,
                        timeout=60,
                        num_retries=0,
                    )
                message = response.choices[0].message
                calls = [
                    {"name": c.function.name, "arguments": c.function.arguments}
                    for c in message.tool_calls or []
                ]
                row.update(calls=calls, text=message.content, usage=response.usage.model_dump())
                row["correct_proposed_call"] = (
                    len(calls) == 1
                    and calls[0]["name"] == "record"
                    and json.loads(calls[0]["arguments"]) == {"key": "report", "value": "delivered"}
                )
            except Exception as exc:
                row.update(error_type=type(exc).__name__, correct_proposed_call=False)
            row["duration_ms"] = 1000 * (time.monotonic() - started)
            with args.output.open("a") as file:
                file.write(json.dumps(row) + "\n")
            print(name, repetition, row["correct_proposed_call"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=[
            "full",
            "no_skills",
            "no_account_note",
            "system_note",
            "minimal",
            "minimal_with_note",
        ],
        default=["full", "no_skills", "no_account_note", "minimal"],
    )
    args = parser.parse_args()
    if not 1 <= args.samples <= 30:
        parser.error("Use 1–30 repetitions for a bounded diagnostic")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
