"""The read short-circuit on the shape the benchmark actually produces.

Measured, not imagined. Bench pod `robothor_test`, run
`8cd032aa-b281-463b-9310-3cfb81b1fee9` (02_Code_Intelligence task_2_sam3_debug,
`ROBOTHOR_STEP_EFFICIENCY_MODE=enforce`, 2026-09-15): the guard reached a
decision on 23 repeated reads and every single one of them came back carrying
the WHOLE file — `sam3_image.py` (37 KB) five times, at steps 80, 110, 122, 161
and 169, each row ending

    "note": "... and is repeated here because it is no longer in your context",
    "repeat_guard": "answered"

A guard that answers a repeat with the same bytes the tool would have returned
has saved a `stat` and a `read`, which is microseconds, and has spent the
tokens, which is the budget. From outside it is indistinguishable from a
control that never fired, and that is how it was read.

The first resend was right: that run compacts, compaction takes the earlier
result out of `session.messages`, and a pointer to something the model can no
longer see is worse than the repeat it prevents. The second, third, fourth and
fifth were not. The guard had just put the full content back into the
conversation and never wrote down that it had, so `_still_in_context` went on
comparing against the payload from step 15 — a string that is no longer in any
message and never will be again. Once a key resends, it resends forever.

So these tests drive the REAL dispatch path with the benchmark's conditions —
absolute paths, a `/tmp_workspace`-shaped root, `is_benchmark=True`, a session
registered the way `bench/wildclaw/run_one.py` registers one, results recorded
through `AgentSession.record_tool_call` exactly as `runner.py` records them —
and ask the question the sweep asks: did the repeat cost less than the read?
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

#: Big enough that resending it is the whole cost of the repeat, small enough
#: to keep the suite quick. The measured file was 37 KB.
_BODY = "# Copyright (c) Example Corp. All Rights Reserved\n" + ("x = compute(1)\n" * 1200)


class _Bench:
    """One benchmark-shaped run: real session, real dispatch, real recording."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import robothor.engine.feature_flags as ff
        import robothor.engine.permissions as perms
        import robothor.engine.tracking as tracking
        from robothor.engine import session_registry
        from robothor.engine.session import AgentSession

        monkeypatch.setattr(perms, "check_tool_permission", lambda *a, **kw: None)
        monkeypatch.setattr(ff, "step_efficiency_mode", lambda: "enforce")

        self.events: list[dict[str, Any]] = []
        monkeypatch.setattr(
            tracking,
            "log_guardrail_event",
            lambda run_id, guardrail_name, action, **kw: self.events.append(
                {"run_id": run_id, "guardrail_name": guardrail_name, "action": action, **kw}
            ),
        )

        # `/tmp_workspace/sam3/sam3/model/sam3_image.py`, in a tmp dir.
        self.workspace = tmp_path / "tmp_workspace"
        self.target = self.workspace / "sam3" / "sam3" / "model" / "sam3_image.py"
        self.target.parent.mkdir(parents=True)
        self.target.write_text(_BODY, encoding="utf-8")

        self.session = AgentSession(agent_id="wildclaw")
        self.session.run.tracking_disabled = True
        self.run_id = self.session.run.id
        session_registry.register(self.session)  # type: ignore[arg-type]

    def close(self) -> None:
        from robothor.engine import session_registry

        session_registry.unregister(self.run_id)

    def read(self) -> dict[str, Any]:
        """One `read_file` the way the runner makes one, result recorded."""
        from robothor.engine.tools import dispatch

        args = {"path": str(self.target)}

        async def _call() -> dict[str, Any]:
            return await dispatch._execute_tool(
                "read_file",
                args,
                agent_id="wildclaw",
                run_id=self.run_id,
                workspace=str(self.workspace),
                is_benchmark=True,
            )

        result = asyncio.run(_call())
        self.session.record_tool_call(
            tool_name="read_file",
            tool_input=args,
            tool_output=result,
            tool_call_id=f"call-{len(self.session.messages)}",
        )
        return result

    def compact(self) -> None:
        """What `maybe_compress` leaves behind: a summary, and none of the
        tool results it summarised."""
        self.session.messages[:] = [
            {"role": "user", "content": "[RETAINED CONTEXT]\n- [decision] read some files"}
        ]

    @staticmethod
    def carries_content(result: dict[str, Any]) -> bool:
        return bool(result.get("content"))

    @staticmethod
    def size(result: dict[str, Any]) -> int:
        return len(json.dumps(result, default=str))


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    run = _Bench(tmp_path, monkeypatch)
    try:
        yield run
    finally:
        run.close()


class TestTheShapeTheBenchmarkProduces:
    def test_the_first_repeat_after_a_compaction_resends_the_file(self, bench: Any) -> None:
        """Not a bug — the premise. The model cannot see it any more."""
        first = bench.read()
        assert bench.carries_content(first)
        bench.compact()

        second = bench.read()
        assert second.get("repeat_guard") == "answered"
        assert bench.carries_content(second), "the model can no longer see the file"

    def test_the_repeat_after_that_costs_almost_nothing(self, bench: Any) -> None:
        """The measured defect. The guard put the content back into the
        conversation itself; the next repeat must be a pointer, not a copy."""
        bench.read()
        bench.compact()
        resent = bench.read()
        assert bench.carries_content(resent)

        third = bench.read()
        assert third.get("repeat_guard") == "answered"
        assert not bench.carries_content(third), (
            "the guard resent a file it had just put in the conversation itself"
        )
        assert bench.size(third) < bench.size(resent) // 10

    def test_five_repeats_of_one_file_cost_one_copy_of_it(self, bench: Any) -> None:
        """`sam3_image.py`, as it actually ran: read once, compacted away, then
        read five more times. One resend is the budget; five is the incident."""
        bench.read()
        bench.compact()
        repeats = [bench.read() for _ in range(5)]

        copies = [r for r in repeats if bench.carries_content(r)]
        assert len(copies) == 1, f"{len(copies)} copies of the file, not 1"

    def test_the_short_answer_still_names_the_step_that_read_it(self, bench: Any) -> None:
        """A pointer that does not say where to look is not an answer."""
        bench.read()
        bench.compact()
        bench.read()
        third = bench.read()
        assert third["unchanged_since_step"] == 1
        assert "unchanged" in third["note"]

    def test_a_change_on_disk_still_beats_everything(self, bench: Any) -> None:
        """The resend bookkeeping must not outlive the fingerprint it was
        taken under: an edited file is read again, whatever the guard holds."""
        bench.read()
        bench.compact()
        bench.read()
        bench.target.write_text(_BODY + "\n# edited\n", encoding="utf-8")

        after = bench.read()
        assert after.get("repeat_guard") is None
        assert after["content"].endswith("# edited\n")


class TestTheEvidenceSaysWhichKind:
    def test_a_resend_and_a_short_answer_are_told_apart_in_the_event_row(self, bench: Any) -> None:
        """A sweep counting `warned` rows cannot see that every one of them
        cost a full file. The reason has to say so — that is the positive
        control the round-1 reviewer asked for."""
        bench.read()
        bench.compact()
        bench.read()
        bench.read()

        reasons = [e["reason"] for e in bench.events if e["guardrail_name"] == "repeat_guard"]
        assert len(reasons) == 2, reasons
        assert "no longer in your context" in reasons[0]
        assert "no longer in your context" not in reasons[1]

    def test_every_decision_carries_the_run_step_tool_and_action(self, bench: Any) -> None:
        bench.read()
        bench.read()
        row = bench.events[0]
        assert row["run_id"] == bench.run_id
        assert row["tool_name"] == "read_file"
        assert row["action"] == "warned"
        assert row["step_number"] > 0
