"""Who gets contained, and who is allowed to say no.

Extracted from `runner.py`, which is a 2,957-line god-object held at a
decomposition ratchet. Moving a cohesive cluster out is the documented remedy;
raising the cap by the size of each fix is how a ratchet stops being one.

The finding this module exists to make visible, measured 2026-08-27:

    6 agents hold `exec`
    4 of them declare `sandbox: host` — main, crm-hygiene,
      conversation-inbox, vision-monitor
    the opt-out is honoured BEFORE the mode is consulted

So promoting `ROBOTHOR_SANDBOX_DEFAULT_MODE` to `enforce` would containerise
`auto-agent` and `email-analyst` and nothing else, while the operator
reasonably believes they have just contained their fleet. The dashboard would
say `enforce` and be telling the truth.

The opt-out stays — some agents genuinely need the host, and silently
containerising `main` would be a worse failure than not containerising it. What
changes is that the opt-out is stated, countable, and overridable, so a
promotion can be argued from a number instead of an assumption.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from robothor.engine.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: Lets `enforce` override a manifest's `sandbox: host`. Deliberately not the
#: default: an operator who has not read the opt-out list should not discover
#: it by having `main` start failing inside a container.
OVERRIDE_OPT_OUT_ENV = "ROBOTHOR_SANDBOX_ENFORCE_OVERRIDES_MANIFEST"

#: One warning per agent per process, not per run.
_warned: set[str] = set()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def agent_holds_exec(config: Any) -> bool:
    """True if the agent can call ``exec`` (i.e. touches the host shell).

    An empty ``tools_allowed`` means the agent receives the full tool set
    (including ``exec``); a ``tools_denied`` entry removes it.
    """
    denied = set(getattr(config, "tools_denied", None) or [])
    if "exec" in denied:
        return False
    allowed = getattr(config, "tools_allowed", None) or []
    return "exec" in allowed or not allowed


def resolve_sandbox_decision(config: Any, mode: str, *, agent_id: str = "") -> str:
    """``'docker'`` | ``'observe'`` | ``'host'`` for one run.

    ``observe`` means an exec-holding agent that WOULD be contained but runs on
    the host; the caller logs it. Observe must never change behaviour — that is
    the entire point of the mode — so the override below applies only under
    ``enforce``. An override that quietly contained agents during observe would
    make the would-block set a record of the override rather than of the fleet.
    """
    declared = getattr(config, "sandbox", "") or ""

    if declared == "docker":
        return "docker"

    holds_exec = agent_holds_exec(config)

    if declared == "host":
        # Only worth mentioning when it is actually bypassing something: an
        # agent with no `exec` is not weakening anything by asking for the
        # host, and warning about it would be noise.
        if holds_exec and mode in ("observe", "enforce"):
            _warn_opt_out(agent_id or getattr(config, "id", "") or "<unknown>", mode)
        if not (holds_exec and _truthy(OVERRIDE_OPT_OUT_ENV)):
            return "host"
        # With the override set, an opted-out agent is in scope again.
        # Under observe it is REPORTED but still runs on the host: that does
        # not change behaviour (both run on the host either way), and it is
        # what tells the operator which agents the override would newly
        # capture before they flip enforce. Promoting on a would-block set
        # that silently excluded them is how you contain two agents out of six
        # and believe you contained the fleet.
        return "docker" if mode == "enforce" else "observe"

    if mode != "off" and holds_exec:
        return "docker" if mode == "enforce" else "observe"
    return "host"


def _warn_opt_out(agent_id: str, mode: str) -> None:
    if agent_id in _warned:
        return
    _warned.add(agent_id)
    logger.warning(
        "Agent %s holds `exec` and declares `sandbox: host`, so it opts out of "
        "containment. sandbox_default is in %s mode; this agent is unaffected "
        "by it either way. Set %s=1 to make enforce override the manifest.",
        # Agent ids come from manifest files, so a newline in one could forge a
        # second log line saying whatever it liked about containment.
        sanitize_log(agent_id),
        mode,
        OVERRIDE_OPT_OUT_ENV,
    )


def opted_out_of_containment(manifests: Iterable[dict[str, Any]]) -> list[str]:
    """Agent ids that hold ``exec`` and decline the sandbox.

    One number for the readiness probe, the dashboard and any promotion
    argument, rather than three callers each re-deriving it slightly
    differently — which is how a hand-maintained agent-name list drifted from
    what was registered and produced three separate bug reports for one defect.
    """
    out = []
    for manifest in manifests:
        if manifest.get("sandbox") != "host":
            continue
        shim = type(
            "_M",
            (),
            {
                "tools_allowed": manifest.get("tools_allowed", []),
                "tools_denied": manifest.get("tools_denied", []),
            },
        )()
        if agent_holds_exec(shim):
            out.append(str(manifest.get("id", "")))
    return out
