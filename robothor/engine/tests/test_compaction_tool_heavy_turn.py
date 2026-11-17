"""A tool-heavy turn must shrink when it crosses the compaction trigger.

Measured on a live instance: an interactive run of 55 iterations logged
``Proactive compaction at iter N`` on every iteration past the 80,000-token
trigger, mostly reporting no reduction ("34→34 messages"), while the estimate
climbed to 800k-900k tokens and only the provider-ceiling shrink ever cut it.
The runs were tool-heavy: one turn, many calls, results of tens to hundreds of
thousands of characters of JSON.

These tests drive the REAL per-iteration budget step
(``context_budget.keep_context_within_budget``) through a scripted single-turn
run: one assistant tool call and one large JSON result per iteration, the
summariser mocked, the model's window 1M. They assert what was actually sent
to the model each iteration, not what the compaction says about itself.
"""

from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from robothor.engine.context import BACKOFF_YIELD_RATIO
from robothor.engine.context_budget import keep_context_within_budget
from robothor.engine.context_control import ContextControl, control
from robothor.engine.context_fit import estimate_for
from robothor.engine.run_budget import proactive_compaction_threshold

WINDOW = 1_000_000
REQUEST = "Audit every item in the catalog and report the inconsistencies."


def _catalog_json(page: int, chars: int) -> str:
    """Compact JSON, the shape a catalog or CRM listing tool returns."""
    rows: list[dict] = []
    size = 0
    while size < chars:
        row = {"id": f"item-{page}-{len(rows)}", "sku": f"SKU{len(rows):06d}", "qty": 3}
        rows.append(row)
        size += len(json.dumps(row)) + 2
    return json.dumps({"page": page, "items": rows})


class _Summariser:
    """The compaction model, scripted. Counts calls; never sees a network."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, models, **kwargs):
        self.calls += 1
        system = kwargs["messages"][0]["content"]
        text = '{"facts": []}' if "Extract key facts" in system else "Summary of the slice."
        message = SimpleNamespace(content=text, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _start(history_pairs: int = 0, system_reps: int = 300) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": "You are a careful agent. " * system_reps}]
    for i in range(history_pairs):
        messages.append({"role": "user", "content": f"Earlier question {i}. " * 15})
        messages.append({"role": "assistant", "content": f"Earlier answer {i}. " * 40})
    messages.append({"role": "user", "content": REQUEST})
    return messages


def _call(iteration: int) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{iteration}",
                "type": "function",
                "function": {"name": "catalog_list", "arguments": json.dumps({"page": iteration})},
            }
        ],
    }


def _newest_exchange(messages: list[dict]) -> list[dict]:
    start = max(i for i, m in enumerate(messages) if m.get("tool_calls"))
    return messages[start:]


async def _scripted_run(
    *,
    iterations: int,
    result_chars: int,
    history_pairs: int = 0,
    window: int = WINDOW,
    content_for=None,
    system_reps: int = 300,
):
    """Run the real budget step before every model call of a one-turn run.

    Returns, per iteration, the messages the model would have been sent.
    """
    messages = _start(history_pairs, system_reps)
    session = SimpleNamespace(
        messages=messages, run_id="run-1", thin_previous_tool_results=lambda **_: 0
    )
    config = SimpleNamespace(id="probe", eager_tool_compression=False, model_compaction="")
    state = ContextControl(active_request=REQUEST)
    token = control.set(state)
    summariser = _Summariser()
    sent: list[list[dict]] = []
    originals: dict[str, str] = {}
    limits = SimpleNamespace(max_input_tokens=window)
    try:
        with (
            patch("robothor.engine.compaction._acompletion_over_chain", new=summariser),
            patch("robothor.engine.model_registry.get_model_limits", return_value=limits),
            patch("robothor.engine.model_registry.get_output_tokens", return_value=16_000),
        ):
            for iteration in range(iterations):
                await keep_context_within_budget(
                    session,
                    config,
                    iteration=iteration,
                    models=["provider/large-window-model"],
                    broken_models=set(),
                    hook_registry=None,
                    pre_iteration_msg_idx=len(session.messages),
                )
                sent.append([dict(m) for m in session.messages])
                session.messages.append(_call(iteration))
                content = (content_for or _catalog_json)(iteration, result_chars)
                originals[f"call_{iteration}"] = content
                session.messages.append(
                    {"role": "tool", "tool_call_id": f"call_{iteration}", "content": content}
                )
    finally:
        control.reset(token)
    return SimpleNamespace(sent=sent, summariser=summariser, originals=originals, state=state)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ROBOTHOR_COMPACTION_TRIGGER_TOKENS",
        "ROBOTHOR_REAL_TOKENIZER_ENABLED",
        "ROBOTHOR_COMPACTION_MIN_GAP_ITERATIONS",
    ):
        monkeypatch.delenv(name, raising=False)


def _bound(messages: list[dict]) -> int:
    """What one call may carry: the trigger plus its backoff band, plus the
    newest exchange, which the model asked for and has not seen yet."""
    threshold = proactive_compaction_threshold(WINDOW)
    return int(threshold * BACKOFF_YIELD_RATIO) + estimate_for(_newest_exchange(messages))


class TestATurnOfManyLargeResultsShrinks:
    async def test_thirty_results_of_forty_thousand_tokens_stay_near_the_trigger(self):
        run = await _scripted_run(iterations=30, result_chars=44_000)
        for i, messages in enumerate(run.sent[1:], start=1):
            sent = estimate_for(messages)
            assert sent <= _bound(messages), (
                f"iteration {i} sent ~{sent:,} tokens; the trigger is "
                f"{proactive_compaction_threshold(WINDOW):,}"
            )

    async def test_oversized_results_after_a_chat_history_stay_bounded(self):
        """The live shape: a chat history, then results of 160,000 characters."""
        run = await _scripted_run(iterations=16, result_chars=160_000, history_pairs=10)
        for i, messages in enumerate(run.sent[1:], start=1):
            sent = estimate_for(messages)
            assert sent <= _bound(messages), f"iteration {i} sent ~{sent:,} tokens"

    async def test_elision_alone_needs_no_summary_call(self):
        """Replacing old results is deterministic; it must not cost a model call
        (each summary pass measured 4-7 s)."""
        run = await _scripted_run(iterations=30, result_chars=44_000)
        assert run.summariser.calls == 0


class TestWhatTheModelStillSees:
    async def test_the_newest_result_is_sent_verbatim(self):
        run = await _scripted_run(iterations=12, result_chars=44_000)
        for messages in run.sent[1:]:
            newest = messages[-1]
            assert newest["role"] == "tool"
            assert newest["content"] == run.originals[newest["tool_call_id"]]

    async def test_an_elided_result_names_its_tool_and_how_to_get_it_back(self):
        run = await _scripted_run(iterations=12, result_chars=44_000)
        last = run.sent[-1]
        stubs = [
            m
            for m in last
            if m.get("role") == "tool" and m["content"] != run.originals[m["tool_call_id"]]
        ]
        assert stubs, "nothing was elided"
        for stub in stubs:
            assert "catalog_list" in stub["content"]
            assert "again" in stub["content"]
            assert len(stub["content"]) < 2_000

    async def test_the_prefix_is_stable_between_compactions(self):
        """Prompt caching reuses a byte-identical prefix. A message, once
        elided, must not change again on a later pass."""
        run = await _scripted_run(iterations=20, result_chars=44_000)
        seen: dict[str, str] = {}
        for messages in run.sent:
            for m in messages:
                if m.get("role") != "tool":
                    continue
                cid, content = m["tool_call_id"], m["content"]
                if cid in seen and seen[cid] != run.originals[cid]:
                    assert content == seen[cid], f"{cid} was rewritten twice"
                seen[cid] = content


class TestTheLogSaysWhatHappened:
    async def test_every_compaction_line_reports_tokens_and_a_no_op_says_why(self, caplog):
        caplog.set_level(logging.INFO, logger="robothor.engine.context_budget")
        await _scripted_run(iterations=16, result_chars=160_000, history_pairs=10)
        lines = [r.getMessage() for r in caplog.records if "Proactive compaction" in r.getMessage()]
        assert lines
        for line in lines:
            match = re.search(r"~([\d,]+)→~([\d,]+) tokens", line)
            assert match, f"no before/after tokens in: {line}"
            before, after = (int(g.replace(",", "")) for g in match.groups())
            if after >= before:
                assert "nothing reduced:" in line, f"a no-op without a reason: {line}"


def _wire(messages: list[dict]) -> list[dict]:
    """What the provider receives: engine keys stripped, system turn hoisted."""
    from robothor.engine.context_control import strip_engine_keys

    return [m for m in strip_engine_keys(messages) if m.get("role") != "system"]


class TestCallsAndResultsStayPaired:
    """Every provider rejects a result with no call, and most a call with no
    result. Elision rewrites content only; these check the pairing survives
    on every wire format the engine sends through."""

    async def _histories(self) -> list[list[dict]]:
        run = await _scripted_run(iterations=16, result_chars=160_000, history_pairs=10)
        return run.sent[1:]

    async def test_openai_format_every_call_has_its_result_and_back(self):
        from robothor.engine.llm_client import LLMClient

        for messages in await self._histories():
            assert LLMClient._validate_tool_pairs(messages) is messages, "a result was orphaned"
            open_calls: set[str] = set()
            for m in messages:
                if m.get("tool_calls"):
                    assert not open_calls, f"calls left unanswered: {open_calls}"
                    open_calls = {c["id"] for c in m["tool_calls"]}
                elif m.get("role") == "tool":
                    assert m["tool_call_id"] in open_calls, "a result answers no open call"
                    open_calls.discard(m["tool_call_id"])
                else:
                    assert not open_calls, f"calls left unanswered: {open_calls}"

    async def test_anthropic_format_every_tool_use_is_answered_next_turn(self):
        from litellm.litellm_core_utils.prompt_templates.factory import anthropic_messages_pt

        for messages in await self._histories():
            wire = anthropic_messages_pt(
                _wire(messages), model="claude-model", llm_provider="anthropic"
            )
            for i, turn in enumerate(wire):
                uses = [
                    b["id"]
                    for b in turn["content"]
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
                if not uses:
                    continue
                answered = {
                    b["tool_use_id"]
                    for b in wire[i + 1]["content"]
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                }
                assert set(uses) <= answered

    async def test_gemini_format_calls_and_responses_match(self):
        from litellm.llms.vertex_ai.gemini.transformation import (
            _gemini_convert_messages_with_history,
        )

        for messages in await self._histories():
            wire = _gemini_convert_messages_with_history(_wire(messages))
            calls = [p for t in wire for p in t["parts"] if "function_call" in p]
            responses = [p for t in wire for p in t["parts"] if "function_response" in p]
            assert len(calls) == len(responses)


class TestTheExactTokenizerPathAgreesToo:
    """With ``ROBOTHOR_REAL_TOKENIZER_ENABLED`` the trigger counts exactly for
    the sizing model. Compaction must count the same way, or a context the
    trigger sees as over is measured under and returned unchanged."""

    async def test_compact_measures_with_the_sizing_model(self, monkeypatch):
        from robothor.engine.compaction import compact
        from robothor.engine.context import estimate_context_tokens

        monkeypatch.setenv("ROBOTHOR_REAL_TOKENIZER_ENABLED", "1")
        # An exact counter that sees four times what the heuristics see.
        monkeypatch.setattr(
            "litellm.token_counter",
            lambda model, messages: 4 * estimate_for(messages),
        )
        messages = _start()
        for i in range(6):
            messages.append(_call(i))
            messages.append(
                {"role": "tool", "tool_call_id": f"call_{i}", "content": _catalog_json(i, 20_000)}
            )
        model = "provider/large-window-model"
        heuristic = estimate_for(messages)
        threshold = heuristic * 2
        assert estimate_context_tokens(messages, model) > threshold > heuristic

        with patch("robothor.engine.compaction._acompletion_over_chain", new=_Summariser()):
            result = await compact(
                messages, models=[model], threshold=threshold, drain_to=int(threshold * 0.75)
            )

        assert result.tokens_before == estimate_context_tokens(messages, model)
        assert result.tokens_after < result.tokens_before

    async def test_the_exact_count_runs_off_the_event_loop(self, monkeypatch):
        """``litellm.token_counter`` is synchronous and CPU-bound; compaction
        counts many times, and each count on the loop stalls every session."""
        import asyncio
        import threading
        import time

        from robothor.engine.compaction import compact

        loop = asyncio.get_running_loop()
        gate = threading.Barrier(8, timeout=2)
        await asyncio.gather(*(asyncio.to_thread(gate.wait) for _ in range(8)))
        lags: list[float] = []

        def slow_counter(model, messages):
            entered = time.monotonic()
            loop.call_soon_threadsafe(lambda: lags.append(time.monotonic() - entered))
            time.sleep(0.05)
            return 4 * estimate_for(messages)

        monkeypatch.setenv("ROBOTHOR_REAL_TOKENIZER_ENABLED", "1")
        monkeypatch.setattr("litellm.token_counter", slow_counter)
        messages = _start()
        for i in range(6):
            messages.append(_call(i))
            messages.append(
                {"role": "tool", "tool_call_id": f"call_{i}", "content": _catalog_json(i, 20_000)}
            )
        threshold = estimate_for(messages) * 2

        with patch("robothor.engine.compaction._acompletion_over_chain", new=_Summariser()):
            await compact(
                messages,
                models=["provider/large-window-model"],
                threshold=threshold,
                drain_to=int(threshold * 0.75),
            )
        await asyncio.sleep(0.05)

        assert lags, "the exact counter was never reached"
        assert max(lags) <= 0.02, f"a loop callback waited {max(lags) * 1000:.0f} ms"


FACT = "EARLY_FACT=EXAMPLE-7731"


def _file_with_an_early_fact(iteration: int, chars: int) -> str:
    """A ``read_file`` result; only the first file carries the fact, at its head."""
    text = (FACT + "\n" if iteration == 0 else "") + _catalog_json(iteration, chars)
    return json.dumps({"path": f"data/file-{iteration}.txt", "content": text})


class TestElisionLosesLittle:
    """Measured in a lab run on a 128k-window model: the only result holding the
    fact the final answer needed was compacted away, the summarizer extracted
    nothing, and the answer missed it. Elision must keep a bounded excerpt, and
    a context only just over the drain target must not drop whole exchanges."""

    async def test_an_early_fact_survives_a_long_turn_on_a_128k_window(self):
        run = await _scripted_run(
            iterations=30,
            result_chars=40_000,
            window=128_000,
            content_for=_file_with_an_early_fact,
        )
        assert FACT in json.dumps(run.sent[-1]), "the early fact left the context"
        for messages in run.sent[1:]:
            assert estimate_for(messages) <= int(64_000 * BACKOFF_YIELD_RATIO) + estimate_for(
                _newest_exchange(messages)
            )

    async def test_the_stub_says_it_was_removed_and_to_call_again_with_the_same_arguments(
        self,
    ):
        run = await _scripted_run(iterations=6, result_chars=44_000)
        stub = next(
            m["content"]
            for m in run.sent[-1]
            if m.get("role") == "tool" and m["content"] != run.originals[m["tool_call_id"]]
        )
        assert "removed to save space" in stub
        assert "same arguments" in stub
        assert '{"page": ' in stub, "the arguments to repeat are shown"

    async def test_excerpts_shrink_as_more_results_are_elided(self):
        run = await _scripted_run(iterations=30, result_chars=44_000)
        stubs = [
            m["content"]
            for m in run.sent[-1]
            if m.get("role") == "tool" and m["content"] != run.originals[m["tool_call_id"]]
        ]
        assert len(stubs) > 10
        assert all(len(stub) < 2_000 for stub in stubs)
        assert len(stubs[0]) > len(stubs[-1]), "the excerpt budget does not scale down"


class TestElisionStopsUnderTheTarget:
    async def test_only_as_many_results_as_needed_are_elided_largest_first(self):
        from robothor.engine.compaction import compact

        messages = _start()
        sizes = [30_000, 90_000, 30_000, 30_000]
        for i, chars in enumerate(sizes):
            messages.append(_call(i))
            messages.append(
                {"role": "tool", "tool_call_id": f"call_{i}", "content": _catalog_json(i, chars)}
            )
        before = estimate_for(messages)
        largest = estimate_for([messages[5]])
        drain_to = before - largest // 2
        with patch("robothor.engine.compaction._acompletion_over_chain", new=_Summariser()):
            result = await compact(
                messages, models=["provider/m"], threshold=before - 1, drain_to=drain_to
            )

        changed = [
            m["tool_call_id"]
            for m, original in zip(result.messages, messages, strict=False)
            if m.get("role") == "tool" and m["content"] != original["content"]
        ]
        assert changed == ["call_1"], f"elided {changed}; one, the largest, was enough"
        assert result.tokens_after <= drain_to

    async def test_a_pass_that_lands_on_the_target_stubs_every_old_result_before_summarizing(
        self,
    ):
        """Targeted elision can stop exactly at, or a token over, the drain
        target. The summary that follows must not take the large results that
        were never elided with it, unstubbed."""
        from robothor.engine.compaction import compact, llm_free_pre_pass

        messages = _start(history_pairs=10)  # long enough to reach the summary path
        for i, chars in enumerate([30_000, 30_000, 90_000, 30_000]):
            messages.append(_call(i))
            messages.append(
                {"role": "tool", "tool_call_id": f"call_{i}", "content": _catalog_json(i, chars)}
            )
        before = estimate_for(messages)
        one_elided, _ = llm_free_pre_pass(messages, 2, target=before - 1)
        drain_to = estimate_for(one_elided)  # exactly where targeted elision stops

        with patch("robothor.engine.compaction._acompletion_over_chain", new=_Summariser()):
            result = await compact(
                messages, models=["provider/m"], threshold=before - 1, drain_to=drain_to
            )

        kept = {m.get("tool_call_id") for m in result.messages if m.get("role") == "tool"}
        assert kept == {"call_0", "call_1", "call_2", "call_3"}, f"summarized away: {kept}"


class TestStubsAreNeverSummarizedAway:
    """Lab pass three: on a 128k window with a production-sized system prompt,
    thirty reads of stubs plus their calls crossed the trigger by themselves,
    and the last-resort drain dropped every one of them, the early fact's
    excerpt included (63 -> 5 messages). Stubs are kept; when they alone do
    not fit, they are folded into one deterministic ledger, never dropped."""

    async def test_the_early_fact_survives_thirty_reads_under_a_large_system_prompt(self):
        run = await _scripted_run(
            iterations=31,
            result_chars=40_000,
            window=128_000,
            content_for=_file_with_an_early_fact,
            system_reps=1_500,
        )
        last = run.sent[-1]
        assert FACT in json.dumps(last), "the early fact left the context"
        for messages in run.sent[1:]:
            bound = int(64_000 * BACKOFF_YIELD_RATIO) + estimate_for(_newest_exchange(messages))
            assert estimate_for(messages) <= bound

    async def test_every_read_is_still_named_with_its_arguments(self):
        run = await _scripted_run(
            iterations=31,
            result_chars=40_000,
            window=128_000,
            content_for=_file_with_an_early_fact,
            system_reps=1_500,
        )
        text = json.dumps(run.sent[-1])
        for i in range(29):
            assert f"data/file-{i}.txt" in text, f"read {i} vanished without a trace"
        assert "same arguments" in text

    async def test_pairing_stays_valid_after_folding(self):
        from robothor.engine.llm_client import LLMClient

        run = await _scripted_run(
            iterations=31,
            result_chars=40_000,
            window=128_000,
            content_for=_file_with_an_early_fact,
            system_reps=1_500,
        )
        for messages in run.sent:
            assert LLMClient._validate_tool_pairs(messages) is messages
            calls = {c["id"] for m in messages for c in m.get("tool_calls") or []}
            results = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
            assert calls == results


class TestADegenerateSummaryIsNotTrusted:
    async def test_a_near_empty_summary_does_not_replace_the_originals(self, caplog):
        """A summary of a few characters standing in for tens of thousands of
        tokens is a failed summary. The originals are shrunk, not discarded."""
        import logging

        from robothor.engine.compaction import compact

        messages = _start(history_pairs=30)
        for i in range(3):
            messages.append({"role": "user", "content": f"Note {i}: the code is BLUE-{i}. " * 400})
            messages.append({"role": "assistant", "content": f"Noted {i}. " * 50})
        before = estimate_for(messages)

        class _Tiny(_Summariser):
            async def __call__(self, models, **kwargs):
                response = await super().__call__(models, **kwargs)
                if "Extract key facts" not in kwargs["messages"][0]["content"]:
                    response.choices[0].message.content = "ok"
                return response

        caplog.set_level(logging.WARNING, logger="robothor.engine.compaction")
        with patch("robothor.engine.compaction._acompletion_over_chain", new=_Tiny()):
            result = await compact(
                messages, models=["provider/m"], threshold=before // 2, drain_to=before // 3
            )

        text = json.dumps(result.messages)
        assert all(f"BLUE-{i}" in text for i in range(3)), "originals were discarded"
        assert "Conversation summary]\\nok" not in text
        assert any("summary" in r.getMessage().lower() for r in caplog.records)
