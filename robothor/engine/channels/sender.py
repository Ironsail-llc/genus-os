"""A channel built from a bare platform sender.

``delivery.register_platform_sender`` predates the channel protocol: it stores a
send function in a module dict, and until now exactly one consumer read that
dict — ``_deliver_telegram``, which asked for ``"telegram"`` and nothing else.
``engine/slack.py:register_platform_sender("slack", slack_send)`` has therefore
been registering a sender that no code path could ever reach.

Wrapping each registration in a channel closes that gap without touching either
caller: a manifest naming ``delivery.channel: slack`` now resolves to something
real. It resolves to something *honest*, too. ``slack_send`` returns ``None``, so
this wrapper reports zero acknowledged and the delivery is recorded
``failed:``. That is the correct outcome for a sender that offers no evidence
anything landed, and it is strictly better than the alternative the receipt rule
exists to forbid — assuming success because the call did not raise. A channel
that wants to report delivery has to return its messages.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robothor.engine.channels.base import SendReceipt, receipt_from
from robothor.engine.chunking import split_message

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from robothor.engine.models import AgentConfig, AgentRun

logger = logging.getLogger(__name__)

__all__ = ["SenderChannel"]


def _chunk_count(body: str, chunk_size: int | None) -> int:
    """How many messages ``body`` becomes on a surface that splits at ``chunk_size``.

    Uses ``chunking.split_message`` — the same function the sender must split
    with. A ceiling of ``len(body) / chunk_size`` would be wrong in the
    dangerous direction: the splitter prefers newline boundaries, so it can
    produce MORE chunks than the arithmetic predicts, and an under-counted
    ``expected`` makes a truncated send read as complete.

    ``None`` means the sender declared no limit, so the shim treats the body as
    one message: it can only ever prove what it was told.
    """
    if not chunk_size or chunk_size <= 0:
        return 1
    return len(split_message(body, chunk_size))


class SenderChannel:
    """Adapts a ``register_platform_sender`` function to the channel protocol."""

    inbound_router: Any | None = None

    def __init__(
        self,
        name: str,
        send_func: Callable[..., Any] | None = None,
        *,
        chunk_size: int | None = None,
    ) -> None:
        self.name = name
        #: Kept only as a fallback. The registry dict is consulted first on
        #: every send so a re-registration — which is how the Telegram bot
        #: publishes its bound method at construction — is honoured rather than
        #: shadowed by whatever was current when this object was built.
        self._send_func = send_func
        #: The length the sender splits a body at, when it splits at all.
        #:
        #: This is how a chunking sender opts into truncation detection, and it
        #: is not optional in practice for one that chunks. Without it the shim
        #: hands over one body, expects one acknowledgement, and a sender that
        #: split into three and landed two reports ``acknowledged=2 >=
        #: expected=1`` — ``delivered``, for a briefing the operator received
        #: two thirds of. Telegram reports ``partial:2/3`` for identical
        #: evidence, and one surface being laxer than another about the same
        #: evidence is exactly what the shared counter exists to prevent.
        #:
        #: So omitting it while chunking anyway is refused rather than believed:
        #: a sender that hands back more than one message for the one body it
        #: was given has demonstrably split, and ``send`` records
        #: ``failed:<name>_unproven`` with a log line naming this argument.
        self.chunk_size = chunk_size

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"channel": self.name, "sender_registered": self._resolve() is not None}

    def _resolve(self) -> Callable[..., Any] | None:
        from robothor.engine.delivery import get_platform_sender

        return get_platform_sender(self.name) or self._send_func

    async def send(
        self,
        target: str,
        text: str,
        *,
        config: AgentConfig | None = None,
        run: AgentRun | None = None,
        **kw: Any,
    ) -> SendReceipt:
        """Send through the registered function and count what came back."""
        sender = self._resolve()
        if sender is None:
            logger.warning("No sender registered for channel %s", self.name)
            return SendReceipt(
                acknowledged=0, expected=1, status=f"failed:{self.name}_no_sender", target=target
            )
        if not target:
            return SendReceipt(
                acknowledged=0, expected=1, status=f"failed:{self.name}_no_target", target=target
            )
        if "${" in target:
            # `failed:telegram_unexpanded_chat_id` exists because a manifest
            # shipped with an unexpanded variable once. Every surface gets the
            # guard, or the next one posts to a literal `${SLACK_CHANNEL}`.
            logger.error(
                "Unexpanded env var in delivery target for channel %s: %s", self.name, target
            )
            return SendReceipt(
                acknowledged=0,
                expected=1,
                status=f"failed:{self.name}_unexpanded_target",
                target=target,
            )

        # The agent's display name, plain. The Telegram wrapper's ``*name*``
        # header is Telegram markdown and would render as literal asterisks on
        # any other surface; per-surface formatting arrives with the first real
        # non-Telegram target.
        body = f"{config.name}\n\n{text}" if config is not None and config.name else text

        # How many acknowledgements this body needs to count as complete. A
        # sender that declared its chunk size is measured against its own
        # splitting, so 2 of 3 reads `partial:2/3` exactly as Telegram's does.
        # One that declared none is handed a single body and owes a single
        # acknowledgement — deliberately NOT Telegram's 4096, which would record
        # `partial:1/3` for a send that completed in one message on a surface
        # with no such limit.
        expected = _chunk_count(body, self.chunk_size)

        try:
            sent = await sender(target, body)
        except Exception as e:
            logger.error("Delivery on channel %s failed: %s", self.name, e)
            return SendReceipt(
                acknowledged=0,
                expected=expected,
                status=f"failed:{self.name}_exception: {e}",
                target=target,
                body=body,
            )

        if sent and not isinstance(sent, list | tuple):
            # A sender must return the messages it landed, one per chunk. An API
            # response object is not that, and counting it would be a guess with
            # a number attached: ``list()`` of a mapping yields its KEYS, so a
            # three-field Slack response counted as three delivered chunks.
            logger.error(
                "Channel %s returned %s, not a sequence of messages — "
                "refusing to read it as proof of delivery",
                self.name,
                type(sent).__name__,
            )
            return SendReceipt(
                acknowledged=0,
                expected=expected,
                status=f"failed:{self.name}_unproven",
                target=target,
                body=body,
            )

        receipt = receipt_from(sent, expected, target=target, body=body)

        if self.chunk_size is None and receipt.acknowledged > 1:
            # The sender split the body — it returned more messages than the one
            # it was handed — while declaring no chunk size. So `expected` is 1,
            # `acknowledged` is N, and `N >= 1` reads as DELIVERED no matter how
            # many chunks were actually lost: a sender that split into five and
            # landed two would be recorded exactly like one that landed all
            # five. The evidence does not support either reading, so it supports
            # neither.
            logger.error(
                "Channel %s returned %d messages for one body but declared no chunk_size — "
                "truncation cannot be detected, so this send is unproven. Pass "
                "chunk_size= to register_platform_sender and split with "
                "chunking.split_message.",
                self.name,
                receipt.acknowledged,
            )
            return SendReceipt(
                acknowledged=0,
                expected=expected,
                platform_ids=receipt.platform_ids,
                status=f"failed:{self.name}_unproven",
                target=target,
                body=body,
            )

        if receipt.acknowledged == 0:
            logger.error("Channel %s acknowledged nothing — nothing was seen", self.name)
        return receipt

    async def ask(self, question: str, options: Sequence[str]) -> str:
        raise NotImplementedError(f"channel {self.name!r} cannot ask a question")

    async def resolve_identity(self, native_id: str) -> Any:
        raise NotImplementedError(f"channel {self.name!r} resolves no identities")
