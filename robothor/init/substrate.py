"""Where the instance will run, and what that changes about the wizard.

Most of the seventeen steps are the same everywhere: an operator has a name, a
database has a host, agents come from a preset. Three things are not, and they
are exactly what a substrate owns — which prerequisites are REQUIRED, how
services are started, and what the first-run URL looks like.

Only ``local`` is selectable in this release. The other three are present as
stubs that say where they land rather than as absent names, because a wizard
that silently does not offer compose is indistinguishable from one that has
forgotten it exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from robothor.init.context import InitContext
    from robothor.init.plan import InitPlan
    from robothor.init.steps import Step

__all__ = [
    "ALL_SUBSTRATES",
    "AVAILABLE_SUBSTRATES",
    "Substrate",
    "build_plan",
    "get_substrate",
]

#: What ``--substrate`` accepts today.
AVAILABLE_SUBSTRATES: tuple[str, ...] = ("local",)

#: Every substrate the design names, including the three still to land. Listed
#: so ``--substrate compose`` gets "lands in A10" rather than "unknown value".
ALL_SUBSTRATES: tuple[str, ...] = ("local", "compose", "systemd", "helm")


@runtime_checkable
class Substrate(Protocol):
    """One way of running Genus OS, as a wizard sees it."""

    name: str

    def steps(self) -> Sequence[Step]:
        """The ordered steps for this substrate, core steps included."""
        ...

    def first_run_url(self, ctx: InitContext) -> str:
        """The authenticated URL that lands the operator in the dashboard."""
        ...


def get_substrate(name: str) -> Substrate:
    """The substrate for ``name``.

    Raises ``NotImplementedError`` for one that is designed but not yet built,
    and ``ValueError`` for one that does not exist — a different answer for a
    different mistake.
    """
    chosen = (name or "").strip().lower()
    if chosen == "local":
        from robothor.init.substrates.local import LocalSubstrate

        return LocalSubstrate()
    if chosen in ALL_SUBSTRATES:
        from robothor.init import substrates

        module = getattr(substrates, chosen, None)
        if module is None:
            import importlib

            module = importlib.import_module(f"robothor.init.substrates.{chosen}")
        return module.substrate()  # type: ignore[no-any-return]
    raise ValueError(
        f"no substrate named {name!r}; available now: {', '.join(AVAILABLE_SUBSTRATES)} "
        f"(designed: {', '.join(ALL_SUBSTRATES)})"
    )


def build_plan(ctx: InitContext) -> InitPlan:
    """The ordered plan for the substrate the context names."""
    from robothor.init.plan import InitPlan

    substrate = get_substrate(ctx.substrate_name)
    return InitPlan(substrate_name=substrate.name, steps=list(substrate.steps()))
