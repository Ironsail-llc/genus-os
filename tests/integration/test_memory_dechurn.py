"""Dechurn is safe to schedule, and its observe mode produces evidence.

dechurn has never had a production caller. The harness measured the cost of
that: 14 near-duplicate rows outranking gold on 6 of 41 questions, with
recall@5 dropping to 70.0% on the near-duplicate stratum.

Before it can run unattended it needs three properties it did not have — a cap
so a mis-tuned threshold cannot deactivate the store in one night, a required
tenant so a shell call cannot hit the live store by default, and a persisted
manifest so the soft delete is reversible in practice and not just in
principle.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def churn_tenant(db_cursor, test_prefix):
    tenant = f"{test_prefix}-churn"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING",
        (tenant, tenant),
    )
    ids = []
    # Three near-identical restatements sharing an entity, plus one distinct.
    for text in (
        "the northern depot runs on a fortnightly schedule",
        "the northern depot runs on a fortnightly schedule this quarter",
        "the northern depot is running a fortnightly schedule",
        "the southern hub uses a weekly cadence entirely",
    ):
        db_cursor.execute(
            "INSERT INTO memory_facts (fact_text, category, entities, tenant_id, is_active) "
            "VALUES (%s,'project',%s,%s,TRUE) RETURNING id",
            (text, ["Depot"], tenant),
        )
        ids.append(db_cursor.fetchone()["id"])
    return {"tenant": tenant, "ids": ids}


def test_requires_an_explicit_tenant(churn_tenant):
    """A shell call with no tenant used to hit the operator's live store."""
    from robothor.memory.dechurn import dechurn

    with pytest.raises(ValueError, match="tenant"):
        dechurn("", dry_run=False)


def test_observe_writes_evidence_and_changes_nothing(
    churn_tenant, db_cursor, mock_get_connection
):
    from robothor.memory.dechurn import dechurn

    t = churn_tenant["tenant"]
    rep = dechurn(t, dry_run=True, jaccard=0.6)
    assert rep["near_dup_losers"] >= 1, f"fixture produced no candidates: {rep}"

    db_cursor.execute(
        "SELECT count(*) AS n FROM memory_facts WHERE tenant_id=%s AND is_active", (t,)
    )
    assert db_cursor.fetchone()["n"] == 4, "observe mode deactivated something"

    db_cursor.execute(
        "SELECT count(*) AS n FROM memory_facts_audit "
        "WHERE tenant_id=%s AND reason='dechurn_would_deactivate'",
        (t,),
    )
    assert db_cursor.fetchone()["n"] == rep["near_dup_losers"], (
        "observe produced no evidence rows — a soak with no events cannot "
        "distinguish a working control from an inert one"
    )


def test_enforce_deactivates_and_leaves_a_restorable_manifest(
    churn_tenant, db_cursor, mock_get_connection
):
    from robothor.memory.dechurn import dechurn

    t = churn_tenant["tenant"]
    rep = dechurn(t, dry_run=False, jaccard=0.6)
    losers = rep["deactivated_ids"]
    assert losers and rep["deactivated"] == len(losers)

    db_cursor.execute(
        "SELECT id FROM memory_facts WHERE tenant_id=%s AND NOT is_active", (t,)
    )
    assert {r["id"] for r in db_cursor.fetchall()} == set(losers)

    # The newest of a duplicate cluster must survive, and the distinct fact too.
    db_cursor.execute(
        "SELECT count(*) AS n FROM memory_facts WHERE tenant_id=%s AND is_active", (t,)
    )
    assert db_cursor.fetchone()["n"] == 4 - len(losers)

    db_cursor.execute(
        "SELECT fact_id FROM memory_facts_audit "
        "WHERE tenant_id=%s AND reason='dechurn_deactivated'",
        (t,),
    )
    assert {r["fact_id"] for r in db_cursor.fetchall()} == set(losers)

    # And the manifest actually restores.
    db_cursor.execute(
        "UPDATE memory_facts SET is_active=TRUE WHERE id IN "
        "(SELECT fact_id FROM memory_facts_audit WHERE tenant_id=%s AND reason='dechurn_deactivated')",
        (t,),
    )
    db_cursor.execute(
        "SELECT count(*) AS n FROM memory_facts WHERE tenant_id=%s AND is_active", (t,)
    )
    assert db_cursor.fetchone()["n"] == 4, "restore from the manifest did not recover every row"


def test_cap_refuses_rather_than_doing_partial_damage(
    churn_tenant, db_cursor, mock_get_connection
):
    """A mis-tuned jaccard must not deactivate a subset and call it progress."""
    from robothor.memory.dechurn import dechurn

    t = churn_tenant["tenant"]
    rep = dechurn(t, dry_run=False, jaccard=0.6, max_deactivations=0)
    assert "refused" in rep
    db_cursor.execute(
        "SELECT count(*) AS n FROM memory_facts WHERE tenant_id=%s AND is_active", (t,)
    )
    assert db_cursor.fetchone()["n"] == 4, "refused run still deactivated rows"


def test_other_tenants_are_untouched(churn_tenant, db_cursor, test_prefix, mock_get_connection):
    from robothor.memory.dechurn import dechurn

    other = f"{test_prefix}-other-churn"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING",
        (other, other),
    )
    for text in ("shared phrasing alpha beta", "shared phrasing alpha beta gamma"):
        db_cursor.execute(
            "INSERT INTO memory_facts (fact_text, category, entities, tenant_id, is_active) "
            "VALUES (%s,'project',%s,%s,TRUE)",
            (text, ["Depot"], other),
        )
    dechurn(churn_tenant["tenant"], dry_run=False, jaccard=0.6)
    db_cursor.execute(
        "SELECT count(*) AS n FROM memory_facts WHERE tenant_id=%s AND is_active", (other,)
    )
    assert db_cursor.fetchone()["n"] == 2, "dechurn crossed a tenant boundary"
