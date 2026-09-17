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

from typing import Any

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
from robothor.engine.observation_ledger import (
    ObservationLedger,
    ledger_for,
    observation_notes,
    unread_observation_hold,
)

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

    def test_source_tokens_find_the_endpoint(self) -> None:
        assert API in source_tokens({"command": f"curl -s {API} | head"})

    def test_source_tokens_ignore_bare_words(self) -> None:
        assert source_tokens({"command": "echo hello --quiet"}) == frozenset()


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

    def test_reading_the_spill_back_resolves_it(self) -> None:
        ledger = ObservationLedger()
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        ledger.record(12, "read_file", {"path": _cut()["stdout_path"]}, {"content": "..."})
        assert ledger.unresolved() == []

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

    def test_a_resolved_entry_never_holds_the_run(self, session: _Session, monkeypatch) -> None:
        _set_modes(monkeypatch, truncation="enforce", act="off")
        ledger = ledger_for(session)
        assert ledger is not None
        ledger.record(11, "exec", {"command": f"curl {API}"}, _cut())
        ledger.record(12, "read_file", {"path": _cut()["stdout_path"]}, {"content": "..."})
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
