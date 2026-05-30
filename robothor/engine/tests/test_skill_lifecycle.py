"""Tests for skill time-retirement (self-improvement Phase 3).

The anti-bloat guardrail: autonomously-accreted skills age out of the prompt
catalog so accretion can't degrade the agent by stuffing every prompt with
stale one-offs. Pinned and operator-authored skills are never retired.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from robothor.engine.skills import (
    apply_skill_lifecycle,
    build_skill_catalog,
    compute_skill_state,
    load_skills,
)

NOW = datetime(2026, 5, 30, tzinfo=UTC)


def _meta(**kw):
    base = {"is_agent_created": True, "created_at": NOW.isoformat(), "last_used": None}
    base.update(kw)
    return base


class TestComputeSkillState:
    def test_recent_agent_skill_is_active(self):
        m = _meta(created_at=(NOW - timedelta(days=5)).isoformat())
        assert compute_skill_state(m, NOW) == "active"

    def test_unused_30d_is_stale(self):
        m = _meta(created_at=(NOW - timedelta(days=45)).isoformat())
        assert compute_skill_state(m, NOW) == "stale"

    def test_unused_90d_is_archived(self):
        m = _meta(created_at=(NOW - timedelta(days=120)).isoformat())
        assert compute_skill_state(m, NOW) == "archived"

    def test_recent_use_reactivates(self):
        m = _meta(
            created_at=(NOW - timedelta(days=200)).isoformat(),
            last_used=(NOW - timedelta(days=2)).isoformat(),
        )
        assert compute_skill_state(m, NOW) == "active"

    def test_pinned_never_retired(self):
        m = _meta(created_at=(NOW - timedelta(days=300)).isoformat(), pinned=True)
        assert compute_skill_state(m, NOW) == "active"

    def test_operator_authored_never_retired(self):
        m = _meta(created_at=(NOW - timedelta(days=300)).isoformat(), is_agent_created=False)
        assert compute_skill_state(m, NOW) == "active"

    def test_auto_generated_without_is_agent_created_still_retires(self):
        """BUG-2: existing skills stamp auto_generated, not is_agent_created.
        Time-retirement must treat auto_generated as agent-made or it's inert
        for the entire existing corpus."""
        m = {"auto_generated": True, "created_at": (NOW - timedelta(days=200)).isoformat()}
        assert compute_skill_state(m, NOW) == "archived"

    def test_no_meta_is_active(self):
        assert compute_skill_state(None, NOW) == "active"

    def test_unparseable_dates_are_active(self):
        assert compute_skill_state(_meta(created_at="not-a-date", last_used=None), NOW) == "active"


def _make_skill(base, name, *, body="do a thing", **meta_kw):
    d = base / "agents" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n")
    (d / "meta.json").write_text(json.dumps(_meta(**meta_kw)))


class TestCatalogFilterAndLifecycle:
    def test_archived_agent_skill_excluded_from_catalog(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        _make_skill(tmp_path, "fresh-skill", created_at=(NOW - timedelta(days=3)).isoformat())
        _make_skill(tmp_path, "ancient-skill", created_at=(NOW - timedelta(days=200)).isoformat())
        # bust the mtime cache
        import robothor.engine.skills as skills_mod

        skills_mod._skills_cache = None
        catalog = build_skill_catalog(load_skills())
        assert "fresh-skill" in catalog
        assert "ancient-skill" not in catalog

    def test_pinned_ancient_skill_stays_in_catalog(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        _make_skill(
            tmp_path, "pinned-old", created_at=(NOW - timedelta(days=300)).isoformat(), pinned=True
        )
        import robothor.engine.skills as skills_mod

        skills_mod._skills_cache = None
        assert "pinned-old" in build_skill_catalog(load_skills())

    def test_apply_lifecycle_persists_transitions(self, tmp_path):
        base = tmp_path / "agents" / "skills"
        _make_skill(tmp_path, "going-stale", created_at=(NOW - timedelta(days=45)).isoformat())
        _make_skill(tmp_path, "going-archived", created_at=(NOW - timedelta(days=200)).isoformat())
        result = apply_skill_lifecycle(base=base, now=NOW)
        assert "going-stale" in result["to_stale"]
        assert "going-archived" in result["to_archived"]
        persisted = json.loads((base / "going-archived" / "meta.json").read_text())
        assert persisted["state"] == "archived"
