"""The connect log line names the options presented, never their values.

The names come from a fixed vocabulary of connect options; an option outside
it is counted, not echoed, so nothing a caller passes can reach the log.
"""

from __future__ import annotations

import logging
import sys
import types
from unittest.mock import AsyncMock, MagicMock

from robothor.federation.nats import NATSManager


def _fake_nats(monkeypatch) -> None:
    module = types.ModuleType("nats")
    client = MagicMock()
    client.jetstream = MagicMock(return_value=MagicMock())
    module.connect = AsyncMock(return_value=client)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nats", module)


async def test_known_options_are_named_and_values_never_logged(monkeypatch, caplog) -> None:
    _fake_nats(monkeypatch)
    manager = NATSManager("nats://127.0.0.1:4222", user="engine", password="s3cret-pw")
    with caplog.at_level(logging.INFO, logger="robothor.federation.nats"):
        assert await manager.connect()
    line = next(r.getMessage() for r in caplog.records if "Connected to NATS" in r.getMessage())
    assert line == "Connected to NATS at nats://127.0.0.1:4222 (auth: password, user)"
    assert "s3cret" not in caplog.text and "engine" not in line.split("(auth")[1]


async def test_an_unknown_option_is_counted_not_echoed(monkeypatch, caplog) -> None:
    _fake_nats(monkeypatch)
    manager = NATSManager("nats://127.0.0.1:4222", token="t0k", sneaky_label_9=True)
    with caplog.at_level(logging.INFO, logger="robothor.federation.nats"):
        assert await manager.connect()
    line = next(r.getMessage() for r in caplog.records if "Connected to NATS" in r.getMessage())
    assert "sneaky_label_9" not in line
    assert line == "Connected to NATS at nats://127.0.0.1:4222 (auth: token, +1 other)"


async def test_no_options_says_none(monkeypatch, caplog) -> None:
    _fake_nats(monkeypatch)
    with caplog.at_level(logging.INFO, logger="robothor.federation.nats"):
        assert await NATSManager("nats://127.0.0.1:4222").connect()
    assert "(auth: none)" in caplog.text
