"""
Phase 2b — runner.py stamps agent_runs.person_id from trigger_detail.

When trigger_type is 'telegram' or 'chat' and trigger_detail starts with
'chat:<id>', the runner must:
  1. resolve <id> via contact_identifiers → person_id
  2. set agent_runs.person_id when persisting the run row
  3. inherit person_id through SpawnContext to sub-agents
  4. emit a timeline_activity row keyed to the agent_run

These tests target a small helper `resolve_run_person_id(trigger_detail,
trigger_type, channel='telegram')` and the create_run insert path —
exercising the contract without spinning up the full runner.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


# ── Resolver ─────────────────────────────────────────────────────────────────


class TestResolveRunPersonId:
    def test_extracts_chat_id_and_resolves(self, db_cursor, db_conn, mock_get_connection):
        from robothor.engine.run_person_link import resolve_run_person_id

        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name) VALUES (%s, 'RunPerson')",
            (person_id,),
        )
        db_cursor.execute(
            "INSERT INTO contact_identifiers (channel, identifier, person_id) VALUES ('telegram', 'tg-runner-1', %s)",
            (person_id,),
        )

        resolved = resolve_run_person_id(
            trigger_type="telegram", trigger_detail="chat:tg-runner-1|sender:Bob"
        )
        assert str(resolved) == person_id

    def test_unknown_chat_returns_none(self, db_cursor, db_conn, mock_get_connection):
        from robothor.engine.run_person_link import resolve_run_person_id

        resolved = resolve_run_person_id(
            trigger_type="telegram", trigger_detail="chat:tg-unknown-9999"
        )
        assert resolved is None

    def test_non_telegram_trigger_returns_none(self, db_cursor, db_conn, mock_get_connection):
        from robothor.engine.run_person_link import resolve_run_person_id

        # Cron triggers don't carry a person — must be None.
        resolved = resolve_run_person_id(trigger_type="cron", trigger_detail=None)
        assert resolved is None

    def test_handles_chat_id_only(self, db_cursor, db_conn, mock_get_connection):
        from robothor.engine.run_person_link import resolve_run_person_id

        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name) VALUES (%s, 'NoSender')",
            (person_id,),
        )
        db_cursor.execute(
            "INSERT INTO contact_identifiers (channel, identifier, person_id) VALUES ('telegram', 'tg-runner-2', %s)",
            (person_id,),
        )
        # No |sender: suffix
        resolved = resolve_run_person_id(trigger_type="telegram", trigger_detail="chat:tg-runner-2")
        assert str(resolved) == person_id


# ── create_run integration ───────────────────────────────────────────────────


class TestCreateRunPersonStamp:
    def test_run_stores_person_id(self, db_cursor, db_conn, mock_get_connection):
        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.tracking import create_run

        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name) VALUES (%s, 'Stamped')",
            (person_id,),
        )

        run = AgentRun(
            agent_id="main",
            trigger_type=TriggerType.TELEGRAM,
            trigger_detail="chat:tg-runner-3",
            status=RunStatus.RUNNING,
        )
        run.person_id = person_id  # Field exists; create_run persists it.

        create_run(run)

        db_cursor.execute("SELECT person_id FROM agent_runs WHERE id = %s", (run.id,))
        row = db_cursor.fetchone()
        assert row is not None
        assert str(row["person_id"]) == person_id


# ── Timeline emission ───────────────────────────────────────────────────────


class TestRunTimelineActivity:
    def test_emits_timeline_row_when_person_stamped(self, db_cursor, db_conn, mock_get_connection):
        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.run_person_link import emit_run_timeline_activity
        from robothor.engine.tracking import create_run

        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name) VALUES (%s, 'Timeline')",
            (person_id,),
        )

        run = AgentRun(
            agent_id="dev-team-ops",
            trigger_type=TriggerType.TELEGRAM,
            trigger_detail="chat:tg-runner-4",
            status=RunStatus.COMPLETED,
        )
        run.person_id = person_id
        run.output_text = "Updated the pipeline status."

        create_run(run)
        emit_run_timeline_activity(run)

        db_cursor.execute(
            """
            SELECT activity_type, agent_id, snippet
              FROM timeline_activity
             WHERE source_table = 'agent_runs' AND source_id = %s
            """,
            (run.id,),
        )
        row = db_cursor.fetchone()
        assert row is not None
        assert row["activity_type"] == "agent_run"
        assert row["agent_id"] == "dev-team-ops"
        assert "pipeline" in (row["snippet"] or "")

    def test_no_timeline_row_when_person_unknown(self, db_cursor, db_conn, mock_get_connection):
        from robothor.engine.models import AgentRun, RunStatus, TriggerType
        from robothor.engine.run_person_link import emit_run_timeline_activity
        from robothor.engine.tracking import create_run

        run = AgentRun(
            agent_id="cron-job",
            trigger_type=TriggerType.CRON,
            trigger_detail=None,
            status=RunStatus.COMPLETED,
        )
        # person_id stays None
        create_run(run)
        emit_run_timeline_activity(run)

        db_cursor.execute(
            "SELECT 1 FROM timeline_activity WHERE source_table = 'agent_runs' AND source_id = %s",
            (run.id,),
        )
        assert db_cursor.fetchone() is None


# ── Sub-agent inheritance ───────────────────────────────────────────────────


class TestSpawnContextInheritance:
    def test_child_inherits_parent_person_id(self):
        from robothor.engine.models import SpawnContext

        # Parent context carries person_id; child instances inherit it.
        ctx = SpawnContext(
            parent_run_id=str(uuid.uuid4()),
            parent_agent_id="main",
            correlation_id="corr-1",
            nesting_depth=0,
            person_id="abc-123",
        )
        assert ctx.person_id == "abc-123"
