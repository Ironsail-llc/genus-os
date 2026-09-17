"""A contract stated inside a loaded SKILL is still the run's contract.

MEASURED 2026-09-16, probed read-only against two real benchmark tasks under
`.venv` with `PYTHONPATH=.`:

    task_2 prompt-only    required_deliverables -> []
    task_2 prompt+skill   required_deliverables -> ['<results path>']
    task_5 prompt-only    required_deliverables -> ['<results path>']

`task_text_for_run` read the prompt and nothing else, so for a task whose
output path is stated only in its skill the contract was empty — for the
mid-run comparison, for the re-ask and for the finalizer's verdict alike. All
three read one accessor, which is what makes this one fix rather than three.
"""

from __future__ import annotations

from typing import Any

from robothor.engine.deliverable_contract import task_text_for_run
from robothor.engine.skill_contract import (
    SKILL_TEXT_MAX_CHARS,
    loaded_skill_text,
    remember_skill_text,
)

SKILL_BODY = (
    "# Inbox triage skill\n\n"
    "Read every message, then write your findings to "
    "`/tmp_workspace/results/results.md` with one section per message.\n"
)


class _Run:
    def __init__(self, task_text: str = "") -> None:
        self.task_text = task_text
        self.task_id = None
        self.tenant_id = "default"


class _Session:
    originating_message = ""


def test_a_skill_view_body_is_remembered() -> None:
    session: Any = _Session()
    remember_skill_text(session, "skill_view", {"name": "triage", "content": SKILL_BODY})
    assert "results.md" in loaded_skill_text(session)


def test_an_invoked_skill_body_is_remembered() -> None:
    session: Any = _Session()
    remember_skill_text(session, "invoke_skill", {"skill": "triage", "content": SKILL_BODY})
    assert "results.md" in loaded_skill_text(session)


def test_a_catalogue_listing_is_not_a_body() -> None:
    """`list_skills` returns metadata. A description an agent saw in a list is
    not an instruction it was given."""
    session: Any = _Session()
    remember_skill_text(session, "list_skills", {"skills": [{"content": SKILL_BODY}]})
    assert loaded_skill_text(session) == ""


def test_the_same_body_is_not_kept_twice() -> None:
    session: Any = _Session()
    for _ in range(5):
        remember_skill_text(session, "skill_view", {"content": SKILL_BODY})
    assert loaded_skill_text(session).count("Inbox triage skill") == 1


def test_what_is_kept_is_bounded() -> None:
    """A skill library is not a task description, and this text is re-scanned
    by the contract extractor on every check-in."""
    session: Any = _Session()
    for index in range(50):
        remember_skill_text(session, "skill_view", {"content": f"body {index} " + "x" * 2_000})
    assert len(loaded_skill_text(session)) <= SKILL_TEXT_MAX_CHARS


def test_a_session_that_loaded_nothing_contributes_nothing() -> None:
    assert loaded_skill_text(_Session()) == ""
    assert loaded_skill_text(None) == ""


class TestTheContractSeesIt:
    def test_the_prompt_alone_names_no_path(self) -> None:
        run = _Run("Have a look at the inbox and tell me what needs doing.")
        assert "results.md" not in task_text_for_run(run, _Session())

    def test_the_loaded_skill_puts_the_path_in_the_contract(self) -> None:
        from robothor.engine.deliverable_extract import required_deliverables

        session: Any = _Session()
        run = _Run("Have a look at the inbox and tell me what needs doing.")
        assert required_deliverables(task_text_for_run(run, session)) == []
        remember_skill_text(session, "skill_view", {"content": SKILL_BODY})
        assert required_deliverables(task_text_for_run(run, session)) == [
            "/tmp_workspace/results/results.md"
        ]

    def test_the_prompt_still_comes_first(self) -> None:
        """A skill is additional instruction, never a replacement for what the
        operator asked — so the prompt leads and the bodies follow."""
        session: Any = _Session()
        remember_skill_text(session, "skill_view", {"content": SKILL_BODY})
        text = task_text_for_run(_Run("THE PROMPT"), session)
        assert text.startswith("THE PROMPT")
        assert text.index("THE PROMPT") < text.index("Inbox triage skill")

    def test_no_run_and_no_session_is_still_the_empty_contract(self) -> None:
        assert task_text_for_run(None, None) == ""
