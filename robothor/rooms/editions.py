"""Editions: what an agent may publish into a room, and what publishing does.

- ``validate_blocks`` / ``validate_suggestions``: the content contract. Every
  block is one of ``models.ALLOWED_COMPONENTS`` with the props that component
  renders, checked per component; an error names the offending block index.
- ``clean_summary`` / ``clean_title``: plain text. The summary re-enters later
  prompts, so control sequences and prompt or dashboard marker syntax go.
- ``build_provenance``: built by the server from the run's recorded tool
  calls (``agent_run_steps``), its run id, its run-as user and start time.
  Nothing provenance-like in tool arguments is ever read.
- ``publish_from_run``: the whole of ``room_publish_edition``, sync (the tool
  handler runs it in a worker thread). The room and the preview flag come from
  ``robothor.rooms.run_context``, never from arguments.
- ``register_edition_published``: the one extension point; hooks run after a
  non-preview edition is published (task creation, notifications).
- ``stamp_turn_message``: the chat store stamps ``edition_id`` on the
  assistant message saved for the turn that published (the editions are
  collected on the run's ``RoomRun``), so history can re-render it.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from robothor.db.connection import get_connection
from robothor.rooms import dal
from robothor.rooms.access import Actor, require
from robothor.rooms.errors import InvalidBlocks, RoomForbidden, RoomNotFound
from robothor.rooms.keys import canonical_uuid
from robothor.rooms.models import ALLOWED_COMPONENTS, EDITION_KINDS, GRAINS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from robothor.rooms.models import Edition, Room
    from robothor.rooms.run_context import RoomRun

logger = logging.getLogger(__name__)

__all__ = [
    "COMPONENT_CHECKS",
    "build_provenance",
    "clean_summary",
    "clean_title",
    "edition_event_payload",
    "emit_edition_event",
    "load_run_record",
    "run_cost",
    "on_edition_published",
    "publish_from_run",
    "register_edition_published",
    "stamp_turn_message",
    "unregister_edition_published",
    "validate_blocks",
    "validate_suggestions",
]

MAX_BLOCKS = 40
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_ROWS = 500
MAX_COLUMNS = 30
MAX_POINTS = 500
MAX_SLICES = 50
MAX_METRICS = 24
MAX_EVENTS = 200
MAX_LABEL_CHARS = 500
MAX_CELL_CHARS = 2000
MAX_MARKDOWN_CHARS = 20_000
MAX_SUGGESTIONS = 10
MAX_SUGGESTION_TITLE_CHARS = 200
MAX_SUGGESTION_DETAIL_CHARS = 2000
MAX_SUGGESTION_HINT_CHARS = 200
MAX_SUMMARY_CHARS = 4000
MAX_TITLE_CHARS = 200
MAX_PROVENANCE_CALLS = 200

_PUBLISH_TOOL = "room_publish_edition"


# ─── block shapes ────────────────────────────────────────────────────────


def _keys(props: Mapping[str, Any], allowed: set[str], required: set[str]) -> None:
    unknown = sorted(set(props) - allowed)
    if unknown:
        raise ValueError(f"unknown props {', '.join(unknown)}")
    missing = sorted(required - set(props))
    if missing:
        raise ValueError(f"missing props {', '.join(missing)}")


def _text(value: Any, what: str, limit: int = MAX_LABEL_CHARS) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    if len(value) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _label_or_number(value: Any, what: str) -> None:
    if not _is_number(value):
        _text(value, what)


def _optional_text(props: Mapping[str, Any], *names: str) -> None:
    for name in names:
        if name in props:
            _text(props[name], name)


def _items(value: Any, what: str, limit: int, *, minimum: int = 1) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list")
    if not minimum <= len(value) <= limit:
        raise ValueError(f"{what} must hold {minimum} to {limit} items, not {len(value)}")
    return value


def _row(row: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"{what} must be an object")
    if len(row) > MAX_COLUMNS:
        raise ValueError(f"{what} has more than {MAX_COLUMNS} columns")
    for key, cell in row.items():
        if cell is not None and not isinstance(cell, bool) and not _is_number(cell):
            _text(cell, f"{what}.{key}", MAX_CELL_CHARS)
    return row


_METRIC_KEYS = {"title", "value", "description", "trend", "trendValue"}


def _metric(props: Mapping[str, Any], what: str, extra: frozenset[str] = frozenset()) -> None:
    _keys(props, _METRIC_KEYS | extra, {"title", "value"})
    _text(props["title"], f"{what}title")
    _label_or_number(props["value"], f"{what}value")
    _optional_text(props, "description", "trendValue", *extra)
    if "trend" in props and props["trend"] not in ("up", "down", "neutral"):
        raise ValueError(f"{what}trend must be up, down or neutral")


def _metric_card(props: Mapping[str, Any]) -> None:
    _metric(props, "", frozenset({"accentColor"}))


def _metric_grid(props: Mapping[str, Any]) -> None:
    _keys(props, {"metrics"}, {"metrics"})
    for i, metric in enumerate(_items(props["metrics"], "metrics", MAX_METRICS)):
        if not isinstance(metric, dict):
            raise ValueError(f"metrics[{i}] must be an object")
        _metric(metric, f"metrics[{i}].")


def _series_chart(props: Mapping[str, Any]) -> None:
    _keys(props, {"title", "data", "dataKey", "nameKey", "color"}, {"title", "data", "dataKey"})
    _text(props["title"], "title")
    _text(props["dataKey"], "dataKey")
    _optional_text(props, "nameKey", "color")
    for i, row in enumerate(_items(props["data"], "data", MAX_POINTS)):
        point = _row(row, f"data[{i}]")
        if not _is_number(point.get(props["dataKey"])):
            raise ValueError(f"data[{i}].{props['dataKey']} must be a finite number")


def _pie_chart(props: Mapping[str, Any]) -> None:
    _keys(props, {"title", "data", "colors"}, {"title", "data"})
    _text(props["title"], "title")
    if "colors" in props:
        for i, color in enumerate(_items(props["colors"], "colors", MAX_SLICES, minimum=0)):
            _text(color, f"colors[{i}]")
    for i, slice_ in enumerate(_items(props["data"], "data", MAX_SLICES)):
        if not isinstance(slice_, dict) or set(slice_) != {"name", "value"}:
            raise ValueError(f"data[{i}] must be exactly {{name, value}}")
        _text(slice_["name"], f"data[{i}].name")
        if not _is_number(slice_["value"]) or slice_["value"] < 0:
            raise ValueError(f"data[{i}].value must be a non-negative number")


def _data_table(props: Mapping[str, Any]) -> None:
    _keys(props, {"title", "rows", "data", "columns"}, set())
    _optional_text(props, "title")
    sources = [name for name in ("rows", "data") if name in props]
    if len(sources) != 1:
        raise ValueError("a table needs exactly one of rows or data")
    for i, row in enumerate(_items(props[sources[0]], sources[0], MAX_ROWS, minimum=0)):
        _row(row, f"{sources[0]}[{i}]")
    if "columns" in props:
        for i, column in enumerate(_items(props["columns"], "columns", MAX_COLUMNS)):
            _text(column, f"columns[{i}]")


def _markdown(props: Mapping[str, Any]) -> None:
    _keys(props, {"content", "title"}, {"content"})
    _text(props["content"], "content", MAX_MARKDOWN_CHARS)
    _optional_text(props, "title")


def _timeline(props: Mapping[str, Any]) -> None:
    _keys(props, {"events", "title"}, {"events"})
    _optional_text(props, "title")
    for i, event in enumerate(_items(props["events"], "events", MAX_EVENTS)):
        if not isinstance(event, dict):
            raise ValueError(f"events[{i}] must be an object")
        _keys(
            event, {"id", "title", "timestamp", "description", "type"}, {"id", "title", "timestamp"}
        )
        for name in ("id", "title", "timestamp"):
            _text(event[name], f"events[{i}].{name}")
        _optional_text(event, "description", "type")


#: component -> its props check. Kept equal to ``models.ALLOWED_COMPONENTS``.
COMPONENT_CHECKS: dict[str, Callable[[Mapping[str, Any]], None]] = {
    "render_metric_card": _metric_card,
    "render_metric_grid": _metric_grid,
    "render_bar_chart": _series_chart,
    "render_line_chart": _series_chart,
    "render_pie_chart": _pie_chart,
    "render_data_table": _data_table,
    "render_markdown": _markdown,
    "render_timeline": _timeline,
}
assert set(COMPONENT_CHECKS) == ALLOWED_COMPONENTS  # noqa: S101 - import-time invariant


def validate_blocks(blocks: Any) -> list[dict[str, Any]]:
    """``blocks`` unchanged when every block is an allowed component with the
    props it renders. Raises ``InvalidBlocks`` naming the first bad block."""
    if not isinstance(blocks, list):
        raise InvalidBlocks("blocks must be a list of {component, props}")
    if len(blocks) > MAX_BLOCKS:
        raise InvalidBlocks(f"an edition holds at most {MAX_BLOCKS} blocks, not {len(blocks)}")
    for i, block in enumerate(blocks):
        if not isinstance(block, dict) or set(block) != {"component", "props"}:
            raise InvalidBlocks(f"block {i} must be exactly {{component, props}}")
        component = block["component"]
        if component not in ALLOWED_COMPONENTS:
            allowed = ", ".join(sorted(ALLOWED_COMPONENTS))
            raise InvalidBlocks(f"block {i}: component {component!r} is not one of {allowed}")
        if not isinstance(block["props"], dict):
            raise InvalidBlocks(f"block {i} ({component}): props must be an object")
        try:
            COMPONENT_CHECKS[component](block["props"])
        except ValueError as exc:
            raise InvalidBlocks(f"block {i} ({component}): {exc}") from exc
    size = len(json.dumps(blocks, ensure_ascii=False).encode())
    if size > MAX_PAYLOAD_BYTES:
        raise InvalidBlocks(f"blocks are {size} bytes; at most {MAX_PAYLOAD_BYTES} bytes")
    return blocks


# ─── suggestions ─────────────────────────────────────────────────────────

_SUGGESTION_KEY = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")
_SUGGESTION_LIMITS = {
    "title": MAX_SUGGESTION_TITLE_CHARS,
    "detail": MAX_SUGGESTION_DETAIL_CHARS,
    "action_hint": MAX_SUGGESTION_HINT_CHARS,
}


def _suggestion(item: Any, seen: set[str]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("must be an object")
    unknown = sorted(set(item) - {"key", *_SUGGESTION_LIMITS})
    if unknown:
        raise ValueError(f"unknown fields {', '.join(unknown)}")
    key = item.get("key")
    if not isinstance(key, str) or not _SUGGESTION_KEY.match(key):
        raise ValueError("key must be 1-64 lowercase letters, digits, '-' or '_'")
    if key in seen:
        raise ValueError(f"key {key!r} is used twice")
    out: dict[str, Any] = {"key": key}
    for name, limit in _SUGGESTION_LIMITS.items():
        value = item.get(name, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"{name} must be a string of at most {limit} characters")
        out[name] = clean_text(value, limit, one_line=name != "detail")
    if not out["title"]:
        raise ValueError("title is required")
    seen.add(key)
    return out


def validate_suggestions(suggestions: Any) -> list[dict[str, Any]]:
    """Suggested tasks as ``{key, title, detail, action_hint}``, text cleaned.
    Raises ``InvalidBlocks`` naming the first bad suggestion."""
    if suggestions is None:
        return []
    if not isinstance(suggestions, list):
        raise InvalidBlocks("suggestions must be a list of {key, title, detail, action_hint}")
    if len(suggestions) > MAX_SUGGESTIONS:
        raise InvalidBlocks(f"an edition holds at most {MAX_SUGGESTIONS} suggestions")
    seen: set[str] = set()
    out = []
    for i, item in enumerate(suggestions):
        try:
            out.append(_suggestion(item, seen))
        except ValueError as exc:
            raise InvalidBlocks(f"suggestion {i}: {exc}") from exc
    return out


# ─── plain text ──────────────────────────────────────────────────────────

#: CSI / OSC / two-character escape sequences.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
#: The characters a tag, a prompt boundary or a dashboard marker is built
#: from, and their plain-text stand-ins. Titles, summaries and suggestions are
#: plain text, so no markup in them is legitimate: rather than recognising tags
#: by name (a list that can always be dodged), none of these survives in any
#: spelling. The stand-ins are not folded back by NFKC and no detector reads
#: them: ``<``/``>`` become single angle quotation marks, ``[``/``]``
#: parentheses. ``&`` is kept unless it starts an entity that decodes to one.
_BRACKETS = {"<": "\u2039", ">": "\u203a", "[": "(", "]": ")"}
_TRANSLATE = str.maketrans(_BRACKETS)
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})
#: Format characters ordinary text needs: ZWJ (emoji sequences, Indic
#: conjuncts) and ZWNJ (Persian, Indic). Kept only between two characters that
#: are not ASCII even after folding, so neither can split an entity or a name.
_JOINERS = frozenset({"\u200c", "\u200d"})
_JOINABLE = ("L", "M", "So", "Sk")
#: Line breaks a one-line field turns into a space; other whitespace is kept.
_LINE_BREAKS = re.compile(r"[\n\r\x0b\x0c\x85\u2028\u2029]+")
#: The longest entity spelling worth looking for, doubled encodings included.
_ENTITY_WINDOW = 48


def _fold(value: str) -> str:
    """The text as a reader that decodes entities, folds look-alikes and drops
    hidden characters would see it, to a fixed point. Used to decide what is
    offending; never emitted."""
    for _ in range(64):
        folded = "".join(
            ch
            for ch in unicodedata.normalize("NFKC", html.unescape(value))
            if unicodedata.category(ch) not in _HIDDEN_CATEGORIES
        )
        if folded == value:
            break
        value = folded
    return value


def _offending(value: str) -> bool:
    return any(ch in _BRACKETS for ch in _fold(value))


def _joinable(ch: str) -> bool:
    folded = unicodedata.normalize("NFKC", ch)
    return not folded.isascii() and unicodedata.category(ch).startswith(_JOINABLE)


def _visible(value: str) -> str:
    """Escape sequences and control, format, surrogate and private-use
    characters removed; newlines, tabs and joiners between non-ASCII letters,
    marks or symbols kept."""
    text = _ANSI.sub("", value)
    return "".join(ch for i, ch in enumerate(text) if _kept(text, i))


def _kept(text: str, i: int) -> bool:
    ch = text[i]
    if ch in "\n\t" or unicodedata.category(ch) not in _HIDDEN_CATEGORIES:
        return True
    return (
        ch in _JOINERS
        and 0 < i < len(text) - 1
        and _joinable(text[i - 1])
        and _joinable(text[i + 1])
    )


def _entity_end(text: str, start: int) -> int | None:
    """Where an entity starting at ``start`` ends, when it decodes (however
    deeply encoded) to a bracket; None for any other ampersand."""
    for end in range(start + 2, min(len(text), start + _ENTITY_WINDOW) + 1):
        if _offending(text[start:end]):
            return end
    return None


def _neutralised(text: str) -> str:
    """The text with only its offending characters replaced: a character that
    is or folds to a bracket, and an entity that decodes to one."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        folded = unicodedata.normalize("NFKC", ch)
        end = _entity_end(text, i) if "&" in folded else None
        if end is not None:
            out.append(_fold(text[i:end]).translate(_TRANSLATE))
            i = end
            continue
        out.append(folded.translate(_TRANSLATE) if _offending(ch) else ch)
        i += 1
    return "".join(out)


def _settle(value: str, *, one_line: bool) -> str:
    for _ in range(16):
        text = _neutralised(_visible(value))
        if one_line:
            text = _LINE_BREAKS.sub(" ", text)
        text = text.strip()
        if text == value:
            break
        value = text
    return value


def clean_text(value: str, limit: int, *, one_line: bool = False) -> str:
    """``value`` as plain text, with no markup able to come back to life and
    everything else left exactly as written.

    Hidden characters are removed (newlines and tabs kept, and ZWJ/ZWNJ
    between non-ASCII letters, marks or symbols); every character that is or
    folds (NFKC) to ``<``, ``>``, ``[`` or ``]``, and every entity that decodes
    to one however deeply encoded, is replaced by a stand-in; a one-line field
    turns line breaks into spaces. Other characters (superscripts, fractions,
    fullwidth punctuation, ligatures) are emitted as written. Repeated until
    nothing changes, so cleaning the result again changes nothing."""
    return _settle(_settle(value, one_line=one_line)[:limit], one_line=one_line)


def clean_summary(value: Any) -> str:
    return clean_text(value if isinstance(value, str) else "", MAX_SUMMARY_CHARS)


def clean_title(value: Any) -> str:
    return clean_text(value if isinstance(value, str) else "", MAX_TITLE_CHARS, one_line=True)


# ─── provenance ──────────────────────────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def build_provenance(
    *,
    run_id: str,
    ran_as: str,
    started_at: datetime | None,
    steps: Sequence[Mapping[str, Any]],
    room_rev: int,
    cost_usd: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, Any]:
    """``{run_id, ran_as, started_at, room_rev, tool_calls: [{tool, at, ok}]}``
    from the run's recorded tool-call steps. A ``tool_call`` wrapper is listed
    as the tool it ran; the publish call itself is not evidence and is left out.

    What the run cost (``cost_usd``, ``input_tokens``, ``output_tokens``) is
    added when known: a flag shown where the report is read, never a limit."""
    calls = []
    for s in steps:
        tool = s.get("tool_name") or ""
        if tool == "tool_call" and s.get("inner_name"):
            tool = s["inner_name"]
        if not tool or tool == _PUBLISH_TOOL:
            continue
        calls.append(
            {"tool": tool, "at": _iso(s.get("started_at")), "ok": not s.get("error_message")}
        )
    prov: dict[str, Any] = {
        "run_id": run_id,
        "ran_as": ran_as,
        "started_at": _iso(started_at),
        "room_rev": room_rev,
        "tool_calls": calls[:MAX_PROVENANCE_CALLS],
    }
    if len(calls) > MAX_PROVENANCE_CALLS:
        prov["tool_calls_truncated"] = len(calls) - MAX_PROVENANCE_CALLS
    prov.update(run_cost(cost_usd, input_tokens, output_tokens))
    return prov


def run_cost(
    cost_usd: Any = None, input_tokens: Any = None, output_tokens: Any = None
) -> dict[str, Any]:
    """The cost keys of an edition's provenance, for the values that are known."""
    out: dict[str, Any] = {}
    if isinstance(cost_usd, int | float | Decimal) and not isinstance(cost_usd, bool):
        out["cost_usd"] = round(float(cost_usd), 6)
    tokens = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    out.update({k: v for k, v in tokens.items() if isinstance(v, int) and not isinstance(v, bool)})
    return out


@dataclass(frozen=True)
class RunRecord:
    """What ``agent_runs`` and ``agent_run_steps`` hold for one run."""

    started_at: datetime | None
    #: A sub-agent run (a parent run, or depth above 0).
    is_child: bool
    steps: list[dict[str, Any]]
    #: What the row says the run has cost so far.
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def load_run_record(tenant_id: str, run_id: str) -> RunRecord | None:
    """The run of the tenant as recorded, or None when it has no row.

    The runner flushes steps after every iteration, so every call whose result
    the agent had seen when it called the publish tool is here."""
    run = canonical_uuid(run_id)
    if run is None:
        return None
    with get_connection(read_only=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(started_at, created_at), "
            "parent_run_id IS NOT NULL OR COALESCE(nesting_depth, 0) > 0, "
            "total_cost_usd, input_tokens, output_tokens "
            "FROM agent_runs WHERE id = %s AND tenant_id = %s",
            (run, tenant_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "SELECT tool_name, tool_input->>'name', started_at, error_message "
            "FROM agent_run_steps WHERE run_id = %s AND step_type = 'tool_call' "
            "ORDER BY step_number LIMIT %s",
            (run, MAX_PROVENANCE_CALLS * 10),
        )
        steps = [
            {"tool_name": t, "inner_name": inner, "started_at": at, "error_message": err}
            for t, inner, at, err in cur.fetchall()
        ]
        return RunRecord(
            started_at=row[0],
            is_child=bool(row[1]),
            steps=steps,
            cost_usd=float(row[2]) if row[2] is not None else None,
            input_tokens=row[3],
            output_tokens=row[4],
        )


def _recorded_top_level_run(tenant_id: str, run_id: str) -> RunRecord:
    """The run's record. Refuses a run with no row (no provenance could be
    built) and a sub-agent run: a child inherits its parent's room carrier,
    and an edition is published by the room's own run, whose calls it cites."""
    record = load_run_record(tenant_id, run_id)
    if record is None:
        raise ValueError("this run is not recorded, so its edition's provenance cannot be built")
    if record.is_child:
        raise ValueError(
            "a sub-agent run cannot publish into the workspace; return the result to the "
            "workspace's own run and publish from there"
        )
    return record


# ─── the published extension point ───────────────────────────────────────

_published_hooks: list[Callable[[Room, Edition], None]] = []
_hooks_lock = threading.Lock()


def register_edition_published(hook: Callable[[Room, Edition], None]) -> None:
    """Run ``hook(room, edition)`` after every non-preview publish.

    Sync, called from a worker thread after the edition is stored; an
    exception is logged and does not affect the publish or other hooks."""
    with _hooks_lock:
        if hook not in _published_hooks:
            _published_hooks.append(hook)


def unregister_edition_published(hook: Callable[[Room, Edition], None]) -> None:
    with _hooks_lock:
        if hook in _published_hooks:
            _published_hooks.remove(hook)


def on_edition_published(room: Any, edition: Any) -> None:
    """Run every registered hook. Never raises."""
    with _hooks_lock:
        hooks = list(_published_hooks)
    for hook in hooks:
        try:
            hook(room, edition)
        except Exception:
            logger.exception("an edition-published hook failed")


def edition_event_payload(room: Room, edition: Edition) -> dict[str, Any]:
    """Ids only: the dashboard fetches the content through the API, for users
    who can access the room. Never the session key."""
    return {"room_id": room.id, "tenant_id": room.tenant_id, "edition_id": edition.id}


def emit_edition_event(room: Room, edition: Edition) -> None:
    """Publish ``room.edition`` on the ``agent`` stream. Blocking (Redis)."""
    from robothor.events.bus import publish

    publish(
        "agent",
        "room.edition",
        edition_event_payload(room, edition),
        source="rooms",
        tenant_id=room.tenant_id,
    )


# ─── the turn's editions, for the chat store ─────────────────────────────


def stamp_turn_message(cur: Any, tenant_id: str, edition_ids: list[str], message_id: int) -> None:
    """In the chat store's transaction: link the editions to the saved message."""
    cur.execute(
        "UPDATE room_editions SET chat_message_id = %s, updated_at = now() "
        "WHERE tenant_id = %s AND id = ANY(%s::uuid[]) AND chat_message_id IS NULL",
        (message_id, tenant_id, edition_ids),
    )


# ─── publish ─────────────────────────────────────────────────────────────

_NOT_HERE = "room_publish_edition works only inside a run in that workspace's own conversation"


def _period_arg(value: Any) -> tuple[str, date, date] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"grain", "start", "end"}:
        raise ValueError("period must be {grain, start, end} with ISO dates")
    if value["grain"] not in GRAINS:
        raise ValueError(f"period grain must be one of {sorted(GRAINS)}")
    try:
        start, end = date.fromisoformat(value["start"]), date.fromisoformat(value["end"])
    except (TypeError, ValueError) as exc:
        raise ValueError("period start and end must be ISO dates (YYYY-MM-DD)") from exc
    if start > end:
        raise ValueError("a period cannot end before it starts")
    return (value["grain"], start, end)


def _room_for_run(
    args: Mapping[str, Any], actor: Actor, agent_id: str, run: RoomRun | None
) -> Room:
    room_id = canonical_uuid(args.get("room_id"))
    if run is None or run.tenant_id != actor.tenant_id or room_id is None or room_id != run.room_id:
        raise ValueError(_NOT_HERE)
    room = require(actor, room_id, "post")
    if room.session_key != run.session_key or room.agent_id != agent_id:
        raise ValueError(_NOT_HERE)
    return room


def _scheduled_edition(
    room: Room, run: RoomRun, period: tuple[str, date, date] | None, viewer: str
) -> Edition:
    assert run.edition_id is not None  # noqa: S101 - the caller checked
    edition = dal.get_edition(room.tenant_id, run.edition_id, viewer_id=viewer)
    if edition is None or edition.room_id != room.id or edition.kind != "edition":
        raise ValueError("this run's edition no longer exists")
    if period is not None and period != (edition.grain, edition.period_start, edition.period_end):
        raise ValueError(
            f"this run builds the edition for {edition.grain} {edition.period_start} to "
            f"{edition.period_end}; its period cannot change"
        )
    if edition.status != "building":
        raise ValueError("this run's edition was already published or failed")
    return edition


def _building_edition(
    room: Room,
    run: RoomRun,
    kind: str,
    period: tuple[str, date, date] | None,
    actor: Actor,
    run_id: str,
) -> tuple[Edition, bool]:
    """The ``building`` row to complete and whether this call created it."""
    requested_by = (run.requested_by or actor.user_id) if run.preview else actor.user_id
    common: dict[str, Any] = {
        "preview": run.preview,
        "requested_by": requested_by,
        "run_id": run_id,
    }
    if kind == "dashboard":
        if period is not None:
            raise ValueError("a dashboard takes no period; publish kind 'edition' for a period")
        return dal.create_edition(room.tenant_id, room.id, kind="dashboard", **common)
    if room.kind != "report":
        raise ValueError("an edition needs a report workspace; publish kind 'dashboard' instead")
    if run.edition_id:
        return _scheduled_edition(room, run, period, requested_by), False
    period = period or run.period
    if period is None:
        raise ValueError("an edition needs a period: {grain, start, end}")
    grain, start, end = period
    return dal.create_edition(
        room.tenant_id,
        room.id,
        kind="edition",
        grain=grain,
        period_start=start,
        period_end=end,
        **common,
    )


def _publish(
    args: Mapping[str, Any], actor: Actor, agent_id: str, run_id: str, run: RoomRun | None
) -> tuple[Room, Edition]:
    room = _room_for_run(args, actor, agent_id, run)
    assert run is not None  # noqa: S101 - _room_for_run refused otherwise
    kind = args.get("kind") or "edition"
    if kind not in EDITION_KINDS:
        raise ValueError(f"kind must be one of {sorted(EDITION_KINDS)}")
    period = _period_arg(args.get("period"))
    title = clean_title(args.get("title"))
    if not title:
        raise ValueError("an edition needs a title")
    summary = clean_summary(args.get("summary"))
    blocks = validate_blocks(args.get("blocks"))
    suggestions = validate_suggestions(args.get("suggestions"))
    record = _recorded_top_level_run(room.tenant_id, run_id)
    edition, created = _building_edition(room, run, kind, period, actor, run_id)
    try:
        provenance = build_provenance(
            run_id=run_id,
            ran_as=actor.user_id,
            started_at=record.started_at,
            steps=record.steps,
            room_rev=edition.revision,
            cost_usd=record.cost_usd,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
        )
        published = dal.publish_edition(
            room.tenant_id,
            edition.id,
            title=title,
            summary=summary,
            blocks=blocks,
            suggestions=suggestions,
            provenance=provenance,
        )
    except Exception:
        if created:
            dal.fail_edition(room.tenant_id, edition.id, "publishing failed")
        raise
    if published is None:
        raise ValueError("the edition was already published or failed; nothing was published")
    return room, published


def _result(room: Room, edition: Edition) -> dict[str, Any]:
    period = None
    if edition.period_start is not None:
        period = {
            "grain": edition.grain,
            "start": edition.period_start.isoformat(),
            "end": edition.period_end.isoformat() if edition.period_end else None,
        }
    out: dict[str, Any] = {
        "published": True,
        "edition_id": edition.id,
        "room_id": room.id,
        "kind": edition.kind,
        "seq": edition.seq,
        "revision": edition.revision,
        "preview": edition.preview,
        "period": period,
        "link": room.link,
    }
    if edition.preview:
        out["note"] = "a preview: only its requester sees it, and nobody is notified"
    return out


def publish_from_run(
    args: Mapping[str, Any],
    *,
    tenant_id: str,
    agent_id: str,
    run_id: str,
    user_id: str,
    user_role: str,
    run: RoomRun | None,
) -> dict[str, Any]:
    """The ``room_publish_edition`` tool, sync. A result or ``{"error": ...}``."""
    actor = Actor(user_id=user_id, role=user_role, tenant_id=tenant_id)
    try:
        room, edition = _publish(args, actor, agent_id, run_id, run)
    except RoomNotFound:
        return {"error": "no such workspace, or you have no access to it"}
    except (RoomForbidden, InvalidBlocks, ValueError) as exc:
        return {"error": str(exc)}
    if not edition.preview:
        assert run is not None  # noqa: S101 - _publish refused otherwise
        run.note_published(edition.id)
        try:
            emit_edition_event(room, edition)
        except Exception:
            logger.exception("room.edition event not published")
        on_edition_published(room, edition)
    return _result(room, edition)
