"""The off / observe / enforce ladder, at the two points that load manifests.

A validator that can only be on or off does not get turned on: nobody flips a
switch that might refuse the fleet. `observe` is what makes `enforce` a
decision instead of a gamble — it logs and COUNTS what enforcement would have
refused, on the instance's own manifests, so the promotion rests on a number.

`enforce` has one job beyond refusing: the refusal must land in the
"broken, not deleted" bucket. On 2026-08-23 a manifest that would not parse was
dropped silently and the scheduler pruned the agent's schedules five minutes
later. A manifest refused by the schema is exactly as broken, and must reach
`reconcile` the same way — as a `ManifestFailure`, which makes the scan dirty,
which makes reconcile refuse to prune anything.
"""

from __future__ import annotations

import logging

import pytest

from robothor.engine import config, manifest_schema
from robothor.engine.config import (
    _load_manifest_classified,
    load_agent_config,
    load_manifest_dir,
)
from robothor.engine.manifest_schema import ManifestSchemaError

GOOD = """\
id: alice
name: Alice
description: A generic fixture agent
version: "2026-09-11"
department: operations
schedule:
  cron: "0 6 * * *"
"""

# `department: opperations` is a typo of a real enum value: valid YAML, loads
# fine today, and silently means nothing.
BROKEN = """\
id: bob
name: Bob
description: A generic fixture agent
version: "2026-09-11"
department: opperations
schedule:
  cron: "0 7 * * *"
"""


@pytest.fixture(autouse=True)
def _clean_counter():
    """Both the counter and the log-dedup set are process-global.

    The dedup set matters as much as the counter: without clearing it the
    second test to load the same broken agent sees no log line and passes or
    fails on the order pytest happened to pick.
    """
    manifest_schema.reset_would_reject()
    config._logged_schema_issues.clear()
    yield
    manifest_schema.reset_would_reject()
    config._logged_schema_issues.clear()


@pytest.fixture
def fleet(tmp_path):
    (tmp_path / "alice.yaml").write_text(GOOD)
    (tmp_path / "bob.yaml").write_text(BROKEN)
    return tmp_path


# ── The mode itself ─────────────────────────────────────────────────


class TestSchemaMode:
    def test_the_default_is_observe(self, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", raising=False)
        assert manifest_schema.schema_mode() == "observe"

    @pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
    def test_each_rung_is_read_from_the_environment(self, monkeypatch, mode):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", mode.upper())
        assert manifest_schema.schema_mode() == mode

    def test_an_unrecognised_value_degrades_to_observe(self, monkeypatch):
        """Never to `off` (silently stops reporting) and never to `enforce`
        (a typo in an env var would refuse the fleet)."""
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "strict")
        assert manifest_schema.schema_mode() == "observe"


# ── observe ─────────────────────────────────────────────────────────


class TestObserve:
    def test_the_agent_still_loads(self, fleet, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "observe")
        assert load_agent_config("bob", fleet) is not None

    def test_it_logs_the_agent_the_path_and_the_code(self, fleet, monkeypatch, caplog):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "observe")
        with caplog.at_level(logging.WARNING, logger="robothor.engine.config"):
            load_agent_config("bob", fleet)
        lines = [r.getMessage() for r in caplog.records if "would reject" in r.getMessage()]
        assert lines, f"observe logged nothing: {[r.getMessage() for r in caplog.records]}"
        joined = " ".join(lines)
        assert "bob" in joined
        assert "department" in joined
        assert "invalid_enum" in joined

    def test_it_counts_the_agent_it_would_have_rejected(self, fleet, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "observe")
        load_agent_config("bob", fleet)
        assert manifest_schema.manifest_schema_would_reject.get("bob") == 1

    def test_the_log_deduplicates_but_the_count_does_not(self, fleet, monkeypatch, caplog):
        """`load_agent_config` runs on every agent run.

        An undeduplicated warning would repeat the same finding thousands of
        times a day and get filtered — the noise failure this validator exists
        to avoid. The COUNTER must not dedup: "how often" is half the evidence
        a promotion to enforce rests on, and a deduplicated log cannot answer
        it.
        """
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "observe")
        with caplog.at_level(logging.WARNING, logger="robothor.engine.config"):
            for _ in range(3):
                load_agent_config("bob", fleet)
        lines = [r for r in caplog.records if "would reject" in r.getMessage()]
        assert len(lines) == 1
        assert manifest_schema.manifest_schema_would_reject["bob"] == 3

    def test_a_clean_manifest_is_not_counted(self, fleet, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "observe")
        load_agent_config("alice", fleet)
        assert "alice" not in manifest_schema.manifest_schema_would_reject

    def test_a_broken_manifest_is_still_a_live_agent_in_the_scan(self, fleet, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "observe")
        scan = load_manifest_dir(fleet)
        assert scan.clean
        assert {m["id"] for m in scan.manifests} == {"alice", "bob"}


# ── off ─────────────────────────────────────────────────────────────


class TestOff:
    def test_nothing_is_counted(self, fleet, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "off")
        load_agent_config("bob", fleet)
        assert manifest_schema.manifest_schema_would_reject == {}

    def test_nothing_is_logged(self, fleet, monkeypatch, caplog):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "off")
        with caplog.at_level(logging.WARNING, logger="robothor.engine.config"):
            load_agent_config("bob", fleet)
        assert not [r for r in caplog.records if "would reject" in r.getMessage()]

    def test_the_scan_stays_clean(self, fleet, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "off")
        assert load_manifest_dir(fleet).clean


# ── enforce ─────────────────────────────────────────────────────────


class TestEnforce:
    def test_load_agent_config_raises(self, fleet, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        with pytest.raises(ManifestSchemaError) as excinfo:
            load_agent_config("bob", fleet)
        assert excinfo.value.agent_id == "bob"
        assert [i.code for i in excinfo.value.issues] == ["invalid_enum"]

    def test_a_clean_manifest_still_loads(self, fleet, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        assert load_agent_config("alice", fleet) is not None

    def test_classified_load_reports_a_schema_error(self, fleet, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        data, failure, skipped = _load_manifest_classified(fleet / "bob.yaml")
        assert data is None
        assert failure is not None
        assert failure.error_type == "SchemaError"
        assert failure.filename == "bob.yaml"
        assert not skipped

    def test_the_failure_detail_names_the_path_and_code_only(self, fleet, monkeypatch):
        """This string reaches an operator page. Platform code must not carry
        instance values there (CLAUDE.md rules 1 and 2), so the detail says
        WHERE and WHAT KIND, never the offending value."""
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        _data, failure, _skipped = _load_manifest_classified(fleet / "bob.yaml")
        assert "department" in failure.detail
        assert "invalid_enum" in failure.detail
        assert "opperations" not in failure.detail

    def test_a_broken_manifest_makes_the_scan_dirty(self, fleet, monkeypatch):
        """The whole point: dirty scan -> reconcile refuses to prune."""
        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        scan = load_manifest_dir(fleet)
        assert not scan.clean
        assert [f.filename for f in scan.failures] == ["bob.yaml"]
        assert {m["id"] for m in scan.manifests} == {"alice"}


class TestBrokenIsNotDeleted:
    """The 2026-08-23 contract, extended to schema failures.

    A manifest refused by the schema must be indistinguishable, to reconcile,
    from a manifest that will not parse: BROKEN, therefore not prunable. The
    counter-case is the one that makes the test worth writing — with the
    manifest removed instead of broken, the same agent IS pruned.
    """

    def _scheduler(self, manifest_dir):
        from unittest.mock import MagicMock

        from robothor.engine.config import EngineConfig
        from robothor.engine.scheduler import CronScheduler

        config = EngineConfig(manifest_dir=manifest_dir, workspace=manifest_dir.parent)
        return CronScheduler(config, MagicMock())

    def test_enforce_leaves_the_broken_agents_schedule_alone(self, fleet, monkeypatch):
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        scheduler = self._scheduler(fleet)
        bob_job = MagicMock()
        bob_job.id = "bob"
        scheduler.scheduler = MagicMock()
        scheduler.scheduler.get_jobs.return_value = [bob_job]

        mock_delete = MagicMock(return_value=[])
        with patch("robothor.engine.scheduler.delete_stale_schedules", mock_delete):
            pruned = scheduler.reconcile_schedules()

        assert pruned == []
        mock_delete.assert_not_called()
        bob_job.remove.assert_not_called()

    def test_a_deleted_agent_is_still_pruned(self, fleet, monkeypatch):
        """The discriminator. Remove bob's manifest entirely and the same job
        goes — otherwise the test above would pass on a scheduler that simply
        never prunes."""
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("ROBOTHOR_MANIFEST_SCHEMA_MODE", "enforce")
        (fleet / "bob.yaml").unlink()
        scheduler = self._scheduler(fleet)
        bob_job = MagicMock()
        bob_job.id = "bob"
        scheduler.scheduler = MagicMock()
        scheduler.scheduler.get_jobs.return_value = [bob_job]

        with patch("robothor.engine.scheduler.delete_stale_schedules", return_value=[]):
            pruned = scheduler.reconcile_schedules()

        assert pruned == ["bob"]
        bob_job.remove.assert_called_once()
