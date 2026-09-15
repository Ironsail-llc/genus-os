"""Two questions the operator would otherwise answer by messaging themselves.

``genus doctor`` asks a fixed set of questions about the host, and core cannot
know the ones a plugin's capability raises. These are Teams':

``genus_teams_credentials`` — is this instance configured at all, and *where*
did each value come from. Presence and provenance, never values: "which layer
answered" is the question an operator has when ``genus channel list`` and the
daemon disagree, and it is the one thing here that is safe to print.

``genus_teams_endpoint`` — is the channel armed, and is its messaging endpoint
mounted. This is the check that catches the specific silent failure this
distribution can produce: a bot that sends perfectly well, configured in Azure
with a messaging endpoint, and *armed nowhere* — so every message a person sends
it reaches a 404 while the outbound side looks healthy.

Neither check contacts Microsoft. A diagnostic that needs the network is a
diagnostic that fails on the box that most needs it.

The id prefix is not decoration: the doctor's registry refuses a plugin whose
check ids do not carry the contributing distribution's own name, so a package
cannot publish a check that looks like a platform one.
"""

from __future__ import annotations

from typing import Any

__all__ = ["checks"]

#: The distribution's name, normalised, as the doctor's registry requires it.
PREFIX = "genus_teams"


async def _credentials(_ctx: Any) -> Any:
    """What is set, and which layer it came from. No values."""
    from genus_teams.credentials import APP_ID_ENV, APP_PASSWORD_ENV, teams_credentials
    from robothor.doctor.model import fail, ok, skip

    credentials = teams_credentials(live=True)
    if not credentials.app_id and not credentials.app_password:
        # Not a failure: an instance that never wanted Teams has not failed a
        # check it did not ask for. `skip` says so and names the reason, which
        # is what keeps it from reading as a silent pass.
        return skip(
            f"Teams is not configured on this instance ({APP_ID_ENV} and "
            f"{APP_PASSWORD_ENV} are set nowhere). Run `genus channel add teams`."
        )
    missing = [
        env
        for env, present in (
            (APP_ID_ENV, bool(credentials.app_id)),
            (APP_PASSWORD_ENV, bool(credentials.app_password)),
        )
        if not present
    ]
    if missing:
        return fail(
            f"{', '.join(missing)} is set nowhere this instance reads. "
            "Run `genus channel add teams` to store it in the vault."
        )
    return ok(
        f"application id from the {credentials.app_id_source}, client secret "
        f"from the {credentials.app_password_source}, directory "
        f"{credentials.token_tenant}"
    )


async def _endpoint(_ctx: Any) -> Any:
    """Whether the channel is armed and its endpoint exists in this process."""
    from robothor.doctor.model import fail, ok, skip
    from robothor.engine.channels.registry import CHANNELS_ENV, enabled_plugin_channels

    if "teams" not in enabled_plugin_channels():
        return skip(
            f"the Teams channel is installed but not armed: add `teams` to {CHANNELS_ENV} "
            "and restart robothor-engine. Until then a manifest naming it records "
            "failed:no_channel:teams."
        )
    from robothor.engine.channels.routers import is_mounted

    # The MOUNTING, not the channel's own router attribute. The object exists as
    # soon as anything asks for it, and the engine can still have refused it: a
    # route outside /api/channels/teams, an include that raised, or no runner to
    # bind. This check exists to catch "armed nowhere while the outbound side
    # looks healthy", and reading the object made it blind to the layer above
    # the one it watches.
    if not is_mounted("teams"):
        return fail(
            "the Teams channel is armed but its messaging endpoint is not mounted "
            "in this process, so nobody can message the bot. Check the engine's "
            "log at start: the router is refused when it claims a path outside "
            "/api/channels/teams, when there is no runner to bind it to, or when "
            "FastAPI (the `api` extra) is missing."
        )
    return ok(
        "armed, and the messaging endpoint is mounted at "
        "/api/channels/teams/messages. A public HTTPS route to that path is an "
        "ingress decision this check cannot see."
    )


def checks() -> dict[str, Any]:
    """The checks, keyed by id. Built on demand; nothing here runs at import."""
    from robothor.doctor.model import Check

    return {
        f"{PREFIX}_credentials": Check(
            id=f"{PREFIX}_credentials",
            title="Teams credentials",
            category="channels",
            # Recommended, not required: an instance that has not configured
            # Teams is not a broken instance, and a plugin that could fail the
            # operator's whole install gate would be a plugin nobody dares
            # install.
            severity="recommended",
            run=_credentials,
        ),
        f"{PREFIX}_endpoint": Check(
            id=f"{PREFIX}_endpoint",
            title="Teams messaging endpoint",
            category="channels",
            severity="recommended",
            run=_endpoint,
        ),
    }
