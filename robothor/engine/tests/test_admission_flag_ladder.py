"""A refusal that leaves no row is indistinguishable from a control that never ran.

2026-08-27. FleetPool shipped with can_start/register_run/complete_run, twelve
tests, and a daemon that logged the cap it enforced -- with zero production
callers, for its entire existence. It was wired this session, and the wiring
immediately raised the harder question: how would anyone KNOW it fires?
Production showed peak concurrency 5 against a cap of 3 with zero deferrals,
and the only way to tell "never triggered" from "never called" was to read the
source. That is the same hole commit 189a5637 documented.

So admission runs on the standard ladder and writes evidence on the shadow
path too. In observe it computes the verdict, records that it WOULD have
deferred, and admits anyway -- which means the operator can promote to enforce
on real rows rather than on faith. Off is the default and preserves today's
behaviour exactly.

Everything still fails OPEN. A scheduler that refuses work because the evidence
INSERT failed would be the outage the control exists to prevent.
"""

import pytest

import robothor.engine.admission as adm


class FakePool:
    def __init__(self, allowed: bool):
        self._allowed = allowed
        self.asked = 0

    def can_start(self, agent_id, priority=None):
        self.asked += 1
        return self._allowed, "3/3 slots in use"


@pytest.fixture
def recorded(monkeypatch):
    """Capture evidence writes without touching the database."""
    rows = []
    monkeypatch.setattr(adm, "_record_deferral", lambda *a, **k: rows.append(a or k))
    return rows


@pytest.fixture
def refusing_pool(monkeypatch):
    pool = FakePool(allowed=False)
    monkeypatch.setattr(adm, "_pool", lambda: pool)
    return pool


def _mode(monkeypatch, value):
    monkeypatch.setattr(adm, "admission_mode", lambda: value)


class TestOffPreservesTodaysBehaviour:
    def test_off_admits_even_when_the_pool_would_refuse(self, monkeypatch, refusing_pool, recorded):
        _mode(monkeypatch, "off")
        assert adm.admit("crm-dedup", object()) is True

    def test_off_does_not_even_ask_the_pool(self, monkeypatch, refusing_pool, recorded):
        """Off must be free, not merely harmless."""
        _mode(monkeypatch, "off")
        adm.admit("crm-dedup", object())
        assert refusing_pool.asked == 0


class TestObserveRecordsWithoutActing:
    def test_observe_admits_the_run(self, monkeypatch, refusing_pool, recorded):
        _mode(monkeypatch, "observe")
        assert adm.admit("crm-dedup", object()) is True

    def test_observe_still_writes_the_evidence_row(self, monkeypatch, refusing_pool, recorded):
        """Zero rows in observe is how a control stays inert unnoticed."""
        _mode(monkeypatch, "observe")
        adm.admit("crm-dedup", object())
        assert len(recorded) == 1

    def test_an_admitted_run_writes_nothing(self, monkeypatch, recorded):
        monkeypatch.setattr(adm, "_pool", lambda: FakePool(allowed=True))
        _mode(monkeypatch, "observe")
        assert adm.admit("crm-dedup", object()) is True
        assert recorded == []


class TestEnforceActs:
    def test_enforce_refuses(self, monkeypatch, refusing_pool, recorded):
        _mode(monkeypatch, "enforce")
        assert adm.admit("crm-dedup", object()) is False

    def test_enforce_records_the_block(self, monkeypatch, refusing_pool, recorded):
        _mode(monkeypatch, "enforce")
        adm.admit("crm-dedup", object())
        assert len(recorded) == 1


class TestItStillFailsOpen:
    def test_a_missing_pool_admits(self, monkeypatch, recorded):
        monkeypatch.setattr(adm, "_pool", lambda: None)
        _mode(monkeypatch, "enforce")
        assert adm.admit("crm-dedup", object()) is True

    def test_a_raising_pool_admits(self, monkeypatch, recorded):
        class Boom:
            def can_start(self, *a, **k):
                raise RuntimeError("pool exploded")

        monkeypatch.setattr(adm, "_pool", lambda: Boom())
        _mode(monkeypatch, "enforce")
        assert adm.admit("crm-dedup", object()) is True

    def test_a_failing_evidence_write_never_changes_the_verdict(self, monkeypatch, refusing_pool):
        """The INSERT is telemetry. It must not be able to admit or refuse."""

        def boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(adm, "_record_deferral", boom)
        _mode(monkeypatch, "enforce")
        assert adm.admit("crm-dedup", object()) is False
        _mode(monkeypatch, "observe")
        assert adm.admit("crm-dedup", object()) is True


class TestTheFlagIsOperable:
    def test_the_mode_reads_the_standard_ladder(self, monkeypatch):
        from robothor.engine.feature_flags import execution_mode_admission_mode

        monkeypatch.delenv("ROBOTHOR_DISABLE_ALL_GUARDRAILS", raising=False)
        monkeypatch.delenv("ROBOTHOR_ADMISSION_ENABLED", raising=False)
        assert execution_mode_admission_mode() == "off"
        monkeypatch.setenv("ROBOTHOR_ADMISSION_ENABLED", "1")
        assert execution_mode_admission_mode() == "observe"
        monkeypatch.setenv("ROBOTHOR_ADMISSION_MODE", "enforce")
        assert execution_mode_admission_mode() == "enforce"

    def test_it_is_flippable_at_runtime_without_a_restart(self):
        """The point of a governed flag is promoting it DURING an outage."""
        from robothor.flags.store import GOVERNED_FLAGS

        assert "ROBOTHOR_ADMISSION_MODE" in GOVERNED_FLAGS

    def test_it_is_in_the_flag_manifest(self):
        import pathlib

        import yaml

        manifest = pathlib.Path(adm.__file__).parents[2] / "infra" / "flags.yaml"
        names = {f["name"] for f in yaml.safe_load(manifest.read_text())["flags"]}
        assert "ROBOTHOR_ADMISSION_MODE" in names
