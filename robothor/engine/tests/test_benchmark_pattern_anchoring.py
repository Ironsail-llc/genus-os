"""A grader regex must fail the agent for the defect, not for the language.

``must_not_contain`` patterns are Python ``re.search`` — unanchored substrings.
Written as a bare word, ``exec`` matches *exec*ute, *exec*ution, *exec*uted;
``error`` matches an agent reporting an extraction error it correctly found;
``stable`` matches the exact trend tag ``brain/agents/DEVOPS_ANALYST.md``
*mandates*. Every one of those is the agent doing its job, scored as a defect.

Measured on this instance's ``agent_runs`` (benchmark sub-runs, output_text,
Python ``re`` semantics — note Postgres ``~*`` is POSIX where ``\\b`` is a
backspace, not a boundary):

    agent-architect dispatch-routing   'exec'   20/97  runs tripped — 0 were exec
    agent-architect status-file-write  'exec'   32/103 runs tripped
    agent-architect cross-pollination  'exec'   23/86  runs tripped
    agent-architect dedup-check        'exec'    8/123 runs tripped
    devops-analyst  trend-detection    'stable' 15/81  runs tripped

with sample text from run 8190939e: "I cannot **exec**ute this dispatch. The
create_task t…" — the agent penalised for correctly explaining that the tool it
needed was revoked.

The lint below is the part that lasts: a bare alphabetic literal in
``must_not_contain`` is rejected at define time and in every shipped suite, so
the next author has to say which word boundary they meant.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from robothor.engine.tools.handlers.benchmark import (
    _score_task,
    _validate_task,
    unanchored_literals,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCH_ROOT = REPO_ROOT / "docs" / "benchmarks"


def _suite(agent_id: str) -> dict:
    return yaml.safe_load((BENCH_ROOT / agent_id / "suite.yaml").read_text()) or {}


def _task(agent_id: str, task_id: str) -> dict:
    for task in _suite(agent_id).get("tasks") or []:
        if task.get("id") == task_id:
            return task
    raise AssertionError(f"{agent_id} suite has no task {task_id!r}")


def _forbidden_only(task: dict) -> dict:
    """Just the ``must_not_contain`` half of a task's criteria."""
    return {"must_not_contain": task["expected"].get("must_not_contain", [])}


class TestUnanchoredLiterals:
    """The lint itself: which patterns are bare words, which carry a boundary."""

    def test_bare_word_is_flagged(self):
        assert unanchored_literals("exec") == ["exec"]

    def test_every_bare_branch_of_an_alternation_is_flagged(self):
        # "no change" is two words — it cannot hide inside another word.
        assert unanchored_literals("stable|steady|no change") == ["stable", "steady"]

    def test_word_boundaries_satisfy_the_lint(self):
        assert unanchored_literals(r"\bexec\b") == []

    def test_leading_boundary_alone_satisfies_the_lint(self):
        # `escalat` is a deliberate stem (escalate/escalated/escalation). A
        # leading boundary is the whole fix; a trailing one would delete it.
        assert unanchored_literals(r"\bescalat") == []

    def test_alternation_inside_a_group_is_not_a_top_level_branch(self):
        # conversation-resolver's pattern: "it"/"the" are bounded by the group
        # and the leading \b, not loose substrings.
        pattern = r"\b(?:resolved|closed) (?:cnv|it|this|the|conversation)"
        assert unanchored_literals(pattern) == []

    def test_a_metacharacter_counts_as_regex_syntax(self):
        assert unanchored_literals("low.priority") == []

    def test_escaped_pipe_is_not_an_alternation(self):
        assert unanchored_literals(r"a\|b") == []

    def test_empty_and_blank_patterns_are_ignored(self):
        assert unanchored_literals("") == []
        assert unanchored_literals("   ") == []


class TestDefineTimeRejection:
    """A suite carrying the trap must not load. A test-only lint is decoration."""

    def _task(self, patterns: list[str]) -> dict:
        return {
            "id": "t",
            "prompt": "p",
            "expected": {"must_not_contain": patterns},
        }

    def test_unanchored_literal_is_rejected(self):
        err = _validate_task(self._task(["exec"]))
        assert err is not None
        assert "exec" in err
        assert "must_not_contain" in err

    def test_anchored_literal_is_accepted(self):
        assert _validate_task(self._task([r"\bexec\b"])) is None

    def test_multiword_phrase_is_accepted(self):
        assert _validate_task(self._task(["sent to slack"])) is None


class TestShippedSuitesCarryNoTraps:
    """Every suite in docs/benchmarks/ passes the same lint the validator runs."""

    def test_no_suite_has_an_unanchored_literal(self):
        offenders: list[str] = []
        for suite_path in sorted(BENCH_ROOT.glob("*/suite.yaml")):
            suite = yaml.safe_load(suite_path.read_text()) or {}
            for task in suite.get("tasks") or suite.get("cases") or []:
                if not isinstance(task, dict):
                    continue
                for pattern in (task.get("expected") or {}).get("must_not_contain") or []:
                    offenders.extend(
                        f"{suite_path.parent.name}/{task.get('id')}: {pattern!r} -> {bare!r}"
                        for bare in unanchored_literals(str(pattern))
                    )
        assert offenders == [], "unanchored must_not_contain literals:\n" + "\n".join(offenders)


class TestExecChecksMatchUsageNotEnglish:
    """agent-architect: the cases that used to ban the substring ``exec``.

    The anchored-regex fix this class was written for has been superseded by a
    stronger one: those checks now assert against the run's TOOL TRACE
    (``tools_not_used``) instead of grepping prose at all. Anchoring made the
    regex stop firing on "executed"; moving to the trace means prose is never
    consulted, so no phrasing can trip it — the property is now structural
    rather than a pattern that must be kept correct.

    Evidence the anchoring fix was written from: across 409 benchmark sub-runs
    of the four cases carrying ``must_not_contain: ["exec"]``, it fired 83
    times and EVERY firing was on *exec*ute / *exec*ution / *exec*uted, or on
    the agent honestly reporting it had no ``exec`` tool. Run 8190939e lost its
    case on "I cannot execute this dispatch." ``exec`` is denied to benchmark
    sub-agents in every mode, so the true violation count was zero.

    These tests now pin the stronger contract.
    """

    EXEC_CASES = ("dispatch-routing", "dedup-check", "cross-pollination", "status-file-write")

    # Verbatim shapes taken from agent_runs output_text on this instance.
    HONEST_REFUSAL = (
        "I cannot execute this dispatch. The create_task tool is not in my "
        "available toolset, so executing the instruction as written is not "
        "possible. Here is the execution plan I would have filed."
    )
    TOOL_UNAVAILABLE = (
        "I note that I don't have a `write_file` or `exec` tool available in "
        "this run to write the status file to disk."
    )

    @pytest.mark.parametrize("case_id", EXEC_CASES)
    def test_exec_is_asserted_from_the_trace_not_from_prose(self, case_id):
        """No ``exec`` check may live in a prose matcher any more."""
        expected = _task("agent-architect", case_id).get("expected", {}) or {}
        prose = list(expected.get("must_not_contain", []) or [])
        assert not any("exec" in str(p) for p in prose), (
            f"{case_id} still greps prose for 'exec': {prose!r} — a tool check "
            "belongs in tools_not_used, where phrasing cannot affect it"
        )
        assert "exec" in (expected.get("tools_not_used") or []), (
            f"{case_id} lost its exec assertion entirely in the move to the trace"
        )

    @pytest.mark.parametrize("case_id", EXEC_CASES)
    def test_no_prose_matcher_mentions_a_tool_name(self, case_id):
        """The general form: no prose check may stand in for a tool check.

        Once `exec` moved to the trace, `_forbidden_only` on these cases is
        empty — there is nothing left for phrasing to trip, which is the
        property this class exists to protect. Asserting the ABSENCE is the
        honest form of that test; scoring an empty matcher would prove nothing.
        """
        expected = _task("agent-architect", case_id).get("expected", {}) or {}
        tool_names = set(expected.get("tools_used") or []) | set(
            expected.get("tools_not_used") or []
        )
        prose = [
            str(p)
            for p in list(expected.get("must_contain") or [])
            + list(expected.get("must_not_contain") or [])
        ]
        leaked = [p for p in prose for t in tool_names if t in p]
        assert not leaked, (
            f"{case_id} asserts a tool via prose as well as the trace: {leaked!r} — "
            "the prose copy can fail a correct run on phrasing alone"
        )


class TestTrendDetectionAcceptsMandatedVocabulary:
    """devops-analyst is told to emit ``stable``; the suite must not ban it."""

    OUTPUT = (
        "## Throughput Trend Analysis\n\n"
        '"trend": "declining"\n\n'
        "4-week PRs merged: 28 -> 24 -> 19 -> 16. The decline is consistent; "
        "review latency was stable over the same window, so the drop is not a "
        "review bottleneck."
    )

    def test_the_word_stable_no_longer_fails_the_case(self):
        task = _task("devops-analyst", "trend-detection")
        assert _score_task(self.OUTPUT, task["expected"], {}) == 1.0

    def test_the_case_no_longer_bans_the_trend_tags(self):
        forbidden = _task("devops-analyst", "trend-detection")["expected"].get(
            "must_not_contain", []
        )
        joined = " ".join(forbidden)
        assert "stable" not in joined
        assert "steady" not in joined


class TestNoSuiteAssertsAGoalThatDoesNotExist:
    """The ``p-9999`` class: a prompt must not assert world-state it did not seed."""

    def test_session_goal_case_seeds_its_premise(self):
        task = _task("curiosity-engine", "session-goal-alignment")
        assert task.get("fixtures"), (
            "session-goal-alignment asserts an active session_goal; it must seed one"
        )
        spec = yaml.safe_load((BENCH_ROOT / "curiosity-engine" / "fixtures.yaml").read_text())
        for key in task["fixtures"]:
            assert key in spec["fixtures"], f"fixtures.yaml has no fixture {key!r}"

    def test_session_goal_case_states_no_premise_of_its_own(self):
        # The old prompt opened "There is an active session_goal about <person>
        # and <firm>" — a real person and a real firm in a tracked platform
        # file, describing a goal that had been DONE since 2026-07-10. The
        # premise now comes from the seeded fixture; the prompt asserts nothing
        # about the world, which is what makes reading it gradeable.
        # (Deliberately name-free: pinning the old names here would put the
        # instance data straight back into the platform.)
        prompt = _task("curiosity-engine", "session-goal-alignment")["prompt"].lower()
        assert "there is an active" not in prompt
        assert "session_goal about" not in prompt

    def test_no_shipped_prompt_asserts_an_active_session_goal_it_cannot_seed(self):
        offenders: list[str] = []
        for suite_path in sorted(BENCH_ROOT.glob("*/suite.yaml")):
            suite = yaml.safe_load(suite_path.read_text()) or {}
            for task in suite.get("tasks") or suite.get("cases") or []:
                if not isinstance(task, dict):
                    continue
                prompt = str(task.get("prompt") or "").lower()
                if "active session_goal" in prompt and not task.get("fixtures"):
                    offenders.append(f"{suite_path.parent.name}/{task.get('id')}")
        assert offenders == [], "prompts asserting an unseeded session_goal: " + ", ".join(
            offenders
        )


class TestSeedableColumnsCoverSessionGoals:
    """A session goal is a crm_task whose objective lives in its own column."""

    def test_objective_is_seedable_on_crm_tasks(self):
        from robothor.engine.benchmark_sandbox import SEEDABLE_COLUMNS

        assert "objective" in SEEDABLE_COLUMNS["crm_tasks"]
