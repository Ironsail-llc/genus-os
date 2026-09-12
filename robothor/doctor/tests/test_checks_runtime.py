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


def test_redis_passes_when_ping_answers(monkeypatch) -> None:
    class FakeRedis:
        def __init__(self, **_kwargs):
            pass

        def ping(self):
            return True

        def close(self):
            pass

    import sys
    import types

    module = types.ModuleType("redis")
    module.Redis = FakeRedis  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", module)

    rows = _run(redis_checks.CHECKS, "redis.connect", make_ctx())
    assert rows[0].status == "pass"
    assert "6379" in rows[0].detail


def test_redis_failure_does_not_print_the_password(monkeypatch, settings) -> None:
    settings(ROBOTHOR_REDIS_PASSWORD="hunter2-redis")

    class FakeRedis:
        def __init__(self, **_kwargs):
            pass

        def ping(self):
            raise ConnectionError("auth failed for redis://:hunter2-redis@127.0.0.1:6379")

        def close(self):
            pass

    import sys
    import types

    module = types.ModuleType("redis")
    module.Redis = FakeRedis  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis", module)

    rows = _run(redis_checks.CHECKS, "redis.connect", make_ctx())
    assert rows[0].status == "fail"
    assert "hunter2-redis" not in rows[0].detail
    assert "ConnectionError" in rows[0].detail


# ── models ───────────────────────────────────────────────────────────────────


def test_provider_keys_passes_and_reports_only_fingerprints(monkeypatch) -> None:
    from robothor.engine.key_pool import SlotStatus

    monkeypatch.setattr(
        model_checks,
        "_configured",
        lambda: [("openrouter", "OpenRouter", ["vault/active sha256:ab12cd34"])],
    )
    rows = _run(model_checks.CHECKS, "provider.keys", make_ctx())
    assert rows[0].status == "pass"
    assert "sha256:ab12cd34" in rows[0].detail
    assert SlotStatus  # the shape the real implementation reports


def test_provider_keys_fails_with_nothing_configured(monkeypatch) -> None:
    monkeypatch.setattr(model_checks, "_configured", lambda: [])
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


def test_ollama_passes_when_tags_answers() -> None:
    ctx = make_ctx(
        http_fetch=fake_http({"http://127.0.0.1:11434/api/tags": HttpResponse(status=200)})
    )
    rows = _run(model_checks.CHECKS, "ollama.reachable", ctx)
    assert rows[0].status == "pass"


def test_ollama_failure_is_recommended_not_required() -> None:
    check = next(item for item in model_checks.CHECKS if item.id == "ollama.reachable")
    assert check.severity == "recommended"
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
