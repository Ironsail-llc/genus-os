"""Claude Code driver: is the CLI installed, and does a token resolve?

The ``claude_code_*`` tools are opt-in, so an instance that never set them up
is not broken: with neither the CLI nor a token this check SKIPS. Half a setup
is a failure, because an agent offered the tools will hit it at run time.

The live ping is deliberately not here — it spends money and takes longer than
a doctor check's budget. ``robothor claude-code status`` runs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]


async def _ready(ctx: DoctorContext) -> Result:
    """Whether coding jobs can start: the Claude Code CLI and its token.

    A failure names the missing half. Install the CLI for the engine's service
    user (or set ROBOTHOR_CLAUDE_BIN), then run `robothor claude-code login`,
    which stores CLAUDE_CODE_OAUTH_TOKEN in the vault. Prove it end to end with
    `robothor claude-code status`.
    """
    from robothor.engine.coding import env as coding_env
    from robothor.engine.coding import probe, runner
    from robothor.engine.coding.env import TOKEN_ENV
    from robothor.secrets import secret_source

    source = secret_source(TOKEN_ENV)
    has_token = source in ("vault", "env")
    if not has_token and coding_env.auth_mode() != "token" and coding_env.host_login_present():
        has_token, source = True, "host Claude Code login"
    try:
        runner.resolve_claude_binary()
    except runner.ClaudeCodeError:
        if not has_token:
            return skip("Claude Code is not set up on this instance (no CLI, no token)")
        return fail(
            f"{TOKEN_ENV} resolves from the {source}, but the Claude Code CLI is not installed"
        )

    version = "installed"
    if not ctx.offline:
        try:
            version = await probe.cli_version(timeout=max(1.0, ctx.timeout_s - 0.5))
        except Exception as exc:  # noqa: BLE001
            return fail(f"the Claude Code CLI does not run: {type(exc).__name__}"[:200])
    if not has_token:
        return fail(
            f"Claude Code CLI {version}, but {TOKEN_ENV} is {source} and this host has no "
            "Claude Code login: sign in with `claude`, or run `robothor claude-code login`"
        )
    return ok(f"Claude Code CLI {version}; credential from the {source}")


CHECKS: tuple[Check, ...] = (
    Check(
        id="claude_code.ready",
        title="The Claude Code driver has its CLI and token",
        category="claude_code",
        severity="recommended",
        run=_ready,
    ),
)
