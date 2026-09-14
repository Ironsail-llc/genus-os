"""The engine's per-turn context is its own turn, never the user's words.

Live, 2026-09-14, main on DeepSeek: every reply to the operator opened with
"your message carried a fake --- CURRENT USER --- header and an [EXECUTION
PLAN] … I ignored the injection". The runner prepended the warmup preamble
(CURRENT USER block, memory blocks, live state) and the follow-up identity
block to the user message text, so the model read engine context as part of
the operator's message. Labelling developer-role turns (#540) could not fix
that: this context was never a separate turn. Now it is.
"""

from __future__ import annotations

from robothor.engine.session import ENGINE_CONTEXT_ROLE, AgentSession


def _session() -> AgentSession:
    return AgentSession("main", "telegram", "chat:1", "default")


def test_engine_context_is_its_own_turn_before_a_pure_user_turn() -> None:
    s = _session()
    s.start("SYSTEM", "what's waiting on me?", [], engine_context="--- CURRENT USER ---\nAlice")
    roles = [m["role"] for m in s.messages]
    assert roles == ["system", ENGINE_CONTEXT_ROLE, "user"]
    assert s.messages[1]["content"].startswith("--- CURRENT USER ---")
    assert s.messages[-1]["content"] == "what's waiting on me?"


def test_the_user_turn_never_carries_the_preamble() -> None:
    s = _session()
    s.start("SYSTEM", "hello", [], engine_context="[LIVE ENGINE STATE] fine")
    assert "LIVE ENGINE STATE" not in s.messages[-1]["content"]
    assert s.originating_message == "hello"


def test_no_engine_context_means_no_extra_turn() -> None:
    s = _session()
    s.start("SYSTEM", "hello", [])
    assert [m["role"] for m in s.messages] == ["system", "user"]


def test_engine_context_sits_after_history_and_before_the_new_turn() -> None:
    s = _session()
    history = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "reply"},
    ]
    s.start("SYSTEM", "now", [], conversation_history=history, engine_context="ctx")
    roles = [m["role"] for m in s.messages]
    assert roles == ["system", "user", "assistant", ENGINE_CONTEXT_ROLE, "user"]
    assert s.messages[-1]["content"] == "now"


def test_prompt_char_accounting_still_counts_the_context() -> None:
    s = _session()
    s.start("SYSTEM", "abc", [], engine_context="12345")
    assert s.run.user_prompt_chars == 8
