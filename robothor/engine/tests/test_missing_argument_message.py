"""A missing argument must tell the model what to send, not just that it broke.

Walking the task lifecycle through the real registry found `delete_task`
raising KeyError('id') from a bare `args["id"]`, surfaced to the agent as

    {"error": "KeyError: 'id'", "tool_crashed": true}

while `get_task` right beside it returns

    "invalid task id '' — expected a UUID; use list_tasks or list_my_tasks
     to find real task ids"

Its comment explains the difference: LLM-hallucinated placeholder ids used to
reach the uuid-typed SQL parameter verbatim, so that one handler grew a
boundary guard. Forty-one bare `args[...]` accesses across the handler package
did not.

The distinction matters because the reader is a model. A named missing
argument is recoverable on the next turn; "KeyError" is a dead end that burns
an iteration and can derail a run. Fixing this per-handler would be forty-one
edits and a forty-second waiting to be written, so it is fixed once, where
every handler's exception already passes through.

Deliberately not silent: it still logs, still audits as an error, and still
marks the call failed. It only stops being cryptic.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.dispatch import _describe_exception


def test_a_missing_argument_names_the_argument():
    msg, crashed = _describe_exception(KeyError("id"))
    assert "id" in msg
    assert "missing" in msg.lower(), f"not actionable: {msg!r}"
    assert crashed is False, "a bad call is not an engine crash"


def test_the_message_tells_the_model_what_to_do():
    msg, _ = _describe_exception(KeyError("task_id"))
    assert "argument" in msg.lower()
    assert "KeyError" not in msg, "the raw exception name is not guidance"


def test_a_real_crash_is_still_reported_as_a_crash():
    """The guard must be narrow: only a missing argument is reclassified."""
    msg, crashed = _describe_exception(ZeroDivisionError("division by zero"))
    assert crashed is True
    assert "ZeroDivisionError" in msg


def test_a_keyerror_that_is_not_an_argument_still_crashes():
    """A KeyError from deep inside a handler is a real bug, not a bad call.

    Distinguished by shape: an argument lookup fails on a short identifier-like
    key. Anything else is left alone rather than being quietly excused.
    """
    msg, crashed = _describe_exception(KeyError("some internal cache slot that is not an arg name"))
    assert crashed is True


@pytest.mark.parametrize("key", ["id", "task_id", "person_id", "path", "query"])
def test_common_argument_names_are_recognised(key):
    msg, crashed = _describe_exception(KeyError(key))
    assert crashed is False and key in msg
