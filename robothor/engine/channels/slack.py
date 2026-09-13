"""Slack as a place output goes — not as a side effect of the inbound bot.

``engine/slack.py`` is the Socket Mode listener. It registers a platform sender
as part of ``start()``, which meant outbound Slack delivery only existed on an
instance that was also *listening*: no app token, no socket, no sender, no
briefing — and the shim built around that sender could not express a thread, a
DM, or the difference between "Slack rejected this" and "Slack was never
configured here".

So the outbound half owns its own transport, built lazily inside :meth:`send`
exactly as :mod:`robothor.engine.channels.base` insists, and the inbound bot is
left untouched. The two still split with the same function and the same limit —
:data:`MAX_SLACK_LENGTH` and ``chunking.split_message`` are imported here rather
than re-derived, because two splitters that disagree by one chunk turn a
truncated briefing into a delivered one.

What is proof, here
-------------------
``chat_postMessage`` returns a ``SlackResponse`` whose ``ts`` is the message id
the channel bus maps a reply against. One response per chunk that landed, and a
chunk that raised is simply absent — the same contract
``TelegramBot.send_message`` honours, and the reason
:func:`~robothor.engine.channels.base.receipt_from` can count both surfaces with
one function. Nothing here reads "the call did not raise" as delivery.

What is never logged or reported
--------------------------------
A token, or any fragment of one. Workspace names, channel ids and user ids are
the operator's own instance data and appear only in what the operator asked for
(``health``, ``verify``), never in a log line. A ``missing_scope`` error names
the scopes it needed — those are Slack's own vocabulary, not the instance's.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robothor.engine.channels.base import UNCONFIGURED_STEP, SendReceipt, receipt_from
from robothor.engine.channels.slack_credentials import (
    APP_TOKEN_ENV,
    BOT_TOKEN_ENV,
    slack_credentials,
)
from robothor.engine.chunking import split_message
from robothor.engine.slack import MAX_SLACK_LENGTH
from robothor.secrets.redaction import redact

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from robothor.engine.models import AgentConfig, AgentRun

logger = logging.getLogger(__name__)

__all__ = ["MAX_SLACK_LENGTH", "SlackChannel", "split_message"]

#: Slack's own id prefixes. ``C`` public channel, ``G`` private channel, ``D``
#: an already-open DM conversation; ``U``/``W`` a person, which has to be turned
#: into a conversation first.
_CONVERSATION_PREFIXES = ("C", "G", "D")
_USER_PREFIXES = ("U", "W")

#: The reasons ``failed:slack_client:<reason>`` can carry. A CLOSED set, and
#: that is the point: every other ``delivery_status`` is a stable token that a
#: query, a dashboard filter or an alert rule can match on exactly, and the
#: first cut of this one embedded the exception's own text — so nothing could
#: match it beyond ``startswith("failed:")``, and a credential-bearing SDK error
#: went straight into ``agent_runs``. The detail belongs in the log line, where
#: ``_describe`` has already stripped it.
_TRANSPORT = "transport"
_DM_OPEN = "dm_open"


def _is_id(target: str, prefixes: tuple[str, ...]) -> bool:
    return (
        len(target) > 1 and target[0] in prefixes and target.isalnum() and target.upper() == target
    )


def _slack_error(exc: BaseException) -> dict[str, Any]:
    """The error payload a ``SlackApiError`` carries, or an empty mapping.

    Duck-typed on purpose: ``slack_sdk`` is an optional dependency and importing
    it here to name one exception class would make the whole outbound channel
    unimportable on an instance that has not installed the ``channels`` extra —
    including the branch whose entire job is to report that it is missing.
    """
    payload = getattr(exc, "response", None)
    data = getattr(payload, "data", payload)
    return data if isinstance(data, dict) else {}


def _describe(exc: BaseException) -> str:
    """One line about a Slack failure, naming scopes but never a credential.

    Every return goes through :func:`~robothor.secrets.redaction.redact`: a
    third-party exception's message is not this module's to trust, and this
    string reaches both the journal and ``genus channel verify``'s output. The
    pattern set is shared with the CLI's argument parser, which has the same
    problem from the other direction — argparse printing a mistyped token.
    """
    data = _slack_error(exc)
    error = str(data.get("error") or "") or f"{type(exc).__name__}: {exc}"
    if data.get("error") == "missing_scope":
        needed = str(data.get("needed") or "?")
        provided = str(data.get("provided") or "")
        detail = f"missing_scope: the app needs {needed}"
        return redact(f"{detail} (it has {provided})" if provided else detail)
    return redact(error)


class SlackChannel:
    """Outbound Slack, reachable as ``delivery.channel: slack``.

    Registered whether or not this instance has Slack configured. An
    unconfigured send is answered ``failed:slack_not_configured`` rather than
    left unresolvable, because ``failed:no_channel:slack`` would tell the
    operator the platform has no Slack support at all — which is a different
    problem with a different fix.
    """

    name = "slack"

    #: The inbound half is still ``engine/slack.py``'s Socket Mode listener.
    inbound_router: Any | None = None

    def __init__(self) -> None:
        #: How a web client is built from a token. The seam the suite replaces;
        #: production builds an ``AsyncWebClient`` on first use.
        self.client_factory: Callable[[str], Any] = _build_client
        self._client: Any | None = None
        self._client_token: str | None = None
        #: ``user id -> conversation id``. ``conversations.open`` is idempotent
        #: but it is still a round trip per chunked send otherwise.
        self._dms: dict[str, str] = {}

    # ── configuration ────────────────────────────────────────────────────

    @staticmethod
    def bot_token() -> str | None:
        """The bot token, from wherever this instance keeps it. Never logged."""
        return slack_credentials().bot_token

    @staticmethod
    def app_token() -> str | None:
        """The app-level token Socket Mode needs. Never logged."""
        return slack_credentials().app_token

    @staticmethod
    def verify_target() -> str:
        """The conversation ``verify`` and the doctor aim at, when told none.

        Not a send fallback and not named like one: ``send`` refuses an empty
        target outright, because putting a briefing into whatever conversation
        the box happens to point at is the failure
        ``failed:telegram_unexpanded_chat_id`` was created to stop, one surface
        further on.
        """
        try:
            from robothor.settings import get_settings

            return (get_settings().channels.slack_verify_target or "").strip()
        except Exception as exc:  # noqa: BLE001 — unrelated bad config must not
            # break a verify whose whole job is diagnosing configuration.
            logger.warning("Could not resolve the Slack verify target: %s", exc)
            return ""

    def _client_for(self, token: str) -> Any:
        """The web client for ``token``, cached per token value.

        Keyed on the token rather than built once, so a rotation that reaches
        the process environment is picked up without a restart instead of the
        channel going on posting with a revoked credential.
        """
        if self._client is None or self._client_token != token:
            self._client = self.client_factory(token)
            self._client_token = token
        return self._client

    # ── protocol ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """No-op: the transport opens inside :meth:`send`."""
        return

    async def stop(self) -> None:
        """No-op: see :meth:`start`."""
        return

    async def health(self) -> dict[str, Any]:
        """What the operator needs to see. No token, and no fragment of one."""
        token = self.bot_token()
        report: dict[str, Any] = {"channel": self.name, "configured": bool(token), "ok": False}
        if not token:
            return report
        try:
            response = await self._client_for(token).auth_test()
        except Exception as exc:  # noqa: BLE001 — health never raises
            report["error"] = _describe(exc)
            return report
        report["ok"] = True
        report["team"] = response.get("team")
        report["bot_user_id"] = response.get("user_id")
        return report

    async def send(
        self,
        target: str,
        text: str,
        *,
        config: AgentConfig | None = None,
        run: AgentRun | None = None,
        thread: str | None = None,
        **kw: Any,
    ) -> SendReceipt:
        """Post ``text`` to ``target`` and report only what Slack acknowledged.

        Guards run before the credential is even resolved: a manifest defect is
        the same defect whether or not this instance has a token, and saying
        "not configured" about an unexpanded ``${SLACK_CHANNEL}`` would send the
        operator after the wrong thing.
        """
        clean = (target or "").strip()
        if not clean:
            logger.warning("No Slack delivery target for %s", getattr(config, "id", "?"))
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:slack_no_target", target=clean
            )
        if "${" in clean:
            # `failed:telegram_unexpanded_chat_id` exists because a manifest
            # shipped with an unexpanded variable once. Every surface gets it.
            logger.error(
                "Unexpanded env var in the Slack delivery target for %s", getattr(config, "id", "?")
            )
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:slack_unexpanded_target", target=clean
            )
        if not _is_id(clean, _CONVERSATION_PREFIXES) and not _is_id(clean, _USER_PREFIXES):
            logger.error(
                "Slack delivery target for %s is neither a conversation id (C/G/D…) "
                "nor a user id (U/W…); a channel NAME cannot be posted to",
                getattr(config, "id", "?"),
            )
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:slack_unresolved_target", target=clean
            )

        token = self.bot_token()
        if not token:
            logger.warning(
                "Agent %s announces on Slack but %s is set neither in the environment "
                "nor in the vault",
                getattr(config, "id", "?"),
                BOT_TOKEN_ENV,
            )
            return SendReceipt(
                acknowledged=0, expected=1, status="failed:slack_not_configured", target=clean
            )

        # The agent's display name, plain — Telegram's `*name*` is Telegram
        # markdown and renders as literal asterisks here. Same header the sender
        # shim added, so no instance's briefings change shape.
        name = getattr(config, "name", "") if config is not None else ""
        body = f"{name}\n\n{text}" if name else text
        chunks = split_message(body, MAX_SLACK_LENGTH)

        try:
            client = self._client_for(token)
        except Exception as exc:  # noqa: BLE001 — a transport failure is a receipt
            # `_describe`, never the raw exception. A malformed token — one that
            # survived the vault with an embedded newline, say — makes the SDK
            # raise `ValueError: Invalid header value b'Bearer xoxb-…'`, and this
            # line goes to the journal while the status below goes into
            # `agent_runs`, which the dashboard renders.
            logger.error(
                "Slack transport unavailable for %s: %s",
                getattr(config, "id", "?"),
                _describe(exc),
            )
            return SendReceipt(
                acknowledged=0,
                expected=len(chunks),
                status=f"failed:slack_client:{_TRANSPORT}",
                target=clean,
                body=body,
            )

        try:
            conversation = await self._conversation(client, clean)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Slack could not open a conversation for %s: %s",
                getattr(config, "id", "?"),
                _describe(exc),
            )
            return SendReceipt(
                acknowledged=0,
                expected=len(chunks),
                status=f"failed:slack_client:{_DM_OPEN}",
                target=clean,
                body=body,
            )

        landed = await self._post(client, conversation, chunks, thread)
        receipt = receipt_from(landed, len(chunks), target=clean, body=body)
        if receipt.acknowledged == 0:
            logger.error(
                "Slack delivery for %s acknowledged 0 of %d chunk(s) — nobody saw it",
                getattr(config, "id", "?"),
                len(chunks),
            )
        elif not receipt.complete:
            logger.error(
                "Slack delivery for %s was truncated: %d of %d chunk(s) landed",
                getattr(config, "id", "?"),
                receipt.acknowledged,
                len(chunks),
            )
        return receipt

    async def _conversation(self, client: Any, target: str) -> str:
        """The conversation id to post into, opening a DM when asked for a user."""
        if not _is_id(target, _USER_PREFIXES):
            return target
        cached = self._dms.get(target)
        if cached:
            return cached
        response = await client.conversations_open(users=target)
        channel = response.get("channel") or {}
        opened = str(channel.get("id") or "") if isinstance(channel, dict) else str(channel)
        if not opened:
            raise RuntimeError("conversations.open returned no conversation id")
        self._dms[target] = opened
        return opened

    async def _post(
        self, client: Any, conversation: str, chunks: list[str], thread: str | None
    ) -> list[Any]:
        """Post each chunk, keeping whatever landed.

        A chunk that raises is logged and skipped rather than propagated: losing
        the ids of the two chunks the operator can read, because the third was
        rejected, turns a ``partial:2/3`` into a total failure and breaks the
        reply mapping for the messages that are on their screen.

        With no explicit ``thread``, chunks 2..n hang off the first chunk's
        ``ts``. Three loose 4,000-character posts in a busy channel is not a
        briefing, and Slack offers no other way to keep them together.
        """
        landed: list[Any] = []
        thread_ts = thread
        for chunk in chunks:
            try:
                response = await client.chat_postMessage(
                    channel=conversation,
                    text=chunk,
                    mrkdwn=True,
                    **({"thread_ts": thread_ts} if thread_ts else {}),
                )
            except Exception as exc:  # noqa: BLE001 — one bad chunk is not a lost send
                logger.error("Slack rejected a chunk: %s", _describe(exc))
                continue
            landed.append(response)
            if thread_ts is None:
                thread_ts = response.get("ts")
        return landed

    # ── scopes ───────────────────────────────────────────────────────────

    async def scope_probe(self) -> str | None:
        """``None`` when the app's read scopes answer, else what it is missing.

        One read-only round trip (``conversations.list(limit=1)``) that catches
        an app installed without the scopes this module calls. Shared by
        :meth:`verify` and the doctor's ``slack.verify`` so there is one probe
        and one wording, rather than a CLI that reports a missing scope and a
        Health panel that shows green for the same app.

        Opens no transport of its own beyond the cached client, and returns a
        sentence rather than raising: a diagnostic that raises on a broken box
        helps nobody.
        """
        token = self.bot_token()
        if not token:
            return f"{BOT_TOKEN_ENV} is set nowhere this instance reads"
        try:
            return await self._probe_scopes(self._client_for(token))
        except Exception as exc:  # noqa: BLE001 — a probe is a report, not a raise
            return _describe(exc)

    @staticmethod
    async def _probe_scopes(client: Any) -> str | None:
        """The probe itself, against an already-built client."""
        try:
            await client.conversations_list(limit=1)
        except Exception as exc:  # noqa: BLE001
            return _describe(exc)
        return None

    # ── verify ───────────────────────────────────────────────────────────

    async def verify(self, target: str | None = None) -> list[tuple[str, bool, str]]:
        """Prove, step by step, that this instance can actually post to Slack.

        Four steps, in the order an operator's setup fails:

        ``auth.test`` — the bot token is real and unrevoked.
        ``conversations.list`` — the OAuth scopes are the ones the code calls. A
        ``missing_scope`` error is the single most common Slack setup failure
        and it is invisible until a send; the error names the scope it wanted,
        and that name is the whole fix.
        ``chat.postMessage`` — a real message, with a real ``ts`` returned. The
        only step that proves reach, which is why it is here and not merely
        implied by the two above.
        ``apps.connections.open`` — Socket Mode, i.e. whether the *inbound* half
        will start. A token that posts fine and cannot open a socket is an
        instance that answers nobody.

        Returns ``(step, ok, detail)`` per step. No token, and no fragment of
        one, reaches the result: it is printed by ``genus channel verify`` and
        pasted into bug reports.
        """
        token = self.bot_token()
        if not token:
            # ONE step, named `configuration`: that shape is how `genus channel
            # verify` tells "never set up" (exit 2) from "set up and broken"
            # (exit 1) without a second round trip to ask.
            return [
                (
                    UNCONFIGURED_STEP,
                    False,
                    f"{BOT_TOKEN_ENV} is set neither in the environment nor in this "
                    "instance's vault",
                )
            ]

        steps: list[tuple[str, bool, str]] = []
        try:
            client = self._client_for(token)
        except Exception as exc:  # noqa: BLE001
            return [("client", False, _describe(exc))]

        try:
            auth = await client.auth_test()
        except Exception as exc:  # noqa: BLE001
            steps.append(("auth.test", False, _describe(exc)))
        else:
            # Deliberately NOT the team name or the bot id. ``health()`` reports
            # both because the operator asked this instance about itself; this
            # report is the one the module docstring says gets "pasted into bug
            # reports", and a workspace name is the operator's own data.
            del auth
            steps.append(("auth.test", True, "the bot token is live"))

        missing = await self._probe_scopes(client)
        if missing is None:
            steps.append(("conversations.list", True, "the app's read scopes answer"))
        else:
            steps.append(("conversations.list", False, missing))

        steps.append(await self._verify_post(client, (target or self.verify_target()).strip()))
        steps.append(await self._verify_socket())
        return steps

    async def _verify_post(self, client: Any, target: str) -> tuple[str, bool, str]:
        """Post a real message and insist on a ``ts``."""
        step = "chat.postMessage"
        if not target:
            return (
                step,
                False,
                "no target to post to: pass one, or set ROBOTHOR_SLACK_VERIFY_TARGET",
            )
        try:
            conversation = await self._conversation(client, target)
            response = await client.chat_postMessage(
                channel=conversation,
                text="Genus OS channel verification.",
                mrkdwn=True,
            )
        except Exception as exc:  # noqa: BLE001
            return (step, False, _describe(exc))
        ts = response.get("ts")
        if not ts:
            # The one thing that is never evidence: the call returning without
            # an id and without raising.
            return (step, False, "Slack accepted the call but returned no message id")
        del ts  # the message id identifies a conversation; "it posted" is the finding
        return (step, True, "a message posted and Slack returned its id")

    async def _verify_socket(self) -> tuple[str, bool, str]:
        """Whether Socket Mode — the inbound half — can open a connection."""
        step = "apps.connections.open"
        app_token = self.app_token()
        if not app_token:
            return (
                step,
                False,
                f"{APP_TOKEN_ENV} is unset, so the inbound Slack bot will not start",
            )
        try:
            client = self.client_factory(app_token)
            await client.apps_connections_open()
        except Exception as exc:  # noqa: BLE001
            return (step, False, _describe(exc))
        return (step, True, "Socket Mode accepted the app token")

    # ── C8 / C10 ─────────────────────────────────────────────────────────

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        timeout: float = 300.0,
        target: str = "",
        addressee: str = "",
    ) -> str | None:
        """Not implemented yet — Block Kit buttons plus an inbound interactions
        endpoint, neither of which this channel has.

        Raising rather than returning ``None`` is the distinction
        :mod:`robothor.engine.channels.base` draws: ``None`` means *the person
        did not reply*, and this means *there is no way to ask them here*. The
        callers act on it differently — ``ask_user`` records the question
        against ``channel='slack'`` (the channel resolved; it simply cannot
        ask) with ``delivered: false``, so the operator can answer it from the
        Helm and the column still says which surface the run came from, while
        ``PermissionEscalationManager`` denies. Neither fabricates an answer,
        which is the only outcome that would actually be wrong.
        """
        raise NotImplementedError(
            "interactive ask is not implemented for the Slack channel yet; "
            "a Slack-triggered run records its question as an agent_questions row "
            "and is answered through the bridge's /api/approvals endpoint"
        )

    async def resolve_identity(self, native_id: str) -> Any:
        """Not implemented — Slack identity resolution lands with pairing."""
        raise NotImplementedError(
            "identity resolution is not implemented for the Slack channel yet; "
            "the inbound path in engine/slack.py still maps a user to "
            "f'slack:{user_id}' without consulting the identity graph"
        )


def _build_client(token: str) -> Any:
    """An ``AsyncWebClient`` for ``token``.

    Imported here, not at module scope: ``slack_sdk`` ships in the ``channels``
    extra, and an instance without it must still be able to *resolve* the
    channel and be told, in a receipt, that the dependency is missing. A
    module-level import would make the whole channel unimportable and the
    registry would report ``failed:no_channel:slack`` — which reads as "this
    platform has no Slack support", a different problem with a different fix.
    """
    from slack_sdk.web.async_client import AsyncWebClient

    return AsyncWebClient(token=token)
