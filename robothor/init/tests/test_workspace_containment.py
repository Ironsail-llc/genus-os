"""`genus init --workspace X` must touch X and nothing else. Ever.

This module exists because it did not hold. `AgentsStep.apply` called
`cli.agent.install_preset`, which resolved its own workspace from
`ROBOTHOR_WORKSPACE` or `~/robothor` and never saw the `--workspace` the
operator passed — so a run aimed at a temporary directory overwrote ten agent
manifests in a REAL instance with their template versions, and the operator's
main agent ran on a placeholder model chain nobody had chosen. The same shape
of bug was in `create_workspace`, whose template-hash snapshot was saved
through a helper that also resolves the workspace from the environment.

Two rules, both asserted here against the REAL step objects rather than fakes,
because a fake installer is exactly what hid this the first time:

1. A step writes under `ctx.workspace` and nowhere else. Not the environment,
   not `Path.home()`.
2. `--dry-run` never calls `apply()` at all.

The one file that is legitimately outside the workspace is `owner.yaml`: the
operator identity belongs to the account, not the workspace (root `CLAUDE.md`
rule 12). It is redirected here with `ROBOTHOR_OWNER_CONFIG`, which is what the
loader honours, and the assertion below is about the workspace tree.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from robothor.init.context import InitContext
from robothor.init.plan import InitPlan, run_plan
from robothor.init.substrates.local import LocalSubstrate

SENTINEL = "# DO NOT TOUCH: this manifest belongs to another instance\nagent_id: main\n"


def _fingerprint(root: Path) -> dict[str, str]:
    """Every file under ``root``, by relative path and content digest."""
    if not root.exists():
        return {}
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            found[str(path.relative_to(root))] = digest
    return found


@pytest.fixture
def other_instance(tmp_path, monkeypatch):
    """A second workspace, seeded as if a real instance lived there.

    ``ROBOTHOR_WORKSPACE`` and ``HOME`` both point at it, which is the state
    that turned a `--workspace /tmp/...` run into a live-instance edit.
    """
    root = tmp_path / "other-instance"
    (root / "docs" / "agents").mkdir(parents=True)
    (root / "docs" / "agents" / "main.yaml").write_text(SENTINEL, encoding="utf-8")
    (root / "brain").mkdir()
    (root / "brain" / "MAIN.md").write_text("# another instance's agent\n", encoding="utf-8")

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(root))
    monkeypatch.setenv("HOME", str(root))
    # Identity is account-scoped by design, so point it somewhere throwaway
    # rather than asserting it never moves.
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(tmp_path / "identity" / "owner.yaml"))
    from robothor.settings import reset_settings

    reset_settings()
    yield root
    reset_settings()


@pytest.fixture
def _no_doctor(monkeypatch):
    """The verify step's runner, stubbed: it is read-only and slow."""
    from robothor.doctor.runner import DoctorReport

    monkeypatch.setattr("robothor.doctor.runner.run_sync", lambda *a, **k: DoctorReport(results=[]))


def _ctx(workspace: Path, **kwargs: Any) -> InitContext:
    answers = {
        "skip_db": True,
        "skip_models": True,
        "provider_id": "openrouter",
        "provider_model": "openrouter/openai/gpt-5.4",
        "owner_name": "Alice Example",
        "owner_email": "alice@example.com",
        "preset": "minimal",
    }
    answers.update(kwargs.pop("answers", {}))
    kwargs.setdefault("yes", True)
    kwargs.setdefault("offline", True)
    return InitContext(workspace=workspace, answers=answers, **kwargs)


@pytest.mark.usefixtures("_no_doctor")
class TestARealRunStaysInsideItsWorkspace:
    def test_the_other_instance_is_byte_identical_afterwards(self, tmp_path, other_instance):
        target = tmp_path / "target"
        before = _fingerprint(other_instance)

        result = run_plan(_ctx(target), InitPlan("local", list(LocalSubstrate().steps())))

        assert _fingerprint(other_instance) == before, (
            "genus init wrote into the workspace named by ROBOTHOR_WORKSPACE/HOME "
            "instead of the one it was given"
        )
        assert result.exit_code == 0

    def test_the_manifest_of_another_instance_is_untouched(self, tmp_path, other_instance):
        target = tmp_path / "target"
        run_plan(_ctx(target), InitPlan("local", list(LocalSubstrate().steps())))

        assert (other_instance / "docs" / "agents" / "main.yaml").read_text() == SENTINEL

    def test_the_agents_land_in_the_chosen_workspace(self, tmp_path, other_instance):
        target = tmp_path / "target"
        run_plan(_ctx(target), InitPlan("local", list(LocalSubstrate().steps())))

        installed = sorted(p.name for p in (target / "docs" / "agents").glob("*.yaml"))
        assert installed, "the preset installed no manifests into the target workspace"

    def test_the_config_and_the_state_file_are_in_the_chosen_workspace(
        self, tmp_path, other_instance
    ):
        target = tmp_path / "target"
        run_plan(_ctx(target), InitPlan("local", list(LocalSubstrate().steps())))

        assert (target / ".robothor" / "config.yaml").exists()
        assert (target / ".robothor" / "init_state.yaml").exists()
        assert not (other_instance / ".robothor").exists()


@pytest.mark.usefixtures("_no_doctor")
class TestDryRunAppliesNothingAnywhere:
    def test_neither_workspace_changes(self, tmp_path, other_instance):
        target = tmp_path / "target"
        before = _fingerprint(other_instance)

        result = run_plan(
            _ctx(target, dry_run=True), InitPlan("local", list(LocalSubstrate().steps()))
        )

        assert _fingerprint(other_instance) == before
        assert _fingerprint(target) == {}
        assert result.exit_code == 0
        assert {row.status for row in result.steps} <= {"planned", "skipped"}

    def test_apply_is_never_called_in_a_dry_run(self, tmp_path):
        """Not "wrote nothing" — never entered. A step that reaches a helper
        which resolves its own paths has already escaped the workspace."""
        from robothor.init.steps import BaseStep, CheckResult

        class _MustNotApply(BaseStep):
            id = "explode"
            title = "Explode"

            def check(self, ctx: InitContext) -> CheckResult:
                return CheckResult(True, detail="would do something irreversible")

            def apply(self, ctx: InitContext) -> None:
                raise AssertionError("apply() ran during a dry run")

        ctx = _ctx(tmp_path / "target", dry_run=True)
        result = run_plan(ctx, InitPlan("local", [_MustNotApply()]))

        assert result.steps[0].status == "planned"


class TestTheConftestGuardIsOn:
    """The fixture is autouse, so nothing here opts in. That is the point.

    These assert the guard is ACTIVE rather than merely present: a redirect
    nobody checks is a comment, and the incident happened in a package whose
    one test fixture pinned HOME and not ROBOTHOR_WORKSPACE.
    """

    def test_the_environment_workspace_is_a_temporary_directory(self):
        from tests.conftest_workspace_containment import assert_contained

        assert_contained()

    def test_home_is_redirected_too(self):
        import os

        assert "contained-workspace" in os.environ["HOME"]

    def test_the_owner_config_is_redirected(self):
        import os

        assert "contained-workspace" in os.environ["ROBOTHOR_OWNER_CONFIG"]

    def test_a_helper_that_resolves_its_own_workspace_lands_in_the_sentinel_tree(
        self, env_workspace
    ):
        """What a careless caller does, and where it ends up.

        `default_workspace_root` is the resolver the outage went through. Here
        it answers with the throwaway directory, so the write the sentinel check
        then catches is a test's mistake rather than an operator's loss.
        """
        from robothor.templates.safety import default_workspace_root

        assert default_workspace_root().resolve() == env_workspace.resolve()

    def test_the_sentinel_starts_intact(self, env_workspace):
        from tests.conftest_workspace_containment import SENTINEL_BODY, SENTINEL_RELATIVE

        assert (env_workspace / SENTINEL_RELATIVE).read_text() == SENTINEL_BODY

    def test_a_real_preset_install_with_no_workspace_hits_the_throwaway(self, env_workspace):
        """The exact call that caused the outage, and where it goes now.

        `install_preset` with no `workspace=` still resolves the environment --
        that fallback is the CLI's -- so this writes the sentinel tree. The
        conftest's post-test check would fail on it, which is why the file is
        restored here: the assertion under test is WHERE it landed.
        """
        from tests.conftest_workspace_containment import SENTINEL_BODY, SENTINEL_RELATIVE

        sentinel = env_workspace / SENTINEL_RELATIVE
        try:
            from robothor.cli.agent import install_preset

            install_preset("minimal", auto_yes=True)
            landed = sorted(p.name for p in (env_workspace / "docs" / "agents").glob("*.yaml"))
            assert landed, "the install went somewhere other than the contained workspace"
            assert "main.yaml" in landed
        finally:
            sentinel.write_text(SENTINEL_BODY, encoding="utf-8")


class TestNoStepResolvesAWorkspaceOfItsOwn:
    """A tripwire, cheaper than the end-to-end test above and earlier.

    The end-to-end test catches an escape in the steps as they are today; this
    catches the next one being written, at the moment it is written.
    """

    FORBIDDEN = ('os.environ.get("ROBOTHOR_WORKSPACE"', "Path.home()", "os.environ[")

    def test_the_package_never_derives_a_workspace_from_the_environment(self):
        package = Path(__file__).resolve().parent.parent
        offenders: list[str] = []
        for module in sorted(package.rglob("*.py")):
            if "tests" in module.parts:
                continue
            for number, line in enumerate(module.read_text().splitlines(), start=1):
                code = line.split("#", 1)[0]
                if any(token in code for token in self.FORBIDDEN):
                    offenders.append(f"{module.relative_to(package)}:{number}: {line.strip()}")

        assert not offenders, (
            "a module in robothor/init/ resolves a path from the environment. "
            "Every write goes under ctx.workspace, which is what --workspace "
            "set:\n  " + "\n  ".join(offenders)
        )


class TestTheInstallerIsToldWhereToWrite:
    def test_the_agents_step_passes_its_context_workspace(self, tmp_path):
        from robothor.init.steps import AgentsStep

        seen: dict[str, Any] = {}

        def fake_install(preset: str, **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs, preset=preset)
            return {
                "unknown_preset": False,
                "available": ["minimal"],
                "requested": 1,
                "installed": ["main"],
                "failed": {},
                "missing": [],
            }

        workspace = tmp_path / "target"
        AgentsStep(installer=fake_install).apply(_ctx(workspace))

        assert seen["workspace"] == workspace

    def test_install_preset_forwards_the_workspace_to_the_installer(self, tmp_path, monkeypatch):
        """The parameter has to reach `templates.installer.install`, not just
        exist on the signature."""
        from robothor.cli import agent as agent_cli

        seen: dict[str, Any] = {}

        class _Catalog:
            presets = {"minimal": ["main"]}

            def get_preset_agents(self, preset: str) -> list[str]:
                return ["main"]

            def find_template(self, agent_id: str) -> Path:
                return tmp_path / "bundle"

        def fake_install(template_path: Any, **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {}

        monkeypatch.setattr("robothor.templates.catalog.Catalog", _Catalog)
        monkeypatch.setattr("robothor.templates.installer.install", fake_install)

        workspace = tmp_path / "target"
        agent_cli.install_preset("minimal", workspace=workspace)

        assert seen["repo_root"] == workspace
        assert seen["instance_dir"] == workspace / ".robothor"

    def test_create_workspace_saves_its_state_under_the_given_path(self, tmp_path, monkeypatch):
        """`create_workspace` snapshotted template hashes through a helper that
        resolves the workspace from the environment, so a scaffold created at X
        wrote a state file into Y."""
        from robothor.setup import create_workspace

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(elsewhere))
        monkeypatch.setenv("HOME", str(elsewhere))

        target = tmp_path / "target"
        create_workspace(target)

        assert not (elsewhere / ".robothor").exists()
        assert (target / ".robothor" / "migrations_applied.yaml").exists()
