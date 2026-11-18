"""Fold elided tool results into one deterministic ledger instead of dropping them.

Elision replaces an old tool result with a small stub: what was called, a
bounded excerpt, and how to fetch it again. A long tool-heavy turn can still
outgrow its budget on stubs alone, because every exchange carries its call as
well (the estimate prices a call at 400 tokens). Measured in a lab run on a
128k-window model with a production-sized system prompt: thirty reads of stubs
crossed the trigger by themselves, and the last-resort drain dropped every one
of them, the excerpt holding the fact the final answer needed included.

This module is the alternative to dropping. The stub exchanges outside the
protected tail are replaced, as whole exchanges so every call keeps its result,
by one message: one line per call naming the tool, its
arguments and its call id, with the excerpt, under a single instruction to call
again before relying on what is not shown. No model call, so a summarizer that
extracts nothing cannot lose it. When even the ledger does not fit, excerpts are
cut oldest first; the tool name, the arguments and the instruction stay.

A rewrite of history, so it runs only when the context is still over its drain
target after every cheaper step; between folds the prefix is stable.

Trust: the excerpts are tool output, which can carry injected instructions. The
ledger is a ``user`` turn, like the compaction summary, never an engine-context
turn (those are labeled ``[engine]`` and followed as platform context), and its
entries sit inside ``<untrusted_content>``, the tag tool output from outside
sources already carries. It is recognised by an engine-private key that is
stripped before every provider call, never by its text, so nobody can make a
message of their own count as the ledger by typing its heading.
"""

from __future__ import annotations

from typing import Any

LEDGER_MARKER = "[Earlier tool results, removed to save space]"
#: The engine-private key that marks the ledger (``_``-prefixed, so
#: ``context_control.strip_engine_keys`` removes it from every payload).
LEDGER_KEY = "_compaction_ledger"
_UNTRUSTED_OPEN = '<untrusted_content source="earlier tool results">'
_UNTRUSTED_CLOSE = "</untrusted_content>"
LEDGER_INSTRUCTION = (
    " Each line names a call and keeps an excerpt of what it returned. Before "
    "relying on a detail not shown, call that tool again with the same arguments, "
    "if it only reads data."
)
_ENTRY_PREFIX = "- "
_EXCERPT_SEP = " :: "


def is_ledger(message: dict[str, Any]) -> bool:
    """Only a message the engine made: the private key, never the text."""
    return message.get(LEDGER_KEY) is True and message.get("role") == "user"


def _entries_of(ledger: dict[str, Any]) -> list[str]:
    lines = str(ledger.get("content") or "").split("\n")[1:]
    return [line for line in lines if line.startswith(_ENTRY_PREFIX)]


def _ledger(entries: list[str]) -> dict[str, Any]:
    body = "\n".join([_UNTRUSTED_OPEN, *entries, _UNTRUSTED_CLOSE])
    return {
        "role": "user",
        "content": LEDGER_MARKER + LEDGER_INSTRUCTION + "\n" + body,
        LEDGER_KEY: True,
    }


def _stub_exchange_spans(
    messages: list[dict[str, Any]], start: int, stop: int
) -> list[tuple[int, int]]:
    """``[i, j)`` spans of whole exchanges in ``messages[start:stop]`` whose
    every result is already an elision stub."""
    from robothor.engine.compaction import STUB_PREFIX

    spans: list[tuple[int, int]] = []
    i = start
    while i < stop:
        calls = messages[i].get("tool_calls") if messages[i].get("role") == "assistant" else None
        if not calls:
            i += 1
            continue
        j = i + 1
        while j < stop and messages[j].get("role") == "tool":
            j += 1
        results = messages[i + 1 : j]
        ids = {str(c.get("id")) for c in calls if isinstance(c, dict)}
        if (
            results
            and ids == {str(r.get("tool_call_id")) for r in results}
            and all(str(r.get("content") or "").startswith(STUB_PREFIX) for r in results)
        ):
            spans.append((i, j))
        i = j
    return spans


def _entries_for(exchange: list[dict[str, Any]]) -> list[str]:
    from robothor.engine.compaction import EXCERPT_MARKER, _call_details

    details = _call_details(exchange)
    said = str(exchange[0].get("content") or "").strip().replace("\n", " ")[:200]
    entries: list[str] = []
    for result in exchange[1:]:
        call_id = str(result.get("tool_call_id"))
        name, args = details.get(call_id, ("the tool", "{}"))
        args = args if len(args) <= 120 else args[:120] + "..."
        _, _, excerpt = str(result.get("content") or "").partition(EXCERPT_MARKER)
        line = f"{_ENTRY_PREFIX}{name} {args} (ref {call_id})"
        if said:
            line += f" [said: {said}]"
        # The excerpt cannot close the untrusted block it is quoted inside.
        quoted = excerpt.replace("\n", " ").replace("untrusted_content", "untrusted-content")
        entries.append(line + _EXCERPT_SEP + quoted)
    return entries


def fold_stub_exchanges(
    messages: list[dict[str, Any]],
    head_len: int,
    protect_tail: int,
    target: int,
    model: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Replace stub exchanges outside the head and tail with one ledger.

    Returns the messages and how many exchanges were folded. An existing ledger
    absorbs the new entries, in order, so there is only ever one. Excerpts are
    cut oldest first only while the result is still over *target*. Synchronous
    and CPU-bound with the exact tokenizer on: callers run it in a thread.
    """
    from robothor.engine.context import estimate_context_tokens

    stop = max(head_len, len(messages) - protect_tail)
    spans = _stub_exchange_spans(messages, head_len, stop)
    if not spans:
        return messages, 0
    inside = {k for i, j in spans for k in range(i, j)}
    # From index 1, not the head: a ledger written at the head's edge is
    # counted into the protected prefix by the next compaction.
    old = next((k for k in range(1, stop) if is_ledger(messages[k])), None)
    entries = _entries_of(messages[old]) if old is not None else []
    for i, j in spans:
        entries.extend(_entries_for(messages[i:j]))
    at = old if old is not None else spans[0][0]
    kept = [(k, m) for k, m in enumerate(messages) if k not in inside and k != old]
    before = [m for k, m in kept if k < at]
    after = [m for k, m in kept if k > at]

    # Cut excerpts, oldest first, only while still over the target.
    others = estimate_context_tokens([*before, *after], model)
    for index, entry in enumerate(entries):
        if others + estimate_context_tokens([_ledger(entries)], model) <= target:
            break
        head, sep, _excerpt = entry.partition(_EXCERPT_SEP)
        entries[index] = head + sep if sep else head
    return [*before, _ledger(entries), *after], len(spans)
