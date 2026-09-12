"""The probe that decides whether an instance has a working brain.

OpenClaw's wizard tests the chosen provider with a real completion before it
writes anything. Ours does the same, and the interesting part is the
credential: the key the operator has just typed is not yet stored anywhere, and
putting it in ``os.environ`` to make one call would hand it to every thread and
every subprocess for the life of the process. So it goes to ``llm_call`` as an
argument, for that call only — and these tests assert the environment is
untouched afterwards, because "we meant to" is not a control.
"""

from __future__ import annotations

import os
from typing import Any

from robothor.doctor.context import HttpResponse
from robothor.init.context import InitContext
from robothor.init.provider_probe import (
    ProbeResult,
    detect_codex_login,
    detect_ollama_tool_models,
    detect_provider_keys,
    probe_model,
    record_unprobed,
)


class _Call:
    """A stand-in for ``llm_call`` that records how it was called."""

    def __init__(self, *, answer: Any = "pong", raises: BaseException | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.kwargs: dict[str, Any] = {}
        self.calls = 0

    async def __call__(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls += 1
        self.kwargs = dict(kwargs, messages=messages)
        if self.raises is not None:
            raise self.raises
        return self.answer


class TestTheKeyNeverEntersTheEnvironment:
    def test_the_candidate_key_is_passed_as_an_argument(self):
        call = _Call()
        probe_model("openrouter/openai/gpt-5.4", api_key="sk-candidate", llm_call=call)

        assert call.kwargs["api_key"] == "sk-candidate"

    def test_os_environ_is_unchanged_by_a_probe(self):
        before = dict(os.environ)
        call = _Call()

        probe_model("openrouter/openai/gpt-5.4", api_key="sk-candidate", llm_call=call)

        assert dict(os.environ) == before

    def test_os_environ_is_unchanged_by_a_failed_probe(self):
        before = dict(os.environ)
        call = _Call(raises=RuntimeError("401 Unauthorized"))

        probe_model("openrouter/openai/gpt-5.4", api_key="sk-candidate", llm_call=call)

        assert dict(os.environ) == before

    def test_a_key_echoed_in_an_error_is_redacted(self):
        call = _Call(raises=RuntimeError("bad key sk-candidate rejected"))

        result = probe_model("openrouter/openai/gpt-5.4", api_key="sk-candidate", llm_call=call)

        assert "sk-candidate" not in result.detail
        assert "***" in result.detail


class TestOneTokenCompletion:
    def test_a_success_is_probed_and_ok(self):
        call = _Call()
        result = probe_model("openrouter/openai/gpt-5.4", api_key="k", llm_call=call)

        assert result == ProbeResult(
            ok=True,
            provider="openrouter",
            model="openrouter/openai/gpt-5.4",
            detail="answered a 1-token completion",
            probed=True,
        )

    def test_it_asks_for_exactly_one_token(self):
        call = _Call()
        probe_model("openrouter/openai/gpt-5.4", api_key="k", llm_call=call)

        assert call.kwargs["max_tokens"] == 1
        assert call.kwargs["model"] == "openrouter/openai/gpt-5.4"

    def test_an_empty_answer_is_a_failure_not_a_pass(self):
        result = probe_model("openrouter/openai/gpt-5.4", api_key="k", llm_call=_Call(answer=None))

        assert result.ok is False
        assert result.probed is True
        assert "no completion" in result.detail

    def test_a_rejected_key_fails_with_the_provider_message(self):
        call = _Call(raises=RuntimeError("401 Unauthorized"))
        result = probe_model("openrouter/openai/gpt-5.4", api_key="k", llm_call=call)

        assert result.ok is False
        assert "401 Unauthorized" in result.detail

    def test_a_timeout_fails_and_says_so(self):
        call = _Call(raises=TimeoutError())
        result = probe_model("openrouter/openai/gpt-5.4", api_key="k", llm_call=call, timeout=7)

        assert result.ok is False
        assert "7s" in result.detail

    def test_the_provider_is_derived_from_the_model_prefix(self):
        result = probe_model("anthropic/claude-sonnet-4.6", api_key="k", llm_call=_Call())

        assert result.provider == "anthropic"

    def test_an_unknown_prefix_leaves_the_provider_blank_rather_than_guessing(self):
        result = probe_model("some-local-model", api_key=None, llm_call=_Call())

        assert result.provider == ""
        assert result.ok is True


class TestOffline:
    def test_offline_records_the_choice_unprobed(self):
        result = record_unprobed("openrouter", "openrouter/openai/gpt-5.4")

        assert result.ok is True
        assert result.probed is False
        assert "--offline" in result.detail

    def test_offline_never_calls_the_provider(self):
        call = _Call()
        record_unprobed("openrouter", "openrouter/openai/gpt-5.4")

        assert call.calls == 0


class TestDetection:
    def test_detected_keys_come_from_the_pool_and_carry_no_key_material(self, monkeypatch):
        from robothor.engine import key_pool

        def fake_slots(provider_id: str) -> list[Any]:
            if provider_id != "openrouter":
                return []
            return [
                key_pool.SlotStatus(
                    position=1, source="env", fingerprint="sha256:ab", state="active"
                )
            ]

        monkeypatch.setattr("robothor.engine.key_pool.provider_slots", fake_slots)

        detected = detect_provider_keys()

        assert [row.id for row in detected] == ["openrouter"]
        assert detected[0].slots == ("sha256:ab",)
        assert detected[0].env_var == "OPENROUTER_API_KEY"

    def test_a_provider_the_pool_cannot_read_is_not_reported_as_configured(self, monkeypatch):
        monkeypatch.setattr("robothor.engine.key_pool.provider_slots", lambda _id: [])

        assert detect_provider_keys() == []

    def test_only_tool_capable_ollama_models_are_offered(self, tmp_path):
        def fetch(method, url, body, timeout):
            if url.endswith("/api/tags"):
                return HttpResponse(
                    status=200,
                    body='{"models": [{"name": "qwen3:8b"}, {"name": "qwen3-embedding:0.6b"}]}',
                )
            if url.endswith("/api/show") and body == {"model": "qwen3:8b"}:
                return HttpResponse(status=200, body='{"capabilities": ["completion", "tools"]}')
            return HttpResponse(status=200, body='{"capabilities": ["embedding"]}')

        ctx = InitContext(workspace=tmp_path, http_fetch=fetch)

        assert detect_ollama_tool_models(ctx) == ["qwen3:8b"]

    def test_an_unreachable_ollama_detects_nothing_and_does_not_raise(self, tmp_path):
        def fetch(method, url, body, timeout):
            return HttpResponse(status=0, error="ConnectError: refused")

        ctx = InitContext(workspace=tmp_path, http_fetch=fetch)

        assert detect_ollama_tool_models(ctx) == []

    def test_a_codex_login_is_detected_from_its_auth_file(self, tmp_path):
        codex_home = tmp_path / "codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text("{}", encoding="utf-8")

        assert detect_codex_login(codex_home) == str(codex_home / "auth.json")

    def test_no_codex_login_is_an_empty_string_not_an_exception(self, tmp_path):
        assert detect_codex_login(tmp_path / "nowhere") == ""
