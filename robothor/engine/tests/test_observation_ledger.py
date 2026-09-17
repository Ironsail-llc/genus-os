"""A run knows what it did not finish reading, and says so before it answers.

Two measured failures, one shape. On `task_2` a listing was cut and the agent
reported over 12 of 20 records as though they were all of them. On `task_5` the
agent made nineteen state-changing calls, printed only each one's `status`, and
never read the inbox it had just changed — the grader scored its report 1.0 for
quality and 0.0 for accuracy in the same pass.

Both are the same defect from the engine's side: nothing anywhere asked whether
the answer about to be written was based on everything the run actually saw.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from robothor.engine.act_observe import (
    CHANGE,
    NEITHER,
    READ,
    act_observe_note,
    classify,
    response_evidence,
    source_tokens,
    unread_proxy_responses,
)
from robothor.engine.observation_ledger import ObservationLedger, ledger_for
from robothor.engine.observation_notes import observation_notes, unread_observation_hold

if TYPE_CHECKING:
    from pathlib import Path

API = "http://service.invalid:9110/inbox/messages"
SEND = "http://service.invalid:9110/inbox/send"
READ_ONLY = frozenset({"read_file", "list_directory", "get_inbox"})


class _Step:
    def __init__(self, number: int, tool: str, args: dict, output: Any) -> None:
        self.step_number = number
        self.tool_name = tool
        self.tool_input = args
        self.tool_output = output


class _Session:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.run = type("R", (), {"id": "run-1"})()


def _cut(total: int = 12_431, path: str = "/w/.robothor/exec/run-1__stdout__abc.txt") -> dict:
    return {
        "stdout": "x" * 4_000,
        "stdout_truncated": True,
        "stdout_chars": total,
        "stdout_path": path,
        "exit_code": 0,
    }


class TestClassification:
    def test_a_declared_read_only_tool_reads(self) -> None:
        assert classify("get_inbox", {}, READ_ONLY) == READ

    def test_a_curl_get_reads(self) -> None:
        assert classify("exec", {"command": f"curl -s {API}"}, READ_ONLY) == READ

    def test_an_explicit_post_changes(self) -> None:
        assert classify("exec", {"command": f"curl -X POST {SEND} -d '{{}}'"}, READ_ONLY) == CHANGE

    def test_curl_with_a_body_changes_even_without_the_verb(self) -> None:
        """curl with `--data` is a POST whether or not `-X` says so, which is
        how the measured run sent nineteen messages with the string POST
        nowhere near some of them."""
        assert classify("exec", {"command": f"curl {SEND} --data @body.json"}, READ_ONLY) == CHANGE

    def test_a_requests_post_inside_a_snippet_changes(self) -> None:
        code = f"import requests\nfor m in msgs:\n    requests.post('{SEND}', json=m)\n"
        assert classify("execute_code", {"code": code}, READ_ONLY) == CHANGE

    def test_a_local_command_is_neither(self) -> None:
        """Writing a local file changes nothing an earlier observation was
        about. A control that fired on `ls` would be ignored within a run."""
        assert classify("exec", {"command": "wc -l notes"}, READ_ONLY) == NEITHER

    def test_a_send_tool_changes_by_its_name(self) -> None:
        assert classify("send_notification", {"body": "hi"}, READ_ONLY) == CHANGE

    def test_an_unknown_local_tool_is_neither(self) -> None:
        """Conservative in the one direction that matters: a false 'you changed
        something' teaches an agent to ignore the note."""
        assert classify("todo_write", {"items": []}, READ_ONLY) == NEITHER

    def test_the_runs_own_deliverable_write_is_not_a_state_change(self) -> None:
        """Hostile review I1. Every WildClaw task writes its answer to an
        absolute path, `source_tokens` counted anything with a slash, and the
        note therefore fired on essentially every run telling the agent to go
        and re-read the file it had just written. Advice about nothing, and an
        `act_observe` evidence column that is non-zero on every run carries no
        signal at all."""
        args = {"path": "/tmp_workspace/results/results.md", "content": "# Report"}
        assert classify("write_file", args, READ_ONLY) == NEITHER

    def test_a_relative_and_an_absolute_write_classify_the_same(self) -> None:
        """The first cut answered `neither` for a relative path and `change`
        for an absolute one — the same call, two answers, decided by spelling."""
        for path in ("/tmp_workspace/results/results.md", "results/results.md"):
            assert classify("write_file", {"path": path, "content": "x"}, READ_ONLY) == NEITHER

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("create_task", {"title": "Follow up with the vendor"}),
            ("update_person", {"person_id": "p1", "email": "someone@example.com"}),
            ("delete_note", {"note_id": "n1"}),
            ("resolve_task", {"task_id": "t1"}),
            ("memory_block_write", {"block": "identity", "content": "x"}),
            ("gws_gmail_reply", {"thread_id": "t", "body": "ok"}),
            ("store_memory", {"text": "remember this"}),
        ],
    )
    def test_a_real_crm_or_messaging_write_is_a_state_change(self, tool: str, args: dict) -> None:
        """None of these names a host in its arguments, so a target-only rule
        would miss every one of them. The CRM row, the mailbox and the memory
        block are all state an earlier observation was about."""
        assert classify(tool, args, READ_ONLY) == CHANGE

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("tool_search", {"query": "email"}),
            ("skill_view", {"name": "triage"}),
            ("invoke_skill", {"name": "triage"}),
            ("search_files", {"pattern": "TODO", "path": "/w/src"}),
            ("wait_seconds", {"seconds": 5}),
        ],
    )
    def test_a_local_or_meta_call_is_neither(self, tool: str, args: dict) -> None:
        assert classify(tool, args, READ_ONLY) == NEITHER

    def test_a_local_shell_command_naming_a_path_is_neither(self) -> None:
        """`wc -l /tmp_workspace/results/results.md` reaches nothing."""
        args = {"command": "wc -l /tmp_workspace/results/results.md"}
        assert classify("exec", args, READ_ONLY) == NEITHER

    def test_a_bare_host_and_port_still_counts_as_remote(self) -> None:
        """The mock services every graded task talks to are reached as
        `localhost:9110/...` — no dot, and often no scheme."""
        args = {"command": "curl -s -X POST localhost:9110/inbox/send --data @m.json"}
        assert classify("exec", args, READ_ONLY) == CHANGE

    def test_source_tokens_find_the_endpoint(self) -> None:
        assert API in source_tokens({"command": f"curl -s {API} | head"})

    def test_source_tokens_ignore_bare_words(self) -> None:
        assert source_tokens({"command": "echo hello --quiet"}) == frozenset()


def _with_spill(tmp_path: Path) -> tuple[ObservationLedger, str]:
    """A ledger holding one REAL truncation, with the spill file on disk.

    Real, because the read-back check resolves the path and requires the file
    to exist — a fixture built from a made-up path would certify nothing.
    """
    from robothor.engine.exec_spill import shape_exec_result

    out = shape_exec_result(
        {"stdout": "x" * 12_431, "stderr": "", "exit_code": 0},
        workspace=tmp_path,
        run_id="run-1",
    )
    ledger = ObservationLedger()
    ledger.record(11, "exec", {"command": f"curl -s {API}"}, out)
    assert len(ledger.unresolved()) == 1
    return ledger, out["stdout_path"]


class TestTheTruncationLedger:
    def test_a_cut_result_registers(self) -> None:
        ledger = ObservationLedger()
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        assert len(ledger.unresolved()) == 1
        entry = ledger.unresolved()[0]
        assert entry.step == 11
        assert entry.chars_total == 12_431
        assert entry.chars_shown == 4_000

    def test_an_uncut_result_registers_nothing(self) -> None:
        ledger = ObservationLedger()
        ledger.record(11, "exec", {"command": f"curl {API}"}, {"stdout": "short", "exit_code": 0})
        assert ledger.unresolved() == []

    def test_reading_the_spill_back_resolves_it(self, tmp_path: Path) -> None:
        ledger, path = _with_spill(tmp_path)
        ledger.record(12, "read_file", {"path": path}, {"content": "..."})
        assert ledger.unresolved() == []

    @pytest.mark.parametrize(
        "tool,args",
        [
            ("exec", {"command": "head -c 500 {path}"}),
            ("exec", {"command": "cat {path}"}),
            ("exec", {"command": "grep total {path} | tail -3"}),
            ("exec", {"command": "sed -n '4000,$p' {path}"}),
            ("execute_code", {"code": "print(open('{path}').read())"}),
        ],
    )
    def test_reading_the_spill_back_through_the_shell_resolves_it(
        self, tmp_path: Path, tool: str, args: dict
    ) -> None:
        """Hostile review I4. Resolution used to be asked only inside the READ
        branch, and `cat`/`head`/`grep` of a local path classify as `neither` —
        they reach nothing remote. So the most natural read-back in the world
        left the entry open, and at `enforce` the run was then handed a note
        telling it to declare it never read something it had just read."""
        ledger, path = _with_spill(tmp_path)
        filled = {k: v.format(path=path) for k, v in args.items()}
        ledger.record(12, tool, filled, {"stdout": "the rest of it", "exit_code": 0})
        assert ledger.unresolved() == []

    def test_a_snippet_that_only_mentions_the_path_does_not_resolve_it(
        self, tmp_path: Path
    ) -> None:
        """A path inside a comment is not a path the call is reading."""
        ledger, path = _with_spill(tmp_path)
        ledger.record(
            12,
            "execute_code",
            {"code": f"# TODO: read {path}\nprint('later')\n"},
            {"stdout": "later", "exit_code": 0},
        )
        assert len(ledger.unresolved()) == 1

    def test_both_streams_of_one_step_are_tracked_separately(self, tmp_path: Path) -> None:
        """One `exec` can cut BOTH its streams. The entries were keyed by step
        number alone, so reading the stdout spill back silently cleared the
        stderr one as well (hostile review M2)."""
        from robothor.engine.exec_spill import shape_exec_result

        out = shape_exec_result(
            {"stdout": "x" * 12_000, "stderr": "e" * 9_000, "exit_code": 1},
            workspace=tmp_path,
            run_id="run-1",
        )
        ledger = ObservationLedger()
        ledger.record(11, "exec", {"command": f"curl {API}"}, out)
        assert len(ledger.unresolved()) == 2

        ledger.record(12, "read_file", {"path": out["stdout_path"]}, {"content": "whole"})
        remaining = ledger.unresolved()
        assert [entry.stream for entry in remaining] == ["stderr"]

    def test_a_narrower_query_on_the_same_target_resolves_it(self, tmp_path: Path) -> None:
        """Hostile review I5. `…/messages?limit=2` is what "re-run it narrower"
        MEANS, and a literal token comparison made the narrowing itself the
        reason the entry stayed open."""
        ledger, _path = _with_spill(tmp_path)
        ledger.record(
            12,
            "exec",
            {"command": f"curl -s {API}?limit=2"},
            {"stdout": "two records", "exit_code": 0},
        )
        assert ledger.unresolved() == []

    def test_a_rerun_that_observed_nothing_does_not_resolve_it(self, tmp_path: Path) -> None:
        """`curl -s -o /dev/null <url>`: same tool, same target, nothing
        truncated — and the model was shown nothing at all. It used to resolve
        the entry, which is the weaker half of "cannot be satisfied by merely
        mentioning it"."""
        ledger, _path = _with_spill(tmp_path)
        ledger.record(
            12,
            "exec",
            {"command": f"curl -s -o /dev/null {API}"},
            {"stdout": "", "stderr": "", "exit_code": 0},
        )
        assert len(ledger.unresolved()) == 1

    def test_a_narrower_rerun_of_the_same_source_resolves_it(self) -> None:
        ledger = ObservationLedger()
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        ledger.record(
            12,
            "exec",
            {"command": f"curl {API} | jq -c '.records[15:]'"},
            {"stdout": "whole", "exit_code": 0},
        )
        assert ledger.unresolved() == []

    def test_a_rerun_that_was_cut_again_does_not_resolve_it(self) -> None:
        ledger = ObservationLedger()
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        ledger.record(12, "exec", {"command": f"curl {API} | tail"}, _cut())
        assert len(ledger.unresolved()) == 2

    def test_reading_something_else_does_not_resolve_it(self) -> None:
        """The entry is about a specific source. A read of an unrelated file is
        not an answer to it."""
        ledger = ObservationLedger()
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        ledger.record(12, "read_file", {"path": "/w/notes.md"}, {"content": "..."})
        assert len(ledger.unresolved()) == 1

    def test_mentioning_the_path_in_a_later_command_is_not_reading_it(self) -> None:
        """The hostile case. An agent that writes "output truncated, see
        <path>" in its report has demonstrated that it read the marker, not
        that it read the output."""
        ledger = ObservationLedger()
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        ledger.record(
            12,
            "write_file",
            {"path": "/w/results.md", "content": f"see {_cut()['stdout_path']}"},
            {"ok": True},
        )
        assert len(ledger.unresolved()) == 1

    def test_the_sentence_quotes_the_step_the_counts_and_the_path(self) -> None:
        ledger = ObservationLedger()
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        sentence = ledger.unresolved()[0].sentence()
        assert "step 11" in sentence
        assert "4,000 of 12,431" in sentence
        assert _cut()["stdout_path"] in sentence

    def test_an_entry_is_quoted_at_most_once(self) -> None:
        ledger = ObservationLedger()
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        assert len(ledger.take_unquoted()) == 1
        assert ledger.take_unquoted() == []


class TestActThenObserve:
    def test_sending_then_writing_without_a_read_fires(self) -> None:
        ledger = ObservationLedger()
        ledger.record(5, "exec", {"command": f"curl -X POST {SEND} -d '{{}}'"}, {"exit_code": 0})
        assert ledger.unobserved_changes()

    def test_sending_then_reading_the_same_source_does_not_fire(self) -> None:
        ledger = ObservationLedger()
        ledger.record(5, "exec", {"command": f"curl -X POST {SEND} -d '{{}}'"}, {"exit_code": 0})
        ledger.record(6, "exec", {"command": f"curl -s {SEND}"}, {"stdout": "[]", "exit_code": 0})
        assert ledger.unobserved_changes() == []

    def test_reading_an_unrelated_source_does_not_clear_it(self) -> None:
        """ "I read a file" does not make an inbox current."""
        ledger = ObservationLedger()
        ledger.record(5, "exec", {"command": f"curl -X POST {SEND} -d '{{}}'"}, {"exit_code": 0})
        ledger.record(6, "read_file", {"path": "/w/notes.md"}, {"content": "..."})
        assert ledger.unobserved_changes()

    def test_a_refused_call_changed_nothing(self) -> None:
        """A guardrail block, a timeout or a connection error leaves the world
        as it was. Counting one as a change fires the note on a run that did
        nothing — the false positive that gets a control ignored."""
        ledger = ObservationLedger()
        ledger.record(
            5,
            "send_notification",
            {"body": "hi"},
            {"error": "blocked by no_secrets_in_output", "guardrail": "no_secrets_in_output"},
        )
        assert ledger.unobserved_changes() == []

    def test_a_read_only_run_never_fires(self) -> None:
        ledger = ObservationLedger()
        ledger.record(1, "exec", {"command": f"curl -s {API}"}, {"stdout": "[]", "exit_code": 0})
        ledger.record(2, "read_file", {"path": "/w/notes.md"}, {"content": "..."})
        assert ledger.unobserved_changes() == []

    def test_the_note_names_the_count_the_tool_and_the_source(self) -> None:
        note = act_observe_note([(5, "exec", frozenset({SEND}))])
        assert "1 state-changing call" in note
        assert SEND in note
        assert "read it again" in note.lower()

    def test_no_changes_means_no_note(self) -> None:
        assert act_observe_note([]) == ""


class TestProxiedResponsesTheSnippetNeverPrinted:
    """The task-5 case as a unit test.

    The send returned `{"status": "sent", "new_reply": {...}}`; the snippet
    printed only the status. The response was on the run's step ledger and
    nowhere the model could see it.
    """

    @staticmethod
    def _send_result(body: str) -> dict:
        return {"status": "sent", "new_reply": {"text": body}}

    def test_a_response_whose_body_was_never_printed_is_counted(self) -> None:
        body = "The deadline moved to 48 hours and four accounts are affected."
        responses = [("slack_send", response_evidence(self._send_result(body)))]
        out = unread_proxy_responses(responses, "  [1/12] To: @someone -> sent\n")
        assert out["unread_responses"] == 1
        assert out["unread_response_tools"] == ["slack_send"]
        assert "genus_tools" in out["unread_response_note"]

    def test_a_response_the_snippet_printed_is_not_counted(self) -> None:
        body = "The deadline moved to 48 hours and four accounts are affected."
        responses = [("slack_send", response_evidence(self._send_result(body)))]
        assert unread_proxy_responses(responses, f"reply: {body}") == {}

    def test_a_response_with_nothing_substantial_is_never_counted(self) -> None:
        """A `{"ok": true}` carries no evidence to look for, and the absence of
        evidence must not become evidence of absence."""
        responses = [("ping", response_evidence({"ok": True, "id": 4}))]
        assert unread_proxy_responses(responses, "") == {}

    def test_no_calls_means_nothing(self) -> None:
        assert unread_proxy_responses([], "anything") == {}


@pytest.fixture
def session() -> _Session:
    return _Session()


class TestTheLadder:
    def test_off_does_nothing(self, session: _Session, monkeypatch) -> None:
        _set_modes(monkeypatch, truncation="off", act="off")
        ledger = ledger_for(session)
        assert ledger is not None
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        assert observation_notes(session) == ""
        assert unread_observation_hold(session) is False

    def test_observe_says_nothing_to_the_model(
        self, session: _Session, monkeypatch, caplog
    ) -> None:
        """This repo's rule for an observe rung: it logs what enforce would
        have done and changes nothing about the run, so a sweep comparing the
        rungs is measuring the control rather than the wording."""
        _set_modes(monkeypatch, truncation="observe", act="observe")
        ledger = ledger_for(session)
        assert ledger is not None
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        with caplog.at_level("WARNING"):
            assert observation_notes(session) == ""
        assert "truncation ledger observe" in caplog.text
        assert unread_observation_hold(session) is False
        assert session.messages == []

    def test_enforce_quotes_the_entry_once(self, session: _Session, monkeypatch) -> None:
        _set_modes(monkeypatch, truncation="enforce", act="off")
        ledger = ledger_for(session)
        assert ledger is not None
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        first = observation_notes(session)
        assert "step 11" in first
        assert observation_notes(session) == ""

    def test_both_holds_are_real_holds_and_then_the_run_ends(
        self, session: _Session, monkeypatch
    ) -> None:
        """Driven into a run that can never resolve its entry.

        BOTH holds return True. The runner's stop branch is `if nudge(...):
        continue / return`, so a False ends the run with no further LLM call
        and `get_final_text` walks back past anything appended afterwards — the
        first cut returned False on the second hold and its "honest completion"
        reached nothing but the transcript (hostile review C1).

        This is the unit-level shape. That the run's ANSWER changes is asserted
        through the real loop in
        `test_unread_observation_hold_reaches_the_answer.py`, which is where a
        control like this has to be proved.
        """
        _set_modes(monkeypatch, truncation="enforce", act="off")
        ledger = ledger_for(session)
        assert ledger is not None
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())

        assert unread_observation_hold(session) is True
        assert "never shown to you" in session.messages[-1]["content"]

        assert unread_observation_hold(session) is True
        assert "say in it what you did not read" in session.messages[-1]["content"]
        assert "step 11" in session.messages[-1]["content"]

        assert unread_observation_hold(session) is False
        assert len(session.messages) == 2  # bounded: two turns, never a loop
        assert unread_observation_hold(session) is False
        assert len(session.messages) == 2

    def test_a_resolved_entry_never_holds_the_run(
        self, session: _Session, tmp_path: Path, monkeypatch
    ) -> None:
        from robothor.engine.exec_spill import shape_exec_result

        _set_modes(monkeypatch, truncation="enforce", act="off")
        ledger = ledger_for(session)
        assert ledger is not None
        out = shape_exec_result(
            {"stdout": "x" * 12_431, "stderr": "", "exit_code": 0},
            workspace=tmp_path,
            run_id="run-1",
        )
        ledger.record(11, "exec", {"command": f"curl {API}"}, out)
        ledger.record(12, "read_file", {"path": out["stdout_path"]}, {"content": "..."})
        assert unread_observation_hold(session) is False
        assert session.messages == []

    def test_act_observe_enforce_says_it_once(self, session: _Session, monkeypatch) -> None:
        _set_modes(monkeypatch, truncation="off", act="enforce")
        ledger = ledger_for(session)
        assert ledger is not None
        ledger.record(5, "exec", {"command": f"curl -X POST {SEND} -d '{{}}'"}, {"exit_code": 0})
        assert "state-changing" in observation_notes(session)
        assert observation_notes(session) == ""


def _set_modes(monkeypatch, *, truncation: str, act: str) -> None:
    import robothor.engine.feature_flags as flags
    import robothor.engine.observation_ledger as mod

    monkeypatch.setattr(flags, "truncation_ledger_mode", lambda: truncation)
    monkeypatch.setattr(flags, "act_observe_mode", lambda: act)
    monkeypatch.setattr(mod, "_read_only", lambda: READ_ONLY)
