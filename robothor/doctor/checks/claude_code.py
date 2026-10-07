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

_HOST_LOGIN = "host Claude Code login"


async def _ready(ctx: DoctorContext) -> Result:
    """Whether coding jobs can start: the CLI, a credential, and the Bash sandbox.

    A failure names the missing half. Install the CLI for the engine's service
    user (or set ROBOTHOR_CLAUDE_BIN), then run `robothor claude-code login`,
    which stores CLAUDE_CODE_OAUTH_TOKEN in the vault. Prove it end to end with
    `robothor claude-code status`. Every job's Bash runs in Claude Code's
    sandbox with failIfUnavailable, so bubblewrap must be able to create
    unprivileged user namespaces and mount a fresh /proc in them — which the
    engine unit's ProtectKernelTunables=/ProtectKernelLogs= prevent, so the
    check also reads both off the unit (zz-claude-code.conf turns them off) —
    and socat must be installed. On the host
    login, ~/.claude and ~/.claude.json must be writable by the engine (the
    zz-claude-code.conf drop-in).
    """
    from robothor.engine.coding import env as coding_env
    from robothor.engine.coding import probe, runner, sandbox
    from robothor.engine.coding.env import TOKEN_ENV
    from robothor.secrets import secret_source

    source: str = secret_source(TOKEN_ENV)
    has_token = source in ("vault", "env")
    if not has_token and coding_env.auth_mode() != "token" and coding_env.host_login_present():
        has_token, source = True, _HOST_LOGIN
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
    problems = sandbox.sandbox_problems()
    if problems:
        return fail(
            "Claude Code's Bash sandbox cannot run here, and every coding job runs with "
            "failIfUnavailable, so every job would fail: " + "; ".join(problems)
        )
    if source == _HOST_LOGIN:
        unwritable = sandbox.unwritable_login_paths()
        if unwritable:
            return fail(
                "coding jobs use the host Claude Code login, but this process cannot write "
                f"{', '.join(unwritable)} (Claude Code refreshes its login there): install "
                "infra/systemd/robothor-engine.service.d/zz-claude-code.conf with "
                "scripts/install-units.sh, or store a token with `robothor claude-code login`"
            )
    return ok(f"Claude Code CLI {version}; credential from the {source}; Bash sandbox ready")


CHECKS: tuple[Check, ...] = (
    Check(
        id="claude_code.ready",
        title="The Claude Code driver has its CLI, a credential and its sandbox",
        category="claude_code",
        severity="recommended",
        run=_ready,
    ),
)
