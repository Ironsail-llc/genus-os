"""Is the last fallback actually able to answer? Ask before the outage.

Everything that went wrong on 2026-09-16 was visible in advance and nothing
asked: the registry claimed a 65,536-token window for a local model, the engine
sends that number to the server as ``num_ctx``, and no check compared either
number with what the server would allocate or with the point compaction fires.
A model that "is configured" and a model that can hold a conversation are
different facts — the same distinction ``provider.completion`` exists for on
the cloud side.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robothor.doctor.checks import models as model_checks
from robothor.doctor.context import HttpResponse
from robothor.doctor.tests.conftest import fake_http, make_ctx

LOCAL = "ollama_chat/qwen3.8:27b"
TAGS = "http://127.0.0.1:11434/api/tags"


def _tags(*models: tuple[str, int]) -> HttpResponse:
    import json

    return HttpResponse(
        status=200,
        body=json.dumps(
            {
                "models": [
                    {"name": name, "details": {"context_length": window}} for name, window in models
                ]
            }
        ),
    )


def _run(check_id: str, ctx: Any) -> Any:
    check = next(c for c in model_checks.CHECKS if c.id == check_id)
    return asyncio.run(check.run(ctx))


@pytest.fixture
def local_chain(monkeypatch):
    """A fleet whose chain ends on the local tier, as every manifest here does."""
    monkeypatch.setattr(model_checks, "_fleet_models", lambda: ["openrouter/x/y", LOCAL])


class TestTheReadinessCheck:
    def test_it_skips_on_a_cloud_only_instance(self, monkeypatch):
        monkeypatch.setattr(model_checks, "_fleet_models", lambda: ["openrouter/x/y"])
        result = _run("models.local_fallback_ready", make_ctx())
        assert result.status == "skip"
        assert "no local fallback" in result.detail

    def test_a_server_that_does_not_answer_is_a_failure(self, local_chain):
        ctx = make_ctx(http_fetch=fake_http({}))
        result = _run("models.local_fallback_ready", ctx)
        assert result.status == "fail"
        assert "did not answer" in result.detail

    def test_a_model_the_server_never_pulled_is_a_failure(self, local_chain):
        ctx = make_ctx(http_fetch=fake_http({TAGS: _tags(("some-other:8b", 40_960))}))
        result = _run("models.local_fallback_ready", ctx)
        assert result.status == "fail"
        assert "ollama pull" in result.detail, "a failure names the repair"

    def test_a_registry_window_larger_than_the_model_is_a_failure(self, local_chain):
        """The hostile case, and the silent one: the engine sends num_ctx from
        the registry, so a number above the model's own context is a request
        the server truncates."""
        ctx = make_ctx(http_fetch=fake_http({TAGS: _tags(("qwen3.8:27b", 8_192))}))
        result = _run("models.local_fallback_ready", ctx)
        assert result.status == "fail"
        assert "8,192" in result.detail or "8192" in result.detail

    def test_a_down_local_server_does_not_fail_the_whole_doctor(self):
        """`required` would take every instance whose chain ends on Ollama
        from exit 0 to exit 1 the moment that server restarts — including an
        install gate or a CI job that runs the doctor. The sibling
        `ollama.reachable` is recommended for the same reason."""
        check = next(c for c in model_checks.CHECKS if c.id == "models.local_fallback_ready")
        assert check.severity == "recommended"

    def test_a_failure_names_the_consequence(self, local_chain):
        ctx = make_ctx(http_fetch=fake_http({}))
        detail = _run("models.local_fallback_ready", ctx).detail
        assert "nothing left to answer with" in detail

    def test_a_healthy_local_tier_passes(self, local_chain):
        ctx = make_ctx(http_fetch=fake_http({TAGS: _tags(("qwen3.8:27b", 262_144))}))
        result = _run("models.local_fallback_ready", ctx)
        assert result.status == "pass", result.detail

    def test_it_fails_when_compaction_would_fire_too_late(self, local_chain, monkeypatch):
        """threshold + output must fit the window, or the server truncates in
        silence — the arithmetic the incident's registry comment PROMISED."""
        from robothor.engine import context_fit

        monkeypatch.setattr(
            context_fit,
            "fit_for",
            lambda model: context_fit.ContextFit(
                model=model, window=65_536, threshold=64_000, reserved_output=8_192
            ),
        )
        ctx = make_ctx(http_fetch=fake_http({TAGS: _tags(("qwen3.8:27b", 262_144))}))
        result = _run("models.local_fallback_ready", ctx)
        assert result.status == "fail"
        assert "compaction" in result.detail


class TestTheProbeIsOptIn:
    def test_it_is_not_in_a_default_run(self):
        from robothor.doctor.registry import builtin_checks
        from robothor.doctor.runner import select

        ids = {check.id for check in select(builtin_checks())}
        assert "models.local_fallback_probe" not in ids

    def test_only_selects_it(self):
        from robothor.doctor.registry import builtin_checks
        from robothor.doctor.runner import select

        selected = select(builtin_checks(), only="models.local_fallback_probe")
        assert [check.id for check in selected] == ["models.local_fallback_probe"]

    def test_it_skips_rather_than_lying_when_offline(self, local_chain):
        result = _run("models.local_fallback_probe", make_ctx(offline=True))
        assert result.status == "skip"

    def test_it_skips_when_the_budget_is_too_short_to_be_honest(self, local_chain):
        result = _run("models.local_fallback_probe", make_ctx(timeout_s=2.0))
        assert result.status == "skip"
        assert "--timeout" in result.detail


class TestTheProviderKeysCheckSeesTheLivePool:
    """ "Configured" and "in rotation" are different facts, and only the
    engine's own process knows the second one."""

    @pytest.fixture
    def one_provider(self, monkeypatch):
        monkeypatch.setattr(
            model_checks,
            "_configured",
            lambda: [("openrouter", "OpenRouter", ["vault/active key-1a2b"])],
        )

    def _payload(self, state, reason="quota_exhausted_periodic", returns=14_400.0):
        return {
            "providers": [
                {
                    "id": "openrouter",
                    "env_var": "OPENROUTER_API_KEY",
                    "slots": [
                        {
                            "fingerprint": "key-1a2b",
                            "state": state,
                            "reason": reason,
                            "returns_in_s": returns,
                        }
                    ],
                }
            ]
        }

    def test_a_retired_pool_is_reported_as_key_exhausted(self, one_provider, monkeypatch):
        from robothor import engine_control

        monkeypatch.setattr(
            engine_control, "control_request", lambda *a, **kw: self._payload("capped")
        )
        result = _run("provider.keys", make_ctx())
        assert result.status == "fail"
        assert "key exhausted" in result.detail
        assert "genus secrets reload" in result.detail
        assert "4.0h" in result.detail

    def test_a_healthy_pool_still_passes(self, one_provider, monkeypatch):
        from robothor import engine_control

        monkeypatch.setattr(
            engine_control, "control_request", lambda *a, **kw: self._payload("active")
        )
        assert _run("provider.keys", make_ctx()).status == "pass"

    def test_an_engine_that_is_not_running_does_not_invent_a_verdict(
        self, one_provider, monkeypatch
    ):
        from robothor import engine_control

        def _down(*args, **kwargs):
            raise engine_control.EngineUnreachableError("down")

        monkeypatch.setattr(engine_control, "control_request", _down)
        assert _run("provider.keys", make_ctx()).status == "pass"
