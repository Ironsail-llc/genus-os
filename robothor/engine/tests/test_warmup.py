"""Tests for the warmup module — session warmth preamble building."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from robothor.engine.models import AgentConfig
from robothor.engine.warmup import (
    _CONTEXT_HOOKS,
    MAX_WARMTH_CHARS,
    build_interactive_preamble,
    build_warmth_preamble,
)
from robothor.identity import EnrichedIdentity, IdentityContext

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def empty_config() -> AgentConfig:
    return AgentConfig(id="test-agent", name="Test Agent")


@pytest.fixture
def warm_config() -> AgentConfig:
    return AgentConfig(
        id="email-responder",
        name="Email Responder",
        warmup_memory_blocks=["operational_findings"],
        warmup_context_files=["brain/memory/response-status.md"],
        warmup_peer_agents=["email-classifier"],
    )


# Patch targets — functions are imported lazily inside warmup.py
TRACKING_PATCH = "robothor.engine.tracking.get_schedule"
BLOCK_PATCH = "robothor.memory.blocks.read_block"


class TestBuildWarmthPreamble:
    """Tests for the main build_warmth_preamble function."""

    def test_empty_config_returns_empty(self, empty_config: AgentConfig, tmp_path: Path) -> None:
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with patch(TRACKING_PATCH, return_value=None):
                result, _ = build_warmth_preamble(empty_config, tmp_path)
            assert result == ""
        finally:
            _CONTEXT_HOOKS.extend(saved)

    def test_history_with_consecutive_errors(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_context_files=["nonexistent.md"],
        )
        schedule = {
            "last_status": "failed",
            "last_duration_ms": 5000,
            "last_run_at": datetime.now(UTC) - timedelta(hours=2),
            "consecutive_errors": 3,
        }
        with patch(TRACKING_PATCH, return_value=schedule):
            result, _ = build_warmth_preamble(config, tmp_path)
        assert "WARNING" in result
        assert "3 consecutive errors" in result

    def test_history_no_data_graceful(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_context_files=["nonexistent.md"],
        )
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with patch(TRACKING_PATCH, return_value=None):
                result, _ = build_warmth_preamble(config, tmp_path)
            assert result == ""
        finally:
            _CONTEXT_HOOKS.extend(saved)

    def test_memory_block_injection(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_memory_blocks=["operational_findings"],
        )
        with (
            patch(TRACKING_PATCH, return_value=None),
            patch(BLOCK_PATCH, return_value={"content": "Key finding: system is healthy."}),
        ):
            result, _ = build_warmth_preamble(config, tmp_path)
        assert "MEMORY BLOCKS" in result
        assert "operational_findings" in result
        assert "Key finding" in result

    def test_memory_block_missing_graceful(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_memory_blocks=["nonexistent_block"],
        )
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                patch(TRACKING_PATCH, return_value=None),
                patch(BLOCK_PATCH, return_value={"content": ""}),
            ):
                result, _ = build_warmth_preamble(config, tmp_path)
            assert result == ""
        finally:
            _CONTEXT_HOOKS.extend(saved)

    def test_context_file_injection(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_context_files=["status.md"],
        )
        status_file = tmp_path / "status.md"
        status_file.write_text("Last run: 2026-02-27 OK\nProcessed 5 emails.")

        with patch(TRACKING_PATCH, return_value=None):
            result, _ = build_warmth_preamble(config, tmp_path)
        assert "CONTEXT FILES" in result
        assert "status.md" in result
        assert "Last run: 2026-02-27 OK" in result

    def test_context_file_missing_graceful(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_context_files=["does-not-exist.md"],
        )
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with patch(TRACKING_PATCH, return_value=None):
                result, _ = build_warmth_preamble(config, tmp_path)
            assert result == ""
        finally:
            _CONTEXT_HOOKS.extend(saved)

    def test_peer_section(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_peer_agents=["email-classifier", "email-analyst"],
        )

        def side_effect(agent_id: str):
            if agent_id == "test-agent":
                return None
            if agent_id == "email-classifier":
                return {
                    "last_status": "completed",
                    "last_run_at": datetime.now(UTC) - timedelta(hours=1),
                    "consecutive_errors": 0,
                }
            if agent_id == "email-analyst":
                return {
                    "last_status": "failed",
                    "last_run_at": datetime.now(UTC) - timedelta(minutes=30),
                    "consecutive_errors": 2,
                }
            return None

        with patch(TRACKING_PATCH, side_effect=side_effect):
            result, _ = build_warmth_preamble(config, tmp_path)
        assert "PEER AGENTS" in result
        assert "email-classifier: completed" in result
        assert "email-analyst: failed" in result
        assert "2 errors" in result

    def test_total_truncation(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_memory_blocks=["big_block"],
            warmup_context_files=["big_file.md"],
        )
        big_file = tmp_path / "big_file.md"
        big_file.write_text("y" * 5000)

        with (
            patch(TRACKING_PATCH, return_value=None),
            patch(BLOCK_PATCH, return_value={"content": "x" * 5000}),
        ):
            result, _ = build_warmth_preamble(config, tmp_path)
        assert len(result) <= MAX_WARMTH_CHARS + 50  # allow for truncation marker

    def test_history_section_completed_run(self, tmp_path: Path) -> None:
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_context_files=["nonexistent.md"],
        )
        schedule = {
            "last_status": "completed",
            "last_duration_ms": 12345,
            "last_run_at": datetime.now(UTC) - timedelta(hours=3),
            "consecutive_errors": 0,
        }
        with patch(TRACKING_PATCH, return_value=schedule):
            result, _ = build_warmth_preamble(config, tmp_path)
        assert "SESSION HISTORY" in result
        assert "completed" in result
        assert "12345ms" in result

    def test_all_sections_combined(self, tmp_path: Path) -> None:
        """Full warmup with all sections populated."""
        config = AgentConfig(
            id="test-agent",
            name="Test",
            warmup_memory_blocks=["findings"],
            warmup_context_files=["status.md"],
            warmup_peer_agents=["peer-1"],
        )
        status_file = tmp_path / "status.md"
        status_file.write_text("Agent OK")

        schedule_self = {
            "last_status": "completed",
            "last_duration_ms": 100,
            "last_run_at": datetime.now(UTC) - timedelta(hours=1),
            "consecutive_errors": 0,
        }
        schedule_peer = {
            "last_status": "completed",
            "last_run_at": datetime.now(UTC) - timedelta(hours=2),
            "consecutive_errors": 0,
        }

        def schedule_side_effect(agent_id: str):
            if agent_id == "test-agent":
                return schedule_self
            return schedule_peer

        with (
            patch(TRACKING_PATCH, side_effect=schedule_side_effect),
            patch(BLOCK_PATCH, return_value={"content": "block content here"}),
        ):
            result, _ = build_warmth_preamble(config, tmp_path)

        assert "SESSION HISTORY" in result
        assert "MEMORY BLOCKS" in result
        assert "CONTEXT FILES" in result
        assert "PEER AGENTS" in result


class TestSchedulerWarmup:
    """Test that warmup is now handled by runner, not scheduler."""

    def test_build_payload_no_warmup(self, engine_config, sample_agent_config) -> None:
        """_build_payload no longer calls warmup — that's centralized in runner.execute()."""
        from robothor.engine.runner import AgentRunner
        from robothor.engine.scheduler import CronScheduler

        runner = AgentRunner(engine_config)
        scheduler = CronScheduler(engine_config, runner)

        payload = scheduler._build_payload(sample_agent_config)
        assert "SESSION HISTORY" not in payload
        assert "Execute your scheduled tasks" in payload


class TestBuildInteractivePreamble:
    """Tests for build_interactive_preamble with sender identity."""

    def test_sender_name_injects_identity_section(self) -> None:
        """When sender_name is provided, preamble includes identity section."""
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with patch(BLOCK_PATCH, return_value=None):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello",
                    include_blocks=False,
                    sender_name="Alice",
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "CURRENT USER" in result
        assert "Alice" in result
        assert "Do not confuse" in result

    def test_no_sender_name_omits_identity_section(self) -> None:
        """When sender_name is empty, no identity section is injected."""
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with patch(BLOCK_PATCH, return_value=None):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello",
                    include_blocks=False,
                    sender_name="",
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "CURRENT USER" not in result

    def test_entity_context_excludes_sender_name(self) -> None:
        """_build_entity_context skips the sender's name from entity search."""
        from robothor.engine.warmup import _build_entity_context

        # Mock the DB call — if "Alice" is excluded, it shouldn't appear
        # in the entity search candidates at all
        with patch("robothor.db.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = []

            # Message mentions "Alice" — but she's excluded
            result = _build_entity_context(
                "Tell Alice about the project",
                exclude_names={"Alice"},
            )

            # If Alice was excluded, fewer (or zero) queries should have been made
            # for that name. With only "Alice" as candidate and it excluded,
            # we should get empty result.
            assert result == ""

    def test_entity_context_without_exclusion(self) -> None:
        """Without exclusion, names are searched normally."""
        from robothor.engine.warmup import _build_entity_context

        with patch("robothor.db.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.return_value = mock_cursor
            mock_cursor.fetchall.return_value = [
                {"fact_text": "Alice works at Acme", "category": "person", "importance_score": 0.8},
            ]

            result = _build_entity_context(
                "Tell Alice about the project",
                exclude_names=None,
            )

            assert "Alice works at Acme" in result

    def test_identity_injects_prompt_block_instead_of_sender_name(self) -> None:
        """When identity is provided, its prompt_block() replaces the legacy
        sender_name text — even if sender_name is also passed (back-compat)."""
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        identity = IdentityContext(
            tenant_id="t-alpha",
            channel="webchat",
            identifier="acct-1",
            verified=True,
            display_name="Alice",
            role="owner",
        )
        try:
            with (
                patch(BLOCK_PATCH, return_value=None),
                patch("robothor.engine.warmup.enrich_identity", return_value=None) as mock_enrich,
            ):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello",
                    include_blocks=False,
                    sender_name="Bob",  # should be ignored — identity wins
                    identity=identity,
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "--- CURRENT USER ---" in result
        assert "Alice" in result
        assert "Bob" not in result
        assert "Verified: yes" in result
        mock_enrich.assert_called_once_with(identity)

    def test_identity_enrichment_appears_in_block(self) -> None:
        """Enrichment (company/job title) is folded into the CURRENT USER block."""
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        identity = IdentityContext(
            tenant_id="t-alpha",
            channel="webchat",
            identifier="acct-1",
            verified=True,
            display_name="Alice",
            role="owner",
        )
        enriched = EnrichedIdentity(company="Acme", job_title="Ops Lead")
        try:
            with (
                patch(BLOCK_PATCH, return_value=None),
                patch("robothor.engine.warmup.enrich_identity", return_value=enriched),
            ):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello",
                    include_blocks=False,
                    identity=identity,
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "Acme" in result
        assert "Ops Lead" in result

    def test_no_identity_falls_back_to_sender_name(self) -> None:
        """identity=None preserves the legacy sender_name behavior."""
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with patch(BLOCK_PATCH, return_value=None):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello",
                    include_blocks=False,
                    sender_name="Alice",
                    identity=None,
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "CURRENT USER" in result
        assert "You are speaking with Alice" in result

    def test_identity_display_name_excluded_from_entity_context(self) -> None:
        """The entity-context exclusion set uses identity.display_name, not
        sender_name, when identity is present."""
        identity = IdentityContext(
            tenant_id="t-alpha",
            channel="webchat",
            identifier="acct-1",
            verified=True,
            display_name="Alice",
            role="owner",
        )
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                patch(BLOCK_PATCH, return_value=None),
                patch("robothor.engine.warmup.enrich_identity", return_value=None),
                patch(
                    "robothor.engine.warmup._build_entity_context", return_value=""
                ) as mock_entity_ctx,
            ):
                build_interactive_preamble(
                    "main",
                    user_message="Tell Alice about the project",
                    include_blocks=False,
                    identity=identity,
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert mock_entity_ctx.call_args.kwargs["exclude_names"] == {"Alice"}


class TestInteractivePanoramicSections:
    """Phase 4 — main's interactive warmup gets two new sections:
    open task queue (grouped by agent) + recent fleet deliveries.
    So main can answer 'what's going on?' without spinning tool calls.
    """

    def test_open_tasks_section_injected_for_main(self):
        """Main on Telegram gets an OPEN TASKS summary in its preamble."""
        from robothor.engine.warmup import build_interactive_preamble

        mock_tasks = [
            {
                "id": "t1",
                "title": "Reply to Kait",
                "status": "TODO",
                "assigned_to_agent": "main",
                "objective": "close pricing thread",
                "updated_at": None,
            },
            {
                "id": "t2",
                "title": "Deploy fix",
                "status": "IN_PROGRESS",
                "assigned_to_agent": "devops-manager",
                "objective": None,
                "updated_at": None,
            },
        ]

        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                patch(BLOCK_PATCH, return_value=None),
                patch("robothor.crm.dal.list_tasks", return_value=mock_tasks),
                patch("robothor.engine.warmup._recent_fleet_surfaces", return_value=""),
            ):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello",
                    include_blocks=False,
                    sender_name="",
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "OPEN TASKS" in result
        assert "Reply to Kait" in result
        assert "Deploy fix" in result
        # Grouped by assigned agent for drill-down
        assert "main" in result.lower() or "devops-manager" in result.lower()

    def test_open_tasks_section_skipped_for_non_main(self):
        """Other agents don't get the fleet-wide task summary."""
        from robothor.engine.warmup import build_interactive_preamble

        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                patch(BLOCK_PATCH, return_value=None),
                patch("robothor.crm.dal.list_tasks") as mock_list,
            ):
                result = build_interactive_preamble(
                    "email-classifier",  # not main
                    user_message="hello",
                    include_blocks=False,
                    sender_name="",
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "OPEN TASKS" not in result
        mock_list.assert_not_called()

    def test_fleet_surfaces_section_injected_for_main(self):
        """Main on Telegram gets a RECENT FLEET SURFACES snippet."""
        from robothor.engine.warmup import build_interactive_preamble

        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                patch(BLOCK_PATCH, return_value=None),
                patch("robothor.crm.dal.list_tasks", return_value=[]),
                patch(
                    "robothor.engine.warmup._recent_fleet_surfaces",
                    return_value=(
                        "--- RECENT FLEET SURFACES (last 6h) ---\n"
                        "[@devops-manager 14:02] weekly report clean"
                    ),
                ),
            ):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello",
                    include_blocks=False,
                    sender_name="",
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "RECENT FLEET SURFACES" in result
        assert "devops-manager" in result

    def test_empty_queue_shows_nothing_open(self):
        """When list_tasks returns nothing, the section still renders with a
        'nothing open' line so main can confidently answer the operator."""
        from robothor.engine.warmup import build_interactive_preamble

        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                patch(BLOCK_PATCH, return_value=None),
                patch("robothor.crm.dal.list_tasks", return_value=[]),
                patch("robothor.engine.warmup._recent_fleet_surfaces", return_value=""),
            ):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello",
                    include_blocks=False,
                    sender_name="",
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "OPEN TASKS" in result
        assert "Nothing open" in result or "nothing open" in result.lower()


class TestWarmupDataScoping:
    """Fix 1 (final-review, Unified Identity Context): warmup prompt assembly
    must respect ``ROBOTHOR_DATA_SCOPING`` the same way tool handlers do
    (``robothor.identity.scope``) — a restricted identity's FIRST message
    (no prior tool call, no filter) must not pre-load operator-private
    memory blocks or another person's entity facts into the prompt.
    """

    RESTRICTED = IdentityContext(
        tenant_id="tenant-a",
        channel="webchat",
        identifier="user-1",
        verified=True,
        display_name="Bob",
        role="member",
        person_id="person-1",
    )
    PRIVILEGED = IdentityContext(
        tenant_id="tenant-a",
        channel="webchat",
        identifier="user-owner",
        verified=True,
        display_name="Owner",
        role="owner",
        person_id="person-owner",
    )

    @staticmethod
    def _mode_env(mode: str):
        import os

        return patch.dict(os.environ, {"ROBOTHOR_DATA_SCOPING": mode}, clear=False)

    @staticmethod
    def _block_side_effect(name: str, tenant_id: str = "default") -> dict | None:
        return {"content": f"OPERATOR-SECRET-{name.upper()}", "last_written_at": None}

    # ── Memory blocks ──────────────────────────────────────────────

    def test_enforce_restricted_identity_drops_operator_blocks(self) -> None:
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                self._mode_env("enforce"),
                patch(BLOCK_PATCH, side_effect=self._block_side_effect),
                patch("robothor.engine.warmup.enrich_identity", return_value=None),
            ):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello there",
                    include_blocks=True,
                    identity=self.RESTRICTED,
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        # The agent's own persona is not operator-private — it stays.
        assert "OPERATOR-SECRET-PERSONA" in result
        # user_profile / user_model / working_context describe the OPERATOR —
        # a restricted identity under enforce must never see them.
        assert "OPERATOR-SECRET-USER_PROFILE" not in result
        assert "OPERATOR-SECRET-USER_MODEL" not in result
        assert "OPERATOR-SECRET-WORKING_CONTEXT" not in result

    def test_enforce_privileged_identity_keeps_operator_blocks(self) -> None:
        """Owner/admin/service identities are unaffected — byte-identical."""
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                self._mode_env("enforce"),
                patch(BLOCK_PATCH, side_effect=self._block_side_effect),
                patch("robothor.engine.warmup.enrich_identity", return_value=None),
            ):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello there",
                    include_blocks=True,
                    identity=self.PRIVILEGED,
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "OPERATOR-SECRET-USER_PROFILE" in result
        assert "OPERATOR-SECRET-USER_MODEL" in result
        assert "OPERATOR-SECRET-WORKING_CONTEXT" in result

    def test_off_mode_restricted_identity_keeps_operator_blocks(self) -> None:
        """Flag off: byte-identical to pre-Task-5 behavior, even restricted."""
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                self._mode_env("off"),
                patch(BLOCK_PATCH, side_effect=self._block_side_effect),
                patch("robothor.engine.warmup.enrich_identity", return_value=None),
            ):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello there",
                    include_blocks=True,
                    identity=self.RESTRICTED,
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "OPERATOR-SECRET-USER_PROFILE" in result
        assert "OPERATOR-SECRET-USER_MODEL" in result
        assert "OPERATOR-SECRET-WORKING_CONTEXT" in result

    def test_no_identity_keeps_operator_blocks(self) -> None:
        """identity=None (system/cron path) is unaffected regardless of mode."""
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                self._mode_env("enforce"),
                patch(BLOCK_PATCH, side_effect=self._block_side_effect),
            ):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello there",
                    include_blocks=True,
                    identity=None,
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        assert "OPERATOR-SECRET-USER_PROFILE" in result

    def test_observe_mode_keeps_output_but_logs_would_drop(self, caplog) -> None:
        import logging

        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        caplog.set_level(logging.INFO, logger="robothor.identity.scope")
        try:
            with (
                self._mode_env("observe"),
                patch(BLOCK_PATCH, side_effect=self._block_side_effect),
                patch("robothor.engine.warmup.enrich_identity", return_value=None),
            ):
                result = build_interactive_preamble(
                    "main",
                    user_message="hello there",
                    include_blocks=True,
                    identity=self.RESTRICTED,
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        # Observe mode never changes output.
        assert "OPERATOR-SECRET-USER_PROFILE" in result
        assert "OPERATOR-SECRET-USER_MODEL" in result
        assert "OPERATOR-SECRET-WORKING_CONTEXT" in result
        # ...but it must log what enforce would have dropped.
        msgs = [r.getMessage() for r in caplog.records]
        assert any("would_drop" in m and "warmup:memory_blocks" in m for m in msgs)

    # ── Entity context SQL ─────────────────────────────────────────

    def test_enforce_restricted_identity_scopes_entity_sql(self) -> None:
        from robothor.engine.warmup import _build_entity_context
        from robothor.identity.scope import scope_for_query

        scope = scope_for_query("enforce", self.RESTRICTED)
        assert scope is not None
        assert scope.restricted is True

        with patch("robothor.db.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = []

            _build_entity_context("Tell Carol about the project", scope=scope)

            sql, params = mock_cursor.execute.call_args[0]
            assert "person_id" in sql
            assert scope.person_id in params

    def test_off_mode_entity_sql_unscoped(self) -> None:
        from robothor.engine.warmup import _build_entity_context

        with patch("robothor.db.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = []

            _build_entity_context("Tell Carol about the project", scope=None)

            sql, _params = mock_cursor.execute.call_args[0]
            assert "person_id = %s OR person_id IS NULL" not in sql

    def test_observe_mode_entity_sql_unscoped_but_logs_would_drop(self, caplog) -> None:
        import logging

        from robothor.engine.warmup import _build_entity_context
        from robothor.identity.scope import observe_scope

        caplog.set_level(logging.INFO, logger="robothor.identity.scope")
        obs = observe_scope("observe", self.RESTRICTED)
        assert obs is not None

        with patch("robothor.db.get_connection") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value.cursor.return_value
            mock_cursor.fetchall.return_value = [
                {"fact_text": "own fact", "category": "x", "importance_score": 0.9,
                 "person_id": "person-1"},
                {"fact_text": "someone else's fact", "category": "x", "importance_score": 0.9,
                 "person_id": "person-9"},
            ]  # fmt: skip

            result = _build_entity_context(
                "Tell Carol about the project",
                scope=None,
                observe_scope_obj=obs,
            )

        # Observe mode must not filter the actual output.
        assert "own fact" in result
        assert "someone else's fact" in result
        msgs = [r.getMessage() for r in caplog.records]
        assert any("would_drop" in m and "warmup:entity_context" in m for m in msgs)

    def test_full_preamble_enforce_threads_scope_into_entity_context(self) -> None:
        """End-to-end: build_interactive_preamble under enforce actually
        passes the restricted scope down into _build_entity_context — this
        is the exact leak path (entity mention on the FIRST message, no
        prior tool call to filter through)."""
        saved = _CONTEXT_HOOKS.copy()
        _CONTEXT_HOOKS.clear()
        try:
            with (
                self._mode_env("enforce"),
                patch(BLOCK_PATCH, return_value=None),
                patch("robothor.engine.warmup.enrich_identity", return_value=None),
                patch(
                    "robothor.engine.warmup._build_entity_context", return_value=""
                ) as mock_entity_ctx,
            ):
                build_interactive_preamble(
                    "main",
                    user_message="Tell Carol about the project",
                    include_blocks=False,
                    identity=self.RESTRICTED,
                )
        finally:
            _CONTEXT_HOOKS[:] = saved

        passed_scope = mock_entity_ctx.call_args.kwargs.get("scope")
        assert passed_scope is not None
        assert passed_scope.restricted is True
        assert passed_scope.person_id == "person-1"
