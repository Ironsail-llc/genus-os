"""Identity: one node per person or organisation, anchored to the CRM.

A *mention* is a name as it appeared in a fact (its surface form). Resolving it
means deciding which ``memory_entities`` row it refers to, or deliberately
leaving it unresolved. Every decision is recorded in
``memory_resolution_decisions`` with its method, confidence and evidence, and a
mention (``memory_mentions``) carries the decision it was resolved by. Nothing
ever rewrites entity ids: identity lives in decisions and mentions, so a wrong
decision is undone by reversing it (:func:`reverse_decision`), not by moving
rows. There is no entity-level merge and no ``canonical_entity_id``.

The rules, in order, first match wins (:func:`decide` is the whole table):

a. **Strong key** — a CRM id, an email address the CRM holds, or a channel
   identifier from ``contact_identifiers`` names one CRM record: its node.
   Keys that disagree leave the mention unresolved.
b. **Full name** — at least two name tokens, neither end an initial, matching
   exactly one node of a compatible type by name or alias (case and
   whitespace ignored).
c. **Context** — a full name matching several nodes, or only a nickname variant
   of one ("Bob Rivera" for "Robert Rivera"), resolves only when supporting
   context singles out one: the same organisation (named in the context or in
   the same fact), the same non-free-mail email domain, or evidence that the
   candidate was already mentioned in the same conversation.
d. **Single token** — a first name, nickname or initials (or a name with an
   initial at either end) never merges on its own, whatever any fuzzy score
   says. It resolves only by (a), or when exactly one candidate is already
   evidenced in the same conversation. The operator's own node is never
   reached this way: the operator is in nearly every conversation.
e. **Provisional node** — no candidate at all, and a full name or a known
   non-person type: an ordinary unlinked node is created. Never a person node
   from a single token, never a node of unknown type.

Everything here is tenant-scoped: ``tenant_id`` is a required argument and no
default tenant is ever substituted. Resolution makes no model call.

Memory never writes to the CRM.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection, tenant_scope
from robothor.memory.contact_matching import first_name_variants
from robothor.memory.entities import VALID_ENTITY_TYPES, is_junk_entity_name

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

logger = logging.getLogger(__name__)

PERSON = "person"
ORGANIZATION = "organization"

#: Confidence per way of resolving. A context resolution is always below 1.
CONFIDENCE = {
    "strong_key": 1.0,
    "full_name": 0.95,
    "exact": 0.9,
    "create": 1.0,
    "context_full_name": 0.9,
    "context_nickname": 0.85,
    "context_single_token": 0.75,
}

#: Domains anyone can have an address at: sharing one says nothing about who.
FREE_MAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "fastmail.com",
        "hey.com",
    }
)

#: Methods whose decisions count as evidence that an entity appeared in a
#: conversation. A ``context`` decision is not: chaining guesses compounds them.
EVIDENCE_METHODS: tuple[str, ...] = ("strong_key", "full_name", "operator")

#: The columns a lookup returns. ``embedding`` (1024 floats) is never among them.
ENTITY_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "entity_type",
    "aliases",
    "metadata",
    "first_seen",
    "last_seen",
    "mention_count",
    "tenant_id",
    "crm_person_id",
    "crm_company_id",
    "scope_kind",
    "scope_id",
)

#: How far a chain of CRM merges is followed (A merged into B merged into C ...).
_MAX_MERGE_HOPS = 5

#: Strong-key kinds a mention may carry.
STRONG_KEY_KINDS: tuple[str, ...] = ("crm_person_id", "crm_company_id", "email", "channel")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_WS_RE = re.compile(r"\s+")
_MAX_NAME = 255

UNIQUE_INDEX_NOTE = (
    "memory_mentions has no unique key on (fact_id, lower(surface_form)); the backfill "
    "enforces it in code with a guarded insert under a per-tenant advisory lock. "
    "A unique index is wanted."
)


# ═══ public dataclasses ══════════════════════════════════════════════════


@dataclass(frozen=True)
class MentionInput:
    """A name to resolve.

    ``strong_keys`` may hold ``crm_person_id``, ``crm_company_id``, ``email`` and
    ``channel`` (``"<channel>:<identifier>"`` as stored in
    ``contact_identifiers``, e.g. ``"telegram:4242"``).

    ``context`` may hold ``organisation`` (a name), ``email_domain``,
    ``conversation_ref`` (the ``session_key`` the write belongs to; facts carry
    it in ``metadata->>'session_key'``), ``co_mentions`` (other surface forms in
    the same fact), and the scope of the write itself — ``scope_kind``,
    ``scope_id`` and ``person_id`` — which bounds the evidence a context
    resolution may use. Absent scope means a shared write with no person scope:
    only shared, unscoped evidence is used.
    """

    surface_form: str
    entity_type: str | None = None
    strong_keys: Mapping[str, str] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedMention:
    """The outcome for one mention. ``entity_id`` is ``None`` when unresolved."""

    entity_id: int | None
    decision_id: int
    method: str
    confidence: float
    canonical_name: str | None


@dataclass(frozen=True)
class Ambiguity:
    """A read-side lookup that matched more than one node."""

    surface_form: str
    candidates: tuple[dict[str, Any], ...]


# ═══ the pure rule layer ═════════════════════════════════════════════════


@dataclass(frozen=True)
class Candidate:
    """A node a mention might refer to, and how it matched.

    ``match``: ``name`` or ``alias`` (exact, normalised), ``nickname`` (a
    nickname-table variant of the first name), ``first_name`` (a single token
    equal to the node's first name), ``initial`` (an initial plus a name).
    ``organisations`` and ``email_domains`` come from the node's CRM record,
    normalised; ``in_conversation`` is true when a visible fact of the same
    conversation already mentions it on strong evidence.
    """

    entity_id: int
    name: str
    entity_type: str
    match: str
    organisations: frozenset[str] = frozenset()
    email_domains: frozenset[str] = frozenset()
    in_conversation: bool = False
    is_operator: bool = False


@dataclass(frozen=True)
class StrongHit:
    """A strong key that named one node."""

    kind: str
    entity_id: int
    entity_type: str
    name: str


@dataclass(frozen=True)
class Outcome:
    """What the rules decided, before anything is written.

    ``basis`` identifies the evidence a decision rests on; an identical live
    decision with the same basis is reused rather than recorded again.
    ``create_type`` is set when a provisional node should be created.
    """

    entity_id: int | None
    method: str
    confidence: float
    basis: str
    reason: str
    evidence: Mapping[str, Any]
    create_type: str | None = None


def normalize_surface(text: str | None) -> str:
    """Lowercase, Unicode-compatibility-normalised, whitespace collapsed."""
    if not text:
        return ""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", str(text))).strip().lower()


def _clean(text: str) -> str:
    """The surface form as written, whitespace collapsed (case kept)."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def _is_initial(token: str) -> bool:
    return len(token.strip(".")) <= 1


def _norm_type(entity_type: str | None) -> str | None:
    if not entity_type:
        return None
    t = str(entity_type).strip().lower()
    if t in ("organisation", "org", "company"):
        return ORGANIZATION
    return t


def _ids(cands: Sequence[Candidate]) -> list[int]:
    return sorted({c.entity_id for c in cands})


def _unresolved(
    reason: str, method: str, cands: Sequence[Candidate], ev: dict[str, Any], conv: str | None
) -> Outcome:
    ids = _ids(cands)
    basis = f"unresolved:{reason}:{','.join(map(str, ids))}:{conv or ''}"
    return Outcome(None, method, 0.0, basis, reason, {**ev, "candidates": ids, "rule": reason})


def _signals(c: Candidate, ctx: Mapping[str, Any], conv: str | None) -> dict[str, str]:
    """Which pieces of context support ``c``, with the value that matched."""
    found: dict[str, str] = {}
    orgs = [normalize_surface(ctx.get("organisation"))] if ctx.get("organisation") else []
    orgs += [normalize_surface(m) for m in ctx.get("co_mentions") or [] if m]
    for org in orgs:
        if org and org in c.organisations:
            found["organisation"] = org
            break
    domain = str(ctx.get("email_domain") or "").strip().lower().lstrip("@")
    if domain and domain not in FREE_MAIL_DOMAINS and domain in c.email_domains:
        found["email_domain"] = domain
    if conv and c.in_conversation:
        found["conversation"] = conv
    return found


def _by_context(
    cands: Sequence[Candidate],
    ctx: Mapping[str, Any],
    conv: str | None,
    *,
    confidence: float,
    rule: str,
    ev: dict[str, Any],
) -> Outcome:
    support = {c.entity_id: (c, _signals(c, ctx, conv)) for c in cands}
    support = {k: v for k, v in support.items() if v[1]}
    ids = _ids(cands)
    if len(support) == 1:
        eid, (_c, sig) = next(iter(support.items()))
        values = ";".join(f"{k}={v}" for k, v in sorted(sig.items()))
        return Outcome(
            eid,
            "context",
            confidence,
            f"context:{','.join(map(str, ids))}:{values}",
            "context",
            {
                **ev,
                "rule": rule,
                "candidates": ids,
                "signals": {str(eid): sorted(sig)},
                "support": dict(sorted(sig.items())),
            },
        )
    reason = "needs_context" if rule == "nickname" else "ambiguous"
    out = _unresolved(reason, "context", cands, ev, conv)
    if support:
        out = Outcome(
            None,
            out.method,
            0.0,
            out.basis,
            out.reason,
            {**out.evidence, "supported": sorted(support)},
        )
    return out


def decide(
    mention: MentionInput,
    candidates: Sequence[Candidate],
    *,
    strong_hits: Sequence[StrongHit] = (),
    blocked: bool = False,
) -> Outcome:
    """Apply the identity rules to one mention and its candidate nodes.

    Pure: no database, no model. ``blocked`` means a node of this name exists
    but cannot be used (its CRM record is gone and no merge stands), so no
    duplicate may be created beside it.
    """
    surface = normalize_surface(mention.surface_form)
    mtype = _norm_type(mention.entity_type)
    ctx = mention.context or {}
    conv = str(ctx["conversation_ref"]) if ctx.get("conversation_ref") else None
    ev: dict[str, Any] = {"normalized": surface}

    # (a) strong keys — they name the record whatever the surface form says
    hit_ids = sorted({h.entity_id for h in strong_hits})
    if len(hit_ids) > 1:
        out = _unresolved("strong_key_conflict", "strong_key", (), ev, None)
        return Outcome(
            None,
            out.method,
            0.0,
            f"unresolved:strong_key_conflict:{hit_ids}",
            out.reason,
            {**out.evidence, "candidates": hit_ids, "keys": sorted({h.kind for h in strong_hits})},
        )
    if hit_ids:
        hit = strong_hits[0]
        keys = sorted({h.kind for h in strong_hits})
        if mtype is not None and mtype != _norm_type(hit.entity_type):
            return Outcome(
                None,
                "strong_key",
                0.0,
                f"unresolved:type_mismatch:{hit.entity_id}",
                "type_mismatch",
                {**ev, "candidates": [hit.entity_id], "keys": keys},
            )
        return Outcome(
            hit.entity_id,
            "strong_key",
            CONFIDENCE["strong_key"],
            "strong_key",
            "strong_key",
            {**ev, "keys": keys},
        )

    if is_junk_entity_name(mention.surface_form):
        return _unresolved("junk", "exact", (), ev, None)

    tokens = surface.split()
    compat = [c for c in candidates if mtype is None or _norm_type(c.entity_type) == mtype]
    exact = [c for c in compat if c.match in ("name", "alias")]

    eff = mtype
    if mtype is None:
        types = {_norm_type(c.entity_type) for c in exact} or {
            _norm_type(c.entity_type) for c in compat
        }
        if len(types) > 1:
            return _unresolved("type_ambiguous", "context", compat, ev, conv)
        eff = next(iter(types)) if types else None

    person_like = eff in (PERSON, None)
    full = len(tokens) >= 2 and not (
        person_like and (_is_initial(tokens[0]) or _is_initial(tokens[-1]))
    )

    if full:
        # (b) exact full name, (c) ambiguity needs context
        if len(exact) == 1:
            c = exact[0]
            return Outcome(
                c.entity_id,
                "full_name",
                CONFIDENCE["full_name"],
                "full_name",
                "full_name",
                {**ev, "candidates": [c.entity_id], "match": c.match},
            )
        if len(exact) > 1:
            return _by_context(
                exact,
                ctx,
                conv,
                confidence=CONFIDENCE["context_full_name"],
                rule="full_name_ambiguous",
                ev=ev,
            )
        nick = [c for c in compat if c.match == "nickname"]
        if nick:
            return _by_context(
                nick, ctx, conv, confidence=CONFIDENCE["context_nickname"], rule="nickname", ev=ev
            )
        # (e) nothing to resolve to
        if blocked:
            return _unresolved("blocked", "exact", (), ev, None)
        if eff is None or eff not in VALID_ENTITY_TYPES:
            return _unresolved("no_type", "exact", (), ev, None)
        return Outcome(None, "exact", CONFIDENCE["create"], "exact", "create", ev, create_type=eff)

    if eff is not None and eff != PERSON and len(tokens) == 1:
        # A single-token name of another type ("Globex"): an exact match is enough,
        # unless the type was only inferred and the token is also someone's first name.
        if mtype is None and any(_norm_type(c.entity_type) == PERSON for c in compat):
            return _unresolved("type_ambiguous", "context", compat, ev, conv)
        if len(exact) == 1:
            c = exact[0]
            return Outcome(
                c.entity_id,
                "exact",
                CONFIDENCE["exact"],
                "exact",
                "exact",
                {**ev, "candidates": [c.entity_id], "match": c.match},
            )
        if len(exact) > 1:
            return _unresolved("ambiguous", "context", exact, ev, conv)
        if blocked:
            return _unresolved("blocked", "exact", (), ev, None)
        if mtype is None or eff not in VALID_ENTITY_TYPES:
            return _unresolved("no_type", "exact", (), ev, None)
        return Outcome(None, "exact", CONFIDENCE["create"], "exact", "create", ev, create_type=eff)

    # (d) a single token, or a name with an initial: conversation evidence only
    pool = [c for c in compat if _norm_type(c.entity_type) == PERSON]
    reason = "single_token" if len(tokens) == 1 else "partial_name"
    if conv:
        # Count every evidenced candidate first, then refuse the operator: dropping
        # the operator before counting would hand their first name to a namesake.
        in_conv = [c for c in pool if c.in_conversation]
        if len(in_conv) == 1 and not in_conv[0].is_operator:
            c = in_conv[0]
            ids = _ids(pool)
            return Outcome(
                c.entity_id,
                "context",
                CONFIDENCE["context_single_token"],
                f"context:{','.join(map(str, ids))}:conversation={conv}",
                "context",
                {
                    **ev,
                    "rule": reason,
                    "candidates": ids,
                    "signals": {str(c.entity_id): ["conversation"]},
                    "support": {"conversation": conv},
                },
            )
    return _unresolved(reason, "context", pool, ev, conv)


# ═══ database plumbing ═══════════════════════════════════════════════════


def _require_tenant(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("identity: tenant_id is required (no default tenant is substituted)")
    return tenant_id


@contextmanager
def _cursor(
    tenant_id: str, cur: Any = None, *, read_only: bool = False, guard: bool = False
) -> Iterator[Any]:
    """A RealDictCursor in the caller's transaction, or in a connection of our own.

    With ``read_only`` the transaction is declared READ ONLY, so a dry run cannot
    write even by mistake (a sequence ``nextval`` would fail). With ``guard`` the
    work runs under a savepoint, so a failure cannot abort a transaction this
    connection happens to share (the CRM hook path).
    """
    if cur is not None:
        own = cur.connection.cursor(cursor_factory=RealDictCursor)
        try:
            yield own
        finally:
            own.close()
        return
    with tenant_scope(tenant_id), get_connection() as conn:
        c = conn.cursor(cursor_factory=RealDictCursor)
        if read_only:
            c.execute("SET TRANSACTION READ ONLY")
        savepoint = guard and not getattr(conn, "autocommit", False)
        if savepoint:
            c.execute("SAVEPOINT robothor_identity")
        try:
            yield c
        except BaseException:
            if savepoint:
                c.execute("ROLLBACK TO SAVEPOINT robothor_identity")
            raise
        if savepoint:
            c.execute("RELEASE SAVEPOINT robothor_identity")


def _schema_ready(cur: Any) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'memory_entities' AND column_name = 'crm_person_id'"
    )
    return cur.fetchone() is not None


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _scope_of(ctx: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    kind = str(ctx.get("scope_kind") or "shared")
    sid = str(ctx["scope_id"]) if ctx.get("scope_id") is not None else None
    pid = str(ctx["person_id"]) if ctx.get("person_id") else None
    if kind == "shared":
        sid = None
    return kind, sid, pid if pid and _is_uuid(pid) else None


def _visible(row: Mapping[str, Any], scope: tuple[str, str | None, str | None]) -> bool:
    kind = row.get("scope_kind") or "shared"
    if kind == "shared":
        return True
    return kind == scope[0] and row.get("scope_id") == scope[1]


def _emails_of(person: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    raw = [person.get("email")]
    extra = person.get("additional_emails")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = []
    if isinstance(extra, list):
        raw += extra
    for e in raw:
        if isinstance(e, str) and _EMAIL_RE.match(e.strip()):
            low = e.strip().lower()
            if low not in out:
                out.append(low)
    return out


def _full_name(person: Mapping[str, Any]) -> str:
    return _clean(f"{person.get('first_name') or ''} {person.get('last_name') or ''}")


def _crm_name(kind: str, row: Mapping[str, Any]) -> str:
    return _full_name(row) if kind == PERSON else _clean(str(row.get("name") or ""))


def _entity_type_for(kind: str) -> str:
    return PERSON if kind == PERSON else ORGANIZATION


def _crm_col(kind: str) -> str:
    return "crm_person_id" if kind == PERSON else "crm_company_id"


def _kind(kind: str) -> str:
    k = kind.strip().lower()
    if k in ("person", "people"):
        return PERSON
    if k in ("company", "companies", "organization", "organisation"):
        return "company"
    raise ValueError(f"identity: unknown CRM record kind {kind!r}")


def _insert_decision(
    cur: Any,
    tenant_id: str,
    *,
    surface: str,
    entity_type: str | None,
    entity_id: int | None,
    method: str,
    confidence: float,
    evidence: Mapping[str, Any],
) -> int:
    cur.execute(
        "INSERT INTO memory_resolution_decisions (tenant_id, surface_form, entity_type, "
        "resolved_entity_id, method, confidence, evidence) VALUES (%s, %s, %s, %s, %s, %s, "
        "%s::jsonb) RETURNING id",
        (
            tenant_id,
            surface,
            entity_type,
            entity_id,
            method,
            confidence,
            json.dumps(evidence, default=str),
        ),
    )
    return int(cur.fetchone()["id"])


def _live_decision_by_basis(cur: Any, tenant_id: str, basis: str) -> dict[str, Any] | None:
    cur.execute(
        "SELECT id, resolved_entity_id FROM memory_resolution_decisions WHERE tenant_id = %s "
        "AND evidence->>'basis' = %s AND reversed_at IS NULL AND superseded_by IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (tenant_id, basis),
    )
    row = cur.fetchone()
    return dict(row) if row else None


# ═══ CRM seeding ═════════════════════════════════════════════════════════


def _disambiguated(name: str, crm_id: str) -> str:
    suffix = f" #{str(crm_id)[:8]}"
    return name[: _MAX_NAME - len(suffix)] + suffix


def _name_free(cur: Any, tenant_id: str, name: str, etype: str, own_id: int | None) -> bool:
    cur.execute(
        "SELECT 1 FROM memory_entities WHERE tenant_id = %s AND name = %s AND entity_type = %s "
        "AND id IS DISTINCT FROM %s",
        (tenant_id, name, etype, own_id),
    )
    return cur.fetchone() is None


def _is_full_person_name(name: str) -> bool:
    """Two or more tokens and no initial at either end: the bar rule (b) sets."""
    tokens = normalize_surface(name).split()
    return len(tokens) >= 2 and not (_is_initial(tokens[0]) or _is_initial(tokens[-1]))


def _sync_one(
    cur: Any,
    tenant_id: str,
    kind: str,
    row: Mapping[str, Any],
    *,
    ambiguous_name: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Keep one CRM record's node current. Returns the action taken (or planned).

    No CRM contact data (emails, channel handles, domains) is copied into the
    node: ``get_entity`` applies no data scope, and the CRM withholds those
    fields from restricted readers. Strong keys are read from the CRM itself at
    resolution time instead.

    A person whose CRM name is not a full name (a single token, or an initial at
    either end) never takes over an existing node, and its node is always named
    ``"<name> #<crm id prefix>"`` with no bare-name alias, so the legacy
    exact-name writer cannot pour every extracted "Alex" into one CRM record.
    """
    crm_id = str(row["id"])
    etype = _entity_type_for(kind)
    col = _crm_col(kind)
    name = _crm_name(kind, row)
    action: dict[str, Any] = {"kind": kind, "crm_id": crm_id, "name": name, "entity_id": None}
    if not name or is_junk_entity_name(name):
        return {**action, "action": "skipped"}
    full = kind != PERSON or _is_full_person_name(name)

    def _naming(own_id: int | None, current: str | None) -> tuple[str, list[str]]:
        """The node name and the names the CRM implies as aliases."""
        if full and (current == name or _name_free(cur, tenant_id, name, etype, own_id)):
            return name, []
        return _disambiguated(name, crm_id), [name] if full else []

    cur.execute(
        f"SELECT id, name, aliases, metadata FROM memory_entities WHERE tenant_id = %s "  # noqa: S608
        f"AND {col} = %s ORDER BY id LIMIT 1",
        (tenant_id, crm_id),
    )
    linked = cur.fetchone()
    if linked:
        eid = int(linked["id"])
        meta = linked["metadata"] if isinstance(linked["metadata"], dict) else {}
        old_crm = {a.lower() for a in meta.get("crm_aliases") or []}
        new_name, crm_set = _naming(eid, linked["name"])
        kept = [a for a in (linked["aliases"] or []) if a.lower() not in old_crm]
        # A renamed record keeps its previous name as an alias: facts already
        # name the person by it. Only a full name is kept (see _is_full_person_name).
        previous = str(meta.get("crm_name") or linked["name"] or "")
        if (
            previous
            and normalize_surface(previous) != normalize_surface(name)
            and (kind != PERSON or _is_full_person_name(previous))
            and "#" not in previous
            and previous.lower() not in {k.lower() for k in kept}
        ):
            kept.append(previous)
        merged = kept + [a for a in crm_set if a.lower() not in {k.lower() for k in kept}]
        action["entity_id"] = eid
        if (
            new_name == linked["name"]
            and merged == list(linked["aliases"] or [])
            and sorted(a.lower() for a in crm_set) == sorted(old_crm)
            and meta.get("crm_name") == name
        ):
            return {**action, "action": "unchanged"}
        if not dry_run:
            cur.execute(
                "UPDATE memory_entities SET name = %s, aliases = %s, metadata = "
                "COALESCE(metadata, '{}'::jsonb) || %s::jsonb, last_seen = NOW() WHERE id = %s",
                (new_name, merged, json.dumps({"crm_aliases": crm_set, "crm_name": name}), eid),
            )
        return {**action, "action": "updated", "name": new_name}

    # An unlinked node of exactly this name and type is the same record — unless
    # two live CRM records share the name, when it could be either, or the name
    # is not a full name, when the node is every bare mention of it.
    legacy: list[dict[str, Any]] = []
    if full and not ambiguous_name:
        cur.execute(
            "SELECT id, name, aliases FROM memory_entities WHERE tenant_id = %s AND "
            "entity_type = %s AND crm_person_id IS NULL AND crm_company_id IS NULL AND "
            "scope_kind = 'shared' AND lower(regexp_replace(btrim(name), '\\s+', ' ', 'g')) = %s "
            "ORDER BY id",
            (tenant_id, etype, normalize_surface(name)),
        )
        legacy = [dict(r) for r in cur.fetchall()]
    same_case = [r for r in legacy if r["name"] == name]
    pick = same_case[0] if same_case else (legacy[0] if len(legacy) == 1 else None)
    if pick is not None:
        eid = int(pick["id"])
        action["entity_id"] = eid
        if not dry_run:
            new_name = name if _name_free(cur, tenant_id, name, etype, eid) else pick["name"]
            cur.execute(
                f"UPDATE memory_entities SET {col} = %s, name = %s, metadata = "  # noqa: S608
                "COALESCE(metadata, '{}'::jsonb) || %s::jsonb, last_seen = NOW() WHERE id = %s",
                (crm_id, new_name, json.dumps({"crm_aliases": [], "crm_name": name}), eid),
            )
            _insert_decision(
                cur,
                tenant_id,
                surface=pick["name"],
                entity_type=etype,
                entity_id=eid,
                method="exact",
                confidence=1.0,
                evidence={
                    "basis": f"crm_link:{kind}:{crm_id}",
                    "reason": "crm_link",
                    "crm_id": crm_id,
                    "normalized": normalize_surface(name),
                },
            )
        return {**action, "action": "linked"}

    new_name, crm_set = _naming(None, None)
    if not dry_run:
        cur.execute(
            f"INSERT INTO memory_entities (name, entity_type, aliases, metadata, tenant_id, {col}) "  # noqa: S608
            "VALUES (%s, %s, %s, %s::jsonb, %s, %s) RETURNING id",
            (
                new_name,
                etype,
                crm_set,
                json.dumps({"crm_aliases": crm_set, "crm_name": name}),
                tenant_id,
                crm_id,
            ),
        )
        action["entity_id"] = int(cur.fetchone()["id"])
    return {**action, "action": "created", "name": new_name}


_PEOPLE_SQL = (
    "SELECT id::text AS id, first_name, last_name, email, additional_emails, company_id "
    "FROM crm_people WHERE tenant_id = %s AND deleted_at IS NULL"
)
_COMPANIES_SQL = (
    "SELECT id::text AS id, name, domain_name FROM crm_companies "
    "WHERE tenant_id = %s AND deleted_at IS NULL"
)


def _name_counts(kind: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        n = normalize_surface(_crm_name(kind, r))
        counts[n] = counts.get(n, 0) + 1
    return counts


def sync_crm_entities(tenant_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Give every live CRM person and company in the tenant exactly one linked node.

    Linked through ``crm_person_id`` / ``crm_company_id`` and named with the
    CRM full name or company name. No CRM contact data (emails, handles,
    domains) and no nickname variant is written to the node. An unlinked node of
    exactly the same name and type is linked rather than duplicated (a decision
    with method ``exact`` records it), unless two live CRM records share that
    name or it is not a full name. When the name is already taken by another
    record's node, or is not a full name, the node is named
    ``"<name> #<crm id prefix>"``; a taken full name is kept as an alias, so a
    bare mention of it stays ambiguous. A renamed record keeps its previous full
    name as an alias. Idempotent.
    ``dry_run`` writes nothing and reports what it would do.
    """
    _require_tenant(tenant_id)
    blank = {"created": 0, "linked": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    report: dict[str, Any] = {
        "tenant_id": tenant_id,
        "dry_run": dry_run,
        "people": dict(blank),
        "companies": dict(blank),
        "actions": [],
        "merges_recorded": 0,
    }
    with _cursor(tenant_id, read_only=dry_run) as cur:
        cur.execute(_PEOPLE_SQL + " ORDER BY created_at, id", (tenant_id,))
        people = [dict(r) for r in cur.fetchall()]
        cur.execute(_COMPANIES_SQL + " ORDER BY created_at, id", (tenant_id,))
        companies = [dict(r) for r in cur.fetchall()]
        for kind, rows, bucket in ((PERSON, people, "people"), ("company", companies, "companies")):
            counts = _name_counts(kind, rows)
            for row in rows:
                act = _sync_one(
                    cur,
                    tenant_id,
                    kind,
                    row,
                    ambiguous_name=counts.get(normalize_surface(_crm_name(kind, row)), 0) > 1,
                    dry_run=dry_run,
                )
                report[bucket][act["action"]] += 1
                if act["action"] != "unchanged":
                    report["actions"].append(act)
        for keeper, loser in _unrecorded_merges(cur, tenant_id):
            if not dry_run:
                record_crm_merge(tenant_id, PERSON, keeper, loser, cur=cur)
            report["merges_recorded"] += 1
    return report


def _unrecorded_merges(cur: Any, tenant_id: str) -> list[tuple[str, str]]:
    """CRM people merges the DAL hook failed to record, read from the merge note.

    ``merge_people`` writes a "Duplicate Merged" note on the keeper in the same
    transaction as the merge, naming the loser's id. A loser with a node, no
    merge decision ever recorded (live or reversed — an operator's reversal is
    not undone), and exactly one live keeper is repaired. Company merges leave no
    such note and are not repaired here.
    """
    cur.execute(
        """
        SELECT n.person_id::text AS keeper, p.id::text AS loser
          FROM crm_people p
          JOIN memory_entities e ON e.tenant_id = p.tenant_id AND e.crm_person_id = p.id
          JOIN crm_notes n ON n.tenant_id = p.tenant_id AND n.deleted_at IS NULL
                          AND n.title = 'Duplicate Merged'
                          AND n.body LIKE '%%(id: ' || p.id::text || ')%%'
          JOIN crm_people k ON k.id = n.person_id AND k.tenant_id = p.tenant_id
                           AND k.deleted_at IS NULL
         WHERE p.tenant_id = %s AND p.deleted_at IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM memory_resolution_decisions d
                            WHERE d.tenant_id = p.tenant_id
                              AND d.evidence->>'basis' = 'crm_merge:person:' || p.id::text)
        """,
        (tenant_id,),
    )
    pairs: dict[str, set[str]] = {}
    for r in cur.fetchall():
        pairs.setdefault(r["loser"], set()).add(r["keeper"])
    return [(next(iter(k)), loser) for loser, k in sorted(pairs.items()) if len(k) == 1]


def _sync_record(
    cur: Any, tenant_id: str, kind: str, record_id: str, *, dry_run: bool = False
) -> dict[str, Any]:
    if not _is_uuid(record_id):
        return {"kind": kind, "crm_id": record_id, "action": "skipped", "entity_id": None}
    sql = _PEOPLE_SQL if kind == PERSON else _COMPANIES_SQL
    cur.execute(sql + " AND id = %s", (tenant_id, record_id))
    row = cur.fetchone()
    if not row:
        return {"kind": kind, "crm_id": record_id, "action": "missing", "entity_id": None}
    row = dict(row)
    name = normalize_surface(_crm_name(kind, row))
    if kind == PERSON:
        cur.execute(
            _PEOPLE_SQL + " AND id <> %s AND lower(regexp_replace(btrim(coalesce(first_name, '') "
            "|| ' ' || coalesce(last_name, '')), '\\s+', ' ', 'g')) = %s LIMIT 1",
            (tenant_id, record_id, name),
        )
    else:
        cur.execute(
            _COMPANIES_SQL + " AND id <> %s AND lower(regexp_replace(btrim(name), '\\s+', ' ', "
            "'g')) = %s LIMIT 1",
            (tenant_id, record_id, name),
        )
    ambiguous = cur.fetchone() is not None
    return _sync_one(cur, tenant_id, kind, row, ambiguous_name=ambiguous, dry_run=dry_run)


def sync_crm_record(
    tenant_id: str, kind: str, record_id: str, *, cur: Any = None
) -> dict[str, Any]:
    """Keep one CRM record's node current (the CRM DAL hook calls this).

    ``kind`` is ``person`` or ``company``. A deleted or unknown record is a
    no-op. Against a database without the memory graph schema it does nothing.
    """
    _require_tenant(tenant_id)
    k = _kind(kind)
    with _cursor(tenant_id, cur, guard=cur is None) as c:
        if not _schema_ready(c):
            return {"kind": k, "crm_id": record_id, "action": "skipped", "reason": "schema"}
        return _sync_record(c, tenant_id, k, str(record_id))


def record_crm_merge(
    tenant_id: str, kind: str, keeper_id: str, loser_id: str, *, cur: Any = None
) -> dict[str, Any]:
    """Record that a CRM merge made the loser's node the keeper's.

    No id is rewritten: the loser's node and every mention of it stay as they
    are. A decision (method ``strong_key``, basis ``crm_merge:<kind>:<loser>``)
    resolves the loser's node to the keeper's, and resolution follows it from
    then on. Reversing that decision undoes the merge for memory.
    """
    _require_tenant(tenant_id)
    k = _kind(kind)
    col = _crm_col(k)
    basis = f"crm_merge:{k}:{loser_id}"
    with _cursor(tenant_id, cur, guard=cur is None) as c:
        if not _schema_ready(c):
            return {"action": "skipped", "reason": "schema"}
        keeper = _sync_record(c, tenant_id, k, str(keeper_id))
        keeper_node = keeper.get("entity_id")
        c.execute(
            f"SELECT id, name, entity_type FROM memory_entities WHERE tenant_id = %s AND {col} = %s "  # noqa: S608
            "ORDER BY id LIMIT 1",
            (tenant_id, str(loser_id)),
        )
        loser = c.fetchone()
        if keeper_node is None or loser is None or int(loser["id"]) == keeper_node:
            return {"action": "nothing_to_record", "keeper_entity_id": keeper_node}
        live = _live_decision_by_basis(c, tenant_id, basis)
        if live and live["resolved_entity_id"] == keeper_node:
            return {"action": "unchanged", "decision_id": live["id"]}
        did = _insert_decision(
            c,
            tenant_id,
            surface=loser["name"],
            entity_type=loser["entity_type"],
            entity_id=keeper_node,
            method="strong_key",
            confidence=1.0,
            evidence={
                "basis": basis,
                "reason": "crm_merge",
                "normalized": normalize_surface(loser["name"]),
                "crm_merge": {
                    "keeper_crm_id": str(keeper_id),
                    "loser_crm_id": str(loser_id),
                    "from_entity_id": int(loser["id"]),
                },
            },
        )
        return {
            "action": "recorded",
            "decision_id": did,
            "keeper_entity_id": keeper_node,
            "loser_entity_id": int(loser["id"]),
        }


# ═══ resolution ══════════════════════════════════════════════════════════


@dataclass
class _Result:
    resolved: ResolvedMention
    outcome: Outcome
    created: bool = False


@dataclass
class _Plan:
    """What a dry run would have written, so later mentions in the run see it."""

    next_id: int = -1
    entities: dict[tuple[str, str], tuple[int, str]] = field(default_factory=dict)
    evidence: list[tuple[str, int, tuple[str, str | None, str | None], str | None]] = field(
        default_factory=list
    )
    decisions: dict[tuple[Any, ...], int] = field(default_factory=dict)

    def new_id(self) -> int:
        nid = self.next_id
        self.next_id -= 1
        return nid


class _Resolver:
    """Resolves mentions against one tenant on one cursor; caches per call."""

    def __init__(
        self,
        cur: Any,
        tenant_id: str,
        *,
        dry_run: bool = False,
        plan: _Plan | None = None,
        read_only: bool = False,
    ) -> None:
        self.cur = cur
        self.tenant = tenant_id
        self.dry_run = dry_run
        self.read_only = read_only or dry_run
        self.plan = plan if plan is not None else _Plan()
        self._rows: dict[str, list[dict[str, Any]]] = {}
        self._fetched: set[str] = set()
        self._crm: dict[str, dict[str, Any] | None] = {}
        self._companies: dict[str, dict[str, Any] | None] = {}
        self._merge: dict[str, tuple[int, int] | None] = {}
        # Per mention: entity id -> the CRM-merge decisions followed to reach it.
        self._via: dict[int, set[int]] = {}
        self._entity: dict[int, dict[str, Any] | None] = {}
        self._owner: str | None | bool = False

    # ── candidate rows ──
    @staticmethod
    def _forms(surface: str) -> tuple[set[str], set[str], set[str]]:
        norm = normalize_surface(surface)
        tokens = norm.split()
        forms: set[str] = {norm}
        firsts: set[str] = set()
        lasts: set[str] = set()
        if len(tokens) >= 2:
            for v in first_name_variants(tokens[0]) - {tokens[0]}:
                forms.add(" ".join([v, *tokens[1:]]))
            if _is_initial(tokens[0]) or _is_initial(tokens[-1]):
                lasts.add(tokens[-1])
                if not _is_initial(tokens[0]):
                    firsts |= first_name_variants(tokens[0])
        elif tokens:
            firsts |= first_name_variants(tokens[0])
        return forms, firsts, lasts

    def prefetch(self, surfaces: Sequence[str]) -> None:
        todo = [s for s in {normalize_surface(s) for s in surfaces} if s and s not in self._fetched]
        if not todo:
            return
        forms: set[str] = set()
        firsts: set[str] = set()
        lasts: set[str] = set()
        for s in todo:
            f, fi, la = self._forms(s)
            forms |= f
            firsts |= fi
            lasts |= la
        self.cur.execute(
            """
            SELECT id, name, entity_type, aliases, crm_person_id::text AS crm_person_id,
                   crm_company_id::text AS crm_company_id, scope_kind, scope_id, norm, alias_norms
              FROM (SELECT e.id, e.name, e.entity_type, e.aliases, e.crm_person_id,
                           e.crm_company_id, e.scope_kind, e.scope_id,
                           lower(regexp_replace(btrim(e.name), '\\s+', ' ', 'g')) AS norm,
                           ARRAY(SELECT lower(regexp_replace(btrim(a), '\\s+', ' ', 'g'))
                                   FROM unnest(coalesce(e.aliases, '{}'::text[])) a) AS alias_norms
                      FROM memory_entities e
                     WHERE e.tenant_id = %s) x
             WHERE norm = ANY(%s) OR alias_norms && %s::text[]
                OR split_part(norm, ' ', 1) = ANY(%s)
                OR regexp_replace(norm, '^.* ', '') = ANY(%s)
            """,
            (self.tenant, sorted(forms), sorted(forms), sorted(firsts), sorted(lasts)),
        )
        rows = [dict(r) for r in self.cur.fetchall()]
        for s in todo:
            self._rows[s] = [r for r in rows if self._classify(r, s) is not None]
            self._fetched.add(s)
        for r in rows:
            self._entity[int(r["id"])] = r

    def invalidate(self) -> None:
        self._rows.clear()
        self._fetched.clear()

    @staticmethod
    def _classify(row: Mapping[str, Any], norm: str) -> str | None:
        tokens = norm.split()
        rnorm = row["norm"] or ""
        alias_norms = set(row.get("alias_norms") or [])
        if rnorm == norm:
            return "name"
        if norm in alias_norms:
            return "alias"
        is_person = _norm_type(row["entity_type"]) == PERSON
        rtok = rnorm.split()
        if not tokens or not is_person:
            return None
        initials = len(tokens) >= 2 and (_is_initial(tokens[0]) or _is_initial(tokens[-1]))
        if len(tokens) >= 2 and not initials:
            variants = {
                " ".join([v, *tokens[1:]]) for v in first_name_variants(tokens[0]) - {tokens[0]}
            }
            if rnorm in variants or alias_norms & variants:
                return "nickname"
            return None
        if len(tokens) == 1:
            if rtok and rtok[0] == tokens[0]:
                return "first_name"
            if rtok and rtok[0] in first_name_variants(tokens[0]):
                return "nickname"
            return None
        # A name with an initial at either end ("A. Morgan", "Alex M.").
        if len(rtok) < 2:
            return None
        first, last = tokens[0].strip("."), tokens[-1].strip(".")
        if not rtok[0].startswith(first[:1]) or (
            len(first) > 1 and rtok[0] not in first_name_variants(first)
        ):
            return None
        if not rtok[-1].startswith(last[:1]) or (len(last) > 1 and rtok[-1] != last):
            return None
        return "initial"

    # ── CRM state ──
    def _load_people(self, ids: Sequence[str]) -> None:
        todo = sorted({i for i in ids if i and i not in self._crm and _is_uuid(i)})
        if not todo:
            return
        self.cur.execute(
            "SELECT p.id::text AS id, p.first_name, p.last_name, p.email, p.additional_emails, "
            "p.deleted_at IS NOT NULL AS deleted, c.name AS company_name "
            "FROM crm_people p LEFT JOIN crm_companies c ON c.id = p.company_id "
            "AND c.deleted_at IS NULL AND c.tenant_id = p.tenant_id "
            "WHERE p.tenant_id = %s AND p.id = ANY(%s::uuid[])",
            (self.tenant, todo),
        )
        found = {r["id"]: dict(r) for r in self.cur.fetchall()}
        for i in todo:
            self._crm[i] = found.get(i)

    def _load_companies(self, ids: Sequence[str]) -> None:
        todo = sorted({i for i in ids if i and i not in self._companies and _is_uuid(i)})
        if not todo:
            return
        self.cur.execute(
            "SELECT id::text AS id, name, deleted_at IS NOT NULL AS deleted FROM crm_companies "
            "WHERE tenant_id = %s AND id = ANY(%s::uuid[])",
            (self.tenant, todo),
        )
        found = {r["id"]: dict(r) for r in self.cur.fetchall()}
        for i in todo:
            self._companies[i] = found.get(i)

    def _merged_into(self, kind: str, crm_id: str) -> tuple[int, int] | None:
        """The (keeper node, merge decision id) a merged-away record resolves to."""
        key = f"{kind}:{crm_id}"
        if key not in self._merge:
            live = _live_decision_by_basis(self.cur, self.tenant, f"crm_merge:{key}")
            self._merge[key] = (
                (int(live["resolved_entity_id"]), int(live["id"]))
                if live and live["resolved_entity_id"] is not None
                else None
            )
        return self._merge[key]

    def entity_row(self, eid: int) -> dict[str, Any] | None:
        if eid < 0:
            for (_n, t), (pid, name) in self.plan.entities.items():
                if pid == eid:
                    return {
                        "id": eid,
                        "name": name,
                        "entity_type": t,
                        "scope_kind": "shared",
                        "scope_id": None,
                        "crm_person_id": None,
                        "crm_company_id": None,
                    }
            return None
        if eid not in self._entity:
            self.cur.execute(
                "SELECT id, name, entity_type, aliases, crm_person_id::text AS crm_person_id, "
                "crm_company_id::text AS crm_company_id, scope_kind, scope_id "
                "FROM memory_entities WHERE id = %s AND tenant_id = %s",
                (eid, self.tenant),
            )
            row = self.cur.fetchone()
            self._entity[eid] = dict(row) if row else None
        return self._entity[eid]

    def effective(
        self, row: Mapping[str, Any], hops: int = 0, via: tuple[int, ...] = ()
    ) -> dict[str, Any] | None:
        """The node a row stands for now: itself, its CRM merge winner, or nothing.

        A node reached through CRM merges carries ``_via_merge``: the merge
        decisions followed, so a decision built on it can be undone with them.
        """
        pid, cid = row.get("crm_person_id"), row.get("crm_company_id")
        target: tuple[int, int] | None
        if pid:
            self._load_people([pid])
            person = self._crm.get(pid)
            live = person is not None and not person["deleted"]
            target = None if live or person is None else self._merged_into(PERSON, pid)
        elif cid:
            self._load_companies([cid])
            company = self._companies.get(cid)
            live = company is not None and not company["deleted"]
            target = None if live or company is None else self._merged_into("company", cid)
        else:
            live = True
            target = None
        if live:
            out = dict(row)
            if via:
                out["_via_merge"] = list(via)
            return out
        if target is None or hops >= _MAX_MERGE_HOPS:
            return None
        nxt = self.entity_row(target[0])
        return self.effective(nxt, hops + 1, (*via, target[1])) if nxt else None

    def _note_via(self, eff: Mapping[str, Any] | None) -> None:
        if eff is not None and eff.get("_via_merge"):
            self._via.setdefault(int(eff["id"]), set()).update(eff["_via_merge"])

    def owner_person(self) -> str | None:
        if self._owner is False:
            from robothor.crm.dal import get_owner_person

            try:
                self._owner = get_owner_person(self.tenant).get("id")
            except Exception:  # an unknown operator is never a reason to fail resolution
                logger.debug("identity: operator lookup failed", exc_info=True)
                self._owner = None
            self._owner = str(self._owner) if self._owner else None
        return self._owner if isinstance(self._owner, str) else None

    # ── conversation evidence ──
    def in_conversation(
        self, raw_ids: Sequence[int], conv: str, scope: tuple[str, str | None, str | None]
    ) -> set[int]:
        ids = sorted({i for i in raw_ids if i >= 0})
        found: set[int] = set()
        if ids:
            self.cur.execute(
                """
                SELECT DISTINCT m.entity_id
                  FROM memory_mentions m
                  JOIN memory_facts f ON f.id = m.fact_id AND f.tenant_id = m.tenant_id
                  JOIN memory_resolution_decisions d ON d.id = m.decision_id
                 WHERE m.tenant_id = %s AND m.entity_id = ANY(%s)
                   AND f.is_active AND f.metadata->>'session_key' = %s
                   AND d.reversed_at IS NULL AND d.method = ANY(%s)
                   AND (f.scope_kind = 'shared' OR (f.scope_kind = %s AND f.scope_id = %s))
                   AND (f.person_id IS NULL OR f.person_id = %s::uuid)
                """,
                (self.tenant, ids, conv, list(EVIDENCE_METHODS), scope[0], scope[1], scope[2]),
            )
            found = {int(r["entity_id"]) for r in self.cur.fetchall()}
        for pconv, eid, pscope, ppid in self.plan.evidence:
            if pconv != conv or eid not in raw_ids:
                continue
            if pscope[0] != "shared" and pscope[:2] != scope[:2]:
                continue
            if ppid is not None and ppid != scope[2]:
                continue
            found.add(eid)
        return found

    # ── strong keys ──
    def _person_node(self, person_id: str) -> tuple[int | None, str | None]:
        """The node for a live or merged CRM person; links a live one on demand."""
        self._load_people([person_id])
        person = self._crm.get(person_id)
        if person is None:
            return None, "unknown"
        self.cur.execute(
            "SELECT id, name, entity_type, crm_person_id::text AS crm_person_id, "
            "crm_company_id::text AS crm_company_id, scope_kind, scope_id FROM memory_entities "
            "WHERE tenant_id = %s AND crm_person_id = %s ORDER BY id LIMIT 1",
            (self.tenant, person_id),
        )
        row = self.cur.fetchone()
        if row is not None:
            eff = self.effective(dict(row))
            self._note_via(eff)
            return (int(eff["id"]), None) if eff else (None, "deleted")
        if person["deleted"]:
            return None, "deleted"
        if self.read_only:
            return None, "not_seeded"
        act = _sync_record(self.cur, self.tenant, PERSON, person_id)
        self.invalidate()
        return (act.get("entity_id"), None) if act.get("entity_id") else (None, "unlinkable")

    def _company_node(self, company_id: str) -> tuple[int | None, str | None]:
        self._load_companies([company_id])
        company = self._companies.get(company_id)
        if company is None:
            return None, "unknown"
        self.cur.execute(
            "SELECT id, name, entity_type, crm_person_id::text AS crm_person_id, "
            "crm_company_id::text AS crm_company_id, scope_kind, scope_id FROM memory_entities "
            "WHERE tenant_id = %s AND crm_company_id = %s ORDER BY id LIMIT 1",
            (self.tenant, company_id),
        )
        row = self.cur.fetchone()
        if row is not None:
            eff = self.effective(dict(row))
            self._note_via(eff)
            return (int(eff["id"]), None) if eff else (None, "deleted")
        if company["deleted"]:
            return None, "deleted"
        if self.read_only:
            return None, "not_seeded"
        act = _sync_record(self.cur, self.tenant, "company", company_id)
        self.invalidate()
        return (act.get("entity_id"), None) if act.get("entity_id") else (None, "unlinkable")

    def strong_hits(self, mention: MentionInput) -> tuple[list[StrongHit], dict[str, str]]:
        keys = {k: str(v).strip() for k, v in (mention.strong_keys or {}).items() if v}
        if "email" not in keys and _EMAIL_RE.match(_clean(mention.surface_form or "")):
            keys["email"] = _clean(mention.surface_form)
        hits: list[StrongHit] = []
        ignored: dict[str, str] = {}
        for kind, value in keys.items():
            eid: int | None = None
            why: str | None = None
            if kind == "crm_person_id":
                eid, why = self._person_node(value) if _is_uuid(value) else (None, "malformed")
            elif kind == "crm_company_id":
                eid, why = self._company_node(value) if _is_uuid(value) else (None, "malformed")
            elif kind == "email":
                eid, why = self._email_node(value.lower())
            elif kind == "channel":
                eid, why = self._channel_node(value)
            else:
                why = "unknown_kind"
            if eid is None:
                ignored[kind] = why or "unknown"
                continue
            row = self.entity_row(eid)
            if row is None:
                ignored[kind] = "unknown"
                continue
            hits.append(StrongHit(kind, eid, row["entity_type"], row["name"]))
        return hits, ignored

    def _email_node(self, email: str) -> tuple[int | None, str | None]:
        self.cur.execute(
            "SELECT id::text AS id FROM crm_people WHERE tenant_id = %s AND deleted_at IS NULL "
            "AND (lower(email) = %s OR (jsonb_typeof(additional_emails) = 'array' AND EXISTS ("
            "SELECT 1 FROM jsonb_array_elements_text(additional_emails) x WHERE lower(x) = %s)))",
            (self.tenant, email, email),
        )
        ids = [r["id"] for r in self.cur.fetchall()]
        if len(ids) > 1:
            return None, "ambiguous"
        if not ids:
            return None, "unknown"
        return self._person_node(ids[0])

    def _channel_node(self, value: str) -> tuple[int | None, str | None]:
        channel, sep, ident = value.partition(":")
        if not sep or not channel or not ident:
            return None, "malformed"
        self.cur.execute(
            "SELECT person_id::text AS person_id, memory_entity_id FROM contact_identifiers "
            "WHERE tenant_id = %s AND channel = %s AND identifier = %s LIMIT 1",
            (self.tenant, channel, ident),
        )
        row = self.cur.fetchone()
        if row is None:
            return None, "unknown"
        if row["person_id"]:
            return self._person_node(row["person_id"])
        if row["memory_entity_id"] is not None:
            ent = self.entity_row(int(row["memory_entity_id"]))
            if ent is not None and _norm_type(ent["entity_type"]) == PERSON:
                eff = self.effective(ent)
                self._note_via(eff)
                return (int(eff["id"]), None) if eff else (None, "deleted")
        return None, "unknown"

    # ── candidates for one mention ──
    def candidates(self, mention: MentionInput) -> tuple[list[Candidate], bool]:
        norm = normalize_surface(mention.surface_form)
        self.prefetch([mention.surface_form])
        ctx = mention.context or {}
        scope = _scope_of(ctx)
        conv = str(ctx["conversation_ref"]) if ctx.get("conversation_ref") else None
        raw = [r for r in self._rows.get(norm, []) if _visible(r, scope)]
        for (pnorm, ptype), (pid, pname) in self.plan.entities.items():
            if pnorm == norm and not any(r["id"] == pid for r in raw):
                raw.append(
                    {
                        "id": pid,
                        "name": pname,
                        "entity_type": ptype,
                        "norm": pnorm,
                        "alias_norms": [],
                        "scope_kind": "shared",
                        "scope_id": None,
                        "crm_person_id": None,
                        "crm_company_id": None,
                    }
                )
        blocked = False
        by_id: dict[int, tuple[dict[str, Any], str, set[int]]] = {}
        rank = {"name": 0, "alias": 1, "nickname": 2, "first_name": 3, "initial": 4}
        for r in raw:
            match = self._classify(r, norm)
            if match is None:
                continue
            eff = self.effective(r)
            if eff is None or not _visible(eff, scope):
                blocked = blocked or match in ("name", "alias")
                continue
            eid = int(eff["id"])
            self._note_via(eff)
            if eid in by_id:
                prev = by_id[eid]
                prev[2].add(int(r["id"]))
                if rank[match] < rank[prev[1]]:
                    by_id[eid] = (prev[0], match, prev[2])
            else:
                by_id[eid] = (eff, match, {int(r["id"]), eid})

        person_ids = [e["crm_person_id"] for e, _m, _s in by_id.values() if e.get("crm_person_id")]
        self._load_people(person_ids)
        tokens = norm.split()
        partial = len(tokens) < 2 or _is_initial(tokens[0]) or _is_initial(tokens[-1])
        conv_hits: set[int] = set()
        if conv is not None and by_id:
            all_raw = sorted({i for _e, _m, s in by_id.values() for i in s})
            conv_hits = self.in_conversation(all_raw, conv, scope)
        # The operator matters only where a first name could be taken for them.
        owner = self.owner_person() if conv_hits and partial else None

        out: list[Candidate] = []
        for eid, (eff, match, raw_ids) in sorted(by_id.items()):
            orgs: set[str] = set()
            domains: set[str] = set()
            person = self._crm.get(eff["crm_person_id"]) if eff.get("crm_person_id") else None
            if person is not None:
                if person.get("company_name"):
                    orgs.add(normalize_surface(person["company_name"]))
                domains = {e.split("@", 1)[1] for e in _emails_of(person)}
            out.append(
                Candidate(
                    entity_id=eid,
                    name=eff["name"],
                    entity_type=eff["entity_type"],
                    match=match,
                    organisations=frozenset(orgs),
                    email_domains=frozenset(domains),
                    in_conversation=bool(conv_hits & raw_ids),
                    is_operator=owner is not None and eff.get("crm_person_id") == owner,
                )
            )
        return out, blocked

    # ── deciding and recording ──
    def resolve(self, mention: MentionInput) -> _Result:
        self._via = {}
        hits, ignored = self.strong_hits(mention)
        cands, blocked = self.candidates(mention)
        outcome = decide(mention, cands, strong_hits=hits, blocked=blocked)
        if ignored:
            outcome = Outcome(
                outcome.entity_id,
                outcome.method,
                outcome.confidence,
                outcome.basis,
                outcome.reason,
                {**outcome.evidence, "ignored_keys": ignored},
                outcome.create_type,
            )
        return self.record(mention, outcome)

    def _create(self, mention: MentionInput, etype: str) -> int | None:
        name = _clean(mention.surface_form)[:_MAX_NAME]
        key = (normalize_surface(name), etype)
        if key in self.plan.entities:
            return self.plan.entities[key][0]
        if self.read_only:
            pid = self.plan.new_id()
            self.plan.entities[key] = (pid, name)
            return pid
        self.cur.execute(
            "INSERT INTO memory_entities (name, entity_type, aliases, tenant_id) "
            "VALUES (%s, %s, '{}', %s) ON CONFLICT (tenant_id, name, entity_type) DO UPDATE "
            "SET last_seen = memory_entities.last_seen RETURNING id, name, entity_type, "
            "crm_person_id::text AS crm_person_id, crm_company_id::text AS crm_company_id, "
            "scope_kind, scope_id",
            (name, etype, self.tenant),
        )
        row = dict(self.cur.fetchone())
        self._entity[int(row["id"])] = row
        self.invalidate()
        eff = self.effective(row)
        scope = _scope_of(mention.context or {})
        if eff is None or int(eff["id"]) != int(row["id"]) or not _visible(row, scope):
            return None
        return int(row["id"])

    def record(self, mention: MentionInput, outcome: Outcome) -> _Result:
        surface = _clean(mention.surface_form or "")[:_MAX_NAME] or "?"
        etype = _norm_type(mention.entity_type)
        created = False
        if outcome.create_type:
            eid = self._create(mention, outcome.create_type)
            if eid is None:
                outcome = Outcome(
                    None,
                    "exact",
                    0.0,
                    f"unresolved:name_taken:{surface.lower()}",
                    "name_taken",
                    {**outcome.evidence, "rule": "name_taken"},
                )
            else:
                created = True
                outcome = Outcome(
                    eid, "exact", outcome.confidence, "exact", "create", outcome.evidence
                )
                etype = etype or outcome.create_type

        override = self._operator_override(surface, etype, outcome)
        if override is not None:
            row, did = override
            name = row["name"] if row else None
            return _Result(
                ResolvedMention(int(row["id"]) if row else None, did, "operator", 1.0, name),
                outcome,
                created,
            )
        if outcome.entity_id is not None and self._rejected(surface, etype, outcome.entity_id):
            outcome = Outcome(
                None,
                "operator",
                0.0,
                f"unresolved:rejected:{outcome.entity_id}",
                "rejected",
                {**outcome.evidence, "rejected_entity_id": outcome.entity_id, "rule": "rejected"},
            )

        via = sorted(self._via.get(outcome.entity_id, ())) if outcome.entity_id is not None else []
        if via:
            # Reached through a CRM merge: say so, so reversing the merge finds it.
            outcome = Outcome(
                outcome.entity_id,
                outcome.method,
                outcome.confidence,
                f"{outcome.basis}|via_merge:{','.join(map(str, via))}",
                outcome.reason,
                {**outcome.evidence, "via_merge": via},
                outcome.create_type,
            )
        evidence = {**outcome.evidence, "basis": outcome.basis, "reason": outcome.reason}
        key = (surface.lower(), etype, outcome.entity_id, outcome.method, outcome.basis)
        reused = self._reuse(key)
        if reused is not None:
            decision_id = reused
        elif self.read_only:
            decision_id = self.plan.decisions.get(key) or self.plan.new_id()
            self.plan.decisions[key] = decision_id
        else:
            decision_id = _insert_decision(
                self.cur,
                self.tenant,
                surface=surface,
                entity_type=etype,
                entity_id=outcome.entity_id,
                method=outcome.method,
                confidence=outcome.confidence,
                evidence=evidence,
            )
        row = self.entity_row(outcome.entity_id) if outcome.entity_id is not None else None
        res = ResolvedMention(
            outcome.entity_id,
            decision_id,
            outcome.method,
            outcome.confidence,
            row["name"] if row else None,
        )
        return _Result(res, outcome, created)

    def _reuse(self, key: tuple[Any, ...]) -> int | None:
        surface, etype, eid, method, basis = key
        if self.read_only and key in self.plan.decisions:
            return self.plan.decisions[key]
        self.cur.execute(
            "SELECT id FROM memory_resolution_decisions WHERE tenant_id = %s "
            "AND lower(surface_form) = %s AND entity_type IS NOT DISTINCT FROM %s "
            "AND resolved_entity_id IS NOT DISTINCT FROM %s AND method = %s "
            "AND evidence->>'basis' = %s AND reversed_at IS NULL AND superseded_by IS NULL "
            "ORDER BY id LIMIT 1",
            (self.tenant, surface, etype, eid if eid is None or eid >= 0 else None, method, basis),
        )
        row = self.cur.fetchone()
        return int(row["id"]) if row and (eid is None or eid >= 0) else None

    def _operator_override(
        self, surface: str, etype: str | None, outcome: Outcome
    ) -> tuple[dict[str, Any] | None, int] | None:
        """A live, whole-decision operator ruling on this surface form and node.

        Only a resolved outcome can be overridden, and only by an operator decision
        that replaced this node or chose it. A ruling on one fact (``scope:
        facts``) never reaches other mentions, and nor does settling an
        unresolved name. A decision without a type matches any type: the
        backfill records untyped decisions.
        """
        if outcome.entity_id is None or outcome.entity_id < 0:
            return None
        self.cur.execute(
            "SELECT id, resolved_entity_id FROM memory_resolution_decisions WHERE tenant_id = %s "
            "AND method = 'operator' AND reversed_at IS NULL AND superseded_by IS NULL "
            "AND evidence->>'scope' IS DISTINCT FROM 'facts' "
            "AND lower(surface_form) = %s AND (entity_type IS NULL OR %s::text IS NULL "
            "OR entity_type = %s) AND (evidence->>'replaces_entity_id' = %s "
            "OR resolved_entity_id = %s) ORDER BY id DESC LIMIT 1",
            (
                self.tenant,
                surface.lower(),
                etype,
                etype,
                str(outcome.entity_id),
                outcome.entity_id,
            ),
        )
        row = self.cur.fetchone()
        if row is None:
            return None
        ent = (
            self.entity_row(int(row["resolved_entity_id"]))
            if row["resolved_entity_id"] is not None
            else None
        )
        return ent, int(row["id"])

    def _rejected(self, surface: str, etype: str | None, eid: int) -> bool:
        if eid < 0:
            return False
        self.cur.execute(
            "SELECT 1 FROM memory_resolution_decisions WHERE tenant_id = %s "
            "AND lower(surface_form) = %s AND (entity_type IS NULL OR %s::text IS NULL "
            "OR entity_type = %s) AND resolved_entity_id = %s AND reversed_at IS NOT NULL LIMIT 1",
            (self.tenant, surface.lower(), etype, etype, eid),
        )
        return self.cur.fetchone() is not None


def resolve_mentions(
    tenant_id: str, mentions: Sequence[MentionInput], *, cur: Any = None
) -> list[ResolvedMention]:
    """Resolve each mention to a node, or leave it unresolved, and record why.

    Synchronous and database-only (no model call). With ``cur`` it runs inside
    the caller's transaction and commits nothing; without, in its own.

    Every mention gets a decision row, or reuses an identical live one: same
    tenant, same surface form (case- and whitespace-insensitive), same entity
    type, same resolved node, same method and same basis (the evidence the
    decision rests on — for a context decision the candidates and the matching
    context; for an unresolved one the reason, the candidates and the
    conversation). Re-ingesting the same text therefore adds no rows. A live
    operator decision for the same surface form and node takes precedence, and a
    node the operator reversed a decision away from is never chosen again for
    that surface form.

    May create a provisional node (rule e) and may link a CRM record that had no
    node yet (rule a). Never writes mentions: :func:`write_mentions` does that.
    """
    _require_tenant(tenant_id)
    if not mentions:
        return []
    with _cursor(tenant_id, cur) as c:
        resolver = _Resolver(c, tenant_id)
        resolver.prefetch([m.surface_form for m in mentions])
        return [resolver.resolve(m).resolved for m in mentions]


def write_mentions(
    tenant_id: str,
    fact_id: int,
    mentions: Sequence[MentionInput],
    resolved: Sequence[ResolvedMention],
    *,
    cur: Any = None,
) -> int:
    """Write one ``memory_mentions`` row per mention of ``fact_id``. Returns rows written.

    Idempotent per ``(fact_id, lower(surface_form))``: the table has no unique
    key for it, so the insert is guarded in SQL. A fact of another tenant gets
    nothing.
    """
    _require_tenant(tenant_id)
    if len(mentions) != len(resolved):
        raise ValueError("write_mentions: one resolution per mention")
    written = 0
    with _cursor(tenant_id, cur) as c:
        for m, r in zip(mentions, resolved, strict=True):
            surface = _clean(m.surface_form or "")[:_MAX_NAME]
            if not surface:
                continue
            c.execute(
                "INSERT INTO memory_mentions (tenant_id, fact_id, entity_id, surface_form, "
                "entity_type, decision_id) SELECT %s, %s, %s, %s, %s, %s "
                "WHERE EXISTS (SELECT 1 FROM memory_facts WHERE id = %s AND tenant_id = %s) "
                "AND NOT EXISTS (SELECT 1 FROM memory_mentions WHERE fact_id = %s "
                "AND lower(surface_form) = lower(%s))",
                (
                    tenant_id,
                    fact_id,
                    r.entity_id,
                    surface,
                    _norm_type(m.entity_type),
                    r.decision_id,
                    fact_id,
                    tenant_id,
                    fact_id,
                    surface,
                ),
            )
            written += c.rowcount
    return written


# ═══ reversal ════════════════════════════════════════════════════════════


class _Rollback(Exception):  # noqa: N818 - a control-flow signal, not an error
    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__("dry run")
        self.report = report


def _rebuild_set(cur: Any, tenant_id: str, fact_ids: Sequence[int]) -> list[int]:
    if not fact_ids:
        return []
    cur.execute(
        "SELECT relation_id AS id FROM memory_relation_evidence WHERE tenant_id = %s "
        "AND fact_id = ANY(%s) UNION SELECT id FROM memory_relations WHERE tenant_id = %s "
        "AND fact_id = ANY(%s)",
        (tenant_id, list(fact_ids), tenant_id, list(fact_ids)),
    )
    return sorted({int(r["id"]) for r in cur.fetchall()})


def _check_entity(cur: Any, tenant_id: str, entity_id: int | None) -> None:
    if entity_id is None:
        return
    cur.execute(
        "SELECT id FROM memory_entities WHERE id = %s AND tenant_id = %s", (entity_id, tenant_id)
    )
    if cur.fetchone() is None:
        raise LookupError(f"no entity {entity_id} in this tenant")


def _correct_facts(
    cur: Any,
    tenant_id: str,
    row: Mapping[str, Any],
    reason: str,
    new_entity_id: int | None,
    fact_ids: Sequence[int],
) -> dict[str, Any]:
    """Re-point the mentions of one decision on the given facts only."""
    decision_id = int(row["id"])
    wanted = sorted({int(f) for f in fact_ids})
    cur.execute(
        "SELECT id, fact_id FROM memory_mentions WHERE tenant_id = %s AND decision_id = %s "
        "AND fact_id = ANY(%s) ORDER BY id",
        (tenant_id, decision_id, wanted),
    )
    mentions = [dict(r) for r in cur.fetchall()]
    missing = sorted(set(wanted) - {int(m["fact_id"]) for m in mentions})
    if missing:
        raise LookupError(f"decision {decision_id} has no mention on fact(s) {missing}")
    relation_ids = _rebuild_set(cur, tenant_id, wanted)
    new_id = _insert_decision(
        cur,
        tenant_id,
        surface=row["surface_form"],
        entity_type=row["entity_type"],
        entity_id=new_entity_id,
        method="operator",
        confidence=1.0,
        evidence={
            "basis": f"operator_facts:{decision_id}:{','.join(map(str, wanted))}",
            "scope": "facts",
            "reason": reason,
            "corrects": decision_id,
            "corrects_entity_id": row["resolved_entity_id"],
            "fact_ids": wanted,
            "normalized": normalize_surface(row["surface_form"]),
            "rebuild": {"fact_ids": wanted, "relation_ids": relation_ids},
        },
    )
    cur.execute(
        "UPDATE memory_mentions SET entity_id = %s, decision_id = %s WHERE tenant_id = %s "
        "AND id = ANY(%s)",
        (new_entity_id, new_id, tenant_id, [int(m["id"]) for m in mentions]),
    )
    return {
        "reversed": False,
        "new_decision_id": new_id,
        "mention_ids": [int(m["id"]) for m in mentions],
        "fact_ids": wanted,
        "relation_ids": relation_ids,
        "cascaded": [],
    }


def _reverse_whole(
    cur: Any,
    tenant_id: str,
    row: Mapping[str, Any],
    reason: str,
    new_entity_id: int | None,
) -> dict[str, Any]:
    decision_id = int(row["id"])
    evidence = row["evidence"] if isinstance(row["evidence"], dict) else {}
    basis = str(evidence.get("basis") or "")
    if row["resolved_entity_id"] is None and new_entity_id is not None:
        raise ValueError(
            f"decision {decision_id} left its name unresolved; settle it per fact "
            "(fact_ids / --fact), not for every mention at once"
        )

    mention_ids: list[int] = []
    fact_set: set[int] = set()
    cascaded: list[int] = []
    if basis.startswith("crm_merge:"):
        # Undo every decision that reached the keeper through this merge, returning
        # its mentions to the node they had before the merge.
        back_to = (evidence.get("crm_merge") or {}).get("from_entity_id")
        cur.execute(
            "SELECT id FROM memory_entities WHERE id = %s AND tenant_id = %s", (back_to, tenant_id)
        )
        back = int(back_to) if back_to is not None and cur.fetchone() else None
        cur.execute(
            "SELECT id, surface_form, entity_type, resolved_entity_id, method, evidence, "
            "reversed_at FROM memory_resolution_decisions WHERE tenant_id = %s "
            "AND reversed_at IS NULL AND superseded_by IS NULL "
            "AND evidence->'via_merge' @> %s::jsonb ORDER BY id FOR UPDATE",
            (tenant_id, json.dumps([decision_id])),
        )
        for derived in [dict(r) for r in cur.fetchall()]:
            sub = _reverse_whole(
                cur, tenant_id, derived, f"{reason} (CRM merge {decision_id} reversed)", back
            )
            cascaded.append(int(derived["id"]))
            mention_ids += sub["mention_ids"]
            fact_set.update(sub["fact_ids"])

    cur.execute(
        "SELECT id, fact_id FROM memory_mentions WHERE tenant_id = %s AND decision_id = %s "
        "ORDER BY id",
        (tenant_id, decision_id),
    )
    own = [dict(r) for r in cur.fetchall()]
    mention_ids += [int(m["id"]) for m in own]
    fact_set.update(int(m["fact_id"]) for m in own)
    fact_ids = sorted(fact_set)
    relation_ids = _rebuild_set(cur, tenant_id, fact_ids)

    new_id: int | None = None
    if new_entity_id is not None:
        new_id = _insert_decision(
            cur,
            tenant_id,
            surface=row["surface_form"],
            entity_type=row["entity_type"],
            entity_id=new_entity_id,
            method="operator",
            confidence=1.0,
            evidence={
                "basis": evidence.get("basis"),
                "reason": reason,
                "reverses": decision_id,
                "replaces_entity_id": row["resolved_entity_id"],
                "normalized": normalize_surface(row["surface_form"]),
            },
        )
    reversal = {
        "reason": reason,
        "superseded_by": new_id,
        "cascaded": cascaded,
        "rebuild": {"fact_ids": fact_ids, "relation_ids": relation_ids},
    }
    cur.execute(
        "UPDATE memory_resolution_decisions SET reversed_at = NOW(), superseded_by = %s, "
        "evidence = evidence || jsonb_build_object('reversal', %s::jsonb) WHERE id = %s",
        (new_id, json.dumps(reversal), decision_id),
    )
    cur.execute(
        "UPDATE memory_mentions SET entity_id = %s, decision_id = COALESCE(%s, decision_id) "
        "WHERE tenant_id = %s AND decision_id = %s",
        (new_entity_id, new_id, tenant_id, decision_id),
    )
    return {
        "reversed": True,
        "new_decision_id": new_id,
        "mention_ids": mention_ids,
        "fact_ids": fact_ids,
        "relation_ids": relation_ids,
        "cascaded": cascaded,
    }


def reverse_decision(
    tenant_id: str,
    decision_id: int,
    *,
    reason: str,
    new_entity_id: int | None = None,
    fact_ids: Sequence[int] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Undo a resolution decision, for every mention under it or for some facts.

    **Whole decision** (``fact_ids`` omitted): marks it reversed (``reversed_at``);
    with ``new_entity_id`` records an ``operator`` decision for that node and
    points ``superseded_by`` at it. Every mention carrying the decision moves to
    that node, or to NULL. A decision is shared by every mention it resolved the
    same way, so this reaches all of them: preview it with ``dry_run``. The name
    is never resolved to the rejected node again. A decision that left its name
    unresolved cannot be settled this way (``ValueError``): settle it per fact.
    Reversing a CRM merge also reverses every decision that reached the keeper
    through it, and their mentions return to the loser's node.

    **Some facts** (``fact_ids``): only the mentions of this decision on those
    facts move, onto a new ``operator`` decision that applies to them alone. The
    decision stays live for every other mention, and nothing about future
    mentions changes. Not for a CRM merge or link.

    Returns the mentions moved, their fact ids and the relation ids whose
    evidence includes any of those facts — the derived rows to rebuild. The same
    set is stored on the decision written (``evidence.reversal.rebuild`` or
    ``evidence.rebuild``), so a rebuild that runs later can find it.
    ``dry_run`` returns the same report and writes nothing (the work is rolled
    back; sequence values may advance). Entity rows are never rewritten.

    Raises ``LookupError`` for a decision, node or fact outside the tenant or
    the decision, and ``ValueError`` for a decision already reversed.
    """
    _require_tenant(tenant_id)
    if not reason or not reason.strip():
        raise ValueError("reverse_decision: a reason is required")
    why = reason.strip()
    try:
        with _cursor(tenant_id) as cur:
            cur.execute(
                "SELECT id, surface_form, entity_type, resolved_entity_id, method, evidence, "
                "reversed_at FROM memory_resolution_decisions WHERE id = %s AND tenant_id = %s "
                "FOR UPDATE",
                (decision_id, tenant_id),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError(f"no decision {decision_id} in this tenant")
            row = dict(row)
            if row["reversed_at"] is not None:
                raise ValueError(f"decision {decision_id} is already reversed")
            _check_entity(cur, tenant_id, new_entity_id)
            if fact_ids is not None:
                basis = str((row["evidence"] or {}).get("basis") or "")
                if basis.startswith(("crm_merge:", "crm_link:")):
                    raise ValueError("a CRM merge or link is reversed as a whole, not per fact")
                if not fact_ids:
                    raise ValueError("fact_ids is empty")
                body = _correct_facts(cur, tenant_id, row, why, new_entity_id, fact_ids)
            else:
                body = _reverse_whole(cur, tenant_id, row, why, new_entity_id)
            report = {
                "tenant_id": tenant_id,
                "decision_id": int(decision_id),
                "dry_run": dry_run,
                "entity_id": new_entity_id,
                "previous_entity_id": row["resolved_entity_id"],
                **body,
            }
            if dry_run:
                raise _Rollback(report)
    except _Rollback as preview:
        return preview.report
    return report


# ═══ backfill ════════════════════════════════════════════════════════════


def _crm_unsynced(cur: Any, tenant_id: str) -> int:
    cur.execute(
        "SELECT (SELECT count(*) FROM crm_people p WHERE p.tenant_id = %s AND p.deleted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM memory_entities e WHERE e.tenant_id = p.tenant_id AND "
        "e.crm_person_id = p.id)) + (SELECT count(*) FROM crm_companies c WHERE c.tenant_id = %s "
        "AND c.deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM memory_entities e WHERE "
        "e.tenant_id = c.tenant_id AND e.crm_company_id = c.id)) AS n",
        (tenant_id, tenant_id),
    )
    return int(cur.fetchone()["n"])


def _fact_mentions(resolver: _Resolver, fact: Mapping[str, Any]) -> list[MentionInput]:
    """The mentions of one legacy fact, from its ``entities`` names."""
    seen: set[str] = set()
    names: list[str] = []
    for n in fact.get("entities") or []:
        if not isinstance(n, str) or is_junk_entity_name(n):
            continue
        key = normalize_surface(n)
        if key in seen:
            continue
        seen.add(key)
        names.append(_clean(n))
    ctx_base: dict[str, Any] = {
        "scope_kind": fact.get("scope_kind") or "shared",
        "scope_id": fact.get("scope_id"),
        "person_id": fact.get("person_id"),
    }
    if fact.get("session_key"):
        ctx_base["conversation_ref"] = fact["session_key"]

    # The fact's person scope is a strong key for the one person it names —
    # only when that name is exactly the CRM person's full name or alias.
    keyed: str | None = None
    pid = fact.get("person_id")
    if pid:
        resolver.prefetch(names)
        scope = _scope_of(ctx_base)
        person_named = [
            n
            for n in names
            if any(
                _norm_type(r["entity_type"]) == PERSON
                and _visible(r, scope)
                and _Resolver._classify(r, normalize_surface(n)) in ("name", "alias")
                for r in resolver._rows.get(normalize_surface(n), [])
            )
        ]
        if len(person_named) == 1:
            resolver._load_people([str(pid)])
            person = resolver._crm.get(str(pid))
            if person is not None:
                full = normalize_surface(_full_name(person))
                if normalize_surface(person_named[0]) == full and len(full.split()) >= 2:
                    keyed = person_named[0]
    out = []
    for n in names:
        ctx = {**ctx_base, "co_mentions": [o for o in names if o != n]}
        keys = {"crm_person_id": str(pid)} if keyed == n else {}
        out.append(MentionInput(n, None, strong_keys=keys, context=ctx))
    return out


def backfill_mentions(
    tenant_id: str,
    *,
    dry_run: bool,
    batch_size: int = 500,
    resume_from: int | None = None,
    max_batches: int | None = None,
) -> dict[str, Any]:
    """Bring existing facts into the mention model. A resumable job, not a migration.

    Walks active facts of the tenant in id order, after ``resume_from``, in
    batches of ``batch_size``, one transaction per batch (under a per-tenant
    advisory lock, so two backfills of one tenant cannot interleave). Each name
    in ``memory_facts.entities`` becomes a mention resolved by the normal rules:
    context from the other names in the fact (co-mentions), its
    ``session_key`` (conversation) and its scope; the fact's ``person_id`` is a
    strong key for the one person-typed name it carries, when that name is the
    CRM person's full name. Names already mentioned for a fact are skipped.

    Returns the last fact id done (``last_fact_id``) — pass it back as
    ``resume_from`` to continue — and whether the walk reached the end
    (``done``). ``max_batches`` bounds one call.

    ``dry_run=True`` writes nothing at all (each batch runs READ ONLY) and
    reports what it would do: counts by method, every unresolved surface form
    with its candidates, the provisional nodes it would create, and for every
    ``full_name`` or ``context`` resolution its evidence. Run
    :func:`sync_crm_entities` first; ``crm_unsynced`` counts CRM records still
    without a node.
    """
    _require_tenant(tenant_id)
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    report: dict[str, Any] = {
        "tenant_id": tenant_id,
        "dry_run": dry_run,
        "facts_scanned": 0,
        "mentions_planned": 0,
        "mentions_written": 0,
        "skipped_existing": 0,
        "by_method": {
            "strong_key": 0,
            "full_name": 0,
            "context": 0,
            "exact": 0,
            "operator": 0,
            "unresolved": 0,
        },
        "unresolved": [],
        "provisional": [],
        "resolutions": [],
        "batches": 0,
        "last_fact_id": resume_from,
        "done": False,
        "crm_unsynced": 0,
        "notes": [UNIQUE_INDEX_NOTE],
    }
    plan = _Plan()
    unresolved_seen: set[tuple[str, str]] = set()
    cursor_id = resume_from or 0
    first = True
    while max_batches is None or report["batches"] < max_batches:
        with _cursor(tenant_id, read_only=dry_run) as cur:
            if first:
                report["crm_unsynced"] = _crm_unsynced(cur, tenant_id)
                first = False
            if not dry_run:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"memory_identity_backfill:{tenant_id}",),
                )
            cur.execute(
                "SELECT id, entities, person_id::text AS person_id, scope_kind, scope_id, "
                "metadata->>'session_key' AS session_key FROM memory_facts "
                "WHERE tenant_id = %s AND is_active = TRUE AND id > %s ORDER BY id LIMIT %s",
                (tenant_id, cursor_id, batch_size),
            )
            facts = [dict(r) for r in cur.fetchall()]
            if not facts:
                report["done"] = True
                break
            cur.execute(
                "SELECT fact_id, lower(surface_form) AS s FROM memory_mentions "
                "WHERE tenant_id = %s AND fact_id = ANY(%s)",
                (tenant_id, [f["id"] for f in facts]),
            )
            existing = {(int(r["fact_id"]), r["s"]) for r in cur.fetchall()}
            resolver = _Resolver(cur, tenant_id, dry_run=dry_run, plan=plan)
            resolver.prefetch(
                [n for f in facts for n in (f.get("entities") or []) if isinstance(n, str)]
            )
            for fact in facts:
                fid = int(fact["id"])
                mentions = _fact_mentions(resolver, fact)
                pending = [m for m in mentions if (fid, m.surface_form.lower()) not in existing]
                report["skipped_existing"] += len(mentions) - len(pending)
                results = [resolver.resolve(m) for m in pending]
                for m, res in zip(pending, results, strict=True):
                    _tally(report, unresolved_seen, resolver, fid, m, res)
                    if (
                        dry_run
                        and res.resolved.entity_id is not None
                        and (res.resolved.method in EVIDENCE_METHODS)
                        and m.context.get("conversation_ref")
                    ):
                        scope = _scope_of(m.context)
                        plan.evidence.append(
                            (
                                str(m.context["conversation_ref"]),
                                res.resolved.entity_id,
                                scope,
                                scope[2],
                            )
                        )
                report["mentions_planned"] += len(pending)
                if not dry_run and pending:
                    report["mentions_written"] += write_mentions(
                        tenant_id, fid, pending, [r.resolved for r in results], cur=cur
                    )
            cursor_id = int(facts[-1]["id"])
            report["last_fact_id"] = cursor_id
            report["facts_scanned"] += len(facts)
            report["batches"] += 1
        if len(facts) < batch_size:
            report["done"] = True
            break
    return report


def _tally(
    report: dict[str, Any],
    seen: set[tuple[str, str]],
    resolver: _Resolver,
    fact_id: int,
    mention: MentionInput,
    res: _Result,
) -> None:
    r, out = res.resolved, res.outcome
    if r.entity_id is None:
        report["by_method"]["unresolved"] += 1
        key = (mention.surface_form, out.reason)
        if key not in seen:
            seen.add(key)
            cands = []
            for cid in out.evidence.get("candidates") or []:
                row = resolver.entity_row(int(cid))
                if row is not None:
                    cands.append(
                        {"id": int(cid), "name": row["name"], "entity_type": row["entity_type"]}
                    )
            report["unresolved"].append(
                {
                    "surface_form": mention.surface_form,
                    "reason": out.reason,
                    "fact_id": fact_id,
                    "candidates": cands,
                }
            )
        return
    report["by_method"][r.method] = report["by_method"].get(r.method, 0) + 1
    if res.created:
        report["provisional"].append(
            {
                "name": r.canonical_name,
                "entity_type": out.create_type or mention.entity_type,
                "fact_id": fact_id,
            }
        )
    if r.method in ("full_name", "context"):
        report["resolutions"].append(
            {
                "fact_id": fact_id,
                "surface_form": mention.surface_form,
                "method": r.method,
                "entity_id": r.entity_id,
                "canonical_name": r.canonical_name,
                "confidence": r.confidence,
                "evidence": {**out.evidence, "basis": out.basis},
            }
        )


# ═══ read side ═══════════════════════════════════════════════════════════


def _public_row(cur: Any, tenant_id: str, ids: Sequence[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    cur.execute(
        f"SELECT {', '.join(ENTITY_COLUMNS)} FROM memory_entities "  # noqa: S608
        "WHERE tenant_id = %s AND id = ANY(%s) ORDER BY id",
        (tenant_id, list(ids)),
    )
    return [dict(r) for r in cur.fetchall()]


def find_entity(
    tenant_id: str, surface_form: str, entity_type: str | None = None, *, cur: Any = None
) -> dict[str, Any] | Ambiguity | None:
    """Look a name up by the resolution rules, read-only. Records nothing.

    An exact name or alias of exactly one shared node of a compatible type
    returns that node (explicit columns, never ``embedding``). Several such
    nodes return an :class:`Ambiguity`. A first name or nickname alone matches
    nothing here: it returns ``None``. A node whose CRM record was merged away
    answers as the winner's node.

    Strong keys are deliberately not used on the read side: callers of
    ``get_entity`` are not data-scoped, and answering an email with the CRM
    person who owns it would disclose what the CRM withholds from restricted
    readers. An email returns ``None``.
    """
    _require_tenant(tenant_id)
    if is_junk_entity_name(surface_form) or _EMAIL_RE.match(_clean(surface_form)):
        return None
    mtype = _norm_type(entity_type)
    scope = ("shared", None, None)
    with _cursor(tenant_id, cur, read_only=cur is None) as c:
        resolver = _Resolver(c, tenant_id, read_only=True)
        resolver.prefetch([surface_form])
        norm = normalize_surface(surface_form)
        found: set[int] = set()
        for r in resolver._rows.get(norm, []):
            if resolver._classify(r, norm) not in ("name", "alias") or not _visible(r, scope):
                continue
            if mtype is not None and _norm_type(r["entity_type"]) != mtype:
                continue
            eff = resolver.effective(r)
            if eff is not None and _visible(eff, scope):
                found.add(int(eff["id"]))
        ids = sorted(found)
        rows = [r for r in _public_row(c, tenant_id, ids) if _visible(r, scope)]
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    return Ambiguity(surface_form, tuple(rows))


__all__ = [
    "CONFIDENCE",
    "ENTITY_COLUMNS",
    "EVIDENCE_METHODS",
    "FREE_MAIL_DOMAINS",
    "Ambiguity",
    "Candidate",
    "MentionInput",
    "Outcome",
    "ResolvedMention",
    "StrongHit",
    "backfill_mentions",
    "decide",
    "find_entity",
    "normalize_surface",
    "record_crm_merge",
    "resolve_mentions",
    "reverse_decision",
    "sync_crm_entities",
    "sync_crm_record",
    "write_mentions",
]
