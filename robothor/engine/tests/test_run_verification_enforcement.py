"""Acting on the run-verification verdict: task closure, delivery, prompt.

WHY. ``_persist_run_sync`` auto-resolved the originating CRM task on
``COMPLETED`` with ``resolution=f"Run completed: {output_text[:200]}"`` — the
agent's own claim became the permanent record, and nothing checked it. On the
live box 300 of the 571 tasks closed in the last 7 days (53%) carry that
string, and ``email-analyst`` holds 1,692 DONE tasks while having had no
production run since 2026-06-14: every one of those closures came from a
benchmark run, resolved with benchmark fixture text.

THE LADDER (``ROBOTHOR_RUN_VERIFICATION_ENABLED`` / ``_MODE``):
  off / observe  nothing here happens — byte-identical to the old behavior.
                 The observe tests below are the safety pin for the merge
                 posture, and they must never be relaxed.
  alert          the operator is told the truth: the delivered message carries
                 an honest-failure banner, the prompt rule is injected, and a
                 resolution written from an unverified run is labelled
                 ``[claimed]``. Task state is NOT changed.
  enforce        the verdict acts: an unverified run does not resolve its
                 task at all (a next_action is written instead), a verified
                 run resolves with a ``[verified]`` prefix, and a benchmark
                 run never resolves a production task regardless of verdict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.config import EngineConfig
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode, RunStatus, RunStep, StepType
from robothor.engine.runner import AgentRunner

# The real incident, reduced to its shape: a confident payment confirmation
# whose entire tool trace is one write to /tmp.
VENMO_OUTPUT = "✅ Payment confirmed — $270 sent via Venmo. The rest is handled."


@pytest.fixture
def runner() -> AgentRunner:
    return AgentRunner(EngineConfig())


@pytest.fixture
def crm(monkeypatch: pytest.MonkeyPatch):
    """Capture every CRM write the persist path would make."""
    calls: dict[str, list[dict]] = {"resolve": [], "next_action": [], "update": []}
    monkeypatch.setattr(
        "robothor.crm.dal.resolve_task",
        lambda task_id, **kw: calls["resolve"].append({"task_id": task_id, **kw}) or True,
    )
    monkeypatch.setattr(
        "robothor.crm.dal.set_next_action",
        lambda **kw: calls["next_action"].append(kw) or True,
    )
    monkeypatch.setattr(
        "robothor.crm.dal.update_task",
        lambda task_id, **kw: calls["update"].append({"task_id": task_id, **kw}) or True,
    )
    return calls


def _run(
    output_text: str = VENMO_OUTPUT,
    *,
    verified_status: str | None = "unverified_claims",
    trigger_detail: str | None = "telegram",
    unsupported: tuple[str, ...] = ("payment",),
    status: RunStatus = RunStatus.COMPLETED,
) -> AgentRun:
    run = AgentRun(
        id="00000000-0000-0000-0000-0000000000rv",
        tenant_id="default",
        agent_id="main",
        trigger_type="telegram",  # type: ignore[arg-type]
        trigger_detail=trigger_detail,
        status=status,
        output_text=output_text,
        task_id="task-1234",
    )
    run.verified_status = verified_status
    if verified_status not in (None, "no_claims"):
        run.verification = {
            "version": 1,
            "status": verified_status,
            "summary": f"unsupported claim(s): {', '.join(unsupported)}",
            "claims": [
                {
                    "kind": kind,
                    "phrase": "…",
                    "supported": verified_status != "verified",
                    "attempted": verified_status == "failed_verification",
                    "evidence_steps": [],
                    "detail": "",
                }
                for kind in unsupported
            ],
            "unsupported": [] if verified_status == "verified" else list(unsupported),
        }
    return run


def _mode(value: str):
    return patch("robothor.engine.feature_flags.run_verification_mode", return_value=value)


# ──────────────────────────────────────────────────────────────────────
# 1. Task-resolution gating
# ──────────────────────────────────────────────────────────────────────


class TestObserveChangesNothing:
    """THE SAFETY PIN. The flag ships at observe; observe must be a no-op."""

    def test_unverified_run_still_resolves_exactly_as_today(self, runner, crm):
        with _mode("observe"):
            runner._update_task_for_run(_run())
        assert len(crm["resolve"]) == 1
        assert crm["resolve"][0]["resolution"].startswith("Run completed: ")
        assert crm["next_action"] == []

    def test_verified_run_resolution_is_unprefixed(self, runner, crm):
        with _mode("observe"):
            runner._update_task_for_run(_run(verified_status="verified"))
        assert crm["resolve"][0]["resolution"].startswith("Run completed: ")

    def test_off_mode_is_identical(self, runner, crm):
        with _mode("off"):
            runner._update_task_for_run(_run())
        assert crm["resolve"][0]["resolution"].startswith("Run completed: ")
        assert crm["next_action"] == []


class TestEnforceGatesResolution:
    def test_unverified_claims_does_not_resolve(self, runner, crm):
        with _mode("enforce"):
            runner._update_task_for_run(_run())
        assert crm["resolve"] == [], "an unverified claim closed the task"

    def test_unverified_claims_sets_a_next_action_naming_the_claim(self, runner, crm):
        with _mode("enforce"):
            runner._update_task_for_run(_run())
        assert len(crm["next_action"]) == 1
        wrote = crm["next_action"][0]
        assert wrote["task_id"] == "task-1234"
        assert "payment" in wrote["next_action"].lower()
        assert len(wrote["next_action"]) <= 500

    def test_failed_verification_also_stays_open(self, runner, crm):
        with _mode("enforce"):
            runner._update_task_for_run(
                _run(verified_status="failed_verification", unsupported=("sent_email",))
            )
        assert crm["resolve"] == []
        assert "email" in crm["next_action"][0]["next_action"].lower()

    def test_verified_run_resolves_with_the_verified_prefix(self, runner, crm):
        with _mode("enforce"):
            runner._update_task_for_run(_run(verified_status="verified"))
        assert crm["resolve"][0]["resolution"].startswith("[verified] Run completed: ")
        assert crm["next_action"] == []

    def test_no_claims_run_resolves_with_the_verified_prefix(self, runner, crm):
        with _mode("enforce"):
            runner._update_task_for_run(
                _run(output_text="Here are three restaurants.", verified_status="no_claims")
            )
        assert crm["resolve"][0]["resolution"].startswith("[verified] Run completed: ")

    def test_failed_run_still_reopens_the_task(self, runner, crm):
        """Non-COMPLETED handling is untouched by verification."""
        with _mode("enforce"):
            runner._update_task_for_run(_run(status=RunStatus.FAILED, verified_status=None))
        assert crm["update"][0]["status"] == "TODO"
        assert crm["resolve"] == []


class TestAlertLabelsTheLedger:
    """alert tells the truth in the record without changing task state."""

    def test_unverified_resolution_is_labelled_claimed(self, runner, crm):
        with _mode("alert"):
            runner._update_task_for_run(_run())
        assert len(crm["resolve"]) == 1, "alert must not gate the close — that is enforce"
        assert crm["resolve"][0]["resolution"].startswith("[claimed] Run completed: ")

    def test_verified_resolution_is_labelled_verified(self, runner, crm):
        with _mode("alert"):
            runner._update_task_for_run(_run(verified_status="verified"))
        assert crm["resolve"][0]["resolution"].startswith("[verified] Run completed: ")


# ──────────────────────────────────────────────────────────────────────
# 2. Benchmark runs never close production tasks
# ──────────────────────────────────────────────────────────────────────


class TestBenchmarkRunsNeverResolve:
    def test_enforce_blocks_a_benchmark_close_even_when_verified(self, runner, crm):
        with _mode("enforce"):
            runner._update_task_for_run(
                _run(verified_status="verified", trigger_detail="benchmark:crm-hygiene#3")
            )
        assert crm["resolve"] == [], "a benchmark run closed a production task"
        assert crm["next_action"] == []

    def test_observe_leaves_benchmark_behavior_untouched(self, runner, crm):
        with _mode("observe"):
            runner._update_task_for_run(
                _run(verified_status="verified", trigger_detail="benchmark:crm-hygiene#3")
            )
        assert len(crm["resolve"]) == 1

    def test_predicate_is_the_shared_one_not_a_second_copy(self):
        """Reuse the decontamination prefix — a second literal is how this drifts."""
        from pathlib import Path

        from robothor.engine import analytics
        from robothor.engine import runner as runner_mod

        assert analytics.is_benchmark_run("benchmark:crm-hygiene#3") is True
        assert analytics.is_benchmark_run("telegram") is False
        assert analytics.is_benchmark_run(None) is False
        src = Path(runner_mod.__file__).read_text()
        assert '"benchmark:' not in src and "'benchmark:" not in src, (
            "runner.py hand-rolls the benchmark trigger prefix instead of "
            "reusing analytics.BENCHMARK_TRIGGER_PREFIX"
        )


# ──────────────────────────────────────────────────────────────────────
# 3. The Venmo fixture, end to end through the persist path
# ──────────────────────────────────────────────────────────────────────


class TestVenmoFixtureEndToEnd:
    def test_the_incident_leaves_its_task_open_with_a_reason(self, runner, crm):
        """Run 6cb7e492's shape: one /tmp write, a payment confirmation."""
        run = AgentRun(
            id="00000000-0000-0000-0000-00000006cb7e",
            tenant_id="default",
            agent_id="main",
            trigger_type="telegram",  # type: ignore[arg-type]
            trigger_detail="chat:1|sender:Operator",
            status=RunStatus.COMPLETED,
            output_text=VENMO_OUTPUT,
            task_id="task-venmo",
        )
        run.steps = [
            RunStep(
                run_id=run.id,
                step_number=2,
                step_type=StepType.TOOL_CALL,
                tool_name="write_file",
                tool_input={"path": "/tmp/payment_note.md", "content": "…"},
                tool_output={"success": True},
            )
        ]
        with (
            _mode("enforce"),
            patch("robothor.engine.tracking.log_guardrail_event"),
            patch("robothor.engine.feature_flags.notify_guardrail_alert"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_steps_batch"),
        ):
            runner._verify_run_claims(run)
            runner._persist_run_sync(run)

        assert run.verified_status == "unverified_claims"
        assert crm["resolve"] == [], "the Venmo run closed its task"
        assert len(crm["next_action"]) == 1
        assert "payment" in crm["next_action"][0]["next_action"].lower()


# ──────────────────────────────────────────────────────────────────────
# 4. Honest-failure banner at delivery
# ──────────────────────────────────────────────────────────────────────


def _delivery_config() -> AgentConfig:
    return AgentConfig(
        id="main",
        name="Main",
        delivery_mode=DeliveryMode.ANNOUNCE,
        delivery_to="12345",
    )


class TestDeliveryBanner:
    @pytest.fixture
    def sent(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        bodies: list[str] = []

        async def _fake_telegram(config, text, run):  # noqa: ANN001
            bodies.append(text)
            return True

        monkeypatch.setattr("robothor.engine.delivery._deliver_telegram", _fake_telegram)
        monkeypatch.setattr(
            "robothor.engine.delivery._persist_delivery_status", AsyncMock(return_value=None)
        )
        return bodies

    @pytest.mark.asyncio
    async def test_banner_appended_for_unverified_claims(self, sent):
        from robothor.engine.delivery import deliver

        run = _run(output_text="I emailed the report to the team.", unsupported=("sent_email",))
        with _mode("alert"):
            await deliver(_delivery_config(), run)
        assert sent, "nothing delivered"
        assert "⚠️ Unverified" in sent[0]
        assert "email" in sent[0].lower()

    @pytest.mark.asyncio
    async def test_output_text_in_the_db_is_untouched(self, sent):
        from robothor.engine.delivery import deliver

        run = _run(output_text="I emailed the report to the team.", unsupported=("sent_email",))
        with _mode("alert"):
            await deliver(_delivery_config(), run)
        assert run.output_text == "I emailed the report to the team."

    @pytest.mark.asyncio
    async def test_no_banner_when_verified(self, sent):
        from robothor.engine.delivery import deliver

        with _mode("enforce"):
            await deliver(_delivery_config(), _run(verified_status="verified"))
        assert "Unverified" not in sent[0]

    @pytest.mark.asyncio
    async def test_no_banner_for_failed_verification(self, sent):
        """Only the 'nothing was even attempted' class gets the banner."""
        from robothor.engine.delivery import deliver

        with _mode("enforce"):
            await deliver(_delivery_config(), _run(verified_status="failed_verification"))
        assert "Unverified" not in sent[0]

    @pytest.mark.asyncio
    async def test_no_banner_at_observe(self, sent):
        from robothor.engine.delivery import deliver

        with _mode("observe"):
            await deliver(_delivery_config(), _run())
        assert "Unverified" not in sent[0]


# ──────────────────────────────────────────────────────────────────────
# 5. The prompt rule
# ──────────────────────────────────────────────────────────────────────


class TestPromptRule:
    def test_absent_at_observe(self):
        from robothor.engine.prompts import behavioral_rules

        with _mode("observe"):
            assert (
                behavioral_rules()
                == __import__(
                    "robothor.engine.prompts", fromlist=["BEHAVIORAL_RULES"]
                ).BEHAVIORAL_RULES
            )

    def test_present_at_alert(self):
        from robothor.engine.prompts import behavioral_rules

        with _mode("alert"):
            text = behavioral_rules()
        assert "tool result" in text.lower()
        assert "abstention" in text.lower()

    def test_present_at_enforce(self):
        from robothor.engine.prompts import behavioral_rules

        with _mode("enforce"):
            assert "abstention" in behavioral_rules().lower()

    def test_config_uses_the_gated_accessor(self):
        """config.py must call behavioral_rules(), not the raw constant."""
        from pathlib import Path

        from robothor.engine import config as config_mod

        src = Path(config_mod.__file__).read_text()
        assert "behavioral_rules()" in src
