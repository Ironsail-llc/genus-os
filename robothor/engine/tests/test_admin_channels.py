"""Channel status and verify, read from outside the engine.

``genus channel list`` and ``genus channel verify`` answer two questions an
operator cannot answer any other way — *what could a manifest deliver to*, and
*does it actually work* — and both of them run inside the engine process,
because that is where a channel object and its credentials live. This module is
the same two questions over HTTP so the Helm can ask them.

What these tests hold down, in order of how badly each one has bitten before:

* **A health report is a diagnostic, not a credential dump.** ``health()`` is
  implemented per channel and a plugin implements its own; anything
  secret-shaped that comes back is fingerprinted rather than served.
* **One slow channel is not a broken page.** Every ``health()`` is time-boxed,
  and a timeout is a reported field rather than a 500 for the whole listing.
* **"Not configured" and "failed" stay different answers.** The CLI's
  ``UNCONFIGURED_STEP`` convention decides ``configured``; a channel with no
  ``verify`` gets ``steps: []``, never a fabricated pass.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robothor.engine.channels.base import UNCONFIGURED_STEP


class FakeChannel:
    """A channel that answers whatever the test needs it to answer."""

    def __init__(
        self,
        name: str,
        report: dict[str, Any] | None = None,
        *,
        steps: list[tuple[str, bool, str]] | None = None,
        health_delay: float = 0.0,
        health_raises: BaseException | None = None,
        verify_raises: BaseException | None = None,
    ) -> None:
        self.name = name
        self._report = report if report is not None else {"channel": name, "configured": True}
        self._steps = steps
        self._delay = health_delay
        self._health_raises = health_raises
        self._verify_raises = verify_raises
        self.verify_targets: list[str | None] = []
        if steps is None and verify_raises is None:
            # No ``verify`` attribute at all — the shape ``event_bus`` has.
            self.verify = None  # type: ignore[assignment]

    async def send(self, target: str, text: str, **kw: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("a status route must never send anything")

    async def health(self) -> dict[str, Any]:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._health_raises is not None:
            raise self._health_raises
        return dict(self._report)

    async def verify(self, target: str | None = None) -> list[tuple[str, bool, str]]:  # noqa: F811
        self.verify_targets.append(target)
        if self._verify_raises is not None:
            raise self._verify_raises
        return list(self._steps or [])


@pytest.fixture
def registry(monkeypatch):
    """Own the channel registry for the length of one test.

    Patched at the SOURCE module rather than where it is used: the routes
    import it lazily, so a patch on a name the route never reads through would
    leave the real registry — and the real Slack credential — in play.
    """
    from robothor.engine.channels import registry as registry_module

    channels: dict[str, Any] = {}
    monkeypatch.setattr(registry_module, "list_channels", lambda: dict(channels))
    monkeypatch.setattr(registry_module, "get_channel", lambda name: channels.get(name))
    return channels


# ── The listing ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_it_lists_every_registered_channel(registry):
    from robothor.engine.admin_channels import channel_status

    registry["telegram"] = FakeChannel("telegram")
    registry["event_bus"] = FakeChannel("event_bus", {"channel": "event_bus", "enabled": False})

    body = await channel_status()

    assert [entry["name"] for entry in body["channels"]] == ["event_bus", "telegram"]


@pytest.mark.asyncio
async def test_a_builtin_is_marked_as_one(registry):
    """An operator deciding whether a channel is removable needs to know which
    ones the platform owns and which arrived with a plugin."""
    from robothor.engine.admin_channels import channel_status

    registry["telegram"] = FakeChannel("telegram")
    registry["carrier_pigeon"] = FakeChannel("carrier_pigeon")

    by_name = {entry["name"]: entry for entry in (await channel_status())["channels"]}

    assert by_name["telegram"]["builtin"] is True
    assert by_name["carrier_pigeon"]["builtin"] is False


@pytest.mark.asyncio
async def test_configured_is_read_off_the_health_report(registry):
    from robothor.engine.admin_channels import channel_status

    registry["slack"] = FakeChannel("slack", {"channel": "slack", "configured": False, "ok": False})

    entry = (await channel_status())["channels"][0]

    assert entry["configured"] is False
    assert entry["health"]["ok"] is False


@pytest.mark.asyncio
async def test_a_report_that_says_enabled_instead_still_answers_configured(registry):
    """``event_bus`` says ``enabled`` and ``telegram`` says ``sender_registered``.
    Neither spells ``configured``, and a listing that reported ``null`` for both
    would leave the two channels most instances have looking unknown."""
    from robothor.engine.admin_channels import channel_status

    registry["event_bus"] = FakeChannel("event_bus", {"channel": "event_bus", "enabled": True})
    registry["telegram"] = FakeChannel(
        "telegram", {"channel": "telegram", "sender_registered": False}
    )

    by_name = {entry["name"]: entry for entry in (await channel_status())["channels"]}

    assert by_name["event_bus"]["configured"] is True
    assert by_name["telegram"]["configured"] is False


@pytest.mark.asyncio
async def test_a_report_that_answers_nothing_is_unknown_not_configured(registry):
    """Fail closed on the OPTIMISTIC side of nothing: a plugin whose health says
    nothing about configuration must not be reported as ready."""
    from robothor.engine.admin_channels import channel_status

    registry["carrier_pigeon"] = FakeChannel("carrier_pigeon", {"channel": "carrier_pigeon"})

    assert (await channel_status())["channels"][0]["configured"] is None


@pytest.mark.asyncio
async def test_a_channel_that_can_verify_says_so(registry):
    from robothor.engine.admin_channels import channel_status

    registry["slack"] = FakeChannel("slack", steps=[("auth", True, "ok")])
    registry["event_bus"] = FakeChannel("event_bus")

    by_name = {entry["name"]: entry for entry in (await channel_status())["channels"]}

    assert by_name["slack"]["verify_available"] is True
    assert by_name["event_bus"]["verify_available"] is False


# ── One channel must not take the page down ──────────────────────────────


@pytest.mark.asyncio
async def test_a_slow_health_is_a_reported_field_not_a_hung_page(registry, monkeypatch):
    """The whole reason the listing is time-boxed: ``SlackChannel.health`` makes
    an ``auth.test`` round trip, and a provider that is up but not answering
    would otherwise hold the operator's Settings page open indefinitely."""
    import robothor.engine.admin_channels as admin_channels

    monkeypatch.setattr(admin_channels, "HEALTH_TIMEOUT_SECONDS", 0.05)
    registry["slack"] = FakeChannel("slack", health_delay=5.0)
    registry["telegram"] = FakeChannel("telegram", {"channel": "telegram", "configured": True})

    by_name = {
        entry["name"]: entry for entry in (await admin_channels.channel_status())["channels"]
    }

    assert by_name["slack"]["health"]["timed_out"] is True
    assert by_name["slack"]["configured"] is None
    assert by_name["telegram"]["configured"] is True, "a slow sibling must not spoil a good report"


@pytest.mark.asyncio
async def test_a_health_that_raises_is_reported_not_propagated(registry):
    from robothor.engine.admin_channels import channel_status

    registry["slack"] = FakeChannel("slack", health_raises=RuntimeError("boom"))

    entry = (await channel_status())["channels"][0]

    assert entry["health"]["error"].startswith("RuntimeError")
    assert entry["configured"] is None


@pytest.mark.asyncio
async def test_the_error_text_of_a_raising_health_is_redacted(registry):
    """``slack_sdk`` raises ``Invalid header value b'Bearer xoxb-…'`` when a
    token survives the vault with a trailing newline. That exception text
    reached the journal once already; it must not reach a browser."""
    from robothor.engine.admin_channels import channel_status

    registry["slack"] = FakeChannel(
        "slack", health_raises=ValueError("Invalid header value b'Bearer xoxb-000-111-aaa'")
    )

    entry = (await channel_status())["channels"][0]

    assert "xoxb-000-111-aaa" not in entry["health"]["error"]


# ── Nothing secret-shaped travels ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_secret_named_field_is_fingerprinted(registry):
    """``health()`` is implemented per channel and a plugin implements its own,
    so the redaction cannot rely on every author having been careful."""
    from robothor.engine.admin_channels import channel_status

    registry["carrier_pigeon"] = FakeChannel(
        "carrier_pigeon",
        {
            "channel": "carrier_pigeon",
            "configured": True,
            "bot_token": "xoxb-not-a-real-token-0000",
            "chat_id": "-1001234567890",
            "nested": {"api_key": "sk-test-abcdefghijklmnop"},
        },
    )

    health = (await channel_status())["channels"][0]["health"]

    assert "xoxb-not-a-real-token-0000" not in str(health)
    assert "-1001234567890" not in str(health)
    assert "sk-test-abcdefghijklmnop" not in str(health)
    assert health["bot_token"].startswith("sha256:")
    assert health["nested"]["api_key"].startswith("sha256:")


@pytest.mark.asyncio
async def test_a_credential_shaped_value_under_an_innocent_name_is_redacted(registry):
    """The key name is the weaker half of the guard: a plugin that reports its
    credential under ``detail`` gets caught by the shape instead."""
    from robothor.engine.admin_channels import channel_status

    registry["carrier_pigeon"] = FakeChannel(
        "carrier_pigeon",
        {"channel": "carrier_pigeon", "configured": True, "detail": "using xoxb-0000-1111-zzzz"},
    )

    health = (await channel_status())["channels"][0]["health"]

    assert "xoxb-0000-1111-zzzz" not in health["detail"]


@pytest.mark.asyncio
async def test_a_fingerprint_identifies_without_carrying(registry):
    """Two different credentials must fingerprint differently, or the field is
    decoration; the same one twice must match, or it cannot be compared."""
    from robothor.engine.admin_channels import _fingerprint

    assert _fingerprint("one") == _fingerprint("one")
    assert _fingerprint("one") != _fingerprint("two")
    assert "one" not in _fingerprint("one")


# ── Verify ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_returns_each_step(registry):
    from robothor.engine.admin_channels import verify_channel

    registry["slack"] = FakeChannel(
        "slack", steps=[("auth", True, "team acme"), ("post", False, "not_in_channel")]
    )

    body = await verify_channel("slack", target="C0PLACEHOLDER")

    assert body["channel"] == "slack"
    assert body["configured"] is True
    assert body["steps"] == [
        {"step": "auth", "ok": True, "detail": "team acme"},
        {"step": "post", "ok": False, "detail": "not_in_channel"},
    ]
    assert registry["slack"].verify_targets == ["C0PLACEHOLDER"]


@pytest.mark.asyncio
async def test_verify_reports_unconfigured_rather_than_failed(registry):
    """The CLI's exit code 2: an instance that never wanted Slack has not failed
    a check it did not ask for. The step survives so the operator can read the
    reason, but ``configured`` is what the UI colours on."""
    from robothor.engine.admin_channels import verify_channel

    registry["slack"] = FakeChannel(
        "slack", steps=[(UNCONFIGURED_STEP, False, "no bot token on this instance")]
    )

    body = await verify_channel("slack")

    assert body["configured"] is False
    assert body["steps"][0]["step"] == UNCONFIGURED_STEP


@pytest.mark.asyncio
async def test_a_channel_with_no_verify_gets_no_steps_and_never_a_pass(registry):
    from robothor.engine.admin_channels import verify_channel

    registry["event_bus"] = FakeChannel("event_bus", {"channel": "event_bus", "enabled": True})

    body = await verify_channel("event_bus")

    assert body["verify_available"] is False
    assert body["steps"] == []
    assert body["configured"] is True, "configured still comes from health()"


@pytest.mark.asyncio
async def test_an_unknown_channel_is_a_404(registry):
    from fastapi import HTTPException

    from robothor.engine.admin_channels import verify_channel

    with pytest.raises(HTTPException) as caught:
        await verify_channel("not-a-channel")

    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_a_verify_that_raises_is_a_failed_step_not_a_500(registry):
    from robothor.engine.admin_channels import verify_channel

    registry["slack"] = FakeChannel("slack", verify_raises=RuntimeError("socket closed"))

    body = await verify_channel("slack")

    assert body["steps"][0]["ok"] is False
    assert "RuntimeError" in body["steps"][0]["detail"]


@pytest.mark.asyncio
async def test_a_verify_detail_is_redacted_the_same_way(registry):
    """Slack's own verify quotes upstream errors, and those echo the token back
    on a 401. The listing and this route share one redactor for that reason."""
    from robothor.engine.admin_channels import verify_channel

    registry["slack"] = FakeChannel(
        "slack", steps=[("auth", False, "invalid_auth for xoxb-0000-1111-yyyy")]
    )

    body = await verify_channel("slack")

    assert "xoxb-0000-1111-yyyy" not in body["steps"][0]["detail"]


# ── Mounting ─────────────────────────────────────────────────────────────


def test_the_routes_are_mounted_under_the_admin_prefix():
    """``engine/auth.py`` gates ``engine:control`` by PREFIX, so a route mounted
    anywhere else would be reachable with a read-scoped dashboard token."""
    from fastapi import FastAPI

    from robothor.engine.admin_channels import register

    app = FastAPI()
    register(app)

    paths = {
        (method, getattr(candidate, "path", ""))
        for outer in app.routes
        for candidate in getattr(getattr(outer, "original_router", None), "routes", (outer,))
        for method in getattr(candidate, "methods", ())
    }

    assert ("GET", "/api/admin/channels") in paths
    assert ("POST", "/api/admin/channels/{name}/verify") in paths


def test_the_engine_app_registers_them():
    """A module nothing calls is the inert control this codebase keeps shipping."""
    import inspect

    from robothor.engine import health

    assert "admin_channels" in inspect.getsource(health)
