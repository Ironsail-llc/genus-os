"""Microsoft Teams for Genus OS — the first channel that ships as a plugin.

Every channel before this one lived in the engine. The protocol, the registry
and the access gate were all built to be extended and nothing had ever extended
them: ``genus.channels`` was a socket with nothing plugged into it, which is not
an extensibility story. This distribution is the payload that proves the seam
carries a real surface — outbound delivery, an authenticated inbound endpoint,
pairing, and an interactive ``ask`` — with none of it living in the platform.

What the operator gets
----------------------
``delivery.channel: teams`` in a manifest, once they have named ``teams`` in
``ROBOTHOR_CHANNELS``. Installing this distribution does **not** arm it; see
``docs/PLUGINS.md`` and ``robothor/engine/channels/registry.py`` for why that
rule exists for channels and sandbox backends alone.

What is deliberately not here
-----------------------------
Attachments, proactive conversation creation, and any parsing of a message
beyond stripping the bot's own mention. Each is a real feature; none is needed
to answer the question this distribution exists to answer.
"""

from __future__ import annotations

from typing import Any

from genus_teams.channel import TeamsChannel

__all__ = ["CHANNEL", "PLUGIN", "TeamsChannel"]

#: The single channel instance the registry resolves ``teams`` to. A module
#: attribute rather than a factory so that ``ask`` and the inbound router share
#: one object: two instances would be two pending-question maps, and an answer
#: arriving on the router could never settle a question asked by the tool.
CHANNEL = TeamsChannel()


def _checks() -> dict[str, Any]:
    """The doctor checks. Empty rather than fatal if the doctor is unavailable.

    A diagnostic that cannot be built must not stop the channel loading: the
    operator would lose the surface as well as the check that would have told
    them why.
    """
    try:
        from genus_teams.doctor import checks

        return checks()
    except Exception:  # noqa: BLE001 - see above
        return {}


class _Plugin(dict[str, Any]):
    """The contribution map, with ``checks`` built on first read.

    ``PLUGIN["checks"] = _checks()`` at import time meant a transient failure
    inside the doctor's own imports — at that one instant — left the checks
    permanently empty, silently, for the life of the process. The diagnostics
    that exist to tell an operator why the channel is not working are the last
    thing that should be decided by a race at import.
    """

    def __missing__(self, key: str) -> Any:
        if key != "checks":
            raise KeyError(key)
        built = _checks()
        if built:
            # Cached only once it is real: an empty answer is retried, never
            # remembered.
            self["checks"] = built
        return built


#: What this distribution contributes: the channel, and the two diagnostics that
#: answer "is this actually configured, and is its endpoint reachable" without
#: the operator having to send themselves a test message.
PLUGIN: dict[str, Any] = _Plugin(
    {
        "genus_contract_version": "1.0",
        "channels": {"teams": CHANNEL},
    }
)
