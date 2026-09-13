"""Redis, models, channels and services -- the categories that reach outward.

Grouped in one module because they share one shape: a probe against a fake HTTP
client or a fake client library, a pass, a fail, and a skip that names its
reason. The properties worth pinning are the ones that were wrong before this
package existed: an absent optional dependency is a SKIP and not a failure, a
401 from an auth-gated endpoint is UP, and nothing prints a credential.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.doctor.checks import channels as channel_checks
from robothor.doctor.checks import models as model_checks
from robothor.doctor.checks import redis as redis_checks
from robothor.doctor.checks import services as service_checks
from robothor.doctor.context import HttpResponse
from robothor.doctor.tests.conftest import fake_http, make_ctx

FAKE_TOKEN = "1234567:AAfake-telegram-token-value"
FAKE_API_KEY = "sk-test-do-not-print-9999999999"


def _run(checks, check_id: str, ctx):
    check = next(item for item in checks if item.id == check_id)
    answer = asyncio.run(check.run(ctx))
    return answer if isinstance(answer, list) else [answer]


@pytest.fixture
def settings(monkeypatch):
    """Re-resolve settings after a test sets a variable."""

    def _set(**pairs: str):
        from robothor.settings import reset_settings

        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
        reset_settings()

    return _set


# ── redis ────────────────────────────────────────────────────────────────────


REDIS_PASSWORD = "hunter2-redis"


def _fake_redis(monkeypatch, *, ping_error: Exception | None = None) -> None:
    class FakeRedis:
        def __init__(self, **_kwargs):
            pass

        def ping(self):
            if ping_error is not None:
                raise ping_error
            return True

        def close(self):
            pass

    import sys
    import types

    module = types.ModuleType("redis")
    module.Redis = FakeRedis  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", module)


def test_redis_passes_when_ping_answers(monkeypatch) -> None:
    _fake_redis(monkeypatch)
    rows = _run(redis_checks.CHECKS, "redis.connect", make_ctx())
    assert rows[0].status == "pass"
    assert "6379" in rows[0].detail


def test_the_redis_pass_line_does_not_print_the_password(monkeypatch, settings) -> None:
    """The failure path was guarded and the PASS path was not, so appending
    the password to the healthy line shipped green under mutation. A passing
    detail is printed far more often than a failing one."""
    settings(ROBOTHOR_REDIS_PASSWORD=REDIS_PASSWORD)
    _fake_redis(monkeypatch)
    rows = _run(redis_checks.CHECKS, "redis.connect", make_ctx())
    assert rows[0].status == "pass"
    assert REDIS_PASSWORD not in rows[0].detail


def test_redis_failure_does_not_print_the_password(monkeypatch, settings) -> None:
    settings(ROBOTHOR_REDIS_PASSWORD=REDIS_PASSWORD)
    _fake_redis(
        monkeypatch,
        ping_error=ConnectionError(f"auth failed for redis://:{REDIS_PASSWORD}@127.0.0.1:6379"),
    )
    rows = _run(redis_checks.CHECKS, "redis.connect", make_ctx())
    assert rows[0].status == "fail"
    assert REDIS_PASSWORD not in rows[0].detail
    assert "ConnectionError" in rows[0].detail


# ── models ───────────────────────────────────────────────────────────────────


def test_no_provider_credential_is_visible_to_a_test_in_this_tree() -> None:
    """The property every models test relies on, asserted once.

    `_completion` consults the real credential pool, so a test that forgets to
    fake the pool silently reads whatever THIS machine exports: green here, red
    on CI, and unreproducible on a clean checkout. The sibling fixtures in
    `tests/` strip `ROBOTHOR_*`/`GENUS_*` only, because provider keys are not
    platform-prefixed -- so this tree's `_isolated_settings` strips them too,
    and this is what fails if it stops.
    """
    import os

    from robothor.engine import key_pool

    visible = [
        name
        for name in os.environ
        if name.endswith(("_API_KEY", "_API_TOKEN")) or key_pool.provider_for_var(name) is not None
    ]
    assert visible == [], f"a provider credential reached a test from the host: {visible}"


def _fake_slots(monkeypatch, slots_by_provider: dict[str, list]) -> None:
    """Fake `key_pool.provider_slots` and let the REAL `_configured` run.

    Stubbing `_configured` itself left the only line that formats credential
    state unexecuted by the suite, so changing it to emit key material shipped
    green under mutation. The seam has to be one level lower than the code
    whose output is the invariant.
    """
    from robothor.engine import key_pool

    monkeypatch.setattr(
        key_pool, "provider_slots", lambda provider_id: slots_by_provider.get(provider_id, [])
    )


def test_provider_keys_passes_and_reports_only_fingerprints(monkeypatch) -> None:
    from robothor.engine.key_pool import SlotStatus, key_fingerprint

    slot = SlotStatus(
        position=1, source="vault", fingerprint=key_fingerprint(FAKE_API_KEY), state="active"
    )
    _fake_slots(monkeypatch, {"openrouter": [slot]})

    rows = _run(model_checks.CHECKS, "provider.keys", make_ctx())
    assert rows[0].status == "pass"
    assert slot.fingerprint in rows[0].detail
    assert "OpenRouter" in rows[0].detail
    assert "vault" in rows[0].detail


def test_the_provider_keys_pass_line_never_carries_key_material(monkeypatch) -> None:
    """The line that lists credentials is the one most likely to grow a value,
    and it is printed on a healthy instance, into a terminal, a journal and
    `GET /api/doctor`'s JSON.

    The credential is put where a leak would find it -- the provider's real
    environment variable -- so that reaching for the value rather than the
    fingerprint fails here instead of shipping.
    """
    from robothor.engine.key_pool import SlotStatus, key_fingerprint

    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_API_KEY)
    slot = SlotStatus(
        position=1, source="env", fingerprint=key_fingerprint(FAKE_API_KEY), state="active"
    )
    _fake_slots(monkeypatch, {"openrouter": [slot], "openai": [slot]})

    rows = _run(model_checks.CHECKS, "provider.keys", make_ctx())
    assert rows[0].status == "pass"
    assert FAKE_API_KEY not in rows[0].detail
    assert rows[0].detail.count("sha256:") == 2


def test_provider_keys_fails_with_nothing_configured(monkeypatch) -> None:
    _fake_slots(monkeypatch, {})
    rows = _run(model_checks.CHECKS, "provider.keys", make_ctx())
    assert rows[0].status == "fail"
    assert "genus config set" in rows[0].detail


def test_completion_is_skipped_offline() -> None:
    rows = _run(model_checks.CHECKS, "provider.completion", make_ctx(offline=True))
    assert rows[0].status == "skip"
    assert "--offline" in rows[0].detail


def test_completion_is_skipped_when_there_is_no_model_to_call(monkeypatch) -> None:
    _one_credential(monkeypatch)
    monkeypatch.setattr(model_checks, "_fleet_model", lambda: "")
    rows = _run(model_checks.CHECKS, "provider.completion", make_ctx())
    assert rows[0].status == "skip"


def _one_credential(monkeypatch) -> None:
    """A resolvable credential, so `_completion` gets past its M3 skip.

    Stubbing `_fleet_model` alone left these tests reading the HOST's ambient
    OPENROUTER_API_KEY: green on this box, red on CI, and unreproducible on a
    clean machine -- which is the exact failure `_isolated_settings` exists to
    prevent.
    """
    from robothor.engine.key_pool import SlotStatus, key_fingerprint

    slot = SlotStatus(
        position=1, source="env", fingerprint=key_fingerprint(FAKE_API_KEY), state="active"
    )
    _fake_slots(monkeypatch, {"openrouter": [slot]})


def test_completion_passes_on_a_real_answer(monkeypatch) -> None:
    _one_credential(monkeypatch)
    monkeypatch.setattr(model_checks, "_fleet_model", lambda: "openrouter/test/model")

    seen: dict = {}

    async def _call(messages, **kwargs):
        seen.update(kwargs)
        seen["messages"] = messages
        return object()

    monkeypatch.setattr("robothor.engine.llm_client.llm_call", _call)
    rows = _run(model_checks.CHECKS, "provider.completion", make_ctx())

    assert rows[0].status == "pass"
    assert seen["max_tokens"] == 1, "the probe must stay a one-token call"
    assert seen["model"] == "openrouter/test/model"


def test_completion_failure_reports_an_error_class_not_the_exception_text(monkeypatch) -> None:
    _one_credential(monkeypatch)
    monkeypatch.setattr(model_checks, "_fleet_model", lambda: "openrouter/test/model")

    async def _call(_messages, **_kwargs):
        raise RuntimeError("AuthenticationError: key sk-live-abcdef is invalid")

    monkeypatch.setattr("robothor.engine.llm_client.llm_call", _call)
    rows = _run(model_checks.CHECKS, "provider.completion", make_ctx())

    assert rows[0].status == "fail"
    assert "sk-live-abcdef" not in rows[0].detail


def test_completion_skips_rather_than_failing_twice_with_no_credential(monkeypatch) -> None:
    """`provider.keys` already reports "no provider credential is configured"
    as a required failure. Failing here as well tells the operator the same
    thing twice and hides which one is the cause."""
    _fake_slots(monkeypatch, {})

    def _never():
        raise AssertionError("the fleet model must not be resolved with no credential")

    monkeypatch.setattr(model_checks, "_fleet_model", _never)

    rows = _run(model_checks.CHECKS, "provider.completion", make_ctx())
    assert rows[0].status == "skip"
    assert "no provider credential" in rows[0].detail


def test_completion_makes_no_call_when_no_credential_resolves(monkeypatch) -> None:
    calls: list[str] = []

    async def _call(_messages, **kwargs):
        calls.append(str(kwargs.get("model")))
        return object()

    monkeypatch.setattr("robothor.engine.llm_client.llm_call", _call)
    _fake_slots(monkeypatch, {})

    _run(model_checks.CHECKS, "provider.completion", make_ctx())
    assert calls == []


def _no_ollama_in_the_fleet(monkeypatch) -> None:
    """A cloud-only fleet: no model in the chain routes through Ollama."""
    monkeypatch.setattr(model_checks, "_fleet_models", lambda: ["openrouter/test/model"])


def _ollama_in_the_fleet(monkeypatch) -> None:
    monkeypatch.setattr(
        model_checks, "_fleet_models", lambda: ["openrouter/test/model", "ollama_chat/qwen3:8b"]
    )


def test_ollama_skips_on_a_cloud_only_instance(monkeypatch) -> None:
    """Every Ollama setting carries a default, so a cloud-only instance that
    never configured one would otherwise carry a permanent recommended failure
    for a service it does not use -- and a red line an operator learns to
    ignore is worse than no line."""
    _no_ollama_in_the_fleet(monkeypatch)
    ctx = make_ctx(http_fetch=fake_http({}))
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "skip"
    assert "no model in the fleet" in rows[0].detail
    assert ctx.http_fetch.calls == []  # type: ignore[union-attr]


def test_a_cloud_only_instance_does_not_dial_a_live_ollama_either(monkeypatch) -> None:
    """Whether an unused server happens to answer is not information an
    operator needs, and the check decides "is it used" BEFORE dialling so a
    cloud-only instance spends nothing. The dependency this used to protect --
    defaults untouched but the fleet using Ollama anyway -- is now caught by the
    fleet signal instead of by probing everything."""
    _no_ollama_in_the_fleet(monkeypatch)
    ctx = make_ctx(
        http_fetch=fake_http({"http://127.0.0.1:11434/api/tags": HttpResponse(status=200)})
    )
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "skip"
    assert ctx.http_fetch.calls == []  # type: ignore[union-attr]


def test_an_explicitly_configured_ollama_that_is_down_is_a_failure(monkeypatch, settings) -> None:
    """The operator said it should be there. Silence is then a real finding.

    Asked of `settings.provenance`, which knows the value came from the
    environment -- comparing against the declared default cannot tell a
    deliberate `ROBOTHOR_OLLAMA_HOST=127.0.0.1` from no setting at all.
    """
    _no_ollama_in_the_fleet(monkeypatch)
    settings(ROBOTHOR_OLLAMA_URL="http://ollama.example.test:11434")
    ctx = make_ctx(http_fetch=fake_http({}))
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "fail"


def test_an_endpoint_set_to_its_own_default_value_still_counts_as_configured(
    monkeypatch, settings
) -> None:
    """The structural test could not see this: typing the default value out is
    still a statement that Ollama should be there."""
    _no_ollama_in_the_fleet(monkeypatch)
    settings(ROBOTHOR_OLLAMA_HOST="127.0.0.1")
    ctx = make_ctx(http_fetch=fake_http({}))
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "fail"
    assert "not configured" not in rows[0].detail


def test_a_dead_ollama_an_instance_actually_uses_is_a_failure_not_a_skip(monkeypatch) -> None:
    """The commonest install: every Ollama setting on its default AND the fleet
    routing a tier through it. A skip there leaves `recommended_failed` at 0 and
    the Helm banner green during an embedding outage -- and calls a completed,
    failed probe "could not run", which is not what the word means here."""
    _ollama_in_the_fleet(monkeypatch)
    ctx = make_ctx(http_fetch=fake_http({}))
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)

    assert rows[0].status == "fail"
    assert "not configured" not in rows[0].detail
    assert ctx.http_fetch.calls  # type: ignore[union-attr]


def test_a_skip_never_describes_a_probe_that_ran(monkeypatch) -> None:
    """`model.py` defines skip as "a check that could not run". A failed probe
    ran."""
    _no_ollama_in_the_fleet(monkeypatch)
    ctx = make_ctx(http_fetch=fake_http({}))
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "skip"
    assert "did not answer" not in rows[0].detail


def test_ollama_passes_when_tags_answers(monkeypatch) -> None:
    """An instance that uses Ollama, with a server that answers."""
    _ollama_in_the_fleet(monkeypatch)
    ctx = make_ctx(
        http_fetch=fake_http({"http://127.0.0.1:11434/api/tags": HttpResponse(status=200)})
    )
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "pass"


def test_ollama_failure_is_recommended_not_required(monkeypatch, settings) -> None:
    check = next(item for item in model_checks.CHECKS if item.id == "ollama.reachable")
    assert check.severity == "recommended"
    _no_ollama_in_the_fleet(monkeypatch)
    settings(ROBOTHOR_OLLAMA_URL="http://ollama.example.test:11434")
    ctx = make_ctx(http_fetch=fake_http({}))
    assert _run(model_checks.CHECKS, "ollama.reachable", ctx)[0].status == "fail"


# ── channels ─────────────────────────────────────────────────────────────────


def test_telegram_absent_is_a_skip_not_a_failure() -> None:
    """A Telegram-free instance is the documented headless deploy."""
    rows = _run(channel_checks.CHECKS, "telegram.token", make_ctx())
    assert [row.status for row in rows] == ["skip"]
    assert "delivery=none" in rows[0].detail


def test_a_malformed_token_fails_without_printing_it(settings) -> None:
    settings(ROBOTHOR_TELEGRAM_BOT_TOKEN="not-a-token", ROBOTHOR_TELEGRAM_CHAT_ID="12345")
    rows = _run(channel_checks.CHECKS, "telegram.token", make_ctx(offline=True))
    joined = " ".join(row.detail for row in rows)
    assert any(row.status == "fail" for row in rows)
    assert "not-a-token" not in joined


def test_a_token_with_no_chat_id_fails(settings) -> None:
    settings(ROBOTHOR_TELEGRAM_BOT_TOKEN=FAKE_TOKEN)
    rows = _run(channel_checks.CHECKS, "telegram.token", make_ctx(offline=True))
    assert any(row.status == "fail" and row.sub_id == "chat" for row in rows)


def test_a_live_token_is_proved_by_get_me(settings) -> None:
    settings(ROBOTHOR_TELEGRAM_BOT_TOKEN=FAKE_TOKEN, ROBOTHOR_TELEGRAM_CHAT_ID="12345")
    fetch = fake_http({f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe": HttpResponse(status=200)})
    rows = _run(channel_checks.CHECKS, "telegram.token", make_ctx(http_fetch=fetch))
    assert [row.status for row in rows] == ["pass", "pass"]
    assert "1234567" in rows[0].detail


def test_the_telegram_pass_line_never_carries_the_token(settings) -> None:
    """`"1234567" in detail` is satisfied by the FULL token too, so asserting
    the public bot id is present does not prove the secret half is absent --
    and only the 401 branch checked. A regression here puts the live bot token
    in the CLI table, the journal and the bridge's JSON."""
    settings(ROBOTHOR_TELEGRAM_BOT_TOKEN=FAKE_TOKEN, ROBOTHOR_TELEGRAM_CHAT_ID="12345")
    fetch = fake_http({f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe": HttpResponse(status=200)})
    rows = _run(channel_checks.CHECKS, "telegram.token", make_ctx(http_fetch=fetch))

    assert rows[0].status == "pass"
    joined = " ".join(row.detail for row in rows)
    assert FAKE_TOKEN not in joined
    assert FAKE_TOKEN.partition(":")[2] not in joined


def test_the_token_and_the_chat_id_never_share_a_row_id(settings) -> None:
    """Two findings about two different settings. A dashboard keying on
    `telegram.token` would otherwise show one of them at random."""
    settings(ROBOTHOR_TELEGRAM_BOT_TOKEN=FAKE_TOKEN, ROBOTHOR_TELEGRAM_CHAT_ID="12345")
    fetch = fake_http({f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe": HttpResponse(status=200)})
    rows = _run(channel_checks.CHECKS, "telegram.token", make_ctx(http_fetch=fetch))
    assert [row.sub_id for row in rows] == ["token", "chat"]


def test_a_revoked_token_is_a_failure(settings) -> None:
    settings(ROBOTHOR_TELEGRAM_BOT_TOKEN=FAKE_TOKEN, ROBOTHOR_TELEGRAM_CHAT_ID="12345")
    fetch = fake_http({f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe": HttpResponse(status=401)})
    rows = _run(channel_checks.CHECKS, "telegram.token", make_ctx(http_fetch=fetch))
    assert rows[0].status == "fail"
    assert FAKE_TOKEN not in rows[0].detail


def test_an_unreachable_telegram_is_a_skip_not_a_verdict_on_the_token(settings) -> None:
    """A box with no egress has not proved the token wrong."""
    settings(ROBOTHOR_TELEGRAM_BOT_TOKEN=FAKE_TOKEN, ROBOTHOR_TELEGRAM_CHAT_ID="12345")
    rows = _run(channel_checks.CHECKS, "telegram.token", make_ctx(http_fetch=fake_http({})))
    assert rows[0].status == "skip"


def test_offline_does_not_call_telegram(settings) -> None:
    settings(ROBOTHOR_TELEGRAM_BOT_TOKEN=FAKE_TOKEN, ROBOTHOR_TELEGRAM_CHAT_ID="12345")
    fetch = fake_http({})
    _run(channel_checks.CHECKS, "telegram.token", make_ctx(offline=True, http_fetch=fetch))
    assert fetch.calls == []


FAKE_SLACK_TOKEN = "xoxb-test-not-a-real-token"


def test_slack_absent_is_a_skip() -> None:
    assert _run(channel_checks.CHECKS, "slack.token", make_ctx())[0].status == "skip"


def test_an_app_token_where_a_bot_token_belongs_is_reported(settings) -> None:
    settings(ROBOTHOR_SLACK_BOT_TOKEN="xapp-1-not-a-bot-token")
    row = _run(channel_checks.CHECKS, "slack.token", make_ctx())[0]
    assert row.status == "fail"
    assert "xapp-1-not-a-bot-token" not in row.detail


def test_slack_is_informational() -> None:
    check = next(item for item in channel_checks.CHECKS if item.id == "slack.token")
    assert check.severity == "info"


def test_every_slack_setting_gets_its_own_row(settings) -> None:
    """Two findings about two settings must not share a row id: a dashboard
    filtering on `slack.token` would see one of them at random."""
    settings(
        ROBOTHOR_SLACK_BOT_TOKEN=FAKE_SLACK_TOKEN,
        ROBOTHOR_SLACK_APP_TOKEN="xapp-test-not-a-real-token",
        ROBOTHOR_SLACK_VERIFY_TARGET="C0000000000",
    )
    rows = _run(channel_checks.CHECKS, "slack.token", make_ctx())
    assert [row.sub_id for row in rows] == ["token", "app_token", "target"]
    assert all(row.status == "pass" for row in rows)

    details = " ".join(row.detail for row in rows)
    assert FAKE_SLACK_TOKEN not in details
    # This whole list rides into `GET /api/doctor` on every healthy refresh,
    # which the bridge serves over HTTP and caches. The operator's channel id is
    # their own data; a FAILURE names the bad value because that is the finding,
    # a pass has nothing to add by naming it.
    assert "C0000000000" not in details
    # The layer that answered IS worth printing: it is the question an operator
    # has when `genus channel list` and `genus doctor` disagree.
    assert "env" in details


def test_a_bot_token_in_the_app_token_slot_is_reported(settings) -> None:
    """The mirror of the case above, and just as invisible: outbound delivery
    works perfectly while the inbound bot never starts."""
    settings(
        ROBOTHOR_SLACK_BOT_TOKEN=FAKE_SLACK_TOKEN,
        ROBOTHOR_SLACK_APP_TOKEN=FAKE_SLACK_TOKEN,
    )
    rows = {row.sub_id: row for row in _run(channel_checks.CHECKS, "slack.token", make_ctx())}
    assert rows["token"].status == "pass"
    assert rows["app_token"].status == "fail"
    assert FAKE_SLACK_TOKEN not in rows["app_token"].detail


def test_a_channel_name_as_the_verify_target_is_reported(settings) -> None:
    """`#general` is a name; the channel refuses it rather than walking
    conversations.list on every send."""
    settings(ROBOTHOR_SLACK_BOT_TOKEN=FAKE_SLACK_TOKEN, ROBOTHOR_SLACK_VERIFY_TARGET="#general")
    rows = {row.sub_id: row for row in _run(channel_checks.CHECKS, "slack.token", make_ctx())}
    assert rows["target"].status == "fail"


def test_slack_verify_skips_when_unconfigured() -> None:
    assert _run(channel_checks.CHECKS, "slack.verify", make_ctx())[0].status == "skip"


def test_slack_verify_skips_under_offline(settings, monkeypatch) -> None:
    """--offline means nothing leaves the box. A zero call count, not merely a
    skipped status: a check could report `skip` after doing the work."""
    calls: list[str] = []

    def _never(_token: str):
        calls.append(_token)
        raise AssertionError("the doctor called Slack under --offline")

    from robothor.engine.channels import slack as slack_channel

    monkeypatch.setattr(slack_channel, "_build_client", _never)
    settings(ROBOTHOR_SLACK_BOT_TOKEN=FAKE_SLACK_TOKEN)
    assert _run(channel_checks.CHECKS, "slack.verify", make_ctx(offline=True))[0].status == "skip"
    assert calls == []


def test_slack_verify_fails_on_bad_auth(settings, monkeypatch) -> None:
    class _Rejects:
        async def auth_test(self):
            raise RuntimeError("invalid_auth")

    from robothor.engine.channels import slack as slack_channel

    monkeypatch.setattr(slack_channel, "_build_client", lambda _token: _Rejects())
    settings(ROBOTHOR_SLACK_BOT_TOKEN=FAKE_SLACK_TOKEN)
    row = _run(channel_checks.CHECKS, "slack.verify", make_ctx())[0]
    assert row.status == "fail"
    assert FAKE_SLACK_TOKEN not in row.detail


class _Answers:
    """A Slack that accepts the token and holds the scopes."""

    def __init__(self, *, scope_error: Exception | None = None) -> None:
        self.scope_error = scope_error
        self.list_calls = 0

    async def auth_test(self):
        return {"ok": True, "team": "Example Workspace", "user_id": "U0000000001"}

    async def conversations_list(self, **_):
        self.list_calls += 1
        if self.scope_error is not None:
            raise self.scope_error
        return {"ok": True, "channels": []}


def test_slack_verify_passes_on_a_live_token(settings, monkeypatch) -> None:
    from robothor.engine.channels import slack as slack_channel

    client = _Answers()
    monkeypatch.setattr(slack_channel, "_build_client", lambda _token: client)
    settings(ROBOTHOR_SLACK_BOT_TOKEN=FAKE_SLACK_TOKEN)
    row = _run(channel_checks.CHECKS, "slack.verify", make_ctx())[0]

    assert row.status == "pass"
    assert client.list_calls == 1, "the scope probe did not run"
    # The workspace name is instance data and this row is served over HTTP.
    assert "Example Workspace" not in row.detail


def test_slack_verify_fails_on_a_missing_scope(settings, monkeypatch) -> None:
    """The brief required `auth.test` + the scope probe, and a first cut ran
    only the first. `missing_scope` is invisible until a send, so a verify that
    skips it shows green -- here and on the bridge Health panel -- for an app
    that cannot post a thing."""
    from robothor.engine.channels import slack as slack_channel

    class _MissingScopeError(Exception):
        def __init__(self) -> None:
            super().__init__("missing_scope")
            self.response = {
                "error": "missing_scope",
                "needed": "channels:read",
                "provided": "chat:write",
            }

    client = _Answers(scope_error=_MissingScopeError())
    monkeypatch.setattr(slack_channel, "_build_client", lambda _token: client)
    settings(ROBOTHOR_SLACK_BOT_TOKEN=FAKE_SLACK_TOKEN)
    row = _run(channel_checks.CHECKS, "slack.verify", make_ctx())[0]

    assert row.status == "fail"
    assert "channels:read" in row.detail, "the scope name IS the fix; it must be named"
    assert FAKE_SLACK_TOKEN not in row.detail


# ── email ────────────────────────────────────────────────────────────────────

FAKE_SMTP_PASSWORD = "not-a-real-smtp-password-9999"


@pytest.fixture
def no_gws(monkeypatch):
    """A box without the gws CLI, whatever this one actually has installed."""
    from robothor.engine.tools.handlers import gws

    monkeypatch.setattr(gws, "gws_available", lambda: False)


@pytest.fixture
def has_gws(monkeypatch):
    from robothor.engine.tools.handlers import gws

    monkeypatch.setattr(gws, "gws_available", lambda: True)


def test_email_transport_skips_when_unconfigured(no_gws) -> None:
    """Optional, and absent is information. A fail here would teach the
    operator that red output from the doctor is normal."""
    row = _run(channel_checks.CHECKS, "email.transport", make_ctx())[0]
    assert row.status == "skip"


def test_email_transport_passes_naming_the_transport(has_gws) -> None:
    row = _run(channel_checks.CHECKS, "email.transport", make_ctx())[0]
    assert row.status == "pass"
    assert "gws" in row.detail


def test_email_transport_names_the_credential_source_not_the_credential(
    settings, no_gws, monkeypatch
) -> None:
    from robothor.engine.channels import email as email_channel

    monkeypatch.setattr(
        email_channel, "_build_smtp", lambda *_a, **_kw: _AcceptingSMTP(), raising=True
    )
    settings(
        ROBOTHOR_EMAIL_FROM="genus@example.com",
        ROBOTHOR_EMAIL_SMTP_HOST="smtp.example.com",
        ROBOTHOR_EMAIL_SMTP_USER="genus@example.com",
        ROBOTHOR_EMAIL_SMTP_PASSWORD=FAKE_SMTP_PASSWORD,
    )
    row = _run(channel_checks.CHECKS, "email.transport", make_ctx())[0]

    assert row.status == "pass"
    assert FAKE_SMTP_PASSWORD not in row.detail
    assert "env" in row.detail, "which layer answered is the question an operator has"


def test_email_transport_fails_on_half_configured_smtp(settings, no_gws) -> None:
    """A host with no from-address sends nothing, and says so at the first
    delivery rather than here — which is a message nobody reads until it is
    missed."""
    settings(ROBOTHOR_EMAIL_SMTP_HOST="smtp.example.com")
    row = _run(channel_checks.CHECKS, "email.transport", make_ctx())[0]

    assert row.status == "fail"
    assert "ROBOTHOR_EMAIL_FROM" in row.detail


def test_a_stored_password_nothing_reads_is_reported(settings, no_gws) -> None:
    settings(ROBOTHOR_EMAIL_SMTP_PASSWORD=FAKE_SMTP_PASSWORD)
    row = _run(channel_checks.CHECKS, "email.transport", make_ctx())[0]

    assert row.status == "fail"
    assert "ROBOTHOR_EMAIL_SMTP_HOST" in row.detail
    assert FAKE_SMTP_PASSWORD not in row.detail


def test_email_transport_skips_the_socket_under_offline(settings, no_gws, monkeypatch) -> None:
    """--offline means nothing leaves the box. A zero call count, not merely a
    skipped status: a check could report `skip` after doing the work."""
    from robothor.engine.channels import email as email_channel

    def _never(*_args, **_kwargs):
        raise AssertionError("the doctor opened an SMTP connection under --offline")

    monkeypatch.setattr(email_channel, "_build_smtp", _never)
    settings(
        ROBOTHOR_EMAIL_FROM="genus@example.com",
        ROBOTHOR_EMAIL_SMTP_HOST="smtp.example.com",
    )
    row = _run(channel_checks.CHECKS, "email.transport", make_ctx(offline=True))[0]
    assert row.status == "skip"


def test_the_doctor_never_sends_an_email(settings, no_gws, monkeypatch) -> None:
    """The channel's `verify` sends one message. This must not: a diagnostic
    that mails somebody every time a Health panel refreshes is not a
    diagnostic."""
    from robothor.engine.channels import email as email_channel

    client = _AcceptingSMTP()
    monkeypatch.setattr(email_channel, "_build_smtp", lambda *_a, **_kw: client)
    settings(
        ROBOTHOR_EMAIL_FROM="genus@example.com",
        ROBOTHOR_EMAIL_SMTP_HOST="smtp.example.com",
    )
    _run(channel_checks.CHECKS, "email.transport", make_ctx())
    assert client.messages == []


class _AcceptingSMTP:
    """An SMTP server that answers every step and refuses no recipient."""

    def __init__(self) -> None:
        self.messages: list[object] = []

    def starttls(self) -> None: ...

    def login(self, _user: str, _password: str) -> None: ...

    def send_message(self, message: object) -> dict[str, object]:
        self.messages.append(message)
        return {}

    def quit(self) -> None: ...


# ── services ─────────────────────────────────────────────────────────────────


def test_a_service_that_answers_ready_passes(monkeypatch) -> None:
    monkeypatch.setattr(service_checks, "_probe", lambda url, _t: (True, f"{url}/ready → 200"))
    rows = _run(service_checks.CHECKS, "service.engine", make_ctx())
    assert rows[0].status == "pass"


def test_an_auth_gated_service_counts_as_up(monkeypatch) -> None:
    """probe_service reads 401 as up; the doctor must not second-guess it."""
    monkeypatch.setattr(
        service_checks, "_probe", lambda url, _t: (True, f"{url}/health → 401 (up, authenticated)")
    )
    rows = _run(service_checks.CHECKS, "service.bridge", make_ctx())
    assert rows[0].status == "pass"
    assert "401" in rows[0].detail


def test_a_refused_connection_fails(monkeypatch) -> None:
    monkeypatch.setattr(service_checks, "_probe", lambda url, _t: (False, f"{url}: refused"))
    monkeypatch.setattr(service_checks, "_units_installed", lambda: True)
    rows = _run(service_checks.CHECKS, "service.engine", make_ctx())
    assert rows[0].status == "fail"


def test_a_refused_connection_skips_when_nothing_was_asked_to_run(monkeypatch) -> None:
    """`genus init --yes` without `--start` starts no daemons, by design.

    Failing the install on services the wizard deliberately did not start made
    the documented quickstart exit 1 after writing a complete, working
    instance. "Not started" is not "broken", and the difference is the whole
    contract of a non-interactive install.
    """
    monkeypatch.setattr(service_checks, "_probe", lambda url, _t: (False, f"{url}: refused"))

    rows = _run(service_checks.CHECKS, "service.engine", make_ctx(services_expected=False))

    assert rows[0].status == "skip"
    assert "not started" in rows[0].detail
    assert "genus engine start" in rows[0].detail


def test_a_running_service_passes_even_when_none_was_expected(monkeypatch) -> None:
    """Truth beats the flag: a service that answers is up, whatever we assumed."""
    monkeypatch.setattr(service_checks, "_probe", lambda url, _t: (True, f"{url}/ready → 200"))

    rows = _run(service_checks.CHECKS, "service.engine", make_ctx(services_expected=False))

    assert rows[0].status == "pass"


def test_an_unset_expectation_asks_the_box(monkeypatch) -> None:
    """Nobody said, so look: a box with systemd units is meant to be serving."""
    monkeypatch.setattr(service_checks, "_probe", lambda url, _t: (False, f"{url}: refused"))

    monkeypatch.setattr(service_checks, "_units_installed", lambda: True)
    assert _run(service_checks.CHECKS, "service.bridge", make_ctx())[0].status == "fail"

    monkeypatch.setattr(service_checks, "_units_installed", lambda: False)
    assert _run(service_checks.CHECKS, "service.bridge", make_ctx())[0].status == "skip"


def test_the_optional_services_are_recommended_and_the_core_ones_required() -> None:
    severity = {check.id: check.severity for check in service_checks.CHECKS}
    assert severity["service.engine"] == "required"
    assert severity["service.bridge"] == "required"
    assert severity["service.orchestrator"] == "recommended"
    assert severity["service.vision"] == "recommended"


def test_every_service_is_probed_on_the_port_the_settings_declare(settings) -> None:
    settings(ROBOTHOR_ENGINE_PORT="19000", ROBOTHOR_BRIDGE_PORT="9200")
    urls = service_checks._urls(make_ctx())
    assert urls["bridge"].endswith(":9200")
    assert "19000" in urls["engine"] or urls["engine"].endswith(":19000")


def test_a_crashed_service_on_a_compose_host_is_a_required_failure(monkeypatch, settings) -> None:
    """A compose host has no systemd units -- its services are containers -- so
    "no unit" must not read as "nothing was meant to be running". Before this,
    a compose box whose bridge had crashed got a skip that also told the
    operator to run `genus serve`, which is not how that instance starts."""
    monkeypatch.setattr(service_checks, "_probe", lambda url, _t: (False, f"{url}: refused"))
    monkeypatch.setattr(service_checks, "_units_installed", lambda: False)
    settings(ROBOTHOR_INIT_SUBSTRATE="compose")

    rows = _run(service_checks.CHECKS, "service.bridge", make_ctx())

    assert rows[0].status == "fail"


def test_a_wheel_install_whose_siblings_answer_expects_them_all(monkeypatch, settings) -> None:
    """The documented happy path: the operator ran the two commands the wizard
    printed, and one of the services later died. Nothing on that box carries a
    unit, so the only evidence it was meant to be serving is that its siblings
    are."""
    settings(ROBOTHOR_INIT_SUBSTRATE="local")
    monkeypatch.setattr(service_checks, "_units_installed", lambda: False)
    monkeypatch.setattr(
        service_checks,
        "_probe",
        lambda url, _t: (
            (False, f"{url}: refused") if "9100" in url else (True, f"{url}/ready → 200")
        ),
    )

    rows = _run(service_checks.CHECKS, "service.bridge", make_ctx())

    assert rows[0].status == "fail"


def test_a_box_with_nothing_running_at_all_still_skips(monkeypatch, settings) -> None:
    """The case the skip exists for: `genus init --yes` without `--start`
    started no daemons, so none of them answering is not a fault."""
    settings(ROBOTHOR_INIT_SUBSTRATE="local")
    monkeypatch.setattr(service_checks, "_units_installed", lambda: False)
    monkeypatch.setattr(service_checks, "_probe", lambda url, _t: (False, f"{url}: refused"))

    rows = _run(service_checks.CHECKS, "service.bridge", make_ctx())

    assert rows[0].status == "skip"
    assert "not started" in rows[0].detail
