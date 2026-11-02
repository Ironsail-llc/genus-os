"""Channel streaming is the only code path: there is no switch to turn it off.

Telegram streams into its progress message, plugin channels that pass a
``stream`` callback get ``on_content``, and a transient stream failure inside a
channel-streaming scope retries in place, whatever the environment says.
"""

from __future__ import annotations

import inspect

from robothor.engine import channel_streaming, feature_flags
from robothor.settings.model import ChannelSettings

OLD_FLAG = "ROBOTHOR_CHANNEL_STREAMING"


def test_no_module_reads_a_channel_streaming_flag() -> None:
    assert not hasattr(channel_streaming, "channel_streaming_enabled")
    assert not [name for name in dir(feature_flags) if "channel_stream" in name.lower()]


def test_no_module_names_the_old_flag() -> None:
    for module in (channel_streaming, feature_flags):
        assert OLD_FLAG not in inspect.getsource(module), module.__name__


def test_settings_declare_no_channel_streaming_switch() -> None:
    assert "channel_streaming" not in ChannelSettings.model_fields
