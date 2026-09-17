"""The ceiling has to be conservative for the content this engine actually reads.

``estimate_tokens`` is ``chars / 4``. Measured against a model's own tokenizer
(``prompt_eval_count``, hostile review of this branch, probe p14) that is ~1.4x
HIGH on prose — and 0.28-0.54x on the content a large tool result is made of:

    content               chars  chars/4     real  est/real
    english prose          9380     2345     1691      1.39   over-counts
    python source         12420     3105     2980      1.04   over-counts
    csv numbers           35585     8896    16386      0.54   UNDER by 46%
    base64                 8000     2000     5898      0.34   UNDER by 66%
    minified json         22451     5612    16963      0.33   UNDER by 67%
    hex digest lines      16249     4062    14370      0.28   UNDER by 72%
    indented json         34458     8614    23468      0.37   UNDER by 63%

A conversation the engine believed sat exactly on the local model's 57,344-token
ceiling was, for those contents, 105,000-203,000 real tokens — two to three
times past the 65,536 window. That is the incident again, through the one door
the fix leaves open: a `read_file` on a CSV export, a base64 data URI, a
lockfile diff, log lines full of UUIDs. Exactly what step 13 was.

The fixtures below are the reviewer's samples, rebuilt from the same seed and
pinned by character count so a drifting reconstruction cannot quietly stop
testing what was measured.
"""

from __future__ import annotations

import base64
import json
import random

import pytest

from robothor.engine.context_fit import estimate_for

#: ``(chars, real tokens)`` measured with the model's own tokenizer.
MEASURED = {
    "english prose": (9380, 1691),
    "python source": (12420, 2980),
    "csv numbers": (35585, 16386),
    "base64": (8000, 5898),
    "minified json": (22451, 16963),
    "hex digest lines": (16249, 14370),
    # Added after the first round: the classifier priced INDENTED JSON as
    # prose, because indentation and many short lines made it 35.8%
    # whitespace. Layout is not word structure, and 63% under on a 34KB tool
    # result is the incident's own shape.
    "indented json": (34458, 23468),
}


def _cases() -> dict[str, str]:
    """The measured samples, reconstructed byte-for-byte (seed 7)."""
    random.seed(7)
    rng = random.Random(7)
    return {
        "english prose": "The quarterly revenue report lists every line item for the region. "
        * 140,
        "base64": base64.b64encode(random.randbytes(6_000)).decode(),
        "minified json": json.dumps(
            [{"id": i, "v": random.random(), "k": f"k{i}"} for i in range(500)],
            separators=(",", ":"),
        ),
        "python source": (
            "def handler(request, context):\n"
            "    payload = json.loads(request.body or '{}')\n"
            "    return {'statusCode': 200, 'body': json.dumps(payload)}\n"
        )
        * 90,
        "hex digest lines": "\n".join(random.randbytes(32).hex() for _ in range(250)),
        "csv numbers": "\n".join(
            ",".join(str(random.randint(0, 10**9)) for _ in range(12)) for _ in range(300)
        ),
        # Its own stream, so adding it cannot shift a single byte of the six
        # above — their token counts are measured fixtures, and a fixture whose
        # content silently changes is testing nothing anyone measured.
        "indented json": json.dumps(
            [{"id": index, "v": rng.random(), "k": f"k{index}"} for index in range(500)],
            indent=2,
        ),
    }


@pytest.mark.parametrize("name", sorted(MEASURED))
def test_the_reconstruction_still_matches_what_was_measured(name):
    """A fixture that has drifted is testing something nobody measured."""
    assert len(_cases()[name]) == MEASURED[name][0]


@pytest.mark.parametrize("name", sorted(MEASURED))
def test_the_estimate_is_never_below_the_real_token_count(name):
    """The ceiling may cost context. It may not be optimistic."""
    chars, real = MEASURED[name]
    estimate = estimate_for([{"role": "tool", "content": _cases()[name]}])
    assert estimate >= real, (
        f"{name}: estimated {estimate} for {real} real tokens — a conversation "
        "sized on this lands over the window"
    )


def test_indentation_does_not_make_dense_content_look_like_prose():
    """The classifier reads word structure, not layout. Pretty-printing a
    payload must not halve what the engine thinks it costs."""
    from robothor.engine.context_fit import _chars_per_token

    assert _chars_per_token(_cases()["indented json"]) == _chars_per_token(
        _cases()["minified json"]
    )


def test_source_code_is_still_prose_despite_its_indentation():
    """The other half of the same judgement: code IS indented, and its lines
    are full of real word boundaries."""
    from robothor.engine.context_fit import _CHARS_PER_TOKEN_PROSE, _chars_per_token

    assert _chars_per_token(_cases()["python source"]) == _CHARS_PER_TOKEN_PROSE


def test_prose_is_not_inflated_out_of_all_proportion():
    """Conservative, not paranoid: an estimate 3x high wastes the window."""
    chars, real = MEASURED["english prose"]
    estimate = estimate_for([{"role": "user", "content": _cases()["english prose"]}])
    assert estimate <= real * 2


def test_it_is_never_lower_than_the_old_estimate():
    """The heuristic everything else uses stays a FLOOR, so no path that was
    safe before becomes unsafe now."""
    from robothor.engine.context import estimate_tokens

    for text in _cases().values():
        messages = [{"role": "tool", "content": text}]
        assert estimate_for(messages) >= estimate_tokens(messages)


def test_tool_call_overhead_is_still_counted():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c", "type": "function", "function": {"name": "x"}}],
        }
    ]
    assert estimate_for(messages) >= 400


def test_the_exact_tokenizer_wins_when_an_operator_enables_it(monkeypatch):
    """`ROBOTHOR_REAL_TOKENIZER_ENABLED` could not reach any of the new call
    sites: none of them passed `model`. With it on, the count is the model's,
    not a heuristic dressed up as one."""
    from robothor.engine import context, context_fit

    monkeypatch.setattr(context, "real_tokenizer_enabled", lambda: True)
    monkeypatch.setattr(context_fit, "estimate_tokens", lambda messages, model=None: 4242)

    assert estimate_for([{"role": "user", "content": "x" * 40_000}], "some/model") == 4242


async def test_a_conversation_of_dense_content_is_brought_under_the_ceiling(monkeypatch):
    """The two halves together, on the content that defeats the estimate.

    Worth pinning because of how they interact: the budget decides to compact
    on the content-aware number, `maybe_compress` re-checks with the flat one
    and can decline — and the deterministic ceiling then does the work anyway.
    Summarising base64 with a model would have been a poor use of a call.
    """
    from types import SimpleNamespace

    from robothor.engine.context_budget import keep_context_within_budget
    from robothor.engine.context_fit import estimate_for, fit_for

    local = "ollama_chat/qwen3.8:27b"
    blob = _cases()["hex digest lines"]
    messages = [{"role": "system", "content": "You are the main agent."}]
    for index in range(12):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": blob})
    messages.append({"role": "user", "content": "What is in those files?"})

    session = SimpleNamespace(
        messages=messages,
        run_id="dense",
        thin_previous_tool_results=lambda protect_after_index=0: 0,
    )

    async def _no_llm_compaction(messages, models=None, threshold=None, broken_models=None):
        return messages

    monkeypatch.setattr("robothor.engine.context.maybe_compress", _no_llm_compaction)

    await keep_context_within_budget(
        session,
        SimpleNamespace(id="main", eager_tool_compression=False),
        iteration=3,
        models=[local],
        broken_models=set(),
        hook_registry=None,
        pre_iteration_msg_idx=0,
    )

    assert estimate_for(session.messages, local) <= fit_for(local).hard_limit


@pytest.mark.llm
@pytest.mark.timeout(600)
def test_against_the_models_own_tokenizer():
    """Re-measure rather than trust the table. Skipped in CI."""
    import urllib.request

    def real_tokens(text: str) -> int:
        body = json.dumps(
            {
                "model": "qwen3:8b",
                "messages": [{"role": "user", "content": text}],
                "stream": False,
                "options": {"num_predict": 1, "num_ctx": 32768},
            }
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - loopback, fixed scheme
            "http://localhost:11434/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=600) as response:  # noqa: S310
            return int(json.loads(response.read()).get("prompt_eval_count") or 0)

    optimistic = []
    for name, text in _cases().items():
        real = real_tokens(text)
        estimate = estimate_for([{"role": "tool", "content": text}])
        if estimate < real:
            optimistic.append(f"{name}: estimated {estimate}, really {real}")
    assert not optimistic, optimistic
