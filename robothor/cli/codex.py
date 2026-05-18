"""Codex subscription provider CLI commands."""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace


def _env_without_api_key() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT"):
        env.pop(key, None)
    if os.environ.get("ROBOTHOR_CODEX_HOME"):
        env["CODEX_HOME"] = os.environ["ROBOTHOR_CODEX_HOME"]
    return env


def cmd_codex(args: Namespace) -> int:
    command = args.codex_command or "status"
    if command == "login":
        cmd = ["codex", "login"]
        if getattr(args, "with_access_token", False):
            cmd.append("--with-access-token")
        print("Starting Codex login. Use ChatGPT sign-in for subscription-backed billing.")
        return subprocess.call(cmd, env=_env_without_api_key())

    if command == "status":
        from robothor.engine.codex_provider import login_status

        try:
            print(asyncio.run(login_status()))
            return 0
        except Exception as e:
            print(f"Codex status failed: {e}")
            return 1

    if command == "doctor":
        from robothor.engine.codex_provider import ensure_chatgpt_login

        try:
            asyncio.run(ensure_chatgpt_login())
            print("Codex subscription auth is available (ChatGPT login).")
            if os.environ.get("OPENAI_API_KEY"):
                print(
                    "Warning: OPENAI_API_KEY is set in this shell. "
                    "Genus removes it for codex/* subprocess calls to avoid API billing."
                )
            return 0
        except Exception as e:
            print(f"Codex subscription auth is not ready: {e}")
            return 1

    if command == "test":
        from robothor.engine.codex_provider import acompletion

        async def _run() -> str:
            response = await acompletion(
                model=args.model,
                messages=[{"role": "user", "content": args.prompt}],
                timeout=args.timeout,
            )
            return str(response.choices[0].message.content or "")

        try:
            print(asyncio.run(_run()))
            return 0
        except Exception as e:
            print(f"Codex test failed: {e}")
            return 1

    print(f"Unknown codex command: {command}")
    return 1
