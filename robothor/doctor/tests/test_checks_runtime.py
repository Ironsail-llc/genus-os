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


FAKE_API_KEY = "sk-test-do-not-print-9999999999"


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
    monkeypatch.setattr(model_checks, "_fleet_model", lambda: "")
    rows = _run(model_checks.CHECKS, "provider.completion", make_ctx())
    assert rows[0].status == "skip"


def test_completion_passes_on_a_real_answer(monkeypatch) -> None:
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


def test_ollama_skips_on_a_cloud_only_instance() -> None:
    """Every Ollama setting carries a default, so a cloud-only instance that
    never configured one would otherwise carry a permanent recommended failure
    for a service it does not use -- and a red line an operator learns to
    ignore is worse than no line."""
    ctx = make_ctx(http_fetch=fake_http({}))
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "skip"
    assert "not configured" in rows[0].detail


def test_ollama_that_answers_on_the_default_endpoint_still_passes() -> None:
    """Untouched defaults and a live server is the normal single-box install.
    Skipping there would hide a dependency the memory path actually uses."""
    ctx = make_ctx(
        http_fetch=fake_http({"http://127.0.0.1:11434/api/tags": HttpResponse(status=200)})
    )
    assert _run(model_checks.CHECKS, "ollama.reachable", ctx)[0].status == "pass"


def test_an_explicitly_configured_ollama_that_is_down_is_a_failure(settings) -> None:
    """The operator said it should be there. Silence is then a real finding."""
    settings(ROBOTHOR_OLLAMA_URL="http://ollama.example.test:11434")
    ctx = make_ctx(http_fetch=fake_http({}))
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "fail"


def test_ollama_passes_when_tags_answers() -> None:
    ctx = make_ctx(
        http_fetch=fake_http({"http://127.0.0.1:11434/api/tags": HttpResponse(status=200)})
    )
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "pass"


def test_ollama_failure_is_recommended_not_required(settings) -> None:
    check = next(item for item in model_checks.CHECKS if item.id == "ollama.reachable")
    assert check.severity == "recommended"
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
    rows = _run(service_checks.CHECKS, "service.engine", make_ctx())
    assert rows[0].status == "fail"


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
