"""ROBOTHOR_AMBIENT: off by default, and every unrecognised value is off."""

from __future__ import annotations

import pytest

from robothor.engine.feature_flags import ambient_mode


@pytest.mark.parametrize(
    ("raw", "mode"),
    [
        (None, "off"),
        ("", "off"),
        ("off", "off"),
        ("observe", "observe"),
        ("OBSERVE", "observe"),
        ("enforce", "enforce"),
        ("alert", "off"),
        ("yes", "off"),
    ],
)
def test_mode(monkeypatch, raw, mode):
    if raw is None:
        monkeypatch.delenv("ROBOTHOR_AMBIENT", raising=False)
    else:
        monkeypatch.setenv("ROBOTHOR_AMBIENT", raw)
    assert ambient_mode() == mode


def test_the_panic_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_AMBIENT", "enforce")
    monkeypatch.setenv("ROBOTHOR_DISABLE_ALL_RIPS", "1")
    assert ambient_mode() == "off"
