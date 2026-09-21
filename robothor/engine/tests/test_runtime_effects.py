"""Write-ahead effect attempts survive concurrency and worker process loss."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

from robothor.engine.runtime import ExecutionContext, effects
from robothor.goals.tests.test_store import private_database  # noqa: F401


@pytest.fixture
def effect_db(private_database, monkeypatch):  # noqa: F811
    @contextmanager
    def connect():
        conn = psycopg2.connect(private_database)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with connect() as conn, conn.cursor() as cur:
        migration = Path(__file__).parents[3] / "crm/migrations/141_runtime_effects.sql"
        cur.execute(migration.read_text())
        cur.execute(migration.read_text())
    monkeypatch.setattr(effects, "get_connection", connect)
    return connect


def context():
    return ExecutionContext(str(uuid4()), "operator", "request")


def begin(ctx, *, run="worker", args=None):
    record = effects.begin(ctx, run, "main", "create_note", args or {"body": "one"})
    if record["state"] == "prepared":
        assert effects.mark_dispatched(ctx, record["id"], run)
        record = effects.read(ctx, record["id"])
    return record


def test_concurrent_identical_intent_has_one_dispatch(effect_db):
    ctx = context()
    writes = []

    def invoke(index):
        try:
            receipt = begin(replace(ctx, request_id=f"request-{index}"), run=f"worker-{index}")
        except effects.EffectPendingError:
            return None
        writes.append(receipt["id"])
        return receipt

    with ThreadPoolExecutor(max_workers=20) as pool:
        receipts = list(pool.map(invoke, range(20)))
    assert len(writes) == 1 and sum(row is not None for row in receipts) == 1
    with effect_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT state FROM agent_runtime_effects WHERE tenant_id=%s", (ctx.tenant_id,))
        assert cur.fetchall() == [("dispatching",)]


def test_uncertainty_blocks_related_writes_but_not_other_work_or_identities(effect_db):
    ctx = context()
    row = begin(ctx)
    assert effects.finish(ctx, row["id"], "worker", uncertain=True)
    for other, args in [
        (ctx, {"body": "changed retry"}),
        (replace(ctx, request_id="new-request"), {"body": "one"}),
    ]:
        with pytest.raises(effects.EffectPendingError):
            begin(other, args=args)
    assert begin(replace(ctx, request_id="unrelated"), args={"body": "unrelated"})
    assert begin(replace(ctx, tenant_id=str(uuid4())))
    assert begin(replace(ctx, principal_id="other-operator"))
    assert effects.read(replace(ctx, principal_id="other-operator"), row["id"]) is None
    assert effects.read(replace(ctx, tenant_id=str(uuid4())), row["id"]) is None


def test_goal_family_uncertainty_survives_new_attempt_identity(effect_db):
    ctx = replace(context(), goal_id="goal", attempt_id="attempt", budget_id="family")
    row = begin(ctx)
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    for other in [
        replace(ctx, request_id="next", attempt_id="next-attempt"),
        replace(ctx, goal_id="child", attempt_id="child-attempt", request_id="child"),
    ]:
        with pytest.raises(effects.EffectPendingError):
            begin(other, args={"body": "different child action"})


def test_late_worker_and_elapsed_time_cannot_clear_uncertainty(effect_db):
    ctx = context()
    row = begin(ctx)
    assert not effects.finish(ctx, row["id"], "foreign-worker", uncertain=False)
    assert effects.abandon_run(ctx, "worker") == 1
    assert not effects.finish(ctx, row["id"], "worker", uncertain=False)
    with effect_db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_runtime_effects SET updated_at=now()-interval '30 days' WHERE id=%s",
            (row["id"],),
        )
    with pytest.raises(effects.EffectPendingError):
        begin(ctx)


@pytest.mark.parametrize(
    "verdict",
    [
        effects.Verification("unknown"),
        effects.Verification("not_applied", settled=False, reference="temporary absence"),
        effects.Verification("applied", settled=False, reference="still running", result={}),
    ],
)
def test_unsettled_provider_readback_does_not_release_write_barrier(effect_db, verdict):
    ctx = context()
    row = begin(ctx)
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    assert not effects.resolve(ctx, row["id"], lambda record, verdict=verdict: verdict)
    with pytest.raises(effects.EffectPendingError):
        begin(ctx)


def test_confirmed_recovery_returns_original_result_and_does_not_dispatch_again(effect_db):
    ctx = context()
    row = begin(ctx)
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    calls = []

    def verifier(record):
        calls.append(record["id"])
        return effects.Verification(
            "applied", True, "synthetic-provider:receipt-1", {"id": "note-1"}
        )

    assert effects.resolve(ctx, row["id"], verifier)
    assert not effects.resolve(ctx, row["id"], verifier)
    assert calls == [row["id"]]
    recovered = begin(ctx, run="replacement-worker")
    assert recovered["id"] == row["id"] and recovered["state"] == "confirmed"
    assert recovered["resolution"]["result"] == {"id": "note-1"}
    # A new, authorized request can deliberately perform a new action after resolution.
    assert begin(replace(ctx, request_id="intentional-new-request"))["state"] == "dispatching"


def test_definitive_non_application_allows_a_new_attempt(effect_db):
    ctx = context()
    row = begin(ctx)
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    assert effects.resolve(
        ctx,
        row["id"],
        lambda record: effects.Verification(
            "not_applied", True, "synthetic-provider:definitive-rejection"
        ),
    )
    assert begin(ctx)["id"] != row["id"]


def test_model_shaped_verdict_and_missing_evidence_cannot_release(effect_db):
    ctx = context()
    row = begin(ctx)
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    for verdict in [
        {"outcome": "applied", "settled": True},
        effects.Verification("applied", True, "", {"ok": True}),
    ]:
        with pytest.raises(ValueError):
            effects.resolve(ctx, row["id"], lambda record, verdict=verdict: verdict)
    assert effects.read(ctx, row["id"])["state"] == "uncertain"


def test_process_death_keeps_original_effect_and_recovery_prevents_replay(
    effect_db,
    private_database,  # noqa: F811
    tmp_path,
):
    import json
    import os
    import subprocess
    import sys

    ctx = context()
    provider = tmp_path / "synthetic-provider.json"
    code = """
import json, os
from contextlib import contextmanager
from pathlib import Path
import psycopg2
from robothor.engine.runtime import ExecutionContext, effects
settings = json.loads(os.environ['EFFECT_CRASH_FIXTURE'])
assert 'dbname=goal_pursuit_test ' in settings['dsn'] and '/pytest-' in settings['dsn']
@contextmanager
def connect():
    conn = psycopg2.connect(settings['dsn'])
    try:
        with conn:
            yield conn
    finally:
        conn.close()
effects.get_connection = connect
ctx = ExecutionContext(settings['tenant'], 'operator', 'request')
path = Path(settings['provider'])
try:
    receipt = effects.begin(ctx, 'lost-worker', 'main', 'create_note', {'body': 'one'})
except effects.EffectPendingError:
    print(json.dumps({'blocked': True}))
    raise SystemExit(0)
if receipt['state'] == 'confirmed':
    print(json.dumps({'recovered': receipt['resolution']['result']}))
    raise SystemExit(0)
assert effects.mark_dispatched(ctx, receipt['id'], 'lost-worker')
prior = json.loads(path.read_text())['writes'] if path.exists() else 0
path.write_text(json.dumps({'writes': prior + 1, 'effect_id': str(receipt['id'])}))
os._exit(76)  # Die after the provider commits, before finish() or normal cleanup.
"""
    settings = {"dsn": private_database, "tenant": ctx.tenant_id, "provider": str(provider)}

    def invoke():
        return subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "EFFECT_CRASH_FIXTURE": json.dumps(settings)},
            capture_output=True,
            text=True,
            timeout=20,
        )

    first = invoke()
    assert first.returncode == 76, first.stderr
    recorded = json.loads(provider.read_text())
    second = invoke()
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == {"blocked": True}
    assert json.loads(provider.read_text())["writes"] == 1
    assert effects.read(ctx, recorded["effect_id"])["state"] == "dispatching"
    effects.abandon_run(ctx, "lost-worker")
    assert effects.resolve(
        ctx,
        recorded["effect_id"],
        lambda row: effects.Verification(
            "applied", True, str(provider), {"writes": json.loads(provider.read_text())["writes"]}
        ),
    )
    third = invoke()
    assert third.returncode == 0, third.stderr
    assert json.loads(third.stdout) == {"recovered": {"writes": 1}}
    assert json.loads(provider.read_text())["writes"] == 1


def test_late_admission_cannot_dispatch_after_prepared_record_was_withdrawn(effect_db):
    ctx = context()
    record = effects.begin(ctx, "old-worker", "main", "create_note", {"body": "one"})
    assert record["state"] == "prepared"
    assert effects.abandon_run(ctx, "old-worker") == 1
    assert not effects.mark_dispatched(ctx, record["id"], "old-worker")
    assert effects.read(ctx, record["id"])["state"] == "not_applied"
    assert begin(ctx)["state"] == "dispatching"


def test_effect_rows_obey_database_tenant_policy(effect_db):
    from psycopg2 import sql

    ctx = context()
    record = begin(ctx)
    role = "effect_reader_" + uuid4().hex
    with effect_db() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        cur.execute(
            sql.SQL("GRANT SELECT ON agent_runtime_effects TO {}").format(sql.Identifier(role))
        )
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        cur.execute("SELECT set_config('app.tenant_id',%s,true)", ("foreign-tenant",))
        cur.execute("SELECT id FROM agent_runtime_effects WHERE id=%s", (record["id"],))
        assert cur.fetchall() == []
        cur.execute("SELECT set_config('app.tenant_id',%s,true)", (ctx.tenant_id,))
        cur.execute("SELECT id FROM agent_runtime_effects WHERE id=%s", (record["id"],))
        assert str(cur.fetchone()[0]) == str(record["id"])
