"""Tests for the state.json runtime sidecar.

meta.json is static, tracked metadata; all runtime telemetry (usage_count,
last_used) lives in a gitignored state.json sidecar, and lifecycle ``state``
is pure-derived (compute_skill_state) — never persisted. This is what keeps
tracked skill metadata byte-stable at runtime (no more skip-worktree masking).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from robothor.engine.skills import (
    create_skill_meta,
    create_skill_state,
    increment_usage,
    migrate_skill_runtime_state,
    read_skill_state,
    read_skill_view,
    write_skill_state,
)

NOW = datetime(2026, 8, 19, tzinfo=UTC)

_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class _FakeCtx:
    agent_id: str = "test-agent"
    tenant_id: str = "test-tenant"


def _patch_skills_dir(skills_dir: Path):
    import robothor.engine.skills as _mod

    return patch.object(_mod, "_skills_dir", return_value=skills_dir)


def _mk_skill(
    base: Path,
    name: str,
    meta: dict | None = None,
    state: dict | None = None,
) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\nbody\n")
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    if state is not None:
        (d / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    return d


@pytest.fixture(autouse=True)
def _reset_skills_cache():
    import robothor.engine.skills as _mod

    _mod._skills_cache = None
    yield
    _mod._skills_cache = None


# ─── Sidecar I/O ─────────────────────────────────────────────────────


class TestSidecarIO:
    def test_write_read_roundtrip(self, tmp_path: Path):
        _mk_skill(tmp_path, "sk")
        write_skill_state("sk", {"usage_count": 4, "last_used": NOW.isoformat()}, base=tmp_path)
        loaded = read_skill_state("sk", base=tmp_path)
        assert loaded == {"usage_count": 4, "last_used": NOW.isoformat()}

    def test_read_missing_returns_none(self, tmp_path: Path):
        assert read_skill_state("nope", base=tmp_path) is None

    def test_write_leaves_no_temp_file(self, tmp_path: Path):
        _mk_skill(tmp_path, "sk")
        write_skill_state("sk", create_skill_state(), base=tmp_path)
        names = {p.name for p in (tmp_path / "sk").iterdir()}
        assert names == {"SKILL.md", "state.json"}

    def test_failed_replace_preserves_previous_state(self, tmp_path: Path):
        """Atomicity: a crash mid-write must never corrupt the sidecar."""
        _mk_skill(tmp_path, "sk")
        write_skill_state("sk", {"usage_count": 7, "last_used": None}, base=tmp_path)
        with (
            patch.object(Path, "replace", side_effect=OSError("boom")),
            pytest.raises(OSError),
        ):
            write_skill_state("sk", {"usage_count": 8, "last_used": None}, base=tmp_path)
        loaded = read_skill_state("sk", base=tmp_path)
        assert loaded is not None
        assert loaded["usage_count"] == 7

    def test_create_skill_state_defaults(self):
        assert create_skill_state() == {"usage_count": 0, "last_used": None}

    def test_concurrent_writers_use_unique_tmp_names(self, tmp_path: Path):
        """Two processes writing the same sidecar must not rename each
        other's tmp file away (fixed tmp names raced to FileNotFoundError)."""
        import robothor.engine.skills as _mod

        _mk_skill(tmp_path, "sk")
        seen: list[str] = []
        real_mkstemp = _mod.tempfile.mkstemp

        def spy(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            seen.append(name)
            return fd, name

        with patch.object(_mod.tempfile, "mkstemp", side_effect=spy):
            write_skill_state("sk", {"usage_count": 1, "last_used": None}, base=tmp_path)
            write_skill_state("sk", {"usage_count": 2, "last_used": None}, base=tmp_path)
        assert len(seen) == 2 and seen[0] != seen[1]
        assert all(n.endswith(".json.tmp") for n in seen)

    def test_non_dict_state_json_is_ignored(self, tmp_path: Path):
        d = _mk_skill(tmp_path, "sk", meta={"auto_generated": True})
        (d / "state.json").write_text("[1, 2, 3]\n")
        assert read_skill_state("sk", base=tmp_path) is None
        view = read_skill_view("sk", base=tmp_path, now=NOW)
        assert view is not None
        assert view["usage_count"] == 0

    def test_non_dict_meta_json_is_ignored(self, tmp_path: Path):
        d = _mk_skill(tmp_path, "sk")
        (d / "meta.json").write_text("[1, 2, 3]\n")
        view = read_skill_view("sk", base=tmp_path, now=NOW)  # must not raise
        assert view is None

    def test_create_skill_meta_is_static_only(self):
        meta = create_skill_meta(created_by="main")
        for key in ("usage_count", "last_used", "state"):
            assert key not in meta, f"runtime key {key!r} must not live in meta.json"


# ─── increment_usage writes the sidecar, never meta.json ────────────


class TestIncrementUsage:
    def test_increments_sidecar_not_meta(self, tmp_path: Path):
        _mk_skill(tmp_path, "sk", meta=create_skill_meta(created_by="main"))
        before = (tmp_path / "sk" / "meta.json").read_bytes()

        increment_usage("sk", base=tmp_path)

        assert (tmp_path / "sk" / "meta.json").read_bytes() == before
        state = read_skill_state("sk", base=tmp_path)
        assert state is not None
        assert state["usage_count"] == 1
        assert state["last_used"] is not None

    def test_seeds_counter_from_legacy_meta_keys(self, tmp_path: Path):
        """Pre-migration meta.json may still carry the counter — don't lose it."""
        _mk_skill(tmp_path, "sk", meta={"auto_generated": True, "usage_count": 5})
        increment_usage("sk", base=tmp_path)
        state = read_skill_state("sk", base=tmp_path)
        assert state is not None
        assert state["usage_count"] == 6

    def test_noop_for_missing_skill(self, tmp_path: Path):
        increment_usage("no-such-skill", base=tmp_path)  # should not raise
        assert not (tmp_path / "no-such-skill").exists()

    def test_write_failure_does_not_raise(self, tmp_path: Path):
        """Counter is best-effort telemetry — an I/O race (e.g. a concurrent
        rename) must never break the skill invocation that triggered it."""
        import robothor.engine.skills as _mod

        _mk_skill(tmp_path, "sk", meta=create_skill_meta(created_by="main"))
        with patch.object(_mod, "write_skill_state", side_effect=FileNotFoundError("raced")):
            increment_usage("sk", base=tmp_path)  # must not raise


# ─── read_skill_view: the merged accessor ────────────────────────────


class TestReadSkillView:
    def test_none_when_no_files(self, tmp_path: Path):
        _mk_skill(tmp_path, "sk")
        assert read_skill_view("sk", base=tmp_path) is None

    def test_merges_static_and_runtime(self, tmp_path: Path):
        _mk_skill(
            tmp_path,
            "sk",
            meta={"auto_generated": True, "created_by": "main", "revision": 2},
            state={"usage_count": 9, "last_used": NOW.isoformat()},
        )
        view = read_skill_view("sk", base=tmp_path, now=NOW)
        assert view is not None
        assert view["created_by"] == "main"
        assert view["revision"] == 2
        assert view["usage_count"] == 9
        assert view["last_used"] == NOW.isoformat()

    def test_sidecar_wins_over_legacy_meta_keys(self, tmp_path: Path):
        _mk_skill(
            tmp_path,
            "sk",
            meta={"auto_generated": True, "usage_count": 3, "last_used": None},
            state={"usage_count": 9, "last_used": NOW.isoformat()},
        )
        view = read_skill_view("sk", base=tmp_path, now=NOW)
        assert view is not None
        assert view["usage_count"] == 9
        assert view["last_used"] == NOW.isoformat()

    def test_legacy_meta_runtime_keys_still_readable(self, tmp_path: Path):
        """Back-compat: pre-migration meta.json is the fallback source."""
        _mk_skill(tmp_path, "sk", meta={"auto_generated": True, "usage_count": 3})
        view = read_skill_view("sk", base=tmp_path, now=NOW)
        assert view is not None
        assert view["usage_count"] == 3

    def test_runtime_defaults(self, tmp_path: Path):
        _mk_skill(tmp_path, "sk", meta={"auto_generated": True})
        view = read_skill_view("sk", base=tmp_path, now=NOW)
        assert view is not None
        assert view["usage_count"] == 0
        assert view["last_used"] is None

    def test_state_is_derived_not_read_from_meta(self, tmp_path: Path):
        """A stale persisted 'state' in meta.json is ignored — always derived."""
        _mk_skill(
            tmp_path,
            "sk",
            meta={
                "auto_generated": True,
                "created_at": (NOW - timedelta(days=200)).isoformat(),
                "state": "active",
            },
        )
        view = read_skill_view("sk", base=tmp_path, now=NOW)
        assert view is not None
        assert view["state"] == "archived"

    def test_sidecar_last_used_reactivates(self, tmp_path: Path):
        _mk_skill(
            tmp_path,
            "sk",
            meta={
                "auto_generated": True,
                "created_at": (NOW - timedelta(days=200)).isoformat(),
            },
            state={"usage_count": 1, "last_used": (NOW - timedelta(days=2)).isoformat()},
        )
        view = read_skill_view("sk", base=tmp_path, now=NOW)
        assert view is not None
        assert view["state"] == "active"

    def test_view_from_sidecar_only(self, tmp_path: Path):
        """Hand-authored skills (no meta.json) can still carry usage telemetry."""
        _mk_skill(tmp_path, "sk", state={"usage_count": 2, "last_used": None})
        view = read_skill_view("sk", base=tmp_path, now=NOW)
        assert view is not None
        assert view["usage_count"] == 2
        assert view["state"] == "active"


# ─── Handlers never mutate meta.json ─────────────────────────────────


class TestHandlersDoNotMutateMeta:
    @pytest.mark.asyncio
    async def test_meta_json_is_not_mutated_by_invoke(self, tmp_path: Path):
        _mk_skill(tmp_path, "trackable", meta=create_skill_meta(created_by="test"))
        before = (tmp_path / "trackable" / "meta.json").read_bytes()

        from robothor.engine.tools.handlers.skills import _invoke_skill

        with _patch_skills_dir(tmp_path):
            result = await _invoke_skill({"name": "trackable"}, _FakeCtx())

        assert "content" in result
        assert (tmp_path / "trackable" / "meta.json").read_bytes() == before
        state = read_skill_state("trackable", base=tmp_path)
        assert state is not None
        assert state["usage_count"] == 1

    @pytest.mark.asyncio
    async def test_meta_json_is_not_mutated_by_skill_view(self, tmp_path: Path):
        _mk_skill(tmp_path, "viewable", meta=create_skill_meta(created_by="test"))
        before = (tmp_path / "viewable" / "meta.json").read_bytes()

        from robothor.engine.tools.handlers.skills import _skill_view

        with _patch_skills_dir(tmp_path):
            result = await _skill_view({"name": "viewable"}, _FakeCtx())

        assert result.get("error") is None
        assert (tmp_path / "viewable" / "meta.json").read_bytes() == before
        state = read_skill_state("viewable", base=tmp_path)
        assert state is not None
        assert state["usage_count"] == 1

    @pytest.mark.asyncio
    async def test_create_skill_writes_static_meta_and_fresh_sidecar(self, tmp_path: Path):
        from robothor.engine.tools.handlers.skills import _create_skill

        with _patch_skills_dir(tmp_path):
            result = await _create_skill(
                {"name": "brand-new", "description": "d", "content": "body"},
                _FakeCtx(),
            )

        assert result["created"] is True
        meta = json.loads((tmp_path / "brand-new" / "meta.json").read_text())
        for key in ("usage_count", "last_used", "state"):
            assert key not in meta
        assert read_skill_state("brand-new", base=tmp_path) == create_skill_state()

    @pytest.mark.asyncio
    async def test_update_skill_strips_legacy_runtime_keys(self, tmp_path: Path):
        """update_skill must not re-persist runtime keys into meta.json."""
        legacy = create_skill_meta(created_by="test")
        legacy.update({"usage_count": 4, "last_used": NOW.isoformat(), "state": "stale"})
        _mk_skill(tmp_path, "legacy-skill", meta=legacy)

        from robothor.engine.tools.handlers.skills import _update_skill

        with _patch_skills_dir(tmp_path):
            result = await _update_skill({"name": "legacy-skill", "content": "v2 body"}, _FakeCtx())

        assert result.get("updated") is True
        meta = json.loads((tmp_path / "legacy-skill" / "meta.json").read_text())
        for key in ("usage_count", "last_used", "state"):
            assert key not in meta
        assert meta["revision"] == 2


# ─── One-shot migration ──────────────────────────────────────────────


class TestMigration:
    def test_moves_runtime_keys_to_sidecar(self, tmp_path: Path):
        _mk_skill(
            tmp_path,
            "sk",
            meta={
                "auto_generated": True,
                "created_by": "auto-agent",
                "revision": 1,
                "usage_count": 3,
                "last_used": NOW.isoformat(),
                "state": "stale",
            },
        )
        result = migrate_skill_runtime_state(base=tmp_path)
        assert "sk" in result["migrated"]

        meta = json.loads((tmp_path / "sk" / "meta.json").read_text())
        for key in ("usage_count", "last_used", "state"):
            assert key not in meta
        assert meta["created_by"] == "auto-agent"

        state = read_skill_state("sk", base=tmp_path)
        assert state == {"usage_count": 3, "last_used": NOW.isoformat()}

    def test_idempotent_second_run_is_byte_identical(self, tmp_path: Path):
        _mk_skill(
            tmp_path,
            "sk",
            meta={"auto_generated": True, "usage_count": 3, "last_used": None},
        )
        migrate_skill_runtime_state(base=tmp_path)
        meta1 = (tmp_path / "sk" / "meta.json").read_bytes()
        state1 = (tmp_path / "sk" / "state.json").read_bytes()

        result = migrate_skill_runtime_state(base=tmp_path)
        assert result["migrated"] == []
        assert "sk" in result["unchanged"]
        assert (tmp_path / "sk" / "meta.json").read_bytes() == meta1
        assert (tmp_path / "sk" / "state.json").read_bytes() == state1

    def test_partial_runtime_keys(self, tmp_path: Path):
        _mk_skill(tmp_path, "sk", meta={"auto_generated": True, "state": "active"})
        result = migrate_skill_runtime_state(base=tmp_path)
        assert "sk" in result["migrated"]
        meta = json.loads((tmp_path / "sk" / "meta.json").read_text())
        assert "state" not in meta
        assert read_skill_state("sk", base=tmp_path) == create_skill_state()

    def test_existing_sidecar_wins(self, tmp_path: Path):
        _mk_skill(
            tmp_path,
            "sk",
            meta={"auto_generated": True, "usage_count": 3},
            state={"usage_count": 9, "last_used": NOW.isoformat()},
        )
        migrate_skill_runtime_state(base=tmp_path)
        state = read_skill_state("sk", base=tmp_path)
        assert state is not None
        assert state["usage_count"] == 9

    def test_static_only_meta_untouched(self, tmp_path: Path):
        _mk_skill(tmp_path, "sk", meta={"auto_generated": True, "revision": 1})
        before = (tmp_path / "sk" / "meta.json").read_bytes()
        result = migrate_skill_runtime_state(base=tmp_path)
        assert "sk" in result["unchanged"]
        assert (tmp_path / "sk" / "meta.json").read_bytes() == before

    def test_corrupt_meta_is_skipped(self, tmp_path: Path):
        d = _mk_skill(tmp_path, "bad")
        (d / "meta.json").write_text("{not json")
        _mk_skill(tmp_path, "good", meta={"auto_generated": True, "usage_count": 1})
        result = migrate_skill_runtime_state(base=tmp_path)
        assert "good" in result["migrated"]
        assert "bad" in result["errors"]

    def test_non_dict_meta_is_an_error(self, tmp_path: Path):
        d = _mk_skill(tmp_path, "listy")
        (d / "meta.json").write_text("[1, 2, 3]\n")
        result = migrate_skill_runtime_state(base=tmp_path)
        assert "listy" in result["errors"]
        assert (d / "meta.json").read_text() == "[1, 2, 3]\n"  # left untouched

    def test_cli_hook(self, tmp_path: Path, monkeypatch, capsys):
        import argparse

        skills = tmp_path / "agents" / "skills"
        _mk_skill(skills, "sk", meta={"auto_generated": True, "usage_count": 2})
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))

        from robothor.cli.skills import cmd_skills

        rc = cmd_skills(argparse.Namespace(skills_command="migrate-state"))
        assert rc == 0
        assert (skills / "sk" / "state.json").exists()
        meta = json.loads((skills / "sk" / "meta.json").read_text())
        assert "usage_count" not in meta


# ─── Repo hygiene ────────────────────────────────────────────────────


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=15,
    )


def _skip_unless_repo():
    if not (_REPO_ROOT / ".git").exists() or not (_REPO_ROOT / "agents" / "skills").is_dir():
        pytest.skip("not running from a git checkout with agents/skills")


class TestRepoHygiene:
    def test_state_sidecar_is_gitignored(self):
        _skip_unless_repo()
        probe = "agents/skills/some-skill/state.json"
        out = _git("check-ignore", "-q", probe)
        assert out.returncode == 0, f"{probe} must be gitignored"

    def test_no_skill_meta_is_skip_worktree(self):
        """Tracked skill files must not be masked with skip-worktree.

        (On the live box the 8 masked metas are unmasked in the one-shot
        migration window; in the repo this must always hold.)
        """
        _skip_unless_repo()
        out = _git("ls-files", "-v", "--", "agents/skills")
        if out.returncode != 0:
            pytest.skip("git ls-files unavailable")
        flagged = [ln for ln in out.stdout.splitlines() if ln.strip() and not ln.startswith("H ")]
        assert flagged == [], f"skip-worktree/assume-unchanged flags found: {flagged}"

    def test_no_state_json_tracked(self):
        _skip_unless_repo()
        out = _git("ls-files", "--", "agents/skills/*/state.json")
        assert out.stdout.strip() == "", f"state.json must never be tracked: {out.stdout}"
