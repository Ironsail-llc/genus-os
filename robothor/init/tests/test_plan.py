"""Phase 1 decides, phase 2 writes — and nothing crosses that line.

The rule this module pins is the one the whole wizard exists for: ``genus
init --yes`` either produces a running instance or changes nothing. A
required check that fails in phase 1 must leave the workspace exactly as it
found it, name itself, and exit 1.
"""

from __future__ import annotations

import json
from typing import Any

from robothor.init.context import InitContext
from robothor.init.plan import InitPlan, run_plan
from robothor.init.steps import BaseStep, CheckResult, StepError


class _Marker(BaseStep):
    """A step whose only effect is a file, so "did it write" is answerable."""

    def __init__(
        self,
        step_id: str,
        *,
        ok: bool = True,
        required: bool = True,
        action: str = "create",
        resumable: bool = True,
        boom: bool = False,
    ) -> None:
        self.id = step_id
        self.title = step_id.title()
        self.required = required
        self.resumable = resumable
        self._ok = ok
        self._action = action
        self._boom = boom

    def check(self, ctx: InitContext) -> CheckResult:
        return CheckResult(
            self._ok,
            detail=f"{self.id} detail",
            fix_hint=f"run the {self.id} fixer",
            action=self._action,
        )

    def apply(self, ctx: InitContext) -> None:
        if self._boom:
            raise StepError(f"{self.id} could not be applied")
        (ctx.workspace / f"{self.id}.marker").write_text("written\n", encoding="utf-8")


def _ctx(tmp_path: Any, **kwargs: Any) -> InitContext:
    workspace = tmp_path / "workspace"
    return InitContext(workspace=workspace, **kwargs)


def _plan(*steps: BaseStep) -> InitPlan:
    return InitPlan(substrate_name="local", steps=list(steps))


class TestPhaseOneWritesNothing:
    def test_failing_required_check_writes_nothing_and_exits_one(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True)
        ctx.workspace.mkdir(parents=True)
        plan = _plan(_Marker("first"), _Marker("prereqs", ok=False), _Marker("third"))

        result = run_plan(ctx, plan)

        assert result.exit_code == 1
        assert list(ctx.workspace.iterdir()) == []
        assert result.blocked == ["prereqs"]
        assert not result.steps

    def test_the_failing_check_is_named_with_its_fix_hint(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True)
        plan = _plan(_Marker("prereqs", ok=False))

        result = run_plan(ctx, plan)
        entry = next(row for row in result.plan if row.id == "prereqs")

        assert entry.status == "blocked"
        assert entry.fix_hint == "run the prereqs fixer"

    def test_a_failing_optional_check_does_not_block(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True)
        ctx.workspace.mkdir(parents=True)
        plan = _plan(_Marker("channels", ok=False, required=False), _Marker("link"))

        result = run_plan(ctx, plan)

        assert result.exit_code == 0
        assert (ctx.workspace / "link.marker").exists()
        assert not (ctx.workspace / "channels.marker").exists()


class TestPlanRendering:
    def test_the_plan_says_create_exists_and_skip(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True)
        plan = _plan(
            _Marker("workspace", action="create"),
            _Marker("identity", action="exists"),
            _Marker("models", action="skip"),
        )

        entries = plan.check_all(ctx)

        assert [(row.id, row.status) for row in entries] == [
            ("workspace", "create"),
            ("identity", "exists"),
            ("models", "skip"),
        ]


class TestDryRun:
    def test_dry_run_applies_nothing(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True, dry_run=True)
        ctx.workspace.mkdir(parents=True)
        plan = _plan(_Marker("workspace"), _Marker("agents"))

        result = run_plan(ctx, plan)

        assert result.exit_code == 0
        assert list(ctx.workspace.iterdir()) == []
        assert {row.status for row in result.steps} == {"planned"}

    def test_dry_run_records_no_state(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True, dry_run=True)
        ctx.workspace.mkdir(parents=True)
        run_plan(ctx, _plan(_Marker("workspace")))

        assert not (ctx.workspace / ".robothor" / "init_state.yaml").exists()


class TestResume:
    def test_resume_skips_completed_steps(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True)
        ctx.workspace.mkdir(parents=True)
        run_plan(ctx, _plan(_Marker("workspace"), _Marker("agents")))
        (ctx.workspace / "workspace.marker").unlink()

        resumed = InitContext(workspace=ctx.workspace, yes=True)
        result = run_plan(resumed, _plan(_Marker("workspace"), _Marker("agents")))

        assert [(row.id, row.status) for row in result.steps] == [
            ("workspace", "skipped"),
            ("agents", "skipped"),
        ]
        assert not (ctx.workspace / "workspace.marker").exists()

    def test_state_is_saved_after_each_step_so_a_failure_resumes(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True)
        ctx.workspace.mkdir(parents=True)
        plan = _plan(_Marker("workspace"), _Marker("migrate", boom=True), _Marker("agents"))

        result = run_plan(ctx, plan)

        assert result.exit_code == 1
        assert [(row.id, row.status) for row in result.steps] == [
            ("workspace", "applied"),
            ("migrate", "failed"),
            # Recorded as not-run rather than omitted: a reader of the JSON has
            # to be able to tell "did not run" from "was never in the plan".
            ("agents", "not-run"),
        ]
        state_path = ctx.workspace / ".robothor" / "init_state.yaml"
        assert "workspace: completed" in state_path.read_text()
        assert "migrate" not in state_path.read_text()

    def test_a_non_resumable_step_runs_again(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True)
        ctx.workspace.mkdir(parents=True)
        run_plan(ctx, _plan(_Marker("ack", resumable=False)))
        (ctx.workspace / "ack.marker").unlink()

        resumed = InitContext(workspace=ctx.workspace, yes=True)
        run_plan(resumed, _plan(_Marker("ack", resumable=False)))

        assert (ctx.workspace / "ack.marker").exists()


class TestJsonShape:
    def test_json_carries_plan_steps_url_and_exit_code(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True, json_mode=True)
        ctx.workspace.mkdir(parents=True)
        result = run_plan(ctx, _plan(_Marker("workspace")))
        result.first_run_url = "http://127.0.0.1:3004/setup?token=redacted"

        payload = json.loads(json.dumps(result.as_dict()))

        assert set(payload) == {"plan", "steps", "first_run_url", "exit_code"}
        assert payload["exit_code"] == 0
        assert payload["first_run_url"].endswith("token=redacted")
        assert payload["steps"] == [
            {"id": "workspace", "status": "applied", "detail": "workspace detail"}
        ]
        assert payload["plan"][0]["id"] == "workspace"
        assert payload["plan"][0]["required"] is True

    def test_json_shape_on_a_blocked_run(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True, json_mode=True)
        result = run_plan(ctx, _plan(_Marker("prereqs", ok=False)))

        payload = result.as_dict()

        assert payload["exit_code"] == 1
        assert payload["steps"] == []
        assert payload["plan"][0]["status"] == "blocked"


class TestAnswers:
    def test_yes_takes_the_default_without_prompting(self, tmp_path):
        ctx = _ctx(tmp_path, yes=True)
        assert ctx.ask("tenant", "Tenant id", "default") == "default"

    def test_interactive_uses_the_prompt_seam(self, tmp_path):
        asked: list[tuple[str, str]] = []

        def prompt(question: str, default: str) -> str:
            asked.append((question, default))
            return "typed"

        ctx = _ctx(tmp_path, prompt=prompt)
        assert ctx.ask("tenant", "Tenant id", "default") == "typed"
        assert asked == [("Tenant id", "default")]

    def test_an_answer_already_supplied_is_never_asked_again(self, tmp_path):
        def prompt(question: str, default: str) -> str:  # pragma: no cover - must not run
            raise AssertionError("asked for an answer it already had")

        ctx = _ctx(tmp_path, prompt=prompt, answers={"tenant": "acme"})
        assert ctx.ask("tenant", "Tenant id", "default") == "acme"


class TestStepFailureIsNotAnException:
    def test_an_unexpected_error_is_reported_not_raised(self, tmp_path):
        class _Explode(BaseStep):
            id = "boom"
            title = "Boom"

            def apply(self, ctx: InitContext) -> None:
                raise RuntimeError("something the step did not anticipate")

        ctx = _ctx(tmp_path, yes=True)
        ctx.workspace.mkdir(parents=True)
        result = run_plan(ctx, _plan(_Explode()))

        assert result.exit_code == 1
        assert result.steps[0].status == "failed"
        assert "RuntimeError" in result.steps[0].detail
