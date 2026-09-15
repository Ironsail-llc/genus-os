"""Microsoft Teams as a place output goes — and, with the router, comes back from.

The outbound half owns its own transport and opens it lazily inside
:meth:`TeamsChannel.send`, exactly as ``robothor/engine/channels/base.py``
insists: nothing calls ``start()`` in this release, so a channel that connected
there would ship green tests and deliver nothing.

What proof means here
---------------------
``POST {serviceUrl}/v3/conversations/{id}/activities`` answers with the
platform's own activity id. One response per chunk that landed; a chunk that was
rejected is simply absent, which is the contract ``receipt_from`` counts on
every other surface. Nothing in this module reads "the call did not raise" as
delivery.

The one thing Teams cannot do that Telegram and Slack can
---------------------------------------------------------
Address somebody from an id alone. A proactive activity needs a *conversation
reference* — the tenant's regional ``serviceUrl`` and the conversation id — and
both exist only because the inbound half wrote them down when that person first
spoke. So a target with no recorded reference is
``failed:teams_no_conversation_reference``: loud, in the same ``agent_runs``
column the dashboard already renders, and never a fallback. There is no "the
conversation we happen to know about", and inventing one would post a briefing
into a room nobody chose.

What is never logged or reported
--------------------------------
The client secret, the bot token, or any fragment of either. Conversation ids
and directory object ids are the operator's own instance data: they appear in
``health`` and ``verify`` because the operator asked this instance about itself,
and in no log line.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from genus_teams.credentials import APP_ID_ENV, APP_PASSWORD_ENV, teams_credentials
from genus_teams.tokens import TokenError, TokenSource, build_client
from robothor.constants import DEFAULT_TENANT
from robothor.engine.channels import conversations
from robothor.engine.channels.base import (
    UNCONFIGURED_STEP,
    NoListenerError,
    SendReceipt,
    receipt_from,
)
from robothor.engine.chunking import split_message

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = ["MAX_TEAMS_LENGTH", "TeamsChannel"]

#: Teams caps a single message at roughly 28 KB of HTML. Characters are what the
#: splitter counts and the envelope costs something, so this is deliberately
#: under the documented ceiling: a chunk that comes back 413 is a chunk nobody
#: read, and the margin is cheaper than the failure.
MAX_TEAMS_LENGTH = 20000

#: How a delivery target is recognised. Only used to refuse the obviously
#: wrong — an id shape Microsoft changes is not something to hard-code — and a
#: target that is neither is still looked up, because the conversation store is
#: the authority on what this instance can actually reach.
_UNEXPANDED = "${"


class TeamsChannel:
    """Teams, reachable as ``delivery.channel: teams`` once the operator arms it.

    Inert until ``ROBOTHOR_CHANNELS`` names ``teams``: the channel registry
    refuses to resolve an installed-but-unnamed plugin channel, because a
    package that became the delivery surface merely by being installed could
    intercept every briefing while nothing looked different.
    """

    name = "teams"

    #: ``ask_user`` calls ``ask`` with exactly five arguments unless a channel
    #: opts in; Teams does not need the durable-question extension because it
    #: has an inbound socket of its own.
    ask_wants_question_id = False

    def __init__(self, *, tenant_id: str = DEFAULT_TENANT) -> None:
        self.tenant_id = tenant_id
        self._tokens = TokenSource()
        self._router: Any | None = None
        self._router_built = False

    # ── the receiving half ───────────────────────────────────────────────

    @property
    def inbound_router(self) -> Any | None:
        """The FastAPI router the engine mounts, or ``None``.

        Built once, lazily, and never at import time: ``fastapi`` is an optional
        extra on a base install, and a channel that could not be *imported*
        would resolve as ``failed:no_channel:teams`` — which reads as "this
        platform has no Teams support", a different problem with a different
        fix. Without a router the channel still sends; it simply cannot receive,
        and ``ask`` says so rather than waiting on an answer that cannot arrive.
        """
        if self._router_built:
            return self._router
        self._router_built = True
        try:
            from genus_teams.router import build_router

            self._router = build_router(self)
        except Exception as exc:  # noqa: BLE001 — a missing extra is not a crash
            logger.warning("Teams: no inbound router (%s: %s)", type(exc).__name__, exc)
            self._router = None
        return self._router

    # ── protocol ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """No-op: the transport opens inside :meth:`send`."""
        return

    async def stop(self) -> None:
        """No-op: see :meth:`start`."""
        return

    async def health(self) -> dict[str, Any]:
        """What the operator needs to see. No secret, and no fragment of one."""
        credentials = teams_credentials(tenant_id=self.tenant_id)
        report: dict[str, Any] = {
            "channel": self.name,
            "configured": credentials.can_send,
            "ok": False,
            # Presence and provenance, never values. "which layer answered" is
            # the question an operator has when the UI and the daemon disagree.
            "app_id": {
                "present": bool(credentials.app_id),
                "source": credentials.app_id_source,
            },
            "client_secret": {
                "present": bool(credentials.app_password),
                "source": credentials.app_password_source,
            },
            "directory_tenant": {
                "present": bool(credentials.directory_tenant_id),
                "source": credentials.directory_tenant_source,
            },
            "endpoint_mounted": self.inbound_router is not None,
        }
        if not credentials.can_send:
            report["token"] = self._tokens.last_attempt()
            return report
        try:
            await self._tokens.token()
            report["ok"] = True
        except TokenError as exc:
            report["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 — health never raises
            report["error"] = type(exc).__name__
        report["token"] = self._tokens.last_attempt()
        return report

    async def send(
        self,
        target: str,
        text: str,
        *,
        config: Any | None = None,
        run: Any | None = None,
        **kw: Any,
    ) -> SendReceipt:
        """Post ``text`` into ``target``'s conversation; report only what landed.

        The manifest guards run before the credential is read: an unexpanded
        ``${TEAMS_CHAT}`` is the same defect whether or not this instance has a
        bot, and answering "not configured" would send the operator after the
        wrong thing.
        """
        clean = (target or "").strip()
        if not clean:
            logger.warning("No Teams delivery target for %s", getattr(config, "id", "?"))
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:teams_no_target", target=clean
            )
        if _UNEXPANDED in clean:
            logger.error(
                "Unexpanded env var in the Teams delivery target for %s",
                getattr(config, "id", "?"),
            )
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:teams_unexpanded_target", target=clean
            )

        credentials = teams_credentials(tenant_id=self.tenant_id)
        if not credentials.can_send:
            logger.warning(
                "Agent %s announces on Teams but %s / %s are set neither in the "
                "environment nor in the vault",
                getattr(config, "id", "?"),
                APP_ID_ENV,
                APP_PASSWORD_ENV,
            )
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:teams_not_configured", target=clean
            )

        reference = await self._reference(clean)
        if reference is None:
            # Loud, per the registry's rule. Teams cannot be addressed from an
            # id: somebody has to have spoken to this bot first, and pretending
            # otherwise would post into whichever conversation happened to be
            # at hand.
            logger.error(
                "Teams has no recorded conversation for the delivery target of %s; "
                "the person or channel must message the bot once before it can post",
                getattr(config, "id", "?"),
            )
            return SendReceipt(
                acknowledged=0,
                expected=1,
                status="failed:teams_no_conversation_reference",
                target=clean,
            )

        name = getattr(config, "name", "") if config is not None else ""
        body = f"{name}\n\n{text}" if name else text
        chunks = split_message(body, MAX_TEAMS_LENGTH)

        try:
            token = await self._tokens.token()
        except TokenError as exc:
            logger.error("Teams delivery for %s has no token: %s", getattr(config, "id", "?"), exc)
            return SendReceipt(
                acknowledged=0,
                expected=len(chunks),
                status="failed:teams_auth",
                target=clean,
                body=body,
            )

        landed = await self._post_activities(reference, token, chunks)
        receipt = receipt_from(landed, len(chunks), target=clean, body=body)
        if receipt.acknowledged == 0:
            logger.error(
                "Teams delivery for %s acknowledged 0 of %d chunk(s) — nobody saw it",
                getattr(config, "id", "?"),
                len(chunks),
            )
        elif not receipt.complete:
            logger.error(
                "Teams delivery for %s was truncated: %d of %d chunk(s) landed",
                getattr(config, "id", "?"),
                receipt.acknowledged,
                len(chunks),
            )
        return receipt

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        timeout: float = 300.0,
        target: str = "",
        addressee: str = "",
    ) -> str | None:
        """Put ``question`` to ``addressee`` in ``target``'s conversation.

        An Adaptive Card: the options as ``Action.Submit`` buttons, or a text
        input when there are none. The pending question is bound to BOTH the
        conversation and the addressee's directory object id, and only an answer
        matching both settles it — the binding
        :mod:`robothor.engine.channels.telegram_ask` established, for the reason
        it established it: an answer typed by one person must never settle a
        question asked of another.

        ``None`` means nobody answered — the clock ran out, or the card never
        went out. It is never one of ``options``: a default answer here is an
        approval nobody gave.

        Raises:
            NoListenerError: there is no inbound router mounted, so a card would
                be a question with no way back. The distinction matters to the
                caller: ``None`` is "they did not reply", this is "nobody could
                have".
        """
        from genus_teams import ask as ask_module

        if self.inbound_router is None:
            raise NoListenerError(
                "the Teams messaging endpoint is not mounted on this instance, so "
                "a card would have no way to be answered; the question is recorded "
                "as an agent_questions row instead"
            )
        return await ask_module.ask_over_card(
            self,
            question,
            options,
            timeout=timeout,
            target=target,
            addressee=addressee,
        )

    async def resolve_identity(self, native_id: str, *, tenant_id: str = DEFAULT_TENANT) -> Any:
        """Map a directory object id onto a Genus identity, or ``None``.

        Through the shared resolver, like Slack: one resolution path and one
        cache, and a pairing writes the row this reads.
        """
        from robothor.identity.resolvers import resolve_identity

        return await asyncio.to_thread(resolve_identity, self.name, native_id, tenant_id)

    # ── verify ───────────────────────────────────────────────────────────

    async def verify(self, target: str | None = None) -> list[tuple[str, bool, str]]:
        """Prove, step by step, that this instance can actually reach Teams.

        ``credentials`` — what is set, and which layer it came from. No values.
        ``oauth2.token`` — Entra accepts the client credentials. The single most
        common setup failure (a secret that was copied from the *id* field, or
        one that expired — Azure client secrets do, at most two years out).
        ``activity.typing`` — a real activity, POSTed to a real conversation,
        with the platform's own id returned. The only step that proves reach.

        Returns ``(step, ok, detail)``. Nothing in the result carries a
        credential: it is printed by ``genus channel verify`` and pasted into
        bug reports.
        """
        credentials = teams_credentials(tenant_id=self.tenant_id, live=True)
        if not credentials.app_id and not credentials.app_password:
            # ONE step, named `configuration`: how `genus channel verify` tells
            # "never set up" (exit 2) from "set up and broken" (exit 1).
            return [
                (
                    UNCONFIGURED_STEP,
                    False,
                    f"{APP_ID_ENV} and {APP_PASSWORD_ENV} are set neither in the "
                    "environment nor in this instance's vault",
                )
            ]

        steps: list[tuple[str, bool, str]] = []
        missing = [
            env
            for env, present in (
                (APP_ID_ENV, bool(credentials.app_id)),
                (APP_PASSWORD_ENV, bool(credentials.app_password)),
            )
            if not present
        ]
        if missing:
            steps.append(("credentials", False, f"{', '.join(missing)} is not set anywhere"))
            return steps
        steps.append(
            (
                "credentials",
                True,
                f"application id from the {credentials.app_id_source}, "
                f"client secret from the {credentials.app_password_source}, "
                f"directory {credentials.token_tenant}",
            )
        )

        self._tokens.forget()
        try:
            token = await self._tokens.token()
        except TokenError as exc:
            steps.append(("oauth2.token", False, str(exc)))
            return steps
        steps.append(("oauth2.token", True, "Entra issued a bot token"))

        steps.append(await self._verify_activity(token, (target or self.verify_target()).strip()))
        return steps

    @staticmethod
    def verify_target() -> str:
        """The conversation ``verify`` aims at when told none.

        Not a send fallback and not named like one: :meth:`send` refuses an
        empty target outright.
        """
        try:
            from robothor.settings import get_settings

            return (get_settings().channels.teams_verify_target or "").strip()
        except Exception as exc:  # noqa: BLE001 — a verify diagnosing configuration
            # must not fall over on configuration.
            logger.warning("Could not resolve the Teams verify target: %s", exc)
            return ""

    async def _verify_activity(self, token: str, target: str) -> tuple[str, bool, str]:
        """Send a real (typing) activity and insist on an id coming back."""
        step = "activity.typing"
        if not target:
            return (
                step,
                False,
                "no conversation to post to: pass one with --target, or set "
                "ROBOTHOR_TEAMS_VERIFY_TARGET to a conversation this bot has "
                "already been messaged in",
            )
        reference = await self._reference(target)
        if reference is None:
            return (
                step,
                False,
                "no conversation reference is recorded for that target; somebody "
                "has to message the bot once before it can post to them",
            )
        try:
            response = await self._post_one(reference, token, {"type": "typing"})
        except Exception as exc:  # noqa: BLE001
            return (step, False, _describe(exc))
        if response is None:
            return (step, False, "Teams rejected the activity")
        return (step, True, "a typing activity was accepted by the conversation's service URL")

    # ── transport ────────────────────────────────────────────────────────

    async def _reference(self, target: str) -> Any | None:
        """The conversation reference for ``target``, or ``None``.

        Off the event loop: the store is psycopg2, like every other DAL the
        engine shares with the bridge and the CLI.
        """
        try:
            return await asyncio.to_thread(
                conversations.reference_for_target, self.name, target, tenant_id=self.tenant_id
            )
        except Exception as exc:  # noqa: BLE001 — an unreadable store is a failed
            # send, reported as one, not an exception out of `deliver()`.
            logger.error("Teams could not read the conversation store: %s", _describe(exc))
            return None

    async def _post_activities(
        self, reference: Any, token: str, chunks: list[str]
    ) -> list[dict[str, Any]]:
        """POST each chunk, keeping whatever the platform acknowledged.

        A rejected chunk is logged and skipped rather than propagated: dropping
        the ids of the chunks that ARE on somebody's screen, because a later one
        was throttled, turns ``partial:2/3`` into a total failure and breaks the
        reply mapping for messages they can read.
        """
        landed: list[dict[str, Any]] = []
        for chunk in chunks:
            try:
                response = await self._post_one(
                    reference, token, {"type": "message", "text": chunk}
                )
            except Exception as exc:  # noqa: BLE001 — one bad chunk is not a lost send
                logger.error("Teams rejected a chunk: %s", _describe(exc))
                continue
            if response is None:
                continue
            landed.append(response)
        return landed

    async def _post_one(
        self, reference: Any, token: str, activity: dict[str, Any]
    ) -> dict[str, Any] | None:
        """One activity. ``None`` when the platform did not accept it."""
        url = (
            f"{str(reference.service_url).rstrip('/')}"
            f"/v3/conversations/{reference.conversation_id}/activities"
        )
        async with build_client() as client:
            response = await client.post(
                url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 300:
            logger.error(
                "Teams answered %s to an activity for a recorded conversation",
                response.status_code,
            )
            return None
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 — accepted with no body is still accepted
            payload = {}
        return payload if isinstance(payload, dict) else {}


def _describe(exc: Exception) -> str:
    """An exception in words safe to print. Never the raw object."""
    from robothor.secrets.redaction import redact

    return redact(f"{type(exc).__name__}: {exc}")
