"""Channels — the surfaces an instance reaches people over, resolved by name.

    from robothor.engine.channels import get_channel

    channel = get_channel(config.delivery_channel or "telegram")
    receipt = await channel.send(config.delivery_to, body, config=config, run=run)

:mod:`~robothor.engine.channels.base` holds the contract and the receipt rule,
:mod:`~robothor.engine.channels.registry` decides what a name resolves to, and
one module per surface wraps an existing transport without replacing it.

Distinct from ``robothor/engine/channel_bus.py``, which is the POST_DELIVERY
consumer that mirrors outbound messages into the main agent's session. That
stays exactly where it is: ``deliver()`` fires it once, after the receipt, so
there is still a single instrumentation point no matter which channel sent.
"""

from __future__ import annotations

from robothor.engine.channels.base import (
    Channel,
    SendReceipt,
    acknowledged_messages,
    receipt_from,
)
from robothor.engine.channels.registry import (
    BUILTIN_CHANNELS,
    CHANNELS_ENV,
    enabled_plugin_channels,
    get_channel,
    list_channels,
    register_channel,
    reset_channels,
)

__all__ = [
    "BUILTIN_CHANNELS",
    "CHANNELS_ENV",
    "Channel",
    "SendReceipt",
    "acknowledged_messages",
    "enabled_plugin_channels",
    "get_channel",
    "list_channels",
    "receipt_from",
    "register_channel",
    "reset_channels",
]
