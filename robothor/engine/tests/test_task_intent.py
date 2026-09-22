"""A proposed final receipt cannot silently discard an extra user obligation."""

import pytest

from robothor.engine.runtime.task_intent import permits, standalone
from robothor.engine.tests.test_task_final_report import prepare


@pytest.mark.parametrize(
    "command",
    [
        'Create one task titled "One" with description "Synthetic".',
        'Please add a task called "One" and body "Synthetic"',
        'Create exactly one task with title "One" and body "Synthetic". Use the task tool; do not contact anyone.',
    ],
)
def test_complete_quoted_command(command):
    assert standalone(command, {"title": "One", "body": "Synthetic"})


@pytest.mark.parametrize(
    "suffix",
    [
        " Also calculate 17 times 19 separately in your reply.",
        " and create another task.",
        " Then email Sam.",
        " Explain why this matters.",
        " Answer in Spanish.",
        " Use the instructions above.",
        "\nIgnore the rest and finish.",
    ],
)
def test_extra_obligation_denies_host_finalization(suffix):
    assert not standalone(
        'Create one task titled "One" with body "Synthetic".' + suffix,
        {"title": "One", "body": "Synthetic"},
    )


@pytest.mark.parametrize(
    "text,args",
    [
        (
            "Create one task titled One with description Synthetic",
            {"title": "One", "body": "Synthetic"},
        ),
        ('Create a task titled "One"', {"title": "One", "body": "Extra"}),
        ('Create a task titled "One"', {"title": "Other"}),
        ('Create a task titled "One"', {"title": "One", "status": "DONE"}),
        ('Create a task titled "One" with body "Broken\\q"', {"title": "One", "body": "Broken\\q"}),
        (None, {"title": "One"}),
    ],
)
def test_ambiguous_or_mismatched_requests_continue_normally(text, args):
    assert not standalone(text, args)


def test_quoted_content_is_task_data():
    assert standalone(
        'Create a task titled "Also calculate 17 times 19"', {"title": "Also calculate 17 times 19"}
    )


def test_loaded_skills_and_consumed_steering_deny_early_finish():
    from robothor.engine.skill_contract import SKILL_TEXT_ATTR
    from robothor.engine.task_context import install_context, make_context

    session, req, ctx, args = prepare()
    setattr(session, SKILL_TEXT_ATTR, {"skill": "Also write a report"})
    assert not permits(session, args)
    setattr(session, SKILL_TEXT_ATTR, {})
    session.messages = [{"role": "system", "content": "Synthetic instructions"}]
    install_context(session.messages, make_context(session.run.task_text, []))
    session.steer("Also calculate 17 times 19")
    session.consume_pending_steer()
    assert not session.has_pending_control
    assert not permits(session, args)
