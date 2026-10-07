"""Is Claude Code usable here? The CLI's version, and a one-turn ping.

Shared by ``robothor claude-code status`` and the doctor, so both answer the
question the same way: the ping runs through exactly the environment and argv a
real job uses (:func:`robothor.engine.coding.env.build_claude_env`,
:func:`robothor.engine.coding.runner.run_claude`), so a green ping means a job
would authenticate — not that the operator's own shell login works.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid

from robothor.constants import DEFAULT_TENANT
from robothor.engine.coding import env as coding_env
from robothor.engine.coding.runner import (
    ClaudeInvocation,
    ClaudeResult,
    resolve_claude_binary,
    run_claude,
)

__all__ = ["cli_version", "ping", "store_token", "token_vault_key"]

PING_PROMPT = "Reply with exactly the word: ok"


async def cli_version(timeout: float = 15.0) -> str:
    """``claude --version``, or raise ``ClaudeCodeError``/``RuntimeError``."""
    proc = await asyncio.create_subprocess_exec(
        resolve_claude_binary(),
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"claude --version timed out after {timeout}s") from None
    if proc.returncode != 0:
        raise RuntimeError(
            (err or out).decode(errors="replace").strip() or "claude --version failed"
        )
    return out.decode(errors="replace").strip()


async def ping(
    *, tenant_id: str = DEFAULT_TENANT, timeout_s: float = 120.0, model: str | None = None
) -> ClaudeResult:
    """One turn, no tools, in a throwaway directory, with a job's environment."""
    probe_id = f"probe-{uuid.uuid4().hex[:12]}"
    token = await asyncio.to_thread(coding_env.resolve_oauth_token, tenant_id)
    env = coding_env.build_claude_env(job_id=probe_id, oauth_token=token)
    home = coding_env.job_config_dir(probe_id)
    try:
        with tempfile.TemporaryDirectory(prefix="genus-claude-ping-") as cwd:
            return await run_claude(
                ClaudeInvocation(
                    prompt=PING_PROMPT,
                    cwd=cwd,
                    model=model or "haiku",
                    max_turns=1,
                    disallowed_tools=("Bash", "Edit", "Write", "WebFetch", "WebSearch"),
                ),
                env=env,
                timeout_s=timeout_s,
            )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def token_vault_key() -> str:
    """The canonical vault row for the token — where ``vault_set`` writes it too."""
    from robothor.vault.naming import vault_keys_for_env_name

    return vault_keys_for_env_name(coding_env.TOKEN_ENV)[0]


def store_token(token: str, *, tenant_id: str = DEFAULT_TENANT) -> str:
    """Write the token to the vault and return the key it landed under."""
    from robothor import vault
    from robothor.secrets import reset_secret_cache

    key = token_vault_key()
    vault.set(key, token, category="credential", tenant_id=tenant_id)
    reset_secret_cache()
    return key
