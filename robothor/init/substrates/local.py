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
    OperatorStep,
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

__all__ = ["LOCAL_REQUIRED_PREREQS", "LocalLinkStep", "LocalServicesStep", "LocalSubstrate"]

#: PostgreSQL and Redis are REQUIRED for a local install, not "recommended".
#: The old wizard marked both optional on every substrate, so a box with
#: neither passed the prerequisite check and failed four steps later, after it
#: had already written an operator identity.
LOCAL_REQUIRED_PREREQS: tuple[str, ...] = ("PostgreSQL (psql)", "Redis (redis-cli)")


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
            OperatorStep(),
            ChannelsStep(),
            SecretsStep(),
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
