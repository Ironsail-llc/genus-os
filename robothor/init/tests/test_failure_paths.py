"""What has to keep working on the run that goes wrong.

Three of these are about the same mistake in different places: a wizard that is
honest about failing is worth nothing if the failure takes away the operator's
means of recovery. A `verify` failure used to end the run at step 16, so the ONE
run that had written the identity, the config, the schema, the fleet and the
owner account printed no `/setup?token=…` URL — and a fresh instance has no
other way in.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.init.context import InitContext
from robothor.init.plan import InitPlan, run_plan
from robothor.init.render import render_summary
from robothor.init.steps import BaseStep, StepError, VerifyStep
from robothor.init.substrates.local import LocalLinkStep, LocalSubstrate


def _ctx(tmp_path, **kwargs: Any) -> InitContext:
    kwargs.setdefault("yes", True)
    return InitContext(workspace=tmp_path / "workspace", **kwargs)


class _Boom(BaseStep):
    id = "verify"
    title = "Verify"

    def apply(self, ctx: InitContext) -> None:
        raise StepError("genus doctor found 1 required failure(s): db.connect (refused)")


class _Wrote(BaseStep):
    def __init__(self, step_id: str) -> None:
        self.id = step_id
        self.title = step_id.title()

    def apply(self, ctx: InitContext) -> None:
        ctx.workspace.mkdir(parents=True, exist_ok=True)
        (ctx.workspace / f"{self.id}.marker").write_text("x", encoding="utf-8")


class TestTheLinkSurvivesAFailure:
    def test_the_link_step_still_runs_after_verify_fails(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.workspace.mkdir(parents=True)
        plan = InitPlan(
            "local", [_Wrote("agents"), _Boom(), LocalLinkStep(is_a_terminal=lambda: True)]
        )

        result = run_plan(ctx, plan)

        assert result.exit_code == 1
        assert [(row.id, row.status) for row in result.steps] == [
            ("agents", "applied"),
            ("verify", "failed"),
            ("link", "applied"),
        ]
        assert "/setup?token=" in result.first_run_url

    def test_the_json_carries_both_the_failure_and_the_url(self, tmp_path):
        ctx = _ctx(tmp_path, json_mode=True)
        ctx.workspace.mkdir(parents=True)
        plan = InitPlan("local", [_Boom(), LocalLinkStep(is_a_terminal=lambda: True)])

        payload = json.loads(json.dumps(run_plan(ctx, plan).as_dict()))

        assert payload["exit_code"] == 1
        assert "/setup?token=" in payload["first_run_url"]
        statuses = {row["id"]: row["status"] for row in payload["steps"]}
        assert statuses == {"verify": "failed", "link": "applied"}

    def test_an_ordinary_step_does_not_run_after_a_failure(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.workspace.mkdir(parents=True)
        plan = InitPlan("local", [_Boom(), _Wrote("agents")])

        result = run_plan(ctx, plan)

        assert [(row.id, row.status) for row in result.steps] == [
            ("verify", "failed"),
            ("agents", "not-run"),
        ]
        assert not (ctx.workspace / "agents.marker").exists()

    def test_the_link_is_the_only_step_that_runs_on_failure(self):
        run_anyway = [
            step.id for step in LocalSubstrate().steps() if getattr(step, "run_on_failure", False)
        ]

        assert run_anyway == ["link"]

    def test_the_summary_points_at_the_link_it_printed(self):
        from robothor.init.plan import InitResult, StepOutcome

        result = InitResult(
            steps=[
                StepOutcome("agents", "applied", "3 agents"),
                StepOutcome("verify", "failed", "db.connect refused"),
            ],
            first_run_url="http://127.0.0.1:3004/setup?token=x",
            exit_code=1,
        )

        text = "\n".join(render_summary(result, workspace="/tmp/ws"))

        assert "Stopped at `verify`" in text
        assert "Applied before it: agents" in text
        assert "first-run link above still works" in text

    def test_the_summary_names_setup_link_when_there_is_no_url(self):
        from robothor.init.plan import InitResult, StepOutcome

        result = InitResult(
            steps=[StepOutcome("migrate", "failed", "refused")],
            exit_code=1,
        )

        text = "\n".join(render_summary(result, workspace="/tmp/ws"))

        assert "genus auth setup-link" in text


class TestAnUnprobedProviderIsNotRecordedAsDone:
    def test_offline_does_not_write_provider_completed(self, tmp_path):
        """One --offline install used to make every later run skip the probe."""
        from robothor.init.steps import ProviderStep

        ctx = _ctx(
            tmp_path,
            offline=True,
            answers={"provider_id": "openrouter", "provider_model": "openrouter/openai/gpt-5.4"},
        )
        ctx.workspace.mkdir(parents=True)

        run_plan(ctx, InitPlan("local", [ProviderStep()]))

        state = ctx.workspace / ".robothor" / "init_state.yaml"
        assert not state.exists() or "provider" not in state.read_text()

    def test_a_probed_provider_is_recorded(self, tmp_path, monkeypatch):
        from robothor.engine.key_pool import ResolvedKey
        from robothor.init.provider_probe import ProbeResult
        from robothor.init.steps import ProviderStep

        monkeypatch.setattr(
            "robothor.engine.key_pool.scan_slots",
            lambda provider_id: [ResolvedKey(position=1, key="k", source="env")],
        )
        ctx = _ctx(
            tmp_path,
            answers={"provider_id": "openrouter", "provider_model": "openrouter/openai/gpt-5.4"},
        )
        ctx.workspace.mkdir(parents=True)
        step = ProviderStep(
            probe=lambda *a, **k: ProbeResult(True, "openrouter", "m", "answered", True)
        )

        run_plan(ctx, InitPlan("local", [step]))

        assert (
            "provider: completed" in (ctx.workspace / ".robothor" / "init_state.yaml").read_text()
        )


class TestTheSmallerWaysARunCanMislead:
    def test_a_malformed_state_file_is_no_progress_not_a_traceback(self, tmp_path):
        """This is the command whose job is to fix a broken box."""
        ctx = _ctx(tmp_path)
        (ctx.workspace / ".robothor").mkdir(parents=True)
        (ctx.workspace / ".robothor" / "init_state.yaml").write_text("- not\n- a mapping\n")

        assert ctx.state == {}

    def test_json_mode_keeps_the_token_out_of_the_log_half(self, tmp_path, capsys):
        """`genus init --json > x.json 2> x.log` must not leave a live
        single-use credential in x.log; first_run_url already carries it."""
        ctx = _ctx(tmp_path, json_mode=True)
        ctx.workspace.mkdir(parents=True)

        LocalLinkStep(is_a_terminal=lambda: False).apply(ctx)

        captured = capsys.readouterr()
        token = ctx.first_run_url.partition("token=")[2]
        assert token
        assert token not in captured.out
        assert token not in captured.err

    def test_an_existing_owner_yaml_in_another_tenant_blocks_in_phase_one(
        self, tmp_path, monkeypatch
    ):
        """The tenant guard used to fire at step 12, after the schema and the
        fleet were in place."""
        from robothor.init.steps import IdentityStep

        path = tmp_path / "identity" / "owner.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(
            "tenant_id: acme\nfirst_name: Alice\nlast_name: Example\nemail: alice@example.com\n"
        )
        monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(path))

        result = IdentityStep().check(_ctx(tmp_path, answers={"tenant_id": "other"}))

        assert result.ok is False
        assert "acme" in result.detail
        assert "other" in result.detail

    def test_an_existing_owner_yaml_names_who_it_names(self, tmp_path, monkeypatch):
        from robothor.init.steps import IdentityStep

        path = tmp_path / "identity" / "owner.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("tenant_id: default\nfirst_name: Alice\nemail: alice@example.com\n")
        monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(path))

        result = IdentityStep().check(_ctx(tmp_path, answers={"tenant_id": "default"}))

        assert result.action == "exists"
        assert "alice@example.com" in result.detail

    def test_the_bot_token_is_sent_once_not_twice(self, tmp_path):
        from robothor.doctor.context import HttpResponse
        from robothor.init.steps import ChannelsStep

        calls: list[str] = []

        def fetch(method, url, body, timeout):
            calls.append(url)
            return HttpResponse(status=200, body='{"ok": true, "result": {"username": "a_bot"}}')

        ctx = _ctx(tmp_path, answers={"telegram_token": "123:abc"}, http_fetch=fetch)
        ctx.workspace.mkdir(parents=True)
        step = ChannelsStep()
        step.check(ctx)
        step.apply(ctx)

        assert len(calls) == 1


class TestVerifySkipsDatabaseChecksUnderSkipDb:
    def _report(self, *rows: tuple[str, str, str, str]) -> Any:
        from robothor.doctor.runner import CheckResult as DoctorResult
        from robothor.doctor.runner import DoctorReport

        return DoctorReport(
            results=[
                DoctorResult(check_id, check_id, category, severity, status, "detail", False)
                for check_id, category, severity, status in rows
            ]
        )

    def test_the_database_checks_are_excluded_and_said_to_be(self, tmp_path):
        seen: dict[str, Any] = {}

        def fake_doctor(doctor_ctx: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return self._report(("host.disk", "host", "required", "pass"))

        ctx = _ctx(tmp_path, answers={"skip_db": True})
        VerifyStep(doctor=fake_doctor).apply(ctx)

        categories = {check.category for check in seen["checks"]}
        assert "database" not in categories
        assert "--skip-db" in ctx.details["verify"]

    def test_without_skip_db_every_check_runs(self, tmp_path):
        seen: dict[str, Any] = {}

        def fake_doctor(doctor_ctx: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return self._report(("db.connect", "database", "required", "pass"))

        VerifyStep(doctor=fake_doctor).apply(_ctx(tmp_path))

        assert seen.get("checks") is None

    def test_a_non_database_required_failure_still_blocks_under_skip_db(self, tmp_path):
        def fake_doctor(doctor_ctx: Any, **kwargs: Any) -> Any:
            return self._report(("manifests.schema", "manifests", "required", "fail"))

        ctx = _ctx(tmp_path, answers={"skip_db": True})

        with pytest.raises(StepError) as exc:
            VerifyStep(doctor=fake_doctor).apply(ctx)

        assert "manifests.schema" in str(exc.value)


class TestModelsFollowOllamaNotTheChatProvider:
    """A cloud install still needs embeddings.

    `llm/ollama.py` resolves `ollama.embedding_model` for every instance
    regardless of which provider answers chat, and the doctor's only Ollama
    check is `recommended` and merely asks whether the server answers — so
    gating the pull on the chat provider let a cloud install finish green and
    404 inside Ollama on the first memory write.
    """

    def _reachable(self, ok: bool):
        from robothor.doctor.context import HttpResponse

        def fetch(method, url, body, timeout):
            if ok:
                return HttpResponse(status=200, body='{"models": []}')
            return HttpResponse(status=0, error="ConnectError: refused")

        return fetch

    def test_a_cloud_provider_still_pulls_when_ollama_is_reachable(self, tmp_path):
        from robothor.init.steps import ModelsStep

        ctx = _ctx(
            tmp_path, answers={"provider_id": "openrouter"}, http_fetch=self._reachable(True)
        )
        result = ModelsStep().check(ctx)

        assert result.action == "create"
        assert "qwen3-embedding:0.6b" in result.detail

    def test_an_absent_ollama_skips_with_a_reason(self, tmp_path):
        from robothor.init.steps import ModelsStep

        ctx = _ctx(
            tmp_path, answers={"provider_id": "openrouter"}, http_fetch=self._reachable(False)
        )
        result = ModelsStep().check(ctx)

        assert result.action == "skip"
        assert "not reachable" in result.detail

    def test_skip_models_still_wins(self, tmp_path):
        from robothor.init.steps import ModelsStep

        ctx = _ctx(
            tmp_path,
            answers={"provider_id": "ollama", "skip_models": True},
            http_fetch=self._reachable(True),
        )

        assert ModelsStep().check(ctx).action == "skip"
