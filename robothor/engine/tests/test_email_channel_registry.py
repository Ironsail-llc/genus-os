"""``email`` is a built-in name, registered configured or not.

An instance with no SMTP host and no ``gws`` binary must still *resolve*
``delivery.channel: email`` — to a channel that answers
``failed:email_no_transport``, which names the missing piece. Leaving the name
unresolvable would record ``failed:no_channel:email``, which reads as "this
platform has no email support at all" and sends the operator after a different
fix. That is the reason ``slack`` is a built-in on instances that never
configured it, and it applies here unchanged.
"""

from __future__ import annotations

import pytest

from robothor.engine.channels import BUILTIN_CHANNELS, get_channel, register_channel
from robothor.engine.channels import reset_channels as _reset
from robothor.engine.channels.email import EmailChannel


@pytest.fixture(autouse=True)
def _clean_registry():
    _reset()
    yield
    _reset()


def test_email_is_a_builtin_channel():
    assert "email" in BUILTIN_CHANNELS


def test_email_resolves_on_an_unconfigured_instance(monkeypatch, tmp_path):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("ROBOTHOR_EMAIL_FROM", "ROBOTHOR_EMAIL_SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)
    reset_settings()
    try:
        assert isinstance(get_channel("email"), EmailChannel)
    finally:
        reset_settings()


def test_a_plugin_claiming_email_is_refused():
    class _Impostor:
        name = "email"
        inbound_router = None

        async def send(self, target: str, text: str, **kw: object) -> None: ...

    with pytest.raises(ValueError, match="built-in"):
        register_channel("email", _Impostor())  # type: ignore[arg-type]
