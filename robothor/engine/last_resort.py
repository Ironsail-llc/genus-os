"""What the operator is told when every model has failed, and the try before it.

``RuntimeError("All models failed to respond")`` reached the operator's chat
verbatim on 2026-09-16, as ``Failure: error. All models failed to respond.`` It
is true, and it is useless: it does not say which providers failed, why, or
whether the local model — the entire reason a last fallback exists — was even
reachable. On that box it WAS reachable and had answered twelve steps of the
same run; the chain gave up because the conversation no longer fit it.

Two things live here:

* :func:`last_resort_attempt` — one final, minimal call. The protected head
  plus the last user turn, nothing else, against a local model the server says
  it has. A conversation too long for the window is the commonest reason a
  chain ends with nothing, and the shortest possible conversation is the one
  request that can still work.
* :func:`all_models_failed_error` — the sentence, in place of the repr. It
  names each provider and why it is out, and says whether the local tier
  answered, was unreachable, or was never configured.

Nothing here retries the cloud. Every model in the chain has just been tried,
and the point of a last resort is that it is DIFFERENT from what already
failed.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from robothor.engine.llm_budgets import is_local_model

logger = logging.getLogger(__name__)

#: What the last-resort attempt found, for the sentence built afterwards.
#: A ContextVar rather than a return value because the attempt happens at the
#: dispatch chokepoint (so every caller gets it) and the sentence is composed
#: by the runner two frames up — threading a second return value through both
#: would change a signature a long tail of tests patches.
_local_state: ContextVar[str] = ContextVar("last_resort_local_state", default="unknown")

#: How long the local server has to list its models. It is a loopback GET of a
#: list the server holds in memory; anything slower is a server that will not
#: answer a generation request either, and the run has already spent minutes.
TAGS_TIMEOUT_S = 3.0

#: What the operator is told to do about each way a provider can be out. The
#: remedy is the point — a page that says "exhausted" makes them go and find
#: out which kind (provider_alerts learned this the same way).
_REASONS = {
    "spent": (
        "every credential is retired — the quota window or the balance is "
        "spent. Top up or raise the limit, then run `genus secrets reload`"
    ),
    "breaker": "it has failed repeatedly and is in cooldown",
    "broken": "it failed earlier in this run",
    "tried": "it was tried and did not answer",
}


class AllModelsFailedError(RuntimeError):
    """Every model failed, and the message is for the OPERATOR to read.

    A ``RuntimeError`` subclass on purpose: the runner's callers, the workflow
    engine and several tests catch ``RuntimeError`` around a run, and a new
    base class would have changed who handles this. What changes is the
    ``str()``, which is what the delivery layer puts in front of a person.
    """


def local_state() -> str:
    """What the last-resort attempt on this task found. See :data:`_local_state`."""
    return _local_state.get()


def _reason_for(model: str, broken_models: set[str]) -> str:
    """Why this model is out, in the vocabulary of :data:`_REASONS`."""
    from robothor.engine.key_pool import env_var_for_model, pool_if_built

    if model in broken_models:
        return "broken"
    try:
        from robothor.engine.model_breaker import get_model_breaker

        if get_model_breaker().is_open(model):
            return "breaker"
        var = env_var_for_model(model)
        pool = pool_if_built(var) if var else None
        if pool is not None and pool.exhausted():
            return "spent"
    except Exception as exc:  # noqa: BLE001 — the message must always be built
        logger.debug("could not classify %s (%s)", model, exc)
    return "tried"


def failure_lines(models: list[str], broken_models: set[str] | None = None) -> list[str]:
    """One line per model: what it is and why it did not answer."""
    broken = broken_models or set()
    return [f"{model} — {_REASONS[_reason_for(model, broken)]}" for model in models]


def all_models_failed(
    session: Any, models: list[str], broken_models: set[str] | None = None
) -> AllModelsFailedError:
    """Record the exhaustion on the run and return what the operator reads.

    Returned rather than raised so the call site is one ``raise`` — the run
    loop is at its decomposition cap, and a helper that raises reads as though
    control might come back from it.
    """
    with_error = getattr(session, "record_error", None)
    if with_error is not None:
        with_error("All models failed")
    return all_models_failed_error(models, broken_models)


def all_models_failed_error(
    models: list[str],
    broken_models: set[str] | None = None,
    *,
    local_state: str | None = None,
) -> AllModelsFailedError:
    """The exception whose text an operator can act on.

    ``local_state`` is one of ``answered_none`` (a local model was reachable
    and still could not answer), ``unreachable`` (configured, server did not
    list it), ``absent`` (no local model in this agent's chain) or ``unknown``.
    It defaults to whatever the last-resort attempt on this task found.
    """
    local = {
        "answered_none": (
            "The local model was reachable and was given one last minimal "
            "attempt, which also failed."
        ),
        "unreachable": (
            "The local fallback is configured but its server did not answer — "
            "check `genus doctor --only models.local_fallback_ready`."
        ),
        "absent": (
            "This agent has no local fallback configured, so there was nothing left to try."
        ),
    }.get(local_state if local_state is not None else _local_state.get(), "")
    why = "; ".join(failure_lines(models, broken_models)) or "no model was configured"
    return AllModelsFailedError(
        " ".join(
            filter(
                None,
                [
                    "I could not get an answer from any model, so this run "
                    f"stopped without doing the work. {why}.",
                    local,
                ],
            )
        )
    )


async def list_local_models(base_url: str) -> set[str]:
    """Model names the local server says it has. Empty set on any failure.

    ``/api/tags`` is the cheapest question that distinguishes "the fallback is
    down" from "the fallback refused this conversation" — two failures with
    opposite remedies that look identical in a chain-exhaustion log.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=TAGS_TIMEOUT_S) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/tags")
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 — an unreachable server is an answer
        logger.warning("local model server did not list its models: %s", exc)
        return set()
    return {str(entry.get("name", "")) for entry in payload.get("models") or []}


def local_base_url() -> str:
    """Where the local server answers, from settings — never from the chain."""
    from robothor.settings import get_settings

    return str(get_settings().ollama.base_url or "")


def bare_model_name(model: str) -> str:
    """``ollama_chat/qwen3.8:27b`` → ``qwen3.8:27b``, the name the server uses."""
    return model.split("/", 1)[1] if "/" in model else model


async def reachable_local_model(models: list[str]) -> str | None:
    """The first local model in the chain the server actually carries.

    Both halves matter. A chain naming a model the server never pulled is the
    2026-08-24 defect (requests went to a model that did not exist), and a
    server that is down is the other half — neither is worth one more 300s
    generation timeout to discover.
    """
    candidates = [model for model in models if is_local_model(model)]
    if not candidates:
        return None
    available = await list_local_models(local_base_url())
    if not available:
        return None
    for model in candidates:
        name = bare_model_name(model)
        if any(name == have or have.startswith(f"{name}:") for have in available):
            return model
    logger.warning("the local server carries none of this chain's local models")
    return None


def minimal_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The smallest conversation that is still the operator's question.

    The protected head (the system prompt and the task statement) plus the last
    user turn. Tool traffic is what grew the context past the window, and none
    of it is needed to say something true to the person waiting.
    """
    from robothor.engine.compaction import protected_prefix_len

    if not messages:
        return []
    head = protected_prefix_len(messages)
    minimal = list(messages[:head])
    for message in reversed(messages):
        if message.get("role") == "user":
            if message not in minimal:
                minimal.append(message)
            break
    if not any(message.get("role") == "user" for message in minimal):
        # A chain that ends without a user turn is exactly what Ollama refuses.
        minimal.append({"role": "user", "content": "Continue and answer now."})
    return minimal


async def last_resort_attempt(client: Any, session: Any, models: list[str]) -> Any:
    """One minimal call against a reachable local model, or None.

    Deliberately NOT streamed and deliberately without tools: this runs after
    the whole chain has failed, and the only goal left is a sentence the
    operator can read. Never raises — a failure here is the failure the caller
    is already handling.
    """
    from robothor.engine.llm_attempts import attempts_recorded

    try:
        from robothor.engine.run_context import in_benchmark_run

        if in_benchmark_run():
            # A graded child is not the operator's chat. The whole point of
            # this attempt is that a person is waiting for a sentence; a
            # benchmark case has a grader, and N failing cases at once would
            # queue N generations on a tier that serves a few at a time.
            _local_state.set("absent")
            return None
        if not any(is_local_model(model) for model in models):
            _local_state.set("absent")
            return None
        model = await reachable_local_model(models)
        if model is None:
            _local_state.set("unreachable")
            return None
        logger.warning(
            "Every model failed — one last minimal attempt on the local tier (%s)", model
        )
        before = attempts_recorded()
        # `ignore_breaker`: the local model's own breaker opens after three
        # consecutive failures and stays open for ten minutes, and the chain
        # walk honours it — so with the server healthy and the breaker open
        # this control made ZERO calls while logging, and telling the operator,
        # that it had tried (hostile review of this branch, probe p15). A
        # cooldown says what to try NEXT; here there is no next.
        response = await client._call_llm(
            minimal_messages(session.messages),
            [model],
            [],
            broken_models=set(),
            ignore_breaker=True,
        )
        after = attempts_recorded()
        # Derived from what HAPPENED, never from having reached this line: an
        # attempt that was skipped and an attempt that failed are different
        # facts with different remedies, and the operator reads the difference.
        dialled = before is None or (after is not None and after > before)
        if not dialled:
            logger.warning("the local tier was not dialled at all — reporting it unreachable")
            _local_state.set("unreachable")
            return response
        _local_state.set("answered" if response is not None else "answered_none")
        return response
    except Exception as exc:  # noqa: BLE001
        logger.warning("the last-resort local attempt failed too: %s", exc)
        _local_state.set("answered_none")
        return None
