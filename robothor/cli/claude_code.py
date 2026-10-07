"""``robothor claude-code login|status`` — the Claude Code driver's credential.

Mirrors ``robothor codex``. ``login`` runs ``claude setup-token`` (the
subscription's long-lived token flow; it opens a browser and prints the token)
and then asks for the token to be pasted, or reads it from stdin with
``--token-stdin``. The token goes to the VAULT, never to a file or the
environment, and is then proved with a one-turn ping through exactly the
environment a real job uses. ``status`` reports the CLI version, where the
token resolves from (never its value), and pings.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace

    from robothor.engine.coding.runner import ClaudeResult


def _tenant() -> str:
    from robothor.constants import DEFAULT_TENANT
    from robothor.settings import get_settings

    return get_settings().database.tenant_id or DEFAULT_TENANT


def _report_ping(result: ClaudeResult) -> int:
    if result.is_error:
        print(f"Ping failed: {result.error_summary}")
        return 1
    print(f"Ping ok: {result.result_text.strip()[:40]!r} (${result.total_cost_usd:.4f})")
    return 0


def _ping() -> int:
    from robothor.engine.coding import probe

    try:
        return _report_ping(asyncio.run(probe.ping(tenant_id=_tenant())))
    except Exception as e:  # noqa: BLE001 - a CLI reports, it does not trace
        print(f"Ping failed: {type(e).__name__}: {e}")
        return 1


def _login(args: Namespace) -> int:
    import getpass

    from robothor.engine.coding import probe, runner

    if getattr(args, "token_stdin", False):
        token = sys.stdin.readline().strip()
    else:
        try:
            binary = runner.resolve_claude_binary()
        except runner.ClaudeCodeError as e:
            print(str(e))
            return 1
        print("Running `claude setup-token`. Sign in with the Claude subscription that should pay")
        print("for Genus coding jobs; it prints a long-lived token at the end.")
        code = subprocess.call([binary, "setup-token"])
        if code != 0:
            print(f"claude setup-token exited {code}")
            return 1
        token = getpass.getpass("Paste the token it printed (input hidden): ").strip()
    if not token:
        print("No token given; nothing stored.")
        return 1
    try:
        key = probe.store_token(token, tenant_id=_tenant())
    except Exception as e:  # noqa: BLE001
        print(f"Could not store the token in the vault: {type(e).__name__}")
        return 1
    print(f"Stored in the vault as {key} (tenant {_tenant()}).")
    if getattr(args, "no_ping", False):
        return 0
    return _ping()


def _status(args: Namespace) -> int:
    from robothor.engine.coding import probe
    from robothor.engine.coding.env import TOKEN_ENV
    from robothor.secrets import secret_source

    try:
        version = asyncio.run(probe.cli_version())
        print(f"Claude Code CLI: {version}")
    except Exception as e:  # noqa: BLE001
        print(f"Claude Code CLI: not usable ({e})")
        return 1
    from robothor.engine.coding import env as coding_env

    source = secret_source(TOKEN_ENV, tenant_id=_tenant())
    print(f"{TOKEN_ENV}: {source}")
    if source in ("missing", "unavailable"):
        if coding_env.auth_mode() != "token" and coding_env.host_login_present():
            print("Credential: this host's own Claude Code login (no token needed).")
        else:
            print("Sign in with `claude` on this host, or run `robothor claude-code login`.")
            return 1
    if getattr(args, "no_ping", False):
        return 0
    return _ping()


def cmd_claude_code(args: Namespace) -> int:
    command = getattr(args, "claude_code_command", None) or "status"
    if command == "login":
        return _login(args)
    if command == "status":
        return _status(args)
    print(f"Unknown claude-code command: {command}")
    return 1
