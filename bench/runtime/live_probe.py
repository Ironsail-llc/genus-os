"""Probe the configured model chain with synthetic input; never invokes business tools.

This is an availability/provider timing probe, not a replacement benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import yaml

from bench.runtime.candidates import PROMPT, SCHEMA, SYSTEM


async def probe(manifest, output):
    config = yaml.safe_load(manifest.read_text())
    chain = list(dict.fromkeys([config["model"]["primary"], *config["model"].get("fallbacks", [])]))
    rows = []
    async with httpx.AsyncClient(timeout=60) as client:
        for model in chain:
            if not model.startswith("openrouter/"):
                rows.append(
                    {
                        "model": model,
                        "status": "not_probed",
                        "reason": "local model uses the shared inference gate; no direct shared-GPU load from this probe",
                    }
                )
                continue
            key = os.environ.get("OPENROUTER_API_KEY")
            if not key:
                rows.append(
                    {
                        "model": model,
                        "status": "unavailable",
                        "reason": "provider credential not available",
                    }
                )
                continue
            started = time.perf_counter()
            try:
                response = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={
                        "model": model.removeprefix("openrouter/"),
                        "messages": [
                            {"role": "system", "content": SYSTEM},
                            {"role": "user", "content": PROMPT},
                        ],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "record",
                                    "description": "Store an authorized value",
                                    "parameters": SCHEMA,
                                },
                            }
                        ],
                        "max_tokens": 512,
                        "temperature": config["model"].get("temperature", 0.5),
                    },
                )
                body = response.json()
                rows.append(
                    {
                        "model": model,
                        "status": "available" if response.is_success else "provider_error",
                        "http_status": response.status_code,
                        "duration_ms": (time.perf_counter() - started) * 1000,
                        "usage": body.get("usage"),
                        "tool_call_received": bool(
                            (body.get("choices") or [{}])[0].get("message", {}).get("tool_calls")
                        ),
                        "error": (body.get("error") or {}).get("message"),
                    }
                )
            except (httpx.HTTPError, ValueError) as exc:
                rows.append(
                    {
                        "model": model,
                        "status": "transport_error",
                        "error_type": type(exc).__name__,
                        "duration_ms": (time.perf_counter() - started) * 1000,
                    }
                )
            output.write_text(
                json.dumps(
                    {
                        "scope": "One availability probe per configured cloud model; synthetic input; zero business dispatch",
                        "models": rows,
                    },
                    indent=2,
                )
                + "\n"
            )
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.manifest, args.output)), indent=2))


if __name__ == "__main__":
    main()
