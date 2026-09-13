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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from robothor.engine.models import AgentConfig, AgentRun

logger = logging.getLogger(__name__)

__all__ = ["SenderChannel"]


class SenderChannel:
    """Adapts a ``register_platform_sender`` function to the channel protocol."""

    inbound_router: Any | None = None

    def __init__(self, name: str, send_func: Callable[..., Any] | None = None) -> None:
        self.name = name
        #: Kept only as a fallback. The registry dict is consulted first on
        #: every send so a re-registration — which is how the Telegram bot
        #: publishes its bound method at construction — is honoured rather than
        #: shadowed by whatever was current when this object was built.
        self._send_func = send_func

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

        # The agent's display name, plain. The Telegram wrapper's ``*name*``
        # header is Telegram markdown and would render as literal asterisks on
        # any other surface; per-surface formatting arrives with the first real
        # non-Telegram target.
        body = f"{config.name}\n\n{text}" if config is not None and config.name else text

        # One body handed over, so one acknowledgement is the minimum proof.
        # Deliberately NOT the Telegram chunk count: a surface with a different
        # length limit chunks differently, and borrowing Telegram's 4096 would
        # record `partial:1/3` for a send that completed in one message.
        expected = 1

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
        if receipt.acknowledged == 0:
            logger.error("Channel %s acknowledged nothing — nothing was seen", self.name)
        return receipt

    async def ask(self, question: str, options: Sequence[str]) -> str:
        raise NotImplementedError(f"channel {self.name!r} cannot ask a question")

    async def resolve_identity(self, native_id: str) -> Any:
        raise NotImplementedError(f"channel {self.name!r} resolves no identities")
