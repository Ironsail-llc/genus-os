"""Tests for robothor.identity.enrichment: enrich_identity.

DB access is mocked at the module's own seam (get_connection, get_person_summary)
per platform convention — never a deeper layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from robothor.identity import enrichment
from robothor.identity.context import IdentityContext

TENANT = "t-alpha"


@pytest.fixture(autouse=True)
def _clear_enrichment_cache():
    enrichment.clear_cache()
    yield
    enrichment.clear_cache()


def _ctx(person_id="person-1"):
    return IdentityContext(
        tenant_id=TENANT,
        channel="webchat",
        identifier="acct-1",
        verified=True,
        person_id=person_id,
    )


def _mock_conn(fetchone_seq=(), fetchall_seq=()):
    """A get_connection() context manager backed by one cursor whose
    fetchone()/fetchall() are driven by the given call sequences."""
    cur = MagicMock()
    if fetchone_seq:
        cur.fetchone.side_effect = list(fetchone_seq)
    if fetchall_seq:
        cur.fetchall.side_effect = list(fetchall_seq)
    else:
        cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cur


def test_enrich_identity_returns_none_when_no_person_id():
    ctx = _ctx(person_id=None)
    assert enrichment.enrich_identity(ctx) is None


def test_enrich_identity_builds_full_profile():
    person_row = {"job_title": "Engineer", "company_name": "Acme Corp"}
    entity_row = {"memory_entity_id": 42}
    relation_rows = [
        {"relation_type": "colleague_of", "other_name": "Alice"},
        {"relation_type": "manages", "other_name": "Carol"},
    ]
    conn, cur = _mock_conn(fetchone_seq=[person_row, entity_row], fetchall_seq=[relation_rows])
    last_touched = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    summary = {"counts": {"email": 3, "call": 2}, "last_touched_at": last_touched}

    with (
        patch("robothor.identity.enrichment.get_connection", return_value=conn),
        patch("robothor.identity.enrichment.get_person_summary", return_value=summary),
    ):
        enriched = enrichment.enrich_identity(_ctx())

    assert enriched is not None
    assert enriched.job_title == "Engineer"
    assert enriched.company == "Acme Corp"
    assert enriched.relationships == ("colleague_of → Alice", "manages → Carol")
    assert enriched.last_touched_at == last_touched.isoformat()
    assert enriched.activity_counts == {"email": 3, "call": 2}


def test_enrich_identity_no_person_row_leaves_company_and_title_none():
    conn, cur = _mock_conn(fetchone_seq=[None, None])
    summary = {"counts": {}, "last_touched_at": None}
    with (
        patch("robothor.identity.enrichment.get_connection", return_value=conn),
        patch("robothor.identity.enrichment.get_person_summary", return_value=summary),
    ):
        enriched = enrichment.enrich_identity(_ctx())

    assert enriched is not None
    assert enriched.job_title is None
    assert enriched.company is None
    assert enriched.relationships == ()
    assert enriched.last_touched_at is None
    assert enriched.activity_counts == {}


def test_enrich_identity_no_contact_identifier_row_skips_relations_query():
    person_row = {"job_title": "Engineer", "company_name": None}
    conn, cur = _mock_conn(fetchone_seq=[person_row, None])
    summary = {"counts": {}, "last_touched_at": None}
    with (
        patch("robothor.identity.enrichment.get_connection", return_value=conn),
        patch("robothor.identity.enrichment.get_person_summary", return_value=summary),
    ):
        enriched = enrichment.enrich_identity(_ctx())

    assert enriched.relationships == ()
    cur.fetchall.assert_not_called()


def test_enrich_identity_db_error_returns_none_never_raises():
    with patch("robothor.identity.enrichment.get_connection", side_effect=RuntimeError("db down")):
        assert enrichment.enrich_identity(_ctx()) is None


def test_enrich_identity_caches_result():
    person_row = {"job_title": "Engineer", "company_name": "Acme"}
    conn, cur = _mock_conn(fetchone_seq=[person_row, None, person_row, None])
    summary = {"counts": {}, "last_touched_at": None}
    with (
        patch("robothor.identity.enrichment.get_connection", return_value=conn) as mock_get_conn,
        patch("robothor.identity.enrichment.get_person_summary", return_value=summary),
    ):
        enrichment.enrich_identity(_ctx())
        enrichment.enrich_identity(_ctx())

    assert mock_get_conn.call_count == 1


def test_enrich_identity_cache_expires_after_ttl(monkeypatch):
    person_row = {"job_title": "Engineer", "company_name": "Acme"}
    conn, cur = _mock_conn(fetchone_seq=[person_row, None, person_row, None])
    summary = {"counts": {}, "last_touched_at": None}
    fake_now = [2000.0]
    monkeypatch.setattr(enrichment.time, "monotonic", lambda: fake_now[0])
    with (
        patch("robothor.identity.enrichment.get_connection", return_value=conn) as mock_get_conn,
        patch("robothor.identity.enrichment.get_person_summary", return_value=summary),
    ):
        enrichment.enrich_identity(_ctx())
        fake_now[0] += 61
        enrichment.enrich_identity(_ctx())

    assert mock_get_conn.call_count == 2


def test_enrich_identity_cache_keyed_by_person_and_tenant():
    person_row = {"job_title": "Engineer", "company_name": "Acme"}
    conn, cur = _mock_conn(fetchone_seq=[person_row, None, person_row, None])
    summary = {"counts": {}, "last_touched_at": None}
    with (
        patch("robothor.identity.enrichment.get_connection", return_value=conn) as mock_get_conn,
        patch("robothor.identity.enrichment.get_person_summary", return_value=summary),
    ):
        enrichment.enrich_identity(_ctx(person_id="person-1"))
        enrichment.enrich_identity(_ctx(person_id="person-2"))

    assert mock_get_conn.call_count == 2
