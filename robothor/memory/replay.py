"""Replay real recall questions through two recall configurations, side by side.

The dry run of the memory design: before a change to recall goes live, run the
questions agents and people actually asked through today's recall and the new
recall, and report per question what each returned and how they differ, with
latency and context size. It writes nothing.

Questions come from one tenant, in order of preference:

* ``questions_from_runs`` — the ``query`` of every ``search_memory`` tool call
  the engine persisted in ``agent_run_steps.tool_input`` (direct, or wrapped in
  ``tool_call``), read as a person only when the run served that person one
  to one (:func:`run_private_reader`), otherwise as no reader;
* ``questions_from_chat`` — recent user turns, each read as the owner of its
  conversation (``segments.session_scope``); a turn whose conversation has no
  known owner is skipped, never replayed as an unscoped reader;
* ``questions_from_file`` — one question per line, with an optional reader.

Nothing in the platform records the query text of automatic (warm-up) recall
or of a search made outside a tool call: ``fact_access_log`` links runs to
facts without the question, and structured logs are not queryable here.

Read-only by construction and by enforcement: ``search_facts`` is called
directly (the tool handler's access recording, access log and plugin merge
are not on this path), and every connection is checked out inside
``read_only_scope``, so a write that slipped in would raise
``ReadOnlySqlTransaction``. Embedding a question is a model call, not a write.

The configurations are :data:`PRESETS`, the one place to add another.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from robothor.db.connection import get_connection, read_only_scope, tenant_scope
from robothor.memory.block_budget import estimate_tokens

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence
    from pathlib import Path

    Search = Callable[..., Awaitable[list[dict[str, Any]]]]

logger = logging.getLogger(__name__)

NOTICE = (
    "NOTICE: this report contains tenant memory (recalled facts and conversation); "
    "handle it as you would that data. --redact replaces the text with ids and lengths."
)

#: Results per question when the question does not say (the handler's default).
DEFAULT_RESULTS = 10
#: The most a replayed call may ask for, whatever its recorded ``limit``.
MAX_RESULTS = 50
#: Rows read from a source before de-duplication and sampling.
SOURCE_POOL = 2000
#: Longest question replayed; a pasted document is cut here.
MAX_QUESTION_CHARS = 2000
EXCERPT_CHARS = 160

ORDER_NOTE = (
    "alternating: even-numbered questions run the first configuration first, "
    "odd-numbered the second, so cache warming favours neither side"
)


# --------------------------------------------------------------------------- #
# Questions and readers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Reader:
    """Who a question is read as: what ``search_memory`` derives from the caller."""

    person_id: str | None = None
    user_id: str | None = None
    room_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "user_id": self.user_id,
            "room_ids": list(self.room_ids),
        }


@dataclass(frozen=True)
class Question:
    text: str
    reader: Reader = field(default_factory=Reader)
    limit: int = DEFAULT_RESULTS
    origin: dict[str, Any] = field(default_factory=dict, compare=False)

    def identity(self) -> tuple[Any, ...]:
        return (self.text, self.reader.person_id, self.reader.user_id, self.reader.room_ids)


# --------------------------------------------------------------------------- #
# Presets: the configurations a replay compares
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    #: ``search_facts`` include_segments.
    segments: bool
    #: ``search_facts`` include_graph.
    graph: bool


PRESETS: dict[str, Preset] = {
    p.name: p
    for p in (
        Preset(
            "today",
            "recall as main serves search_memory: dense + keyword legs, reranked head, "
            "one-hop entity expansion, insights and episodes; no segments, no graph",
            segments=False,
            graph=False,
        ),
        Preset(
            "new",
            "the product default on this tree: today's legs plus conversation segments "
            "and the graph-activation leg",
            segments=True,
            graph=True,
        ),
        Preset("new-graph", "new without the graph-activation leg", segments=True, graph=False),
        Preset("new-segments", "new without conversation segments", segments=False, graph=True),
    )
}

DEFAULT_COMPARE: tuple[str, str] = ("today", "new")


def preset_kwargs(preset: Preset, *, fan_out: bool) -> dict[str, Any]:
    """The ``search_facts`` switches of a preset.

    ``fan_out`` is the handler's: on unless ``MEMORY_NARROW_SEARCH`` is set, and
    the same on both sides, so a replay compares legs, not that flag.
    """
    return {
        "include_segments": preset.segments,
        "include_graph": preset.graph,
        "expand_entities": fan_out,
        "include_insights": fan_out,
        "include_episodes": fan_out,
    }


def resolve_compare(spec: str | None) -> tuple[str, str]:
    """``"a,b"`` to two distinct preset names; None is :data:`DEFAULT_COMPARE`."""
    if spec is None:
        return DEFAULT_COMPARE
    names = [n.strip() for n in spec.split(",") if n.strip()]
    if len(names) != 2 or names[0] == names[1]:
        raise ValueError(f"--compare takes two different presets, A,B (got {spec!r})")
    unknown = [n for n in names if n not in PRESETS]
    if unknown:
        raise ValueError(f"unknown preset(s) {unknown}; available: {', '.join(PRESETS)}")
    return names[0], names[1]


def default_search() -> Search:
    from robothor.memory.facts import search_facts

    return search_facts


def _fan_out() -> bool:
    from robothor.engine.feature_flags import narrow_memory_search_enabled

    return not narrow_memory_search_enabled()


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def require_tenant(tenant_id: str) -> None:
    """Refuse an empty or unknown tenant: a replay never falls back to a default."""
    if not (tenant_id or "").strip():
        raise LookupError("a tenant is required (--tenant)")
    from robothor.crm.dal import get_tenant

    with read_only_scope(), tenant_scope(tenant_id):
        if get_tenant(tenant_id) is None:
            raise LookupError(f"unknown tenant {tenant_id!r}")


_SINCE = re.compile(r"^(\d+)([dhm])$")


def parse_since(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """``7d`` / ``12h`` / ``30m`` before now, or an ISO date or timestamp (UTC if naive)."""
    if value is None:
        return None
    now = now or datetime.now(UTC)
    m = _SINCE.match(value.strip())
    if m:
        unit = {"d": "days", "h": "hours", "m": "minutes"}[m.group(2)]
        return now - timedelta(**{unit: int(m.group(1))})
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"--since takes 7d, 12h, 30m or an ISO date (got {value!r})") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def pick_questions(
    items: Sequence[Question], *, limit: int, sample: bool, seed: int
) -> list[Question]:
    """One per (text, reader), then the first ``limit`` or a seeded sample of them."""
    seen: set[tuple[Any, ...]] = set()
    unique: list[Question] = []
    for q in items:
        if q.identity() not in seen:
            seen.add(q.identity())
            unique.append(q)
    if not sample:
        return unique[: max(0, limit)]
    ordered = sorted(unique, key=lambda q: tuple(str(x) for x in q.identity()))
    return random.Random(seed).sample(ordered, min(max(0, limit), len(ordered)))  # noqa: S311


def _clean(text: Any) -> str:
    return str(text or "").strip()[:MAX_QUESTION_CHARS]


def questions_from_file(
    path: str | Path,
    *,
    reader: Reader | None = None,
    limit: int = 10_000,
    sample: bool = False,
    seed: int = 1,
) -> list[Question]:
    """One question per line; blank lines and ``#`` comments are skipped."""
    from pathlib import Path as _Path

    out: list[Question] = []
    for n, line in enumerate(_Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        text = _clean(line)
        if text and not text.startswith("#"):
            out.append(
                Question(text, reader=reader or Reader(), origin={"source": "file", "line": n})
            )
    return pick_questions(out, limit=limit, sample=sample, seed=seed)


def reader_for(tenant_id: str, *, person_id: Any = None, user_id: Any = None) -> Reader:
    """A reader with the workspaces its user belongs to, as the handler derives them."""
    user = str(user_id) if user_id else None
    return Reader(
        person_id=str(person_id) if person_id else None,
        user_id=user,
        room_ids=_rooms_for(tenant_id, user) if user else (),
    )


def _rooms_for(tenant_id: str, user_id: str) -> tuple[str, ...]:
    try:
        from robothor.rooms import dal as rooms_dal

        with read_only_scope(), tenant_scope(tenant_id):
            return tuple(
                sorted(str(s.id) for s in rooms_dal.list_rooms_for_user(tenant_id, user_id))
            )
    except Exception as exc:  # noqa: BLE001 - fewer rooms, never more
        logger.warning("replay: workspace lookup failed (%s); none searched", type(exc).__name__)
        return ()


def search_arguments(tool_name: str | None, tool_input: Any) -> dict[str, Any] | None:
    """The arguments of a persisted step if it was a ``search_memory`` call, else None."""
    from robothor.engine.wrapped_call import inner_call

    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except ValueError:
            return None
    name, args = inner_call(
        str(tool_name or ""), tool_input if isinstance(tool_input, dict) else {}
    )
    return dict(args) if name == "search_memory" else None


def _result_limit(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RESULTS
    return min(max(n, 1), MAX_RESULTS)


def run_private_reader(
    *,
    trigger_type: str | None,
    trigger_detail: str | None,
    user_id: str | None,
    user_role: str | None,
    person_id: str | None,
    parent_run_id: str | None,
) -> tuple[str | None, str | None] | None:
    """``(person_id, user_id)`` a persisted run could read private memory as, or None.

    A replay must never read more than the original run could, so this is the
    engine's own rule, ``robothor.engine.private_recall.private_reader_for_run``,
    applied to the run record: a person only when the run served them in their
    own one-to-one webchat conversation (their webchat session or plan step,
    the operator's main session), and no one for any other run (scheduled, hook, event, manual, workflow, sub-agent, workspace,
    channel), whoever it ran as. The record does not carry whether the identity
    was verified, so the run's person is taken as verified. A spawned run
    (``parent_run_id``) reads as no one: ``agent_runs.person_id`` is copied onto
    spawned runs, so the record alone cannot show whom it served. A Telegram
    run reads as no one too: its ``person_id`` may have been filled by
    ``link_run_person``'s chat-id lookup when the live identity carried no
    person, and then the live call had no private reader; nothing on the run
    record says which source filled it.
    """
    if parent_run_id:
        return None
    if str(trigger_type or "") == "telegram":
        return None
    from robothor.engine import private_recall
    from robothor.identity.context import IdentityContext

    identity = IdentityContext(
        tenant_id="",
        channel=str(trigger_type or ""),
        identifier=user_id or person_id or "",
        verified=True,
        role=user_role or "",
        person_id=person_id or None,
    )
    reader = private_recall.private_reader_for_run(
        trigger_type, trigger_detail, user_id or None, identity
    )
    if reader is None:
        return None
    return (reader.person_id, reader.user_id)


_RUN_STEPS_SQL = """
    SELECT s.id::text AS step_id, s.created_at, s.tool_name, s.tool_input,
           r.id::text AS run_id, r.agent_id, r.person_id::text AS person_id, r.user_id,
           r.user_role, r.trigger_type, r.trigger_detail, r.parent_run_id::text AS parent_run_id
      FROM agent_run_steps s
      JOIN agent_runs r ON r.id = s.run_id
     WHERE r.tenant_id = %s
       AND s.step_type = 'tool_call'
       AND (s.tool_name = 'search_memory'
            OR (s.tool_name = 'tool_call' AND s.tool_input->>'name' = 'search_memory'))
       AND (%s::timestamptz IS NULL OR s.created_at >= %s::timestamptz)
     ORDER BY s.created_at DESC, s.id
     LIMIT %s
"""


def questions_from_runs(
    tenant_id: str,
    *,
    since: datetime | None = None,
    limit: int = 50,
    sample: bool = False,
    seed: int = 1,
) -> list[Question]:
    """The queries of the tenant's persisted ``search_memory`` calls, newest first."""
    with read_only_scope(), tenant_scope(tenant_id), get_connection() as conn:
        cur = conn.cursor()
        cur.execute(_RUN_STEPS_SQL, (tenant_id, since, since, SOURCE_POOL))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    readers: dict[tuple[Any, Any] | None, Reader] = {None: Reader()}
    out: list[Question] = []
    for row in rows:
        args = search_arguments(row["tool_name"], row["tool_input"])
        text = _clean((args or {}).get("query"))
        if not text:
            continue
        who = run_private_reader(
            trigger_type=row["trigger_type"],
            trigger_detail=row["trigger_detail"],
            user_id=row["user_id"] or None,
            user_role=row["user_role"],
            person_id=row["person_id"],
            parent_run_id=row["parent_run_id"],
        )
        if who is not None and who not in readers:
            readers[who] = reader_for(tenant_id, person_id=who[0], user_id=who[1])
        origin = {
            "source": "runs",
            "run_id": row["run_id"],
            "step_id": row["step_id"],
            "agent_id": row["agent_id"],
            "at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        limit_arg = _result_limit(args.get("limit")) if args else DEFAULT_RESULTS
        out.append(Question(text, reader=readers[who], limit=limit_arg, origin=origin))
    return pick_questions(out, limit=limit, sample=sample, seed=seed)


_CHAT_TURNS_SQL = """
    SELECT m.id, m.created_at, m.message->>'content' AS content,
           s.id AS session_id, s.session_key, s.person_id::text AS person_id
      FROM chat_messages m
      JOIN chat_sessions s ON s.id = m.session_id
     WHERE s.tenant_id = %s
       AND m.message->>'role' = 'user'
       AND jsonb_typeof(m.message->'content') = 'string'
       AND (%s::timestamptz IS NULL OR m.created_at >= %s::timestamptz)
     ORDER BY m.created_at DESC, m.id DESC
     LIMIT %s
"""


def _chat_reader(tenant_id: str, session_key: str, person_id: str | None) -> Reader | None:
    """The owner of a conversation as a reader; None when it has no known owner."""
    from robothor.memory.segments import session_scope
    from robothor.rooms.keys import parse_room_key

    scope_kind, scope_id = session_scope(tenant_id, session_key, person_id)
    if scope_kind == "shared":
        room = parse_room_key(session_key)
        return Reader(room_ids=(room[1],)) if room else None
    if not scope_id:
        return None
    kind, _, ident = scope_id.partition(":")
    if kind == "person":
        return Reader(person_id=ident)
    if kind == "user":
        return reader_for(tenant_id, user_id=ident)
    return None


def questions_from_chat(
    tenant_id: str,
    *,
    since: datetime | None = None,
    limit: int = 50,
    sample: bool = False,
    seed: int = 1,
    skipped: list[dict[str, Any]] | None = None,
) -> list[Question]:
    """Recent user turns, each read as its conversation's owner; ownerless turns skipped."""
    from robothor.secrets.redaction import redact

    with read_only_scope(), tenant_scope(tenant_id):
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(_CHAT_TURNS_SQL, (tenant_id, since, since, SOURCE_POOL))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        readers: dict[tuple[str, Any], Reader | None] = {}
        out: list[Question] = []
        for row in rows:
            text = _clean(redact(row["content"] or ""))
            if not text:
                continue
            key = (row["session_key"], row["person_id"])
            if key not in readers:
                readers[key] = _chat_reader(tenant_id, *key)
            reader = readers[key]
            if reader is None:
                if skipped is not None:
                    skipped.append({"message_id": row["id"], "reason": "conversation has no owner"})
                continue
            origin = {"source": "chat", "message_id": row["id"], "session_id": row["session_id"]}
            out.append(Question(text, reader=reader, origin=origin))
    return pick_questions(out, limit=limit, sample=sample, seed=seed)


# --------------------------------------------------------------------------- #
# The replay
# --------------------------------------------------------------------------- #


def _text(row: dict[str, Any]) -> str:
    return str(
        row.get("fact_text")
        or row.get("insight_text")
        or row.get("text")
        or row.get("content")
        or ""
    )


def _row(rank: int, row: dict[str, Any], *, redact: bool) -> dict[str, Any]:
    text = _text(row)
    flat = " ".join(text.split())
    excerpt = flat if len(flat) <= EXCERPT_CHARS else flat[: EXCERPT_CHARS - 1] + "…"
    kind = str(row.get("kind") or "fact")
    out = {
        "rank": rank,
        "kind": kind,
        "id": row.get("id"),
        "key": f"{kind}:{row.get('id')}",
        "legs": sorted(row.get("leg_ranks") or {}),
        "relevance": row.get("relevance"),
        "source": row.get("source"),
        "excerpt": None if redact else excerpt,
        "chars": len(text),
    }
    if row.get("reserved_leg"):
        out["reserved_leg"] = row["reserved_leg"]
    return out


async def _run_side(
    search: Search, q: Question, tenant_id: str, preset: Preset, fan_out: bool, redact: bool
) -> dict[str, Any]:
    started = time.perf_counter()
    rows = await search(
        q.text,
        limit=q.limit,
        tenant_id=tenant_id,
        reader_person_id=q.reader.person_id,
        reader_user_id=q.reader.user_id,
        room_ids=list(q.reader.room_ids),
        **preset_kwargs(preset, fan_out=fan_out),
    )
    latency = (time.perf_counter() - started) * 1000
    return {
        "latency_ms": round(latency, 2),
        "context_tokens": sum(estimate_tokens(_text(r)) for r in rows),
        "results": [_row(i, r, redact=redact) for i, r in enumerate(rows, start=1)],
    }


def diff_sides(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> dict[str, Any]:
    """Overlap (Jaccard on row identity), rows on one side only, and moved rows."""
    rank_a = {r["key"]: r["rank"] for r in a}
    rank_b = {r["key"]: r["rank"] for r in b}
    union = set(rank_a) | set(rank_b)
    shared = set(rank_a) & set(rank_b)
    moved = [
        {"key": k, "rank_a": rank_a[k], "rank_b": rank_b[k], "delta": rank_a[k] - rank_b[k]}
        for k in sorted(shared, key=lambda k: rank_a[k])
        if rank_a[k] != rank_b[k]
    ]
    return {
        "overlap": round(len(shared) / len(union), 4) if union else 1.0,
        "shared": len(shared),
        "only_a": [r for r in a if r["key"] not in rank_b],
        "only_b": [r for r in b if r["key"] not in rank_a],
        "rank_changes": moved,
        "top_changed": (a[0]["key"] if a else None) != (b[0]["key"] if b else None),
    }


async def run_replay(
    tenant_id: str,
    questions: Sequence[Question],
    *,
    compare: Sequence[str] = DEFAULT_COMPARE,
    redact: bool = False,
    search: Search | None = None,
    fan_out: bool | None = None,
    source: str | None = None,
    skipped: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Replay ``questions`` through two presets; the report as a dict. Writes nothing."""
    name_a, name_b = resolve_compare(",".join(compare))
    a, b = PRESETS[name_a], PRESETS[name_b]
    fan = _fan_out() if fan_out is None else fan_out
    run = search or default_search()
    entries: list[dict[str, Any]] = []
    with read_only_scope(), tenant_scope(tenant_id):
        for i, q in enumerate(questions):
            order = (a, b) if i % 2 == 0 else (b, a)
            sides: dict[str, Any] = {}
            for preset in order:
                sides[preset.name] = await _run_side(run, q, tenant_id, preset, fan, redact)
            entries.append(
                {
                    "index": i + 1,
                    "question": None if redact else q.text,
                    "question_chars": len(q.text),
                    "reader": q.reader.as_dict(),
                    "limit": q.limit,
                    "origin": q.origin,
                    "first": order[0].name,
                    "sides": {name_a: sides[name_a], name_b: sides[name_b]},
                    "diff": diff_sides(sides[name_a]["results"], sides[name_b]["results"]),
                }
            )
    return {
        "notice": NOTICE,
        "tenant": tenant_id,
        "source": source,
        "generated_at": datetime.now(UTC).isoformat(),
        "compare": [name_a, name_b],
        "presets": {p.name: _describe(p, fan) for p in (a, b)},
        "order": ORDER_NOTE,
        "redacted": redact,
        "notes": _notes(),
        "skipped": list(skipped),
        "questions": entries,
        "summary": summarize(entries, name_a, name_b),
    }


def _describe(preset: Preset, fan_out: bool) -> dict[str, Any]:
    return {
        "description": preset.description,
        "search": preset_kwargs(preset, fan_out=fan_out),
        "graph_leg_runs": preset.graph,
    }


def _notes() -> list[str]:
    from robothor.engine.feature_flags import data_scoping_mode, is_rip_enabled

    notes = ["latency is end to end per call: question embedding, legs, rerank"]
    if is_rip_enabled(15):
        notes.append("RIP 15 is on: search_memory routes through the router, which is not replayed")
    if data_scoping_mode() == "enforce":
        notes.append("data scoping is enforce: restricted callers' row scope is not replayed")
    return notes


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, -(-len(ordered) * p // 100) - 1))
    return float(ordered[int(k)])


def summarize(entries: Sequence[dict[str, Any]], name_a: str, name_b: str) -> dict[str, Any]:
    n = len(entries)

    def share(pred: Callable[[dict[str, Any]], bool]) -> float | None:
        return round(sum(1 for e in entries if pred(e)) / n, 4) if n else None

    def legs(side: str) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for e in entries:
            for row in e["diff"][side]:
                counts.update(row["legs"] or ["(none)"])
        return dict(sorted(counts.items()))

    def has_segment(side: str) -> Callable[[dict[str, Any]], bool]:
        return lambda e: any(r["kind"] == "segment" for r in e["sides"][side]["results"])

    per_side: dict[str, Any] = {"latency_ms": {}, "context_tokens": {}, "segment_share": {}}
    for name in (name_a, name_b):
        lat = [e["sides"][name]["latency_ms"] for e in entries]
        tok = [e["sides"][name]["context_tokens"] for e in entries]
        per_side["latency_ms"][name] = {"p50": percentile(lat, 50), "p95": percentile(lat, 95)}
        per_side["context_tokens"][name] = {"mean": round(sum(tok) / n, 1) if n else None}
        per_side["segment_share"][name] = share(has_segment(name))
    overlaps = [e["diff"]["overlap"] for e in entries]
    return {
        "questions": n,
        "mean_overlap": round(sum(overlaps) / n, 4) if n else None,
        "top_changed_share": share(lambda e: e["diff"]["top_changed"]),
        **per_side,
        "only_a_by_leg": legs("only_a"),
        "only_b_by_leg": legs("only_b"),
    }


# --------------------------------------------------------------------------- #
# Text output
# --------------------------------------------------------------------------- #


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _reader_label(reader: dict[str, Any]) -> str:
    parts = [f"person:{reader['person_id']}"] if reader.get("person_id") else []
    if reader.get("user_id"):
        parts.append(f"user:{reader['user_id']}")
    if reader.get("room_ids"):
        parts.append(f"{len(reader['room_ids'])} workspace(s)")
    return ", ".join(parts) or "no reader (shared rows only)"


def _entry_lines(e: dict[str, Any], a: str, b: str) -> list[str]:
    question = (
        e["question"] if e["question"] is not None else f"(redacted, {e['question_chars']} chars)"
    )
    lines = [f"[{e['index']}] {question}", f"    reader: {_reader_label(e['reader'])}"]
    for name in (a, b):
        side = e["sides"][name]
        lines.append(f"  {name}: {side['latency_ms']:.1f} ms, ~{side['context_tokens']} tokens")
        for r in side["results"]:
            rel = "—" if r["relevance"] is None else f"{float(r['relevance']):.2f}"
            text = r["excerpt"] if r["excerpt"] is not None else f"({r['chars']} chars)"
            lines.append(f"    {r['rank']:>2}. {r['key']} [{','.join(r['legs'])}] {rel}  {text}")
    d = e["diff"]
    moved = ", ".join(f"{c['key']} {c['rank_a']}→{c['rank_b']}" for c in d["rank_changes"])
    lines.append(
        f"  overlap {d['overlap']:.2f}; top {'changed' if d['top_changed'] else 'same'}; "
        f"only {a}: {', '.join(r['key'] for r in d['only_a']) or '-'}; "
        f"only {b}: {', '.join(r['key'] for r in d['only_b']) or '-'}; moved: {moved or '-'}"
    )
    return lines


def summary_lines(report: dict[str, Any]) -> list[str]:
    a, b = report["compare"]
    s = report["summary"]

    def per(key: str, fmt: Callable[[Any], str]) -> str:
        return f"{a} {fmt(s[key][a])} · {b} {fmt(s[key][b])}"

    def lat(v: dict[str, Any]) -> str:
        return "n/a" if v["p50"] is None else f"{v['p50']:.1f}/{v['p95']:.1f}"

    def counts(d: dict[str, int]) -> str:
        return ", ".join(f"{k} {v}" for k, v in d.items()) or "-"

    lines = [
        f"summary ({a} against {b})",
        f"  questions: {s['questions']}",
        f"  mean overlap: {_num(s['mean_overlap'])}",
        f"  top result changed: {_pct(s['top_changed_share'])}",
        f"  returns a conversation segment: {per('segment_share', _pct)}",
        f"  latency p50/p95 ms: {per('latency_ms', lat)}",
        f"  context tokens, mean: {per('context_tokens', lambda v: str(v['mean']))}",
        f"  rows only in {b}, by leg: {counts(s['only_b_by_leg'])}",
        f"  rows only in {a}, by leg: {counts(s['only_a_by_leg'])}",
    ]
    if report.get("skipped"):
        lines.append(f"  skipped: {len(report['skipped'])} question(s) with no known reader")
    return lines


def format_text(report: dict[str, Any]) -> str:
    a, b = report["compare"]
    lines = [
        NOTICE,
        f"memory replay, tenant {report['tenant']}: {a} against {b}, "
        f"{len(report['questions'])} question(s)",
        f"order: {report['order']}",
        *(f"note: {n}" for n in report.get("notes", [])),
        "",
    ]
    for e in report["questions"]:
        lines.extend(_entry_lines(e, a, b))
        lines.append("")
    lines.extend(summary_lines(report))
    return "\n".join(lines)


__all__ = [
    "DEFAULT_COMPARE",
    "NOTICE",
    "PRESETS",
    "Preset",
    "Question",
    "Reader",
    "diff_sides",
    "format_text",
    "parse_since",
    "percentile",
    "pick_questions",
    "preset_kwargs",
    "questions_from_chat",
    "questions_from_file",
    "questions_from_runs",
    "require_tenant",
    "resolve_compare",
    "run_replay",
    "search_arguments",
    "summarize",
    "summary_lines",
]
