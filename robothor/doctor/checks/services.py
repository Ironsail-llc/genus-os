"""Are the long-running services up?

Every probe goes through :func:`robothor.config.probe_service`, which is not
re-implemented here for one reason: it prefers ``/ready`` (unauthenticated
everywhere) and reads a 401/403 from ``/health`` as proof the service is UP and
enforcing auth. A second implementation that missed that would report a healthy
production bridge as down -- which is exactly what the CLI did before that
function existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]


def _loopback(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _engine_url(engine: Any) -> str:
    """The engine's base URL, preferring the PORT over the default URL.

    ``ROBOTHOR_ENGINE_URL`` and ``ROBOTHOR_ENGINE_PORT`` are two names for
    overlapping facts, and they carry the same default. An operator who moves
    the engine sets the port -- so taking the URL unconditionally would probe
    18800 on a box serving 19000 and report a healthy engine as down. The URL
    wins only when it has actually been changed from its default, which is the
    case where it says something the port cannot.
    """
    from robothor.settings.model import EngineSettings

    default_url = EngineSettings.model_fields["url"].default
    if engine.url and engine.url != default_url:
        return str(engine.url)
    return _loopback(engine.port)


def _urls(ctx: DoctorContext) -> dict[str, str]:
    settings = ctx.settings
    return {
        "engine": _engine_url(settings.engine),
        "bridge": _loopback(settings.auth.bridge_port),
        "orchestrator": _loopback(settings.services.orchestrator_port),
        "vision": _loopback(settings.services.vision_port),
    }


def _probe(url: str, timeout: float) -> tuple[bool, str]:
    from robothor.config import probe_service

    return probe_service(url, timeout=timeout)


#: How to start each service on an install that carries no systemd units.
#: Quoted back to the operator in the skip, because "not started" without the
#: command that starts it is a dead end.
_START_HINTS = {
    "engine": "genus engine start",
    "bridge": "genus serve",
    "orchestrator": "genus serve",
    "vision": "genus vision serve",
}


def _units_installed() -> bool:
    """Does this host carry the systemd units that would run the services?

    The same question ``genus start`` asks before it shells out, and the only
    honest way to tell "this instance is down" from "this instance was never
    meant to be running here". A wheel install has no units: ``genus start``
    prints "skipped (not installed)" and starts nothing, so requiring the
    services afterwards demands a state nothing on the box can produce.
    """
    import shutil
    import subprocess

    if shutil.which("systemctl") is None:
        return False
    try:
        listed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["systemctl", "list-unit-files", "robothor-engine.service"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001 - no systemd is an answer, not a crash
        return False
    return "robothor-engine.service" in listed.stdout


def _substrate_runs_services(ctx: DoctorContext) -> bool:
    """Does this instance's substrate run the services by itself?

    A compose instance's services are CONTAINERS, so it carries no systemd
    unit -- and reading that as "nothing was meant to be running here" turned
    a crashed bridge into a skip that also told the operator to run `genus
    serve`, which is not how that instance starts. `genus init` records the
    substrate it set up; this is the question that record exists to answer.
    """
    try:
        return str(ctx.settings.substrate.init_substrate or "").strip().lower() == "compose"
    except Exception:  # noqa: BLE001 - config.settings_load owns unreadable settings
        return False


async def _a_sibling_is_up(ctx: DoctorContext, name: str) -> bool:
    """Is any OTHER service on this box answering?

    The documented wheel-install happy path starts the services by hand, so
    nothing on that machine carries a unit and nothing recorded an intention.
    One sibling answering is the evidence: an instance serving on three ports
    and refusing on the fourth is down, not un-started.
    """
    for other, url in _urls(ctx).items():
        if other == name:
            continue
        up, _detail = await ctx.run_blocking(_probe, url, ctx.timeout_s)
        if up:
            return True
    return False


def _service_check(name: str, severity: str, meaning: str) -> Check:
    async def run(ctx: DoctorContext) -> Result:
        url = _urls(ctx)[name]
        up, detail = await ctx.run_blocking(_probe, url, ctx.timeout_s)
        if up:
            # Truth beats the expectation: something answered, so it is up.
            return ok(detail)

        expected = ctx.services_expected
        if expected is None:
            # Three ways to learn this box is supposed to be serving, cheapest
            # first. The sibling probe is last because it costs three more
            # requests, and it only runs on a box that has already failed one.
            expected = (
                _substrate_runs_services(ctx)
                or await ctx.run_blocking(_units_installed)
                or await _a_sibling_is_up(ctx, name)
            )
        if expected:
            return fail(detail)
        return skip(
            f"{detail}; services not started on this install — "
            f"start it with `{_START_HINTS[name]}` (or `genus init --start`), "
            "then run genus doctor again"
        )

    run.__doc__ = (
        f"The {name} service answers a health endpoint on loopback.\n\n{meaning}\n\n"
        "A 401 or 403 counts as UP: in production the bridge gates /health behind "
        "auth, and a service that is enforcing authentication is running. "
        "Only a refused connection, a timeout or a 5xx is a failure."
    )
    return Check(
        id=f"service.{name}",
        title=f"The {name} service is up",
        category="services",
        severity=severity,  # type: ignore[arg-type]
        run=run,
    )


CHECKS: tuple[Check, ...] = (
    _service_check(
        "engine",
        "required",
        "The engine runs every agent, every schedule and every tool call. "
        "Nothing in this instance happens without it.",
    ),
    _service_check(
        "bridge",
        "required",
        "The bridge is the only door to the CRM, the memory API and the Helm. "
        "With it down the dashboard is blank and agents cannot read or write "
        "people, tasks or notes.",
    ),
    _service_check(
        "orchestrator",
        "recommended",
        "The orchestrator serves retrieval-augmented answers. Agents fall back "
        "to direct memory queries without it, so this degrades recall rather "
        "than stopping work.",
    ),
    _service_check(
        "vision",
        "recommended",
        "The vision service handles camera capture and image understanding. "
        "An instance with no camera does not need it.",
    ),
)
