"""Check a tool's RESULT before the model sees it.

Extracted from the tool-execution block in `_run_loop`. The security property
is a once-vs-always pair that is easy to collapse by accident:

    redact  — EVERY time a credential is found
    notify  — ONCE per run

Collapsing it the wrong way is a real leak. If redaction were also
once-per-run, the second file containing a key would reach the model verbatim.
If notification were every time, the warning would crowd out the task.

Both halves exist because of live failures. Detection used to be a log line
alone: the platform could spot a credential in a file the agent had just read
and the agent would never know — it carried on, published it, and never warned
the user. Detection that reaches no one is the same shape as a control that
never runs. And without redaction the agent quotes the key back while
correctly explaining why the key is dangerous, landing it in the transcript,
the session store, and every log downstream of them.
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.engine.session import ENGINE_CONTEXT_ROLE

logger = logging.getLogger(__name__)

#: The one guardrail whose warning means "there is a credential in this
#: payload". Redacting on every warning would mangle legitimate output.
SENSITIVE_DATA_GUARDRAIL = "no_sensitive_data"

_NOTICE = (
    "Treat this as a credential exposure: tell the user which file "
    "or output contains it — WITHOUT repeating the value — and that "
    "it should be removed from the code and rotated. Do not commit, "
    "push, send, or otherwise publish content containing it."
)


def apply_post_execution_guardrails(
    session: Any,
    guardrail_engine: Any,
    *,
    tool_name: str,
    result: Any,
    error_msg: str | None,
    state: Any,
) -> Any:
    """Return the result the model should see — redacted if it had to be.

    Skipped entirely on a failed call: the "result" there is an error string,
    not tool output.
    """
    if not guardrail_engine or error_msg:
        return result

    verdict = guardrail_engine.check_post_execution(tool_name, result)
    if verdict.action != "warned":
        return result

    logger.warning("Guardrail warning for %s: %s", tool_name, verdict.reason)
    if verdict.guardrail_name != SENSITIVE_DATA_GUARDRAIL:
        return result

    from robothor.engine.guardrails import redact_secrets

    # Every time. The agent needs to know a credential is THERE — which file,
    # what kind — and never needs the characters.
    result = redact_secrets(result)

    # Once per run. The reason string names the KIND of credential and never
    # the value, because this message is persisted with the conversation.
    if not state.secret_notified:
        state.secret_notified = True
        session.messages.append(
            {
                "role": ENGINE_CONTEXT_ROLE,
                "content": f"[SYSTEM] {verdict.reason} {_NOTICE}",
            }
        )
    return result
