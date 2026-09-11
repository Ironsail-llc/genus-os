"""Thinking-mode reasoning must round-trip back to the model that produced it.

A reasoning model does not treat its own reasoning as decoration: DeepSeek's
thinking mode rejects a conversation whose assistant turns come back without
the ``reasoning_content`` it emitted —

    The reasoning_content in the thinking mode must be passed back to the API.

which OpenRouter surfaces as a bare 400 "Provider returned error". Single-turn
calls are unaffected (there is no history to replay), so the failure looks like
a provider outage and takes the whole fallback chain with it.

Two rules, and they pull in opposite directions:

1. Keep the field on the stored assistant message exactly as received, so the
   next request to the SAME model carries it.
2. Never send it anywhere else. The blob is provider-specific; an
   Anthropic-shaped provider rejects it. So the producing model is recorded on
   the stored message under ``_model`` and the reasoning is stripped whenever
   the history is replayed to a different one.

``_model`` is engine bookkeeping and is removed from every outbound payload.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

#: Fields a reasoning provider puts on the assistant message. ``reasoning_content``
#: is litellm's normalized name (``litellm.types.utils.Message``); ``reasoning`` and
#: ``reasoning_details`` are what OpenRouter itself returns and documents as
#: needing to be echoed back. litellm keeps unknown fields on the Message object,
#: so all three are readable the same way.
REASONING_FIELDS: Final[tuple[str, ...]] = (
    "reasoning_content",
    "reasoning",
    "reasoning_details",
)

#: Where the stored assistant message records which model produced it. Leading
#: underscore: this key never reaches a provider.
PRODUCER_MODEL_KEY: Final = "_model"


#: OpenRouter routing hints, not different models: ``…-v4-flash:free`` and
#: ``…-v4-flash`` are one model reached two ways. Local Ollama tags use the same
#: colon syntax for genuinely different weights (``qwen3:8b`` vs ``qwen3:32b``),
#: so the suffix is only stripped for non-local ids.
_VARIANT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {"free", "nitro", "floor", "extended", "online", "thinking"}
)

#: Model ids whose colon segment is a weight tag, never a routing variant.
_LOCAL_PREFIXES: Final[tuple[str, ...]] = ("ollama_chat/", "ollama/")


def _normalize_model_id(model: str) -> str:
    """Collapse a model id to a provider/format-agnostic core for comparison.

    litellm reports ``response.model`` without the ``openrouter/`` prefix and
    often with a trailing date or dashes-for-dots, so an exact compare against
    the id the engine dispatched on would never match and the reasoning would
    be stripped on every turn — i.e. the bug this module exists to fix.

    The imprecision is deliberate and worth naming: this collapses the provider
    prefix and any dated snapshot, so two models that differ ONLY by routing
    prefix or release date compare equal (``bedrock/anthropic.claude-opus-4-7``
    == ``claude-opus-4-7-20260416``). That is the right trade for both callers —
    "did the primary answer?" and "may this reasoning go back?" — but it means
    this is not an identity function: never use it to pick a model, price one,
    or key a cache.
    """
    raw = (model or "").strip().lower()
    core = raw.rsplit("/", 1)[-1]
    if not raw.startswith(_LOCAL_PREFIXES):
        head, sep, tail = core.rpartition(":")
        if sep and head and tail in _VARIANT_SUFFIXES:
            core = head
    core = re.sub(r"[-_]?\d{6,}$", "", core)  # trailing date/build stamp
    return re.sub(r"[.\-_\s]", "", core)


def same_model(a: str, b: str) -> bool:
    """True when two model ids name the same model across routing spellings."""
    normalized = _normalize_model_id(a)
    return bool(normalized) and normalized == _normalize_model_id(b)


def merge_streamed_reasoning_details(response: Any, details: list[Any]) -> None:
    """Put streamed ``reasoning_details`` back on a rebuilt message, in place.

    litellm's ``stream_chunk_builder`` combines ``reasoning_content`` and drops
    every other reasoning field (probed on 1.97.0), so without this the
    interactive path replays strictly fewer fields than a scheduled run of the
    same agent against the same model.

    Best-effort by design: a rebuilt response the engine cannot annotate is
    still a complete answer, and losing the run over a bookkeeping field would
    be worse than losing the field.
    """
    if not details:
        return
    try:
        message = response.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return
    try:
        message.reasoning_details = details
    except (AttributeError, TypeError, ValueError):
        return


def capture_reasoning_fields(message: Any) -> dict[str, Any]:
    """The reasoning fields a provider returned on one assistant message.

    Values are taken verbatim — a reasoning blob is the provider's, and any
    reshaping here is a 400 on the next turn. Only ``str``/``list`` values are
    accepted so a stubbed or unusual response object cannot smuggle an
    unserializable object into the conversation.

    A value already captured under another name is not captured twice: litellm
    normalizes OpenRouter's ``reasoning`` onto ``reasoning_content`` and leaves
    the original in ``provider_specific_fields``, and shipping the same blob
    under both names would pay for it twice on every turn of every run.
    """
    captured: dict[str, Any] = {}
    provider_fields = getattr(message, "provider_specific_fields", None)
    for field in REASONING_FIELDS:
        value = getattr(message, field, None)
        if value is None and isinstance(provider_fields, dict):
            value = provider_fields.get(field)
        if not value or not isinstance(value, (str, list)):
            continue
        if any(value == already for already in captured.values()):
            continue
        captured[field] = value
    return captured


def strip_reasoning_for_model(messages: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    """Messages safe to send to ``model``.

    Drops the ``_model`` bookkeeping key from every message that carries it,
    and with it the reasoning fields whenever that message was produced by a
    different model. Returns the same list object when there is nothing to
    change, so ordinary payloads stay byte-identical, and never mutates the
    caller's messages — the history stays intact for the next leg of the
    fallback chain.
    """
    if not any(isinstance(m, dict) and PRODUCER_MODEL_KEY in m for m in messages):
        return messages

    cleaned: list[dict[str, Any]] = []
    for message in messages:
        producer = message.get(PRODUCER_MODEL_KEY) if isinstance(message, dict) else None
        if producer is None:
            cleaned.append(message)
            continue
        keep_reasoning = same_model(str(producer), model)
        cleaned.append(
            {
                key: value
                for key, value in message.items()
                if key != PRODUCER_MODEL_KEY and (keep_reasoning or key not in REASONING_FIELDS)
            }
        )
    return cleaned


#: How many messages a rejection digest may describe. A capped run's history is
#: thousands of turns; the tail is where the rejected shape lives.
HISTORY_DIGEST_MAX_TURNS: Final = 60


def _field_shape(value: Any) -> Any:
    """The size of a reasoning field, never its content."""
    if isinstance(value, str):
        return {"chars": len(value)}
    if isinstance(value, list):
        return {"items": len(value), "chars": len(json.dumps(value, default=str))}
    return {"type": type(value).__name__}


def redacted_history_digest(
    messages: list[dict[str, Any]],
    model: str,
    *,
    limit: int = HISTORY_DIGEST_MAX_TURNS,
) -> str:
    """A one-line JSON sketch of a rejected history — shapes only, no content.

    Live replay of every obvious variant against OpenRouter/DeepSeek returned
    200, so the shape that actually 400s is one nobody has reproduced: most
    likely a history where some assistant turns kept their reasoning and others
    lost it (persistence, compaction, hygiene), or reasoning that crossed models
    on a fallback. Only the rejected conversation itself can say which, and it
    is made of the operator's mail and CRM — so this records per turn: its index,
    role, whether content is empty, how many tool calls it carries, which
    reasoning keys are present **and how large**, and which model produced it.
    No message text, ever.

    Indices are true positions in the history, so a capped digest still says
    where in the conversation each turn sat.
    """
    turns: list[dict[str, Any]] = []
    start = max(0, len(messages) - limit)
    for index, message in enumerate(messages[start:], start=start):
        if not isinstance(message, dict):
            turns.append({"i": index, "role": "?", "type": type(message).__name__})
            continue
        content = message.get("content")
        turn: dict[str, Any] = {
            "i": index,
            "role": str(message.get("role", "")),
            "content_empty": not content,
            "tool_calls": len(message.get("tool_calls") or []),
        }
        for field in REASONING_FIELDS:
            if field in message and message[field] is not None:
                turn[field] = _field_shape(message[field])
        producer = message.get(PRODUCER_MODEL_KEY)
        if producer is not None:
            turn[PRODUCER_MODEL_KEY] = str(producer)
        turns.append(turn)

    digest: dict[str, Any] = {
        "target_model": model,
        "messages": len(messages),
        "turns": turns,
    }
    if start:
        digest["omitted_head"] = start
    return json.dumps({"reasoning_replay_history": digest}, default=str)


#: The provider's own wording for a history replayed without its reasoning.
#: Matched on the phrase rather than a status code: OpenRouter reports it as a
#: generic 400 "Provider returned error" with the real message in the raw body.
_REPLAY_REJECTION = re.compile(
    r"reasoning(?:_content|_details)?.{0,120}?must be passed back",
    re.IGNORECASE | re.DOTALL,
)


def is_reasoning_replay_error(error: BaseException) -> bool:
    """True when a provider rejected the request for missing reasoning.

    Walks the exception chain: litellm wraps the provider's message, and the
    phrase can arrive on a cause rather than the exception the caller sees.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for text in (str(current), str(getattr(current, "message", "") or "")):
            if _REPLAY_REJECTION.search(text):
                return True
        current = current.__cause__ or current.__context__
    return False
