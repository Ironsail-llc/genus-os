"""Run a public long-memory benchmark through the memory eval.

    load (eval_bench_data)  ->  for each history: ingest into an eval tenant
    through the product's write path  ->  every system retrieves for every
    question on it  ->  answer with the benchmark's reader prompt  ->  score
    with the benchmark's own judge or metric  ->  clean the tenant.

Ingestion is the product's, at the benchmark's clock:

    segments  chat turns through ``chat_store.save_exchange`` (stamped with the
              session's time), then ``segments.index_conversation``, as every
              chat turn is indexed. No model call but the embedder.
    extract   the same, then the conversation's fact extraction: the
              transcript in windows of the product's transcript cap, each
              role through ``ingestion.ingest_content`` with the other role
              read-only (as ``conversation_ingest`` does), with the session's
              date as the extraction's "today"; every row written is then
              stamped with the session's time.

Every stage is cached on disk (``cache_dir``), so ingestion is done once per
code version and reused by every system and answer model, an answer is never
paid for twice, and a cell whose code did not change is carried forward
instead of re-run. The run's own records go to ``progress_path`` (JSONL; a
rerun with the same parameters resumes from it).

Tenant hygiene: a run uses one eval tenant (``memory-eval-bench`` by default),
cleans it before and after every history, and refuses any tenant that is not
``memory-eval-<suffix>`` before the first database call.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from robothor.memory import eval_bench_data as bd
from robothor.memory.eval import EVAL_TENANT, EvalPreconditionError
from robothor.memory.eval_systems import ContextBundle, ContextItem, WorldHandle, make_systems
from robothor.memory.eval_world_seed import SeededWorld, _require_eval_tenants

logger = logging.getLogger(__name__)

__all__ = ["SeededWorld"]

BENCH_TENANT = f"{EVAL_TENANT}-bench"
BENCH_CHANNEL = "eval"
INGEST_MODES: tuple[str, ...] = ("segments", "extract")
DEFAULT_TOKEN_BUDGET = 1000
#: Bumped when the reader prompt or its rendering changes: cached answers
#: under another version are not reused.
ANSWER_PROMPT_VERSION = 2
#: Bumped when judging changes.
BENCH_JUDGE_VERSION = 1
LONGMEMEVAL_ANSWER_MAX_TOKENS = 512
VALIDATION_THRESHOLD = 0.9

#: Tables the extract write path can fill beyond the eval world's, deleted
#: first (they reference facts). Only those present in the schema are touched.
BENCH_EXTRA_TABLES: tuple[str, ...] = (
    "memory_conflict_decisions",
    "memory_fact_access",
    "memory_fact_history",
    "memory_conflicts",
    "memory_write_jobs",
    "ingested_items",
)

#: Back-dated history is read whole: the segment indexer's window, in days.
_RETENTION_DAYS = 36500

Chat = Callable[..., Awaitable[Any]]


# --------------------------------------------------------------------------- #
# Cleanup
# --------------------------------------------------------------------------- #


def cleanup_bench(tenant_id: str) -> dict[str, int]:
    """Delete every row a benchmark ingest can write for this eval tenant.

    The extract path's extra tables first, then the eval world's delete
    (children before parents, identity rows included, the tenant row last).
    """
    (tenant,) = _require_eval_tenants([tenant_id])
    from robothor.db.connection import get_connection, tenant_scope
    from robothor.memory import eval_world_seed as seed

    deleted: dict[str, int] = {}
    names = ", ".join(f"'{t}'" for t in BENCH_EXTRA_TABLES)  # fixed names
    with tenant_scope(tenant), get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT c.table_name FROM information_schema.columns c "
            f"WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id' "
            f"AND c.table_name IN ({names})"  # noqa: S608 — fixed names
        )
        present = {r[0] for r in cur.fetchall()}
        for table in BENCH_EXTRA_TABLES:
            if table in present:
                cur.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))  # noqa: S608
                deleted[table] = max(cur.rowcount, 0)
    for table, n in seed.cleanup_world([tenant]).items():
        deleted[table] = deleted.get(table, 0) + n
    return deleted


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


@dataclass
class IngestResult:
    seeded: SeededWorld
    #: turn id -> the session date, in the benchmark's own format.
    turn_dates: dict[str, str]
    sessions: int
    turns: int
    seconds: float
    facts: int = 0
    extraction_windows: int = 0
    extraction_failed: int = 0


def exchanges(
    turns: Sequence[bd.BenchTurn],
) -> list[tuple[bd.BenchTurn | None, bd.BenchTurn | None]]:
    """Turns as the (user, assistant) exchanges chat storage records.

    A user turn and the assistant turn after it are one exchange; a turn with
    no partner is an exchange with an empty other side.
    """
    out: list[tuple[bd.BenchTurn | None, bd.BenchTurn | None]] = []
    i = 0
    while i < len(turns):
        t = turns[i]
        if t.role == "user":
            nxt = turns[i + 1] if i + 1 < len(turns) else None
            if nxt is not None and nxt.role == "assistant":
                out.append((t, nxt))
                i += 2
                continue
            out.append((t, None))
        else:
            out.append((None, t))
        i += 1
    return out


def transcript_windows(turns: Sequence[bd.BenchTurn]) -> list[list[dict[str, str]]]:
    """Consecutive windows of the product's transcript cap (MAX_TRANSCRIPT_MESSAGES).

    Production extracts the trailing window of a conversation after each run;
    consecutive windows extract every turn once.
    """
    from robothor.memory.conversation_ingest import MAX_TRANSCRIPT_MESSAGES

    msgs = [{"role": t.role, "content": t.text} for t in turns]
    return [
        msgs[i : i + MAX_TRANSCRIPT_MESSAGES] for i in range(0, len(msgs), MAX_TRANSCRIPT_MESSAGES)
    ]


async def _ingest_content(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from robothor.memory.ingestion import ingest_content

    return await ingest_content(*args, **kwargs)


async def extract_session(
    session: bd.BenchSession, tenant: str, session_key: str, *, retries: int = 1
) -> dict[str, int]:
    """The conversation's fact extraction for one session, at its own date."""
    from robothor.db.connection import tenant_scope
    from robothor.memory.conversation_ingest import role_transcripts
    from robothor.memory.facts import extraction_reference_date

    windows = transcript_windows(session.turns)
    failed = facts = 0
    with tenant_scope(tenant), extraction_reference_date(session.at.date().isoformat()):
        for window in windows:
            if len(window) < 1:
                continue
            turns = role_transcripts(window)
            for role, other in (("user", "assistant"), ("assistant", "user")):
                if not turns[role].strip():
                    continue
                result: dict[str, Any] = {}
                for _attempt in range(1 + max(0, retries)):
                    try:
                        result = await _ingest_content(
                            turns[role],
                            BENCH_CHANNEL,
                            "conversation",
                            metadata={"session_key": session_key, "agent_id": "memory-eval"},
                            tenant_id=tenant,
                            role=role,
                            read_only_context=turns[other] or None,
                            fallback_on_failure=False,
                        )
                    except Exception as exc:  # noqa: BLE001 — counted, the run goes on
                        logger.warning("benchmark extraction failed for %s: %s", session_key, exc)
                        result = {"extraction_failed": True}
                    if not result.get("extraction_failed"):
                        break
                if result.get("extraction_failed"):
                    failed += 1
                facts += len(result.get("fact_ids") or [])
    return {"windows": len(windows), "failed": failed, "facts": facts}


def _db_now() -> datetime:
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT clock_timestamp()")
        now: datetime = cur.fetchone()[0]
    return now


def _stamp_facts(tenant: str, since: datetime, at: datetime) -> None:
    """Every fact row this session's extraction wrote or changed takes its time."""
    from robothor.db.connection import get_connection, tenant_scope

    with tenant_scope(tenant), get_connection() as conn:
        cur = conn.cursor()
        for column in ("created_at", "updated_at", "valid_from", "valid_to"):
            cur.execute(
                f"UPDATE memory_facts SET {column} = %s WHERE tenant_id = %s AND {column} >= %s",  # noqa: S608
                (at, tenant, since),
            )


def _utc(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=UTC)


#: Attempts at one history's ingestion (cleaned in between) and at one embedding batch.
INGEST_ATTEMPTS = 3
EMBED_ATTEMPTS = 3


def _embed_backoff(attempt: int) -> float:
    return float(2**attempt)


def validated_embedder(
    inner: Callable[[list[str]], Awaitable[list[list[float]]]],
    *,
    dim: int | None = None,
    attempts: int = EMBED_ATTEMPTS,
) -> Callable[[list[str]], Awaitable[list[list[float]]]]:
    """``inner``, refusing a batch with a missing, empty or wrong-length vector.

    A bad batch is asked again after a back-off; one still bad raises, so a
    zero-length vector never reaches the write path. ``dim`` is the expected
    length; unset, the first non-empty vector sets it.
    """
    expected: list[int] = [dim] if dim else []

    async def embed(texts: list[str]) -> list[list[float]]:
        problem = ""
        for attempt in range(max(1, attempts)):
            vectors = list(await inner(texts))
            if not expected:
                first = next((len(v) for v in vectors if v), 0)
                if first:
                    expected.append(first)
            want = expected[0] if expected else 0
            bad = [
                (i, len(v) if v is not None else None)
                for i, v in enumerate(vectors)
                if v is None or not want or len(v) != want
            ]
            if len(vectors) == len(texts) and not bad:
                return vectors
            problem = (
                f"embedder returned {len(vectors)} vectors for {len(texts)} texts"
                if len(vectors) != len(texts)
                else f"embedder returned {bad[0][1] or 0} dimensions for text {bad[0][0]} "
                f"of {len(texts)} (expected {want or 'a non-empty vector'})"
            )
            logger.warning("%s (attempt %d/%d)", problem, attempt + 1, attempts)
            if attempt + 1 < attempts:
                await asyncio.sleep(_embed_backoff(attempt))
        raise RuntimeError(problem)

    return embed


async def _ingest_with_retry(
    ingest: Callable[..., Awaitable[IngestResult]],
    history: bd.BenchHistory,
    tenant: str,
    *,
    clean: Callable[[str], Any],
    cleanup: bool,
    **kwargs: Any,
) -> IngestResult:
    """Ingest one history, cleaning and trying again on failure; the last error raises."""
    last: Exception | None = None
    for attempt in range(INGEST_ATTEMPTS):
        if cleanup and attempt:
            clean(tenant)
        try:
            return await ingest(history, tenant, **kwargs)
        except Exception as exc:  # noqa: BLE001 — retried, then raised
            last = exc
            logger.warning(
                "benchmark ingest of %s failed (attempt %d/%d): %s",
                history.id,
                attempt + 1,
                INGEST_ATTEMPTS,
                exc,
                exc_info=attempt + 1 == INGEST_ATTEMPTS,
            )
    assert last is not None
    raise last


async def ingest_history(
    history: bd.BenchHistory,
    tenant_id: str = BENCH_TENANT,
    *,
    mode: str = "segments",
    embed_batch: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
    embed_turns: bool = False,
    date_format: Callable[[datetime], str] | None = None,
    extraction_store: ExtractionStore | None = None,
    prefetch_concurrency: int = 1,
) -> IngestResult:
    """Write one history into ``tenant_id`` the way production writes chat.

    Sessions go in clock order. Each turn is stored at its own time, the
    session's segments are indexed, and in ``extract`` mode its facts are
    extracted and stamped with the session's time. The tenant's
    conversations belong to one eval reader, whom the systems search as.
    ``embed_turns`` also embeds each turn (the raw-verbatim baseline's dense
    leg), as the engine embeds chat turns.
    """
    (tenant,) = _require_eval_tenants([tenant_id])
    if mode not in INGEST_MODES:
        raise ValueError(f"unknown ingest mode {mode!r} (one of {', '.join(INGEST_MODES)})")
    from robothor.db.connection import get_connection, tenant_scope
    from robothor.engine.chat_store import save_exchange
    from robothor.memory import eval_world_seed as seed
    from robothor.memory import segments

    started = time.perf_counter()
    fmt = date_format or (lambda d: d.isoformat())
    embed = validated_embedder(embed_batch or seed._default_embed_batch)

    async def _embed(texts: list[str]) -> list[list[float] | None]:
        return list(await embed(texts))

    result = IngestResult(SeededWorld(tenant_ids=(tenant,)), {}, 0, 0, 0.0)
    with tenant_scope(tenant):
        seed._ensure_world_tenant(tenant)
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO crm_people (tenant_id, first_name, last_name) "
                "VALUES (%s, 'Eval', 'Reader') RETURNING id::text",
                (tenant,),
            )
            reader = str(cur.fetchone()[0])
        result.seeded.reader_person_id = reader
        if mode == "extract" and extraction_store is not None:
            # The model calls run ahead, concurrently; the writes stay in order.
            await prefetch_extractions(history, extraction_store, concurrency=prefetch_concurrency)
        stored: list[tuple[int, str]] = []
        for session in sorted(history.sessions, key=lambda s: s.at):
            key = f"bench:{history.id}:{session.id}"
            stamps: list[tuple[int, datetime]] = []
            for n, (user, assistant) in enumerate(exchanges(session.turns)):
                first = user or assistant
                assert first is not None
                ids = save_exchange(
                    key,
                    user.text if user else "",
                    assistant.text if assistant else "",
                    channel=BENCH_CHANNEL,
                    tenant_id=tenant,
                    user_at=_utc(first.at),
                )
                for message_id, turn, side in ((ids[0], user, "u"), (ids[1], assistant, "a")):
                    turn_id = f"{session.id}#{n}{side}"
                    result.seeded.turn_message_ids[turn_id] = message_id
                    result.turn_dates[turn_id] = fmt(session.at)
                    stamps.append((message_id, _utc(turn.at if turn else first.at)))
                    if turn is not None:
                        stored.append((message_id, turn.text))
                        result.turns += 1
            with get_connection() as conn:
                cur = conn.cursor()
                for message_id, at in stamps:
                    cur.execute(
                        "UPDATE chat_messages SET created_at = %s WHERE id = %s", (at, message_id)
                    )
                cur.execute(
                    "UPDATE chat_sessions SET created_at = %s, last_active_at = %s, "
                    "person_id = %s::uuid WHERE tenant_id = %s AND session_key = %s",
                    (
                        _utc(session.at),
                        stamps[-1][1] if stamps else _utc(session.at),
                        reader,
                        tenant,
                        key,
                    ),
                )
            result.seeded.chat_session_keys.append(key)
            await segments.index_conversation(
                key, tenant_id=tenant, embed_batch=_embed, retention_days=_RETENTION_DAYS
            )
            if mode == "extract":
                since = _db_now()
                with (
                    cached_extraction(extraction_store)
                    if extraction_store is not None
                    else contextlib.nullcontext()
                ):
                    stats = await extract_session(session, tenant, key)
                _stamp_facts(tenant, since, _utc(session.at))
                result.extraction_windows += stats["windows"]
                result.extraction_failed += stats["failed"]
                result.facts += stats["facts"]
            result.sessions += 1
        if embed_turns and stored:
            vectors = await seed._embed_all([t for _m, t in stored], embed)
            with get_connection() as conn:
                cur = conn.cursor()
                for (message_id, _t), vector in zip(stored, vectors, strict=True):
                    cur.execute(
                        "UPDATE chat_messages SET embedding = %s, embedded_at = NOW() WHERE id = %s",
                        (vector, message_id),
                    )
    seed._analyze_seeded_tables()
    result.seconds = time.perf_counter() - started
    return result


# --------------------------------------------------------------------------- #
# Extraction: prefetched concurrently, cached on disk
# --------------------------------------------------------------------------- #


class ExtractionStore:
    """Extractions by (extraction model, exact prompt), on disk under ``root``.

    The prompt holds the content, the read-only context and the pinned date,
    so a cached extraction is served only for exactly the call that made it.
    A failed extraction is not stored.
    """

    def __init__(self, root: str | Path | None, *, model: str) -> None:
        self.model = model
        self._rows = _Jsonl(Path(root) / "extractions.jsonl") if root else None
        self._memory: dict[str, dict[str, Any]] = {}

    def key(self, prompt: str) -> str:
        return _key("extraction", self.model, prompt)

    def get(self, prompt: str) -> list[dict[str, Any]] | None:
        k = self.key(prompt)
        row = self._memory.get(k) or (self._rows.get(k) if self._rows else None)
        return None if row is None else list(row["facts"])

    def put(self, prompt: str, extracted: list[dict[str, Any]]) -> None:
        k = self.key(prompt)
        row = {"model": self.model, "facts": extracted}
        self._memory[k] = row
        if self._rows:
            self._rows.put(k, row)


def _extraction_inputs(history: bd.BenchHistory) -> list[tuple[str, str | None, str]]:
    """Every (content, read-only context, date) the extract ingest will ask for."""
    from robothor.memory.conversation_ingest import role_transcripts

    out: list[tuple[str, str | None, str]] = []
    for session in sorted(history.sessions, key=lambda s: s.at):
        date = session.at.date().isoformat()
        for window in transcript_windows(session.turns):
            turns = role_transcripts(window)
            for role, other in (("user", "assistant"), ("assistant", "user")):
                if turns[role].strip():
                    out.append((turns[role], turns[other] or None, date))
    return out


async def prefetch_extractions(
    history: bd.BenchHistory, store: ExtractionStore, *, concurrency: int = 4
) -> dict[str, int]:
    """Run the history's extraction calls ahead of the write path, concurrently."""
    from robothor.memory import facts

    inputs = _extraction_inputs(history)
    gate = asyncio.Semaphore(max(1, concurrency))
    stats = {"inputs": len(inputs), "cached": 0, "computed": 0, "failed": 0}

    async def _one(content: str, context: str | None, date: str) -> None:
        prompt = facts.build_extraction_prompt(content, context=context, reference_date=date)
        if store.get(prompt) is not None:
            stats["cached"] += 1
            return
        async with gate:
            with facts.extraction_reference_date(date):
                result = await facts.extract_facts_detailed(content, context=context)
        if result.error is None:
            store.put(prompt, list(result.facts))
            stats["computed"] += 1
        else:
            stats["failed"] += 1

    await asyncio.gather(*(_one(c, x, d) for c, x, d in inputs))
    return stats


@contextlib.contextmanager
def cached_extraction(store: ExtractionStore) -> Iterator[None]:
    """Serve ``facts.extract_facts_detailed`` from ``store`` while inside.

    A call the store has (same model, same prompt) returns its facts; any
    other runs as usual and is stored when it succeeds. The original is put
    back on exit.
    """
    from robothor.memory import facts

    original = facts.extract_facts_detailed

    async def _served(
        content: str, max_retries: int = 3, *, timeout: float = 180.0, context: str | None = None
    ) -> Any:
        prompt = facts.build_extraction_prompt(content, context=context)
        hit = store.get(prompt)
        if hit is not None:
            return facts.Extraction(hit)
        result = await original(content, max_retries, timeout=timeout, context=context)
        if result.error is None:
            store.put(prompt, list(result.facts))
        return result

    facts.extract_facts_detailed = _served
    try:
        yield
    finally:
        facts.extract_facts_detailed = original


class BenchHandle(WorldHandle):
    """A world handle for an ingested benchmark history.

    Facts are whatever extraction wrote, so every fact id is its own
    (``fact:<id>``); turns map to the benchmark's turn ids.
    """

    def fact_id(self, db_id: int) -> str | None:
        return f"fact:{int(db_id)}"


# --------------------------------------------------------------------------- #
# Caches: ingest once, ask many, never pay twice
# --------------------------------------------------------------------------- #


def code_version(root: Path | None = None) -> str:
    """A hash of the memory system's source: the version a context belongs to.

    The eval harness itself (``eval_*.py``) is left out: changing how a run is
    scored or reported does not change what a system retrieves, so it must
    not throw the cached contexts away.
    """
    base = root if root is not None else Path(__file__).resolve().parent
    h = hashlib.sha256()
    for path in sorted(base.glob("*.py")):
        if path.name.startswith("eval"):
            continue
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


class _Jsonl:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a line cut off by a kill: that entry is redone
                self.rows[row["key"]] = row

    def get(self, key: str) -> dict[str, Any] | None:
        return self.rows.get(key)

    def put(self, key: str, row: dict[str, Any]) -> None:
        row = {"key": key, **row}
        self.rows[key] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()


def _key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:24]


class BenchCache:
    """Contexts, answers and judgements, each keyed by what produced it.

    * context: benchmark file, ingest mode, budget, system, code version, question;
    * answer: its context, the answer model and the reader prompt version;
    * judgement: its answer, the judge model and the judge version.

    A system named in ``carry_forward`` reuses its context from any code
    version (the earliest written), so a cell whose code did not change is
    not re-answered or re-judged; its records say where they came from.
    """

    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root else None
        self.contexts = _Jsonl(self.root / "contexts.jsonl") if self.root else None
        self.answers = _Jsonl(self.root / "answers.jsonl") if self.root else None
        self.judgements = _Jsonl(self.root / "judgements.jsonl") if self.root else None
        self._any_version: dict[str, dict[str, Any]] = {}
        if self.contexts:
            for row in self.contexts.rows.values():
                self._any_version.setdefault(row["base"], row)

    def context(self, base: str, version: str, *, carry_forward: bool) -> dict[str, Any] | None:
        if not self.contexts:
            return None
        row = self.contexts.get(_key(base, version))
        if row is None and carry_forward:
            row = self._any_version.get(base)
        return row

    def put_context(self, base: str, version: str, row: dict[str, Any]) -> None:
        if self.contexts:
            self.contexts.put(_key(base, version), {"base": base, "version": version, **row})
            self._any_version.setdefault(base, self.contexts.rows[_key(base, version)])


def _bundle_to_row(bundle: ContextBundle, dates: dict[str, str]) -> dict[str, Any]:
    return {
        "items": [
            {
                "id": i.id,
                "kind": i.kind,
                "text": i.text,
                "tokens": i.tokens,
                "rank": i.rank,
                "date": dates.get(i.id),
                **({"meta": dict(i.meta)} if i.meta else {}),
            }
            for i in bundle.items
        ],
        "budget": bundle.budget,
        "equal_budget": bundle.equal_budget,
        "retrieval_ms": bundle.retrieval_ms,
    }


def _prompt_items(row: dict[str, Any]) -> list[bd.PromptItem]:
    return [bd.PromptItem(i["kind"], i["text"], i.get("date")) for i in row["items"]]


_POSITION = re.compile(r"#(\d+)")


def _iso_date(value: str | None) -> str | None:
    for parse in (bd.parse_longmemeval_date, bd.parse_locomo_date):
        try:
            return parse(value).isoformat() if value else None
        except ValueError:
            continue
    return None


def bundle_from_row(row: dict[str, Any]) -> ContextBundle:
    """A cached context row as the bundle its system returned.

    Each item's ``meta`` is what the system recorded, plus what the cache
    knows about it: the session date in the benchmark's format (``date``),
    its session (``session``, the turn id before ``#``) and exchange index
    (``position``). An item from a session with no recorded renderer fields
    is described as the conversation segment it is (``kind``,
    ``source_ref``, ``started_at``), so a renderer that groups and dates
    segments reads cached contexts too.
    """
    items = []
    for n, i in enumerate(row.get("items") or [], start=1):
        meta: dict[str, Any] = dict(i.get("meta") or {})
        item_id = str(i.get("id", ""))
        if "#" in item_id:
            session = item_id.rsplit("#", 1)[0]
            pos = _POSITION.search(item_id)
            meta.setdefault("session", session)
            meta.setdefault("position", int(pos.group(1)) if pos else 0)
            if not {"source_ref", "started_at"} & set(meta):
                meta.update(
                    {
                        "kind": "segment",
                        "source_ref": session,
                        "started_at": _iso_date(i.get("date")),
                    }
                )
        if i.get("date"):
            meta.setdefault("date", i["date"])
        items.append(
            ContextItem(
                item_id,
                str(i.get("kind", "")),
                str(i.get("text", "")),
                int(i.get("tokens") or 0),
                int(i.get("rank") or n),
                meta,
            )
        )
    return ContextBundle(
        system=str(row.get("system", "")),
        items=tuple(items),
        budget=row.get("budget"),
        equal_budget=bool(row.get("equal_budget", True)),
        abstained=not items,
        retrieval_ms=float(row.get("retrieval_ms") or 0.0),
    )


# --------------------------------------------------------------------------- #
# Answering and judging
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChatResult:
    """A model reply with its token counts (and cost, when the provider says)."""

    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None


def _as_result(raw: Any, prompt: str) -> ChatResult:
    if isinstance(raw, ChatResult):
        return raw
    from robothor.memory.eval_systems import estimate_tokens

    text = str(raw or "")
    return ChatResult(text, estimate_tokens(prompt), estimate_tokens(text))


def _default_chat(model: str) -> Chat:
    from robothor.memory.eval_hosted import chat_for

    return chat_for(model)


def answer_prompt(
    bench: bd.Benchmark, q: bd.BenchQuestion, items: list[bd.PromptItem]
) -> tuple[str, dict[str, str] | None, int]:
    """The benchmark's reader prompt, its category-5 key (LoCoMo) and max tokens."""
    if bench.name == "locomo":
        text, key = bd.locomo_question_text(q)
        speakers = bench.histories[q.history_id].speakers
        return bd.locomo_answer_prompt(speakers, items, text, q), key, bd.LOCOMO_ANSWER_MAX_TOKENS
    return (
        bd.longmemeval_answer_prompt(q.question, q.as_of, items),
        None,
        LONGMEMEVAL_ANSWER_MAX_TOKENS,
    )


def answer_prompt_for(
    bench: bd.Benchmark, q: bd.BenchQuestion, ctx: dict[str, Any]
) -> tuple[str, dict[str, str] | None, int]:
    """The reader prompt for a cached context row.

    LongMemEval's context text is the eval renderer's
    (``eval_answer.render_bundle``), so the benchmark reads recall the way
    every other answer surface does; the official template wraps it.
    """
    if bench.name == "locomo":
        return answer_prompt(bench, q, _prompt_items(ctx))
    from robothor.memory import eval_answer

    history = eval_answer.render_bundle(bundle_from_row(ctx), now=q.as_of)
    return (
        bd.longmemeval_answer_prompt(q.question, q.as_of, _prompt_items(ctx), history=history),
        None,
        LONGMEMEVAL_ANSWER_MAX_TOKENS,
    )


def _answer_key(
    ctx: dict[str, Any], bench: bd.Benchmark, q: bd.BenchQuestion, model: str, *, prompt: str
) -> str:
    """The answer cache key: the context, the answer model, the reader prompt
    version and the exact prompt, so a changed rendering is answered again."""
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    return _key(ctx.get("key", ""), model, ANSWER_PROMPT_VERSION, digest)


async def _call(
    chat: Chat, prompt: str, *, model: str, max_tokens: int
) -> tuple[ChatResult, float]:
    started = time.perf_counter()
    raw = await chat(
        [{"role": "user", "content": prompt}],
        model=model,
        temperature=0.0,
        max_tokens=max_tokens,
        think=False,
    )
    return _as_result(raw, prompt), (time.perf_counter() - started) * 1000


def _clean_answer(text: str) -> str:
    from robothor.memory.generation import strip_think_blocks

    return strip_think_blocks(text or "").strip()


async def validate_longmemeval_judge(
    questions: Sequence[bd.BenchQuestion], *, chat: Chat, model: str, seed: int = 1
) -> dict[str, Any]:
    """The judge on planted right and wrong responses, before any question runs."""
    planted = bd.longmemeval_planted(questions, seed=seed)
    by_kind: dict[str, dict[str, int]] = {}
    right = right_ok = wrong = wrong_ok = 0
    failures: list[str] = []
    spent = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for p in planted:
        res, _ms = await _call(
            chat,
            bd.longmemeval_judge_prompt(p.question, p.response),
            model=model,
            max_tokens=bd.LONGMEMEVAL_JUDGE_MAX_TOKENS,
        )
        spent["calls"] += 1
        spent["input_tokens"] += res.input_tokens
        spent["output_tokens"] += res.output_tokens
        spent["cost_usd"] += res.cost_usd or 0.0
        ok = bd.longmemeval_label(res.text) == p.expect_yes
        agg = by_kind.setdefault(p.kind, {"n": 0, "ok": 0})
        agg["n"] += 1
        agg["ok"] += int(ok)
        if p.expect_yes:
            right += 1
            right_ok += int(ok)
        else:
            wrong += 1
            wrong_ok += int(ok)
        if not ok and len(failures) < 10:
            failures.append(f"{p.kind} {p.question.id}: {p.response[:60]!r} -> {res.text[:20]!r}")
    rr = right_ok / right if right else 0.0
    wr = wrong_ok / wrong if wrong else 0.0
    return {
        "judge_model": model,
        "n_right": right,
        "n_wrong": wrong,
        "right_marked_right": rr,
        "wrong_marked_wrong": wr,
        "by_kind": by_kind,
        "threshold": VALIDATION_THRESHOLD,
        "passed": rr >= VALIDATION_THRESHOLD and wr >= VALIDATION_THRESHOLD,
        "failures": failures,
        "usage": spent,
    }


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


def evidence_recall(q: bd.BenchQuestion, items: Sequence[dict[str, Any]]) -> float | None:
    """Share of ``q``'s evidence sessions that reached the context sent.

    An item's session is the part of its turn id before ``#`` (a segment
    enters as the turn it starts with; segments never span sessions). None
    for an abstention question or one with no evidence listed.
    """
    if q.abstention or not q.evidence_sessions:
        return None
    present = {str(i.get("id", "")).rsplit("#", 1)[0] for i in items if "#" in str(i.get("id", ""))}
    want = set(q.evidence_sessions)
    return len(want & present) / len(want)


#: Word n-gram length for turn presence, and the share of a turn's n-grams
#: that must appear in the context for the turn to count as delivered.
TURN_NGRAM = 6
TURN_PRESENT_COVERAGE = 0.5

_WORDS = re.compile(r"[a-z0-9]+")


def _ngrams(words: Sequence[str], n: int) -> set[tuple[str, ...]]:
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def turn_coverage(turn: str, context_words: Sequence[str]) -> float:
    """Share of ``turn``'s word ``TURN_NGRAM``-grams found in the context's words.

    Words are lower-cased runs of letters and digits, so case, punctuation,
    role prefixes and line breaks do not decide presence. A turn shorter
    than the n-gram is one gram: present only whole.
    """
    grams = _ngrams(_WORDS.findall(turn.lower()), TURN_NGRAM)
    if not grams:
        return 1.0
    n = min(TURN_NGRAM, max(len(g) for g in grams))
    have = _ngrams(list(context_words), n)
    return sum(1 for g in grams if g in have) / len(grams)


@dataclass(frozen=True)
class TurnEvidence:
    """The answer-bearing turns a context delivered (None: not measured)."""

    recall: float | None
    complete: bool | None
    coverage: tuple[float, ...] = ()


def answer_turn_evidence(q: bd.BenchQuestion, items: Sequence[dict[str, Any]]) -> TurnEvidence:
    """Share of ``q``'s labelled answer turns whose text is in the context sent.

    A turn counts when at least ``TURN_PRESENT_COVERAGE`` of its word
    n-grams appear in the delivered text (after excerpting and packing), so
    a session that is present only as an excerpt without the answer does not
    count. Not measured for an abstention question or one with no labelled
    turn.
    """
    if q.abstention or not q.evidence_turns:
        return TurnEvidence(None, None)
    words = _WORDS.findall("\n".join(str(i.get("text") or "") for i in items).lower())
    cov = tuple(turn_coverage(t, words) for t in q.evidence_turns)
    present = [c >= TURN_PRESENT_COVERAGE for c in cov]
    return TurnEvidence(sum(present) / len(present), all(present), cov)


def dev_split(questions: Sequence[bd.BenchQuestion], *, seed: int) -> dict[str, str]:
    """A recorded, stratified half: ``dev`` to diagnose losses, ``held_out`` never tuned on.

    Within each question type, questions are ordered by a seeded hash of the
    id and the first half (rounded down) is ``dev``.
    """
    by_type: dict[str, list[bd.BenchQuestion]] = {}
    for q in questions:
        by_type.setdefault(q.question_type, []).append(q)
    out: dict[str, str] = {}
    for qs in by_type.values():
        ordered = sorted(qs, key=lambda q: hashlib.sha256(f"{seed}:{q.id}".encode()).hexdigest())
        half = len(ordered) // 2
        for n, q in enumerate(ordered):
            out[q.id] = "dev" if n < half else "held_out"
    return out


DEFAULT_SPLIT_SEED = 885


@dataclass
class _Ledger:
    path: str | None
    fingerprint: dict[str, Any]
    done: dict[tuple[str, str], bd.BenchRecord] = field(default_factory=dict)

    def load(self) -> None:
        if not self.path:
            return
        file = Path(self.path)
        if not file.exists() or not file.read_text(encoding="utf-8").strip():
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(self.fingerprint, sort_keys=True) + "\n", encoding="utf-8")
            return
        lines = file.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        if header != self.fingerprint:
            diff = sorted(k for k in self.fingerprint if header.get(k) != self.fingerprint[k])
            raise ValueError(
                f"progress file {self.path} was written with different parameters "
                f"({', '.join(diff)}); use another --progress path or delete it"
            )
        fields = {f.name for f in dataclasses.fields(bd.BenchRecord)}
        for line in lines[1:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec = bd.BenchRecord(**{k: v for k, v in row.items() if k in fields})
            self.done[(rec.system, rec.question_id)] = rec

    def add(self, rec: bd.BenchRecord) -> None:
        if self.path and not rec.error:
            with Path(self.path).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(dataclasses.asdict(rec), default=str) + "\n")


def session_date_format(bench_name: str) -> Callable[[datetime], str]:
    if bench_name == "locomo":
        return lambda d: (
            d.strftime("%-I:%M %p on %-d %B, %Y").replace("AM", "am").replace("PM", "pm")
        )
    return lambda d: d.strftime("%Y/%m/%d (%a) %H:%M")


async def run_benchmark(
    bench: bd.Benchmark,
    *,
    systems: Sequence[str] = ("recall_today", "recall"),
    questions: Sequence[bd.BenchQuestion] | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    answer_model: str | None = None,
    judge_model: str | None = None,
    ingest_mode: str = "segments",
    tenant_id: str = BENCH_TENANT,
    progress_path: str | None = None,
    cache_dir: str | None = None,
    carry_forward: Sequence[str] = (),
    concurrency: int = 2,
    cleanup: bool = True,
    split_seed: int = DEFAULT_SPLIT_SEED,
    ingest_fn: Callable[..., Awaitable[IngestResult]] | None = None,
    cleanup_fn: Callable[[str], Any] | None = None,
    chat: Chat | None = None,
    judge_chat: Chat | None = None,
    embed_batch: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
    validate_judge: bool = True,
    extract_concurrency: int = 1,
) -> dict[str, Any]:
    """Ingest each history once, ask every system every question on it, score."""
    (tenant,) = _require_eval_tenants([tenant_id])
    if ingest_mode not in INGEST_MODES:
        raise ValueError(f"unknown ingest mode {ingest_mode!r}")
    names = list(systems)
    built = make_systems(names)
    chosen = list(questions if questions is not None else bench.questions)
    answer_model = answer_model or _default_local_model()
    judge_model = judge_model or answer_model
    answer_chat = chat or _default_chat(answer_model)
    judging_chat = judge_chat or chat or _default_chat(judge_model)
    ingest = ingest_fn or ingest_history
    clean = cleanup_fn or cleanup_bench
    cache = BenchCache(cache_dir)
    extraction_store = (
        ExtractionStore(cache_dir, model=_extraction_model()) if ingest_mode == "extract" else None
    )
    version = code_version()
    split = dev_split(bench.questions, seed=split_seed)

    started = time.perf_counter()
    validation: dict[str, Any] | None = None
    if validate_judge and bench.name == "longmemeval":
        validation = await validate_longmemeval_judge(
            bench.questions, chat=judging_chat, model=judge_model
        )
        if not validation["passed"]:
            raise EvalPreconditionError(
                f"judge {judge_model} failed validation: "
                f"{validation['right_marked_right']:.1%} right / "
                f"{validation['wrong_marked_wrong']:.1%} wrong marked correctly "
                f"(each must be >= {VALIDATION_THRESHOLD:.0%}); {validation['failures'][:3]}"
            )

    ledger = _Ledger(
        progress_path,
        {
            "kind": "header",
            "benchmark": bench.name,
            "variant": bench.variant,
            "sha256": bench.sha256,
            "ingest_mode": ingest_mode,
            "token_budget": token_budget,
            "answer_model": answer_model,
            "judge_model": judge_model,
            "answer_prompt_version": ANSWER_PROMPT_VERSION,
            "judge_version": BENCH_JUDGE_VERSION,
        },
    )
    ledger.load()

    records: list[bd.BenchRecord] = []
    errors: list[str] = []
    usage: dict[str, dict[str, float]] = {}
    if validation is not None:
        usage[f"judge_validation:{judge_model}"] = dict(validation["usage"])
    carried: dict[str, int] = {}
    ingests: list[dict[str, Any]] = []
    resumed = 0
    gate = asyncio.Semaphore(max(1, concurrency))

    def _use(role: str, model: str, res: ChatResult | None) -> None:
        if res is None:
            return
        u = usage.setdefault(
            f"{role}:{model}", {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        u["calls"] += 1
        u["input_tokens"] += res.input_tokens
        u["output_tokens"] += res.output_tokens
        u["cost_usd"] += res.cost_usd or 0.0

    def _base(system: str, q: bd.BenchQuestion) -> str:
        return context_base(bench, ingest_mode, token_budget, system, q)

    by_history: dict[str, list[bd.BenchQuestion]] = {}
    for q in chosen:
        by_history.setdefault(q.history_id, []).append(q)

    async def _answer_and_score(
        system: str, q: bd.BenchQuestion, ctx: dict[str, Any]
    ) -> bd.BenchRecord:
        prompt, cat5_key, max_tokens = answer_prompt_for(bench, q, ctx)
        akey = _answer_key(ctx, bench, q, answer_model, prompt=prompt)
        cached = cache.answers.get(akey) if cache.answers else None
        if cached is not None:
            answer, answer_ms = cached["answer"], float(cached["answer_ms"])
        else:
            async with gate:
                res, answer_ms = await _call(
                    answer_chat, prompt, model=answer_model, max_tokens=max_tokens
                )
            _use("answer", answer_model, res)
            answer = _clean_answer(res.text)
            if cache.answers:
                cache.answers.put(
                    akey,
                    {
                        "model": answer_model,
                        "answer": answer,
                        "answer_ms": answer_ms,
                        "input_tokens": res.input_tokens,
                        "output_tokens": res.output_tokens,
                        "cost_usd": res.cost_usd,
                    },
                )
        judge_output, judge_ms = "", 0.0
        if bench.name == "locomo":
            pred = bd.locomo_cat5_answer(answer, cat5_key) if cat5_key else answer
            score = bd.locomo_score(q, pred)
        else:
            jkey = _key(akey, judge_model, BENCH_JUDGE_VERSION)
            jc = cache.judgements.get(jkey) if cache.judgements else None
            if jc is not None:
                judge_output = jc["output"]
            else:
                async with gate:
                    jres, judge_ms = await _call(
                        judging_chat,
                        bd.longmemeval_judge_prompt(q, answer),
                        model=judge_model,
                        max_tokens=bd.LONGMEMEVAL_JUDGE_MAX_TOKENS,
                    )
                _use("judge", judge_model, jres)
                judge_output = jres.text
                if cache.judgements:
                    cache.judgements.put(
                        jkey,
                        {
                            "output": judge_output,
                            "input_tokens": jres.input_tokens,
                            "output_tokens": jres.output_tokens,
                            "cost_usd": jres.cost_usd,
                        },
                    )
            score = 1.0 if bd.longmemeval_label(judge_output) else 0.0
        turns = answer_turn_evidence(q, ctx["items"])
        return bd.BenchRecord(
            system=system,
            question_id=q.id,
            question_type=q.question_type,
            abstention=q.abstention,
            score=score,
            tokens=sum(int(i.get("tokens") or 0) for i in ctx["items"]),
            retrieval_ms=float(ctx.get("retrieval_ms") or 0.0),
            answer_ms=answer_ms,
            judge_ms=judge_ms,
            answer=answer,
            judge_output=judge_output,
            evidence_recall=evidence_recall(q, ctx["items"]),
            answer_turn_recall=turns.recall,
            answer_turns_complete=turns.complete,
        )

    def _error(system: str, q: bd.BenchQuestion, exc: Exception) -> bd.BenchRecord:
        errors.append(f"{system} {q.id}: {type(exc).__name__}: {exc}")
        return bd.BenchRecord(
            system,
            q.id,
            q.question_type,
            q.abstention,
            0.0,
            0,
            0.0,
            0.0,
            error=f"{type(exc).__name__}: {exc}",
        )

    answering: list[asyncio.Task[None]] = []
    excluded: list[dict[str, Any]] = []

    def _exclude(system: str, q: bd.BenchQuestion, reason: str) -> None:
        """A question whose memory could not be built is not asked and not scored."""
        excluded.append(
            {
                "system": system,
                "question_id": q.id,
                "question_type": q.question_type,
                "reason": reason[:300],
            }
        )

    async def _finish(system: str, q: bd.BenchQuestion, ctx: dict[str, Any]) -> None:
        try:
            rec = await _answer_and_score(system, q, ctx)
        except Exception as exc:  # noqa: BLE001 — recorded, never hidden
            rec = _error(system, q, exc)
        records.append(rec)
        ledger.add(rec)

    fmt = session_date_format(bench.name)
    for hid, qs in by_history.items():
        pending = [(s, q) for q in qs for s in built if (s.name, q.id) not in ledger.done]
        for q in qs:
            for s in built:
                if (s.name, q.id) in ledger.done:
                    records.append(ledger.done[(s.name, q.id)])
                    resumed += 1
        if not pending:
            continue
        contexts: dict[tuple[str, str], dict[str, Any]] = {}
        need: list[tuple[Any, bd.BenchQuestion]] = []
        for s, q in pending:
            row = cache.context(_base(s.name, q), version, carry_forward=s.name in carry_forward)
            if row is not None:
                contexts[(s.name, q.id)] = row
                if row.get("version") != version:
                    carried[s.name] = carried.get(s.name, 0) + 1
            else:
                need.append((s, q))
        if need:
            if cleanup:
                clean(tenant)
            try:
                ing = await _ingest_with_retry(
                    ingest,
                    bench.histories[hid],
                    tenant,
                    clean=clean,
                    cleanup=cleanup,
                    mode=ingest_mode,
                    embed_batch=embed_batch,
                    embed_turns=any(s.name == "raw_verbatim" for s, _ in need),
                    date_format=fmt,
                    extraction_store=extraction_store,
                    prefetch_concurrency=extract_concurrency,
                )
                ingests.append(
                    {
                        "history": hid,
                        "sessions": ing.sessions,
                        "turns": ing.turns,
                        "seconds": round(ing.seconds, 2),
                        "facts": ing.facts,
                        "extraction_windows": ing.extraction_windows,
                        "extraction_failed": ing.extraction_failed,
                    }
                )
                # No generated world: the systems read only the tenant and ids.
                handle = BenchHandle(
                    cast("Any", None),
                    ing.seeded,
                    tenant,
                    f"{tenant}-shadow",
                    _embed_one(embed_batch),
                )
                for s, q in need:
                    try:
                        bundle = await s.retrieve(q, world_handle=handle, token_budget=token_budget)
                    except Exception as exc:  # noqa: BLE001 — excluded and counted, never scored
                        _exclude(s.name, q, f"retrieval failed: {type(exc).__name__}: {exc}")
                        continue
                    row = _bundle_to_row(bundle, ing.turn_dates)
                    cache.put_context(_base(s.name, q), version, row)
                    row = {"key": _key(_base(s.name, q), version), "version": version, **row}
                    contexts[(s.name, q.id)] = row
            except Exception as exc:  # noqa: BLE001 — excluded and counted, never scored
                logger.warning("benchmark ingest failed for %s: %s", hid, exc)
                for s, q in need:
                    _exclude(s.name, q, f"ingest failed: {type(exc).__name__}: {exc}")
            finally:
                if cleanup:
                    clean(tenant)
        for s, q in pending:
            ctx = contexts.get((s.name, q.id))
            if ctx is None:
                continue
            if "key" not in ctx:
                ctx = {**ctx, "key": _key(ctx.get("base", ""), ctx.get("version", ""))}
            # Answered while the next history is ingested; the gate bounds
            # how many model calls are in flight.
            answering.append(asyncio.create_task(_finish(s.name, q, ctx)))
    if answering:
        await asyncio.gather(*answering)

    summary = bd.summarise_bench(records, benchmark=bench.name)
    by_split = {
        half: bd.summarise_bench(
            [r for r in records if split.get(r.question_id) == half], benchmark=bench.name
        )
        for half in ("dev", "held_out")
    }
    counts: dict[str, int] = {}
    for q in chosen:
        counts[q.question_type] = counts.get(q.question_type, 0) + 1
    return {
        "suite_id": f"memory-bench-{bench.name}-v1",
        "params": {
            "benchmark": bench.name,
            "variant": bench.variant,
            "source": bench.source,
            "sha256": bench.sha256,
            "systems": names,
            "questions": len(chosen),
            "questions_by_type": dict(sorted(counts.items())),
            "abstention_questions": sum(q.abstention for q in chosen),
            "ingest_mode": ingest_mode,
            "token_budget": token_budget,
            "answer_model": answer_model,
            "judge_model": judge_model,
            "official_judge": bd.LONGMEMEVAL_OFFICIAL_JUDGE
            if bench.name == "longmemeval"
            else None,
            "metric": "judge_accuracy" if bench.name == "longmemeval" else "token_f1",
            "code_version": version,
            "carried_forward": carried,
            "split_seed": split_seed,
            "split_counts": {
                h: sum(1 for q in chosen if split.get(q.id) == h) for h in ("dev", "held_out")
            },
            "resumed": resumed,
            "excluded_questions": len({e["question_id"] for e in excluded}),
            "progress_path": progress_path,
            "cache_dir": cache_dir,
        },
        "judge_validation": validation,
        "systems": summary,
        "by_split": by_split,
        "usage": usage,
        "ingests": ingests,
        "timings": {"total_s": round(time.perf_counter() - started, 2)},
        "errors": errors,
        "excluded": excluded,
        "records": [dataclasses.asdict(r) for r in records],
    }


async def prefill_contexts(
    bench: bd.Benchmark,
    questions: Sequence[bd.BenchQuestion],
    *,
    systems: Sequence[str],
    token_budgets: Sequence[int],
    cache_dir: str,
    ingest_mode: str = "segments",
    tenant_id: str = BENCH_TENANT,
    carry_forward: Sequence[str] = (),
    cleanup: bool = True,
    ingest_fn: Callable[..., Awaitable[IngestResult]] | None = None,
    cleanup_fn: Callable[[str], Any] | None = None,
    embed_batch: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
    extract_concurrency: int = 1,
) -> dict[str, int]:
    """Ingest each history once and cache every system's context at every budget.

    The runs that follow (one per answer model and budget) then find their
    contexts in ``cache_dir`` and ingest nothing. Nothing is answered here.
    """
    (tenant,) = _require_eval_tenants([tenant_id])
    if not cache_dir:
        raise ValueError("prefill_contexts needs a cache_dir")
    built = make_systems(list(systems))
    ingest = ingest_fn or ingest_history
    clean = cleanup_fn or cleanup_bench
    cache = BenchCache(cache_dir)
    store = (
        ExtractionStore(cache_dir, model=_extraction_model()) if ingest_mode == "extract" else None
    )
    version = code_version()
    fmt = session_date_format(bench.name)
    by_history: dict[str, list[bd.BenchQuestion]] = {}
    for q in questions:
        by_history.setdefault(q.history_id, []).append(q)
    stats = {
        "histories": len(by_history),
        "ingested": 0,
        "contexts": 0,
        "cached": 0,
        "errors": 0,
        "failed_histories": 0,
    }
    for hid, qs in by_history.items():
        need = [
            (s, q, b)
            for q in qs
            for s in built
            for b in token_budgets
            if cache.context(
                context_base(bench, ingest_mode, b, s.name, q),
                version,
                carry_forward=s.name in carry_forward,
            )
            is None
        ]
        stats["cached"] += len(qs) * len(built) * len(token_budgets) - len(need)
        if not need:
            continue
        if cleanup:
            clean(tenant)
        try:
            ing = await _ingest_with_retry(
                ingest,
                bench.histories[hid],
                tenant,
                clean=clean,
                cleanup=cleanup,
                mode=ingest_mode,
                embed_batch=embed_batch,
                embed_turns=any(s.name == "raw_verbatim" for s, _q, _b in need),
                date_format=fmt,
                extraction_store=store,
                prefetch_concurrency=extract_concurrency,
            )
            stats["ingested"] += 1
            handle = BenchHandle(
                cast("Any", None), ing.seeded, tenant, f"{tenant}-shadow", _embed_one(embed_batch)
            )
            for s, q, b in need:
                try:
                    bundle = await s.retrieve(cast("Any", q), world_handle=handle, token_budget=b)
                except Exception as exc:  # noqa: BLE001 — counted; the run retries it
                    logger.warning("prefill retrieve failed for %s %s: %s", s.name, q.id, exc)
                    stats["errors"] += 1
                    continue
                cache.put_context(
                    context_base(bench, ingest_mode, b, s.name, q),
                    version,
                    _bundle_to_row(bundle, ing.turn_dates),
                )
                stats["contexts"] += 1
        except Exception as exc:  # noqa: BLE001 — counted; the run retries it
            logger.warning("prefill ingest failed for %s: %s", hid, exc)
            stats["errors"] += 1
            stats["failed_histories"] += 1
        finally:
            if cleanup:
                clean(tenant)
    return stats


def _session_bucket(rec: dict[str, Any]) -> str:
    """A wrong record's bucket as the session-level split put it."""
    if rec.get("abstention"):
        return "abstention"
    er = rec.get("evidence_recall")
    if er is None:
        return "unmeasured"
    return "all_sessions_present" if er >= 1.0 else "sessions_missing"


def rescore_report(
    report: dict[str, Any], bench: bd.Benchmark, *, cache_dir: str | None = None
) -> dict[str, Any]:
    """``report`` with answer-turn evidence computed from its saved contexts.

    No model call and no database: each record's context is the cached one
    its run answered from (the report's code version first, else any
    version), and it is linked only when the answer cached for that context
    and the report's answer model is the record's own answer. An unlinked
    record keeps no turn measurement and is counted. The result carries the
    old (session-level) and new bucket of every wrong answer, and how many
    moved between them.
    """
    p = dict(report.get("params") or {})
    cache = BenchCache(cache_dir or p.get("cache_dir"))
    version = str(p.get("code_version") or "")
    model = str(p.get("answer_model") or "")
    questions = {q.id: q for q in bench.questions}
    fields = {f.name for f in dataclasses.fields(bd.BenchRecord)}
    stats = {"linked": 0, "unlinked": 0, "answer_mismatch": 0, "no_context": 0}
    transitions: dict[str, int] = {}
    records: list[bd.BenchRecord] = []
    out_rows: list[dict[str, Any]] = []
    for raw in report.get("records") or []:
        row = {k: v for k, v in raw.items() if k in fields}
        row["answer_turn_recall"] = None
        row["answer_turns_complete"] = None
        q = questions.get(str(raw.get("question_id")))
        ctx = None
        if q is not None and not raw.get("error"):
            base = context_base(
                bench, str(p.get("ingest_mode")), int(p.get("token_budget") or 0), raw["system"], q
            )
            ctx = cache.context(base, version, carry_forward=True)
        linked = False
        if ctx is None:
            stats["no_context"] += 1
        else:
            ctx_key = ctx.get("key") or _key(ctx.get("base", ""), ctx.get("version", ""))
            assert q is not None
            now_prompt = answer_prompt_for(bench, q, ctx)[0]
            keys = [
                _key(ctx_key, model, 1),  # reports written before the prompt digest
                _answer_key({**ctx, "key": ctx_key}, bench, q, model, prompt=now_prompt),
            ]
            cached = next(
                (a for k in keys if cache.answers and (a := cache.answers.get(k)) is not None),
                None,
            )
            if cached is not None and cached.get("answer") == raw.get("answer"):
                linked = True
                ev = answer_turn_evidence(q, ctx["items"])
                row["answer_turn_recall"] = ev.recall
                row["answer_turns_complete"] = ev.complete
                row["evidence_recall"] = evidence_recall(q, ctx["items"])
            else:
                stats["answer_mismatch"] += 1
        stats["linked" if linked else "unlinked"] += 1
        rec = bd.BenchRecord(**row)
        records.append(rec)
        new = bd.loss_bucket(rec)
        out = {**raw, **dataclasses.asdict(rec), "linked": linked}
        if new is not None:
            old = _session_bucket(raw)
            out["old_bucket"], out["new_bucket"] = old, new
            if linked:
                label = f"{old} -> {new}"
                transitions[label] = transitions.get(label, 0) + 1
        out_rows.append(out)
    split = dev_split(bench.questions, seed=int(p.get("split_seed") or DEFAULT_SPLIT_SEED))
    p["rescored_from"] = str(cache.root) if cache.root else None
    return {
        **report,
        "params": p,
        "systems": bd.summarise_bench(records, benchmark=bench.name),
        "by_split": {
            half: bd.summarise_bench(
                [r for r in records if split.get(r.question_id) == half], benchmark=bench.name
            )
            for half in ("dev", "held_out")
        },
        "records": out_rows,
        "rescore": {**stats, "transitions": dict(sorted(transitions.items()))},
    }


def context_base(
    bench: bd.Benchmark, ingest_mode: str, token_budget: int, system: str, q: bd.BenchQuestion
) -> str:
    """What a context depends on, before the code version."""
    return _key(bench.name, bench.variant, bench.sha256, ingest_mode, token_budget, system, q.id)


#: Dry-run assumptions where no measured value is cached yet.
ANSWER_OUTPUT_ASSUMED = 80
REASONING_OUTPUT_ASSUMED = 1500
JUDGE_OUTPUT_ASSUMED = 2


def estimate_run(
    bench: bd.Benchmark,
    questions: Sequence[bd.BenchQuestion],
    *,
    systems: Sequence[str],
    answer_models: Sequence[str],
    judge_model: str,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    ingest_mode: str = "segments",
    cache_dir: str | None = None,
    carry_forward: Sequence[str] = (),
) -> dict[str, Any]:
    """Tokens and spend per (system, answer model) cell, with nothing ingested or called.

    A cell's answer input is its reader prompt over the cached context when
    one exists, else over a context filled to ``token_budget``. Output is the
    mean the cache measured for that model, else an assumption (stated in the
    result). A cached answer or judgement costs nothing: it is not asked
    again. Tokens are estimated at four characters each.
    """
    from robothor.memory.eval_hosted import cost_usd, is_hosted, is_reasoning, price
    from robothor.memory.eval_systems import estimate_tokens

    cache = BenchCache(cache_dir)
    version = code_version()
    measured: dict[str, list[int]] = {}
    for row in cache.answers.rows.values() if cache.answers else []:
        if row.get("model") and row.get("output_tokens") is not None:
            measured.setdefault(row["model"], []).append(int(row["output_tokens"]))

    def _price(model: str) -> tuple[float, float]:
        return price(model) if is_hosted(model) else (0.0, 0.0)

    def _cost(model: str, tin: int, tout: int) -> float:
        return cost_usd(model, tin, tout) if is_hosted(model) else 0.0

    judged = bench.name == "longmemeval"
    prices = {m.removeprefix("openai/"): list(_price(m)) for m in [*answer_models, judge_model]}
    cells: list[dict[str, Any]] = []
    for model in answer_models:
        out_mean = measured.get(model)
        answer_out = (
            round(sum(out_mean) / len(out_mean))
            if out_mean
            else ANSWER_OUTPUT_ASSUMED + (REASONING_OUTPUT_ASSUMED if is_reasoning(model) else 0)
        )
        for system in systems:
            c: dict[str, Any] = {
                "system": system,
                "answer_model": model,
                "questions": len(questions),
                "cached_contexts": 0,
                "cached_answers": 0,
                "cached_judgements": 0,
                "answer_input_tokens": 0,
                "answer_output_tokens": 0,
                "judge_input_tokens": 0,
                "judge_output_tokens": 0,
            }
            for q in questions:
                base = context_base(bench, ingest_mode, token_budget, system, q)
                ctx = cache.context(base, version, carry_forward=system in carry_forward)
                if ctx is not None:
                    c["cached_contexts"] += 1
                    prompt, _k, _m = answer_prompt_for(bench, q, ctx)
                    ain = estimate_tokens(prompt)
                    akey = _answer_key(ctx, bench, q, model, prompt=prompt)
                else:
                    prompt, _k, _m = answer_prompt(bench, q, [])
                    ain = estimate_tokens(prompt) + token_budget
                    akey = ""
                cached = cache.answers.get(akey) if (akey and cache.answers) else None
                if cached is not None:
                    c["cached_answers"] += 1
                else:
                    c["answer_input_tokens"] += ain
                    c["answer_output_tokens"] += answer_out
                if not judged:
                    continue
                jkey = _key(akey, judge_model, BENCH_JUDGE_VERSION) if akey else ""
                if jkey and cache.judgements and cache.judgements.get(jkey) is not None:
                    c["cached_judgements"] += 1
                    continue
                response = cached["answer"] if cached else "x" * (4 * ANSWER_OUTPUT_ASSUMED)
                c["judge_input_tokens"] += estimate_tokens(bd.longmemeval_judge_prompt(q, response))
                c["judge_output_tokens"] += JUDGE_OUTPUT_ASSUMED
            c["answer_cost_usd"] = _cost(model, c["answer_input_tokens"], c["answer_output_tokens"])
            c["judge_cost_usd"] = _cost(
                judge_model, c["judge_input_tokens"], c["judge_output_tokens"]
            )
            c["cost_usd"] = c["answer_cost_usd"] + c["judge_cost_usd"]
            c["answer_output_per_question"] = answer_out
            c["answer_output_measured"] = bool(out_mean)
            cells.append(c)
    return {
        "benchmark": bench.name,
        "variant": bench.variant,
        "questions": len(questions),
        "judge_model": judge_model,
        "token_budget": token_budget,
        "prices": prices,
        "cells": cells,
        "total_cost_usd": sum(c["cost_usd"] for c in cells),
        "assumptions": [
            "tokens estimated at 4 characters each (the repo's estimate_tokens)",
            f"answer output {ANSWER_OUTPUT_ASSUMED} tokens per question unless measured; "
            f"a reasoning model adds {REASONING_OUTPUT_ASSUMED} reasoning tokens",
            f"judge output {JUDGE_OUTPUT_ASSUMED} tokens (the official ceiling is "
            f"{bd.LONGMEMEVAL_JUDGE_MAX_TOKENS})",
            "no cached context: the context is assumed filled to the token budget",
            "prices are list prices per 1M tokens, input / output, in USD",
        ],
    }


def format_estimate(est: dict[str, Any]) -> str:
    lines = [
        f"Dry run: {est['benchmark']} ({est['variant']}), {est['questions']} questions, "
        f"judge {est['judge_model']}, context budget {est['token_budget']} tokens.",
        "",
        "| System | Answer model | Cached ctx / ans / judg | Answer in / out tokens | "
        "Judge in / out tokens | Spend |",
        "|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {c['system']} | {c['answer_model']} | {c['cached_contexts']} / "
        f"{c['cached_answers']} / {c['cached_judgements']} | {c['answer_input_tokens']:,} / "
        f"{c['answer_output_tokens']:,} | {c['judge_input_tokens']:,} / "
        f"{c['judge_output_tokens']:,} | ${c['cost_usd']:.2f} |"
        for c in est["cells"]
    )
    priced = ", ".join(f"{k} {v[0]} / {v[1]}" for k, v in est["prices"].items())
    lines += [
        "",
        f"Total: ${est['total_cost_usd']:.2f}.",
        f"Prices (USD per 1M tokens, in / out): {priced}.",
        "Assumptions: " + "; ".join(est["assumptions"]) + ".",
    ]
    return "\n".join(lines) + "\n"


def _extraction_model() -> str:
    """The model the product's extraction calls, as the eval names it."""
    from robothor.memory.eval_answer import _default_answer_model

    return _default_answer_model()


def _default_local_model() -> str:
    from robothor.llm import ollama

    return ollama.GENERATION_MODEL


def _embed_one(
    embed_batch: Callable[[list[str]], Awaitable[list[list[float]]]] | None,
) -> Callable[[str], Awaitable[list[float]]]:
    async def one(text: str) -> list[float]:
        if embed_batch is not None:
            return (await embed_batch([text]))[0]
        from robothor.llm import ollama

        return (await ollama.get_embeddings_batch_async([text]))[0]

    return one


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _pct(x: Any) -> str:
    return "–" if x is None else f"{float(x):.1%}"


def _complete(b: dict[str, Any]) -> str:
    n = b.get("answer_turn_n") or 0
    return f"{b.get('answer_turns_complete', 0)} / {n}" if n else "–"


def format_bench_markdown(report: dict[str, Any]) -> str:
    p = report.get("params", {})
    metric = "mean token F1" if p.get("benchmark") == "locomo" else "judge accuracy"
    lines = [
        f"# Memory eval: {p.get('benchmark')} ({p.get('variant')})",
        "",
        f"{p.get('questions')} questions ({', '.join(f'{k} {v}' for k, v in (p.get('questions_by_type') or {}).items())}); "
        f"ingest mode `{p.get('ingest_mode')}`; context budget {p.get('token_budget')} tokens.",
        f"Answer model `{p.get('answer_model')}`, judge `{p.get('judge_model')}`; metric: {metric}.",
    ]
    if p.get("ingest_mode") == "segments":
        lines.append(
            "Ingest mode `segments`: extracted facts are absent in this mode, so this measures "
            "raw-segment recall plus ranking, not the whole product."
        )
    jv = report.get("judge_validation")
    if jv:
        lines.append(
            f"Judge validation on planted responses: {_pct(jv.get('right_marked_right'))} of "
            f"{jv.get('n_right')} right marked right, {_pct(jv.get('wrong_marked_wrong'))} of "
            f"{jv.get('n_wrong')} wrong marked wrong."
        )
    lines += [
        "",
        "| System | Type | n | Score [95% CI] | Answer-turn evidence | All answer turns | "
        "Session evidence | Errors | Tokens (mean / median) | Retrieval p50 / p95 ms | Answer p50 ms |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for system, m in (report.get("systems") or {}).items():
        rows = [*m.get("by_type", {}).items(), ("all", m["overall"])]
        if m.get("abstention", {}).get("n"):
            rows.append(("abstention", m["abstention"]))
        for name, b in rows:
            lo, hi = b.get("wilson", (0.0, 0.0))
            lines.append(
                f"| {system} | {name} | {b.get('n')} | {_pct(b.get('accuracy'))} [{lo:.0%}–{hi:.0%}] | "
                f"{_pct(b.get('answer_turn_recall'))} | "
                f"{_complete(b)} | "
                f"{_pct(b.get('evidence_recall'))} | "
                f"{b.get('errors', 0)} | {b.get('mean_tokens', 0):.0f} / {b.get('median_tokens', 0):.0f} | "
                f"{b.get('retrieval_p50_ms', 0):.0f} / {b.get('retrieval_p95_ms', 0):.0f} | "
                f"{b.get('answer_p50_ms', 0):.0f} |"
            )
        lines.append(
            f"| {system} | task-averaged | | {_pct(m.get('task_averaged'))} | | | | | | | |"
        )
        ov = m["overall"]
        lo_ = ov.get("losses") or {}
        lines.append(
            f"| {system} | losses | | answer turns absent {lo_.get('answer_turns_absent', 0)} "
            f"(retrieval or packing); answer turns present, answer wrong "
            f"{lo_.get('answer_turns_present_answer_wrong', 0)} (answer step); "
            f"abstention {lo_.get('abstention', 0)} | | | "
            f"session level: all sessions present {ov.get('wrong_with_evidence', 0)}, "
            f"missing evidence {ov.get('wrong_missing_evidence', 0)} | | | | |"
        )
    halves = report.get("by_split") or {}
    if any(halves.values()):
        lines += [
            "",
            f"Split (seed {p.get('split_seed')}): dev is for diagnosis, held_out is never tuned on.",
            "",
        ]
        lines += ["| System | Half | n | Score | Task-averaged |", "|---|---|---|---|---|"]
        for half, systems in halves.items():
            for system, m in systems.items():
                lines.append(
                    f"| {system} | {half} | {m['overall']['n']} | {_pct(m['overall']['accuracy'])} | "
                    f"{_pct(m.get('task_averaged'))} |"
                )
    rs = report.get("rescore")
    if rs:
        lines += [
            "",
            f"Re-scored from saved contexts (`{p.get('rescored_from')}`), no model call: "
            f"{rs.get('linked', 0)} records linked to the context they were answered from, "
            f"{rs.get('unlinked', 0)} not ({rs.get('no_context', 0)} no context, "
            f"{rs.get('answer_mismatch', 0)} cached answer differs). Wrong answers, "
            "old (session-level) bucket -> new (answer-turn) bucket:",
            "",
            "| Old -> new | n |",
            "|---|---|",
        ]
        lines += [f"| {k} | {v} |" for k, v in (rs.get("transitions") or {}).items()]
    if p.get("carried_forward"):
        lines += [
            "",
            f"Carried forward from an earlier code version (not re-run): {p['carried_forward']}.",
        ]
    if report.get("usage"):
        lines += [
            "",
            "| Calls | n | Input tokens | Output tokens | Cost (provider token counts at list prices) |",
            "|---|---|---|---|---|",
        ]
        for k, u in report["usage"].items():
            lines.append(
                f"| {k} | {u['calls']} | {u['input_tokens']:.0f} | {u['output_tokens']:.0f} | ${u['cost_usd']:.4f} |"
            )
    lines += [
        "",
        "**Comparability.** Published results on this benchmark name their own answer and judge "
        "models; this run's are named above. A local judge stands in for the official hosted judge "
        f"(`{p.get('official_judge') or 'none: the metric is deterministic'}`) only when the judge "
        "named above is not that model. Full context, subset size and context budget are stated "
        "beside every number; compare only rows with the same answer model and judge.",
    ]
    if report.get("excluded"):
        ids = sorted({e["question_id"] for e in report["excluded"]})
        lines += [
            "",
            f"Excluded (not asked, not scored): {len(ids)} question(s) whose memory could not be "
            "built (failed ingest or retrieval, after retries); they are in no number above. "
            "See `excluded` in the JSON.",
        ]
    if report.get("errors"):
        lines += ["", f"Errors: {len(report['errors'])} (scored as wrong; see the JSON)."]
    return "\n".join(lines) + "\n"
