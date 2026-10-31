"""The Helm's Memory page: list facts, preview a forget, forget one fact.

What these tests pin, and why each is a test rather than a docstring:

* **The gate is the operator's.** Forgetting a fact is an irreversible edit to
  what the fleet believes; an agent's own token must not reach it, and neither
  must a member session, an auditor, or another tenant's owner.
* **Preview writes nothing.** The whole point of a preview is that an operator
  can look before they act, so "read-only" is asserted against the row itself
  rather than trusted from the handler's shape.
* **A second forget is a 409, not a silent success.** Idempotence that reports
  success hides a double-click from the person who has to explain, later, when
  the fact stopped being believed.
* **The audit row names the fact, never quotes it.** ``fact_text`` is the
  content the operator is deleting, and the audit log is exported to a SIEM.

The DB-backed half lives in ``test_memory_facts_query_integration.py`` — this
module is the unit lane, so every assertion here holds with no database at all.
"""

from __future__ import annotations

import pytest

FACTS = "/api/memory/facts"
PREVIEW = "/api/memory/facts/1/forget/preview"
FORGET = "/api/memory/facts/1/forget"


@pytest.fixture
def no_db(monkeypatch):
    """Any query this router makes is a test failure in this lane.

    Not a convenience: every assertion below is about a refusal, and a refusal
    that happens AFTER the row is read is a different (worse) route than one
    that happens before. Binding the connection to an exception is what proves
    which of the two shipped.
    """
    from routers import memory_facts

    def _boom(*_args, **_kwargs):
        raise AssertionError("the unit lane must not open a database connection")

    monkeypatch.setattr(memory_facts, "get_connection", _boom)


# ── the gate ─────────────────────────────────────────────────────────────────


def test_every_route_rejects_a_service_token(controls_client_as_service, no_db):
    """An agent must not be able to read, preview or erase the fleet's memory."""
    assert controls_client_as_service.get(FACTS).status_code == 403
    assert controls_client_as_service.post(PREVIEW).status_code == 403
    assert controls_client_as_service.post(FORGET, json={"reason": "spam"}).status_code == 403


def test_every_route_rejects_a_non_operator_human(controls_client_as_viewer, no_db):
    assert controls_client_as_viewer.get(FACTS).status_code == 403
    assert controls_client_as_viewer.post(PREVIEW).status_code == 403
    assert controls_client_as_viewer.post(FORGET, json={"reason": "spam"}).status_code == 403


def test_every_route_rejects_another_tenants_owner(controls_client_as_other_tenant_owner, no_db):
    client = controls_client_as_other_tenant_owner
    assert client.get(FACTS).status_code == 403
    assert client.post(PREVIEW).status_code == 403
    assert client.post(FORGET, json={"reason": "spam"}).status_code == 403


def test_an_auditor_may_not_touch_memory(controls_client_as_auditor, no_db):
    """Read-only plus the audit log — not a licence to edit what is believed."""
    assert controls_client_as_auditor.get(FACTS).status_code == 403
    assert controls_client_as_auditor.post(FORGET, json={"reason": "spam"}).status_code == 403


# ── validation, all of it before any query ───────────────────────────────────


@pytest.mark.parametrize("limit", ["0", "201", "-1", "abc", "1_0", "+7", " 5 ", "0x10"])
def test_limit_is_bounded(controls_client_as_operator, no_db, limit):
    assert controls_client_as_operator.get(f"{FACTS}?limit={limit}").status_code == 422


@pytest.mark.parametrize("active", ["yes", "1", "TRUE-ish", ""])
def test_active_takes_only_the_three_words(controls_client_as_operator, no_db, active):
    assert controls_client_as_operator.get(f"{FACTS}?active={active}").status_code == 422


@pytest.mark.parametrize("cursor", ["abc", "1.5", "-", "0", "1_0", "+7", " 5 ", "007"])
def test_a_cursor_that_is_not_a_fact_id_is_422(controls_client_as_operator, no_db, cursor):
    assert controls_client_as_operator.get(f"{FACTS}?cursor={cursor}").status_code == 422


@pytest.mark.parametrize(
    "fact_id", ["abc", "1.5", "-3", "0", "1%20OR%201=1", "1_0", "+7", "%205%20", "007"]
)
def test_a_fact_id_that_is_not_an_id_is_422(controls_client_as_operator, no_db, fact_id):
    """Validated at the boundary, by SHAPE and not by ``int()``.

    ``int("1_0")`` is 10, so ``POST /api/memory/facts/1_0/forget`` used to
    deactivate fact **10** — and the audit row then carried ``fact_id: 10``
    beside a ``path`` naming ``1_0``: two answers to "which fact did the
    operator forget" inside one audit record. ``+7``, ``007`` and a padded
    ``" 5 "`` went the same way.
    """
    client = controls_client_as_operator
    assert client.post(f"/api/memory/facts/{fact_id}/forget/preview").status_code == 422
    assert (
        client.post(f"/api/memory/facts/{fact_id}/forget", json={"reason": "spam"}).status_code
        == 422
    )


def test_a_cursor_cannot_be_combined_with_a_query(controls_client_as_operator, no_db):
    """There is no keyset over a relevance ranking, so there is no cursor.

    Refused rather than ignored: the page that sends both would otherwise be
    handed page one under the impression it asked for page two, and would go on
    asking forever.
    """
    resp = controls_client_as_operator.get(f"{FACTS}?q=tea&cursor=99")
    assert resp.status_code == 422
    assert "cursor" in resp.json()["detail"]
    assert "q" in resp.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reason": ""},
        {"reason": "  "},
        {"reason": "ab"},
        # A non-string reason is the same refusal, and must render the same
        # way: a pydantic ``str`` annotation would reject these before the
        # handler runs and answer with the nested ``{"detail": [ {...} ]}``.
        {"reason": 12345},
        {"reason": ["a", "b"]},
        {"reason": None},
    ],
)
def test_a_forget_without_a_real_reason_is_422(controls_client_as_operator, no_db, body):
    resp = controls_client_as_operator.post(FORGET, json=body)
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str), "the Helm was promised a flat 422"


def test_a_reason_longer_than_the_audit_column_is_422(controls_client_as_operator, no_db):
    assert controls_client_as_operator.post(FORGET, json={"reason": "x" * 501}).status_code == 422


# ── edit ─────────────────────────────────────────────────────────────────────
#
# ``PATCH /api/memory/facts/{id}`` corrects one fact and keeps the original on
# record. The gate and every validation refusal come before any query, which is
# what ``no_db`` proves; the two outcomes that need a row under them (the 409
# mapping and the audit call) run against a scripted connection below, and the
# real table is exercised in ``test_memory_facts_query_integration.py``.

EDIT = "/api/memory/facts/1"
GOOD_EDIT = {"fact_text": "Alice prefers green tea", "reason": "the extraction got it wrong"}


def test_edit_rejects_a_service_token(controls_client_as_service, no_db):
    assert controls_client_as_service.patch(EDIT, json=GOOD_EDIT).status_code == 403


def test_edit_rejects_a_viewer(controls_client_as_viewer, no_db):
    assert controls_client_as_viewer.patch(EDIT, json=GOOD_EDIT).status_code == 403


def test_edit_rejects_an_auditor(controls_client_as_auditor, no_db):
    assert controls_client_as_auditor.patch(EDIT, json=GOOD_EDIT).status_code == 403


def test_edit_rejects_another_tenants_owner(controls_client_as_other_tenant_owner, no_db):
    assert controls_client_as_other_tenant_owner.patch(EDIT, json=GOOD_EDIT).status_code == 403


@pytest.mark.parametrize("fact_id", ["abc", "1.5", "-3", "0", "1_0", "+7", "007"])
def test_an_edit_of_something_that_is_not_an_id_is_422(controls_client_as_operator, no_db, fact_id):
    resp = controls_client_as_operator.patch(f"/api/memory/facts/{fact_id}", json=GOOD_EDIT)
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        # Nothing to change: a reason alone is not an edit.
        {"reason": "the extraction got it wrong"},
        {"fact_text": None, "entities": None, "reason": "the extraction got it wrong"},
        # A fact cannot be edited into nothing — forget is the way to stop
        # believing one.
        {"fact_text": "", "reason": "the extraction got it wrong"},
        {"fact_text": "   ", "reason": "the extraction got it wrong"},
        {"fact_text": 12345, "reason": "the extraction got it wrong"},
        {"fact_text": "x" * 2001, "reason": "the extraction got it wrong"},
        {"entities": "Alice", "reason": "the extraction got it wrong"},
        {"entities": ["Alice", 7], "reason": "the extraction got it wrong"},
        {"entities": ["x" * 201], "reason": "the extraction got it wrong"},
        {"entities": [f"e{n}" for n in range(51)], "reason": "the extraction got it wrong"},
        # The reason rule is forget's, word for word.
        {"fact_text": "Alice prefers green tea"},
        {"fact_text": "Alice prefers green tea", "reason": ""},
        {"fact_text": "Alice prefers green tea", "reason": "ab"},
        {"fact_text": "Alice prefers green tea", "reason": 12345},
        {"fact_text": "Alice prefers green tea", "reason": "x" * 501},
    ],
)
def test_an_edit_that_is_not_a_real_edit_is_a_flat_422(controls_client_as_operator, no_db, body):
    resp = controls_client_as_operator.patch(EDIT, json=body)
    assert resp.status_code == 422, body
    assert isinstance(resp.json()["detail"], str), "the Helm was promised a flat 422"


def test_an_edit_without_a_reason_says_edit_not_forget(controls_client_as_operator, no_db):
    detail = controls_client_as_operator.patch(
        EDIT, json={"fact_text": "Alice prefers green tea", "reason": "ab"}
    ).json()["detail"]
    assert "edit" in detail
    assert "forget" not in detail


# A scripted connection for the two outcomes that need a row. It answers by
# statement, not by call order, so the test says what the table holds rather
# than how many queries the handler happens to make.

_LOCKED = ("Alice prefers tea", ["Alice"], True, "fact", None, None)


class _ScriptedCursor:
    def __init__(self, locked, *, update_raises=None):
        self.locked = locked
        self.update_raises = update_raises
        self.statements: list[tuple[str, object]] = []
        self._last = ""
        self._text = locked[0] if locked else ""
        self._entities = list(locked[1]) if locked else []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self._last = sql
        if sql.lstrip().upper().startswith("UPDATE MEMORY_FACTS"):
            if self.update_raises is not None:
                raise self.update_raises
            self._text, self._entities = params[0], params[1]

    def fetchone(self):
        if "FOR UPDATE" in self._last:
            return self.locked
        if "INSERT INTO memory_facts_audit" in self._last:
            return (77,)
        if "FROM memory_facts" in self._last:
            return (
                1,
                self._text,
                self._entities,
                True,
                None,
                None,
                0.9,
                "chat",
                None,
                None,
                {"at": "2026-10-30T12:00:00+00:00", "by": "operator:alice"},
            )
        return None


VECTOR = [0.25] * 4


@pytest.fixture
def embeddings(monkeypatch):
    """The embedding provider, stubbed: records each text, answers ``VECTOR``.

    Set ``state["fail"]`` to make it raise the way an unreachable provider does.
    """
    state: dict = {"calls": [], "fail": False}

    async def _embed(text, *_args, **_kwargs):
        state["calls"].append(text)
        if state["fail"]:
            raise ConnectionError("embedding provider unreachable")
        return VECTOR

    monkeypatch.setattr("robothor.llm.ollama.get_embedding_async", _embed)
    return state


@pytest.fixture
def scripted_db(monkeypatch, embeddings):
    """Bind the router to a ``_ScriptedCursor``; return a setter for its script."""
    from contextlib import contextmanager

    from routers import memory_facts

    holder: dict[str, _ScriptedCursor] = {}

    def _bind(locked=_LOCKED, *, update_raises=None) -> _ScriptedCursor:
        cursor = _ScriptedCursor(locked, update_raises=update_raises)
        holder["cursor"] = cursor

        class _Conn:
            def cursor(self):
                return cursor

        @contextmanager
        def _conn():
            yield _Conn()

        monkeypatch.setattr(memory_facts, "get_connection", _conn)
        return cursor

    return _bind


@pytest.fixture
def audit_calls(monkeypatch):
    from routers import memory_facts

    calls: list[dict] = []

    def _audited(request, event_type, **kwargs):
        calls.append({"event_type": event_type, **kwargs})

    monkeypatch.setattr(memory_facts, "audited", _audited)
    return calls


def test_editing_another_tenants_fact_is_404(controls_client_as_operator, scripted_db, audit_calls):
    """The tenant predicate is on the locking read: no row there is a 404, never a 403."""
    cursor = scripted_db(locked=None)
    resp = controls_client_as_operator.patch(EDIT, json=GOOD_EDIT)
    assert resp.status_code == 404
    lock_sql, lock_params = cursor.statements[0]
    assert "FOR UPDATE" in lock_sql
    assert "tenant_id" in lock_sql
    assert not any(sql.lstrip().upper().startswith("UPDATE") for sql, _ in cursor.statements)
    assert audit_calls == []


def test_editing_an_inactive_fact_is_422(controls_client_as_operator, scripted_db, audit_calls):
    cursor = scripted_db(locked=("Alice moved on", ["Alice"], False, "fact", None, None))
    resp = controls_client_as_operator.patch(EDIT, json=GOOD_EDIT)
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
    assert "inactive" in resp.json()["detail"]
    assert not any("memory_facts_audit" in sql for sql, _ in cursor.statements)
    assert audit_calls == []


def test_an_edit_onto_another_active_facts_text_is_a_409(
    controls_client_as_operator, scripted_db, audit_calls
):
    """Migration 078's unique index, in the operator's words — not a 500."""
    import psycopg2.errors

    scripted_db(update_raises=psycopg2.errors.UniqueViolation("duplicate key"))
    resp = controls_client_as_operator.patch(EDIT, json=GOOD_EDIT)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "an active fact with this text already exists"
    assert audit_calls == []


def test_an_edit_audits_the_id_and_the_reason_never_the_text(
    controls_client_as_operator, scripted_db, audit_calls
):
    scripted_db()
    resp = controls_client_as_operator.patch(
        EDIT,
        json={
            "fact_text": "Alice prefers green tea",
            "entities": ["Alice"],
            "reason": "the extraction got it wrong",
        },
    )
    assert resp.status_code == 200
    assert audit_calls == [
        {
            "event_type": "memory.edit",
            "action": "1",
            "fact_id": 1,
            "reason": "the extraction got it wrong",
        }
    ]
    # Neither the text being replaced nor its replacement.
    assert "tea" not in str(audit_calls)


def test_an_edit_returns_the_fact_in_the_list_shape(
    controls_client_as_operator, scripted_db, audit_calls
):
    scripted_db()
    body = controls_client_as_operator.patch(EDIT, json=GOOD_EDIT).json()
    fact = body["fact"]
    assert fact["id"] == 1
    assert fact["fact_text"] == "Alice prefers green tea"
    assert fact["operator_confirmed"] == {
        "at": "2026-10-30T12:00:00+00:00",
        "by": "operator:alice",
    }
    assert set(fact) >= {"entities", "is_active", "valid_from", "source", "superseded_by"}


def test_an_edit_snapshots_the_original_before_it_writes(
    controls_client_as_operator, scripted_db, audit_calls
):
    cursor = scripted_db()
    controls_client_as_operator.patch(EDIT, json=GOOD_EDIT)
    kinds = [sql.lstrip().split()[0].upper() for sql, _ in cursor.statements]
    snapshot_at = next(
        i for i, (sql, _) in enumerate(cursor.statements) if "memory_facts_audit" in sql
    )
    update_at = next(
        i
        for i, (sql, _) in enumerate(cursor.statements)
        if sql.lstrip().upper().startswith("UPDATE MEMORY_FACTS")
    )
    assert snapshot_at < update_at, kinds
    assert "DELETE" not in kinds
    snapshot_params = cursor.statements[snapshot_at][1]
    assert "Alice prefers tea" in snapshot_params
    assert "operator_edit" in snapshot_params


# ── edit: the embedding follows the text ─────────────────────────────────────


def _update(cursor: _ScriptedCursor) -> tuple[str, object]:
    return next(
        (sql, params)
        for sql, params in cursor.statements
        if sql.lstrip().upper().startswith("UPDATE MEMORY_FACTS")
    )


def _metadata_patch(params) -> dict:
    import json

    patches = [json.loads(p) for p in params if isinstance(p, str) and p.startswith("{")]
    assert len(patches) == 1, params
    return patches[0]


def test_a_text_edit_writes_a_fresh_embedding(
    controls_client_as_operator, scripted_db, embeddings, audit_calls
):
    cursor = scripted_db()
    resp = controls_client_as_operator.patch(EDIT, json=GOOD_EDIT)
    assert resp.status_code == 200, resp.text
    assert embeddings["calls"] == ["Alice prefers green tea"]
    sql, params = _update(cursor)
    assert "embedding" in sql
    assert VECTOR in params
    assert "embedding_stale" not in _metadata_patch(params)


def test_an_unreachable_embedding_provider_still_saves_and_marks_the_vector_stale(
    controls_client_as_operator, scripted_db, embeddings, audit_calls, caplog
):
    embeddings["fail"] = True
    cursor = scripted_db()
    resp = controls_client_as_operator.patch(EDIT, json=GOOD_EDIT)
    assert resp.status_code == 200, resp.text
    _, params = _update(cursor)
    assert VECTOR not in params
    assert _metadata_patch(params)["embedding_stale"] is True
    # The warning says what happened, never what the fact says.
    assert "green tea" not in caplog.text


def test_an_entities_only_edit_asks_for_no_embedding(
    controls_client_as_operator, scripted_db, embeddings, audit_calls
):
    cursor = scripted_db()
    resp = controls_client_as_operator.patch(
        EDIT, json={"entities": ["Alice", "Tea"], "reason": "wrong entity name"}
    )
    assert resp.status_code == 200, resp.text
    assert embeddings["calls"] == []
    _, params = _update(cursor)
    assert "embedding_stale" not in _metadata_patch(params)


def test_an_unchanged_text_keeps_the_stored_vector(
    controls_client_as_operator, scripted_db, embeddings, audit_calls
):
    """Saving the same words (give or take surrounding blanks) is not a text edit."""
    cursor = scripted_db(locked=("Alice prefers tea ", ["Alice"], True, "fact", None, None))
    resp = controls_client_as_operator.patch(
        EDIT, json={"fact_text": "  Alice prefers tea", "reason": "confirming it"}
    )
    assert resp.status_code == 200, resp.text
    _, params = _update(cursor)
    assert VECTOR not in params
    assert "embedding_stale" not in _metadata_patch(params)


def test_entities_are_deduplicated_ignoring_case(
    controls_client_as_operator, scripted_db, embeddings, audit_calls
):
    cursor = scripted_db()
    controls_client_as_operator.patch(
        EDIT, json={"entities": ["Alice", "alice", " ALICE ", "Tea"], "reason": "tidy up"}
    )
    _, params = _update(cursor)
    assert ["Alice", "Tea"] in params
