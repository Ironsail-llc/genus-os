"""Identity enrichment cached for a chat session's lifetime.

``enrich_identity`` caches for 60 s, so a chat that pauses for a minute pays
the CRM and memory-graph lookups again on its next turn. With
``ROBOTHOR_WARMUP_PARALLEL`` on, the warmup and the follow-up turn use
``enrich_identity_for_session``: one lookup per (agent, identity) session,
recomputed when the identity itself changes.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.identity import enrichment
from robothor.identity.context import EnrichedIdentity, IdentityContext


@pytest.fixture(autouse=True)
def _clear() -> None:
    enrichment.clear_cache()
    yield
    enrichment.clear_cache()


def _ctx(**over: object) -> IdentityContext:
    base: dict[str, object] = {
        "tenant_id": "test-tenant",
        "channel": "webchat",
        "identifier": "user-1",
        "verified": True,
        "display_name": "Alice",
        "role": "owner",
        "person_id": "person-1",
    }
    base.update(over)
    return IdentityContext(**base)  # type: ignore[arg-type]


def _counting_build():
    calls: list[str] = []

    def _build(person_id: str, tenant_id: str) -> EnrichedIdentity:
        calls.append(person_id)
        return EnrichedIdentity(company="Example Co", job_title="Engineer")

    return calls, patch("robothor.identity.enrichment._build_enrichment", side_effect=_build)


def test_same_session_same_identity_looks_up_once_even_past_the_ttl() -> None:
    calls, patcher = _counting_build()
    clock = [1000.0]
    with patcher, patch("robothor.identity.enrichment.time.monotonic", lambda: clock[0]):
        first = enrichment.enrich_identity_for_session("main", _ctx())
        clock[0] += enrichment._CACHE_TTL_SECONDS * 10
        second = enrichment.enrich_identity_for_session("main", _ctx())
    assert first == second == EnrichedIdentity(company="Example Co", job_title="Engineer")
    assert calls == ["person-1"]


def test_identity_change_recomputes() -> None:
    calls, patcher = _counting_build()
    with patcher:
        enrichment.enrich_identity_for_session("main", _ctx())
        enrichment.enrich_identity_for_session("main", _ctx(person_id="person-2"))
    assert calls == ["person-1", "person-2"]


def test_role_change_on_the_same_person_is_an_identity_change() -> None:
    calls, patcher = _counting_build()
    clock = [1000.0]
    with patcher, patch("robothor.identity.enrichment.time.monotonic", lambda: clock[0]):
        enrichment.enrich_identity_for_session("main", _ctx())
        clock[0] += enrichment._CACHE_TTL_SECONDS + 1
        enrichment.enrich_identity_for_session("main", _ctx(role="member"))
    assert calls == ["person-1", "person-1"]


def test_sessions_are_per_agent() -> None:
    calls, patcher = _counting_build()
    clock = [1000.0]
    with patcher, patch("robothor.identity.enrichment.time.monotonic", lambda: clock[0]):
        enrichment.enrich_identity_for_session("main", _ctx())
        clock[0] += enrichment._CACHE_TTL_SECONDS + 1
        enrichment.enrich_identity_for_session("example-agent", _ctx())
    assert calls == ["person-1", "person-1"]


def test_failed_enrichment_is_not_pinned_for_the_session() -> None:
    clock = [1000.0]
    with (
        patch(
            "robothor.identity.enrichment._build_enrichment",
            side_effect=[RuntimeError("db down"), EnrichedIdentity(company="Example Co")],
        ),
        patch("robothor.identity.enrichment.time.monotonic", lambda: clock[0]),
    ):
        assert enrichment.enrich_identity_for_session("main", _ctx()) is None
        clock[0] += enrichment._CACHE_TTL_SECONDS + 1
        assert enrichment.enrich_identity_for_session("main", _ctx()) == EnrichedIdentity(
            company="Example Co"
        )


def test_clear_cache_drops_sessions() -> None:
    calls, patcher = _counting_build()
    with patcher:
        enrichment.enrich_identity_for_session("main", _ctx())
        enrichment.clear_cache()
        enrichment.enrich_identity_for_session("main", _ctx())
    assert calls == ["person-1", "person-1"]


def test_held_entry_expires_so_the_history_line_moves() -> None:
    """``History: N prior interactions, last <ts>`` comes from the enrichment;
    a held entry with no lifetime froze it until the engine restarted."""
    calls, patcher = _counting_build()
    clock = [1000.0]
    with patcher, patch("robothor.identity.enrichment.time.monotonic", lambda: clock[0]):
        enrichment.enrich_identity_for_session("main", _ctx())
        clock[0] += enrichment._SESSION_TTL_SECONDS - 1
        enrichment.enrich_identity_for_session("main", _ctx())
        assert calls == ["person-1"]
        clock[0] += 2
        enrichment.enrich_identity_for_session("main", _ctx())
    assert calls == ["person-1", "person-1"]
    assert 60 < enrichment._SESSION_TTL_SECONDS <= 3600
