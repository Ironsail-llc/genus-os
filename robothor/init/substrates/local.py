"""A checkout or a wheel on this machine, talking to a local PostgreSQL.

The substrate the appliance itself runs, and the one the quickstart's
``install-gate: local`` block replays. Its two own steps are the ones the
others will answer differently: how services start (here, two commands the
operator runs, or ``genus start`` with ``--start``), and what the first-run URL
is (here, loopback plus an ``ssh -L`` line when nobody is at the keyboard).
"""

from __future__ import annotations

import socket
import sys
from typing import TYPE_CHECKING, Any

from robothor.init.steps import (
    AckStep,
    AgentsStep,
    BaseStep,
    ChannelsStep,
    CheckResult,
    DatabaseStep,
    DetectStep,
    IdentityStep,
    MigrateStep,
    ModelsStep,
    PrereqsStep,
    ProviderStep,
    SecretsStep,
    SubstrateStep,
    VerifyStep,
    WorkspaceStep,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from robothor.init.context import InitContext
    from robothor.init.steps import Step

__all__ = [
    "LOCAL_REQUIRED_PREREQS",
    "SHARED_SIGNIN_SECRETS",
    "LocalLinkStep",
    "LocalServicesStep",
    "LocalSignInStep",
    "LocalSubstrate",
]

#: PostgreSQL and Redis are REQUIRED for a local install, not "recommended".
#: The old wizard marked both optional on every substrate, so a box with
#: neither passed the prerequisite check and failed four steps later, after it
#: had already written an operator identity.
LOCAL_REQUIRED_PREREQS: tuple[str, ...] = ("PostgreSQL (psql)", "Redis (redis-cli)")

#: The two secrets the dashboard and the bridge authenticate each other with.
#: `app/src/lib/services/health.ts` refuses to report ready without both, and
#: the bridge refuses every SSO exchange without the second. Named here so the
#: local substrate and the compose one cannot drift apart on which they mint.
SHARED_SIGNIN_SECRETS: tuple[str, ...] = ("AUTH_SECRET", "GENUS_BRIDGE_SSO_SECRET")


def _stdin_is_a_terminal() -> bool:
    """Whether a human is watching this install. Never raises."""
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001 - a detached stdin is not a terminal
        return False


class LocalServicesStep(BaseStep):
    """Start the engine and the bridge, or say exactly how to.

    Default is to print rather than to start. ``genus init`` is run by hand and
    by provisioning scripts alike, and launching two daemons as a side effect
    of a configuration command is a surprise in both.
    """

    id = "services"
    title = "Services"
    required = False
    resumable = False

    def __init__(self, *, starter: Callable[[], Any] | None = None) -> None:
        self._starter = starter

    @staticmethod
    def commands() -> tuple[str, str]:
        """The two commands that start this install, worded for THIS install.

        A wheel has no systemd units, so telling it to ``systemctl start``
        something is advice that cannot work; a checkout has them, and its
        in-process equivalents would start a second copy beside the units.
        """
        from robothor.setup import _detect_install_mode

        if _detect_install_mode() == "checkout":
            return (
                "sudo systemctl start robothor-engine    # the agent engine",
                "sudo systemctl start robothor-bridge robothor-app"
                "    # the bridge and the Helm dashboard",
            )
        return (
            "genus engine start    # the agent engine, in-process",
            "genus serve           # the API the bridge and dashboard call "
            "(needs: pip install genusos[api])",
        )

    def check(self, ctx: InitContext) -> CheckResult:
        if ctx.answers.get("start"):
            return CheckResult(True, detail="will start the engine and the bridge")
        return CheckResult(True, detail="will print the two commands that start the services")

    def apply(self, ctx: InitContext) -> None:
        if not ctx.answers.get("start"):
            ctx.say("  Start the services:")
            for command in self.commands():
                ctx.say(f"    {command}")
            ctx.detail(self.id, "not started; the two commands were printed")
            return

        start = self._starter
        if start is None:
            import argparse

            from robothor.cli.admin import cmd_start

            # In-process, the same function `genus start` dispatches to. A
            # subprocess here would run a different interpreter against a
            # different workspace and report its failures only as parsed stdout.
            def start() -> Any:
                return cmd_start(argparse.Namespace())

        start()
        ctx.detail(self.id, "started")


class LocalSignInStep(BaseStep):
    """Mint the two shared secrets sign-in needs, into the workspace's genus.env.

    Only the compose substrate used to write these, so ``secrets.bridge_sso`` --
    a REQUIRED doctor check -- failed every local install on a value nothing on
    that path ever produced, and a documented ``genus init --yes`` ended in exit
    1 after writing a complete instance. Had the check not caught it, the
    outcome was worse and quieter: the bridge refuses every ``/api/auth/sso``
    exchange without ``GENUS_BRIDGE_SSO_SECRET``, so the dashboard would have
    shown a sign-in page that could not work, with ``/ready`` green.

    The same 0600 file the compose substrate writes, in the same place, because
    ``genus doctor`` and ``verify`` already load it from the workspace.
    A systemd install reads it by adding one line to
    ``/etc/robothor/robothor.env`` -- see ``docs/deployment.md``.
    """

    id = "signin"
    title = "Sign-in secrets"
    #: Never skipped: a re-run must still produce a file, and `keep_or_mint`
    #: is what stops it becoming a rotation.
    resumable = False

    #: Written verbatim. The wizard turns email+password sign-in on for the
    #: instance; the dashboard asks the bridge which methods are live rather
    #: than reading its own environment.
    LOCAL_LOGIN = "true"

    def check(self, ctx: InitContext) -> CheckResult:
        from robothor.secrets.env_file import instance_env_path

        path = instance_env_path(ctx.workspace)
        action = "exists" if path.exists() else "create"
        return CheckResult(True, detail=f"will write {path} (0600)", action=action)

    def apply(self, ctx: InitContext) -> None:
        from robothor.secrets.env_file import (
            env_line,
            instance_env_path,
            keep_or_mint,
            write_private,
        )

        path = instance_env_path(ctx.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Written by `genus init`. Mode 0600 — it holds this instance's",
            "# shared sign-in secrets. `genus doctor` reads it from the",
            "# workspace and refuses it if it stops being 0600.",
            "",
        ]
        lines += [env_line(name, keep_or_mint(path, name)) for name in SHARED_SIGNIN_SECRETS]
        lines.append(env_line("GENUS_LOCAL_LOGIN", self.LOCAL_LOGIN))
        write_private(path, "\n".join(lines) + "\n")

        # Into THIS process too: `verify` runs the doctor in-process, and
        # `secrets.bridge_sso` reads the environment, not the file.
        from robothor.secrets.env_file import load_instance_env

        load_instance_env(ctx.workspace)
        from robothor.settings import reset_settings

        reset_settings()
        ctx.detail(self.id, f"{path} (0600); local email+password sign-in is on")


class LocalLinkStep(BaseStep):
    """Mint the single-use link that turns a finished install into a session.

    Before this line existed the sign-in page on a fresh box rendered no
    buttons at all: no OIDC, no Cloudflare Access, no account, and the only
    escape hatch was a dev-mode environment variable.

    A failure here never fails the install. A completed install with no printed
    link is one command away from recovery; a non-zero exit sends the operator
    back to the start of a ten-minute process.
    """

    id = "link"
    title = "First-run link"
    required = False
    #: Never skipped: each run mints a fresh single-use token, and a resumed
    #: install whose link step was "already completed" would print nothing on
    #: exactly the run where the operator needs the URL.
    resumable = False
    #: Runs even after an earlier step failed. A `verify` failure used to end
    #: the run here, so the ONE run that had written the identity, the config,
    #: the schema, the fleet and the owner account printed no way into the box
    #: -- and a fresh instance has no other way in: no account, no OIDC.
    run_on_failure = True

    def __init__(
        self,
        *,
        substrate: Any = None,
        is_a_terminal: Callable[[], bool] | None = None,
    ) -> None:
        self._substrate = substrate
        self._is_a_terminal = is_a_terminal or _stdin_is_a_terminal

    def check(self, ctx: InitContext) -> CheckResult:
        return CheckResult(True, detail="will mint a single-use sign-in link")

    def apply(self, ctx: InitContext) -> None:
        from robothor import setup_token
        from robothor.setup import helm_port

        substrate = self._substrate or LocalSubstrate()
        try:
            url = substrate.first_run_url(ctx)
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            ctx.say(f"  ! Could not mint the setup link ({type(exc).__name__}: {exc}).")
            ctx.say("    Run `genus auth setup-link` once the workspace is writable.")
            ctx.detail(
                self.id,
                f"no link ({type(exc).__name__}); run `genus auth setup-link` to mint one",
            )
            return

        ctx.first_run_url = url
        if ctx.json_mode:
            # `first_run_url` already carries it, and in json mode the human
            # lines go to stderr -- so printing them here would put a live
            # single-use credential in the log half of
            # `genus init --json > x.json 2> x.log`.
            ctx.detail(self.id, "a single-use sign-in link is in first_run_url")
            return

        minutes = max(1, setup_token.configured_ttl_seconds() // 60)
        ctx.say(f"  Open the setup wizard (the link works once, for {minutes} minutes):")
        ctx.say(f"    {url}")
        if not self._is_a_terminal():
            # Nobody is watching this terminal -- a container, a provisioning
            # script -- so the loopback address above is on a machine the
            # operator is not sitting at. Forward the port rather than exposing
            # it.
            ctx.say("    Not at this machine? Forward the port first:")
            ctx.say(f"      {setup_token.port_forward_hint(socket.getfqdn(), helm_port())}")
        ctx.detail(self.id, "a single-use sign-in link was printed (once, to this terminal)")


class LocalSubstrate:
    """Genus OS on this machine, with the services run by hand or by systemd."""

    name = "local"

    def steps(self) -> Sequence[Step]:
        return (
            AckStep(),
            SubstrateStep(),
            PrereqsStep(required=LOCAL_REQUIRED_PREREQS),
            DetectStep(),
            ProviderStep(),
            IdentityStep(),
            WorkspaceStep(),
            DatabaseStep(),
            MigrateStep(),
            ModelsStep(),
            AgentsStep(),
            ChannelsStep(),
            SecretsStep(),
            LocalSignInStep(),
            LocalServicesStep(),
            VerifyStep(),
            LocalLinkStep(),
        )

    def first_run_url(self, ctx: InitContext) -> str:
        """A loopback URL carrying a token that exists in no log.

        Loopback on purpose: the dashboard binds locally on the appliance and
        is published, if at all, through a tunnel. Printing a guessed public
        hostname would hand the operator a URL that does not resolve and a
        token that has already started expiring.
        """
        from robothor import setup_token
        from robothor.setup import helm_port

        token = setup_token.create_setup_token(ctx.workspace)
        return setup_token.setup_link("127.0.0.1", helm_port(), token)


def substrate() -> LocalSubstrate:
    """Factory, so every substrate module has the same shape."""
    return LocalSubstrate()
