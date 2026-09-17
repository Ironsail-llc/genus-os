"""What is an agent actually reading when it invokes a bundled skill?

An instance skill of the same name shadows the one the platform ships. That
is deliberate -- an instance must be able to correct a skill it was given --
but before this check nothing on the box said it had happened: the tracked
file is untouched, so the checkout is clean, and the agent silently reads a
different procedure from the one an operator would open.

``skills.shadowed`` names every such pair. It never fails: shadowing is a
supported thing to do. ``skills.instance_dir`` is the one with teeth -- an
instance skills directory pointed back inside the platform tree recreates
exactly the leak this whole boundary exists to stop.
"""

from __future__ import annotations

import pytest

from robothor.doctor.checks.skills import CHECKS


def _check(check_id: str):
    found = [check for check in CHECKS if check.id == check_id]
    assert found, f"{check_id} is not registered"
    return found[0]


class _Ctx:
    """The doctor context these checks use: settings and blocking work."""

    def __init__(self, settings):
        self.settings = settings

    async def run_blocking(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    from robothor.settings import get_settings, reset_settings

    reset_settings()
    import robothor.engine.skills as skills_mod

    skills_mod._skills_cache = None
    skills_mod._meta_cache.clear()
    skills_mod._state_cache.clear()
    (tmp_path / "agents" / "skills").mkdir(parents=True)
    yield _Ctx(get_settings())
    skills_mod._skills_cache = None
    reset_settings()


def _skill(base, name: str) -> None:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}\n---\n\nbody\n")


class TestShadowedCheck:
    def test_it_is_a_built_in(self):
        from robothor.doctor.registry import builtin_ids

        assert "skills.shadowed" in builtin_ids()

    @pytest.mark.asyncio
    async def test_passes_when_nothing_is_shadowed(self, ctx, tmp_path):
        _skill(tmp_path / "agents" / "skills", "escalate")
        _skill(tmp_path / "brain" / "skills", "own-one")

        result = await _check("skills.shadowed").run(ctx)
        assert result.status == "pass"
        assert "escalate" not in result.detail

    @pytest.mark.asyncio
    async def test_names_every_shadowed_skill(self, ctx, tmp_path):
        _skill(tmp_path / "agents" / "skills", "escalate")
        _skill(tmp_path / "agents" / "skills", "code-review")
        _skill(tmp_path / "brain" / "skills", "escalate")

        result = await _check("skills.shadowed").run(ctx)
        assert result.status == "pass", "shadowing is supported, not a failure"
        assert "escalate" in result.detail
        assert "code-review" not in result.detail

    @pytest.mark.asyncio
    async def test_a_missing_instance_tree_is_not_a_finding(self, ctx, tmp_path):
        _skill(tmp_path / "agents" / "skills", "escalate")
        result = await _check("skills.shadowed").run(ctx)
        assert result.status == "pass"


class TestInstanceDirCheck:
    def test_it_is_a_built_in(self):
        from robothor.doctor.registry import builtin_ids

        assert "skills.instance_dir" in builtin_ids()

    @pytest.mark.asyncio
    async def test_passes_on_the_default(self, ctx, tmp_path):
        result = await _check("skills.instance_dir").run(ctx)
        assert result.status == "pass"

    @pytest.mark.asyncio
    async def test_fails_when_it_is_pointed_back_into_the_platform_tree(
        self, ctx, tmp_path, monkeypatch
    ):
        from robothor.settings import get_settings, reset_settings

        monkeypatch.setenv(
            "ROBOTHOR_INSTANCE_SKILLS_DIR", str(tmp_path / "agents" / "skills" / "mine")
        )
        reset_settings()
        result = await _check("skills.instance_dir").run(_Ctx(get_settings()))
        assert result.status == "fail"
        assert "agents/skills" in result.detail

    @pytest.mark.asyncio
    async def test_fails_when_it_is_outside_the_workspace(self, ctx, tmp_path, monkeypatch):
        from robothor.settings import get_settings, reset_settings

        monkeypatch.setenv("ROBOTHOR_INSTANCE_SKILLS_DIR", str(tmp_path.parent / "elsewhere"))
        reset_settings()
        result = await _check("skills.instance_dir").run(_Ctx(get_settings()))
        assert result.status == "fail"
        assert "snapshot" in result.detail.lower() or "workspace" in result.detail.lower()


class TestInstanceDirMustBeIgnored:
    """Inside the workspace and outside agents/skills is not enough.

    `<workspace>/docs/skills` passes both of those and is tracked ground: the
    skills an agent writes would be one `add -A` from the platform repository,
    which is the leak this whole boundary exists to close. When the workspace
    is a checkout, the check asks the checkout.
    """

    @staticmethod
    def _repo(path):
        import subprocess

        subprocess.run(["git", "init", "-q", str(path)], check=True)
        return path

    @pytest.fixture
    def repo_workspace(self, tmp_path, monkeypatch):
        workspace = self._repo(tmp_path / "workspace")
        (workspace / "agents" / "skills").mkdir(parents=True)
        (workspace / ".gitignore").write_text("brain/skills/\n")
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
        from robothor.settings import reset_settings

        reset_settings()
        yield workspace
        reset_settings()

    @pytest.mark.asyncio
    async def test_the_default_is_ignored_and_passes(self, repo_workspace):
        from robothor.settings import get_settings

        result = await _check("skills.instance_dir").run(_Ctx(get_settings()))
        assert result.status == "pass"

    @pytest.mark.asyncio
    async def test_a_tracked_directory_fails(self, repo_workspace, monkeypatch):
        from robothor.settings import get_settings, reset_settings

        monkeypatch.setenv("ROBOTHOR_INSTANCE_SKILLS_DIR", str(repo_workspace / "docs" / "skills"))
        reset_settings()

        result = await _check("skills.instance_dir").run(_Ctx(get_settings()))
        assert result.status == "fail"
        assert "ignore" in result.detail.lower()
        assert "docs/skills" in result.detail

    @pytest.mark.asyncio
    async def test_a_workspace_that_is_not_a_checkout_is_not_a_finding(self, tmp_path, monkeypatch):
        """Most instances are not a checkout; that is not a reason to fail."""
        workspace = tmp_path / "plain"
        (workspace / "agents" / "skills").mkdir(parents=True)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
        monkeypatch.setenv("ROBOTHOR_INSTANCE_SKILLS_DIR", str(workspace / "docs" / "skills"))
        from robothor.settings import get_settings, reset_settings

        reset_settings()
        try:
            result = await _check("skills.instance_dir").run(_Ctx(get_settings()))
        finally:
            reset_settings()
        assert result.status == "pass"
