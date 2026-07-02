"""Flag-gated real tokenizer (Wave-2, W2-11).

estimate_tokens keeps the char/4 heuristic by default; with a model AND
ROBOTHOR_REAL_TOKENIZER_ENABLED it uses litellm.token_counter, falling back to
the heuristic on any error. Default behavior is unchanged (compaction-safe).
"""

from __future__ import annotations

import robothor.engine.context as context_mod
from robothor.engine.context import estimate_tokens

_MSGS = [{"role": "user", "content": "hello world " * 10}]


def test_default_is_heuristic(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_REAL_TOKENIZER_ENABLED", raising=False)
    # No model, flag off → char/4 path (no litellm call).
    expected = len(_MSGS[0]["content"]) // 4
    assert estimate_tokens(_MSGS) == expected


def test_no_model_stays_heuristic_even_when_enabled(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_REAL_TOKENIZER_ENABLED", "1")
    assert estimate_tokens(_MSGS) == (len(_MSGS[0]["content"]) // 4)


def test_uses_litellm_when_enabled(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_REAL_TOKENIZER_ENABLED", "1")
    import litellm

    monkeypatch.setattr(litellm, "token_counter", lambda model, messages: 4242)
    assert estimate_tokens(_MSGS, model="some/model") == 4242


def test_falls_back_on_counter_error(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_REAL_TOKENIZER_ENABLED", "1")
    import litellm

    def _boom(model, messages):
        raise RuntimeError("unknown model")

    monkeypatch.setattr(litellm, "token_counter", _boom)
    assert estimate_tokens(_MSGS, model="weird/model") == (len(_MSGS[0]["content"]) // 4)


def test_flag_helper(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_REAL_TOKENIZER_ENABLED", "on")
    assert context_mod._real_tokenizer_enabled() is True
    monkeypatch.setenv("ROBOTHOR_REAL_TOKENIZER_ENABLED", "0")
    assert context_mod._real_tokenizer_enabled() is False
