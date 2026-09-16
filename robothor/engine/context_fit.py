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
            logger.debug("reachability check for %s failed (%s); assuming reachable", model, exc)
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


def _message_tokens(message: dict[str, Any], model: str | None = None) -> int:
    """One message's share of the estimate, priced like ``estimate_tokens``.

    The per-message price and the whole-list price have to come from the same
    arithmetic, or the running budget spends tokens the total does not believe
    in.
    """
    return estimate_tokens([message])


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

    """
    content = message.get("content")
    if not isinstance(content, str):
        return message
    chars = max(0, allowance * 4)
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
    before = estimate_tokens(messages)
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
    if estimate_tokens(working) > budget:
        working, _total, removed = _drop_tool_exchanges(
            working, head=head, keep=keep, budget=budget
        )
        dropped += removed
    if estimate_tokens(working) > budget:
        working = _truncate_to_budget(working, head=head, budget=budget)
        dropped += 1

    after = estimate_tokens(working)
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
    if not outcome.fits or outcome.tokens_after >= outcome.tokens_before:
        logger.warning(
            "Context overflow on %s and nothing could be dropped (~%d → ~%d tokens) — "
            "advancing instead of re-sending the same request",
            model,
            outcome.tokens_before,
            outcome.tokens_after,
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
                reason=f"{model}: ~{outcome.tokens_before} -> ~{outcome.tokens_after} tokens",
                mode="enforce",
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never breaks a call
            logger.debug("context_overflow event not recorded: %s", exc)
    logger.warning(
        "Context overflow on %s — shrank to ~%d tokens, retrying the same model once",
        model,
        outcome.tokens_after,
    )
    return True
