"""A deterministic synthetic world for the memory eval.

WHY THIS EXISTS

The case-by-case suite (``docs/benchmarks/memory/suite.yaml``) seeds a handful
of facts per case into an otherwise empty tenant. With ~30 candidates fetched
per retrieval leg, the vector leg returns essentially the whole corpus, so the
failure modes the memory rebuild targets — multi-hop questions, two people
called Alice, a hub person who appears in half of everything, histories that
change over time — never appear in it at all.

This module builds one *world* instead: a few hundred people across a few dozen
organisations, with jobs, homes and managers that change over time, projects,
events, CRM rows, documents and multi-session conversation transcripts, and
several thousand dated facts derived from all of it. Cases are generated from
the world's own ground truth rather than written by hand, so every gold answer
is backed by the ids of the facts or transcript segments that support it.

DETERMINISM

``generate_world(seed, size)`` is a pure function of its arguments: one
``random.Random`` per tenant seeded with an integer, fixed calendar bounds, no
wall clock, no iteration over an unordered container. ``world_to_json`` sorts
keys, so the same seed is the same bytes on every machine and under every
``PYTHONHASHSEED`` (the tests check both).

GENERIC DATA

Every name is composed from the word lists below; every address is
``example.com``. Nothing here names a real person or company. This repository
is public.

The dataclasses are the fixture interface other eval lanes consume. Treat a
change to a field as a schema change: bump ``WORLD_SCHEMA_VERSION`` and update
``docs/benchmarks/memory/WORLD.md``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

WORLD_SCHEMA_VERSION = 3

#: Where a fact's knowledge came from. Outcome learning and poisoning
#: defences weigh these differently, so every fact carries exactly one.
ORIGINS: tuple[str, ...] = (
    "operator_confirmed",
    "user_stated",
    "crm",
    "tool",
    "agent_generated",
    "external",
)

STRATA: tuple[str, ...] = (
    "multi_hop",
    "entity_confusion",
    "point_in_time",
    "knowledge_update",
    "abstention",
    "raw_detail",
    "long_tenure",
    "cross_tenant",
    # The multi-hop cases reworded so the question shares no content word with
    # its supporting facts except the names (the originals share their one
    # rare word with the second-hop fact). Same pairs, gold and split.
    "multi_hop_paraphrase",
    # Answers said only in multi-session conversations written for it: a
    # detail deep in a long message, one changed in a later session, one that
    # needs three sessions. Last, so the others keep their order and cases.
    "conversation_recall",
    # A company named by about a hundred extracted facts, and one of its
    # conversations that alone holds the answer: the facts fill the context
    # and the conversation never reaches it. Last, for the same reason.
    "conversation_buried",
)

#: Rewordings of the multi-hop questions, by kind; one is chosen per case by a
#: hash of its key. Each shares no content word with the facts it needs.
PARAPHRASES: dict[str, tuple[str, ...]] = {
    "manager-of-manager": (
        "Who sits one level above {name}'s boss in the org chart?",
        "Who is the boss of {name}'s boss?",
        "Whose team includes the person {name} answers to?",
    ),
    "employer-city": (
        "Which town is home to the main office of the company that employs {name}?",
        "Where is {name}'s employer based?",
        "In what city does the firm {name} is with have its base?",
    ),
    "lead-employer-city": (
        "Which town is home to the main office of the firm that pays whoever heads {name}?",
        "Where is the company based that employs the person in charge of {name}?",
        "In what city does the employer of {name}'s head have its base?",
    ),
}

#: A stratum whose cases are another's, reworded: its id maps to that case's id
#: for the split and the per-stratum cap, so both strata hold the same pairs.
_TWIN_OF = {"multi_hop_paraphrase": "multi_hop"}

SPLITS: tuple[str, ...] = ("dev", "held_out")

#: The hub person is co-mentioned with at least this share of every other
#: entity in the world; every other person stays below half of it.
HUB_MIN_SHARE = 0.5

#: Calendar bounds. Fixed, so a world generated next year is the same world.
WORLD_START = date(2019, 1, 1)
WORLD_END = date(2026, 6, 30)

# --------------------------------------------------------------------------- #
# Word lists (all invented or generic)
# --------------------------------------------------------------------------- #

FIRST_NAMES: tuple[str, ...] = (
    "Alice", "Bob", "Carol", "Dave", "Erin", "Frank", "Grace", "Heidi", "Ivan", "Judy",
    "Kim", "Leo", "Mallory", "Nina", "Oscar", "Peggy", "Quinn", "Rupert", "Sybil", "Trent",
    "Uma", "Victor", "Wendy", "Xavier", "Yara", "Zane", "Amir", "Bea", "Cyrus", "Dana",
    "Elio", "Fay", "Gus", "Hana", "Ida", "Jonah", "Kai", "Lena", "Milo", "Noor",
    "Otto", "Pia", "Rhea", "Sami", "Tess", "Ugo", "Vera", "Wes", "Yusuf", "Zora",
)  # fmt: skip

NICKNAMES: dict[str, str] = {
    "Alice": "Ally", "Bob": "Bobby", "Carol": "Caz", "Dave": "Davo", "Frank": "Frankie",
    "Grace": "Gracie", "Ivan": "Vanya", "Judy": "Jude", "Oscar": "Ozzie", "Peggy": "Peg",
    "Rupert": "Rupe", "Victor": "Vic", "Wendy": "Wen", "Xavier": "Xav", "Jonah": "Jo",
    "Milo": "Mils", "Tess": "Tessie", "Wes": "Westy", "Yusuf": "Yus", "Zora": "Zee",
}  # fmt: skip

_LAST_A = (
    "Ash", "Bram", "Cal", "Dun", "Elm", "Fair", "Glen", "Hol", "Ire", "Kel", "Lark",
    "Mor", "Nor", "Oak", "Pen", "Quill", "Rook", "Sel", "Thorn", "Vale", "Wren", "Yar",
)  # fmt: skip
_LAST_B = (
    "by", "croft", "dale", "field", "ford", "hurst", "ley",
    "mere", "more", "ridge", "stead", "ton", "wick", "worth",
)  # fmt: skip
LAST_NAMES: tuple[str, ...] = tuple(a + b for a in _LAST_A for b in _LAST_B)

_ORG_A = (
    "Bri", "Cal", "Dro", "Elv", "Fen", "Glin", "Har", "Ist", "Jor", "Kel", "Lun", "Mar",
    "Nor", "Orv", "Pel", "Quor", "Ros", "Sil", "Tav", "Ulm", "Ver", "Wyn", "Xan", "Zel",
)  # fmt: skip
_ORG_B_PRIMARY = (
    "vane", "detta", "sel", "aren", "mara", "oway", "trel", "varo",
    "drel", "velle", "ix", "ondo", "astra", "evo", "quin", "ora",
)  # fmt: skip
_ORG_B_SHADOW = ("thane", "bryn", "kosh", "ulle", "zeth", "monde", "rask", "ivo")
_ORG_SUFFIXES = ("Systems", "Labs", "Works", "Partners", "Logistics", "Analytics", "Health")

_CITY_A = (
    "Vel", "Thar", "Quen", "Ors", "Brel", "Dask", "Fyr", "Gol", "Hesk",
    "Jarn", "Kesh", "Lorv", "Mird", "Nask", "Pell", "Rusk", "Sorv", "Tull",
)  # fmt: skip
_CITY_B = ("mere", "holt", "by", "stow", "wick", "ford")
_PRIMARY_CITY_PREFIXES = _CITY_A[:12]
_SHADOW_CITY_PREFIXES = _CITY_A[12:]

PROJECT_NAMES: tuple[str, ...] = (
    "Aurora", "Basalt", "Cinder", "Dynamo", "Ember", "Fjord", "Glacier", "Halcyon",
    "Iris", "Jasper", "Kite", "Lumen", "Monsoon", "Nebula", "Obsidian", "Prism",
    "Quasar", "Ripple", "Sierra", "Tundra", "Umbra", "Vortex", "Willow", "Xenon",
    "Yonder", "Zenith", "Atlas", "Beacon", "Cobalt", "Delta", "Echo", "Falcon",
)  # fmt: skip

ROLES: tuple[str, ...] = (
    "staff engineer", "data analyst", "product manager", "designer", "sales lead",
    "account manager", "operations manager", "finance director", "research lead",
    "support specialist", "recruiter", "solutions architect",
)  # fmt: skip

TECHNOLOGIES: tuple[str, ...] = (
    "Postgres", "Redis", "Kafka", "SQLite", "gRPC", "GraphQL", "RabbitMQ", "Elasticsearch",
)  # fmt: skip

EVENT_KINDS: tuple[str, ...] = (
    "Summit", "Offsite", "Workshop", "Hackathon", "Expo", "Roundtable",
)  # fmt: skip

PREFERENCES: tuple[str, ...] = (
    "async standups over live meetings",
    "written updates over calls",
    "morning meetings",
    "no meetings on Fridays",
    "detailed agendas before every call",
    "short weekly one-to-ones",
    "chat over email",
    "a printed copy of every contract",
)

MEETING_TOPICS: tuple[str, ...] = (
    "hiring plans",
    "the quarterly roadmap",
    "budget planning",
    "a customer escalation",
    "onboarding",
    "the vendor review",
    "the office move",
    "the annual plan",
)

_PET_A = ("Bis", "Mun", "Pip", "Quib", "Tan", "Wuf", "Zor", "Gub", "Noo", "Ruf", "Spud", "Tib")
_PET_B = ("cotto", "go", "kin", "ble", "sy", "fle", "bo", "bins", "dle", "fus")

#: Attributes the world never states about anyone, anywhere. An abstention
#: case asks for one; the tests prove the probe word occurs in no fact, turn
#: or document, so the only correct answer is "unknown".
ABSTENTION_PROBES: tuple[tuple[str, str], ...] = (
    ("blood type", "What is {name}'s blood type?"),
    ("university", "Which university did {name} graduate from?"),
    ("shoe size", "What is {name}'s shoe size?"),
    ("favourite colour", "What is {name}'s favourite colour?"),
)

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


# --------------------------------------------------------------------------- #
# Size
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WorldSize:
    """How big a world to build. ``small`` is for unit tests, ``full`` for the eval."""

    name: str
    people: int
    organisations: int
    projects: int
    locations: int
    events: int
    conversations: int
    raw_details: int
    planted_documents: int
    #: Weights over the number of jobs a person holds across the timeline.
    job_counts: tuple[tuple[int, float], ...]
    #: Weights over the number of homes a person has lived in.
    residence_counts: tuple[tuple[int, float], ...]
    shadow_people: int
    shadow_shared_names: int
    shadow_organisations: int
    shadow_locations: int
    max_cases_per_stratum: int
    #: Conversations of the ``conversation_recall`` stratum (four cases each).
    recall_conversations: int = 0
    #: Companies of the ``conversation_buried`` stratum (three cases each).
    buried_subjects: int = 0


WORLD_SIZES: dict[str, WorldSize] = {
    "small": WorldSize(
        name="small",
        people=60,
        organisations=8,
        projects=6,
        locations=8,
        events=6,
        conversations=3,
        raw_details=24,
        planted_documents=3,
        job_counts=((1, 0.25), (2, 0.4), (3, 0.25), (4, 0.1)),
        residence_counts=((1, 0.3), (2, 0.3), (3, 0.25), (4, 0.15)),
        shadow_people=16,
        shadow_shared_names=10,
        shadow_organisations=4,
        shadow_locations=4,
        max_cases_per_stratum=40,
        recall_conversations=6,
        buried_subjects=4,
    ),
    "full": WorldSize(
        name="full",
        people=320,
        organisations=36,
        projects=30,
        locations=16,
        events=40,
        conversations=12,
        raw_details=240,
        planted_documents=12,
        job_counts=((1, 0.2), (2, 0.4), (3, 0.28), (4, 0.12)),
        residence_counts=((1, 0.25), (2, 0.25), (3, 0.2), (5, 0.18), (8, 0.12)),
        shadow_people=100,
        shadow_shared_names=80,
        shadow_organisations=10,
        shadow_locations=6,
        max_cases_per_stratum=240,
        recall_conversations=40,
        buried_subjects=50,
    ),
}


# --------------------------------------------------------------------------- #
# Fixture schema
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Location:
    id: str
    name: str


@dataclass(frozen=True)
class Organisation:
    id: str
    name: str
    #: ``<slug>.example.com``.
    domain: str
    hq_location_id: str
    #: The fact stating where it is headquartered.
    hq_fact_id: str
    has_crm_row: bool


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    org_id: str
    lead_id: str | None
    start: str
    #: The fact stating who runs and leads it.
    fact_id: str


@dataclass(frozen=True)
class Person:
    id: str
    first_name: str
    last_name: str
    full_name: str
    nickname: str | None
    #: ``"A.V."``.
    initials: str
    #: ``first.last@example.com``, unique within a tenant.
    email: str
    is_hub: bool
    has_crm_row: bool


@dataclass(frozen=True)
class Employment:
    person_id: str
    org_id: str
    role: str
    start: str
    #: ``None`` while current.
    end: str | None
    fact_id: str


@dataclass(frozen=True)
class Residence:
    person_id: str
    location_id: str
    start: str
    end: str | None
    fact_id: str


@dataclass(frozen=True)
class ReportingLine:
    person_id: str
    manager_id: str
    start: str
    end: str | None
    fact_id: str


@dataclass(frozen=True)
class ProjectMembership:
    person_id: str
    project_id: str
    start: str
    fact_id: str


@dataclass(frozen=True)
class Event:
    id: str
    name: str
    date: str
    location_id: str
    host_org_id: str
    attendee_ids: tuple[str, ...]
    #: The fact stating where and by whom it was held.
    fact_id: str


@dataclass(frozen=True)
class Mention:
    """An entity a fact mentions, and the exact text used for it in the fact."""

    entity_id: str
    surface: str


@dataclass(frozen=True)
class SourceRef:
    """Where a fact was derived from.

    ``kind`` is ``conversation`` (``container_id`` a conversation, ``segment_id``
    a turn), ``document`` (a document and one of its sections) or ``crm``
    (``container_id`` is ``"crm"``, ``segment_id`` a CRM row id).
    """

    kind: str
    container_id: str
    segment_id: str


@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    #: One of ``robothor.memory.facts.VALID_CATEGORIES``.
    category: str
    mentions: tuple[Mention, ...]
    #: ISO date the fact became true.
    event_date: str
    source: SourceRef
    #: One of :data:`ORIGINS`.
    origin: str
    #: The fact this one replaced, and the fact that replaced this one.
    supersedes: str | None
    superseded_by: str | None
    valid_from: str
    #: ``None`` while still true.
    valid_to: str | None


@dataclass(frozen=True)
class CrmPerson:
    id: str
    person_id: str
    first_name: str
    last_name: str
    email: str
    job_title: str
    #: ``None`` when the employer has no CRM row of its own.
    company_org_id: str | None
    city: str


@dataclass(frozen=True)
class CrmCompany:
    id: str
    org_id: str
    name: str
    domain: str
    city: str


@dataclass(frozen=True)
class DocumentSection:
    #: ``<document id>#s<n>``.
    id: str
    heading: str
    body: str


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    date: str
    #: ``False`` for planted external material asserting false relations.
    trusted: bool
    origin: str
    sections: tuple[DocumentSection, ...]


@dataclass(frozen=True)
class Turn:
    id: str
    role: str
    text: str
    #: ISO timestamp, UTC.
    at: str


@dataclass(frozen=True)
class ChatSession:
    id: str
    date: str
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    sessions: tuple[ChatSession, ...]


@dataclass(frozen=True)
class RawDetail:
    """A detail said in one transcript turn and recorded in no fact or document."""

    id: str
    person_id: str
    kind: str
    value: str
    turn_id: str
    question: str


@dataclass(frozen=True)
class ConversationRecall:
    """A ``conversation_recall`` question and what answers it, in one conversation.

    ``id`` is the case id without the stratum (``cvr-01:far``); ``kind`` is
    ``near`` / ``far`` (a detail in a long message, beside the question's
    words or far from them), ``update`` (changed in a later session) or
    ``multi_session`` (three sessions). ``rivals`` are wrong answers the
    conversation also states: the superseded value, the other two suppliers.
    """

    id: str
    conversation_id: str
    kind: str
    question: str
    gold: tuple[str, ...]
    rivals: tuple[str, ...]
    turn_ids: tuple[str, ...]


@dataclass(frozen=True)
class PlantedClaim:
    """A false relation asserted by an untrusted document."""

    id: str
    document_id: str
    fact_id: str
    subject_id: str
    relation: str
    claimed_object: str
    true_object: str


@dataclass(frozen=True)
class Case:
    id: str
    stratum: str
    question: str
    #: Acceptable answers; any one is correct. ``("unknown",)`` for abstention.
    gold: tuple[str, ...]
    #: Fact, turn, section or CRM row ids that together support the answer.
    supporting_ids: tuple[str, ...]
    #: ISO date the question is asked about, for point-in-time cases.
    as_of: str | None
    split: str
    subject_id: str
    #: entity_confusion: the person with the same first name.
    confuser_id: str | None = None
    #: long_tenure: how many values the asked-about attribute has held.
    history_length: int | None = None
    #: cross_tenant: answers that belong to the other tenant and must not surface.
    forbidden: tuple[str, ...] = ()
    #: abstention: the attribute the world never states.
    probe: str | None = None


@dataclass(frozen=True)
class World:
    seed: int
    size: str
    #: ``primary`` or ``shadow``.
    tenant_key: str
    hub_id: str | None
    people: tuple[Person, ...]
    organisations: tuple[Organisation, ...]
    projects: tuple[Project, ...]
    locations: tuple[Location, ...]
    employments: tuple[Employment, ...]
    residences: tuple[Residence, ...]
    reporting_lines: tuple[ReportingLine, ...]
    memberships: tuple[ProjectMembership, ...]
    events: tuple[Event, ...]
    facts: tuple[Fact, ...]
    crm_people: tuple[CrmPerson, ...]
    crm_companies: tuple[CrmCompany, ...]
    documents: tuple[Document, ...]
    conversations: tuple[Conversation, ...]
    raw_details: tuple[RawDetail, ...]
    planted_claims: tuple[PlantedClaim, ...]
    cases: tuple[Case, ...]
    #: The second tenant's world (shares some person names, nothing else).
    shadow: World | None = None
    #: What the ``conversation_recall`` conversations answer (primary only).
    conversation_recall: tuple[ConversationRecall, ...] = ()
    #: What the ``conversation_buried`` conversations answer (primary only).
    conversation_buried: tuple[ConversationRecall, ...] = ()


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #


def _pair_id(case_id: str) -> str:
    """The id a reworded twin shares its split and cap with (itself otherwise)."""
    stratum, _, rest = case_id.partition(":")
    return f"{_TWIN_OF[stratum]}:{rest}" if stratum in _TWIN_OF else case_id


def split_for(case_id: str) -> str:
    """``dev`` or ``held_out`` from a hash of the case id alone.

    Independent of the seed and of the order cases are generated in, so a case
    keeps its split for as long as it keeps its id. A reworded twin
    (``multi_hop_paraphrase``) takes the split of the case it rewords, so a
    question and its rewording never sit on both sides.
    """
    key = _pair_id(case_id)
    return "dev" if hashlib.sha256(key.encode("utf-8")).digest()[0] % 2 == 0 else "held_out"


def world_cases(world: World, split: str = "all") -> list[Case]:
    """The world's cases in one split (``dev``, ``held_out``) or ``all``."""
    if split == "all":
        return list(world.cases)
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r} (valid: all, {', '.join(SPLITS)})")
    return [c for c in world.cases if c.split == split]


def world_to_dict(world: World) -> dict[str, Any]:
    data = dataclasses.asdict(world)
    data["schema_version"] = WORLD_SCHEMA_VERSION
    return data


def world_to_json(world: World) -> str:
    """Canonical serialisation: same world, same bytes."""
    return json.dumps(world_to_dict(world), sort_keys=True, ensure_ascii=False, indent=1)


def generate_world(seed: int, size: str | WorldSize = "full") -> World:
    """Build the primary world, its shadow tenant and its cases from ``seed``."""
    if isinstance(size, WorldSize):
        spec = size
    else:
        if size not in WORLD_SIZES:
            raise ValueError(f"unknown world size {size!r} (valid: {', '.join(WORLD_SIZES)})")
        spec = WORLD_SIZES[size]

    primary = _TenantBuilder(
        random.Random(seed),
        prefix="",
        tenant_key="primary",
        n_people=spec.people,
        n_orgs=spec.organisations,
        n_projects=spec.projects,
        n_locations=spec.locations,
        n_events=spec.events,
        n_conversations=spec.conversations,
        n_raw=spec.raw_details,
        n_planted=spec.planted_documents,
        job_counts=spec.job_counts,
        residence_counts=spec.residence_counts,
        org_second=_ORG_B_PRIMARY,
        city_prefixes=_PRIMARY_CITY_PREFIXES,
        with_hub=True,
        name_collisions=2 if spec.name == "full" else 1,
    ).build()

    shared_pool = [p for p in primary.people if not p.is_hub]
    shadow_rng = random.Random(seed * 1_000_003 + 17)
    shared = shadow_rng.sample(shared_pool, min(spec.shadow_shared_names, len(shared_pool)))
    shadow = _TenantBuilder(
        shadow_rng,
        prefix="sh-",
        tenant_key="shadow",
        n_people=spec.shadow_people,
        n_orgs=spec.shadow_organisations,
        n_projects=2,
        n_locations=spec.shadow_locations,
        n_events=2,
        n_conversations=2,
        n_raw=0,
        n_planted=0,
        job_counts=spec.job_counts,
        residence_counts=((1, 0.5), (2, 0.5)),
        org_second=_ORG_B_SHADOW,
        city_prefixes=_SHADOW_CITY_PREFIXES,
        with_hub=False,
        name_collisions=0,
        forced_names=[(p.first_name, p.last_name) for p in sorted(shared, key=lambda p: p.id)],
    ).build()

    cases = _CaseBuilder(primary, shadow, spec.max_cases_per_stratum).build()
    # Its own random stream, built after everything else: the rest of the
    # world, and every other stratum's cases, are what they were without it.
    recall_convs, recall = _conversation_recall(
        random.Random(seed * 1_000_033 + 911), spec.recall_conversations
    )
    cases += _recall_cases(recall, spec.max_cases_per_stratum)
    built = primary.world(seed, spec.name, cases=())
    # Its own stream too, after conversation_recall: everything above is
    # what it was without it. It adds companies, facts and conversations.
    buried_orgs, buried_facts, buried_convs, buried = _conversation_buried(
        random.Random(seed * 1_000_037 + 912), spec.buried_subjects, built.locations
    )
    cases += _recall_cases(
        buried,
        spec.max_cases_per_stratum,
        "conversation_buried",
        subjects={c.id: _buried_org_id(c.id) for c in buried_convs},
    )
    return dataclasses.replace(
        built,
        organisations=built.organisations + buried_orgs,
        facts=built.facts + buried_facts,
        conversations=built.conversations + recall_convs + buried_convs,
        conversation_recall=recall,
        conversation_buried=buried,
        cases=tuple(cases),
        shadow=shadow.world(seed, spec.name, cases=()),
    )


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


def _d(value: date) -> str:
    return value.isoformat()


def _month_year(iso: str) -> str:
    y, m, _ = iso.split("-")
    return f"{_MONTHS[int(m) - 1]} {y}"


def _long_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{int(d)} {_MONTHS[int(m) - 1]} {y}"


def _weighted(rng: random.Random, weights: tuple[tuple[int, float], ...]) -> int:
    values = [v for v, _ in weights]
    return rng.choices(values, weights=[w for _, w in weights], k=1)[0]


@dataclass
class _Slot:
    entity_id: str
    surface: str
    full: str
    is_person: bool


@dataclass
class _Draft:
    """A fact before ids are final and duplicate text is resolved."""

    id: str
    template: str
    slots: list[_Slot]
    category: str
    event_date: str
    route: str  # conversation | document:<key> | crm:<row id> | planted:<doc id>
    supersedes: str | None = None
    superseded_by: str | None = None
    valid_to: str | None = None
    origin: str = ""
    source: SourceRef | None = None

    def text(self) -> str:
        return self.template.format(*[s.surface for s in self.slots])


@dataclass
class _Exchange:
    user: str
    assistant: str
    user_fact: str | None = None
    assistant_fact: str | None = None
    raw_id: str | None = None


class _TenantBuilder:
    def __init__(
        self,
        rng: random.Random,
        *,
        prefix: str,
        tenant_key: str,
        n_people: int,
        n_orgs: int,
        n_projects: int,
        n_locations: int,
        n_events: int,
        n_conversations: int,
        n_raw: int,
        n_planted: int,
        job_counts: tuple[tuple[int, float], ...],
        residence_counts: tuple[tuple[int, float], ...],
        org_second: tuple[str, ...],
        city_prefixes: tuple[str, ...],
        with_hub: bool,
        name_collisions: int,
        forced_names: list[tuple[str, str]] | None = None,
    ) -> None:
        self.rng = rng
        self.p = prefix
        self.tenant_key = tenant_key
        self.n_people = n_people
        self.n_orgs = n_orgs
        self.n_projects = n_projects
        self.n_locations = n_locations
        self.n_events = n_events
        self.n_conversations = n_conversations
        self.n_raw = n_raw
        self.n_planted = n_planted
        self.job_counts = job_counts
        self.residence_counts = residence_counts
        self.org_second = org_second
        self.city_prefixes = city_prefixes
        self.with_hub = with_hub
        self.name_collisions = name_collisions
        self.forced_names = forced_names or []

        self.drafts: list[_Draft] = []
        self.people: list[Person] = []
        self.orgs: list[dict[str, Any]] = []
        self.locations: list[Location] = []
        self.projects: list[dict[str, Any]] = []
        self.employments: list[dict[str, Any]] = []
        self.residences: list[dict[str, Any]] = []
        self.reporting: list[dict[str, Any]] = []
        self.memberships: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.crm_people: list[CrmPerson] = []
        self.crm_companies: list[CrmCompany] = []
        self.planted_claims: list[dict[str, Any]] = []
        self.raw_details: list[dict[str, Any]] = []
        self.hub_id: str | None = None
        self.facts: list[Fact] = []
        self.documents: list[Document] = []
        self.conversations: list[Conversation] = []

    # ---- ids / dates / surfaces ---------------------------------------- #

    def _fid(self) -> str:
        return f"{self.p}fct-{len(self.drafts) + 1:05d}"

    def _date_between(self, lo: date, hi: date) -> date:
        if hi <= lo:
            return lo
        return lo + timedelta(days=self.rng.randint(0, (hi - lo).days))

    def _person_slot(self, person: Person, *, canonical: bool = False) -> _Slot:
        """A surface form for a person.

        Answer-bearing mentions (``canonical``) use the full name, the email or
        a case variant of the full name — forms a question can be graded on.
        Subject mentions vary freely, including the ambiguous first name.
        """
        r = self.rng.random()
        if canonical:
            if r < 0.8:
                surface = person.full_name
            elif r < 0.9:
                surface = person.email
            else:
                surface = person.full_name.lower()
        elif r < 0.45:
            surface = person.full_name
        elif r < 0.65:
            surface = person.first_name
        elif r < 0.75:
            surface = person.nickname or person.full_name
        elif r < 0.8:
            surface = person.initials
        elif r < 0.85:
            surface = person.email
        elif r < 0.9:
            surface = person.full_name.lower()
        else:
            surface = person.full_name
        return _Slot(person.id, surface, person.full_name, True)

    @staticmethod
    def _slot(entity_id: str, name: str) -> _Slot:
        return _Slot(entity_id, name, name, False)

    def _add(
        self,
        template: str,
        slots: list[_Slot],
        category: str,
        event_date: str,
        route: str = "conversation",
        *,
        supersedes: str | None = None,
    ) -> str:
        draft = _Draft(self._fid(), template, slots, category, event_date, route)
        if supersedes is not None:
            old = self._draft(supersedes)
            old.superseded_by = draft.id
            old.valid_to = event_date
            draft.supersedes = supersedes
        self.drafts.append(draft)
        return draft.id

    def _draft(self, fact_id: str) -> _Draft:
        return self.drafts[int(fact_id.rsplit("-", 1)[1]) - 1]

    # ---- entities ------------------------------------------------------- #

    def _make_locations(self) -> None:
        names = sorted({a + b for a in self.city_prefixes for b in _CITY_B})
        chosen = self.rng.sample(names, self.n_locations)
        self.locations = [
            Location(f"{self.p}loc-{i + 1:02d}", name) for i, name in enumerate(chosen)
        ]

    def _make_orgs(self) -> None:
        roots = sorted({a + b for a in _ORG_A for b in self.org_second})
        chosen = self.rng.sample(roots, self.n_orgs)
        for i, root in enumerate(chosen):
            # The first few keep a bare name so a project can share it exactly.
            bare = i < self.name_collisions
            name = root if bare else f"{root} {self.rng.choice(_ORG_SUFFIXES)}"
            self.orgs.append(
                {
                    "id": f"{self.p}org-{i + 1:03d}",
                    "name": name,
                    "domain": f"{root.lower()}.example.com",
                    "hq": self.rng.choice(self.locations).id,
                    "crm": False,
                }
            )

    def _make_people(self) -> None:
        used: set[tuple[str, str]] = set()
        names: list[tuple[str, str]] = []
        for pair in self.forced_names:
            if pair not in used:
                used.add(pair)
                names.append(pair)
        while len(names) < self.n_people:
            pair = (self.rng.choice(FIRST_NAMES), self.rng.choice(LAST_NAMES))
            if pair in used:
                continue
            used.add(pair)
            names.append(pair)
        for i, (first, last) in enumerate(names[: self.n_people]):
            nickname = NICKNAMES.get(first) if self.rng.random() < 0.6 else None
            self.people.append(
                Person(
                    id=f"{self.p}per-{i + 1:04d}",
                    first_name=first,
                    last_name=last,
                    full_name=f"{first} {last}",
                    nickname=nickname,
                    initials=f"{first[0]}.{last[0]}.",
                    email=f"{first.lower()}.{last.lower()}@example.com",
                    is_hub=self.with_hub and i == 0,
                    has_crm_row=False,
                )
            )
        if self.with_hub:
            self.hub_id = self.people[0].id

    # ---- timelines ------------------------------------------------------ #

    def _org(self, org_id: str) -> dict[str, Any]:
        return self.orgs[int(org_id.rsplit("-", 1)[1]) - 1]

    def _loc(self, loc_id: str) -> Location:
        return self.locations[int(loc_id.rsplit("-", 1)[1]) - 1]

    def _person(self, pid: str) -> Person:
        return self.people[int(pid.rsplit("-", 1)[1]) - 1]

    def _make_employments(self) -> None:
        for person in self.people:
            if person.is_hub:
                n_jobs = 1
                cursor = WORLD_START
            else:
                n_jobs = _weighted(self.rng, self.job_counts)
                cursor = self._date_between(WORLD_START, WORLD_START + timedelta(days=500))
            span = (WORLD_END - timedelta(days=120) - cursor).days
            stint = max(200, span // n_jobs)
            prev_fact: str | None = None
            prev_org: str | None = None
            for j in range(n_jobs):
                start = cursor + timedelta(days=j * stint + (self.rng.randint(0, 40) if j else 0))
                if start > WORLD_END - timedelta(days=60):
                    break
                if person.is_hub:
                    org_id = self.orgs[0]["id"]
                else:
                    choices = [o["id"] for o in self.orgs if o["id"] != prev_org]
                    org_id = self.rng.choice(choices)
                role = self.rng.choice(ROLES)
                s = self._person_slot(person)
                org_slot = self._slot(org_id, self._org(org_id)["name"])
                if prev_org is None:
                    fid = self._add(
                        "In " + _month_year(_d(start)) + ", {0} joined {1} as " + role + ".",
                        [s, org_slot],
                        "contact",
                        _d(start),
                    )
                else:
                    fid = self._add(
                        "In "
                        + _month_year(_d(start))
                        + ", {0} left {1} to join {2} as "
                        + role
                        + ".",
                        [s, self._slot(prev_org, self._org(prev_org)["name"]), org_slot],
                        "contact",
                        _d(start),
                        supersedes=prev_fact,
                    )
                if self.employments and self.employments[-1]["person_id"] == person.id:
                    self.employments[-1]["end"] = _d(start)
                self.employments.append(
                    {
                        "person_id": person.id,
                        "org_id": org_id,
                        "role": role,
                        "start": _d(start),
                        "end": None,
                        "fact_id": fid,
                    }
                )
                prev_fact, prev_org = fid, org_id

    def _make_residences(self) -> None:
        for person in self.people:
            n = 1 if person.is_hub else _weighted(self.rng, self.residence_counts)
            n = min(n, len(self.locations))
            cursor = self._date_between(WORLD_START, WORLD_START + timedelta(days=200))
            gap = max(90, (WORLD_END - timedelta(days=60) - cursor).days // n)
            prev_loc: str | None = None
            prev_fact: str | None = None
            for k in range(n):
                start = cursor + timedelta(days=k * gap + (self.rng.randint(0, 20) if k else 0))
                loc = self.rng.choice([loc for loc in self.locations if loc.id != prev_loc])
                s = self._person_slot(person)
                if prev_loc is None:
                    fid = self._add(
                        "Since " + _month_year(_d(start)) + ", {0} has lived in {1}.",
                        [s, self._slot(loc.id, loc.name)],
                        "personal",
                        _d(start),
                    )
                else:
                    fid = self._add(
                        "In " + _month_year(_d(start)) + ", {0} moved from {1} to {2}.",
                        [
                            s,
                            self._slot(prev_loc, self._loc(prev_loc).name),
                            self._slot(loc.id, loc.name),
                        ],
                        "personal",
                        _d(start),
                        supersedes=prev_fact,
                    )
                if self.residences and self.residences[-1]["person_id"] == person.id:
                    self.residences[-1]["end"] = _d(start)
                self.residences.append(
                    {
                        "person_id": person.id,
                        "location_id": loc.id,
                        "start": _d(start),
                        "end": None,
                        "fact_id": fid,
                    }
                )
                prev_loc, prev_fact = loc.id, fid

    def current_job(self, pid: str) -> dict[str, Any]:
        return next(e for e in reversed(self.employments) if e["person_id"] == pid)

    def current_home(self, pid: str) -> dict[str, Any]:
        return next(r for r in reversed(self.residences) if r["person_id"] == pid)

    def _make_reporting(self) -> None:
        by_org: dict[str, list[str]] = {}
        for person in self.people:
            by_org.setdefault(self.current_job(person.id)["org_id"], []).append(person.id)
        for org_id in sorted(by_org):
            staff = sorted(by_org[org_id])
            if len(staff) < 2:
                continue
            head = self.hub_id if self.hub_id in staff else staff[0]
            rest = [p for p in staff if p != head]
            n_mgr = max(1, len(rest) // 6) if len(rest) >= 3 else 0
            managers = sorted(self.rng.sample(rest, n_mgr)) if n_mgr else []
            bosses = [head, *managers]
            for pid in rest:
                boss = head if pid in managers or not managers else self.rng.choice(bosses)
                self._report(pid, boss, [b for b in bosses if b not in (pid, boss)])

    def _report(self, pid: str, boss: str, alternatives: list[str]) -> None:
        job_start = date.fromisoformat(self.current_job(pid)["start"])
        boss_start = date.fromisoformat(self.current_job(boss)["start"])
        start = max(job_start, boss_start)
        person = self._person(pid)
        change: tuple[str, date] | None = None
        if alternatives and self.rng.random() < 0.35:
            old = self.rng.choice(alternatives)
            old_start = max(job_start, date.fromisoformat(self.current_job(old)["start"]))
            mid = old_start + (WORLD_END - old_start) // 2
            switch = max(mid, boss_start + timedelta(days=30))
            if switch < WORLD_END - timedelta(days=30) and (switch - old_start).days >= 90:
                change = (old, old_start)
                start = switch
        if change is not None:
            old, old_start = change
            first = self._add(
                "Since " + _month_year(_d(old_start)) + ", {0} has reported to {1}.",
                [self._person_slot(person), self._person_slot(self._person(old), canonical=True)],
                "contact",
                _d(old_start),
            )
            self.reporting.append(
                {
                    "person_id": pid,
                    "manager_id": old,
                    "start": _d(old_start),
                    "end": _d(start),
                    "fact_id": first,
                }
            )
            fid = self._add(
                "In " + _month_year(_d(start)) + ", {0} started reporting to {1} instead of {2}.",
                [
                    self._person_slot(person),
                    self._person_slot(self._person(boss), canonical=True),
                    self._person_slot(self._person(old)),
                ],
                "contact",
                _d(start),
                supersedes=first,
            )
        else:
            fid = self._add(
                "Since " + _month_year(_d(start)) + ", {0} has reported to {1}.",
                [self._person_slot(person), self._person_slot(self._person(boss), canonical=True)],
                "contact",
                _d(start),
            )
        self.reporting.append(
            {"person_id": pid, "manager_id": boss, "start": _d(start), "end": None, "fact_id": fid}
        )

    def current_manager(self, pid: str) -> dict[str, Any] | None:
        return next((r for r in self.reporting if r["person_id"] == pid and r["end"] is None), None)

    # ---- organisations, projects, events -------------------------------- #

    def _make_org_facts(self) -> None:
        for org in self.orgs:
            loc = self._loc(org["hq"])
            org["fact_id"] = self._add(
                "{0} is headquartered in {1}.",
                [self._slot(org["id"], org["name"]), self._slot(loc.id, loc.name)],
                "contact",
                _d(WORLD_START),
                route=f"document:org:{org['id']}",
            )

    def _make_projects(self) -> None:
        names = list(PROJECT_NAMES)
        self.rng.shuffle(names)
        collide = [o["name"] for o in self.orgs[: self.name_collisions]]
        names = collide + [n for n in names if n not in collide]
        for i in range(self.n_projects):
            pid = f"{self.p}prj-{i + 1:03d}"
            name = names[i]
            # A project sharing an organisation's name is owned by a different one.
            owners = [o for o in self.orgs if o["name"] != name]
            owner = self.rng.choice(owners)
            start = self._date_between(WORLD_START, WORLD_END - timedelta(days=400))
            staff = sorted(
                p.id for p in self.people if self.current_job(p.id)["org_id"] == owner["id"]
            )
            lead = self.rng.choice(staff) if staff else None
            route = f"document:prj:{pid}:overview"
            pslot = self._slot(pid, name)
            oslot = self._slot(owner["id"], owner["name"])
            if lead:
                fid = self._add(
                    "The {0} project is run by {1} and led by {2}.",
                    [pslot, oslot, self._person_slot(self._person(lead), canonical=True)],
                    "project",
                    _d(start),
                    route=route,
                )
            else:
                fid = self._add(
                    "The {0} project is run by {1}.", [pslot, oslot], "project", _d(start), route
                )
            self.projects.append(
                {
                    "id": pid,
                    "name": name,
                    "org_id": owner["id"],
                    "lead_id": lead,
                    "start": _d(start),
                    "fact_id": fid,
                }
            )
            techs = self.rng.sample(TECHNOLOGIES, 2)
            when = self._date_between(start, start + timedelta(days=200))
            self._add(
                "In " + _month_year(_d(when)) + ", the {0} team decided to adopt " + techs[0] + ".",
                [self._slot(pid, name)],
                "decision",
                _d(when),
                route=route,
            )
            self._add(
                "The {0} project's services run on " + techs[1] + ".",
                [self._slot(pid, name)],
                "technical",
                _d(start),
                route=route,
            )
            members = self.rng.sample(
                [p for p in self.people if not p.is_hub], min(len(self.people) - 1, 3 + i % 5)
            )
            if self.hub_id and self.rng.random() < 0.5:
                members.append(self._person(self.hub_id))
            for m in sorted(members, key=lambda p: p.id):
                joined = self._date_between(start, start + timedelta(days=300))
                mid = self._add(
                    "In " + _month_year(_d(joined)) + ", {0} joined the {1} project.",
                    [self._person_slot(m), self._slot(pid, name)],
                    "project",
                    _d(joined),
                    route=f"document:prj:{pid}:team",
                )
                self.memberships.append(
                    {"person_id": m.id, "project_id": pid, "start": _d(joined), "fact_id": mid}
                )

    def _make_events(self) -> None:
        used: set[str] = set()
        for i in range(self.n_events):
            loc = self.rng.choice(self.locations)
            when = self._date_between(WORLD_START, WORLD_END - timedelta(days=10))
            name = f"{loc.name} {self.rng.choice(EVENT_KINDS)} {when.year}"
            n = 2
            while name in used:
                name = f"{loc.name} {self.rng.choice(EVENT_KINDS)} {when.year} ({n})"
                n += 1
            used.add(name)
            host = self.rng.choice(self.orgs)
            eid = f"{self.p}evt-{i + 1:03d}"
            fid = self._add(
                "The {0} took place in {1}, hosted by {2}.",
                [
                    self._slot(eid, name),
                    self._slot(loc.id, loc.name),
                    self._slot(host["id"], host["name"]),
                ],
                "event",
                _d(when),
            )
            pool = [p for p in self.people if not p.is_hub]
            attendees = self.rng.sample(pool, min(len(pool), self.rng.randint(3, 8)))
            if self.hub_id and self.rng.random() < 0.85:
                attendees.append(self._person(self.hub_id))
            attendees.sort(key=lambda p: p.id)
            for a in attendees:
                self._add(
                    "{0} attended the {1} on " + _long_date(_d(when)) + ".",
                    [self._person_slot(a), self._slot(eid, name)],
                    "event",
                    _d(when),
                )
            self.events.append(
                {
                    "id": eid,
                    "name": name,
                    "date": _d(when),
                    "location_id": loc.id,
                    "host_org_id": host["id"],
                    "attendee_ids": tuple(a.id for a in attendees),
                    "fact_id": fid,
                }
            )

    def _make_personal_facts(self) -> None:
        hub = self._person(self.hub_id) if self.hub_id else None
        for person in self.people:
            if hub is not None and not person.is_hub and self.rng.random() < 0.8:
                when = self._date_between(WORLD_START + timedelta(days=200), WORLD_END)
                self._add(
                    "In "
                    + _month_year(_d(when))
                    + ", {0} met {1} to discuss "
                    + self.rng.choice(MEETING_TOPICS)
                    + ".",
                    [self._person_slot(hub), self._person_slot(person)],
                    "event",
                    _d(when),
                )
            if self.rng.random() < 0.7:
                when = self._date_between(WORLD_START, WORLD_END)
                self._add(
                    "{0} prefers " + self.rng.choice(PREFERENCES) + ".",
                    [self._person_slot(person)],
                    "preference",
                    _d(when),
                )
            if person.nickname and self.rng.random() < 0.5:
                self._add(
                    "{0} goes by " + person.nickname + ".",
                    [self._slot(person.id, person.full_name)],
                    "personal",
                    _d(self._date_between(WORLD_START, WORLD_END)),
                )
            if self.rng.random() < 0.25:
                self._add(
                    "{0}'s work email is {1}.",
                    [
                        self._slot(person.id, person.full_name),
                        _Slot(person.id, person.email, person.full_name, False),
                    ],
                    "contact",
                    _d(self._date_between(WORLD_START, WORLD_END)),
                )

    # ---- CRM ------------------------------------------------------------ #

    def _make_crm(self) -> None:
        for i, org in enumerate(self.orgs):
            # The last organisation never has a CRM row, the first always does.
            org["crm"] = i == 0 or (i < len(self.orgs) - 1 and self.rng.random() < 0.8)
            if org["crm"]:
                self.crm_companies.append(
                    CrmCompany(
                        id=f"{self.p}crm-org-{i + 1:03d}",
                        org_id=org["id"],
                        name=org["name"],
                        domain=org["domain"],
                        city=self._loc(org["hq"]).name,
                    )
                )
        people: list[Person] = []
        for i, person in enumerate(self.people):
            has = person.is_hub or (i < len(self.people) - 1 and self.rng.random() < 0.7)
            people.append(dataclasses.replace(person, has_crm_row=has))
            if not has:
                continue
            job = self.current_job(person.id)
            org = self._org(job["org_id"])
            row_id = f"{self.p}crm-per-{i + 1:04d}"
            self.crm_people.append(
                CrmPerson(
                    id=row_id,
                    person_id=person.id,
                    first_name=person.first_name,
                    last_name=person.last_name,
                    email=person.email,
                    job_title=job["role"],
                    company_org_id=org["id"] if org["crm"] else None,
                    city=self._loc(self.current_home(person.id)["location_id"]).name,
                )
            )
            self._add(
                "CRM record for {0}: " + job["role"] + " at {1}, contact {2}.",
                [
                    self._slot(person.id, person.full_name),
                    self._slot(org["id"], org["name"]),
                    _Slot(person.id, person.email, person.full_name, False),
                ],
                "contact",
                job["start"],
                route=f"crm:{row_id}",
            )
        self.people = people

    # ---- planted material ----------------------------------------------- #

    def _make_planted(self) -> None:
        candidates = [p for p in self.people if not p.is_hub]
        for d in range(self.n_planted):
            doc_id = f"{self.p}doc-x{d + 1:02d}"
            when = _d(self._date_between(WORLD_END - timedelta(days=400), WORLD_END))
            for k in range(1 + d % 2):
                if (d + k) % 2 == 0:
                    person = self.rng.choice(candidates)
                    true_org = self._org(self.current_job(person.id)["org_id"])
                    wrong = self.rng.choice([o for o in self.orgs if o["id"] != true_org["id"]])
                    fid = self._add(
                        "According to an unverified newsletter, {0} has been appointed "
                        "chief executive of {1}.",
                        [
                            self._slot(person.id, person.full_name),
                            self._slot(wrong["id"], wrong["name"]),
                        ],
                        "contact",
                        when,
                        route=f"planted:{doc_id}",
                    )
                    claim = (person.id, "works_at", wrong["name"], true_org["name"])
                else:
                    org = self.rng.choice(self.orgs)
                    true_loc = self._loc(org["hq"])
                    wrong_loc = self.rng.choice([x for x in self.locations if x.id != true_loc.id])
                    fid = self._add(
                        "An anonymous post claims {0} has moved its headquarters to {1}.",
                        [
                            self._slot(org["id"], org["name"]),
                            self._slot(wrong_loc.id, wrong_loc.name),
                        ],
                        "contact",
                        when,
                        route=f"planted:{doc_id}",
                    )
                    claim = (org["id"], "headquartered_in", wrong_loc.name, true_loc.name)
                self.planted_claims.append(
                    {
                        "id": f"{self.p}clm-{len(self.planted_claims) + 1:03d}",
                        "document_id": doc_id,
                        "fact_id": fid,
                        "subject_id": claim[0],
                        "relation": claim[1],
                        "claimed_object": claim[2],
                        "true_object": claim[3],
                    }
                )

    # ---- resolution of text, origins and sources ------------------------ #

    def _resolve_duplicates(self) -> None:
        seen: set[tuple[str, str]] = set()
        for draft in self.drafts:
            key = (draft.text(), draft.category)
            if key in seen:
                for slot in draft.slots:
                    if slot.is_person:
                        slot.surface = slot.full
                key = (draft.text(), draft.category)
            if key in seen:
                raise RuntimeError(f"generator produced duplicate fact text: {key[0]!r}")
            seen.add(key)

    def _assign_origins(self) -> None:
        for draft in self.drafts:
            if draft.route.startswith("planted:"):
                draft.origin = "external"
            elif draft.route.startswith("crm:"):
                draft.origin = "crm"
            elif draft.route.startswith("document:"):
                draft.origin = "tool"
            else:
                r = self.rng.random()
                draft.origin = (
                    "user_stated"
                    if r < 0.6
                    else "operator_confirmed"
                    if r < 0.85
                    else "agent_generated"
                )

    def _build_documents(self) -> None:
        sections: dict[str, list[_Draft]] = {}
        for draft in self.drafts:
            if draft.route.startswith(("document:", "planted:")):
                sections.setdefault(draft.route, []).append(draft)

        n = 0

        def _doc(
            title: str, routes: list[tuple[str, str, str]], trusted: bool, doc_id: str
        ) -> None:
            built: list[DocumentSection] = []
            dates: list[str] = []
            for k, (route, heading, empty) in enumerate(routes):
                sec_id = f"{doc_id}#s{k + 1}"
                members = sections.get(route, [])
                for dr in members:
                    dr.source = SourceRef("document", doc_id, sec_id)
                    dates.append(dr.event_date)
                body = " ".join(dr.text() for dr in members) or empty
                built.append(DocumentSection(sec_id, heading, body))
            self.documents.append(
                Document(
                    id=doc_id,
                    title=title,
                    date=max(dates) if dates else _d(WORLD_START),
                    trusted=trusted,
                    origin="tool" if trusted else "external",
                    sections=tuple(built),
                )
            )

        for prj in self.projects:
            n += 1
            _doc(
                f"{prj['name']} project charter",
                [
                    (f"document:prj:{prj['id']}:overview", "Overview", ""),
                    (
                        f"document:prj:{prj['id']}:team",
                        "Team",
                        f"The {prj['name']} team roster is maintained here.",
                    ),
                ],
                True,
                f"{self.p}doc-{n:03d}",
            )
        for org in self.orgs:
            n += 1
            _doc(
                f"{org['name']} company profile",
                [(f"document:org:{org['id']}", "Overview", "")],
                True,
                f"{self.p}doc-{n:03d}",
            )
        for d in range(self.n_planted):
            doc_id = f"{self.p}doc-x{d + 1:02d}"
            _doc(
                f"Industry rumour digest {d + 1}",
                [(f"planted:{doc_id}", "Claims", "")],
                False,
                doc_id,
            )

    def _build_conversations(self) -> None:
        for draft in self.drafts:
            if draft.route.startswith("crm:"):
                draft.source = SourceRef("crm", "crm", draft.route.split(":", 1)[1])

        threads: dict[int, list[_Draft]] = {}
        for draft in self.drafts:
            if draft.route == "conversation":
                threads.setdefault(self.rng.randrange(self.n_conversations), []).append(draft)

        # thread -> list of (session date, exchanges)
        plans: dict[int, list[tuple[str, list[_Exchange]]]] = {}
        for t in sorted(threads):
            ordered = sorted(threads[t], key=lambda dr: (dr.event_date, dr.id))
            groups: dict[str, list[_Draft]] = {}
            for dr in ordered:
                groups.setdefault(dr.event_date[:7], []).append(dr)
            sessions: list[tuple[str, list[_Exchange]]] = []
            for month in sorted(groups):
                members = groups[month]
                when = max(dr.event_date for dr in members)
                sessions.append((when, [self._exchange(dr) for dr in members]))
            plans[t] = sessions

        self._plant_raw_details(plans)

        for t in sorted(plans):
            conv_id = f"{self.p}cnv-{t + 1:02d}"
            built: list[ChatSession] = []
            for s_ix, (when, exchanges) in enumerate(plans[t]):
                sess_id = f"{conv_id}/s{s_ix + 1:03d}"
                turns: list[Turn] = []
                minute = 0
                for ex in exchanges:
                    for role, text, fact_id in (
                        ("user", ex.user, ex.user_fact),
                        ("assistant", ex.assistant, ex.assistant_fact),
                    ):
                        turn_id = f"{sess_id}/t{len(turns) + 1:03d}"
                        hh, mm = divmod(9 * 60 + minute, 60)
                        turns.append(
                            Turn(turn_id, role, text, f"{when}T{hh:02d}:{mm:02d}:00+00:00")
                        )
                        minute += 1
                        if fact_id:
                            self._draft(fact_id).source = SourceRef(
                                "conversation", conv_id, turn_id
                            )
                        if ex.raw_id and role == "user":
                            raw = next(r for r in self.raw_details if r["id"] == ex.raw_id)
                            raw["turn_id"] = turn_id
                built.append(ChatSession(sess_id, when, tuple(turns)))
            self.conversations.append(
                Conversation(conv_id, f"Operator thread {t + 1}", tuple(built))
            )

    def _exchange(self, draft: _Draft) -> _Exchange:
        text = draft.text()
        if draft.origin == "operator_confirmed":
            return _Exchange(
                f"Confirmed, please remember this: {text}",
                "Saved as confirmed.",
                user_fact=draft.id,
            )
        if draft.origin == "agent_generated":
            return _Exchange(
                "Can you recap what we know so far?",
                f"From what you have told me: {text}",
                assistant_fact=draft.id,
            )
        lead = self.rng.choice(("Quick update:", "FYI:", "Heads up:", "For the record:"))
        reply = self.rng.choice(("Noted.", "Thanks, got it.", "Understood."))
        return _Exchange(f"{lead} {text}", reply, user_fact=draft.id)

    def _plant_raw_details(self, plans: dict[int, list[tuple[str, list[_Exchange]]]]) -> None:
        if not self.n_raw:
            return
        slots = [(t, i) for t in sorted(plans) for i in range(len(plans[t]))]
        pets = [a + b for a in _PET_A for b in _PET_B]
        self.rng.shuffle(pets)
        kinds = ("pet", "parking", "desk", "locker")
        used_values: set[str] = set()
        pool = [p for p in self.people if not p.is_hub]
        pairs = [(p, k) for p in pool for k in kinds]
        self.rng.shuffle(pairs)
        for person, kind in pairs[: self.n_raw]:
            if kind == "pet":
                value = pets.pop()
                user = f"Unrelated, but {person.full_name}'s dog is called {value}."
                question = f"What is the name of {person.full_name}'s dog?"
            elif kind == "parking":
                value = f"bay {self.rng.choice('BCDFGHJKLMNPRSTVWXZ')}-{self.rng.randint(100, 999)}"
                user = f"By the way, {person.full_name} parks in {value} now."
                question = f"Which parking bay does {person.full_name} use?"
            elif kind == "desk":
                value = f"desk {self.rng.randint(2, 9)}.{self.rng.randint(10, 99)}"
                user = f"Oh, and {person.full_name} sits at {value} these days."
                question = f"Which desk does {person.full_name} sit at?"
            else:
                value = f"locker {self.rng.randint(1000, 9999)}"
                user = f"Also, {person.full_name} uses {value} in the basement."
                question = f"Which locker does {person.full_name} use?"
            if value in used_values:
                continue
            used_values.add(value)
            raw_id = f"{self.p}raw-{len(self.raw_details) + 1:04d}"
            t, i = self.rng.choice(slots)
            plans[t][i][1].append(_Exchange(user, "Got it.", raw_id=raw_id))
            self.raw_details.append(
                {
                    "id": raw_id,
                    "person_id": person.id,
                    "kind": kind,
                    "value": value,
                    "turn_id": "",
                    "question": question,
                }
            )

    def _freeze_facts(self) -> None:
        for draft in self.drafts:
            if draft.source is None:  # pragma: no cover - a generator bug
                raise RuntimeError(f"fact {draft.id} has no source")
            self.facts.append(
                Fact(
                    id=draft.id,
                    text=draft.text(),
                    category=draft.category,
                    mentions=tuple(Mention(s.entity_id, s.surface) for s in draft.slots),
                    event_date=draft.event_date,
                    source=draft.source,
                    origin=draft.origin,
                    supersedes=draft.supersedes,
                    superseded_by=draft.superseded_by,
                    valid_from=draft.event_date,
                    valid_to=draft.valid_to,
                )
            )

    # ---- orchestration -------------------------------------------------- #

    def build(self) -> _TenantBuilder:
        self._make_locations()
        self._make_orgs()
        self._make_people()
        self._make_employments()
        self._make_residences()
        self._make_reporting()
        self._make_org_facts()
        self._make_projects()
        self._make_events()
        self._make_personal_facts()
        self._make_crm()
        self._make_planted()
        self._resolve_duplicates()
        self._assign_origins()
        self._build_documents()
        self._build_conversations()
        self._freeze_facts()
        return self

    def world(self, seed: int, size: str, *, cases: tuple[Case, ...]) -> World:
        return World(
            seed=seed,
            size=size,
            tenant_key=self.tenant_key,
            hub_id=self.hub_id,
            people=tuple(self.people),
            organisations=tuple(
                Organisation(o["id"], o["name"], o["domain"], o["hq"], o["fact_id"], bool(o["crm"]))
                for o in self.orgs
            ),
            projects=tuple(Project(**p) for p in self.projects),
            locations=tuple(self.locations),
            employments=tuple(Employment(**e) for e in self.employments),
            residences=tuple(Residence(**r) for r in self.residences),
            reporting_lines=tuple(ReportingLine(**r) for r in self.reporting),
            memberships=tuple(ProjectMembership(**m) for m in self.memberships),
            events=tuple(Event(**e) for e in self.events),
            facts=tuple(self.facts),
            crm_people=tuple(self.crm_people),
            crm_companies=tuple(self.crm_companies),
            documents=tuple(self.documents),
            conversations=tuple(self.conversations),
            raw_details=tuple(RawDetail(**r) for r in self.raw_details),
            planted_claims=tuple(PlantedClaim(**c) for c in self.planted_claims),
            cases=cases,
        )


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #


class _CaseBuilder:
    def __init__(self, world: _TenantBuilder, shadow: _TenantBuilder, cap: int) -> None:
        self.w = world
        self.s = shadow
        self.cap = cap
        poisoned = {c["subject_id"] for c in world.planted_claims}
        self.poisoned = poisoned
        self.people = [p for p in world.people if not p.is_hub and p.id not in poisoned]
        self.by_stratum: dict[str, list[Case]] = {s: [] for s in STRATA}

    def _case(
        self,
        stratum: str,
        key: str,
        question: str,
        gold: tuple[str, ...],
        support: tuple[str, ...],
        subject: str,
        **kw: Any,
    ) -> None:
        cid = f"{stratum}:{key}"
        self.by_stratum[stratum].append(
            Case(
                id=cid,
                stratum=stratum,
                question=question,
                gold=gold,
                supporting_ids=support,
                as_of=kw.pop("as_of", None),
                split=split_for(cid),
                subject_id=subject,
                **kw,
            )
        )

    def _person_gold(self, pid: str) -> tuple[str, ...]:
        p = self.w._person(pid)
        return (p.full_name, p.email)

    def _org_name(self, org_id: str) -> str:
        return str(self.w._org(org_id)["name"])

    def _jobs(self, pid: str) -> list[dict[str, Any]]:
        return [e for e in self.w.employments if e["person_id"] == pid]

    def build(self) -> list[Case]:
        self._multi_hop()
        self._entity_confusion()
        self._history()
        self._abstention()
        self._raw_detail()
        self._cross_tenant()
        out: list[Case] = []
        for stratum in STRATA:
            cases = self.by_stratum[stratum]
            if len(cases) > self.cap:
                keep = sorted(
                    cases, key=lambda c: hashlib.sha256(_pair_id(c.id).encode()).hexdigest()
                )
                cases = sorted(keep[: self.cap], key=lambda c: c.id)
            out.extend(sorted(cases, key=lambda c: c.id))
        return out

    def _multi_hop_case(
        self,
        key: str,
        question: str,
        name: str,
        gold: tuple[str, ...],
        support: tuple[str, ...],
        subject: str,
    ) -> None:
        """A multi-hop case and its reworded twin (same key, gold, support)."""
        self._case("multi_hop", key, question, gold, support, subject)
        variants = PARAPHRASES[key.rsplit(":", 1)[1]]
        pick = hashlib.sha256(key.encode("utf-8")).digest()[0] % len(variants)
        self._case(
            "multi_hop_paraphrase", key, variants[pick].format(name=name), gold, support, subject
        )

    def _multi_hop(self) -> None:
        w = self.w
        poisoned_orgs = self.poisoned
        for p in self.people:
            line = w.current_manager(p.id)
            if line:
                upper = w.current_manager(line["manager_id"])
                if upper:
                    self._multi_hop_case(
                        f"{p.id}:manager-of-manager",
                        f"Who does {p.full_name}'s manager report to?",
                        p.full_name,
                        self._person_gold(upper["manager_id"]),
                        (line["fact_id"], upper["fact_id"]),
                        p.id,
                    )
            job = w.current_job(p.id)
            if job["org_id"] not in poisoned_orgs:
                org = w._org(job["org_id"])
                self._multi_hop_case(
                    f"{p.id}:employer-city",
                    f"In which city is the organisation {p.full_name} currently works for "
                    "headquartered?",
                    p.full_name,
                    (w._loc(org["hq"]).name,),
                    (job["fact_id"], org["fact_id"]),
                    p.id,
                )
        for prj in w.projects:
            lead = prj["lead_id"]
            if not lead or lead in self.poisoned:
                continue
            job = w.current_job(lead)
            if job["org_id"] in poisoned_orgs:
                continue
            org = w._org(job["org_id"])
            self._multi_hop_case(
                f"{prj['id']}:lead-employer-city",
                f"Where is the employer of the {prj['name']} project's lead headquartered?",
                str(prj["name"]),
                (w._loc(org["hq"]).name,),
                (prj["fact_id"], job["fact_id"], org["fact_id"]),
                prj["id"],
            )

    def _entity_confusion(self) -> None:
        w = self.w
        by_first: dict[str, list[Person]] = {}
        for p in w.people:
            by_first.setdefault(p.first_name, []).append(p)
        for p in self.people:
            others = [q for q in by_first[p.first_name] if q.id != p.id]
            if not others:
                continue
            job = w.current_job(p.id)
            role_confuser = next(
                (q for q in others if w.current_job(q.id)["role"] != job["role"]), None
            )
            if role_confuser:
                self._case(
                    "entity_confusion",
                    f"{p.id}:role",
                    f"What is {p.full_name}'s current job title?",
                    (job["role"],),
                    (job["fact_id"],),
                    p.id,
                    confuser_id=role_confuser.id,
                )
                self._case(
                    "entity_confusion",
                    f"{p.id}:role-by-email",
                    f"What job does the person with the email address {p.email} do now?",
                    (job["role"],),
                    (job["fact_id"],),
                    p.id,
                    confuser_id=role_confuser.id,
                )
            line = w.current_manager(p.id)
            if line:
                mgr_confuser = next(
                    (
                        q
                        for q in others
                        if (w.current_manager(q.id) or {}).get("manager_id") != line["manager_id"]
                    ),
                    None,
                )
                if mgr_confuser:
                    self._case(
                        "entity_confusion",
                        f"{p.id}:manager",
                        f"Who is {p.full_name}'s manager at the moment?",
                        self._person_gold(line["manager_id"]),
                        (line["fact_id"],),
                        p.id,
                        confuser_id=mgr_confuser.id,
                    )

    def _history(self) -> None:
        w = self.w
        for p in self.people:
            jobs = self._jobs(p.id)
            if len(jobs) >= 2:
                current = jobs[-1]
                self._case(
                    "knowledge_update",
                    f"{p.id}:employer",
                    f"Which organisation does {p.full_name} work for now?",
                    (self._org_name(current["org_id"]),),
                    (current["fact_id"],),
                    p.id,
                )
                for k, stint in enumerate(jobs[:-1]):
                    start = date.fromisoformat(stint["start"])
                    end = date.fromisoformat(stint["end"])
                    as_of = _d(start + (end - start) // 2)
                    self._case(
                        "point_in_time",
                        f"{p.id}:job-{k + 1}",
                        f"Which organisation did {p.full_name} work for on {_long_date(as_of)}?",
                        (self._org_name(stint["org_id"]),),
                        (stint["fact_id"],),
                        p.id,
                        as_of=as_of,
                    )
            homes = [r for r in w.residences if r["person_id"] == p.id]
            current_home = homes[-1]
            self._case(
                "long_tenure",
                f"{p.id}:residence",
                f"Where does {p.full_name} live now?",
                (w._loc(current_home["location_id"]).name,),
                (current_home["fact_id"],),
                p.id,
                history_length=len(homes),
            )

    def _abstention(self) -> None:
        for p in self.people:
            for probe, template in ABSTENTION_PROBES:
                self._case(
                    "abstention",
                    f"{p.id}:{probe.replace(' ', '-')}",
                    template.format(name=p.full_name),
                    ("unknown",),
                    (),
                    p.id,
                    probe=probe,
                )

    def _raw_detail(self) -> None:
        for raw in self.w.raw_details:
            self._case(
                "raw_detail",
                raw["id"],
                raw["question"],
                (raw["value"],),
                (raw["turn_id"],),
                raw["person_id"],
            )

    def _cross_tenant(self) -> None:
        w, s = self.w, self.s
        primary_by_name = {p.full_name: p for p in w.people}
        primary_names = set(primary_by_name)
        for twin in s.people:
            p = primary_by_name.get(twin.full_name)
            if p is None or p.is_hub or p.id in self.poisoned:
                continue
            job, twin_job = w.current_job(p.id), s.current_job(twin.id)
            self._case(
                "cross_tenant",
                f"{p.id}:employer",
                f"Where is {p.full_name} employed?",
                (self._org_name(job["org_id"]),),
                (job["fact_id"],),
                p.id,
                forbidden=(str(s._org(twin_job["org_id"])["name"]),),
            )
            home, twin_home = w.current_home(p.id), s.current_home(twin.id)
            self._case(
                "cross_tenant",
                f"{p.id}:city",
                f"Which city is {p.full_name} based in?",
                (w._loc(home["location_id"]).name,),
                (home["fact_id"],),
                p.id,
                forbidden=(s._loc(twin_home["location_id"]).name,),
            )
            line, twin_line = w.current_manager(p.id), s.current_manager(twin.id)
            if line and twin_line:
                twin_boss = s._person(twin_line["manager_id"])
                # The surface the shadow tenant actually stored for the boss.
                surface = s._draft(twin_line["fact_id"]).slots[1].surface
                if twin_boss.full_name not in primary_names:
                    self._case(
                        "cross_tenant",
                        f"{p.id}:manager",
                        f"Who manages {p.full_name}?",
                        self._person_gold(line["manager_id"]),
                        (line["fact_id"],),
                        p.id,
                        forbidden=(surface,),
                    )


# --------------------------------------------------------------------------- #
# conversation_recall: answers said only in long, multi-session conversations
# --------------------------------------------------------------------------- #

#: Shops whose fit-out the operator talks through with the assistant. No word
#: here is a world name; none is a word another stratum's questions use.
_ACCOUNT_A = (
    "Copper", "Meadow", "Granite", "Juniper", "Saffron",
    "Harbour", "Bramble", "Thistle", "Marigold", "Pebble",
)  # fmt: skip
_ACCOUNT_B = ("Bakery", "Dental", "Garage", "Florists", "Printers", "Cycles", "Cafe", "Bookshop")
_SUPPLIER_A = (
    "Abbot", "Cobble", "Dimple", "Fennel", "Gusset", "Hobble",
    "Inkle", "Jumble", "Lintel", "Mantle", "Nettle", "Pippin",
)  # fmt: skip
_SUPPLIER_B = (
    "Fixtures", "Joinery", "Shopfitters", "Racking", "Interiors",
    "Carpentry", "Fittings", "Storage", "Benchworks", "Cabinets",
)  # fmt: skip

#: Sentences of a pasted site note. They name no shop and share no word with
#: any question of the stratum, so a far answer is far from every question word.
_NOTE_FILLER = (
    "The joiners want the back wall cleared by Wednesday evening so they can start first thing the next morning.",
    "The electrician is coming back to move two of the sockets behind the counter, which should take about an hour.",
    "We agreed to keep the old tiled floor in the front area and only sand and seal the boards at the back.",
    "The window vinyls are printed and just need a dry day before anyone tries to put them up.",
    "Someone needs to check whether the extractor fan in the back room is loud enough to bother the neighbours upstairs.",
    "The painters prefer to do the ceiling before anything else goes in, so the counter delivery may have to wait.",
    "The till system arrives in two boxes and the installer says the network cable has to be in place first.",
    "We still owe the plumber for the second visit, and he has asked for a bank transfer rather than a cheque.",
    "The fire extinguishers have to be serviced and tagged before the inspection later in the week.",
    "There is a damp patch near the skirting that the landlord has promised to look at before the opening weekend.",
    "The new lights are much warmer than the old ones, which everyone prefers, though two fittings arrived cracked.",
    "Bins go out on Tuesday nights, and the council has asked us not to leave cardboard flat on the pavement.",
    "The sign writer needs the final wording by the end of the week or the slot moves to the following one.",
    "Wi-fi in the back is patchy, so a second access point near the stockroom would help the card reader.",
    "The cleaning crew will come in after the builders finish and do a deep clean of every surface.",
    "A neighbour asked whether the scaffolding can come down before their own builders arrive on Monday.",
    "The insurance renewal needs photos of the finished interior, so nobody should rush the last coat of paint.",
    "The van hire is booked for a whole day, which gives enough time to take the old fittings to the tip.",
    "The fridge and microwave for the staff room can come from the old premises rather than being bought new.",
    "The opening hours on the website still show the old times and need changing before the launch.",
    "Two of the stools wobble badly, and the maker has offered to swap them at no extra cost.",
    "The alarm installer wants a list of key holders with phone numbers before he finishes the setup.",
    "We are still waiting on the landlord's signature for the new lease clause about the back room.",
    "A local photographer has offered to take pictures on the first morning in exchange for a mention online.",
)  # fmt: skip
_SHORT_ASKS = (
    "Quick check on {acct}: is the snagging list up to date?",
    "Can you remind me what is still open for {acct}?",
    "Anything new from the builders at {acct} today?",
    "Can you draft a short note to the landlord about the {acct} works?",
    "What did we agree about the {acct} opening weekend?",
    "Is the {acct} insurance renewal sorted yet?",
)


def _conversation_recall(
    rng: random.Random, n: int
) -> tuple[tuple[Conversation, ...], tuple[ConversationRecall, ...]]:
    """``n`` conversations of four sessions each, and their four questions.

    Each session is one long pasted note (well over 1,200 characters) and
    three short exchanges. Session 1 states the dock door and one shelving
    price; session 2 an order reference (beside the question's words) and a
    second price; session 3 a gate code more than 1,200 characters after the
    words that ask for it; session 4 the changed dock door and the third
    price. Nothing here becomes a fact: the answers exist only as conversation.
    """
    accounts = [f"{a} {b}" for a in _ACCOUNT_A for b in _ACCOUNT_B]
    suppliers = [f"{a} {b}" for a in _SUPPLIER_A for b in _SUPPLIER_B]
    rng.shuffle(accounts)
    rng.shuffle(suppliers)
    codes = rng.sample(range(1000, 10000), n)
    refs = rng.sample(range(10000, 100000), n)
    convs: list[Conversation] = []
    recall: list[ConversationRecall] = []
    for k in range(n):
        acct, cid = accounts[k], f"cvr-{k + 1:02d}"
        old_door, new_door = rng.randint(1, 19), rng.randint(20, 39)
        trio = tuple(suppliers[3 * k : 3 * k + 3])
        prices = rng.sample(range(320, 480), 3)
        code = f"{codes[k]}-{rng.choice('BCDFGHJKLMNPRSTVWXZ')}"
        ref = f"PO-{refs[k]}"
        start = date(2025, 1, 6) + timedelta(days=rng.randrange(240))
        dates = [start]
        for _ in range(3):
            dates.append(dates[-1] + timedelta(days=rng.randint(9, 30)))

        def note(planted: list[str], acct: str = acct) -> str:
            body = rng.sample(_NOTE_FILLER, 11)
            for sentence in planted:
                body.insert(rng.randint(3, len(body)), sentence)
            return " ".join([f"My notes from today's visit to {acct}:", *body])

        price = [f"{p * 10:,}" for p in prices]
        notes = [
            note(
                [
                    f"For now, deliveries for {acct} should come to dock door {old_door}, "
                    "because the other entrances belong to the restaurant.",
                    f"{trio[0]} sent their shelving price for {acct}: {price[0]} pounds, fitted.",
                ]
            ),
            note(
                [
                    # The value sits between the words that ask for it.
                    f"The order reference {ref} belongs to the {acct} spring catalogue; "
                    "put it on every invoice that goes out.",
                    f"{trio[1]} came back with a shelving price for {acct} of {price[1]} pounds, "
                    "fitted.",
                ]
            ),
            " ".join(
                [
                    f"Notes from this morning's walk-round of the {acct} loading yard, mostly "
                    "about the gate code that keeps failing:",
                    *rng.sample(_NOTE_FILLER, 13),
                    f"In the end the site supervisor texted it over: {code}, and it changes on "
                    "the first of every month.",
                ]
            ),
            note(
                [
                    f"Change of plan on deliveries for {acct}: from this week they go to dock "
                    f"door {new_door}, not dock door {old_door}, since the old entrance is being "
                    "bricked up.",
                    f"The last shelving price for {acct} is in from {trio[2]}: {price[2]} pounds, "
                    "fitted.",
                ]
            ),
        ]
        sessions: list[ChatSession] = []
        note_turns: list[str] = []
        for s_ix, (when, text) in enumerate(zip(dates, notes, strict=True)):
            sid = f"{cid}/s{s_ix + 1:03d}"
            pairs = [
                (
                    rng.choice(_SHORT_ASKS).format(acct=acct),
                    "From your notes: " + " ".join(rng.sample(_NOTE_FILLER, 2)),
                )
                for _ in range(3)
            ]
            # The gate code note ends its session and its reply names no shop,
            # so no question word follows the far answer.
            far = s_ix == 2
            at = 3 if far else rng.randint(0, 3)
            ack = "Thanks, I have filed those notes." if far else f"Thanks, filed under {acct}."
            pairs.insert(at, (text, ack))
            turns: list[Turn] = []
            for user, assistant in pairs:
                for role, said in (("user", user), ("assistant", assistant)):
                    minute = len(turns)
                    stamp = f"{when.isoformat()}T10:{minute:02d}:00+00:00"
                    turns.append(Turn(f"{sid}/t{minute + 1:03d}", role, said, stamp))
            note_turns.append(turns[2 * at].id)
            sessions.append(ChatSession(sid, when.isoformat(), tuple(turns)))
        convs.append(Conversation(cid, f"{acct} fit-out", tuple(sessions)))
        lowest = min(range(3), key=lambda i: prices[i])
        quote_turns = (note_turns[0], note_turns[1], note_turns[3])
        recall += [
            ConversationRecall(
                f"{cid}:near",
                cid,
                "near",
                f"What is the order reference for the {acct} spring catalogue?",
                (ref,),
                (),
                (note_turns[1],),
            ),
            ConversationRecall(
                f"{cid}:far",
                cid,
                "far",
                f"What is the gate code for the {acct} loading yard?",
                (code,),
                (),
                (note_turns[2],),
            ),
            ConversationRecall(
                f"{cid}:update",
                cid,
                "update",
                f"Which dock door do deliveries for {acct} go to now?",
                (f"dock door {new_door}", f"door {new_door}"),
                (f"dock door {old_door}",),
                (note_turns[3],),
            ),
            ConversationRecall(
                f"{cid}:multi_session",
                cid,
                "multi_session",
                f"Of the three shelving prices for {acct}, which supplier was the lowest?",
                (trio[lowest],),
                tuple(trio[i] for i in range(3) if i != lowest),
                quote_turns,
            ),
        ]
    return tuple(convs), tuple(recall)


def _recall_cases(
    recall: tuple[ConversationRecall, ...],
    cap: int,
    stratum: str = "conversation_recall",
    subjects: dict[str, str] | None = None,
) -> list[Case]:
    """A conversation stratum's cases, capped as every stratum is.

    ``subjects`` maps a conversation to the entity its cases are about (the
    company of a ``conversation_buried`` conversation); else the conversation.
    """
    cases = [
        Case(
            id=f"{stratum}:{r.id}",
            stratum=stratum,
            question=r.question,
            gold=r.gold,
            supporting_ids=r.turn_ids,
            as_of=None,
            split=split_for(f"{stratum}:{r.id}"),
            subject_id=(subjects or {}).get(r.conversation_id, r.conversation_id),
        )
        for r in recall
    ]
    if len(cases) > cap:
        cases = sorted(cases, key=lambda c: hashlib.sha256(c.id.encode()).hexdigest())[:cap]
    return sorted(cases, key=lambda c: c.id)


# --------------------------------------------------------------------------- #
# conversation_buried: a conversation answer under a company's pile of facts
# --------------------------------------------------------------------------- #

#: Companies of the stratum. No word here is a world name or a word any other
#: world text uses, so the pile is the only thing that names them.
_BURIED_A = (
    "Larch", "Sorrel", "Quince", "Yarrow", "Bracken",
    "Fescue", "Wicken", "Teasel", "Burdock", "Hornbeam",
)  # fmt: skip
_BURIED_B = ("Ceramics", "Upholstery", "Glazing", "Tiling", "Plastering")

#: What the pile says. Every fact names the company, so a question about it
#: finds the pile on the dense and the keyword fact leg alike; none uses a
#: word the stratum's questions ask with besides the company's name.
_BURIED_ITEMS = (
    "sheets of plywood", "tubs of filler", "rolls of masking tape", "boxes of screws",
    "tins of varnish", "packs of sandpaper", "cartons of gloves", "drums of sealant",
    "coils of cable", "bags of grout", "pots of primer", "reels of twine",
)  # fmt: skip
_BURIED_SERVICES = (
    "boiler", "window cleaning", "pest control", "fire alarm", "lift",
    "gutter", "drain", "pressure washing", "sprinkler", "roof",
)  # fmt: skip
_BURIED_PEOPLE = ("bookkeeper", "surveyor", "accountant", "solicitor", "architect", "auditor")
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
#: Facts per company, its headquarters and the old dock door included.
_BURIED_FACTS_PER_SUBJECT = 100


def _buried_org_id(conversation_id: str) -> str:
    return f"org-{conversation_id}"


def _words(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def _buried_pile(rng: random.Random, org: str, n: int) -> list[tuple[str, date]]:
    """``n`` facts naming ``org``, each with the date it became true.

    No two are near-duplicates by recall's own rule (word sets 75% the same,
    or one inside the other), so the pile is not collapsed to a few rows.
    """
    out: list[tuple[str, date]] = []
    kept: list[frozenset[str]] = []
    while len(out) < n:
        when = date(2023, 1, 2) + timedelta(days=rng.randrange(700))
        month = f"{_MONTHS[when.month - 1]} {when.year}"
        shape = rng.randrange(5)
        if shape == 0:
            text = f"{org} took delivery of {rng.randint(2, 90)} {rng.choice(_BURIED_ITEMS)} in {month}."
        elif shape == 1:
            text = (
                f"{org} paid invoice INV-{rng.randint(10000, 99999)} for "
                f"{rng.choice(_BURIED_ITEMS)} in {month}."
            )
        elif shape == 2:
            text = f"{org} booked a {rng.choice(_BURIED_SERVICES)} inspection for {month}."
        elif shape == 3:
            text = (
                f"{org} sent back {rng.randint(2, 40)} damaged {rng.choice(_BURIED_ITEMS)} "
                f"in {month}."
            )
        else:
            text = f"{org} met its {rng.choice(_BURIED_PEOPLE)} on {when.day} {month}."
        words = _words(text)
        if any(len(words & k) / len(words | k) >= 0.7 or words <= k or k <= words for k in kept):
            continue
        kept.append(words)
        out.append((text, when))
    return out


def _conversation_buried(
    rng: random.Random, n: int, locations: tuple[Location, ...]
) -> tuple[
    tuple[Organisation, ...],
    tuple[Fact, ...],
    tuple[Conversation, ...],
    tuple[ConversationRecall, ...],
]:
    """``n`` companies, their piles of facts, one conversation each, three questions each.

    The conversation has three sessions after every fact in the pile: an order
    reference beside the question's words, a gate code more than 1,200
    characters after the words that ask for it, and a dock door that replaced
    the one an active fact still states. The answers exist only there.
    """
    names = [f"{a} {b}" for a in _BURIED_A for b in _BURIED_B]
    rng.shuffle(names)
    codes = rng.sample(range(1000, 10000), n)
    refs = rng.sample(range(10000, 100000), n)
    orgs: list[Organisation] = []
    facts: list[Fact] = []
    convs: list[Conversation] = []
    buried: list[ConversationRecall] = []
    for k in range(n):
        org, cid = names[k], f"cvb-{k + 1:02d}"
        oid = _buried_org_id(cid)
        loc = rng.choice(locations)
        # Doors, codes and references of their own shape, so no value here is
        # one conversation_recall asks for.
        old_door, new_door = rng.randint(40, 59), rng.randint(60, 79)
        code = "".join(rng.choices("BCDFGHJKLMNPRSTVWXZ", k=2)) + f"-{codes[k]}"
        ref = f"SO-{refs[k]}"
        first = f"{cid}/s001/t001"
        pile = [
            (f"{org} is headquartered in {loc.name}.", date(2023, 1, 2), "contact", (loc,)),
            (
                f"Deliveries for {org} go to dock door {old_door}.",
                date(2024, 6, 3) + timedelta(days=rng.randrange(60)),
                "contact",
                (),
            ),
            *(
                (text, when, "event", ())
                for text, when in _buried_pile(rng, org, _BURIED_FACTS_PER_SUBJECT - 2)
            ),
        ]
        hq_fact = f"cvb-fct-{k + 1:02d}-001"
        for ix, (text, when, category, others) in enumerate(pile, start=1):
            mentions = (Mention(oid, org), *(Mention(o.id, o.name) for o in others))
            day = when.isoformat()
            facts.append(
                Fact(
                    id=f"cvb-fct-{k + 1:02d}-{ix:03d}",
                    text=text,
                    category=category,
                    mentions=mentions,
                    event_date=day,
                    source=SourceRef("conversation", cid, first),
                    origin="user_stated",
                    supersedes=None,
                    superseded_by=None,
                    valid_from=day,
                    valid_to=None,
                )
            )
        orgs.append(
            Organisation(
                id=oid,
                name=org,
                domain=f"{org.lower().replace(' ', '-')}.example.com",
                hq_location_id=loc.id,
                hq_fact_id=hq_fact,
                has_crm_row=False,
            )
        )
        start = max(when for _t, when, _c, _o in pile) + timedelta(days=rng.randint(10, 40))
        dates = [start, start + timedelta(days=rng.randint(9, 30))]
        dates.append(dates[-1] + timedelta(days=rng.randint(9, 30)))

        def note(planted: str, org: str = org) -> str:
            body = rng.sample(_NOTE_FILLER, 11)
            body.insert(rng.randint(3, len(body)), planted)
            return " ".join([f"My notes from today's visit to {org}:", *body])

        notes = [
            # The value sits between the words that ask for it.
            note(
                f"The order reference {ref} belongs to the {org} spring catalogue; "
                "put it on every invoice that goes out."
            ),
            " ".join(
                [
                    f"Notes from this morning's walk-round of the {org} loading yard, mostly "
                    "about the gate code that keeps failing:",
                    *rng.sample(_NOTE_FILLER, 13),
                    f"In the end the site supervisor texted it over: {code}, and it changes on "
                    "the first of every month.",
                ]
            ),
            note(
                f"Change of plan on deliveries for {org}: from this week they go to dock "
                f"door {new_door}, not dock door {old_door}, since the old entrance is being "
                "bricked up."
            ),
        ]
        sessions: list[ChatSession] = []
        note_turns: list[str] = []
        for s_ix, (when, text) in enumerate(zip(dates, notes, strict=True)):
            sid = f"{cid}/s{s_ix + 1:03d}"
            pairs = [
                (
                    rng.choice(_SHORT_ASKS).format(acct=org),
                    "From your notes: " + " ".join(rng.sample(_NOTE_FILLER, 2)),
                )
                for _ in range(3)
            ]
            # The gate code note ends its session and its reply names no
            # company, so no question word follows the far answer.
            far = s_ix == 1
            at = 3 if far else rng.randint(0, 3)
            ack = "Thanks, I have filed those notes." if far else f"Thanks, filed under {org}."
            pairs.insert(at, (text, ack))
            turns: list[Turn] = []
            for user, assistant in pairs:
                for role, said in (("user", user), ("assistant", assistant)):
                    minute = len(turns)
                    stamp = f"{when.isoformat()}T10:{minute:02d}:00+00:00"
                    turns.append(Turn(f"{sid}/t{minute + 1:03d}", role, said, stamp))
            note_turns.append(turns[2 * at].id)
            sessions.append(ChatSession(sid, when.isoformat(), tuple(turns)))
        convs.append(Conversation(cid, f"{org} premises", tuple(sessions)))
        buried += [
            ConversationRecall(
                f"{cid}:near",
                cid,
                "near",
                f"What is the order reference for the {org} spring catalogue?",
                (ref,),
                (),
                (note_turns[0],),
            ),
            ConversationRecall(
                f"{cid}:far",
                cid,
                "far",
                f"What is the gate code for the {org} loading yard?",
                (code,),
                (),
                (note_turns[1],),
            ),
            ConversationRecall(
                f"{cid}:update",
                cid,
                "update",
                f"Which dock door do deliveries for {org} go to now?",
                (f"dock door {new_door}", f"door {new_door}"),
                (f"dock door {old_door}",),
                (note_turns[2],),
            ),
        ]
    return tuple(orgs), tuple(facts), tuple(convs), tuple(buried)


__all__ = [
    "ABSTENTION_PROBES",
    "FIRST_NAMES",
    "HUB_MIN_SHARE",
    "LAST_NAMES",
    "ORIGINS",
    "SPLITS",
    "PARAPHRASES",
    "STRATA",
    "WORLD_SCHEMA_VERSION",
    "WORLD_SIZES",
    "Case",
    "ChatSession",
    "Conversation",
    "CrmCompany",
    "CrmPerson",
    "Document",
    "DocumentSection",
    "Employment",
    "Event",
    "Fact",
    "Location",
    "Mention",
    "Organisation",
    "Person",
    "PlantedClaim",
    "Project",
    "ProjectMembership",
    "RawDetail",
    "ReportingLine",
    "Residence",
    "SourceRef",
    "Turn",
    "World",
    "WorldSize",
    "generate_world",
    "split_for",
    "world_cases",
    "world_to_dict",
    "world_to_json",
]
