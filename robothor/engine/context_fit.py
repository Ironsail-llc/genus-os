"""What the NEXT call can hold, and how to make the messages fit it.

Compaction already had a threshold; what it did not have was the right model.
``LLMClient.sizing_model`` answers with the first model not in
``broken_models``, and a model whose credential pool is exhausted never enters
that set — the spent-credential branch of the chain walk breaks out before
``_handle_model_error`` runs, deliberately, because a rejected key says nothing
about a model. The consequence showed up on 2026-09-16: with the cloud key at
its weekly cap, a run spent its whole life on a local 65,536-token fallback
while sizing its context against the 1M-token primary, grew past the local
window, and died five retries later on

    Ollama_chatException - {"error":"no user query found in messages"}

Ollama's answer when a conversation exceeds ``num_ctx``: it truncates from the
front, and when the tail alone overflows there is no user turn left to answer.

Three things live here, because they are one question asked three ways:

* :func:`fit_for` — the budget for a given model: when to compact, and the
  ceiling nothing may cross.
* :func:`shrink_to_fit` — the deterministic last resort when compaction has
  run and the messages STILL do not fit. It never calls a model (the path
  exists precisely because models are unreachable) and it always terminates
  under the limit.
* :func:`is_context_overflow` — the classifier that keeps this failure out of
  the transient-retry path. Retrying identical oversized messages with backoff
  is five guaranteed failures and ~2 minutes of wall clock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from robothor.engine.context import estimate_tokens
from robothor.engine.reasoning_replay import REASONING_FIELDS

logger = logging.getLogger(__name__)

#: Phrases that mean "these messages do not fit", and nothing else.
#:
#: Deliberately specific. An earlier draft matched on "context", which also
#: appears in "Internal server error while building the context for your
#: request" — a 500 that must keep its backoff, because retrying it works.
#: Every entry here names a LENGTH, a LIMIT or the structural damage
#: truncation does; none of them can be produced by a healthy provider having
#: a bad minute.
OVERFLOW_SIGNATURES: tuple[str, ...] = (
    # Ollama. Reproduced against the real server: the front-truncation that
    # makes an oversized conversation fit can eat every user turn.
    "no user query found in messages",
    # OpenAI-shaped.
    "maximum context length",
    "reduce the length of the messages",
    # Anthropic-shaped.
    "prompt is too long",
    "input length and `max_tokens` exceed context limit",
    # Generic.
    "context length exceeded",
    "context window exceeded",
    "exceeds the context window",
    "too many tokens in the prompt",
)

#: Exception class names that mean overflow whatever their message says.
#: litellm normalises several providers onto ``ContextWindowExceededError``,
#: and a future wording change there must not un-classify this failure.
OVERFLOW_EXCEPTION_NAMES = frozenset({"ContextWindowExceededError"})

#: What replaces a dropped tool result. Short, and honest about what happened —
#: an agent that reads "[tool result dropped …]" can call the tool again; one
#: that reads an empty string concludes the tool returned nothing.
_DROPPED = "[tool result dropped to fit the {model} context window: {chars} chars]"

#: Room held back for the developer note that says what was dropped.
_NOTE_TOKENS = 120

#: Flat token cost of a tool call, mirroring ``context.estimate_tokens`` so the
#: two estimates cannot disagree about the same message list.
_TOOL_CALL_TOKENS = 400


#: Characters per REAL token, by content class, measured with a model's own
#: tokenizer (hostile review of this branch, probe p14; the table is in
#: ``test_dense_content_estimate.py``). Both numbers sit BELOW every measured
#: ratio in their class, so the estimate errs high:
#:
#:   prose 5.5, python source 4.2      -> 3.6 here
#:   csv 2.17, base64 1.36, json 1.32, hex digests 1.13 -> 1.1 here
#:
#: The gap between the two classes is enormous because that is what tokenizers
#: do: a common English word is one token, a line of hex is one token per
#: character or so. ``chars / 4`` splits the difference and is wrong in the
#: dangerous direction for exactly the content a large tool result is made of.
_CHARS_PER_TOKEN_PROSE = 3.6
_CHARS_PER_TOKEN_DENSE = 1.1

#: Two signals, and content is prose only if it passes BOTH. Measured on the
#: text with its LAYOUT removed (see :func:`_chars_per_token`):
#:
#:   content         intra-line ws   digits   real chars/token
#:   english prose        0.164       0.00          5.5
#:   python source        0.079       0.00          4.2
#:   indented json        0.040       0.55          1.47
#:   csv numbers          0.000       0.88          2.17
#:   hex digests          0.000       0.40          1.13
#:   base64               0.000       0.16          1.36
#:
#: Whitespace inside a line is the first signal because it is what a
#: tokenizer's merges are built around; indentation and line breaks are layout
#: and counting them put pretty-printed JSON in the prose class, 63% under what
#: it really costs. Digits are the second, because they are what separates
#: indented CODE — whose lines are words — from an indented numeric PAYLOAD.
#: Both thresholds sit between the two groups with room either side.
_PROSE_WHITESPACE_RATIO = 0.06

#: Digit fraction at or above which content is priced as dense whatever its
#: spacing. Prose with a few figures in it is unaffected; a page of floats
#: wearing indentation is not prose.
_PROSE_MAX_DIGIT_RATIO = 0.15

#: How much of a long string is sampled to classify it. Classification must be
#: O(1) per message: this runs before every call, on conversations that can be
#: megabytes.
_SAMPLE_CHARS = 4096


def _chars_per_token(text: str) -> float:
    """Which density class this text belongs to, from a bounded sample.

    LAYOUT is removed before the ratio is taken — leading whitespace per line
    and the line breaks themselves — because neither is word structure.
    Measured: pretty-printed JSON is 35.8% whitespace and tokenizes at 1.47
    chars/token, and pricing it as prose under-counted a 34KB sample by 59%
    (9,571 estimated against 23,468 real). Nearly all of that whitespace is the
    indent and the many short lines; what is left inside a line is one space
    after each colon. Source code is indented too and stays prose, because its
    lines are full of real word boundaries.
    """
    if len(text) <= _SAMPLE_CHARS:
        sample = text
    else:
        third = _SAMPLE_CHARS // 3
        middle = len(text) // 2
        sample = text[:third] + text[middle : middle + third] + text[-third:]
    sample = "".join(line.lstrip() for line in sample.splitlines())
    if not sample:
        return _CHARS_PER_TOKEN_PROSE
    spaced = sum(1 for character in sample if character.isspace()) / len(sample)
    numeric = sum(1 for character in sample if character.isdigit()) / len(sample)
    if spaced >= _PROSE_WHITESPACE_RATIO and numeric < _PROSE_MAX_DIGIT_RATIO:
        return _CHARS_PER_TOKEN_PROSE
    return _CHARS_PER_TOKEN_DENSE


def _dense_estimate(messages: list[dict[str, Any]]) -> int:
    """A content-aware token estimate: same shape as ``estimate_tokens``, but
    priced per density class instead of at a flat four characters a token."""
    total = 0.0
    for message in messages:
        for field in ("content", *REASONING_FIELDS):
            value = message.get(field)
            text = value if isinstance(value, str) else ""
            if not text and value:
                # Content-block lists and other shapes: fall back to the flat
                # heuristic for that field rather than guessing at its parts.
                total += estimate_tokens([{field: value}])
                continue
            if text:
                total += len(text) / _chars_per_token(text)
        for call in message.get("tool_calls") or []:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            arguments = function.get("arguments") or ""
            total += _TOOL_CALL_TOKENS + len(arguments) / _chars_per_token(arguments)
    return int(total)


def estimate_for(messages: list[dict[str, Any]], model: str | None = None) -> int:
    """The token count a CEILING may be computed from.

    ``estimate_tokens`` is the fleet-wide heuristic and stays exactly as it is
    — cost accounting, dashboards and the compaction trigger all read it. This
    is the same question asked where being wrong sends an over-window request,
    so it answers differently in two ways:

    * ``model`` is passed through, so an operator who turns on
      ``ROBOTHOR_REAL_TOKENIZER_ENABLED`` gets an exact count on the one path
      that needs it. None of the ceiling's call sites passed a model before, so
      that escape hatch could not be reached from here at all.
    * with the exact counter off, the flat ``chars / 4`` is replaced by a
      content-aware estimate and the LARGER of the two is taken, so the old
      number remains a floor and dense content is priced at what it costs.
    """
    from robothor.engine.context import real_tokenizer_enabled

    if model and real_tokenizer_enabled():
        return estimate_tokens(messages, model)
    return max(estimate_tokens(messages), _dense_estimate(messages))


def is_context_overflow(exc: BaseException) -> bool:
    """True when this failure means "the messages do not fit".

    Such a failure is deterministic: the same messages fail the same way
    forever, so it must never reach the transient-retry path. The caller
    shrinks and retries the SAME model once instead.
    """
    for klass in type(exc).__mro__:
        if klass.__name__ in OVERFLOW_EXCEPTION_NAMES:
            return True
    text = str(exc).lower()
    if not text:
        return False
    return any(signature in text for signature in OVERFLOW_SIGNATURES)


@dataclass(frozen=True)
class ContextFit:
    """One model's context budget.

    ``threshold`` is where proactive compaction fires; ``hard_limit`` is the
    ceiling the assembled messages may never cross, because the provider has
    to fit the ANSWER in the same window. The local tier's server enforces
    this by truncating in silence, which is how the incident became an error
    with no mention of length anywhere in it.
    """

    model: str
    window: int
    threshold: int
    reserved_output: int

    @property
    def hard_limit(self) -> int:
        """The most input this model may be sent, answer included."""
        return max(1, self.window - self.reserved_output)


@dataclass
class ShrinkOutcome:
    """What :func:`shrink_to_fit` did. ``note`` is None when it did nothing."""

    messages: list[dict[str, Any]]
    dropped: int
    tokens_before: int
    tokens_after: int
    note: str | None = None
    #: Did it actually get under the ceiling? A shrink that could not — the
    #: protected head alone larger than the budget — must not be reported as
    #: one that did, or the caller spends its one retry on bytes the server has
    #: already refused (hostile review, probe p17).
    fits: bool = True


def next_reachable_model(models: list[str], broken_models: set[str] | None = None) -> str:
    """The model the next call will actually reach.

    Three reasons the chain walk skips a model, and only the first was ever
    consulted here:

    * it is in ``broken_models`` — it failed earlier in THIS run;
    * its circuit breaker is open — it has failed repeatedly and is cooling;
    * every credential in its pool is retired — a spent key, a raised cap not
      yet reloaded, a revoked token. This is the one the incident turned on:
      a spent credential says nothing about a model, so ``_handle_model_error``
      is deliberately not called for it, so the model stays "unbroken" while
      being skipped on every single call.

    Pools are read, never built: :func:`key_pool.pool_if_built` returns what is
    already in the process. Constructing one here would cache it WITHOUT the
    exhaustion alert hook the client attaches, which is how a fix for a silent
    outage becomes a silent outage.

    Falls back to the first model when everything is out — the caller needs a
    window to size against, and the incident is elsewhere by then.
    """
    from robothor.engine.key_pool import env_var_for_model, pool_if_built
    from robothor.engine.model_breaker import get_model_breaker

    broken = broken_models or set()
    breaker = get_model_breaker()
    for model in models:
        if model in broken:
            continue
        try:
            if breaker.is_open(model):
                continue
            var = env_var_for_model(model)
            pool = pool_if_built(var) if var else None
            if pool is not None and pool.exhausted():
                continue
        except Exception as exc:  # noqa: BLE001 — sizing must never break a call
            # The CLASS, never the message. This `try` covers `pool.exhausted()`,
            # which reaches `KeyPool.current()` and the credentials themselves —
            # and an exception raised in there can carry key material in its
            # text. `key_pool` exists because an OpenRouter key reached a log
            # through an exception repr once (2026-08); CodeQL raised this as
            # `py/clear-text-logging-sensitive-data` on PR #582 and was right.
            logger.debug(
                "reachability check for %s failed (%s); assuming reachable",
                model,
                type(exc).__name__,
            )
        return model
    return models[0] if models else ""


def fit_for(model: str) -> ContextFit:
    """The budget for the model that will actually be called.

    Both numbers come from the registry entry for THIS model — the same entry
    ``_build_llm_kwargs`` sends the local server as ``num_ctx``, so the engine
    and the server cannot disagree about how much room there is.
    """
    from robothor.engine.model_registry import get_model_limits, get_output_tokens
    from robothor.engine.run_budget import proactive_compaction_threshold

    limits = get_model_limits(model)
    window = int(limits.max_input_tokens)
    threshold = proactive_compaction_threshold(window)
    reserved = int(get_output_tokens(model, threshold))
    return ContextFit(model=model, window=window, threshold=threshold, reserved_output=reserved)


def _log_overflow(headline: str, model: str, before: int, after: int) -> None:
    """Say what the shrink did, in a scope that holds nothing but numbers.

    The same move ``robothor.secrets.trace`` makes, and for the same reason: a
    log line about message content is only ever allowed to carry counts, and
    the way to prove that is a function with no message, no outcome and no
    conversation in scope to get it wrong with. ``int()`` at the call sites is
    part of the contract — what arrives here is a token count, not a field read
    off an object that also holds the conversation (CodeQL flagged exactly that
    attribute read on PR #582).
    """
    logger.warning(headline, model, before, after)


def _message_tokens(message: dict[str, Any], model: str | None = None) -> int:
    """One message's share of the estimate, priced like :func:`estimate_for`.

    The per-message price and the whole-list price have to come from the same
    arithmetic, or the running budget spends tokens the total does not believe
    in.
    """
    return estimate_for([message], model)


def _last_user_index(messages: list[dict[str, Any]]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return -1


def _drop_tool_results(
    messages: list[dict[str, Any]], *, head: int, keep: int, budget: int, model: str
) -> tuple[int, int]:
    """Replace tool-result CONTENT oldest-first. Returns (tokens, dropped).

    The message itself stays, with its ``tool_call_id``, because a provider
    that sees an assistant tool call whose result vanished rejects the whole
    conversation — the failure this function exists to prevent, arriving by a
    different door.
    """
    total = sum(_message_tokens(m, model) for m in messages)
    dropped = 0
    for index in range(head, len(messages)):
        if total <= budget:
            break
        message = messages[index]
        if index == keep or message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content:
            continue
        replacement = _DROPPED.format(model=model, chars=len(content))
        if len(replacement) >= len(content):
            continue
        before = _message_tokens(message, model)
        message["content"] = replacement
        total += _message_tokens(message, model) - before
        dropped += 1
    return total, dropped


def _drop_tool_exchanges(
    messages: list[dict[str, Any]], *, head: int, keep: int, budget: int, model: str | None = None
) -> tuple[list[dict[str, Any]], int, int]:
    """Remove whole tool exchanges oldest-first: the call AND its results.

    Emptying results is not always enough. A conversation of 200 tool calls
    costs 80,000 tokens in call overhead alone, before a single character of
    content — so the pass that only rewrites content cannot reach the limit
    and would spin. Removing the pair is what makes this terminate.
    """
    total = sum(_message_tokens(m, model) for m in messages)
    doomed: set[int] = set()
    for index in range(head, len(messages)):
        if total <= budget:
            break
        message = messages[index]
        if index == keep or not message.get("tool_calls"):
            continue
        ids = {tc.get("id") for tc in message.get("tool_calls") or []}
        group = [index] + [
            j
            for j in range(index + 1, len(messages))
            if messages[j].get("role") == "tool" and messages[j].get("tool_call_id") in ids
        ]
        for j in group:
            if j == keep:
                continue
            doomed.add(j)
            total -= _message_tokens(messages[j], model)
    if not doomed:
        return messages, total, 0
    return [m for i, m in enumerate(messages) if i not in doomed], total, len(doomed)


#: How many times the last-resort truncation may re-measure and take more off
#: the largest message. Small and bounded: each pass removes the whole excess,
#: so one or two suffice, and a cap means no input shape can spin here.
_TIGHTEN_PASSES = 8

#: Slack the last-resort truncation aims under the budget, so a total that is
#: a token or two over — a marker the allocation could not foresee, floor
#: division in the estimate — is not reported as "could not fit".
_TIGHTEN_SLACK = 32

#: What a truncated message keeps of its tail, so the end of a spec or a
#: question is not lost along with the middle.
_TAIL_CHARS = 200


def _truncated(message: dict[str, Any], allowance: int, model: str | None = None) -> dict[str, Any]:
    """``message`` cut to roughly ``allowance`` tokens, or emptied to a marker.

    The characters-per-token rate is the one this content is actually priced
    at, so a budget of N tokens buys N tokens of base64 and N tokens of prose —
    the flat four-characters-a-token version handed dense content three times
    the room it had paid for.
    """
    content = message.get("content")
    if not isinstance(content, str):
        return message
    chars = max(0, int(allowance * _chars_per_token(content)))
    if len(content) <= chars:
        return message
    if chars <= len(_CUT) + _TAIL_CHARS:
        return {**message, "content": _CUT}
    head = chars - len(_CUT) - _TAIL_CHARS
    return {**message, "content": content[:head] + _CUT + content[-_TAIL_CHARS:]}


#: The marker a truncation leaves behind. Short, because on the path that needs
#: it every character is competing with the operator's question.
_CUT = "\n[…cut to fit the context window…]\n"


def _truncate_to_budget(
    messages: list[dict[str, Any]], *, head: int, budget: int, model: str | None = None
) -> list[dict[str, Any]]:
    """Last resort: the protected head and the final user turn, inside ONE budget.

    Reached only when dropping every tool exchange still leaves the messages
    over the limit — a head or a single turn larger than the whole window.
    Truncating a message is lossy and obvious; sending a conversation the
    server will truncate in silence is lossy and invisible, and that is the
    failure being traded away.

    The allowance is RUNNING, and that is the whole point of this rewrite. The
    first version capped each kept message at ``budget * 4`` characters
    independently, so a protected head of three messages could come back at
    three times the budget — measured at 70,005 tokens against a 57,344
    ceiling, while the note and the log both claimed a reduction (hostile
    review, probe p17). A per-message cap is not a budget.

    The question is served first: whatever else is lost, the model must be able
    to read what it is being asked. The system prompt comes next, then the rest
    of the head in order, each taking only what is still unspent.
    """
    keep = _last_user_index(messages)
    kept = [dict(m) for m in messages[:head]]
    if keep >= head:
        kept.append(dict(messages[keep]))
    if not kept:
        return kept

    # Last user turn first, then the head in order: priority, not position.
    order = [len(kept) - 1, *range(len(kept) - 1)] if len(kept) > 1 else [0]
    remaining = budget
    for index in order:
        cost = _message_tokens(kept[index], model)
        if cost <= remaining:
            remaining -= cost
            continue
        kept[index] = _truncated(kept[index], remaining, model)
        remaining = max(0, remaining - _message_tokens(kept[index], model))

    # Verify, then tighten. Allocation alone can still overshoot: a message cut
    # to its whole remaining allowance leaves nothing for the marker the NEXT
    # one keeps, and a marker is not free. Take the excess off the largest
    # message until the total is inside the budget, or until everything left is
    # a marker and there is genuinely nothing more to give.
    target = max(0, budget - _TIGHTEN_SLACK)
    for _ in range(_TIGHTEN_PASSES):
        total = sum(_message_tokens(m, model) for m in kept)
        if total <= target:
            break
        index = max(range(len(kept)), key=lambda i: _message_tokens(kept[i], model))
        cost = _message_tokens(kept[index], model)
        tightened = _truncated(kept[index], max(0, cost - (total - target)), model)
        if _message_tokens(tightened, model) >= cost:
            break
        kept[index] = tightened
    return kept


def shrink_to_fit(messages: list[dict[str, Any]], fit: ContextFit) -> ShrinkOutcome:
    """Make ``messages`` fit ``fit.hard_limit``, deterministically.

    Never calls a model: this runs when compaction has already been tried, and
    on the path where the reason compaction could not summarise is that every
    cloud model is unreachable. Returns the original list untouched when it
    already fits, so the common case costs one estimate.
    """
    before = estimate_for(messages, fit.model)
    if before <= fit.hard_limit:
        return ShrinkOutcome(messages, 0, before, before, fits=True)

    from robothor.engine.compaction import protected_prefix_len

    working = [dict(m) for m in messages]
    head = protected_prefix_len(working)
    keep = _last_user_index(working)
    # Room for the note the caller appends: a shrink that lands exactly on the
    # limit and is then told about it is over the limit again.
    budget = max(1, fit.hard_limit - _NOTE_TOKENS)

    _total, dropped = _drop_tool_results(
        working, head=head, keep=keep, budget=budget, model=fit.model
    )
    if estimate_for(working, fit.model) > budget:
        working, _total, removed = _drop_tool_exchanges(
            working, head=head, keep=keep, budget=budget, model=fit.model
        )
        dropped += removed
    if estimate_for(working, fit.model) > budget:
        working = _truncate_to_budget(working, head=head, budget=budget, model=fit.model)
        dropped += 1

    after = estimate_for(working, fit.model)
    fits = after <= budget
    if not fits:
        # Said plainly rather than papered over. A note claiming a reduction
        # that did not happen is an evidence line that misleads whoever debugs
        # this next, and the caller spends its one retry on the strength of it.
        logger.error(
            "Context shrink for %s could NOT reach the ceiling: ~%d → ~%d tokens "
            "against a limit of %d — the protected head alone does not fit",
            fit.model,
            before,
            after,
            budget,
        )
        note = (
            f"[SYSTEM] This conversation does not fit {fit.model}'s "
            f"{fit.window}-token context window and could not be cut down to it "
            f"(~{after} tokens against a {budget}-token budget). Expect this model "
            "to refuse or to silently truncate."
        )
        return ShrinkOutcome(working, dropped, before, after, note, fits=False)

    note = (
        f"[SYSTEM] This conversation did not fit {fit.model}'s "
        f"{fit.window}-token context window, so {dropped} older tool result(s) and "
        f"turn(s) were dropped to make room (~{before} → ~{after} tokens). "
        "Anything you still need from earlier, ask for again — do not assume it "
        "is above."
    )
    logger.warning(
        "Context shrink for %s: ~%d → ~%d tokens (limit %d), %d message(s) reduced",
        fit.model,
        before,
        after,
        budget,
        dropped,
    )
    return ShrinkOutcome(working, dropped, before, after, note, fits=True)


#: How many times ONE model may be shrunk and re-asked for a single call.
#: One: the shrink is deterministic and large, so a second overflow on the same
#: model means its real window is not what the registry says — which is the
#: doctor's problem, not something to discover by retrying.
CONTEXT_OVERFLOW_SHRINKS = 1

#: How much of the registry window to trust after the provider has refused it.
#: The estimate is a char/4 heuristic and the server counts real tokens, so a
#: conversation that "fits" and was refused has to come back under a smaller
#: number than the one just proven wrong.
_DISTRUST = 0.75


def _current_run_id() -> str | None:
    """The run whose call is in flight, or None outside a run."""
    from robothor.engine.model_breaker import _current_run_id_var

    return _current_run_id_var.get()


def log_guardrail_event(run_id: str, guardrail_name: str, action: str, **kwargs: Any) -> None:
    """Seam over ``tracking.log_guardrail_event``.

    Imported lazily and wrapped so this module carries no database dependency:
    it sits on the path of every LLM call, including on a box whose database is
    the thing that is broken.
    """
    from robothor.engine import tracking

    tracking.log_guardrail_event(run_id, guardrail_name, action, **kwargs)


def enforce_ceiling(messages: list[dict[str, Any]], fit: ContextFit) -> bool:
    """Put ``messages`` under ``fit``'s ceiling IN PLACE, and say what went.

    The deterministic half of the budget, on a plain message list so that both
    callers can use it: the run loop through ``context_budget`` and the
    pre-flight every dispatch path shares. Returns whether the run was given a
    note — which includes the case where the shrink could not reach the
    ceiling, because an agent whose conversation the server is about to
    truncate needs telling either way.

    Never calls a model. Never raises.
    """
    try:
        outcome = shrink_to_fit(messages, fit)
        if outcome.note is None:
            return False
        from robothor.engine.session import ENGINE_CONTEXT_ROLE

        messages[:] = [*outcome.messages, {"role": ENGINE_CONTEXT_ROLE, "content": outcome.note}]
        return True
    except Exception as exc:  # noqa: BLE001 — a lost ceiling must not lose the run
        # The CLASS, for the same reason as `next_reachable_model`: everything
        # this function touches is built from the conversation, so an
        # exception's text is one `f"...{message}"` away from being a slice of
        # it. What an operator needs here is that the ceiling did not run.
        logger.warning("context ceiling could not be enforced: %s", type(exc).__name__)
        return False


def shrink_after_overflow(messages: list[dict[str, Any]], model: str) -> bool:
    """Make ``messages`` smaller IN PLACE after a provider refused them.

    Returns whether anything changed — False means a retry would send the same
    bytes, so the caller must advance rather than re-ask.

    The budget is deliberately below the registry's: the provider has just
    disproved our arithmetic, and coming back at the same number is how "retry
    once" becomes a second guaranteed failure. The run is told what went, in
    the ``developer`` turn the shrink produces, because an agent that silently
    loses a tool result re-reasons from a hole.
    """
    fit = fit_for(model)
    tightened = ContextFit(
        model=fit.model,
        window=int(fit.window * _DISTRUST),
        threshold=fit.threshold,
        reserved_output=fit.reserved_output,
    )
    outcome = shrink_to_fit(messages, tightened)
    # A TOKEN comparison, not "is there a note": the note was written even when
    # the shrink could not move the total, so the caller decremented its one
    # permitted retry and re-sent bytes the server had already refused
    # (hostile review, probe p17).
    before, after = int(outcome.tokens_before), int(outcome.tokens_after)
    if not outcome.fits or after >= before:
        _log_overflow(
            "Context overflow on %s and nothing could be dropped (~%d → ~%d tokens) — "
            "advancing instead of re-sending the same request",
            model,
            before,
            after,
        )
        return False

    from robothor.engine.session import ENGINE_CONTEXT_ROLE

    messages[:] = [*outcome.messages, {"role": ENGINE_CONTEXT_ROLE, "content": outcome.note}]
    run_id = _current_run_id()
    if run_id:
        # Not an error row and not a model failure: the engine ACTED, and the
        # evidence table is where a control proves it is not inert. Its own
        # action value (migration 124) rather than a borrowed `warned`, because
        # this is not a warning about a policy — it is a record that the engine
        # rewrote a request. An instance that has not migrated yet degrades to
        # `warned` and says so once, in `tracking.log_guardrail_event`.
        try:
            log_guardrail_event(
                run_id,
                "context_overflow",
                "context_overflow",
                reason=f"{model}: ~{before} -> ~{after} tokens",
                mode="enforce",
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never breaks a call
            logger.debug("context_overflow event not recorded: %s", type(exc).__name__)
    _log_overflow(
        "Context overflow on %s — shrank ~%d → ~%d tokens, retrying the same model once",
        model,
        before,
        after,
    )
    return True
